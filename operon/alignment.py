"""Multiple-sequence alignment QC (pure Python reference implementation).

This module measures per-sequence and per-column metrics for an aligned
FASTA file and writes the classic three-file report (``sequence_qc.tsv``,
``column_qc.tsv``, ``alignment_qc.json``).  Gap characters are ``-`` and
``.``.

This is the pure-Python reference implementation.  A future Cython build
(e.g. ``operon/_alignment.pyx`` or an extension inside ``operon.qc_module``)
must produce byte-identical results; parity is enforced by regression tests.
To keep that enforceable, the core computation (``compute_alignment_qc``) is
a pure function over an iterable of ``(header, sequence)`` records with no
I/O and no side effects; all file I/O lives in the thin wrappers
``alignment_qc`` (FASTA in) and ``write_alignment_qc`` (report files out).

The algorithm is single-pass: sequences stream in one at a time and each
character incrementally updates its column's counters, so memory is
O(columns x distinct residues per column) instead of O(sequences x columns).
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from operon.errors import QCError
from operon.qc_module._parsers import iter_fasta
from operon.utils import atomic_write_text

GAP_CHARACTERS = frozenset("-.")

SEQUENCE_QC_FIELDS = ["safe_id", "alignment_length", "non_gap_sites", "coverage", "gap_fraction"]
COLUMN_QC_FIELDS = ["column_1based", "occupancy", "distinct_residues", "consensus", "consensus_fraction"]


@dataclass
class AlignmentQCResult:
    """Alignment QC metrics; numeric values stay floats until written out."""

    sequence_rows: list[dict[str, Any]] = field(default_factory=list)
    column_rows: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def compute_alignment_qc(records: Iterable[tuple[str, str]]) -> AlignmentQCResult:
    """Compute alignment QC from an iterable of (header, sequence) records."""
    sequence_rows: list[dict[str, Any]] = []
    lengths: set[int] = set()
    aln_len: int | None = None
    column_counts: list[Counter] = []
    column_nongap: list[int] = []
    for header, sequence in records:
        lengths.add(len(sequence))
        if aln_len is None:
            aln_len = len(sequence)
            column_counts = [Counter() for _ in range(aln_len)]
            column_nongap = [0] * aln_len
        if len(sequence) != aln_len:
            continue
        nongap = 0
        for index, char in enumerate(sequence):
            if char not in GAP_CHARACTERS:
                nongap += 1
                column_nongap[index] += 1
                column_counts[index][char] += 1
        sequence_rows.append({
            "safe_id": header,
            "alignment_length": aln_len,
            "non_gap_sites": nongap,
            "coverage": nongap / aln_len if aln_len else 0.0,
            "gap_fraction": 1 - nongap / aln_len if aln_len else 1.0,
        })
    if aln_len is None:
        raise QCError("alignment is empty")
    if len(lengths) != 1:
        raise QCError(f"alignment sequences have unequal lengths: {sorted(lengths)[:10]}")
    sequence_count = len(sequence_rows)
    column_rows: list[dict[str, Any]] = []
    for column in range(aln_len):
        counts = column_counts[column]
        nongap = column_nongap[column]
        top = counts.most_common(1)[0] if counts else None
        column_rows.append({
            "column_1based": column + 1,
            "occupancy": nongap / sequence_count,
            "distinct_residues": len(counts),
            "consensus": top[0] if top is not None else "-",
            "consensus_fraction": top[1] / nongap if nongap else 0.0,
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


def alignment_qc(alignment_path: str | Path) -> AlignmentQCResult:
    """Compute alignment QC for an aligned FASTA file."""
    return compute_alignment_qc(iter_fasta(alignment_path))


def _format_coverage(row: dict[str, Any]) -> str:
    return f"{row['coverage']:.6f}" if row["alignment_length"] else "0"


def _format_gap_fraction(row: dict[str, Any]) -> str:
    return f"{row['gap_fraction']:.6f}" if row["alignment_length"] else "1"


def _format_occupancy(row: dict[str, Any]) -> str:
    return f"{row['occupancy']:.6f}"


def _format_consensus_fraction(row: dict[str, Any]) -> str:
    # a column with zero non-gap residues (occupancy 0) has no consensus
    return f"{row['consensus_fraction']:.6f}" if row["occupancy"] else "0"


def _render_tsv(fields: list[str], rows: Iterator[list[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t")
    writer.writerow(fields)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _render_sequence_qc(sequence_rows: list[dict[str, Any]]) -> str:
    return _render_tsv(SEQUENCE_QC_FIELDS, (
        [
            row["safe_id"], row["alignment_length"], row["non_gap_sites"],
            _format_coverage(row), _format_gap_fraction(row),
        ]
        for row in sequence_rows
    ))


def _render_column_qc(column_rows: list[dict[str, Any]]) -> str:
    return _render_tsv(COLUMN_QC_FIELDS, (
        [
            row["column_1based"], _format_occupancy(row), row["distinct_residues"],
            row["consensus"], _format_consensus_fraction(row),
        ]
        for row in column_rows
    ))


def render_summary_json(summary: dict[str, Any]) -> str:
    """Serialize the summary exactly as the report file stores it."""
    return json.dumps(summary, indent=2) + "\n"


def write_alignment_qc(result: AlignmentQCResult, outdir: str | Path) -> None:
    """Write sequence_qc.tsv, column_qc.tsv and alignment_qc.json atomically."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(outdir / "sequence_qc.tsv", _render_sequence_qc(result.sequence_rows))
    atomic_write_text(outdir / "column_qc.tsv", _render_column_qc(result.column_rows))
    atomic_write_text(outdir / "alignment_qc.json", render_summary_json(result.summary))
