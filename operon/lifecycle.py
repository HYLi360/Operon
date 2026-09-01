"""Audited, reversible logical retirement for metadata entities.

Retirement never deletes metadata or artifact bytes.  A direct RETIRE event
also retires descendants through the database's effective-retirement view;
RESTORE reverses only the target's latest direct retirement.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from operon.database import Database
from operon.entity_view import resolve_identifier
from operon.errors import ValidationError
from operon.schema import ENTITY_ID_COLUMNS, ENTITY_TABLES
from operon.utils import now_iso


RETIRE_REASON_CODES = {
    "accidental_import",
    "wrong_source",
    "duplicate",
    "withdrawn_upstream",
    "policy_exclusion",
    "metadata_error",
    "other",
}


def _ordered_ids(db: Database, sql: str, params: Iterable[Any]) -> list[str]:
    return [str(row[0]) for row in db.conn.execute(sql, tuple(params)).fetchall()]


def entity_subtree(
    db: Database, entity_type: str, entity_id: str
) -> dict[str, list[str]]:
    """Return the target and every ownership descendant in stable order."""
    db.require_entity(entity_type, entity_id)
    result = {kind: [] for kind in ENTITY_TABLES}
    result[entity_type] = [entity_id]

    if entity_type == "organism":
        result["sample"] = _ordered_ids(
            db,
            "SELECT sample_id FROM samples WHERE organism_id=? ORDER BY sample_id",
            (entity_id,),
        )
    elif entity_type == "sample":
        result["sample"] = [entity_id]

    sample_ids = result["sample"]
    if sample_ids:
        placeholders = ", ".join("?" for _ in sample_ids)
        if entity_type in {"organism", "sample"}:
            result["run"] = _ordered_ids(
                db,
                f"SELECT run_id FROM runs WHERE sample_id IN ({placeholders}) "
                "ORDER BY run_id",
                sample_ids,
            )
            result["assembly"] = _ordered_ids(
                db,
                f"SELECT assembly_id FROM assemblies WHERE sample_id IN ({placeholders}) "
                "ORDER BY assembly_id",
                sample_ids,
            )

    if entity_type == "run":
        result["run"] = [entity_id]
    elif entity_type == "assembly":
        result["assembly"] = [entity_id]

    assembly_ids = result["assembly"]
    if assembly_ids and entity_type in {"organism", "sample", "assembly"}:
        placeholders = ", ".join("?" for _ in assembly_ids)
        result["annotation"] = _ordered_ids(
            db,
            f"SELECT annotation_id FROM annotations "
            f"WHERE assembly_id IN ({placeholders}) ORDER BY annotation_id",
            assembly_ids,
        )
    if entity_type == "annotation":
        result["annotation"] = [entity_id]
    return result


def _pairs(subtree: dict[str, list[str]]) -> list[tuple[str, str]]:
    return [
        (entity_type, entity_id)
        for entity_type in ENTITY_TABLES
        for entity_id in subtree[entity_type]
    ]


def _rows_for_pairs(
    db: Database,
    table: str,
    type_column: str,
    id_column: str,
    pairs: Iterable[tuple[str, str]],
    *,
    columns: str = "*",
) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for entity_type, entity_id in pairs:
        grouped[entity_type].append(entity_id)
    rows: list[dict[str, Any]] = []
    for entity_type, entity_ids in grouped.items():
        for start in range(0, len(entity_ids), 500):
            chunk = entity_ids[start:start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            found = db.conn.execute(
                f"SELECT {columns} FROM {table} WHERE {type_column}=? "
                f"AND {id_column} IN ({placeholders})",
                (entity_type, *chunk),
            ).fetchall()
            rows.extend(dict(row) for row in found)
    return rows


def _rows_for_ids(
    db: Database,
    table: str,
    id_column: str,
    ids: list[str],
    *,
    columns: str = "*",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        placeholders = ", ".join("?" for _ in chunk)
        found = db.conn.execute(
            f"SELECT {columns} FROM {table} "
            f"WHERE {id_column} IN ({placeholders})",
            chunk,
        ).fetchall()
        rows.extend(dict(row) for row in found)
    return rows


def lifecycle_plan(
    db: Database, identifier: str, *, action: str
) -> dict[str, Any]:
    """Build a read-only retirement/restoration impact plan."""
    action = action.upper()
    if action not in {"RETIRE", "RESTORE"}:
        raise ValidationError(f"unsupported lifecycle action {action!r}")
    if not db.lifecycle_schema_available():
        raise ValidationError(
            "entity lifecycle requires database schema 2.7; run `operon migrate` first"
        )
    entity_type, entity_id = resolve_identifier(db, identifier)
    subtree = entity_subtree(db, entity_type, entity_id)
    entity_pairs = _pairs(subtree)
    files = _rows_for_pairs(
        db, "files", "entity_type", "entity_id", entity_pairs,
        columns="file_id, entity_type, entity_id, file_role, status, relative_path, sha256, size_bytes",
    )
    files.sort(key=lambda row: str(row["file_id"]))
    file_ids = [str(row["file_id"]) for row in files]
    current = db.current_lifecycle_event(entity_type, entity_id)
    effective = db.effective_retirements(entity_type, entity_id)

    if action == "RETIRE":
        will_change = current is None or current["action"] != "RETIRE"
        blocker = None
    else:
        will_change = current is not None and current["action"] == "RETIRE"
        blocker = None if will_change else (
            "target has no current direct RETIRE event; restore the owning retirement root"
            if effective else "target is already active"
        )

    accessions = _rows_for_pairs(
        db, "accessions", "internal_type", "internal_id", entity_pairs,
        columns="namespace, accession, internal_type, internal_id",
    )
    qc_results = _rows_for_pairs(
        db, "qc_results", "entity_type", "entity_id", entity_pairs,
        columns="qc_result_id",
    )
    decisions = _rows_for_pairs(
        db, "decisions", "entity_type", "entity_id", entity_pairs,
        columns="decision_id",
    )
    analysis_jobs = _rows_for_pairs(
        db, "analysis_jobs", "entity_type", "entity_id", entity_pairs,
        columns="job_id",
    )
    workflow_runs = _rows_for_pairs(
        db, "workflow_runs", "entity_type", "entity_id", entity_pairs,
        columns="run_id",
    )
    source_links = _rows_for_pairs(
        db, "source_links", "object_type", "object_id", entity_pairs,
        columns="source_id, object_type, object_id, relationship",
    )
    if file_ids:
        source_links.extend(_rows_for_pairs(
            db, "source_links", "object_type", "object_id",
            [("file", file_id) for file_id in file_ids],
            columns="source_id, object_type, object_id, relationship",
        ))
    remote_locations = _rows_for_ids(
        db, "file_locations", "file_id", file_ids,
        columns="file_id, location_name, status",
    ) if file_ids else []
    release_members = _rows_for_ids(
        db, "release_members", "file_id", file_ids,
        columns="release_version, file_id",
    ) if file_ids else []
    release_versions = sorted({str(row["release_version"]) for row in release_members})

    effective_after = action == "RETIRE"
    if action == "RESTORE" and will_change:
        effective_after = any(
            not (
                row["retired_by_type"] == entity_type
                and row["retired_by_id"] == entity_id
                and int(row["event_id"]) == int(current["event_id"])
            )
            for row in effective
        )

    return {
        "action": action,
        "query": identifier,
        "target": {"entity_type": entity_type, "entity_id": entity_id},
        "will_change": will_change,
        "blocker": blocker,
        "currently_effectively_retired": bool(effective),
        "effectively_retired_after": effective_after,
        "current_direct_event": current,
        "retirement_roots": effective,
        "subtree": subtree,
        "entity_counts": {kind: len(ids) for kind, ids in subtree.items()},
        "files": files,
        "reference_counts": {
            "files": len(files),
            "accessions": len(accessions),
            "qc_results": len(qc_results),
            "decisions": len(decisions),
            "analysis_jobs": len(analysis_jobs),
            "workflow_runs": len(workflow_runs),
            "source_links": len(source_links),
            "remote_locations": len(remote_locations),
            "release_members": len(release_members),
            "release_versions": len(release_versions),
        },
        "historical_release_versions": release_versions,
        "physical_changes": {
            "metadata_rows_deleted": 0,
            "artifact_bytes_deleted": 0,
            "artifact_paths_moved": 0,
            "historical_releases_modified": 0,
        },
    }


def apply_lifecycle_event(
    db: Database,
    entity_type: str,
    entity_id: str,
    *,
    action: str,
    reason: str,
    actor: str,
    reason_code: str | None = None,
    evidence: str | None = None,
    workflow_run_id: str | None = None,
) -> dict[str, Any]:
    """Append one direct RETIRE/RESTORE event and its changes audit row."""
    action = action.upper()
    reason = reason.strip()
    actor = actor.strip()
    if action not in {"RETIRE", "RESTORE"}:
        raise ValidationError(f"unsupported lifecycle action {action!r}")
    if not reason:
        raise ValidationError("lifecycle reason is required")
    if not actor:
        raise ValidationError("lifecycle actor is required")
    db.require_entity(entity_type, entity_id)
    current = db.current_lifecycle_event(entity_type, entity_id)
    if action == "RETIRE":
        normalized_code = str(reason_code or "").strip().lower()
        if normalized_code not in RETIRE_REASON_CODES:
            raise ValidationError(
                f"retire reason_code must be one of {sorted(RETIRE_REASON_CODES)}"
            )
        if current is not None and current["action"] == "RETIRE":
            return {"changed": False, "event": current}
        reverts_event_id = None
        reverts_change_id = None
        old_value, new_value = "ACTIVE", "RETIRED"
    else:
        normalized_code = "manual_restore"
        if current is None or current["action"] != "RETIRE":
            roots = db.effective_retirements(entity_type, entity_id)
            if roots:
                root_text = ", ".join(
                    f"{row['retired_by_type']} {row['retired_by_id']}" for row in roots
                )
                raise ValidationError(
                    f"{entity_type} {entity_id} is retired only through {root_text}; "
                    "restore that direct retirement root"
                )
            raise ValidationError(f"{entity_type} {entity_id} is already active")
        reverts_event_id = int(current["event_id"])
        reverts_change_id = (
            int(current["change_id"]) if current.get("change_id") is not None else None
        )
        old_value, new_value = "RETIRED", "ACTIVE"

    with db.transaction():
        cursor = db.conn.execute(
            "INSERT INTO entity_lifecycle_events "
            "(object_type, object_id, action, reason_code, reason, evidence, actor, "
            "workflow_run_id, occurred_at, reverts_event_id, change_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                entity_type, entity_id, action, normalized_code, reason, evidence,
                actor, workflow_run_id, now_iso(), reverts_event_id,
            ),
        )
        event_id = int(cursor.lastrowid)
        change_id = db.record_change(
            entity_type,
            entity_id,
            "lifecycle",
            old_value,
            new_value,
            reason=reason,
            evidence=evidence,
            actor=actor,
            workflow_run_id=workflow_run_id,
            reverts_change_id=reverts_change_id,
        )
        db.conn.execute(
            "UPDATE entity_lifecycle_events SET change_id=? WHERE event_id=?",
            (change_id, event_id),
        )
    event = db.conn.execute(
        "SELECT * FROM entity_lifecycle_events WHERE event_id=?", (event_id,)
    ).fetchone()
    return {"changed": True, "event": dict(event)}


def list_retired_entities(
    db: Database, *, direct_only: bool = False
) -> list[dict[str, Any]]:
    """List current direct retirements or their full effective descendant set."""
    if not db.lifecycle_schema_available():
        raise ValidationError(
            "entity lifecycle requires database schema 2.7; run `operon migrate` first"
        )
    if direct_only:
        rows = db.conn.execute(
            "SELECT object_type AS entity_type, object_id AS entity_id, "
            "object_type AS retired_by_type, object_id AS retired_by_id, "
            "event_id, reason_code, reason, actor, occurred_at AS retired_at "
            "FROM current_entity_lifecycle WHERE action='RETIRE' "
            "ORDER BY object_type, object_id"
        ).fetchall()
    else:
        rows = db.conn.execute(
            "SELECT * FROM effective_retired_entities "
            "ORDER BY entity_type, entity_id, event_id"
        ).fetchall()
    return [dict(row) for row in rows]
