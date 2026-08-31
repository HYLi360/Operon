"""Reference streaming parsers for the built-in QC stages.

The Cython extension is the production implementation.  This module defines
its required behavior and remains deliberately dependency-free so regression
tests can compare both backends exactly.
"""

from __future__ import annotations

from collections import Counter
from math import ceil
from pathlib import Path
from time import perf_counter
from typing import Any, Iterator
from urllib.parse import unquote

from operon.errors import QCError
from operon.utils import is_gzip_path, median, pct

DNA_BASES = set("ACGTN")
IUPAC_DNA = set("ACGTRYSWKMBDHVN")
PROTEIN_AA = set("ACDEFGHIKLMNPQRSTVWY*X")


def _open_binary(path: str | Path):
    import gzip

    return gzip.open(path, "rb") if is_gzip_path(path) else open(path, "rb")


def _iter_binary_lines(path: str | Path, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
    """Yield lines without terminators, recognizing LF, CRLF, and lone CR."""
    with _open_binary(path) as handle:
        line = bytearray()
        skip_lf = False
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            size = len(chunk)
            index = 0
            if skip_lf:
                if chunk.startswith(b"\n"):
                    index = 1
                skip_lf = False
            start = index
            while index < size:
                char = chunk[index]
                if char == 10:  # LF
                    line.extend(chunk[start:index])
                    yield bytes(line)
                    line.clear()
                    index += 1
                    start = index
                elif char == 13:  # CR or the first byte of CRLF
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


def _decode_utf8(path: Path, value: bytes, context: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise QCError(f"{path}: invalid UTF-8 in {context}") from exc


def _require_ascii(path: Path, value: bytes, context: str) -> None:
    if not value.isascii():
        raise QCError(f"{path}: non-ASCII data in {context}")


def _header_is_circular(header: str) -> bool:
    for token in header.lower().split():
        normalized = token.strip(",;|[](){}")
        if normalized == "circular" or normalized in {"topology=circular", "topology:circular"}:
            return True
    return False


def _iter_fasta_records(path: str | Path) -> Iterator[tuple[str, str, bytes]]:
    path = Path(path)
    header: str | None = None
    seqid: str | None = None
    parts: list[bytes] = []
    for raw in _iter_binary_lines(path):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(b">"):
            if header is not None:
                assert seqid is not None
                yield header, seqid, b"".join(parts)
            header_bytes = line[1:].strip()
            if not header_bytes:
                raise QCError(f"{path}: empty FASTA header")
            header = _decode_utf8(path, header_bytes, "FASTA header")
            fields = header.split()
            if not fields:
                raise QCError(f"{path}: empty FASTA header")
            seqid = fields[0]
            parts = []
        else:
            if header is None:
                raise QCError(f"{path}: sequence data before first FASTA header")
            assert seqid is not None
            _require_ascii(path, line, f"FASTA sequence {seqid!r}")
            parts.append(line)
    if header is not None:
        assert seqid is not None
        yield header, seqid, b"".join(parts)


def iter_fasta(path: str | Path) -> Iterator[tuple[str, str]]:
    """Yield (seqid, sequence) for plain or gzip-compressed FASTA."""
    for _header, seqid, sequence in _iter_fasta_records(path):
        yield seqid, sequence.decode("ascii")


def _n50(lengths: list[int]) -> tuple[float, int]:
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


def fasta_stats(path: str | Path) -> dict[str, Any]:
    """Structural statistics for a nucleotide FASTA file."""
    path = Path(path)
    lengths: list[int] = []
    seqids: set[str] = set()
    headers: set[str] = set()
    total = 0
    a = c = g = t = n = ambiguous = invalid = 0
    empty = 0
    duplicate_seqid = 0
    duplicate_header = 0
    circular = 0
    gap_runs = 0
    for header, seqid, sequence_bytes in _iter_fasta_records(path):
        sequence = sequence_bytes.decode("ascii")
        if seqid in seqids:
            duplicate_seqid += 1
        seqids.add(seqid)
        if header in headers:
            duplicate_header += 1
        headers.add(header)
        lengths.append(len(sequence))
        total += len(sequence)
        if len(sequence) == 0:
            empty += 1
        if _header_is_circular(header):
            circular += 1
        in_gap = False
        for base in sequence.upper():
            if base == "A":
                a += 1
            elif base == "C":
                c += 1
            elif base == "G":
                g += 1
            elif base == "T":
                t += 1
            elif base == "N":
                n += 1
                if not in_gap:
                    gap_runs += 1
                    in_gap = True
                continue
            else:
                if base in IUPAC_DNA:
                    ambiguous += 1
                else:
                    invalid += 1
            in_gap = False
    contig_n50, contig_l50 = _n50(lengths)
    # N90/L90
    ordered = sorted(lengths, reverse=True)
    target90 = total * 0.9
    cumulative = 0
    contig_n90 = ordered[-1] if ordered else 0
    contig_l90 = len(ordered)
    for idx, length in enumerate(ordered, start=1):
        cumulative += length
        if cumulative >= target90:
            contig_n90 = float(length)
            contig_l90 = idx
            break
    gc_denom = a + c + g + t
    return {
        "sequence_count": len(lengths),
        "total_length": total,
        "min_sequence_length": min(lengths) if lengths else 0,
        "max_sequence_length": max(lengths) if lengths else 0,
        "mean_sequence_length": (total / len(lengths)) if lengths else 0.0,
        "median_sequence_length": median([float(x) for x in lengths]),
        "contig_n50": contig_n50,
        "contig_l50": contig_l50,
        "contig_n90": contig_n90,
        "contig_l90": contig_l90,
        "gc_percent": pct(g + c, gc_denom),
        "n_percent": pct(n, total),
        "ambiguous_base_percent": pct(ambiguous, total),
        "invalid_base_count": invalid,
        "gap_count": gap_runs,
        "gap_percent": pct(n, total),
        "empty_sequence_count": empty,
        "duplicate_sequence_id_count": duplicate_seqid,
        "duplicate_header_count": duplicate_header,
        "circular_sequence_count": circular,
    }


def fasta_lengths(path: str | Path) -> dict[str, int]:
    """Map seqid -> length without retaining sequence content."""
    result: dict[str, int] = {}
    for seqid, sequence in iter_fasta(path):
        result[seqid] = result.get(seqid, 0) + len(sequence)
    return result


def fasta_record_count(path: str | Path) -> int:
    return sum(1 for _ in iter_fasta(path))


class _FastqReader:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lines = iter(_iter_binary_lines(self.path))

    def close(self) -> None:
        close = getattr(self.lines, "close", None)
        if close is not None:
            close()

    def _required_line(self, header: str, field: str) -> bytes:
        try:
            return next(self.lines)
        except StopIteration as exc:
            raise QCError(f"{self.path}: truncated FASTQ record near {header[:50]!r} (missing {field})") from exc

    def next_record(self) -> tuple[str, bytes, bytes] | None:
        try:
            header_bytes = next(self.lines)
        except StopIteration:
            return None
        header = _decode_utf8(self.path, header_bytes, "FASTQ header")
        if not header:
            raise QCError(f"{self.path}: blank line where FASTQ header was expected")
        if not header.startswith("@"):
            raise QCError(f"{self.path}: FASTQ header does not start with '@': {header[:50]!r}")
        if not header[1:].split():
            raise QCError(f"{self.path}: empty FASTQ identifier")
        sequence = self._required_line(header, "sequence")
        plus = self._required_line(header, "plus line")
        quality = self._required_line(header, "quality")
        if not plus.startswith(b"+"):
            raise QCError(f"{self.path}: FASTQ plus line malformed near {header[:50]!r}")
        _require_ascii(self.path, sequence, f"FASTQ sequence near {header[:50]!r}")
        _require_ascii(self.path, quality, f"FASTQ quality near {header[:50]!r}")
        if len(sequence) != len(quality):
            raise QCError(
                f"{self.path}: sequence/quality length mismatch near {header[:50]!r} "
                f"({len(sequence)} != {len(quality)})"
            )
        for score in quality:
            if score < 33 or score > 126:
                raise QCError(f"{self.path}: FASTQ quality character outside ASCII 33..126 near {header[:50]!r}")
        return header, sequence, quality


def iter_fastq(path: str | Path) -> Iterator[dict[str, str]]:
    """Yield validated 4-line FASTQ records."""
    reader = _FastqReader(path)
    try:
        while True:
            record = reader.next_record()
            if record is None:
                break
            header, sequence, quality = record
            yield {
                "id": header[1:].split()[0],
                "header": header[1:],
                "sequence": sequence.decode("ascii"),
                "quality": quality.decode("ascii"),
            }
    finally:
        reader.close()


def fastq_record_count(path: str | Path) -> int:
    """Count FASTQ records while performing the same structural validation."""
    reader = _FastqReader(path)
    count = 0
    try:
        while reader.next_record() is not None:
            count += 1
    finally:
        reader.close()
    return count


def _resolve_phred_offset(phred_offset: int | str, min_qual: int, max_qual: int) -> tuple[int, str]:
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
    # The observable ranges overlap. Prefer the modern encoding, but expose
    # that this was an assumption rather than a reliable inference.
    return 33, "ambiguous_assumed_phred33"


def _nx_from_histogram(length_counts: Counter[int], total_bases: int, fraction: float) -> tuple[float, int]:
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


def fastq_stats(path: str | Path, sample_size: int = 1000000,
                phred_offset: int | str = 33) -> dict[str, Any]:
    """Read-level QC metrics computed without external tools."""
    if not isinstance(sample_size, int) or isinstance(sample_size, bool) or sample_size < 1:
        raise ValueError("sample_size must be a positive integer")
    path = Path(path)
    read_count = 0
    total_bases = 0
    gc = 0
    atgc = 0
    min_qual = 255
    max_qual = -1
    length_counts: Counter[int] = Counter()
    sampled_sequences: Counter[bytes] = Counter()
    quality_histogram = [0] * 127
    sampled_total = 0

    reader = _FastqReader(path)
    try:
        while True:
            record = reader.next_record()
            if record is None:
                break
            _header, sequence, quality = record
            read_count += 1
            total_bases += len(sequence)
            length_counts[len(sequence)] += 1
            for base in sequence:
                upper = base - 32 if 97 <= base <= 122 else base
                if upper in {67, 71}:  # C/G
                    gc += 1
                if upper in {65, 67, 71, 84}:  # A/C/G/T
                    atgc += 1
            for score in quality:
                min_qual = min(min_qual, score)
                max_qual = max(max_qual, score)
                quality_histogram[score] += 1
            if read_count <= sample_size:
                sampled_total += 1
                sampled_sequences[sequence] += 1
    finally:
        reader.close()

    sampled_unique = len(sampled_sequences)
    duplicate_value = pct(sampled_total - sampled_unique, sampled_total)
    overrep_count = sum(1 for count in sampled_sequences.values() if count / max(sampled_total, 1) > 0.01)

    offset, encoding = _resolve_phred_offset(phred_offset, min_qual, max_qual)
    q20 = sum(quality_histogram[offset + 20:])
    q30 = sum(quality_histogram[offset + 30:])
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


def parse_attributes(attribute_string: str) -> dict[str, str]:
    """Parse GFF3 column 9 attributes (percent-decoded)."""
    attrs: dict[str, str] = {}
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


def gff3_stats(path: str | Path, fasta_path: str | Path | None = None,
               timings: dict[str, float] | None = None,
               fasta_lengths_map: dict[str, int] | None = None) -> dict[str, Any]:
    """Structural GFF3 validation metrics.

    With `fasta_path` or a precomputed `fasta_lengths_map`, seqid existence and
    end <= sequence length are checked.
    ID/Parent integrity is always checked.  When supplied, ``timings`` is
    populated with non-overlapping diagnostic durations in seconds.
    """
    path = Path(path)
    if fasta_path is not None and fasta_lengths_map is not None:
        raise ValueError("provide either fasta_path or fasta_lengths_map, not both")
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
    feature_counts: Counter[str] = Counter()
    ids: set[str] = set()
    duplicate_id = 0
    parent_refs: Counter[str] = Counter()
    missing_id = 0
    coordinate_errors = 0
    seqid_mismatch = 0
    end_beyond_seq = 0
    cds_phase: Counter[int] = Counter()
    cds_not_multiple3 = 0
    cds_count = 0
    directives = 0
    seqids: set[str] = set()
    scan_started = perf_counter()
    try:
        for raw in _iter_binary_lines(path):
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
            seqid, source, feature_type, start_s, end_s, score, strand, phase_s, attr_s = fields
            seqids.add(seqid)
            feature_counts[feature_type] += 1
            try:
                start = int(start_s)
                end = int(end_s)
            except ValueError:
                coordinate_errors += 1
                continue
            if start < 1 or end < start:
                coordinate_errors += 1
            if lengths and seqid not in lengths:
                seqid_mismatch += 1
            elif lengths and end > lengths[seqid]:
                end_beyond_seq += 1
                coordinate_errors += 1
            attrs = parse_attributes(attr_s)
            if "ID" not in attrs:
                missing_id += 1
            else:
                if attrs["ID"] in ids:
                    duplicate_id += 1
                ids.add(attrs["ID"])
            if "Parent" in attrs:
                for parent in attrs["Parent"].split(","):
                    parent = parent.strip()
                    if parent:
                        parent_refs[parent] += 1
            if feature_type == "CDS":
                cds_count += 1
                if (end - start + 1) % 3 != 0:
                    cds_not_multiple3 += 1
                try:
                    phase = int(phase_s)
                except ValueError:
                    phase = -1
                cds_phase[phase] += 1
    finally:
        if timings is not None:
            timings["gff3_scan"] = perf_counter() - scan_started

    finalize_started = perf_counter()
    try:
        missing_parent = sum(count for parent, count in parent_refs.items() if parent not in ids)
        gene_count = feature_counts.get("gene", 0)
        mrna_count = feature_counts.get("mRNA", 0) + feature_counts.get("transcript", 0)
        cds_phase0 = cds_phase.get(0, 0)
        result = {
            "directive_count": directives,
            "feature_count": sum(feature_counts.values()),
            "gene_count": gene_count,
            "mrna_count": mrna_count,
            "cds_count": cds_count,
            "exon_count": feature_counts.get("exon", 0),
            "feature_type_count": len(feature_counts),
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


def protein_stats(path: str | Path, cds_count: int | None = None) -> dict[str, Any]:
    """Protein FASTA sanity metrics (X content, duplicate IDs, stop codons)."""
    protein_ids: set[str] = set()
    duplicate = 0
    total_aa = 0
    x_count = 0
    internal_stop = 0
    missing_start = 0
    missing_stop = 0
    count = 0
    empty = 0
    for seqid, sequence in iter_fasta(path):
        count += 1
        if seqid in protein_ids:
            duplicate += 1
        protein_ids.add(seqid)
        if not sequence:
            empty += 1
            continue
        upper = sequence.upper()
        if not upper.startswith("M"):
            missing_start += 1
        if not upper.endswith("*"):
            missing_stop += 1
        total_aa += len(upper)
        for aa in upper:
            if aa == "X":
                x_count += 1
            elif aa == "*":
                internal_stop += 1
        # terminal * should not count as internal stop
        if upper.endswith("*"):
            internal_stop -= 1
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
