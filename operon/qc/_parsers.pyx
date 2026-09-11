# cython: language_level=3
"""Cython-accelerated production parsers for built-in QC.

The public API, metrics, and QCError messages are required to match the
pure-Python reference implementation in ``parsers.py``.  Hot loops operate on
validated ASCII bytes, while headers and GFF3 text are decoded as UTF-8.
"""

import gzip
from collections import Counter
from math import ceil
from pathlib import Path
from time import perf_counter
from urllib.parse import unquote

from cpython.bytes cimport PyBytes_AS_STRING, PyBytes_GET_SIZE
from libc.string cimport memset

from operon.errors import QCError
from operon.utils import is_gzip_path, median, pct

cdef enum:
    _CLS_INVALID = 0
    _CLS_A = 1
    _CLS_C = 2
    _CLS_G = 3
    _CLS_T = 4
    _CLS_N = 5
    _CLS_AMBIG = 6
    _CLS_GAP = 7

cdef unsigned char _BASE_CLASS[256]


def _init_base_class():
    cdef int i
    for i in range(256):
        _BASE_CLASS[i] = _CLS_INVALID
    for ch in b"RYSWKMBDHVryswkmbdhv":
        _BASE_CLASS[ch] = _CLS_AMBIG
    _BASE_CLASS[65] = _CLS_A   # A
    _BASE_CLASS[97] = _CLS_A   # a
    _BASE_CLASS[67] = _CLS_C   # C
    _BASE_CLASS[99] = _CLS_C   # c
    _BASE_CLASS[71] = _CLS_G   # G
    _BASE_CLASS[103] = _CLS_G  # g
    _BASE_CLASS[84] = _CLS_T   # T
    _BASE_CLASS[116] = _CLS_T  # t
    _BASE_CLASS[78] = _CLS_N   # N
    _BASE_CLASS[110] = _CLS_N  # n
    _BASE_CLASS[45] = _CLS_GAP  # -


_init_base_class()


def _open_binary(path):
    return gzip.open(path, "rb") if is_gzip_path(path) else open(path, "rb")


def _iter_binary_lines(path, chunk_size=1024 * 1024):
    """Yield lines without terminators, recognizing LF, CRLF, and lone CR."""
    cdef bytearray line = bytearray()
    cdef bytes chunk
    cdef Py_ssize_t start, index, size
    cdef bint skip_lf = False
    with _open_binary(path) as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            size = PyBytes_GET_SIZE(chunk)
            index = 0
            if skip_lf:
                if chunk.startswith(b"\n"):
                    index = 1
                skip_lf = False
            start = index
            while index < size:
                if chunk[index] == 10:  # LF
                    line.extend(chunk[start:index])
                    yield bytes(line)
                    line.clear()
                    index += 1
                    start = index
                elif chunk[index] == 13:  # CR or the first byte of CRLF
                    line.extend(chunk[start:index])
                    yield bytes(line)
                    line.clear()
                    if index + 1 < size and chunk[index + 1] == 10:
                        index += 2
                    else:
                        skip_lf = index + 1 == size
                        index += 1
                    start = index
                else:
                    index += 1
            line.extend(chunk[start:])
        if line:
            yield bytes(line)


def _decode_utf8(path, bytes value, context):
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QCError(f"{path}: invalid UTF-8 in {context}") from exc


def _require_ascii(path, bytes value, context):
    if not value.isascii():
        raise QCError(f"{path}: non-ASCII data in {context}")


def _header_is_circular(header):
    for token in header.lower().split():
        normalized = token.strip(",;|[](){}")
        if normalized == "circular" or normalized in {"topology=circular", "topology:circular"}:
            return True
    return False


def _resolve_phred_offset(phred_offset, min_qual, max_qual):
    if phred_offset in {33, "33"}:
        return 33, "sanger_phred33"
    if phred_offset in {64, "64"}:
        if min_qual < 64:
            raise QCError("FASTQ quality data contains characters below the phred+64 minimum")
        return 64, "illumina_phred64"
    if phred_offset != "auto":
        raise ValueError("phred_offset must be 33, 64, or 'auto'")
    if max_qual < 0 or min_qual < 64:
        return 33, "sanger_phred33"
    return 33, "ambiguous_assumed_phred33"


