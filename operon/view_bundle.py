"""Deterministic, read-only visualization snapshots for independent consumers."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.reports import qc_rows, qc_wide, render_report_rows
from operon.schema import ENTITY_ID_COLUMNS, ENTITY_TABLES, _tsv_cell, write_tsv
from operon.sql import quote_identifier
from operon.utils import sha256_file

BUNDLE_SCHEMA_VERSION = 1
BUNDLE_TABLES = {
    "files.tsv": "files",
    "decisions.tsv": "current_decisions",
    "analysis_results.tsv": "analysis_results",
    "analysis_hits.tsv": "analysis_hits",
    "analysis_alignments.tsv": "analysis_alignments",
    "lineage.tsv": "file_lineage",
    "coverage/reports.tsv": "coverage_reports",
    "coverage/metrics.tsv": "coverage_report_metrics",
}


def render_qc_report(
    db: Database,
    fmt: str,
    *,
    wide: bool = False,
    entity_type: str | None = None,
    entity_id: str | None = None,
    include_retired: bool = False,
) -> str:
    """Render QC with the same wide pivot and escaped TSV used by the bundle."""
    if wide:
        columns, rows = qc_wide(db, entity_type, include_retired=include_retired)
        if entity_id:
            rows = [row for row in rows if row["entity_id"] == entity_id]
    else:
        columns = db.table_columns("qc_results")
        rows = qc_rows(db, entity_type, entity_id, include_retired=include_retired)
    if fmt != "tsv":
        return render_report_rows(rows, fmt, headers=columns)
    lines = ["\t".join(_tsv_cell(column) for column in columns)]
    lines.extend(
        "\t".join("" if row.get(c) is None else _tsv_cell(row[c]) for c in columns)
        for row in rows
    )
    return "\n".join(lines) + "\n"


def _entities(db: Database) -> list[dict[str, Any]]:
    rows = []
    for kind, table in ENTITY_TABLES.items():
        column = quote_identifier(ENTITY_ID_COLUMNS[kind])
        for row in db.conn.execute(
            f"SELECT {column} AS entity_id FROM {quote_identifier(table)} ORDER BY {column}"
        ):
            ident = row["entity_id"]
            rows.append(
                {
                    "entity_type": kind,
                    "entity_id": ident,
                    "state": db.get_entity_state(kind, ident),
                    "retired": int(db.is_entity_retired(kind, ident)),
                }
            )
    return sorted(rows, key=lambda row: (row["entity_type"], row["entity_id"]))


def _same_bundle(left: Path, right: Path) -> bool:
    left_members = sorted(p.relative_to(left) for p in left.rglob("*") if p.is_file())
    right_members = sorted(
        p.relative_to(right) for p in right.rglob("*") if p.is_file()
    )
    return left_members == right_members and all(
        not (left / p).is_symlink()
        and (left / p).read_bytes() == (right / p).read_bytes()
        for p in left_members
    )


def export_view_bundle(db: Database, output: str | Path) -> Path:
    """Stage a consistent snapshot, then publish or reuse identical bytes."""
    out = Path(output).absolute()
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise ConflictError(f"bundle destination is not a plain directory: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{out.name}.", dir=out.parent))
    owns_snapshot = not db.conn.in_transaction
    try:
        if owns_snapshot:
            db.conn.execute("BEGIN")
        members: dict[str, Any] = {}
        entities = _entities(db)
        write_tsv(
            stage / "entities.tsv",
            ["entity_type", "entity_id", "state", "retired"],
            entities,
        )
        members["entities.tsv"] = {"row_count": len(entities)}
        wide = render_qc_report(db, "tsv", wide=True)
        (stage / "qc_wide.tsv").write_text(wide, encoding="utf-8", newline="")
        members["qc_wide.tsv"] = {"row_count": len(qc_wide(db)[1])}
        for name, table in BUNDLE_TABLES.items():
            columns = db.table_columns(table)
            if not columns:
                raise ValidationError(
                    f"view bundle requires table {table}; migrate the project first"
                )
            order = ", ".join(quote_identifier(c) for c in columns)
            rows = [
                dict(row)
                for row in db.conn.execute(
                    f"SELECT * FROM {quote_identifier(table)} ORDER BY {order}"
                )
            ]
            write_tsv(stage / name, columns, rows)
            members[name] = {"row_count": len(rows)}
        for name, details in members.items():
            details["sha256"] = sha256_file(stage / name)
        (stage / "manifest.json").write_text(
            json.dumps(
                {"bundle_schema_version": BUNDLE_SCHEMA_VERSION, "members": members},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if out.exists():
            if not _same_bundle(out, stage):
                raise ConflictError(
                    f"bundle destination contains different bytes: {out}"
                )
        else:
            stage.rename(out)
        return out
    finally:
        if owns_snapshot and db.conn.in_transaction:
            db.conn.rollback()
        if stage.exists():
            shutil.rmtree(stage)
