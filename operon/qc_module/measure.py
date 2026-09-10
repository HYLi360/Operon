"""Project-independent built-in QC measurement (`operon qc-measure`).

This module measures the exact same metrics as the in-project stages in
``operon.qc_module`` but needs no project, database, or manifest: it verifies
the caller-supplied file identity (SHA-256 + size) and returns a JSON-ready
payload that ``operon import-qc`` can load into ``qc_results`` later.  It is
meant to run anywhere the archived bytes are available (e.g. an HPC node),
so it must stay free of project/configuration dependencies.

The ``_*_metric_specs`` helpers are the single source of truth for the
stats-dict -> (stage, name, value, unit) mapping; ``qc_file`` and
``_annotation_metrics`` consume them so local and remote measurement can
never drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from operon import __version__
from operon.errors import ChecksumError, QCError
from operon.qc_module._parsers import (
    fasta_lengths,
    fasta_stats,
    fastq_record_count,
    fastq_stats,
    gff3_stats,
    protein_stats,
)
from operon.utils import sha256_file

TOOL_NAME = "operon.builtin"
TOOL_VERSION = __version__
PARSER_BACKEND = "cython"
DEFAULT_PARAMETER_SET = "builtin_v2"
MEASURE_SCHEMA_VERSION = 1

ASSEMBLY_METRICS = [
    "sequence_count", "total_length", "min_sequence_length", "max_sequence_length",
    "mean_sequence_length", "median_sequence_length", "contig_n50", "contig_l50",
    "contig_n90", "contig_l90", "gc_percent", "n_percent", "ambiguous_base_percent",
    "invalid_base_count", "gap_count", "gap_percent", "empty_sequence_count",
    "duplicate_sequence_id_count", "duplicate_header_count", "circular_sequence_count",
]

ASSEMBLY_FILE_ROLES = {"genome_fasta", "genome_fasta_genbank", "genome_fasta_refseq"}

PAIRED_READ_ROLES = {"reads_r1", "reads_r2"}

_GFF3_UNIT_MAP = {
    "gene_count": None, "mrna_count": None, "cds_count": None, "exon_count": None,
    "feature_count": None, "feature_type_count": None, "seqid_count": None,
    "seqid_mismatch_count": None, "end_beyond_sequence_count": None,
    "coordinate_error_count": None, "missing_id_count": None,
    "duplicate_id_count": None, "missing_parent_count": None,
    "cds_length_multiple3_percent": "percent", "cds_phase0_percent": "percent",
    "cds_not_multiple3_count": None,
}

_PROTEIN_METRICS = [
    "protein_count", "protein_duplicate_id_count", "protein_empty_count",
    "protein_x_percent", "protein_internal_stop_count", "protein_missing_start_count",
    "protein_missing_stop_count", "cds_protein_count_match",
]


def coerce_metric_value(value: Any) -> tuple[str, float | None] | None:
    """Split a raw metric value into its text/numeric storage forms (None drops it)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return ("1" if value else "0"), (1.0 if value else 0.0)
    if isinstance(value, (int, float)):
        return str(value), float(value)
    return str(value), None


def _fasta_metric_specs(stats: dict[str, Any], file_role: str) -> list[tuple[str, str, Any, str | None]]:
    if file_role in ASSEMBLY_FILE_ROLES:
        return [
            (
                "assembly_basic", name, stats[name],
                "bp" if name.endswith("length") or name in {"contig_n50", "contig_n90"} else (
                    "percent" if name.endswith("percent") else None),
            )
            for name in ASSEMBLY_METRICS
        ]
    return [
        ("sequence_basic", "sequence_count", stats["sequence_count"], None),
        ("sequence_basic", "total_length", stats["total_length"], "bp"),
        ("sequence_basic", "empty_sequence_count", stats["empty_sequence_count"], None),
        ("sequence_basic", "duplicate_sequence_id_count", stats["duplicate_sequence_id_count"], None),
    ]


def _fastq_metric_specs(stats: dict[str, Any]) -> list[tuple[str, str, Any, str | None]]:
    specs = []
    for name, value in stats.items():
        if value is None:
            continue
        unit = "bp" if name in {"total_bases", "read_length_min", "read_length_max",
                                "read_length_mean", "read_length_n50"} else (
            "percent" if name.endswith("percent") else None)
        specs.append(("reads_basic", name, value, unit))
    return specs


def _gff3_metric_specs(stats: dict[str, Any],
                       pstats: dict[str, Any] | None) -> list[tuple[str, str, Any, str | None]]:
    specs: list[tuple[str, str, Any, str | None]] = [
        ("annotation_basic", name, value, _GFF3_UNIT_MAP[name])
        for name, value in stats.items()
        if name in _GFF3_UNIT_MAP
    ]
    if pstats:
        for name in _PROTEIN_METRICS:
            value = pstats.get(name)
            if value is not None:
                specs.append((
                    "annotation_basic", name, value,
                    "percent" if name.endswith("percent") else None,
                ))
    specs.append(("annotation_basic", "parseable", 1, None))
    return specs