def _nx_from_histogram(length_counts, total_bases, fraction):
    if not length_counts:
        return 0.0, 0
    target = total_bases * fraction
    cumulative = 0
    record_count = 0
    for length in sorted(length_counts, reverse=True):
        count = length_counts[length]
        if target == 0:
            return float(length), 1
        bases = length * count
        if cumulative + bases >= target and length > 0:
            needed = ceil((target - cumulative) / length)
            return float(length), record_count + max(needed, 1)
        cumulative += bases
        record_count += count
    smallest = min(length_counts)
    return float(smallest), sum(length_counts.values())


def _n50(lengths):
    if not lengths:
        return 0.0, 0
    ordered = sorted(lengths, reverse=True)
    total = sum(ordered)
    half = total / 2.0
    cumulative = 0
    for idx, length in enumerate(ordered, start=1):
        cumulative += length
        if cumulative >= half:
            return float(length), idx
    return float(ordered[-1]), len(ordered)


cdef class _FastaReader:
    """One-pass binary FASTA reader returning (header, sequence) byte pairs."""

    cdef object lines
    cdef object path
    cdef object pending
    cdef bint has_pending

    def __init__(self, path):
        self.path = path
        self.lines = iter(_iter_binary_lines(path))
        self.pending = None
        self.has_pending = False

    def close(self):
        close = getattr(self.lines, "close", None)
        if close is not None:
            close()

    cdef tuple next_record(self):
        cdef object header
        cdef bytes raw, line, header_bytes
        parts = []
        if self.has_pending:
            header_bytes = self.pending
            self.pending = None
            self.has_pending = False
            if not header_bytes:
                raise QCError(f"{self.path}: empty FASTA header")
            header = _decode_utf8(self.path, header_bytes, "FASTA header")
            if not header.split():
                raise QCError(f"{self.path}: empty FASTA header")
        else:
            header = None
        for raw in self.lines:
            line = raw.strip()
            if not line:
                continue
            if line.startswith(b">"):
                if header is None:
                    header_bytes = line[1:].strip()
                    if not header_bytes:
                        raise QCError(f"{self.path}: empty FASTA header")
                    header = _decode_utf8(self.path, header_bytes, "FASTA header")
                    if not header.split():
                        raise QCError(f"{self.path}: empty FASTA header")
                else:
                    header_bytes = line[1:].strip()
                    self.pending = header_bytes
                    self.has_pending = True
                    return (header, b"".join(parts))
            else:
                if header is None:
                    raise QCError(f"{self.path}: sequence data before first FASTA header")
                _require_ascii(self.path, line, f"FASTA sequence {header.split()[0]!r}")
                parts.append(line)
        if header is None:
            return None
        return (header, b"".join(parts))


