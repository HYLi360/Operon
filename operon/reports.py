"""Human-browsing exports. Wide QC tables are exports, never the source of truth."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.schema import write_tsv
from operon.utils import format_table, now_iso, sha256_file


METADATA_REPORT_TABLES = [
    "organisms", "samples", "runs", "assemblies", "annotations", "accessions", "files",
]


def export_metadata_report(db: Database, project: Project, output: str | Path | None = None) -> Path:
    """Write a derived, read-only metadata snapshot from SQLite.

    The report is deliberately one-way: exported TSV files are not a live
    mirror and are never read back automatically.
    """
    from operon.schema import Schema

    out = Path(output).resolve() if output else project.reports_root / "metadata"
    out.mkdir(parents=True, exist_ok=True)
    schema = Schema.from_file(project.schema_path)
    manifest: dict[str, Any] = {
        "report_type": "operon_metadata",
        "created_at": now_iso(),
        "metadata_schema_version": schema.version,
        "database": str(db.path),
        "tables": {},
    }
    for table in METADATA_REPORT_TABLES:
        columns = schema.columns(table)
        rows = db.export_rows(table, columns)
        path = out / f"{table}.tsv"
        write_tsv(path, columns, rows)
        manifest["tables"][path.name] = {
            "row_count": len(rows),
            "sha256": sha256_file(path),
        }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def qc_rows(db: Database, entity_type: str | None = None, entity_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM qc_results WHERE 1=1"
    params: list[Any] = []
    if entity_type:
        sql += " AND entity_type=?"
        params.append(entity_type)
    if entity_id:
        sql += " AND entity_id=?"
        params.append(entity_id)
    sql += " ORDER BY entity_type, entity_id, qc_stage, metric_name, evaluated_at DESC"
    return [dict(r) for r in db.conn.execute(sql, params).fetchall()]


def qc_wide(db: Database, entity_type: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """Pivot long QC results into a wide table for browsing/statistics."""
    rows = qc_rows(db, entity_type=entity_type)
    metric_names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row["metric_name"] not in seen:
            seen.add(row["metric_name"])
            metric_names.append(row["metric_name"])
    columns = ["entity_type", "entity_id"] + metric_names
    by_entity: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["entity_type"], row["entity_id"])
        if key not in by_entity:
            by_entity[key] = {"entity_type": key[0], "entity_id": key[1]}
            by_entity[key].update(db.latest_metrics(key[0], key[1]))
    return columns, list(by_entity.values())


def print_qc_table(db: Database, entity_type: str | None = None, entity_id: str | None = None) -> str:
    rows = qc_rows(db, entity_type, entity_id)
    headers = ["entity_type", "entity_id", "file_id", "qc_stage", "metric_name", "metric_value", "metric_unit", "tool", "evaluated_at"]
    return format_table(headers, ([r[h] for h in headers] for r in rows))


def print_status(db: Database) -> str:
    rows = db.conn.execute(
        "SELECT entity_type, entity_id, state, message, updated_at FROM entity_state ORDER BY entity_type, entity_id"
    ).fetchall()
    return format_table(["entity_type", "entity_id", "state", "message", "updated_at"], ([r[c] for c in r.keys()] for r in rows))


def print_decisions(db: Database, profile: str | None = None) -> str:
    sql = "SELECT entity_type, entity_id, profile, profile_version, decision, COALESCE(curated_decision,'') AS curated_decision, reason_codes, evaluated_at FROM current_decisions"
    params: list[Any] = []
    if profile:
        sql += " WHERE profile=?"
        params.append(profile)
    sql += " ORDER BY entity_type, entity_id, profile"
    rows = db.conn.execute(sql, params).fetchall()
    import json as _json
    def _reasons(value):
        try:
            parsed = _json.loads(value or "[]")
            return ", ".join(str(x) for x in parsed) if isinstance(parsed, list) else value or ""
        except Exception:
            return value or ""
    return format_table(["entity_type", "entity_id", "profile", "version", "decision", "curated", "reasons", "evaluated_at"], (
        [r["entity_type"], r["entity_id"], r["profile"], r["profile_version"], r["decision"], r["curated_decision"], _reasons(r["reason_codes"]), r["evaluated_at"]] for r in rows
    ))


def export_qc_tsv(db: Database, project: Project, entity_type: str | None = None) -> Path:
    out = project.qc_root / "aggregate" / "qc_results.tsv"
    columns = ["entity_type", "entity_id", "file_id", "file_sha256", "input_identity", "qc_stage", "metric_name", "metric_value", "metric_numeric", "metric_unit", "tool", "tool_version", "parameter_set", "evaluated_at"]
    rows = qc_rows(db, entity_type)
    write_tsv(out, columns, rows)
    columns_wide, rows_wide = qc_wide(db, entity_type)
    wide_out = project.qc_root / "aggregate" / "qc_results.wide.tsv"
    write_tsv(wide_out, columns_wide, rows_wide)
    return wide_out