def _payload_metric(stage: str, name: str, value: Any, unit: str | None,
                    parameter_set: str) -> dict[str, Any] | None:
    coerced = coerce_metric_value(value)
    if coerced is None:
        return None
    text, numeric = coerced
    return {
        "qc_stage": stage,
        "metric_name": name,
        "metric_value": text,
        "metric_numeric": numeric,
        "metric_unit": unit,
        "parameter_set": parameter_set,
    }


def measure_file(path: str | Path, *, file_format: str, file_role: str,
                 sha256: str, size_bytes: int, file_id: str | None = None,
                 sample_size: int = 1000000, phred_offset: int | str = 33,
                 parameter_set: str = DEFAULT_PARAMETER_SET,
                 assembly_fasta: str | Path | None = None,
                 protein_fasta: str | Path | None = None,
                 paired_read: str | Path | None = None) -> dict[str, Any]:
    """Measure built-in QC metrics for one file without any project context.

    The file's bytes are first verified against the supplied ``sha256`` /
    ``size_bytes`` identity (raising :class:`ChecksumError` on mismatch), then
    parsed with the same Cython parsers ``qc_file`` uses, producing the same
    stage/metric names and units.  The returned dict is the JSON payload
    consumed by ``operon import-qc``.
    """
    path = Path(path)
    if not path.is_file():
        raise ChecksumError(f"file does not exist or is not a regular file: {path}")
    actual_size = path.stat().st_size
    if actual_size != int(size_bytes):
        raise ChecksumError(
            f"size mismatch for {path}: manifest says {int(size_bytes)} bytes, "
            f"measured {actual_size}"
        )
    actual_sha256 = sha256_file(path)
    if actual_sha256.lower() != str(sha256).lower():
        raise ChecksumError(
            f"sha256 mismatch for {path}: manifest says {str(sha256).lower()}, "
            f"measured {actual_sha256}"
        )

    entries: list[dict[str, Any] | None] = [
        _payload_metric("file_integrity", "file_exists", True, None, DEFAULT_PARAMETER_SET),
        _payload_metric("file_integrity", "size_bytes", actual_size, "bytes", parameter_set),
        _payload_metric("file_integrity", "sha256_match", True, None, parameter_set),
    ]
    sequences: dict[str, int] | None = None
    if file_format == "fasta":
        stats = fasta_stats(path)
        entries.extend(
            _payload_metric(stage, name, value, unit, parameter_set)
            for stage, name, value, unit in _fasta_metric_specs(stats, file_role)
        )
        sequences = fasta_lengths(path)
    elif file_format == "fastq":
        read_parameter_set = f"{parameter_set}:sample_{sample_size}:phred_{phred_offset}"
        stats = fastq_stats(path, sample_size=sample_size, phred_offset=phred_offset)
        entries.extend(
            _payload_metric(stage, name, value, unit, read_parameter_set)
            for stage, name, value, unit in _fastq_metric_specs(stats)
        )
        if paired_read is not None and file_role in PAIRED_READ_ROLES:
            sibling_count = fastq_record_count(Path(paired_read))
            matched = 1 if int(stats["read_count"]) == int(sibling_count) else 0
            entries.append(_payload_metric(
                "reads_basic", "paired_read_count_match", matched, None, read_parameter_set,
            ))
    elif file_format == "gff3":
        if assembly_fasta is None and protein_fasta is None:
            raise QCError(
                "gff3 measurement needs its related inputs: pass --assembly-fasta "
                "and/or --protein-fasta so annotation metrics (seqid/coordinate "
                "checks, protein cross-checks) can be computed"
            )
        lengths = fasta_lengths(Path(assembly_fasta)) if assembly_fasta is not None else None
        stats = gff3_stats(path, fasta_lengths_map=lengths)
        pstats = (
            protein_stats(Path(protein_fasta), cds_count=stats["cds_count"])
            if protein_fasta is not None else None
        )
        entries.extend(
            _payload_metric(stage, name, value, unit, parameter_set)
            for stage, name, value, unit in _gff3_metric_specs(stats, pstats)
        )
    # parseable=1 only when a format parser actually ran; formats without a
    # parser (other, directory, bam, ...) leave parseable unmeasured so
    # required `parseable == 1` gates stay NOT_EVALUATED for them.
    if file_format in {"fasta", "fastq", "gff3"}:
        entries.append(_payload_metric("file_integrity", "parseable", 1, None, parameter_set))

    return {
        "schema_version": MEASURE_SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "parser_backend": PARSER_BACKEND,
        "parameter_set": parameter_set,
        "file": {
            "file_id": file_id,
            "sha256": actual_sha256,
            "size_bytes": actual_size,
            "format": file_format,
            "file_role": file_role,
        },
        "metrics": [entry for entry in entries if entry is not None],
        **({"sequences": sequences} if sequences is not None else {}),
    }