cdef class _FastqReader:
    """One-pass binary FASTQ reader returning (header, sequence, quality)."""

    cdef object lines
    cdef object path

    def __init__(self, path):
        self.path = path
        self.lines = iter(_iter_binary_lines(path))

    def close(self):
        close = getattr(self.lines, "close", None)
        if close is not None:
            close()

    cdef tuple next_record(self):
        cdef bytes header_b, seq_b, plus_b, qual_b
        try:
            header_b = next(self.lines)
        except StopIteration:
            return None
        header_s = _decode_utf8(self.path, header_b, "FASTQ header")
        if not header_s:
            raise QCError(f"{self.path}: blank line where FASTQ header was expected")
        if not header_s.startswith("@"):
            raise QCError(f"{self.path}: FASTQ header does not start with '@': {header_s[:50]!r}")
        if not header_s[1:].split():
            raise QCError(f"{self.path}: empty FASTQ identifier")
        try:
            seq_b = next(self.lines)
        except StopIteration as exc:
            raise QCError(f"{self.path}: truncated FASTQ record near {header_s[:50]!r} (missing sequence)") from exc
        try:
            plus_b = next(self.lines)
        except StopIteration as exc:
            raise QCError(f"{self.path}: truncated FASTQ record near {header_s[:50]!r} (missing plus line)") from exc
        try:
            qual_b = next(self.lines)
        except StopIteration as exc:
            raise QCError(f"{self.path}: truncated FASTQ record near {header_s[:50]!r} (missing quality)") from exc
        if not plus_b.startswith(b"+"):
            raise QCError(f"{self.path}: FASTQ plus line malformed near {header_s[:50]!r}")
        _require_ascii(self.path, seq_b, f"FASTQ sequence near {header_s[:50]!r}")
        _require_ascii(self.path, qual_b, f"FASTQ quality near {header_s[:50]!r}")
        if PyBytes_GET_SIZE(seq_b) != PyBytes_GET_SIZE(qual_b):
            raise QCError(
                f"{self.path}: sequence/quality length mismatch near {header_s[:50]!r} "
                f"({PyBytes_GET_SIZE(seq_b)} != {PyBytes_GET_SIZE(qual_b)})"
            )
        cdef Py_ssize_t i
        cdef unsigned char qv
        cdef char* qp = PyBytes_AS_STRING(qual_b)
        for i in range(PyBytes_GET_SIZE(qual_b)):
            qv = <unsigned char>qp[i]
            if qv < 33 or qv > 126:
                raise QCError(f"{self.path}: FASTQ quality character outside ASCII 33..126 near {header_s[:50]!r}")
        return (header_s, seq_b, qual_b)


def iter_fasta(path):
    """Yield (seqid, sequence) for plain or gzip-compressed FASTA."""
    reader = _FastaReader(path)
    try:
        while True:
            rec = reader.next_record()
            if rec is None:
                break
            header, seq_b = rec
            yield header.split()[0], seq_b.decode("ascii")
    finally:
        reader.close()


cdef class _FastaAccum:
    """Running counters for nucleotide FASTA statistics."""

    cdef list lengths
    cdef set seqids
    cdef set headers
    cdef long long total, a, c, g, t, n, ambiguous, invalid
    cdef long long empty, dup_seqid, dup_header, circular, gap_runs, gap_bases
    cdef long long current_length
    cdef bint current_in_gap

    def __init__(self):
        self.lengths = []
        self.seqids = set()
        self.headers = set()

    cdef void begin(self, str header):
        cdef str seqid = header.split()[0]
        if seqid in self.seqids:
            self.dup_seqid += 1
        self.seqids.add(seqid)
        if header in self.headers:
            self.dup_header += 1
        self.headers.add(header)
        if _header_is_circular(header):
            self.circular += 1
        self.current_length = 0
        self.current_in_gap = False

    cdef void add_chunk(self, bytes seq):
        cdef Py_ssize_t i, length = PyBytes_GET_SIZE(seq)
        cdef char* s = PyBytes_AS_STRING(seq)
        cdef unsigned char cls
        self.current_length += length
        self.total += length
        for i in range(length):
            cls = _BASE_CLASS[<unsigned char>s[i]]
            if cls == _CLS_GAP:
                self.gap_bases += 1
                if not self.current_in_gap:
                    self.gap_runs += 1
                    self.current_in_gap = True
            else:
                if cls == _CLS_N:
                    self.n += 1
                elif cls == _CLS_A:
                    self.a += 1
                elif cls == _CLS_C:
                    self.c += 1
                elif cls == _CLS_G:
                    self.g += 1
                elif cls == _CLS_T:
                    self.t += 1
                elif cls == _CLS_AMBIG:
                    self.ambiguous += 1
                else:
                    self.invalid += 1
                self.current_in_gap = False

    cdef void finish(self):
        self.lengths.append(self.current_length)
        if self.current_length == 0:
            self.empty += 1

    def stats(self):
        lengths = self.lengths
        contig_n50, contig_l50 = _n50(lengths)
        ordered = sorted(lengths, reverse=True)
        target90 = self.total * 0.9
        cumulative = 0
        contig_n90 = ordered[-1] if ordered else 0
        contig_l90 = len(ordered)
        for idx, length in enumerate(ordered, start=1):
            cumulative += length
            if cumulative >= target90:
                contig_n90 = float(length)
                contig_l90 = idx
                break
        gc_denom = self.a + self.c + self.g + self.t
        return {
            "sequence_count": len(lengths),
            "total_length": self.total,
            "min_sequence_length": min(lengths) if lengths else 0,
            "max_sequence_length": max(lengths) if lengths else 0,
            "mean_sequence_length": (self.total / len(lengths)) if lengths else 0.0,
            "median_sequence_length": median([float(x) for x in lengths]),
            "contig_n50": contig_n50,
            "contig_l50": contig_l50,
            "contig_n90": contig_n90,
            "contig_l90": contig_l90,
            "gc_percent": pct(self.g + self.c, gc_denom),
            "n_percent": pct(self.n, self.total),
            "ambiguous_base_percent": pct(self.ambiguous, self.total),
            "invalid_base_count": self.invalid,
            "gap_count": self.gap_runs,
            "gap_percent": pct(self.gap_bases, self.total),
            "empty_sequence_count": self.empty,
            "duplicate_sequence_id_count": self.dup_seqid,
            "duplicate_header_count": self.dup_header,
            "circular_sequence_count": self.circular,
        }


