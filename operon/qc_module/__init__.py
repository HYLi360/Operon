"""Built-in QC stages.

Every stage is a deterministic function that measures metrics and writes them
to the long `qc_results` table.  Stages never decide PASS/FAIL; the rule engine
in operon.rules does that using versioned YAML profiles.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from operon import __version__
from operon.config import Project
from operon.database import Database
from operon.qc_module._parsers import (
    fasta_stats,
    fastq_record_count,
    fastq_stats,
    gff3_stats,
    protein_stats,
)
from operon.utils import now_iso, sha256_file
from operon.workflow import log_run, set_state_bulk

TOOL_NAME = "operon.builtin"
TOOL_VERSION = __version__
PARSER_BACKEND = "cython"
DEFAULT_PARAMETER_SET = "builtin_v2"

ASSEMBLY_METRICS = [
    "sequence_count", "total_length", "min_sequence_length", "max_sequence_length",
    "mean_sequence_length", "median_sequence_length", "contig_n50", "contig_l50",
    "contig_n90", "contig_l90", "gc_percent", "n_percent", "ambiguous_base_percent",
    "invalid_base_count", "gap_count", "gap_percent", "empty_sequence_count",
    "duplicate_sequence_id_count", "duplicate_header_count", "circular_sequence_count",
]


def metric(entity_type: str, entity_id: str, stage: str, name: str, value: Any,
           unit: str | None = None, parameter_set: str = DEFAULT_PARAMETER_SET) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bool):
        numeric: float | None = 1.0 if value else 0.0
        text = "1" if value else "0"
    elif isinstance(value, (int, float)):
        numeric = float(value)
        text = str(value)
    else:
        numeric = None
        text = str(value)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "qc_stage": stage,
        "metric_name": name,
        "metric_value": text,
        "metric_numeric": numeric,
        "metric_unit": unit,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "parameter_set": parameter_set,
        "evaluated_at": now_iso(),
    }


def _write(db: Database, metrics: list[dict[str, Any] | None], file_record: dict[str, Any]) -> None:
    prepared: list[dict[str, Any]] = []
    for item in metrics:
        if item is None:
            continue
        row = dict(item)
        row["file_id"] = file_record["file_id"]
        row["file_sha256"] = file_record["sha256"]
        row["input_identity"] = f"file:{file_record['file_id']}:{file_record['sha256']}"
        prepared.append(row)
    db.insert_many_qc(prepared)


def _file_ok(record: dict[str, Any], project: Project) -> tuple[bool, dict[str, Any]]:
    path = project.root / record["relative_path"]
    info = {"exists": path.exists() and path.is_file()}
    current_sha = sha256_file(path) if info["exists"] else None
    info["sha256_match"] = bool(current_sha and current_sha.lower() == str(record["sha256"]).lower())
    info["size_bytes"] = path.stat().st_size if info["exists"] else 0
    return info["exists"] and info["sha256_match"], info


def qc_file(db: Database, project: Project, file_id: str, sample_size: int = 1000000,
            parameter_set: str = DEFAULT_PARAMETER_SET, phred_offset: int | str = 33,
            read_count_cache: dict[tuple[str, str], int] | None = None) -> dict[str, Any]:
    """Run all applicable built-in QC stages for one manifest file."""
    row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row:
        raise FileNotFoundError(f"file {file_id} not found in manifest")
    record = dict(row)
    entity_type, entity_id = record["entity_type"], record["entity_id"]
    path = project.root / record["relative_path"]
    set_state_bulk(db, entity_type, entity_id, "QC_RUNNING", f"running built-in QC for {file_id}")
    started = now_iso()

    exists_ok, file_info = _file_ok(record, project)
    parseable = 1
    error: str | None = None
    metrics: list[dict[str, Any] | None] = [
        metric(entity_type, entity_id, "file_integrity", "file_exists", file_info["exists"]),
        metric(entity_type, entity_id, "file_integrity", "size_bytes", file_info["size_bytes"], "bytes", parameter_set),
        metric(entity_type, entity_id, "file_integrity", "sha256_match", file_info["sha256_match"], parameter_set=parameter_set),
    ]
    if not exists_ok:
        parseable = 0
        error = "file missing or checksum mismatch"
        metrics.append(metric(entity_type, entity_id, "file_integrity", "parseable", 0, parameter_set=parameter_set))
        _write(db, metrics, record)
        set_state_bulk(db, entity_type, entity_id, "QC_FAILED", error)
        return {"file_id": file_id, "ok": False, "error": error}

    try:
        if record["format"] == "fasta":
            stats = fasta_stats(path)
            if record["file_role"] == "genome_fasta":
                metrics.extend(
                    metric(entity_type, entity_id, "assembly_basic", name, stats[name], unit="bp" if name.endswith("length") or name in {"contig_n50", "contig_n90"} else ("percent" if name.endswith("percent") else None), parameter_set=parameter_set)
                    for name in ASSEMBLY_METRICS
                )
            else:
                metrics.extend([
                    metric(entity_type, entity_id, "sequence_basic", "sequence_count", stats["sequence_count"], parameter_set=parameter_set),
                    metric(entity_type, entity_id, "sequence_basic", "total_length", stats["total_length"], "bp", parameter_set),
                    metric(entity_type, entity_id, "sequence_basic", "empty_sequence_count", stats["empty_sequence_count"], parameter_set=parameter_set),
                    metric(entity_type, entity_id, "sequence_basic", "duplicate_sequence_id_count", stats["duplicate_sequence_id_count"], parameter_set=parameter_set),
                ])
        elif record["format"] == "fastq":
            read_parameter_set = f"{parameter_set}:sample_{sample_size}:phred_{phred_offset}"
            stats = fastq_stats(path, sample_size=sample_size, phred_offset=phred_offset)
            if read_count_cache is not None:
                read_count_cache[(record["file_id"], record["sha256"])] = int(stats["read_count"])
            for name, value in stats.items():
                if value is None:
                    continue
                unit = "bp" if name in {"total_bases", "read_length_min", "read_length_max", "read_length_mean", "read_length_n50"} else (
                    "percent" if name.endswith("percent") else None)
                metrics.append(metric(entity_type, entity_id, "reads_basic", name, value, unit, read_parameter_set))
            pairing = _pairing_metric(
                db, project, record, stats["read_count"], read_parameter_set,
                read_count_cache=read_count_cache,
            )
            if pairing is not None:
                metrics.append(pairing)
        elif record["format"] == "gff3":
            metrics.extend(_annotation_metrics(db, project, record, parameter_set))
        metrics.append(metric(entity_type, entity_id, "file_integrity", "parseable", parseable, parameter_set=parameter_set))
        _write(db, metrics, record)
        set_state_bulk(db, entity_type, entity_id, "QC_COMPLETE", f"built-in QC complete for {file_id}")
        log_run(db, project, {
            "entity_type": entity_type, "entity_id": entity_id, "step": "qc",
            "status": "completed", "started_at": started, "finished_at": now_iso(),
            "tool": TOOL_NAME, "tool_version": TOOL_VERSION, "parameter_set": parameter_set,
            "command": f"operon qc --file-id {file_id}",
        })
        return {"file_id": file_id, "ok": True, "error": None}
    except Exception as exc:
        parseable = 0
        error = f"{type(exc).__name__}: {exc}"
        metrics.append(metric(entity_type, entity_id, "file_integrity", "parseable", 0, parameter_set=parameter_set))
        _write(db, metrics, record)
        set_state_bulk(db, entity_type, entity_id, "QC_FAILED", error)
        log_run(db, project, {
            "entity_type": entity_type, "entity_id": entity_id, "step": "qc",
            "status": "failed", "started_at": started, "finished_at": now_iso(),
            "tool": TOOL_NAME, "tool_version": TOOL_VERSION, "parameter_set": parameter_set,
            "error": error,
        })
        return {"file_id": file_id, "ok": False, "error": error}


def _annotation_metrics(db: Database, project: Project, gff_record: dict[str, Any], parameter_set: str) -> list[dict[str, Any] | None]:
    entity_id = gff_record["entity_id"]
    row = db.conn.execute(
        """
        SELECT an.annotation_id, an.gff_file_id, an.cds_file_id, an.protein_file_id,
               a.assembly_id, af.relative_path AS assembly_path
        FROM annotations an
        JOIN assemblies a ON a.assembly_id = an.assembly_id
        LEFT JOIN files af ON af.file_id = a.fasta_file_id
        WHERE an.annotation_id=?
        """,
        (entity_id,),
    ).fetchone()
    if not row:
        return [metric("annotation", entity_id, "annotation_basic", "parseable", 0, parameter_set=parameter_set)]
    gff_path = project.root / gff_record["relative_path"]
    assembly_path = project.root / row["assembly_path"] if row["assembly_path"] else None
    stats = gff3_stats(gff_path, assembly_path)
    metrics: list[dict[str, Any] | None] = []
    unit_map = {
        "gene_count": None, "mrna_count": None, "cds_count": None, "exon_count": None,
        "feature_count": None, "feature_type_count": None, "seqid_count": None,
        "seqid_mismatch_count": None, "end_beyond_sequence_count": None,
        "coordinate_error_count": None, "missing_id_count": None,
        "duplicate_id_count": None, "missing_parent_count": None,
        "cds_length_multiple3_percent": "percent", "cds_phase0_percent": "percent",
        "cds_not_multiple3_count": None,
    }
    for name, value in stats.items():
        if name in unit_map:
            metrics.append(metric("annotation", entity_id, "annotation_basic", name, value, unit_map[name], parameter_set))
    protein_file = None
    if row["protein_file_id"]:
        prow = db.conn.execute("SELECT * FROM files WHERE file_id=?", (row["protein_file_id"],)).fetchone()
        if prow:
            protein_file = project.root / prow["relative_path"]
    if protein_file and protein_file.exists():
        pstats = protein_stats(protein_file, cds_count=stats["cds_count"])
        for name in [
            "protein_count", "protein_duplicate_id_count", "protein_empty_count",
            "protein_x_percent", "protein_internal_stop_count", "protein_missing_start_count",
            "protein_missing_stop_count", "cds_protein_count_match",
        ]:
            value = pstats.get(name)
            if value is not None:
                unit = "percent" if name.endswith("percent") else None
                metrics.append(metric("annotation", entity_id, "annotation_basic", name, value, unit, parameter_set))
    metrics.append(metric("annotation", entity_id, "annotation_basic", "parseable", 1, parameter_set))
    return metrics


def _pairing_metric(db: Database, project: Project, record: dict[str, Any], own_count: int,
                    parameter_set: str = DEFAULT_PARAMETER_SET,
                    read_count_cache: dict[tuple[str, str], int] | None = None) -> dict[str, Any] | None:
    """When both R1 and R2 are archived, compare read counts from the actual files."""
    if record["file_role"] not in {"reads_r1", "reads_r2"}:
        return None
    sibling_role = "reads_r2" if record["file_role"] == "reads_r1" else "reads_r1"
    sibling = db.conn.execute(
        "SELECT * FROM files WHERE entity_type=? AND entity_id=? AND file_role=?",
        (record["entity_type"], record["entity_id"], sibling_role),
    ).fetchone()
    if not sibling:
        return None
    sibling_path = project.root / sibling["relative_path"]
    if not sibling_path.exists():
        return None
    cache_key = (str(sibling["file_id"]), str(sibling["sha256"]))
    sibling_count = read_count_cache.get(cache_key) if read_count_cache is not None else None
    if sibling_count is None:
        sibling_count = fastq_record_count(sibling_path)
        if read_count_cache is not None:
            read_count_cache[cache_key] = int(sibling_count)
    matched = 1 if int(own_count) == int(sibling_count) else 0
    return metric(record["entity_type"], record["entity_id"], "reads_basic", "paired_read_count_match", matched, parameter_set=parameter_set)


def qc_all(db: Database, project: Project, entity_type: str | None = None,
           entity_id: str | None = None, file_id: str | None = None,
           sample_size: int = 1000000, phred_offset: int | str = 33,
           parameter_set: str = DEFAULT_PARAMETER_SET) -> list[dict[str, Any]]:
    """Run QC for selected manifest files; one failure does not abort the batch."""
    sql = "SELECT file_id FROM files WHERE entity_type IN ('organism','sample','run','assembly','annotation')"
    params: list[Any] = []
    if entity_type:
        sql += " AND entity_type=?"
        params.append(entity_type)
    if entity_id:
        sql += " AND entity_id=?"
        params.append(entity_id)
    if file_id:
        sql += " AND file_id=?"
        params.append(file_id)
    sql += " ORDER BY file_id"
    rows = db.conn.execute(sql, params).fetchall()
    results = []
    read_count_cache: dict[tuple[str, str], int] = {}
    for row in rows:
        results.append(qc_file(
            db, project, row["file_id"], sample_size=sample_size,
            phred_offset=phred_offset, parameter_set=parameter_set,
            read_count_cache=read_count_cache,
        ))
    return results
