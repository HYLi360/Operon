"""Write actions for the Operon TUI (phase 2).

The TUI never reimplements business logic: every function here calls the
same core functions the CLI uses, so audit rows, provenance, and semantics
are identical to the equivalent ``operon`` command.  Each public function
opens its own short-lived *writable* ``Database`` connection, does the work,
closes it, and returns plain dicts.  Writable connections are only ever
opened inside this module — the UI layer never holds one.

``lifecycle_preview`` is the single exception: it is a read-only plan
preview, so it uses a read-only connection like :mod:`operon.tui.data`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import yaml

from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.utils import atomic_write_text


@contextmanager
def _open_writable(project: Project) -> Iterator[Database]:
    db = Database(project.db_path)
    try:
        yield db
    finally:
        db.close()


def evaluate(
        project: Project,
        entity_type: str | None = None,
        entity_id: str | None = None,
        profile: str | None = None,
) -> list[dict[str, Any]]:
    """Evaluate decisions like ``operon evaluate``; returns summary rows."""
    from operon.rules import evaluate_all, evaluate_entity

    with _open_writable(project) as db:
        if entity_id:
            if not entity_type:
                raise ValidationError("--entity-type is required when --entity-id is given")
            rows = [evaluate_entity(db, project, entity_type, entity_id, profile)]
        else:
            rows = evaluate_all(db, project, profile, entity_type)
    return [
        {
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "profile": row["profile"],
            "decision": row["decision"],
            "reason_codes": json.loads(row.get("reason_codes") or "[]"),
        }
        for row in rows
    ]


def curate(
        project: Project,
        entity_type: str,
        entity_id: str,
        profile: str,
        decision: str,
        reviewer: str,
        reason: str,
        evidence: str | None = None,
) -> None:
    """Record a curated decision like ``operon curate`` (audited override)."""
    from operon.rules import curate_decision

    with _open_writable(project) as db:
        curate_decision(db, entity_type, entity_id, profile, decision,
                        reviewer=reviewer, reason=reason, evidence=evidence)


def lifecycle_preview(project: Project, identifier: str, action: str) -> dict[str, Any]:
    """Return the read-only impact plan for a RETIRE/RESTORE operation."""
    from operon.lifecycle import lifecycle_plan

    db = Database(project.db_path, read_only=True)
    try:
        return lifecycle_plan(db, identifier, action=action)
    finally:
        db.close()


def lifecycle_apply(
        project: Project,
        identifier: str,
        action: str,
        reason: str,
        actor: str,
        reason_code: str | None = None,
        evidence: str | None = None,
) -> dict[str, Any]:
    """Apply a RETIRE/RESTORE exactly like ``operon retire|restore --apply``."""
    from operon.lifecycle import apply_lifecycle_event, lifecycle_plan
    from operon.utils import now_iso
    from operon.workflow import flush_run_log, log_run, new_run_id

    action = action.upper()
    if action not in {"RETIRE", "RESTORE"}:
        raise ValidationError(f"unsupported lifecycle action {action!r}")
    actor = (actor or os.environ.get("USER") or "").strip()
    if not actor:
        raise ValidationError("--actor is required when USER is not set")
    with _open_writable(project) as db:
        plan = lifecycle_plan(db, identifier, action=action)
        if not plan["will_change"]:
            if plan["blocker"]:
                raise ValidationError(plan["blocker"])
            return {"applied": False, "action": action, "target": plan["target"], "plan": plan}
        target = plan["target"]
        run_id = new_run_id()
        started_at = now_iso()
        jsonl_buffer: list[dict[str, Any]] = []
        with db.transaction():
            result = apply_lifecycle_event(
                db,
                target["entity_type"],
                target["entity_id"],
                action=action,
                reason=reason,
                reason_code=reason_code,
                evidence=evidence,
                actor=actor,
                workflow_run_id=run_id,
            )
            log_run(
                db,
                project,
                {
                    "run_id": run_id,
                    "entity_type": target["entity_type"],
                    "entity_id": target["entity_id"],
                    "step": f"lifecycle_{action.lower()}",
                    "status": "completed",
                    "started_at": started_at,
                    "finished_at": now_iso(),
                    "exit_code": 0,
                    "command": f"operon {action.lower()} {identifier}",
                    "tool": "operon",
                    "parameter_set": json.dumps({
                        "reason_code": reason_code if reason_code is not None else "manual_restore",
                        "reason": reason,
                        "actor": actor,
                    }, ensure_ascii=False, sort_keys=True),
                    "execution_details": json.dumps({
                        "entity_counts": plan["entity_counts"],
                        "reference_counts": plan["reference_counts"],
                        "physical_changes": plan["physical_changes"],
                    }, ensure_ascii=False, sort_keys=True),
                },
                jsonl_buffer=jsonl_buffer,
            )
        flush_run_log(project, jsonl_buffer)
        return {
            "applied": True,
            "action": action,
            "target": target,
            "event": result["event"],
            "effectively_retired": db.is_entity_retired(target["entity_type"], target["entity_id"]),
        }


def ingest(
        project: Project,
        source: str,
        entity_type: str,
        entity_id: str,
        role: str,
        fmt: str | None = None,
        compression: str | None = None,
        source_url: str | None = None,
        move: bool = False,
) -> dict[str, Any]:
    """Archive one file like ``operon ingest``; ConflictError propagates."""
    from operon.files import ingest_file

    temp_path: Path | None = None
    if source.startswith(("sftp://", "remote://")):
        from operon.remotes import fetch_url_to_temp
        original_url = source
        temp_path = fetch_url_to_temp(project, source)
        source = str(temp_path)
        source_url = source_url or original_url
    try:
        with _open_writable(project) as db:
            return ingest_file(
                db, project, source, entity_type, entity_id, role,
                fmt=fmt, compression=compression, source_url=source_url, move=move,
            )
    finally:
        if temp_path is not None:
            if temp_path.is_dir() and not temp_path.is_symlink():
                shutil.rmtree(temp_path, ignore_errors=True)
            else:
                temp_path.unlink(missing_ok=True)


def verify(project: Project, file_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Verify files like ``operon verify`` (writes statuses/audit rows)."""
    from operon.files import verify_files

    with _open_writable(project) as db:
        return verify_files(db, project, file_ids)


