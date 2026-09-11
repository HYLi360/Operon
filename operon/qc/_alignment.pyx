# cython: language_level=3
"""Cython production backend for multiple-sequence alignment QC.

The public API, metric values, and QCError messages are required to match
the pure-Python reference implementation in ``alignment.py`` byte for byte;
parity is enforced by ``tests/regression/test_cython_alignment_parity.py``.

The hot counting loop operates on ASCII-encoded bytes in flat C arrays
indexed char-major (``residue * aln_len + column``).  Sequences containing
non-ASCII residues are counted through a Python-side per-column fallback and
merged at report time with cross-source first-seen tie-breaking, mirroring
``Counter.most_common`` insertion-order semantics.
"""

from cpython.bytes cimport PyBytes_AS_STRING
from libc.stdint cimport int32_t
from libc.stdlib cimport calloc, free, malloc
from libc.string cimport memset

from operon.errors import QCError
from operon.qc._parsers import iter_fasta
from operon.qc.alignment import (
    AlignmentQCResult,
    _format_coverage,
    _format_occupancy,
)

# GAP_CHARACTERS in the reference is exactly {"-", "."}.
_GAP_DASH = 45  # ord("-")
_GAP_DOT = 46   # ord(".")


cdef int _count_ascii_sequence(bytes sequence, int32_t *counts,
                               int32_t *first_seen, int32_t *column_nongap,
                               Py_ssize_t aln_len,
                               int32_t sequence_index) noexcept:
    cdef char *buffer = PyBytes_AS_STRING(sequence)
    cdef Py_ssize_t index, slot
    cdef unsigned char residue
    cdef int nongap = 0
    for index in range(aln_len):
        residue = <unsigned char>buffer[index]
        if residue == _GAP_DASH or residue == _GAP_DOT:
            continue
        nongap += 1
        column_nongap[index] += 1
        slot = (<Py_ssize_t>residue) * aln_len + index
        counts[slot] += 1
        if first_seen[slot] == -1:
            first_seen[slot] = sequence_index
    return nongap


def compute_alignment_qc(records):
    """Compute alignment QC from an iterable of (header, sequence) records."""
    cdef int32_t *counts = NULL
    cdef int32_t *first_seen = NULL
    cdef int32_t *column_nongap = NULL
    cdef Py_ssize_t aln_len_c = 0
    cdef Py_ssize_t index, column, residue, slot
    cdef int32_t sequence_index = 0
    sequence_rows = []
    lengths = set()
    aln_len = None
    fallback = None
    try:
        for header, sequence in records:
            lengths.add(len(sequence))
            if aln_len is None:
                aln_len = len(sequence)
                aln_len_c = aln_len
                if aln_len_c > 0:
                    counts = <int32_t *>calloc(
                        <size_t>128 * aln_len_c, sizeof(int32_t))
                    first_seen = <int32_t *>malloc(
                        <size_t>128 * aln_len_c * sizeof(int32_t))
                    column_nongap = <int32_t *>calloc(
                        aln_len_c, sizeof(int32_t))
                    if counts == NULL or first_seen == NULL or column_nongap == NULL:
                        raise MemoryError()
                    memset(first_seen, 0xFF,
                           <size_t>128 * aln_len_c * sizeof(int32_t))
            if len(sequence) != aln_len:
                continue
            try:
                encoded = sequence.encode("ascii")
            except UnicodeEncodeError:
                # Python-side fallback: count this whole sequence per column
                # so its residues (ASCII or not) never enter the C arrays.
                if fallback is None:
                    fallback = [dict() for _ in range(aln_len)]
                nongap = 0
                for index, residue_char in enumerate(sequence):
                    if residue_char != "-" and residue_char != ".":
                        nongap += 1
                        column_nongap[index] += 1
                        entry = fallback[index].get(residue_char)
                        if entry is None:
                            fallback[index][residue_char] = [1, sequence_index]
                        else:
                            entry[0] += 1
            else:
                nongap = _count_ascii_sequence(
                    encoded, counts, first_seen, column_nongap,
                    aln_len_c, sequence_index)
            sequence_rows.append({
                "safe_id": header,
                "alignment_length": aln_len,
                "non_gap_sites": nongap,
                "coverage": nongap / aln_len if aln_len else 0.0,
                "gap_fraction": 1 - nongap / aln_len if aln_len else 1.0,
            })
            sequence_index += 1
        if aln_len is None:
            raise QCError("alignment is empty")
        if len(lengths) != 1:
            raise QCError(
                f"alignment sequences have unequal lengths: {sorted(lengths)[:10]}")
        sequence_count = len(sequence_rows)
        column_rows = []
        for column in range(aln_len_c):
            merged = {}
            merged_first = {}
            for residue in range(128):
                slot = residue * aln_len_c + column
                if counts[slot] > 0:
                    symbol = chr(residue)
                    merged[symbol] = counts[slot]
                    merged_first[symbol] = first_seen[slot]
            if fallback is not None:
                for symbol, entry in fallback[column].items():
                    if symbol in merged:
                        merged[symbol] += entry[0]
                        if entry[1] < merged_first[symbol]:
                            merged_first[symbol] = entry[1]
                    else:
                        merged[symbol] = entry[0]
                        merged_first[symbol] = entry[1]
            nongap = column_nongap[column]
            # Counter.most_common(1) semantics: among residues with maximal
            # count, the winner is the one first seen earliest in the stream.
            top_symbol = None
            top_count = 0
            top_first = 0
            for symbol, count in merged.items():
                if top_symbol is None or count > top_count or (
                        count == top_count and merged_first[symbol] < top_first):
                    top_symbol = symbol
                    top_count = count
                    top_first = merged_first[symbol]
            column_rows.append({
                "column_1based": column + 1,
                "occupancy": nongap / sequence_count,
                "distinct_residues": len(merged),
                "consensus": top_symbol if top_symbol is not None else "-",
                "consensus_fraction": top_count / nongap if nongap else 0.0,
            })
        coverages = sorted(float(_format_coverage(row)) for row in sequence_rows)
        summary = {
            "sequences": sequence_count,
            "alignment_length": aln_len,
            "mean_coverage": sum(coverages) / len(coverages),
            "median_coverage": coverages[len(coverages) // 2],
            "columns_occupancy_ge_0_9": sum(
                float(_format_occupancy(row)) >= 0.9 for row in column_rows
            ),
            "columns_occupancy_ge_0_7": sum(
                float(_format_occupancy(row)) >= 0.7 for row in column_rows
            ),
        }
        return AlignmentQCResult(
            sequence_rows=sequence_rows, column_rows=column_rows, summary=summary,
        )
    finally:
        if counts != NULL:
            free(counts)
        if first_seen != NULL:
            free(first_seen)
        if column_nongap != NULL:
            free(column_nongap)


def alignment_qc(alignment_path):
    """Compute alignment QC for an aligned FASTA file."""
    return compute_alignment_qc(iter_fasta(alignment_path))
