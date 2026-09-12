"""Data-derived fan-out: split registered sequence files into per-unit FASTAs.

An adopted assignment manifest (a TSV of unit -> seqid rows, itself already
registered in the file manifest) groups the sequences of one or more manifest
source files into data-determined analysis units (e.g. subfamilies whose
count is only known after classification).  ``fanout_units`` materializes one
FASTA per unit and registers it through the same idempotent ingest path as
``adopt`` (same bytes -> reuse, different bytes for the same entity+role ->
ConflictError) under ``analysis/derived/<entity_id>/`` with role
``<role_prefix>:<unit>`` and ``file_lineage`` edges back to every source file
and the assignment manifest.  Orchestration stays outside: fan-out only
admits the units; a recipe with ``file_role_prefix`` then selects them all.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.errors import ChecksumError, ConflictError, ValidationError
from operon.files import (
    archive_target,
    canonical_filename,
    find_existing_file,
    ingest_file,
    verify_local_file_identity,
)
from operon.qc._parsers import iter_fasta  # noqa: F821
from operon.schema import read_tsv
from operon.sequence_tools import _format_fasta
from operon.utils import now_iso, path_size_bytes, sha256_path
from operon.workflow import finish_run, flush_run_log, start_run

# Operon-internal derivations live apart from externally adopted artifacts.
DERIVED_SUBDIR = "derived"


def _validate_role_component(value: str, label: str) -> None:
    """Same character rules as ``canonical_filename`` applies to roles."""
    if (not value or value in {".", ".."}
            or any(char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValidationError(f"invalid {label}: {value!r}")


def _manifest_file(db: Database, project: Project, file_id: str, label: str) -> tuple[dict[str, Any], Path]:
    row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if row is None:
        raise ValidationError(f"{label} {file_id} is not registered in the manifest")
    record = dict(row)
    if record["status"] == "REMOTE_ONLY":
        raise ValidationError(
            f"{label} {file_id} is REMOTE_ONLY (local bytes were evicted); "
            "restore it with 'operon pull' and re-run"
        )
    path = project.root / record["relative_path"]
    if not path.is_file():
        raise ValidationError(
            f"{label} {file_id} bytes are missing at {record['relative_path']}; "
            "run 'operon verify' to inspect the manifest state"
        )
    return record, path


def parse_assignments(
        path: str | Path,
        *,
        unit_column: str = "unit",
        seqid_column: str = "seqid",
) -> tuple[list[dict[str, Any]], int]:
    """Group an assignment TSV into ordered units of seqids.

    Returns ``(units, duplicate_rows)`` where each unit is
    ``{"unit": name, "seqids": [...]}`` in row order.  Exact duplicate
    (unit, seqid) rows are dropped and counted; row order within a unit is
    preserved so the rendered FASTA is deterministic.  Seqids are normalized
    with the same convention as ``sequence_tools``: the first
    whitespace-delimited token.
    """
    rows = read_tsv(path, required_header=[unit_column, seqid_column])
    units: dict[str, dict[str, Any]] = {}
    duplicate_rows = 0
    for index, row in enumerate(rows, start=2):
        unit = str(row.get(unit_column) or "").strip()
        seqid_raw = str(row.get(seqid_column) or "").strip()
        seqid = seqid_raw.split()[0] if seqid_raw else ""
        if not unit or not seqid:
            raise ValidationError(
                f"{path}: line {index}: both {unit_column!r} and {seqid_column!r} "
                "must be non-empty"
            )
        _validate_role_component(unit, f"fanout unit name in {path} line {index}")
        entry = units.setdefault(unit, {"unit": unit, "seqids": [], "_seen": set()})
        if seqid in entry["_seen"]:
            duplicate_rows += 1
            continue
        entry["_seen"].add(seqid)
        entry["seqids"].append(seqid)
    result = [{"unit": u["unit"], "seqids": u["seqids"]} for u in units.values()]
    if not result:
        raise ValidationError(f"{path}: no assignment rows; nothing to fan out")
    return result, duplicate_rows


def _resolve_seqids(
        db: Database,
        source_file_ids: list[str],
        units: list[dict[str, Any]],
) -> dict[str, str]:
    """Map every assigned seqid to exactly one source file_id.

    Resolution uses the ``sequences`` registry (UNIQUE(file_id, seqid))
    restricted to the declared source files.  A seqid found in no source is a
    hard error listing every unresolvable seqid; a seqid found in more than
    one source is ambiguous and must be disambiguated by narrowing
    ``--source-file``.
    """
    needed = sorted({seqid for unit in units for seqid in unit["seqids"]})
    placeholders = ", ".join("?" for _ in source_file_ids)
    found: dict[str, list[str]] = {}
    for chunk_start in range(0, len(needed), 500):
        chunk = needed[chunk_start:chunk_start + 500]
        seq_placeholders = ", ".join("?" for _ in chunk)
        rows = db.conn.execute(
            f"SELECT seqid, file_id FROM sequences WHERE file_id IN ({placeholders}) "
            f"AND seqid IN ({seq_placeholders})",
            (*source_file_ids, *chunk),
        ).fetchall()
        for row in rows:
            found.setdefault(str(row["seqid"]), []).append(str(row["file_id"]))
    missing = [seqid for seqid in needed if seqid not in found]
    if missing:
        raise ValidationError(
            f"{len(missing)} seqid(s) do not resolve against the source file(s) "
            f"{', '.join(source_file_ids)}: {', '.join(missing)}"
        )
    ambiguous = {seqid: sorted(set(file_ids)) for seqid, file_ids in found.items()
                 if len(set(file_ids)) > 1}
    if ambiguous:
        details = "; ".join(
            f"{seqid} in {', '.join(file_ids)}" for seqid, file_ids in sorted(ambiguous.items())
        )
        raise ValidationError(
            f"{len(ambiguous)} seqid(s) appear in multiple source files ({details}); "
            "narrow --source-file so each seqid resolves to exactly one file"
        )
    return {seqid: file_ids[0] for seqid, file_ids in found.items()}


def _load_source_bodies(
        db: Database,
        source_records: dict[str, tuple[dict[str, Any], Path]],
        needed: set[str],
) -> dict[str, str]:
    """Read sequence bodies from verified source FASTA bytes.

    Each source file's sha256 is verified against the manifest before parsing
    (raw immutability invariant).  First occurrence wins, mirroring
    ``sequence_tools``.
    """
    bodies: dict[str, str] = {}
    for file_id, (record, path) in source_records.items():
        ok, info = verify_local_file_identity(db, record, path)
        if not ok:
            raise ChecksumError(
                f"source file {file_id} failed manifest verification "
                f"({info.get('verification_method')}); run 'operon verify' before fanout"
            )
        stale = db.conn.execute(
            "SELECT 1 FROM sequences WHERE file_id=? AND file_sha256<>? LIMIT 1",
            (file_id, record["sha256"]),
        ).fetchone()
        if stale is not None:
            raise ValidationError(
                f"sequences registry for {file_id} predates the current file bytes; "
                "re-run 'operon qc' on the source file before fanout"
            )
        for seqid, sequence in iter_fasta(path):
            if seqid in needed and seqid not in bodies:
                bodies[seqid] = sequence
    absent = sorted(needed - set(bodies))
    if absent:
        raise ValidationError(
            f"sequences registry lists seqid(s) absent from the source FASTA bytes: "
            f"{', '.join(absent)}; re-run 'operon qc' on the source file"
        )
    return bodies


def fanout_units(
        db: Database,
        project: Project,
        *,
        assignments_file_id: str,
        source_file_ids: list[str],
        entity_type: str,
        entity_id: str,
        role_prefix: str,
        unit_column: str = "unit",
        seqid_column: str = "seqid",
        parent_run_id: str | None = None,
        actor: str = "fanout",
        dry_run: bool = False,
        command: str = "",
) -> dict[str, Any]:
    """Materialize and register one FASTA per assignment unit, idempotently.

    All validation (assignment parsing, seqid resolution, role and content
    conflict checks) happens before any write.  Registration, lineage edges
    and run bookkeeping commit in one transaction; on failure only newly
    created archive targets are removed.  ``dry_run`` returns the planned
    units and writes nothing — no files, no run row.
    """
    if not source_file_ids:
        raise ValidationError("fanout requires at least one --source-file FILE_ID")
    if len(set(source_file_ids)) != len(source_file_ids):
        raise ValidationError("duplicate --source-file FILE_ID values")
    role_prefix = role_prefix.strip()
    _validate_role_component(role_prefix, "role prefix")
    db.require_active_entity(entity_type, entity_id)
    assignments_record, assignments_path = _manifest_file(
        db, project, assignments_file_id, "assignments file")
    source_records = {
        file_id: _manifest_file(db, project, file_id, "source file")
        for file_id in source_file_ids
    }

    units, duplicate_rows = parse_assignments(
        assignments_path, unit_column=unit_column, seqid_column=seqid_column)
    resolution = _resolve_seqids(db, source_file_ids, units)
    derived_root = project.analysis_root / DERIVED_SUBDIR
    for unit in units:
        unit["role"] = f"{role_prefix}:{unit['unit']}"
        # canonical_filename enforces the archive naming rules on the role.
        canonical_filename(entity_id, unit["role"], "fasta", "none")
        unit["target"] = archive_target(
            project, entity_type, entity_id, unit["role"], "fasta", "none", derived_root)

    if dry_run:
        return {
            "dry_run": True,
            "units": [
                {"unit": u["unit"], "role": u["role"], "sequences": len(u["seqids"])}
                for u in units
            ],
            "duplicate_rows": duplicate_rows,
        }

    bodies = _load_source_bodies(
        db, source_records, {seqid for u in units for seqid in u["seqids"]})
    for unit in units:
        text = _format_fasta([(seqid, bodies[seqid]) for seqid in unit["seqids"]])
        payload = text.encode("utf-8")
        unit["text"] = text
        unit["identity"] = (hashlib.sha256(payload).hexdigest(), len(payload))

    # Pre-flight content conflict check, mirroring adopt: same entity+role
    # with different bytes is a ConflictError, identical bytes a reuse.
    for unit in units:
        sha256, size = unit["identity"]
        conflicts = db.conn.execute(
            "SELECT file_id FROM files WHERE entity_type=? AND entity_id=? AND file_role=? "
            "AND (sha256<>? OR size_bytes<>?) LIMIT 1",
            (entity_type, entity_id, unit["role"], sha256, size),
        ).fetchone()
        if conflicts is not None:
            raise ConflictError(
                f"fanout unit {unit['unit']!r} conflicts with registered file "
                f"{conflicts['file_id']} (role {unit['role']!r})"
            )
        existing = find_existing_file(db, entity_type, entity_id, unit["role"], sha256)
        reused = False
        if existing is not None:
            existing_path = project.root / existing["relative_path"]
            reused = existing_path.is_file() and (
                sha256_path(existing_path), path_size_bytes(existing_path)
            ) == unit["identity"]
        unit["reused"] = reused
        if not reused and (unit["target"].exists() or unit["target"].is_symlink()):
            if not unit["target"].is_file() or (
                    sha256_path(unit["target"]), path_size_bytes(unit["target"])
            ) != unit["identity"]:
                raise ConflictError(
                    f"fanout target is occupied by different content: {unit['target']}"
                )

    run = start_run(db, {
        "step": "fanout",
        "parent_run_id": parent_run_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "command": command,
        "tool": "operon",
        "parameter_set": json.dumps({
            "assignments_file_id": assignments_file_id,
            "source_file_ids": source_file_ids,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "role_prefix": role_prefix,
            "unit_column": unit_column,
            "seqid_column": seqid_column,
            "actor": actor,
        }, ensure_ascii=False, sort_keys=True),
    })
    created_targets: list[Path] = []
    jsonl_buffer: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    project.logs_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="operon-fanout-", dir=project.logs_root) as staging:
            with db.transaction():
                for index, unit in enumerate(units, start=1):
                    staged = Path(staging) / f"unit-{index:04d}.fasta"
                    staged.write_text(unit["text"], encoding="utf-8")
                    if not (unit["target"].exists() or unit["target"].is_symlink()):
                        created_targets.append(unit["target"])
                    record = ingest_file(
                        db, project, staged, entity_type, entity_id, unit["role"],
                        fmt="fasta", compression="none",
                        source_url=f"fanout:{assignments_file_id}",
                        run_id=run["run_id"],
                        archive_root=derived_root,
                        provenance_buffer=jsonl_buffer,
                    )
                    for input_file_id in [*source_file_ids, assignments_file_id]:
                        db.conn.execute(
                            "INSERT OR IGNORE INTO file_lineage"
                            "(derived_file_id, input_file_id, workflow_run_id, created_at) "
                            "VALUES(?,?,?,?)",
                            (record["file_id"], input_file_id, run["run_id"], now_iso()),
                        )
                    results.append({
                        "unit": unit["unit"],
                        "role": unit["role"],
                        "sequences": len(unit["seqids"]),
                        "file_id": record["file_id"],
                        "relative_path": record["relative_path"],
                        "sha256": record["sha256"],
                        "status": "reused" if unit["reused"] else "created",
                    })
    except BaseException as exc:
        for target in reversed(created_targets):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        if isinstance(exc, Exception):
            finish_run(db, project, run["run_id"], status="failed", exit_code=1,
                       error=str(exc))
        raise
    flush_run_log(project, jsonl_buffer)

    execution_details = {
        "assignments_file_id": assignments_file_id,
        "source_file_ids": source_file_ids,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "role_prefix": role_prefix,
        "duplicate_rows": duplicate_rows,
        "units": results,
    }
    finish_run(
        db, project, run["run_id"], status="completed", exit_code=0,
        execution_details=json.dumps(execution_details, ensure_ascii=False, sort_keys=True),
    )
    return {
        "dry_run": False,
        "run_id": run["run_id"],
        "units": results,
        "duplicate_rows": duplicate_rows,
        "created": sum(1 for r in results if r["status"] == "created"),
        "reused": sum(1 for r in results if r["status"] == "reused"),
    }
