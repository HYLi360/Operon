"""Built-in QC stages.

Every stage is a deterministic function that measures metrics and writes them
to the long `qc_results` table.  Stages never decide PASS/FAIL; the rule engine
in operon.rules does that using versioned YAML profiles.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from operon import __version__
from operon.config import Project, project_rel
from operon.database import Database
from operon.errors import QCError
from operon.files import verify_local_file_identity
from operon.qc_module._parsers import (
    fasta_lengths,
    fasta_stats,
    fastq_record_count,
    fastq_stats,
    gff3_stats,
    protein_stats,
)
from operon.utils import now_iso
from operon.workflow import log_run, set_state_bulk

TOOL_NAME = "operon.builtin"
TOOL_VERSION = __version__
PARSER_BACKEND = "cython"
DEFAULT_PARAMETER_SET = "builtin_v2"
FASTA_LENGTH_CACHE_FORMAT = "operon-fasta-lengths-v1"

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


def _input_set_identity(file_record: dict[str, Any],
                        related_inputs: list[dict[str, Any]]) -> str:
    inputs = [{
        "kind": "primary",
        "file_id": file_record["file_id"],
        "sha256": str(file_record["sha256"]).lower(),
        "size_bytes": int(file_record["size_bytes"]),
    }]
    inputs.extend({
        "kind": str(item["kind"]),
        "file_id": str(item["file_id"]),
        "sha256": str(item["sha256"]).lower(),
        "size_bytes": int(item["size_bytes"]),
    } for item in related_inputs)
    payload = json.dumps(
        sorted(inputs, key=lambda item: (item["kind"], item["file_id"])),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return f"input-set:v1:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _write(db: Database, metrics: list[dict[str, Any] | None], file_record: dict[str, Any],
           related_inputs: list[dict[str, Any]] | None = None) -> None:
    prepared: list[dict[str, Any]] = []
    related_inputs = related_inputs or []
    primary_identity = f"file:{file_record['file_id']}:{file_record['sha256']}"
    annotation_identity = (
        _input_set_identity(file_record, related_inputs) if related_inputs else primary_identity
    )
    for item in metrics:
        if item is None:
            continue
        row = dict(item)
        row["file_id"] = file_record["file_id"]
        row["file_sha256"] = file_record["sha256"]
        row["input_identity"] = (
            annotation_identity if row["qc_stage"] == "annotation_basic" else primary_identity
        )
        prepared.append(row)
    db.insert_many_qc(prepared)


def _file_ok(db: Database, record: dict[str, Any], project: Project, *,
             rehash: bool = False) -> tuple[bool, dict[str, Any]]:
    path = project.root / record["relative_path"]
    return verify_local_file_identity(db, record, path, rehash=rehash)


def _timed_call(timings: dict[str, float], stage: str, function, *args, **kwargs):
    started = time.perf_counter()
    try:
        return function(*args, **kwargs)
    finally:
        timings[stage] = timings.get(stage, 0.0) + (time.perf_counter() - started)


def _input_descriptor(record: dict[str, Any], *, kind: str,
                      size_bytes: int | None = None) -> dict[str, Any]:
    return {
        "kind": kind,
        "file_id": record.get("file_id"),
        "sha256": record.get("sha256"),
        "size_bytes": int(size_bytes if size_bytes is not None else (record.get("size_bytes") or 0)),
        "file_role": record.get("file_role"),
        "format": record.get("format"),
        "compression": record.get("compression"),
    }


def _related_input_descriptor(record: dict[str, Any], info: dict[str, Any], *,
                              kind: str) -> dict[str, Any]:
    descriptor = _input_descriptor(record, kind=kind, size_bytes=info.get("size_bytes"))
    descriptor["integrity"] = {
        "verification_method": info.get("verification_method"),
        "verification_cached_at": info.get("verification_cached_at"),
    }
    return descriptor


def _verify_related_input(db: Database, project: Project, record: dict[str, Any], *,
                          kind: str, stage: str, timings: dict[str, float],
                          related_inputs: list[dict[str, Any]],
                          rehash: bool) -> tuple[Path, dict[str, Any]]:
    path = project.root / record["relative_path"]
    ok, info = _timed_call(
        timings, stage, verify_local_file_identity,
        db, record, path, rehash=rehash,
    )
    descriptor = _related_input_descriptor(record, info, kind=kind)
    related_inputs.append(descriptor)
    if not ok:
        method = info.get("verification_method") or "unknown"
        raise QCError(
            f"related {kind} {record['file_id']} is missing or does not match its manifest "
            f"({method})"
        )
    return path, descriptor


def _fasta_length_cache_path(project: Project, record: dict[str, Any]) -> Path:
    return (
        project.qc_root / "cache" / "fasta_lengths" /
        f"{record['file_id']}.{str(record['sha256']).lower()}.v1.tsv"
    )


def _fasta_length_cache_row(seqid: str, length: int) -> str:
    if not seqid or "\t" in seqid or "\n" in seqid or "\r" in seqid:
        raise QCError(f"cannot cache invalid FASTA sequence identifier {seqid!r}")
    normalized_length = int(length)
    if normalized_length < 0:
        raise QCError(f"cannot cache negative FASTA sequence length for {seqid!r}")
    return f"{seqid}\t{normalized_length}\n"


def _fasta_length_cache_digest(lengths: dict[str, int]) -> str:
    digest = hashlib.sha256()
    for seqid, length in lengths.items():
        digest.update(_fasta_length_cache_row(seqid, length).encode("utf-8"))
    return digest.hexdigest()


def _load_fasta_length_cache(path: Path, record: dict[str, Any]) -> dict[str, int] | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            header = json.loads(handle.readline())
            expected = {
                "cache_format": FASTA_LENGTH_CACHE_FORMAT,
                "file_id": record["file_id"],
                "sha256": str(record["sha256"]).lower(),
                "size_bytes": int(record["size_bytes"]),
            }
            if any(header.get(key) != value for key, value in expected.items()):
                raise ValueError("cache identity does not match the assembly manifest")
            lengths: dict[str, int] = {}
            digest = hashlib.sha256()
            for line_number, line in enumerate(handle, 2):
                line = line.rstrip("\n")
                if not line:
                    continue
                seqid, separator, length_text = line.rpartition("\t")
                if not separator or not seqid or seqid in lengths:
                    raise ValueError(f"invalid FASTA length cache row {line_number}")
                length = int(length_text)
                if length < 0:
                    raise ValueError(f"negative sequence length at cache row {line_number}")
                lengths[seqid] = length
                digest.update(_fasta_length_cache_row(seqid, length).encode("utf-8"))
            if len(lengths) != int(header.get("sequence_count", -1)):
                raise ValueError("FASTA length cache sequence count does not match its header")
            if digest.hexdigest() != header.get("lengths_sha256"):
                raise ValueError("FASTA length cache content digest does not match its header")
            return lengths
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def _write_fasta_length_cache(path: Path, record: dict[str, Any],
                              lengths: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "cache_format": FASTA_LENGTH_CACHE_FORMAT,
        "file_id": record["file_id"],
        "sha256": str(record["sha256"]).lower(),
        "size_bytes": int(record["size_bytes"]),
        "sequence_count": len(lengths),
        "lengths_sha256": _fasta_length_cache_digest(lengths),
    }
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(header, ensure_ascii=False, sort_keys=True) + "\n")
            for seqid, length in lengths.items():
                handle.write(_fasta_length_cache_row(seqid, length))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _cached_fasta_lengths(project: Project, record: dict[str, Any], path: Path,
                          timings: dict[str, float]) -> tuple[dict[str, int], dict[str, Any]]:
    cache_path = _fasta_length_cache_path(project, record)
    lengths = _timed_call(
        timings, "assembly_fasta_length_cache_lookup",
        _load_fasta_length_cache, cache_path, record,
    )
    if lengths is not None:
        return lengths, {
            "status": "hit",
            "cache_format": FASTA_LENGTH_CACHE_FORMAT,
            "relative_path": project_rel(project, cache_path),
            "sequence_count": len(lengths),
        }
    lengths = _timed_call(timings, "assembly_fasta_lengths", fasta_lengths, path)
    try:
        _timed_call(
            timings, "assembly_fasta_length_cache_write",
            _write_fasta_length_cache, cache_path, record, lengths,
        )
        cache_status = "built"
        cache_error = None
    except OSError as exc:
        cache_status = "write_failed"
        cache_error = f"{type(exc).__name__}: {exc}"
    return lengths, {
        "status": cache_status,
        "cache_format": FASTA_LENGTH_CACHE_FORMAT,
        "relative_path": project_rel(project, cache_path),
        "sequence_count": len(lengths),
        **({"error": cache_error} if cache_error else {}),
    }


def _qc_timing_details(record: dict[str, Any], file_info: dict[str, Any],
                       timings: dict[str, float], related_inputs: list[dict[str, Any]],
                       duration: float) -> dict[str, Any]:
    measured = sum(timings.values())
    stages = dict(timings)
    stages["unattributed"] = max(duration - measured, 0.0)
    return {
        "schema_version": 1,
        "clock": "perf_counter",
        "parser_backend": PARSER_BACKEND,
        "integrity": {
            "verification_method": file_info.get("verification_method"),
            "verification_cached_at": file_info.get("verification_cached_at"),
            "rehash_requested": bool(record.get("_qc_force_checksum", False)),
        },
        "input": _input_descriptor(
            record, kind="primary", size_bytes=int(file_info.get("size_bytes") or 0),
        ),
        "related_inputs": related_inputs,
        "stages_seconds": {
            name: round(value, 6) for name, value in sorted(stages.items())
        },
    }


def _log_qc_run(db: Database, project: Project, record: dict[str, Any], *,
                started_at: str, total_started: float, status: str,
                file_info: dict[str, Any], timings: dict[str, float],
                related_inputs: list[dict[str, Any]], error: str | None = None) -> None:
    duration = time.perf_counter() - total_started
    timing_details = _qc_timing_details(
        record, file_info, timings, related_inputs, duration,
    )
    workflow_record: dict[str, Any] = {
        "entity_type": record["entity_type"],
        "entity_id": record["entity_id"],
        "file_id": record["file_id"],
        "file_role": record.get("file_role"),
        "file_format": record.get("format"),
        "input_size_bytes": int(file_info.get("size_bytes") or 0),
        "checksum_verification_method": file_info.get("verification_method"),
        "parser_backend": PARSER_BACKEND,
        "step": "qc",
        "status": status,
        "started_at": started_at,
        "finished_at": now_iso(),
        "duration_seconds": round(duration, 6),
        "stage_timings_seconds": timing_details["stages_seconds"],
        "qc_timing": timing_details,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "parameter_set": record.get("_qc_parameter_set", DEFAULT_PARAMETER_SET),
        "input_sha256": record.get("sha256"),
        "command": f"operon qc --file-id {record['file_id']}",
        "execution_details": json.dumps(timing_details, ensure_ascii=False, sort_keys=True),
    }
    if error is not None:
        workflow_record["error"] = error
    log_run(db, project, workflow_record)


def qc_file(db: Database, project: Project, file_id: str, sample_size: int = 1000000,
            parameter_set: str = DEFAULT_PARAMETER_SET, phred_offset: int | str = 33,
            read_count_cache: dict[tuple[str, str], int] | None = None,
            force_checksum: bool = False) -> dict[str, Any]:
    """Run all applicable built-in QC stages for one manifest file."""
    row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row:
        raise FileNotFoundError(f"file {file_id} not found in manifest")
    record = dict(row)
    record["_qc_parameter_set"] = parameter_set
    record["_qc_force_checksum"] = force_checksum
    entity_type, entity_id = record["entity_type"], record["entity_id"]
    path = project.root / record["relative_path"]
    started = now_iso()
    total_started = time.perf_counter()
    timings: dict[str, float] = {}
    related_inputs: list[dict[str, Any]] = []
    _timed_call(
        timings, "state_qc_running", set_state_bulk,
        db, entity_type, entity_id, "QC_RUNNING", f"running built-in QC for {file_id}",
    )

    exists_ok, file_info = _timed_call(
        timings, "file_integrity", _file_ok,
        db, record, project, rehash=force_checksum,
    )
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
        _timed_call(
            timings, "qc_results_write", _write,
            db, metrics, record, related_inputs,
        )
        _timed_call(
            timings, "state_qc_failed", set_state_bulk,
            db, entity_type, entity_id, "QC_FAILED", error,
        )
        _log_qc_run(
            db, project, record, started_at=started, total_started=total_started,
            status="failed", file_info=file_info, timings=timings,
            related_inputs=related_inputs, error=error,
        )
        return {"file_id": file_id, "ok": False, "error": error}

    try:
        if record["format"] == "fasta":
            stats = _timed_call(timings, "fasta_stats", fasta_stats, path)
            if record["file_role"] in {
                "genome_fasta", "genome_fasta_genbank", "genome_fasta_refseq",
            }:
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
            stats = _timed_call(
                timings, "fastq_stats", fastq_stats, path,
                sample_size=sample_size, phred_offset=phred_offset,
            )
            if read_count_cache is not None:
                read_count_cache[(record["file_id"], record["sha256"])] = int(stats["read_count"])
            for name, value in stats.items():
                if value is None:
                    continue
                unit = "bp" if name in {"total_bases", "read_length_min", "read_length_max", "read_length_mean", "read_length_n50"} else (
                    "percent" if name.endswith("percent") else None)
                metrics.append(metric(entity_type, entity_id, "reads_basic", name, value, unit, read_parameter_set))
            pairing = _timed_call(
                timings, "paired_read_check", _pairing_metric,
                db, project, record, stats["read_count"], read_parameter_set,
                read_count_cache=read_count_cache,
            )
            if pairing is not None:
                metrics.append(pairing)
        elif record["format"] == "gff3":
            metrics.extend(_annotation_metrics(
                db, project, record, parameter_set,
                timings=timings, related_inputs=related_inputs,
                force_checksum=force_checksum,
            ))
        metrics.append(metric(entity_type, entity_id, "file_integrity", "parseable", parseable, parameter_set=parameter_set))
        _timed_call(
            timings, "qc_results_write", _write,
            db, metrics, record, related_inputs,
        )
        _timed_call(
            timings, "state_qc_complete", set_state_bulk,
            db, entity_type, entity_id, "QC_COMPLETE", f"built-in QC complete for {file_id}",
        )
        _log_qc_run(
            db, project, record, started_at=started, total_started=total_started,
            status="completed", file_info=file_info, timings=timings,
            related_inputs=related_inputs,
        )
        return {"file_id": file_id, "ok": True, "error": None}
    except Exception as exc:
        parseable = 0
        error = f"{type(exc).__name__}: {exc}"
        metrics.append(metric(entity_type, entity_id, "file_integrity", "parseable", 0, parameter_set=parameter_set))
        _timed_call(
            timings, "qc_results_write", _write,
            db, metrics, record, related_inputs,
        )
        _timed_call(
            timings, "state_qc_failed", set_state_bulk,
            db, entity_type, entity_id, "QC_FAILED", error,
        )
        _log_qc_run(
            db, project, record, started_at=started, total_started=total_started,
            status="failed", file_info=file_info, timings=timings,
            related_inputs=related_inputs, error=error,
        )
        return {"file_id": file_id, "ok": False, "error": error}


def _annotation_metrics(db: Database, project: Project, gff_record: dict[str, Any],
                        parameter_set: str, *, timings: dict[str, float] | None = None,
                        related_inputs: list[dict[str, Any]] | None = None,
                        force_checksum: bool = False) -> list[dict[str, Any] | None]:
    timings = timings if timings is not None else {}
    related_inputs = related_inputs if related_inputs is not None else []
    entity_id = gff_record["entity_id"]
    row = _timed_call(
        timings, "annotation_manifest_lookup",
        lambda: db.conn.execute(
            """
            SELECT an.annotation_id, an.gff_file_id, an.cds_file_id, an.protein_file_id,
                   a.assembly_id, af.file_id AS assembly_file_id,
                   af.relative_path AS assembly_relative_path,
                   af.sha256 AS assembly_sha256,
                   af.size_bytes AS assembly_size_bytes, af.file_role AS assembly_file_role,
                   af.format AS assembly_format, af.compression AS assembly_compression
            FROM annotations an
            JOIN assemblies a ON a.assembly_id = an.assembly_id
            LEFT JOIN files af ON af.file_id = a.fasta_file_id
            WHERE an.annotation_id=?
            """,
            (entity_id,),
        ).fetchone(),
    )
    if not row:
        return [metric("annotation", entity_id, "annotation_basic", "parseable", 0, parameter_set=parameter_set)]
    gff_path = project.root / gff_record["relative_path"]
    assembly_lengths = None
    if row["assembly_file_id"]:
        assembly_record = {
            "file_id": row["assembly_file_id"],
            "relative_path": row["assembly_relative_path"],
            "sha256": row["assembly_sha256"],
            "size_bytes": int(row["assembly_size_bytes"] or 0),
            "file_role": row["assembly_file_role"],
            "format": row["assembly_format"],
            "compression": row["assembly_compression"],
        }
        assembly_path, assembly_descriptor = _verify_related_input(
            db, project, assembly_record, kind="assembly_fasta",
            stage="assembly_fasta_integrity", timings=timings,
            related_inputs=related_inputs,
            rehash=force_checksum,
        )
        assembly_lengths, cache_info = _cached_fasta_lengths(
            project, assembly_record, assembly_path, timings,
        )
        assembly_descriptor["length_cache"] = cache_info
    parser_timings: dict[str, float] = {}
    try:
        stats = gff3_stats(
            gff_path, timings=parser_timings,
            fasta_lengths_map=assembly_lengths,
        )
    finally:
        for name, duration in parser_timings.items():
            timings[name] = timings.get(name, 0.0) + duration
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
    prow = _timed_call(
        timings, "protein_manifest_lookup",
        lambda: db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (row["protein_file_id"],),
        ).fetchone() if row["protein_file_id"] else None,
    )
    if prow:
        protein_record = dict(prow)
        protein_file, _protein_descriptor = _verify_related_input(
            db, project, protein_record, kind="protein_fasta",
            stage="protein_fasta_integrity", timings=timings,
            related_inputs=related_inputs,
            rehash=force_checksum,
        )
    if protein_file:
        pstats = _timed_call(
            timings, "protein_stats", protein_stats,
            protein_file, cds_count=stats["cds_count"],
        )
        for name in [
            "protein_count", "protein_duplicate_id_count", "protein_empty_count",
            "protein_x_percent", "protein_internal_stop_count", "protein_missing_start_count",
            "protein_missing_stop_count", "cds_protein_count_match",
        ]:
            value = pstats.get(name)
            if value is not None:
                unit = "percent" if name.endswith("percent") else None
                metrics.append(metric("annotation", entity_id, "annotation_basic", name, value, unit, parameter_set))
    metrics.append(metric(
        "annotation", entity_id, "annotation_basic", "parseable", 1,
        parameter_set=parameter_set,
    ))
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
           parameter_set: str = DEFAULT_PARAMETER_SET,
           force_checksum: bool = False) -> list[dict[str, Any]]:
    """Run QC for selected manifest files; one failure does not abort the batch."""
    sql = (
        "SELECT file_id FROM files WHERE entity_type IN "
        "('organism','sample','run','assembly','annotation') AND NOT EXISTS ("
        "SELECT 1 FROM entity_supersessions s WHERE s.object_type=files.entity_type "
        "AND s.object_id=files.entity_id)"
    )
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
            force_checksum=force_checksum,
        ))
    return results
