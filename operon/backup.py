"""Consistent, checksum-manifested project backups."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from operon import __version__
from operon.config import Project
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.utils import iter_directory_entries, now_iso, sha256_file

SCOPE_PATHS = {
    "control": ["project.yaml", "config", "logs"],
    "results": ["project.yaml", "config", "logs", "qc", "analysis", "reports", "taxonomy", "releases"],
    "full": [
        "project.yaml", "config", "logs", "qc", "analysis", "reports", "taxonomy", "releases",
        "raw", "standardized", ".operon", "metadata", "examples",
    ],
}


def _copy_known_path(project: Project, relative: str, destination: Path) -> None:
    source = project.root / relative
    if not source.exists():
        return
    target = destination / relative
    if source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _rebase_standardized_links(project: Project, staging: Path) -> None:
    """Make generated views portable without altering archived artifact trees."""
    root = staging / "standardized"
    if not root.is_dir():
        return
    for link in iter_directory_entries(root):
        if not link.is_symlink():
            continue
        target = Path(os.readlink(link))
        if not target.is_absolute():
            continue
        try:
            relative = target.relative_to(project.root)
        except ValueError:
            # External targets are preserved as link metadata, not read or
            # copied. The backup does not claim to archive their referents.
            continue
        relocated = staging / relative
        link.unlink()
        link.symlink_to(os.path.relpath(relocated, link.parent))


def create_backup(db: Database, project: Project, output: str | Path, scope: str = "control") -> dict[str, Any]:
    if scope not in SCOPE_PATHS:
        raise ValidationError(f"backup scope must be one of {sorted(SCOPE_PATHS)}")
    output = Path(output).resolve()
    if output.exists():
        raise ConflictError(f"backup destination already exists: {output}")
    try:
        output.relative_to(project.root.resolve())
    except ValueError:
        pass
    else:
        raise ValidationError("backup destination must be outside the project root")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=str(output.parent)))
    try:
        for relative in SCOPE_PATHS[scope]:
            _copy_known_path(project, relative, staging)
        _rebase_standardized_links(project, staging)
        backup_db = staging / "operon.sqlite"
        destination = sqlite3.connect(backup_db)
        try:
            db.conn.backup(destination)
        finally:
            destination.close()
        files: list[dict[str, Any]] = []
        for path in iter_directory_entries(staging):
            if path.is_symlink():
                files.append({
                    "relative_path": path.relative_to(staging).as_posix(),
                    "type": "symlink", "target": os.readlink(path),
                })
                continue
            if not path.is_file():
                continue
            files.append({
                "relative_path": path.relative_to(staging).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
        manifest = {
            "backup_format": 2,
            "created_at": now_iso(),
            "operon_version": __version__,
            "project_id": project.project_id,
            "scope": scope,
            "files": files,
        }
        (staging / "backup-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        staging.rename(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"path": str(output), "scope": scope, "file_count": len(files)}


def verify_backup(path: str | Path) -> dict[str, Any]:
    path = Path(path).resolve()
    manifest_path = path / "backup-manifest.json"
    if not manifest_path.exists():
        raise ValidationError(f"backup manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read backup manifest: {exc}") from exc
    failures: list[dict[str, str]] = []
    manifest_files = manifest.get("files", [])
    expected_paths: set[str] = set()
    for item in manifest_files:
        relative = str(item.get("relative_path", ""))
        expected_paths.add(relative)
        candidate = path / relative
        is_link = manifest.get("backup_format", 1) == 2 and item.get("type") == "symlink"
        try:
            # A link entry authenticates its own text; never follow its target.
            # Parent directories must still be contained in the backup.
            (candidate.parent.resolve() if is_link else candidate.resolve()).relative_to(path)
        except ValueError:
            failures.append({"relative_path": relative, "error": "unsafe path"})
            continue
        if is_link:
            if not candidate.is_symlink():
                failures.append({"relative_path": relative, "error": "missing symlink"})
            elif os.readlink(candidate) != item.get("target"):
                failures.append({"relative_path": relative, "error": "symlink target mismatch"})
            continue
        if manifest.get("backup_format", 1) == 2 and candidate.is_symlink():
            failures.append({"relative_path": relative, "error": "unexpected symlink"})
            continue
        if not candidate.is_file():
            failures.append({"relative_path": relative, "error": "missing"})
            continue
        if candidate.stat().st_size != int(item["size_bytes"]):
            failures.append({"relative_path": relative, "error": "size mismatch"})
            continue
        if sha256_file(candidate) != item["sha256"]:
            failures.append({"relative_path": relative, "error": "checksum mismatch"})
    actual_paths = {
        candidate.relative_to(path).as_posix()
        for candidate in iter_directory_entries(path)
        if (candidate.is_file() or candidate.is_symlink()) and candidate != manifest_path
    }
    unexpected_paths = sorted(actual_paths - expected_paths)
    failures.extend(
        {"relative_path": relative, "error": "unexpected file"}
        for relative in unexpected_paths
    )
    return {
        "path": str(path),
        "scope": manifest.get("scope"),
        "checked": len(manifest_files),
        "unexpected": len(unexpected_paths),
        "ok": not failures,
        "failures": failures,
    }
