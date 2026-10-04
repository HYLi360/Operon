"""Import versioned metric facts and reconstruct adopt drafts from event ledgers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from operon.contracts import validate_payload
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.hits_import import canonical_hash, validate_file_identity, workflow_details
from operon.lineage import normalize_adopt_item
from operon.qc.imports import _recompute_imported_qc_states
from operon.utils import atomic_write_text, now_iso, sha256_file
from operon.workflow import flush_run_log, log_run


def _draft_text(ledger: dict[str, Any]) -> str:
    drafts = [
        item["artifact"] for _, item in sorted(ledger.items()) if item.get("artifact")
    ]
    return json.dumps(drafts, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def _metric(
    db: Database, parent: dict[str, Any], event: dict[str, Any]
) -> dict[str, Any]:
    metric = dict(event["metric"])
    db.require_active_entity(metric["entity_type"], metric["entity_id"])
    if parent["entity_type"] and (parent["entity_type"], parent["entity_id"]) != (
        metric["entity_type"],
        metric["entity_id"],
    ):
        raise ValidationError("event entity does not match the producing run")
    info = metric.pop("file", None)
    if info:
        file = validate_file_identity(db, info)
        if (file["entity_type"], file["entity_id"]) != (
            metric["entity_type"],
            metric["entity_id"],
        ):
            raise ValidationError("metric file belongs to a different entity")
        metric.update(
            file_id=file["file_id"],
            file_sha256=file["sha256"],
            input_identity=f"file:{file['file_id']}:{file['sha256']}",
        )
    else:
        metric["input_identity"] = (
            f"entity:{metric['entity_type']}:{metric['entity_id']}"
        )
    metric["metric_value"] = str(metric["metric_value"])
    # Each event remains independently attributable even for equal metric keys.
    metric["parameter_set"] += (
        f":events:{canonical_hash([parent['run_id'], event['event_id']])[:16]}"
    )
    metric["evaluated_at"] = now_iso()
    return metric


def _artifact(
    db: Database, project, parent: dict[str, Any], event: dict[str, Any], index: int
) -> dict[str, Any]:
    item = normalize_adopt_item(event["artifact"], index)
    db.require_active_entity(item["entity_type"], item["entity_id"])
    if parent["entity_type"] and (parent["entity_type"], parent["entity_id"]) != (
        item["entity_type"],
        item["entity_id"],
    ):
        raise ValidationError("artifact entity does not match the producing run")
    if item["workflow_run_id"] not in {None, parent["run_id"]}:
        raise ValidationError("artifact workflow_run_id does not match producing run")
    item["workflow_run_id"] = parent["run_id"]
    source = Path(item["path"])
    if not source.is_absolute():
        source = project.root / source
    item["path"] = str(source.absolute())
    for file_id in item["derived_from"]:
        if (
            db.conn.execute(
                "SELECT 1 FROM files WHERE file_id=?", (file_id,)
            ).fetchone()
            is None
        ):
            raise ValidationError(
                f"artifact derived_from file {file_id} does not exist"
            )
    return item


def _prepare(
    db: Database, project, run_id: str, source: str | Path, output: str | Path | None
) -> dict[str, Any]:
    parent, details = workflow_details(db, run_id)
    ledger = details.get("event_imports", {})
    if not isinstance(ledger, dict) or any(
        not isinstance(v, dict) for v in ledger.values()
    ):
        raise ValidationError("event_imports ledger must be an object of entries")
    ledger = dict(ledger)
    previous_text = _draft_text(ledger)
    if Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValidationError("event run ID is not safe for a draft filename")
    out = (
        Path(output)
        if output
        else project.analysis_root / "event-drafts" / f"{run_id}.json"
    )
    if (
        out.resolve() == Path(source).resolve()
        or out.resolve() == project.db_path.resolve()
    ):
        raise ValidationError("event draft must not replace its input or the database")
    previous_bytes = out.read_bytes() if out.exists() else None
    metrics = []
    imported = skipped = duplicates = 0
    try:
        lines = Path(source).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValidationError(f"invalid event file {source}: {exc}") from exc
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise ValidationError(f"{source}:{index}: invalid event JSON") from exc
        validate_payload(event, "events")
        digest = canonical_hash(event)
        event_id = event["event_id"]
        if event_id in ledger:
            if ledger[event_id].get("sha256") != digest:
                raise ConflictError(
                    f"event {event_id!r} has different content in run {run_id}"
                )
            duplicates += 1
            continue
        entry = {"sha256": digest, "type": event["type"]}
        if event["type"] == "metric":
            metrics.append(_metric(db, parent, event))
        elif event["type"] == "artifact":
            entry["artifact"] = _artifact(db, project, parent, event, index)
        else:
            skipped += 1
        ledger[event_id] = entry
        imported += 1
    text = _draft_text(ledger)
    publish = text != "[]\n" or output is not None
    if (
        publish
        and previous_bytes is not None
        and previous_bytes != text.encode()
        and previous_bytes != previous_text.encode()
    ):
        raise ConflictError(f"event draft destination contains unrelated bytes: {out}")
    return {
        "parent": parent,
        "details": details,
        "ledger": ledger,
        "metrics": metrics,
        "imported": imported,
        "skipped": skipped,
        "duplicates": duplicates,
        "out": out,
        "draft": text,
        "previous": previous_bytes,
        "publish": publish,
    }


def plan_events_import(
    db: Database,
    project,
    run_id: str,
    source: str | Path,
    *,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Validate the whole JSONL and proposed draft without writing anything."""
    plan = _prepare(db, project, run_id, source, output)
    return {key: plan[key] for key in ("imported", "skipped", "duplicates")} | {
        "metric_count": len(plan["metrics"]),
        "draft": str(plan["out"]) if plan["publish"] else None,
    }