def fasta_stats(path):
    """Structural statistics for a nucleotide FASTA file."""
    cdef _FastaAccum acc = _FastaAccum()
    cdef bytes raw, line, header_bytes
    path = Path(path)
    header = None
    for raw in _iter_binary_lines(path):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(b">"):
            if header is not None:
                acc.finish()
            header_bytes = line[1:].strip()
            if not header_bytes:
                raise QCError(f"{path}: empty FASTA header")
            header = _decode_utf8(path, header_bytes, "FASTA header")
            if not header.split():
                raise QCError(f"{path}: empty FASTA header")
            acc.begin(header)
        else:
            if header is None:
                raise QCError(f"{path}: sequence data before first FASTA header")
            _require_ascii(path, line, f"FASTA sequence {header.split()[0]!r}")
            acc.add_chunk(line)
    if header is not None:
        acc.finish()
    return acc.stats()


def fasta_lengths(path):
    """Map seqid -> length without retaining sequence content."""
    cdef bytes raw, line, header_bytes
    cdef long long current_length = 0
    path = Path(path)
    result = {}
    header = None
    seqid = None
    for raw in _iter_binary_lines(path):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(b">"):
            if header is not None:
                result[seqid] = result.get(seqid, 0) + current_length
            header_bytes = line[1:].strip()
            if not header_bytes:
                raise QCError(f"{path}: empty FASTA header")
            header = _decode_utf8(path, header_bytes, "FASTA header")
            if not header.split():
                raise QCError(f"{path}: empty FASTA header")
            seqid = header.split()[0]
            current_length = 0
        else:
            if header is None:
                raise QCError(f"{path}: sequence data before first FASTA header")
            _require_ascii(path, line, f"FASTA sequence {seqid!r}")
            current_length += PyBytes_GET_SIZE(line)
    if header is not None:
        result[seqid] = result.get(seqid, 0) + current_length
    return result


def fasta_record_count(path):
    cdef bytes raw, line, header_bytes
    cdef long long count = 0
    path = Path(path)
    header = None
    seqid = None
    for raw in _iter_binary_lines(path):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(b">"):
            header_bytes = line[1:].strip()
            if not header_bytes:
                raise QCError(f"{path}: empty FASTA header")
            header = _decode_utf8(path, header_bytes, "FASTA header")
            if not header.split():
                raise QCError(f"{path}: empty FASTA header")
            seqid = header.split()[0]
            count += 1
        else:
            if header is None:
                raise QCError(f"{path}: sequence data before first FASTA header")
            _require_ascii(path, line, f"FASTA sequence {seqid!r}")
    return count


def iter_fastq(path):
    """Yield validated 4-line FASTQ records."""
    reader = _FastqReader(Path(path))
    try:
        while True:
            rec = reader.next_record()
            if rec is None:
                break
            header_s, seq_b, qual_b = rec
            yield {
                "id": header_s[1:].split()[0],
                "header": header_s[1:],
                "sequence": seq_b.decode("ascii"),
                "quality": qual_b.decode("ascii"),
            }
    finally:
        reader.close()


