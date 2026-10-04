"""Additive, audited preset fragment merging without installing plugins."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.utils import atomic_write_text

from ._config import (
    _recipe_from_config,
    _tool_from_config,
    get_recipe,
    get_tool,
    invalidate_tools_config_cache,
    load_tools_config,
)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValidationError(f"invalid preset/config YAML {path}: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("tools"), dict):
        raise ValidationError(f"{path}: tools must be a mapping")
    return doc


def _validate_name(kind: str, name: Any) -> None:
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", name
    ):
        raise ValidationError(f"invalid {kind} name {name!r}")


def _validate_document(project, config: dict[str, Any]) -> None:
    names = set()
    for tool_name, raw in config["tools"].items():
        _validate_name("tool", tool_name)
        if not isinstance(raw, dict) or not isinstance(raw.get("recipes", {}), dict):
            raise ValidationError(
                f"tool {tool_name}: definition/recipes must be mappings"
            )
        _tool_from_config(project, tool_name, config)
        for name in raw.get("recipes", {}):
            _validate_name("recipe", name)
            if name in names:
                raise ConflictError(f"recipe name {name!r} is not unique project-wide")
            names.add(name)
            _recipe_from_config(name, config)


def _prepare(project, source: str | Path) -> dict[str, Any]:
    path = project.tools_config_path
    previous = path.read_bytes() if path.exists() else None
    # A preview must not create defaults when tools.yaml is absent.
    if previous is None:
        from ._defaults import DEFAULT_TOOLS_CONFIG

        config = copy.deepcopy(DEFAULT_TOOLS_CONFIG)
    else:
        config = _read_yaml(path)
    fragment = _read_yaml(Path(source))
    if type(fragment.get("version")) is not int or fragment["version"] != 1:
        raise ValidationError("unsupported preset version; expected integer 1")
    if set(fragment) != {"version", "tools"}:
        raise ValidationError("preset fragment may contain only version and tools")
    _validate_document(project, config)
    merged = copy.deepcopy(config)
    affected: set[str] = set()
    added_tools: list[str] = []
    for tool_name, incoming in fragment["tools"].items():
        _validate_name("tool", tool_name)
        if not isinstance(incoming, dict) or not isinstance(
            incoming.get("recipes", {}), dict
        ):
            raise ValidationError(
                f"tool {tool_name}: definition/recipes must be mappings"
            )
        current = merged["tools"].get(tool_name)
        if current is None:
            merged["tools"][tool_name] = copy.deepcopy(incoming)
            added_tools.append(tool_name)
            affected.update(incoming.get("recipes", {}))
            continue
        incoming_fields = {k: v for k, v in incoming.items() if k != "recipes"}
        current_fields = {k: v for k, v in current.items() if k != "recipes"}
        if incoming_fields != current_fields:
            raise ConflictError(
                f"different tool definition already exists for {tool_name!r}"
            )
        recipes = current.setdefault("recipes", {})
        for name, recipe in incoming.get("recipes", {}).items():
            if name in recipes:
                if recipes[name] != recipe:
                    raise ConflictError(
                        f"different recipe definition already exists for {name!r}"
                    )
            else:
                recipes[name] = copy.deepcopy(recipe)
                # Tool snapshots include the complete tool document.
                affected.update(recipes)
    _validate_document(project, merged)
    return {
        "previous": previous,
        "config": merged,
        "recipes": sorted(affected),
        "tools": sorted(added_tools),
        "unchanged": config == merged,
    }


def plan_preset_import(project, source: str | Path) -> dict[str, Any]:
    """Validate and compare a fragment wholly in memory; no project/DB write."""
    plan = _prepare(project, source)
    return {key: plan[key] for key in ("recipes", "tools", "unchanged")}


def add_preset(project, db: Database, source: str | Path) -> dict[str, Any]:
    """Publish a fragment atomically, validate again, snapshot affected recipes."""
    path = project.tools_config_path
    plan = None
    try:
        # Hold the writer lock through comparison, publication and snapshots.
        with db.transaction():
            plan = _prepare(project, source)
            if plan["unchanged"]:
                return {"unchanged": True, "recipes": [], "snapshots": {}}
            text = yaml.safe_dump(plan["config"], sort_keys=False, allow_unicode=True)
            atomic_write_text(path, text)
            invalidate_tools_config_cache(project)
            if load_tools_config(project) != plan["config"]:
                raise ValidationError("preset round-trip changed configuration")
            snapshots = {}
            for name in plan["recipes"]:
                recipe = get_recipe(project, name)
                tool = get_tool(project, recipe.tool_name)
                snapshots[name] = db.record_recipe(
                    name, recipe.version, {"recipe": recipe.raw, "tool": tool.raw}
                )
            db.record_change(
                "tools_config",
                "tools.yaml",
                "preset",
                None,
                str(source),
                reason="add declarative plugin preset",
            )
    except BaseException:
        invalidate_tools_config_cache(project)
        if plan is not None and not plan["unchanged"]:
            if plan["previous"] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, plan["previous"].decode("utf-8"))
        raise
    return {"unchanged": False, "recipes": plan["recipes"], "snapshots": snapshots}
