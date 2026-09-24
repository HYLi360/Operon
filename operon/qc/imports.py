"""The shared core behind ``operon import-qc`` (CLI and TUI).

``import-qc`` accepts two input shapes and decides between them by content:

* a ``qc-measure`` JSON payload — the output of ``operon qc-measure``, the
  project-independent measurement path, or
* an external TSV table with one metric per row (``entity_type``,
  ``entity_id``, ``qc_stage``, ``metric_name``, ``metric_value``, ``tool``,
  ``tool_version``, ``parameter_set``, plus optional ``file_id`` /
  ``file_sha256``).

Both shapes are validated against the manifest before a single row is
written: file identity by ``file_id`` (cross-checked against the entity) or by
``sha256``, entity liveness, and — for a JSON payload — the payload's schema
version, producing tool, and the ``sha256``/``size_bytes`` agreement with the
manifest.  ``plan_qc_import`` runs exactly the same validation without
writing, which is what the TUI Files screen previews before its Confirm.

The CLI and the TUI call into this module, so the validation rules, the
written rows and the recorded provenance cannot drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from operon.database import Database
from operon.errors import ValidationError
from operon.schema import read_tsv
from operon.utils import now_iso
from operon.workflow import log_run

#: Columns an external TSV must carry; only ``file_id``/``file_sha256`` are optional.
TSV_COLUMNS = (
    "entity_type",
    "entity_id",
    "qc_stage",
    "metric_name",
    "metric_value",
    "tool",
    "tool_version",
    "parameter_set",
)


def is_qc_json_payload(path: Path) -> bool:
    """True when ``path`` looks like a ``qc-measure`` JSON payload."""
    if path.suffix.lower() == ".json":
        return True
    with open(path, "rb") as handle:
        return handle.read(4096).lstrip().startswith(b"{")


def _read_json_payload(db: Database, source: Path) -> tuple[dict[str, Any], Any, list, str, str | None]:
    """Parse and validate a ``qc-measure`` payload, returning (payload, file_row, metrics, tool_version, warning)."""
    from operon import __version__
    from operon.qc import MEASURE_SCHEMA_VERSION, TOOL_NAME

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"{source}: invalid qc-measure JSON payload: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != MEASURE_SCHEMA_VERSION:
        raise ValidationError(
            f"{source}: unsupported qc-measure payload schema_version "
            f"{payload.get('schema_version') if isinstance(payload, dict) else None!r}; "
            f"expected {MEASURE_SCHEMA_VERSION}"
        )
    if payload.get("tool") != TOOL_NAME:
        raise ValidationError(
            f"{source}: payload tool {payload.get('tool')!r} is not {TOOL_NAME!r}"
        )
    file_info = payload.get("file")
    if not isinstance(file_info, dict) or not file_info.get("sha256") or file_info.get("size_bytes") is None:
        raise ValidationError(f"{source}: payload is missing file identity (file.sha256/size_bytes)")
    payload_sha256 = str(file_info["sha256"]).lower()
    file_id = (str(file_info["file_id"]).strip() if file_info.get("file_id") else None) or None
    if file_id:
        file_row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
        if not file_row:
            raise ValidationError(f"{source}: file_id {file_id} does not exist")
    else:
        matches = db.conn.execute(
            "SELECT * FROM files WHERE LOWER(sha256)=?", (payload_sha256,),
        ).fetchall()
        if not matches:
            raise ValidationError(f"{source}: no manifest file matches sha256 {payload_sha256}")
        if len(matches) > 1:
            raise ValidationError(
                f"{source}: sha256 {payload_sha256} matches {len(matches)} manifest files; "
                f"re-run qc-measure with --file-id"
            )
        file_row = matches[0]
    if payload_sha256 != str(file_row["sha256"]).lower():
        raise ValidationError(f"{source}: payload sha256 does not match manifest for {file_row['file_id']}")
    if int(file_info["size_bytes"]) != int(file_row["size_bytes"]):
        raise ValidationError(f"{source}: payload size_bytes does not match manifest for {file_row['file_id']}")
    tool_version = str(payload.get("tool_version") or "")
    warning = None
    if tool_version != __version__:
        warning = (
            f"payload was measured by {TOOL_NAME} {tool_version or 'unknown'} but this "
            f"installation is {__version__}; importing anyway"
        )
    metrics = payload.get("metrics")
    if not isinstance(metrics, list):
        raise ValidationError(f"{source}: payload metrics must be a list")
    db.require_active_entity(file_row["entity_type"], file_row["entity_id"])
    return payload, file_row, metrics, tool_version, warning


def _read_tsv_rows(db: Database, source: Path) -> list[dict[str, str]]:
    """Parse and validate an external TSV, returning the rows."""
    rows = read_tsv(source)
    missing = [column for column in TSV_COLUMNS if not rows or column not in rows[0]]
    if missing:
        raise ValidationError(f"{source}: missing columns {missing}")
    for row in rows:
        db.require_active_entity(row["entity_type"], row["entity_id"])
        file_id = (row.get("file_id") or "").strip() or None
        file_sha256 = (row.get("file_sha256") or "").strip() or None
        if file_id:
            file_row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
            if not file_row:
                raise ValidationError(f"{source}: file_id {file_id} does not exist")
            if file_row["entity_type"] != row["entity_type"] or file_row["entity_id"] != row["entity_id"]:
                raise ValidationError(
                    f"{source}: file_id {file_id} belongs to "
                    f"{file_row['entity_type']} {file_row['entity_id']}, not {row['entity_type']} {row['entity_id']}"
                )
            if file_sha256 and file_sha256.lower() != str(file_row["sha256"]).lower():
                raise ValidationError(f"{source}: file_sha256 does not match manifest for {file_id}")
    return rows


def plan_qc_import(db: Database, source: str | Path) -> dict[str, Any]:
    """Validate an import without writing anything; the TUI's preview path."""
    source = Path(source)
    if is_qc_json_payload(source):
        _, file_row, metrics, tool_version, warning = _read_json_payload(db, source)
        return {
            "format": "json",
            "file_id": file_row["file_id"],
            "entity_type": file_row["entity_type"],
            "entity_id": file_row["entity_id"],
            "metric_count": len(metrics),
            "stages": sorted({str(item.get("qc_stage")) for item in metrics}),
            "tool_version": tool_version,
            "warning": warning,
        }
    rows = _read_tsv_rows(db, source)
    return {
        "format": "tsv",
        "metric_count": len(rows),
        "entities": sorted({(row["entity_type"], row["entity_id"]) for row in rows}),
        "stages": sorted({row["qc_stage"] for row in rows}),
        "tool": sorted({row["tool"] for row in rows}),
    }