def run_qc(
        project: Project,
        entity_type: str | None = None,
        entity_id: str | None = None,
        file_id: str | None = None,
        progress: Callable[[int, int, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Run built-in QC like ``operon qc``.

    ``progress`` is invoked after each file with ``(done, total, result)``;
    raising from it aborts the batch between files (results for files
    already processed are kept).
    """
    from operon.qc_module import qc_all

    with _open_writable(project) as db:
        return qc_all(
            db, project, entity_type=entity_type, entity_id=entity_id,
            file_id=file_id, progress_callback=progress,
        )


# ---------------------------------------------------------------------------
# Config screen: structured editing of QC profiles and tools.yaml recipes.
#
# Every save is a new version: the version field is bumped and a
# content-addressed snapshot is recorded with exactly the same canonical
# document the CLI records (rules.py for profiles, tools.run_analysis for
# recipes), so a TUI save and a later CLI evaluation of identical content map
# to the same snapshot.
# ---------------------------------------------------------------------------

PROFILE_OPERATORS = (">=", "<=", ">", "<", "==", "!=", "between", "in", "not_in", "exists")
ENTITY_TYPE_NAMES = ("organism", "sample", "run", "assembly", "annotation")


def _validate_config_name(kind: str, name: str) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValidationError(f"invalid {kind} name {name!r}")


def coerce_scalar(text: str) -> Any:
    """Return an int/float when ``text`` looks numeric, else the string itself."""
    text = text.strip()
    if not text:
        return ""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _coerce_rule_values(rule: dict[str, Any]) -> None:
    for key in ("value", "min", "max"):
        if isinstance(rule.get(key), str):
            rule[key] = coerce_scalar(rule[key])
    if isinstance(rule.get("values"), list):
        rule["values"] = [coerce_scalar(v) if isinstance(v, str) else v for v in rule["values"]]


def _validate_profile_document(name: str, document: dict[str, Any]) -> None:
    if not isinstance(document, dict):
        raise ValidationError(f"profile {name!r}: document must be a mapping")
    if str(document.get("kind", "")) != "qc":
        raise ValidationError(
            f"profile {name!r}: only kind 'qc' profiles can be saved from the TUI "
            "(taxonomy_coverage profiles are edited by hand)"
        )
    if "version" not in document:
        raise ValidationError(f"profile {name!r}: 'version' is required")
    applies_to = document.get("applies_to", [])
    if not isinstance(applies_to, list):
        raise ValidationError(f"profile {name!r}: 'applies_to' must be a list")
    unknown = sorted(set(map(str, applies_to)) - set(ENTITY_TYPE_NAMES))
    if unknown:
        raise ValidationError(
            f"profile {name!r}: unknown entity types in applies_to: {', '.join(unknown)}"
        )
    for section in ("required", "warnings"):
        rules = document.get(section, [])
        if not isinstance(rules, list):
            raise ValidationError(f"profile {name!r}: '{section}' must be a list of rules")
        for index, rule in enumerate(rules, start=1):
            if not isinstance(rule, dict):
                raise ValidationError(f"profile {name!r}: {section} rule {index} must be a mapping")
            label = f"{section} rule {index}"
            if not str(rule.get("metric", "")).strip():
                raise ValidationError(f"profile {name!r}: {label}: metric is required")
            if not str(rule.get("code", "")).strip():
                raise ValidationError(f"profile {name!r}: {label}: code is required")
            operator = rule.get("operator")
            if operator not in PROFILE_OPERATORS:
                raise ValidationError(
                    f"profile {name!r}: {label} ({rule.get('metric')}): "
                    f"unknown operator {operator!r}"
                )


@contextmanager
def _saved_config(path: Path, text: str, previous_text: str | None,
                  label: str) -> Iterator[None]:
    """Publish configuration atomically and restore it if validation or storage fails."""
    try:
        atomic_write_text(path, text)
        yield
    except BaseException as exc:
        if previous_text is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_text(path, previous_text)
        if isinstance(exc, Exception):
            raise ValidationError(f"{label}: save rolled back: {exc}") from exc
        raise


def save_profile(project: Project, name: str, document: dict[str, Any]) -> dict[str, Any]:
    """Validate and save a ``kind: qc`` profile as a new version.

    The composed document is validated, written to
    ``config/profiles/<name>.yaml`` with the same header style as
    :func:`operon.profiles.write_default_profiles`, round-trip verified
    through :func:`operon.profiles.load_profile`, and recorded as a
    content-addressed snapshot with the exact canonical document
    ``operon evaluate`` records.  On any failure the previous file content
    is restored.  Saving unchanged content is a no-op: the version is not
    bumped and no snapshot is recorded.
    """
    from operon.profiles import load_profile
    from operon.utils import now_iso

    _validate_config_name("profile", name)
    if not isinstance(document, dict) or str(document.get("kind", "qc")) != "qc":
        raise ValidationError(f"profile {name!r}: only kind 'qc' profiles can be saved from the TUI")
    document = {str(key): value for key, value in document.items()}
    path = project.profiles_dir / f"{name}.yaml"
    previous_text = path.read_bytes().decode("utf-8") if path.exists() else None
    existing: dict[str, Any] | None = None
    if previous_text is not None:
        parsed = yaml.safe_load(previous_text)
        if not isinstance(parsed, dict):
            raise ValidationError(f"profile {name!r}: existing file is not a YAML mapping")
        existing = parsed
        if str(existing.get("kind")) != "qc":
            raise ValidationError(
                f"profile {name!r}: on-disk kind is {existing.get('kind')!r}; refusing to edit"
            )
        old_version = int(existing.get("version", 1))
        comparable = {key: value for key, value in document.items() if key != "version"}
        existing_content = {key: value for key, value in existing.items() if key != "version"}
        if comparable == existing_content:
            return {
                "name": name, "version": old_version, "sha256": None,
                "snapshot_id": None, "unchanged": True,
            }
        document["version"] = old_version + 1
    else:
        document["version"] = 1
    for section in ("required", "warnings"):
        for rule in document.get(section, []) or []:
            if isinstance(rule, dict):
                _coerce_rule_values(rule)
    _validate_profile_document(name, document)

    text = (
        f"# Operon qc profile {name} "
        "(versioned; review and rename before changing a frozen definition)\n"
        + yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    )
    with _saved_config(path, text, previous_text, f"profile {name!r}"):
        loaded = load_profile(project.profiles_dir, name, expected_kind="qc")
        profile_document = json.dumps(loaded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        sha256 = hashlib.sha256(profile_document.encode("utf-8")).hexdigest()
        version = int(loaded.get("version", 1))
        with _open_writable(project) as db:
            with db.transaction():
                snapshot_id = db.record_profile(name, version, sha256, profile_document, now_iso())
    return {
        "name": name, "version": version, "sha256": sha256,
        "snapshot_id": snapshot_id, "unchanged": False,
    }


def save_recipe(
        project: Project,
        tool_name: str,
        recipe_name: str,
        recipe_doc: dict[str, Any],
) -> dict[str, Any]:
    """Validate and save one recipe inside ``config/tools.yaml`` as a new version.

    Only ``tools[tool_name]["recipes"][recipe_name]`` is replaced; the rest of
    the configuration is kept as parsed.  The recipe ``version`` is bumped,
    the whole file is written back with ``yaml.safe_dump(sort_keys=False)``,
    and the result is round-trip verified through
    :func:`operon.tools.load_tools_config` + ``get_recipe``; on any failure
    the previous file content is restored.  The snapshot is recorded with the
    same ``{"recipe": ..., "tool": ...}`` document shape
    :func:`operon.tools.run_analysis` uses.

    NOTE: saving from the TUI normalizes tools.yaml formatting and drops
    hand-written comments; every saved version is preserved verbatim in
    ``recipe_snapshots`` (see ``operon recipes history/show``).
    """
    from operon.tools import get_recipe, get_tool, load_tools_config

    _validate_config_name("recipe", recipe_name)
    if not isinstance(recipe_doc, dict):
        raise ValidationError(f"recipe {recipe_name!r}: document must be a mapping")
    path = project.tools_config_path
    previous_text = path.read_bytes().decode("utf-8") if path.exists() else None
    config = load_tools_config(project)
    tools = config.get("tools")
    if not isinstance(tools, dict) or tool_name not in tools:
        raise ValidationError(f"unknown tool {tool_name!r} in {path}")
    tool_raw = tools[tool_name]
    if not isinstance(tool_raw, dict):
        raise ValidationError(f"tool {tool_name!r} in tools.yaml must be a mapping")
    recipes = tool_raw.setdefault("recipes", {})
    if not isinstance(recipes, dict):
        raise ValidationError(f"tool {tool_name!r}: recipes must be a mapping")
    existing = recipes.get(recipe_name)
    if existing is not None and not isinstance(existing, dict):
        raise ValidationError(f"recipe {recipe_name!r} in tools.yaml must be a mapping")

    document = dict(recipe_doc)
    if existing is not None:
        old_version = int(existing.get("version", 1))
        comparable = {key: value for key, value in document.items() if key != "version"}
        existing_content = {key: value for key, value in existing.items() if key != "version"}
        if comparable == existing_content:
            return {
                "name": recipe_name, "tool": tool_name, "version": old_version,
                "snapshot_id": None, "unchanged": True,
            }
        document["version"] = old_version + 1
    else:
        document["version"] = 1
    recipes[recipe_name] = document

    text = (
        "# Operon external tools configuration (YAML)\n"
        "# Saved via the Operon TUI: formatting is normalized and hand-written\n"
        "# comments are dropped; every version is kept in recipe_snapshots.\n"
        + yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    )
    with _saved_config(path, text, previous_text, f"recipe {recipe_name!r}"):
        load_tools_config(project)
        recipe = get_recipe(project, recipe_name)
        tool = get_tool(project, tool_name)
        with _open_writable(project) as db:
            with db.transaction():
                snapshot_id = db.record_recipe(
                    recipe.name, recipe.version, {"recipe": recipe.raw, "tool": tool.raw}
                )
    return {
        "name": recipe.name, "tool": tool_name, "version": recipe.version,
        "snapshot_id": snapshot_id, "unchanged": False,
    }


def check_tools(
        project: Project,
        timeout: float = 60.0,
        on_result: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Probe every configured tool's version, like ``operon tools-check``.

    Runs external commands; call from a worker thread, never the UI thread.
    One broken tool never breaks the batch: its row gets ``ok=False`` and the
    error message.  ``on_result`` (when given) is invoked with each row as it
    completes, for live UI updates.
    """
    from operon.tools import detect_tool_version, get_tool, load_tools_config

    config = load_tools_config(project)
    results: list[dict[str, Any]] = []
    for tool_name, raw in config.get("tools", {}).items():
        if not isinstance(raw, dict):
            continue
        entry: dict[str, Any] = {
            "name": str(tool_name),
            "executable": str(raw.get("executable", tool_name)),
            "run_method": "",
            "version": None,
            "ok": False,
            "error": None,
        }
        try:
            tool = get_tool(project, str(tool_name))
            entry["executable"] = tool.executable
            entry["run_method"] = tool.run_method
            entry["version"] = detect_tool_version(tool, config, timeout=timeout)
            entry["ok"] = True
        except Exception as exc:  # noqa: BLE001 - one bad tool must not break the batch
            entry["error"] = str(exc)
        results.append(entry)
        if on_result is not None:
            on_result(entry)
    return results