def fastq_record_count(path):
    """Count FASTQ records while performing the same structural validation."""
    cdef long long count = 0
    reader = _FastqReader(Path(path))
    try:
        while reader.next_record() is not None:
            count += 1
    finally:
        reader.close()
    return count


def fastq_stats(path, sample_size=1000000, phred_offset=33):
    """Read-level QC metrics computed without external tools."""
    cdef long long read_count = 0, total_bases = 0, gc = 0, atgc = 0
    cdef long long sampled_total = 0, sampled_unique = 0
    cdef long long q20 = 0, q30 = 0
    cdef int min_qual = 255, max_qual = -1
    cdef Py_ssize_t i, length
    cdef unsigned char cls, qv
    cdef char* s
    cdef char* qp
    cdef unsigned long long quality_histogram[127]
    if not isinstance(sample_size, int) or isinstance(sample_size, bool) or sample_size < 1:
        raise ValueError("sample_size must be a positive integer")
    memset(quality_histogram, 0, sizeof(quality_histogram))
    path = Path(path)
    length_counts = Counter()
    sampled_sequences = Counter()

    reader = _FastqReader(path)
    try:
        while True:
            rec = reader.next_record()
            if rec is None:
                break
            header_s, seq_b, qual_b = rec
            read_count += 1
            length = PyBytes_GET_SIZE(seq_b)
            total_bases += length
            length_counts[length] += 1
            s = PyBytes_AS_STRING(seq_b)
            qp = PyBytes_AS_STRING(qual_b)
            for i in range(length):
                cls = _BASE_CLASS[<unsigned char>s[i]]
                if cls == _CLS_C or cls == _CLS_G:
                    gc += 1
                if _CLS_A <= cls <= _CLS_T:
                    atgc += 1
                qv = <unsigned char>qp[i]
                if qv < min_qual:
                    min_qual = qv
                if qv > max_qual:
                    max_qual = qv
                quality_histogram[qv] += 1
            if read_count <= sample_size:
                sampled_total += 1
                sampled_sequences[seq_b] += 1
    finally:
        reader.close()

    sampled_unique = len(sampled_sequences)
    duplicate_value = pct(sampled_total - sampled_unique, sampled_total)
    overrep_count = sum(1 for count in sampled_sequences.values() if count / max(sampled_total, 1) > 0.01)

    offset, encoding = _resolve_phred_offset(phred_offset, min_qual, max_qual)
    for i in range(offset + 20, 127):
        q20 += quality_histogram[i]
    for i in range(offset + 30, 127):
        q30 += quality_histogram[i]

    read_n50, read_l50 = _nx_from_histogram(length_counts, total_bases, 0.5)
    return {
        "read_count": read_count,
        "total_bases": total_bases,
        "read_length_min": min(length_counts) if length_counts else 0,
        "read_length_max": max(length_counts) if length_counts else 0,
        "read_length_mean": (total_bases / read_count) if read_count else 0.0,
        "read_length_n50": read_n50,
        "read_length_l50": read_l50,
        "q20_percent": pct(q20, total_bases),
        "q30_percent": pct(q30, total_bases),
        "gc_percent": pct(gc, atgc),
        "quality_encoding": encoding,
        "duplicate_percent": duplicate_value,
        "duplicate_sampled_read_count": sampled_total,
        "duplicate_is_sampled": read_count > sample_size,
        "duplicate_sampling_strategy": "first_n",
        "overrepresented_sequence_count": overrep_count,
        "adapter_contamination_percent": None,
    }


def parse_attributes(attribute_string):
    """Parse GFF3 column 9 attributes (percent-decoded)."""
    attrs = {}
    if attribute_string in {"", "."}:
        return attrs
    for chunk in attribute_string.split(";"):
        if not chunk:
            continue
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = unquote(key.strip())
        value = unquote(value.strip())
        if key:
            attrs[key] = value
    return attrs


