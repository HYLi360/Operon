"""Per-recipe input resolution for the external-tool subsystem.

Candidate file selection, reference-database paths and identity, runtime
parameter resolution, argument rendering and parameter fingerprints."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.utils import sha256_file

from ._config import Recipe
from ._probe import _identity_cache_get

_DATABASE_IDENTITY_CACHE: dict[str, tuple[float, str]] = {}


def candidate_files(
    db: Database,
    recipe: Recipe,
    entity_type: str | None = None,
    entity_id: str | None = None,
) -> list[dict[str, Any]]:
    if recipe.file_role_prefix:
        prefix = recipe.file_role_prefix.rstrip(":")
        sql = (
            "SELECT * FROM files WHERE (file_role=? OR substr(file_role, 1, ?)=?||':') "
            "AND format=? AND NOT EXISTS ("
        )
        params: list[Any] = [prefix, len(prefix) + 1, prefix, recipe.fmt]
    else:
        sql = "SELECT * FROM files WHERE file_role=? AND format=? AND NOT EXISTS ("
        params = [recipe.file_role, recipe.fmt]
    sql += (
        "SELECT 1 FROM entity_supersessions s WHERE s.object_type=files.entity_type "
        "AND s.object_id=files.entity_id) AND NOT EXISTS ("
        "SELECT 1 FROM effective_retired_entities r WHERE r.entity_type=files.entity_type "
        "AND r.entity_id=files.entity_id)"
    )
    if recipe.entity_type:
        sql += " AND entity_type=?"
        params.append(recipe.entity_type)
    if entity_type:
        sql += " AND entity_type=?"
        params.append(entity_type)
    if entity_id:
        sql += " AND entity_id=?"
        params.append(entity_id)
    sql += " ORDER BY file_id"
    return [dict(r) for r in db.conn.execute(sql, params).fetchall()]


def _resolve_database_path(project: Project, recipe: Recipe) -> Path | None:
    value = recipe.database.strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project.root / path
    return path


def _directory_fingerprint(path: Path) -> str:
    """Cheap but deterministic database-directory identity.

    For cryptographic strictness set `database_checksum` explicitly in the
    recipe; BLAST databases are large directory trees, so hashing every byte on
    each cache lookup would dominate the analysis cost.
    """
    entries: list[str] = []
    # ``is_file()`` may itself raise for an unreadable entry (Python <= 3.13
    # propagates the OSError from ``stat``), so the entry test and the stat
    # share one guard: an unreadable member must degrade into a stable marker
    # instead of aborting the whole fingerprint on some interpreters.
    for file_path in sorted(path.rglob("*")):
        try:
            if not file_path.is_file():
                continue
            stat = file_path.stat()
        except OSError:
            entries.append(f"{file_path.relative_to(path)}:unreadable")
            continue
        entries.append(
            f"{file_path.relative_to(path)}:{stat.st_size}:{stat.st_mtime_ns}"
        )
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def database_identity(
    project: Project, recipe: Recipe, location_identity: str = ""
) -> str:
    """Deterministic identity for the reference database used by a recipe.

    ``location_identity`` distinguishes the same logical database staged on
    different execution locations (e.g. an SSH remote mirror); it is only
    mixed into the digest when non-empty, so local runs keep the digest
    scheme introduced before execution backends existed.
    """
    path = _resolve_database_path(project, recipe)
    database_mode = str(recipe.raw.get("database_mode", "reference") or "reference")
    if database_mode not in {"reference", "mutable_cache"}:
        raise ValidationError(
            f"{recipe.name}: database_mode must be 'reference' or 'mutable_cache'"
        )
    if database_mode == "mutable_cache" and not recipe.database_version:
        raise ValidationError(
            f"{recipe.name}: mutable_cache requires an explicit database_version"
        )
    cache_key = json.dumps(
        {
            "path": str(path) if path is not None else "",
            "checksum": str(recipe.raw.get("database_checksum", "") or ""),
            "version": recipe.database_version,
            "mode": database_mode,
            "location": location_identity,
        },
        sort_keys=True,
    )
    cached = _identity_cache_get(_DATABASE_IDENTITY_CACHE, cache_key)
    if cached is not None:
        return cached
    digest = str(recipe.raw.get("database_checksum", "") or "")
    if database_mode == "mutable_cache":
        digest = f"mutable-cache:{digest.lower()}" if digest else "mutable-cache"
    elif path is not None:
        if digest:
            digest = f"sha256:{digest.lower()}"
        elif path.exists():
            if path.is_file():
                digest = f"sha256:{sha256_file(path)}"
            elif path.is_dir():
                digest = f"dir:{_directory_fingerprint(path)}"
            else:
                digest = "unreadable"
        else:
            digest = "missing"
    else:
        digest = "none"
    canonical = {
        "path": str(path) if path is not None else "",
        "digest": digest,
        "database_version": recipe.database_version,
        "database_mode": database_mode,
    }
    if location_identity:
        canonical["location"] = location_identity
    identity = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    _DATABASE_IDENTITY_CACHE[cache_key] = (time.monotonic(), identity)
    return identity


def resolve_runtime_parameters(
    recipe: Recipe, supplied: dict[str, str] | None = None
) -> dict[str, str]:
    """Validate caller-supplied values against the recipe declaration."""
    supplied = dict(supplied or {})
    unknown = sorted(set(supplied) - set(recipe.parameters))
    if unknown:
        raise ValidationError(
            f"{recipe.name}: undeclared runtime parameter(s): {', '.join(unknown)}; "
            f"declared: {', '.join(sorted(recipe.parameters)) or '(none)'}"
        )
    resolved: dict[str, str] = {}
    for name, spec in recipe.parameters.items():
        if name in supplied:
            value: Any = supplied[name]
        elif "default" in spec:
            value = spec["default"]
        elif bool(spec.get("required", False)):
            raise ValidationError(
                f"{recipe.name}: missing required runtime parameter {name!r}; "
                f"pass --param {name}=VALUE"
            )
        else:
            continue
        if isinstance(value, (dict, list)) or value is None:
            raise ValidationError(
                f"{recipe.name}: parameter {name!r} must be a scalar value"
            )
        rendered = str(value)
        choices = spec.get("choices")
        if choices is not None:
            if not isinstance(choices, list):
                raise ValidationError(
                    f"{recipe.name}: parameter {name!r} choices must be a list"
                )
            allowed = {str(choice) for choice in choices}
            if rendered not in allowed:
                raise ValidationError(
                    f"{recipe.name}: parameter {name!r} must be one of "
                    f"{', '.join(sorted(allowed))}, got {rendered!r}"
                )
        pattern = str(spec.get("pattern", "") or "")
        if pattern and re.fullmatch(pattern, rendered) is None:
            raise ValidationError(
                f"{recipe.name}: parameter {name!r} value {rendered!r} "
                f"does not match pattern {pattern!r}"
            )
        resolved[name] = rendered
    return resolved


def render_arguments(
    recipe: Recipe,
    *,
    input_path: Path,
    output_path: Path,
    database_path: Path | None,
    threads: int,
    file_record: dict[str, Any],
    runtime_parameters: dict[str, str] | None = None,
    work_dir: Path | None = None,
    arguments: list[str] | None = None,
) -> list[str]:
    context = {
        "input": str(input_path),
        "input_parent": str(input_path.parent),
        "input_name": input_path.name,
        "input_stem": input_path.stem,
        "output": str(output_path),
        "output_parent": str(output_path.parent),
        "output_name": output_path.name,
        "output_stem": output_path.stem,
        "database": str(database_path) if database_path is not None else "",
        "threads": str(threads),
        "file_id": str(file_record["file_id"]),
        "file_role": str(file_record["file_role"]),
        "entity_type": str(file_record["entity_type"]),
        "entity_id": str(file_record["entity_id"]),
    }
    if work_dir is not None:
        context["work_dir"] = str(work_dir)
    context.update(runtime_parameters or {})
    rendered: list[str] = []
    for arg in arguments if arguments is not None else recipe.arguments:
        value = arg
        for key, replacement in context.items():
            value = value.replace("${" + key + "}", replacement)
        unresolved = re.findall(r"\$\{[^}]+\}", value)
        if unresolved:
            raise ValidationError(
                f"{recipe.name}: unresolved placeholder(s) in arguments: "
                f"{', '.join(unresolved)}"
            )
        rendered.append(value)
    return rendered


def parameter_fingerprint(
    recipe: Recipe,
    args: list[str],
    threads: int,
    tool_version: str,
    runtime_parameters: dict[str, str] | None = None,
    commands: list[list[str]] | None = None,
    command_versions: list[list[str]] | None = None,
) -> str:
    payload = {
        "analysis_name": recipe.name,
        "tool": recipe.tool_name,
        "tool_version": tool_version,
        "arguments": args,
        "runtime_parameters": runtime_parameters or {},
        "threads": threads,
        "parser": recipe.result_parser,
        "max_hits_per_query": recipe.max_hits_per_query,
        "input_kind": recipe.input_kind,
        "output_kind": recipe.output_kind,
        "output_name": recipe.output_name_template,
        "output_suffix": recipe.output_suffix,
        "result_glob": str(recipe.raw.get("result_glob", "") or ""),
    }
    if commands is not None:
        payload["commands"] = commands
    if command_versions is not None:
        # [executable, probed version] per chain step: upgrading any step's
        # program invalidates the exact cache even when the recipe text and
        # the primary tool version are unchanged.
        payload["command_versions"] = command_versions
    for key in (
        "qstart_column",
        "qend_column",
        "sstart_column",
        "send_column",
        "evalue_column",
        "bitscore_column",
        "pident_column",
        "hmmer_mode",
        "environment_policy",
    ):
        if recipe.raw.get(key) is not None:
            payload[key] = str(recipe.raw[key])
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