def import_events(
    db: Database,
    project,
    run_id: str,
    source: str | Path,
    *,
    output: str | Path | None = None,
) -> dict[str, Any]:
    """Commit metric facts and deduplication atomically with an adopt draft."""
    buffer: list[dict[str, Any]] = []
    plan = None
    published = False
    try:
        with db.transaction():
            plan = _prepare(db, project, run_id, source, output)
            if plan["imported"]:
                entities = sorted(
                    {(m["entity_type"], m["entity_id"]) for m in plan["metrics"]}
                )
                for metric in plan["metrics"]:
                    db.insert_qc_result(metric)
                _recompute_imported_qc_states(db, entities)
                plan["details"]["event_imports"] = plan["ledger"]
                db.conn.execute(
                    "UPDATE workflow_runs SET execution_details=? WHERE run_id=?",
                    (
                        json.dumps(plan["details"], ensure_ascii=False, sort_keys=True),
                        run_id,
                    ),
                )
                record = log_run(
                    db,
                    project,
                    {
                        "parent_run_id": run_id,
                        "step": "import-events",
                        "status": "completed",
                        "tool": "operon",
                        "input_sha256": sha256_file(source),
                        "execution_details": json.dumps(
                            {
                                "source": str(source),
                                "imported": plan["imported"],
                                "skipped": plan["skipped"],
                                "draft": str(plan["out"]) if plan["publish"] else None,
                            },
                            sort_keys=True,
                        ),
                    },
                    jsonl_buffer=buffer,
                )
                db.record_change(
                    "workflow_runs",
                    run_id,
                    "event_imports",
                    None,
                    canonical_hash(plan["ledger"]),
                    reason="import plugin event facts",
                    evidence=str(source),
                    workflow_run_id=record["run_id"],
                )
            if plan["publish"] and plan["previous"] != plan["draft"].encode():
                atomic_write_text(plan["out"], plan["draft"])
                published = True
    except BaseException:
        if published:
            if plan["previous"] is None:
                plan["out"].unlink(missing_ok=True)
            else:
                atomic_write_text(plan["out"], plan["previous"].decode("utf-8"))
        raise
    flush_run_log(project, buffer)
    return {key: plan[key] for key in ("imported", "skipped", "duplicates")} | {
        "metric_count": len(plan["metrics"]),
        "draft": str(plan["out"]) if plan["publish"] else None,
    }