cdef tuple _gff_ascii_id_parent(bytes attribute_bytes):
    """Return ID/Parent presence and values without decoding common ASCII attributes."""
    if attribute_bytes in {b"", b"."}:
        return False, None, False, None
    if b"%" in attribute_bytes:
        attrs = parse_attributes(attribute_bytes.decode("ascii"))
        return (
            "ID" in attrs, attrs.get("ID").encode("utf-8") if "ID" in attrs else None,
            "Parent" in attrs,
            attrs.get("Parent").encode("utf-8") if "Parent" in attrs else None,
        )
    has_id = False
    id_value = None
    has_parent = False
    parent_value = None
    for chunk in attribute_bytes.split(b";"):
        if not chunk or b"=" not in chunk:
            continue
        key, value = chunk.split(b"=", 1)
        key = key.strip()
        if key == b"ID":
            has_id = True
            id_value = value.strip()
        elif key == b"Parent":
            has_parent = True
            parent_value = value.strip()
    return has_id, id_value, has_parent, parent_value


def gff3_stats(path, fasta_path=None, timings=None, fasta_lengths_map=None):
    """Structural GFF3 validation metrics.

    With `fasta_path` or a precomputed `fasta_lengths_map`, seqid existence and
    end <= sequence length are checked.
    ID/Parent integrity is always checked.  When supplied, ``timings`` is
    populated with non-overlapping diagnostic durations in seconds.
    """
    path = Path(path)
    if fasta_path is not None and fasta_lengths_map is not None:
        raise ValueError("provide either fasta_path or fasta_lengths_map, not both")
    has_length_source = fasta_lengths_map is not None or bool(fasta_path)
    if fasta_lengths_map is not None:
        lengths = fasta_lengths_map
    elif fasta_path:
        lengths_started = perf_counter()
        try:
            lengths = fasta_lengths(fasta_path)
        finally:
            if timings is not None:
                timings["assembly_fasta_lengths"] = perf_counter() - lengths_started
    else:
        lengths = {}
    if has_length_source:
        prepare_started = perf_counter()
        try:
            lengths_bytes = {key.encode("utf-8"): value for key, value in lengths.items()}
        finally:
            if timings is not None:
                timings["assembly_fasta_length_map_prepare"] = perf_counter() - prepare_started
    else:
        lengths_bytes = {}
    feature_types = set()
    feature_count = 0
    gene_count = 0
    mrna_count = 0
    exon_count = 0
    ids = set()
    duplicate_id = 0
    parent_refs = {}
    missing_id = 0
    coordinate_errors = 0
    seqid_mismatch = 0
    end_beyond_seq = 0
    cds_phase0 = 0
    cds_not_multiple3 = 0
    cds_count = 0
    directives = 0
    seqids = set()
    scan_started = perf_counter()
    try:
        for raw in _iter_binary_lines(path):
            if raw.isascii():
                if not raw.strip():
                    continue
                if raw.startswith(b"##FASTA"):
                    break
                if raw.startswith(b"#"):
                    directives += 1
                    continue
                if raw.startswith(b">"):
                    continue
                fields = raw.split(b"\t")
                if len(fields) != 9:
                    coordinate_errors += 1
                    continue
                seqid, source, feature_type, start_s, end_s, score, strand, phase_s, attr_s = fields
                has_id, id_value, has_parent, parent_value = _gff_ascii_id_parent(attr_s)
            else:
                line = _decode_utf8(path, raw, "GFF3 line")
                if not line.strip():
                    continue
                if line.startswith("##FASTA"):
                    break
                if line.startswith("#"):
                    directives += 1
                    continue
                if line.startswith(">"):
                    continue
                fields = line.split("\t")
                if len(fields) != 9:
                    coordinate_errors += 1
                    continue
                seqid_s, source, feature_type_s, start_s, end_s, score, strand, phase_s, attr_s = fields
                seqid = seqid_s.encode("utf-8")
                feature_type = feature_type_s.encode("utf-8")
                attrs = parse_attributes(attr_s)
                has_id = "ID" in attrs
                id_value = attrs.get("ID").encode("utf-8") if has_id else None
                has_parent = "Parent" in attrs
                parent_value = attrs.get("Parent").encode("utf-8") if has_parent else None
            seqids.add(seqid)
            feature_types.add(feature_type)
            feature_count += 1
            if feature_type == b"gene":
                gene_count += 1
            elif feature_type == b"mRNA" or feature_type == b"transcript":
                mrna_count += 1
            elif feature_type == b"exon":
                exon_count += 1
            try:
                start = int(start_s)
                end = int(end_s)
            except ValueError:
                coordinate_errors += 1
                continue
            if start < 1 or end < start:
                coordinate_errors += 1
            if lengths_bytes and seqid not in lengths_bytes:
                seqid_mismatch += 1
            elif lengths_bytes and end > lengths_bytes[seqid]:
                end_beyond_seq += 1
                coordinate_errors += 1
            if not has_id:
                missing_id += 1
            else:
                if id_value in ids:
                    duplicate_id += 1
                ids.add(id_value)
            if has_parent:
                for parent in parent_value.split(b","):
                    parent = parent.strip()
                    if parent:
                        parent_refs[parent] = parent_refs.get(parent, 0) + 1
            if feature_type == b"CDS":
                cds_count += 1
                if (end - start + 1) % 3 != 0:
                    cds_not_multiple3 += 1
                try:
                    phase = int(phase_s)
                except ValueError:
                    phase = -1
                if phase == 0:
                    cds_phase0 += 1
    finally:
        if timings is not None:
            timings["gff3_scan"] = perf_counter() - scan_started

    finalize_started = perf_counter()
    try:
        missing_parent = sum(count for parent, count in parent_refs.items() if parent not in ids)
        result = {
            "directive_count": directives,
            "feature_count": feature_count,
            "gene_count": gene_count,
            "mrna_count": mrna_count,
            "cds_count": cds_count,
            "exon_count": exon_count,
            "feature_type_count": len(feature_types),
            "seqid_count": len(seqids),
            "seqid_mismatch_count": seqid_mismatch,
            "end_beyond_sequence_count": end_beyond_seq,
            "coordinate_error_count": coordinate_errors,
            "missing_id_count": missing_id,
            "duplicate_id_count": duplicate_id,
            "missing_parent_count": missing_parent,
            "cds_length_multiple3_percent": pct(cds_count - cds_not_multiple3, cds_count),
            "cds_phase0_percent": pct(cds_phase0, cds_count),
            "cds_not_multiple3_count": cds_not_multiple3,
        }
    finally:
        if timings is not None:
            timings["gff3_finalize"] = perf_counter() - finalize_started
    return result


