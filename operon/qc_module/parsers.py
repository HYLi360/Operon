"""Streaming parsers for FASTA, FASTQ and GFF3 used by the QC stages.

Parsers are deliberately dependency-free and line-streamed so they can handle
large files without loading whole genomes into memory.
"""

from __future__ import annotations

import gzip
from collections import Counter
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote

from operon.errors import QCError
from operon.utils import is_gzip_path, median, open_maybe_gzip, pct

DNA_BASES = set("ACGTN")
IUPAC_DNA = set("ACGTRYSWKMBDHVN")
PROTEIN_AA = set("ACDEFGHIKLMNPQRSTVWY*X")


def _open_text(path: str | Path):
    return gzip.open(path, "rt", encoding="utf-8") if is_gzip_path(path) else open(path, "rt", encoding="utf-8")


def iter_fasta(path: str | Path) -> Iterator[tuple[str, str]]:
    """Yield (seqid, sequence) for plain or gzip-compressed FASTA."""
    header: str | None = None
    parts: list[str] = []
    with _open_text(path) as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            if line.startswith(">"):
                if header is not None:
                    yield header.split()[0], "".join(parts)
                header = line[1:].strip()
                if not header:
                    raise QCError(f"{path}: empty FASTA header")
                parts = []
            else:
                if header is None:
                    raise QCError(f"{path}: sequence data before first FASTA header")
                parts.append(line)
        if header is not None:
            yield header.split()[0], "".join(parts)


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
    for seqid, sequence in iter_fasta(path):
        if seqid in seqids:
            duplicate_seqid += 1
        seqids.add(seqid)
        lengths.append(len(sequence))
        total += len(sequence)
        if len(sequence) == 0:
            empty += 1
        if "circular" in seqid.lower():
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


def iter_fastq(path: str | Path) -> Iterator[dict[str, str]]:
    """Yield one 4-line FASTQ record at a time."""
    with _open_text(path) as handle:
        while True:
            header = handle.readline().rstrip("\n\r")
            if not header:
                break
            sequence = handle.readline().rstrip("\n\r")
            plus = handle.readline().rstrip("\n\r")
            quality = handle.readline().rstrip("\n\r")
            if not header.startswith("@"):
                raise QCError(f"{path}: FASTQ header does not start with '@': {header[:50]!r}")
            if plus and not plus.startswith("+"):
                raise QCError(f"{path}: FASTQ plus line malformed near {header[:50]!r}")
            if len(sequence) != len(quality):
                raise QCError(
                    f"{path}: sequence/quality length mismatch near {header[:50]!r} "
                    f"({len(sequence)} != {len(quality)})"
                )
            yield {"id": header[1:].split()[0], "header": header[1:], "sequence": sequence, "quality": quality}


def fastq_stats(path: str | Path, sample_size: int = 1000000) -> dict[str, Any]:
    """Read-level QC metrics computed without external tools."""
    read_count = 0
    total_bases = 0
    gc = 0
    atgc = 0
    min_qual = 255
    max_qual = -1
    lengths: list[int] = []
    sampled_hashes: set[int] = set()
    sampled_sequences: Counter[str] = Counter()
    sampled_total = 0
    sampled_unique = 0

    for record in iter_fastq(path):
        read_count += 1
        sequence = record["sequence"]
        quality = record["quality"]
        total_bases += len(sequence)
        lengths.append(len(sequence))
        for base in sequence.upper():
            if base in {"G", "C"}:
                gc += 1
            if base in {"A", "T", "G", "C"}:
                atgc += 1
        for char in quality:
            score = ord(char)
            min_qual = min(min_qual, score)
            max_qual = max(max_qual, score)
        if read_count <= sample_size:
            sampled_total += 1
            sampled_sequences[sequence] += 1
            seq_hash = hash(sequence)
            if seq_hash not in sampled_hashes:
                sampled_hashes.add(seq_hash)
                sampled_unique += 1

    sampled = sampled_total > 0 and read_count > sample_size
    duplicate_name = "duplicate_percent"
    duplicate_value = pct(sampled_total - sampled_unique, sampled_total)
    overrep_count = sum(1 for count in sampled_sequences.values() if count / max(sampled_total, 1) > 0.01)

    # Phred offset detection: modern Sanger/Illumina 1.8+ data is phred+33.
    if min_qual < 64 or max_qual <= 74:
        offset = 33
        encoding = "sanger_phred33"
    else:
        offset = 64
        encoding = "illumina_phred64"
    if offset == 33:
        q20 = sum(1 for q in _all_quality_chars(path) if ord(q) - 33 >= 20)
        q30 = sum(1 for q in _all_quality_chars(path) if ord(q) - 33 >= 30)
    else:
        q20 = sum(1 for q in _all_quality_chars(path) if ord(q) - 64 >= 20)
        q30 = sum(1 for q in _all_quality_chars(path) if ord(q) - 64 >= 30)

    read_n50, read_l50 = _n50(lengths)
    return {
        "read_count": read_count,
        "total_bases": total_bases,
        "read_length_min": min(lengths) if lengths else 0,
        "read_length_max": max(lengths) if lengths else 0,
        "read_length_mean": (total_bases / read_count) if read_count else 0.0,
        "read_length_n50": read_n50,
        "read_length_l50": read_l50,
        "q20_percent": pct(q20, total_bases),
        "q30_percent": pct(q30, total_bases),
        "gc_percent": pct(gc, atgc),
        "quality_encoding": encoding,
        duplicate_name: duplicate_value,
        "overrepresented_sequence_count": overrep_count,
        "adapter_contamination_percent": None,
    }


def _all_quality_chars(path: str | Path) -> Iterator[str]:
    """Re-stream quality characters for Q20/Q30 counting."""
    with _open_text(path) as handle:
        line_no = 0
        for line in handle:
            line_no += 1
            if line_no % 4 == 0:
                yield from line.rstrip("\n\r")


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


def gff3_stats(path: str | Path, fasta_path: str | Path | None = None) -> dict[str, Any]:
    """Structural GFF3 validation metrics.

    With `fasta_path`, seqid existence and end <= sequence length are checked.
    ID/Parent integrity is always checked.
    """
    path = Path(path)
    lengths = fasta_lengths(fasta_path) if fasta_path else {}
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
    in_fasta_section = False
    with _open_text(path) as handle:
        for line_no, raw in enumerate(handle, start=1):
            line = raw.rstrip("\n\r")
            if not line.strip():
                continue
            if line.startswith("##FASTA"):
                in_fasta_section = True
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
    missing_parent = sum(count for parent, count in parent_refs.items() if parent not in ids)
    gene_count = feature_counts.get("gene", 0)
    mrna_count = feature_counts.get("mRNA", 0) + feature_counts.get("transcript", 0)
    cds_phase0 = cds_phase.get(0, 0)
    return {
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
