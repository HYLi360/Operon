"""Derived-artifact adoption: register external workflow outputs with lineage.

External workflow managers (snakemake, nextflow, ...) produce derived
artifacts outside the project.  ``adopt_files`` materializes them under
``analysis/adopted/<entity_id>/``, registers them through the same idempotent
ingest path as raw files (same bytes -> reuse, different bytes for the same
entity+role -> ConflictError), and records file-to-file lineage edges in
``file_lineage``.  Once adopted, a derived artifact is a first-class manifest
member: it can be QC'd, evaluated, exported, released, and selected by
``candidate_files`` as the input of a downstream recipe (cascading analysis).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.files import ingest_file
from operon.schema import read_tsv
from operon.utils import now_iso
from operon.workflow import log_run

# Derived artifacts live in their own area under the analysis root, never in
# the immutable raw/ archive.
ADOPTED_SUBDIR = "adopted"

MANIFEST_COLUMNS = [
    "path", "entity_type", "entity_id", "role", "format", "compression", "derived_from",
]

_ITEM_REQUIRED = ("path", "entity_type", "entity_id", "role", "derived_from")


def _normalize_derived_from(value: Any, index: int) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, Iterable):
        items = [str(part).strip() for part in value]
    else:
        raise ValidationError(
            f"adopt item {index}: derived_from must be a list of file_ids "
            "or a comma-separated string"
        )
    items = [item for item in items if item]
    if not items:
        raise ValidationError(f"adopt item {index}: derived_from requires at least one file_id")
    return items


def normalize_adopt_item(raw: dict[str, Any], index: int) -> dict[str, Any]:
    """Validate and normalize one adopt item without touching disk or DB."""
    if not isinstance(raw, dict):
        raise ValidationError(f"adopt item {index}: must be a mapping, got {type(raw).__name__}")
    for field in _ITEM_REQUIRED:
        if raw.get(field) in (None, "", []):
            raise ValidationError(f"adopt item {index}: {field!r} is required")
    return {
        "path": str(raw["path"]).strip(),
        "entity_type": str(raw["entity_type"]).strip(),
        "entity_id": str(raw["entity_id"]).strip(),
        "role": str(raw["role"]).strip(),
        "format": (str(raw["format"]).strip() or None) if raw.get("format") else None,
        "compression": (str(raw["compression"]).strip() or None) if raw.get("compression") else None,
        "derived_from": _normalize_derived_from(raw["derived_from"], index),
        "workflow_run_id": (str(raw["workflow_run_id"]).strip() or None)
        if raw.get("workflow_run_id") else None,
    }


def load_adopt_manifest(path: str | Path) -> list[dict[str, Any]]:
    """Load a batch adopt manifest (JSON list or TSV with a header row).

    JSON: a list of item mappings; ``derived_from`` is a list of file_ids (a
    comma-separated string is also accepted).  TSV: header must contain
    ``path, entity_type, entity_id, role, derived_from``; ``format``,
    ``compression`` and ``workflow_run_id`` columns are optional and
    ``derived_from`` carries comma-separated file_ids.
    """
    path = Path(path)
    if not path.is_file():
        raise ValidationError(f"adopt manifest not found: {path}")
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json" or text.lstrip().startswith("["):
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{path}: invalid JSON adopt manifest: {exc}") from exc
        if not isinstance(document, list):
            raise ValidationError(f"{path}: JSON adopt manifest must be a list of items")
        rows: list[dict[str, Any]] = document
    else:
        rows = read_tsv(path, required_header=["path", "entity_type", "entity_id", "role",
                                               "derived_from"])
    return [normalize_adopt_item(row, index) for index, row in enumerate(rows, start=1)]


def adopt_files(
        db: Database,
        project: Project,
        *,
        items: Iterable[dict[str, Any]],
        actor: str = "adopt",
) -> list[dict[str, Any]]:
    """Register derived artifacts and their lineage edges as one batch.

    The whole batch is validated first (paths exist, entities are active,
    every ``derived_from`` file_id is registered); any failure aborts before
    anything is materialized or registered.  Each item is then archived under
    ``analysis/adopted/<entity_id>/`` via the ingest path (inheriting its
    idempotency/conflict semantics) and its lineage edges are inserted with
    INSERT OR IGNORE, so repeating the same adopt is a no-op.
    """
    normalized = [normalize_adopt_item(item, index) for index, item in enumerate(items, start=1)]
    if not normalized:
        raise ValidationError("adopt requires at least one item")

    # Phase 1: validate the entire batch before touching disk or DB, so a bad
    # item never leaves a half-registered batch behind.
    for item in normalized:
        db.require_active_entity(item["entity_type"], item["entity_id"])
        source = Path(item["path"])
        if not source.is_absolute():
            source = project.root / source
        if not source.exists():
            raise ValidationError(f"adopt item source does not exist: {source}")
        item["path"] = str(source)
        for input_file_id in item["derived_from"]:
            row = db.conn.execute(
                "SELECT file_id FROM files WHERE file_id=?", (input_file_id,)
            ).fetchone()
            if row is None:
                raise ValidationError(
                    f"adopt item {item['entity_id']}/{item['role']}: "
                    f"derived_from file {input_file_id} is not registered"
                )

    results: list[dict[str, Any]] = []
    for item in normalized:
        record = ingest_file(
            db, project, item["path"], item["entity_type"], item["entity_id"], item["role"],
            fmt=item["format"], compression=item["compression"],
            source_url=item["path"],
            archive_root=project.analysis_root / ADOPTED_SUBDIR,
        )
        with db.transaction() as conn:
            for input_file_id in item["derived_from"]:
                conn.execute(
                    "INSERT OR IGNORE INTO file_lineage"
                    "(derived_file_id, input_file_id, workflow_run_id, created_at) "
                    "VALUES(?,?,?,?)",
                    (record["file_id"], input_file_id, item["workflow_run_id"], now_iso()),
                )
        results.append({
            "file_id": record["file_id"],
            "entity_type": record["entity_type"],
            "entity_id": record["entity_id"],
            "role": record["file_role"],
            "format": record["format"],
            "compression": record["compression"],
            "relative_path": record["relative_path"],
            "sha256": record["sha256"],
            "size_bytes": record["size_bytes"],
            "derived_from": item["derived_from"],
            "workflow_run_id": item["workflow_run_id"],
        })

    log_run(db, project, {
        "step": "adopt",
        "status": "completed",
        "command": f"operon adopt ({len(results)} item(s))",
        "tool": "operon",
        "execution_details": json.dumps({
            "actor": actor,
            "items": [
                {
                    "file_id": result["file_id"],
                    "entity_type": result["entity_type"],
                    "entity_id": result["entity_id"],
                    "role": result["role"],
                    "relative_path": result["relative_path"],
                    "derived_from": result["derived_from"],
                    "workflow_run_id": result["workflow_run_id"],
                }
                for result in results
            ],
        }, ensure_ascii=False, sort_keys=True),
    })
    return results