def protein_stats(path, cds_count=None):
    """Protein FASTA sanity metrics (X content, duplicate IDs, stop codons)."""
    protein_ids = set()
    cdef long long duplicate = 0, total_aa = 0, x_count = 0, internal_stop = 0
    cdef long long missing_start = 0, missing_stop = 0, count = 0, empty = 0
    cdef Py_ssize_t i, length
    cdef char* s
    cdef unsigned char ch

    reader = _FastaReader(path)
    try:
        while True:
            rec = reader.next_record()
            if rec is None:
                break
            header, seq_b = rec
            seqid = header.split()[0]
            count += 1
            if seqid in protein_ids:
                duplicate += 1
            protein_ids.add(seqid)
            length = PyBytes_GET_SIZE(seq_b)
            if length == 0:
                empty += 1
                continue
            s = PyBytes_AS_STRING(seq_b)
            if s[0] != b"M"[0] and s[0] != b"m"[0]:
                missing_start += 1
            if s[length - 1] != b"*"[0]:
                missing_stop += 1
            total_aa += length
            for i in range(length):
                ch = <unsigned char>s[i]
                if ch == 88 or ch == 120:  # X / x
                    x_count += 1
                elif ch == 42:  # *
                    internal_stop += 1
            # terminal * should not count as internal stop
            if s[length - 1] == b"*"[0]:
                internal_stop -= 1
    finally:
        reader.close()

    result = {
        "protein_count": count,
        "protein_duplicate_id_count": duplicate,
        "protein_empty_count": empty,
        "protein_x_percent": pct(x_count, total_aa),
        "protein_internal_stop_count": max(internal_stop, 0),
        "protein_missing_start_count": missing_start,
        "protein_missing_stop_count": missing_stop,
    }
    if cds_count is not None:
        result["cds_protein_count_match"] = 1 if count == cds_count else 0
    return result
