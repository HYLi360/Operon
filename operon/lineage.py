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
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.files import archive_target, detect_compression, detect_format, find_existing_file, ingest_file
from operon.schema import read_tsv
from operon.utils import now_iso, path_size_bytes, sha256_path
from operon.workflow import flush_run_log, log_run

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

    Preflight checks the whole batch, including content conflicts. Registration,
    lineage, state, and workflow rows commit together. On failure or a handled
    interruption, only newly created archive targets are removed; existing
    artifacts are preserved. JSONL records are flushed after the DB commit.
    """
    normalized = [normalize_adopt_item(item, index) for index, item in enumerate(items, start=1)]
    if not normalized:
        raise ValidationError("adopt requires at least one item")

    created_targets: list[Path] = []
    jsonl_buffer: list[dict[str, Any]] = []
    try:
        with db.transaction():
            _validate_adopt_batch(db, project, normalized)
            results = _adopt_validated(db, project, normalized, actor, created_targets, jsonl_buffer)
    except BaseException:
        for target in reversed(created_targets):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        raise
    flush_run_log(project, jsonl_buffer)
    return results


def _validate_adopt_batch(db: Database, project: Project, normalized: list[dict[str, Any]]) -> None:
    identities: dict[tuple[str, str, str], tuple[str, int]] = {}
    for item in normalized:
        db.require_active_entity(item["entity_type"], item["entity_id"])
        source = Path(item["path"])
        if not source.is_absolute():
            source = project.root / source
        if not (source.is_file() or source.is_dir()):
            raise ValidationError(f"adopt item source does not exist: {source}")
        item["path"] = str(source)
        item["format"] = item["format"] or detect_format(source, item["role"])
        item["compression"] = item["compression"] or detect_compression(source)
        if (source.is_dir() != (item["format"] == "directory")
                or (source.is_dir() and item["compression"] != "none")):
            raise ValidationError(f"adopt item has incompatible format/compression: {source}")
        target = archive_target(
            project, item["entity_type"], item["entity_id"], item["role"],
            item["format"], item["compression"], project.analysis_root / ADOPTED_SUBDIR,
        )
        identity = (sha256_path(source), path_size_bytes(source))
        key = (item["entity_type"], item["entity_id"], item["role"])
        if key in identities and identities[key] != identity:
            raise ConflictError(f"adopt batch has conflicting bytes for {key}")
        identities[key] = identity
        conflicts = db.conn.execute(
            "SELECT file_id FROM files WHERE entity_type=? AND entity_id=? AND file_role=? "
            "AND (sha256<>? OR size_bytes<>?) LIMIT 1", (*key, *identity),
        ).fetchone()
        if conflicts is not None:
            raise ConflictError(f"adopt item conflicts with registered file {conflicts['file_id']}")
        existing = find_existing_file(db, *key, identity[0])
        reused = False
        if existing is not None:
            existing_path = project.root / existing["relative_path"]
            try:
                existing_path.resolve().relative_to(project.root.resolve())
            except (ValueError, RuntimeError) as exc:
                raise ValidationError(f"adopt manifest path escapes the project: {existing_path}") from exc
            reused = existing_path.exists() and (
                sha256_path(existing_path), path_size_bytes(existing_path)
            ) == identity
        if not reused and (target.exists() or target.is_symlink()):
            if not target.exists() or (sha256_path(target), path_size_bytes(target)) != identity:
                raise ConflictError(f"adopt target is occupied by different content: {target}")
        item["_target"] = target
        for input_file_id in item["derived_from"]:
            row = db.conn.execute(
                "SELECT file_id FROM files WHERE file_id=?", (input_file_id,)
            ).fetchone()
            if row is None:
                raise ValidationError(
                    f"adopt item {item['entity_id']}/{item['role']}: "
                    f"derived_from file {input_file_id} is not registered"
                )


def _adopt_validated(db: Database, project: Project, normalized: list[dict[str, Any]],
                     actor: str, created_targets: list[Path],
                     jsonl_buffer: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in normalized:
        target = item["_target"]
        if not (target.exists() or target.is_symlink()):
            created_targets.append(target)
        record = ingest_file(
            db, project, item["path"], item["entity_type"], item["entity_id"], item["role"],
            fmt=item["format"], compression=item["compression"],
            source_url=item["path"],
            archive_root=project.analysis_root / ADOPTED_SUBDIR,
            provenance_buffer=jsonl_buffer,
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
    }, jsonl_buffer=jsonl_buffer)
    return results