def _log_import_qc_run(project, db: Database, source: str, *, started_at: str,
                       metric_count: int, payload_format: str,
                       entities: list[tuple[str, str]]) -> None:
    entity_type, entity_id = entities[0] if len(entities) == 1 else (None, None)
    log_run(db, project, {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "step": "import-qc",
        "status": "completed",
        "started_at": started_at,
        "finished_at": now_iso(),
        "command": f"operon import-qc --file {source}",
        "tool": "operon",
        "execution_details": json.dumps({
            "source": str(source),
            "format": payload_format,
            "metric_count": metric_count,
            "entities": [f"{kind}:{ident}" for kind, ident in entities],
        }, ensure_ascii=False, sort_keys=True),
    })


def _recompute_imported_qc_states(db: Database, entities: list[tuple[str, str]]) -> None:
    from operon.qc import _recompute_entity_qc_state
    for entity_type, entity_id in entities:
        _recompute_entity_qc_state(db, entity_type, entity_id)


def import_qc(db: Database, project, source: str | Path, *,
              started_at: str | None = None) -> dict[str, Any]:
    """Import QC metrics (JSON payload or external TSV) with full provenance.

    Returns the outcome the caller reports: ``format``, ``metric_count``,
    ``entities``, optionally ``file_id`` (JSON payloads) and ``warning``
    (a tool-version mismatch, which never blocks the import).
    """
    source = Path(source)
    started_at = started_at or now_iso()
    result: dict[str, Any]
    if is_qc_json_payload(source):
        from operon.qc import TOOL_NAME, _sync_sequences

        payload, file_row, metrics, tool_version, warning = _read_json_payload(db, source)
        evaluated_at = now_iso()
        count = 0
        for item in metrics:
            db.insert_qc_result({
                "entity_type": file_row["entity_type"],
                "entity_id": file_row["entity_id"],
                "file_id": file_row["file_id"],
                "file_sha256": file_row["sha256"],
                "input_identity": f"file:{file_row['file_id']}:{file_row['sha256']}",
                "qc_stage": item["qc_stage"],
                "metric_name": item["metric_name"],
                "metric_value": item.get("metric_value"),
                "metric_numeric": item.get("metric_numeric"),
                "metric_unit": item.get("metric_unit"),
                "tool": TOOL_NAME,
                "tool_version": tool_version,
                "parameter_set": item.get("parameter_set") or payload.get("parameter_set") or "external",
                "evaluated_at": evaluated_at,
            })
            count += 1
        sequences = payload.get("sequences")
        if isinstance(sequences, dict):
            _sync_sequences(db, dict(file_row),
                            {str(seqid): int(length) for seqid, length in sequences.items()})
        entities = [(file_row["entity_type"], file_row["entity_id"])]
        result = {"format": "json", "metric_count": count, "file_id": file_row["file_id"],
                  "entities": entities, "warning": warning}
    else:
        rows = _read_tsv_rows(db, source)
        count = 0
        entities = []
        for row in rows:
            file_id = (row.get("file_id") or "").strip() or None
            file_sha256 = (row.get("file_sha256") or "").strip() or None
            if file_id:
                file_row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
                file_sha256 = str(file_row["sha256"])
            try:
                numeric = float(row["metric_value"])
            except (TypeError, ValueError):
                numeric = None
            db.insert_qc_result({
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "file_id": file_id,
                "file_sha256": file_sha256,
                "input_identity": (
                    f"file:{file_id}:{file_sha256}" if file_id
                    else f"entity:{row['entity_type']}:{row['entity_id']}"
                ),
                "qc_stage": row["qc_stage"],
                "metric_name": row["metric_name"],
                "metric_value": row["metric_value"],
                "metric_numeric": numeric,
                "metric_unit": row.get("metric_unit"),
                "tool": row["tool"],
                "tool_version": row["tool_version"],
                "parameter_set": row.get("parameter_set") or "external",
                "evaluated_at": row.get("evaluated_at") or now_iso(),
            })
            entity = (row["entity_type"], row["entity_id"])
            if entity not in entities:
                entities.append(entity)
            count += 1
        result = {"format": "tsv", "metric_count": count, "entities": entities, "warning": None}

    _recompute_imported_qc_states(db, result["entities"])
    _log_import_qc_run(project, db, str(source), started_at=started_at,
                       metric_count=count, payload_format=result["format"],
                       entities=result["entities"])
    return result
