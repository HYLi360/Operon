"""Recipe and tool configuration for the external-tool subsystem.

Loads, validates and models ``config/tools.yaml``: tool specs, recipes,
recipe commands, launcher prefixes and the environment-policy accessor.
Probing, planning, execution and result parsing live in the sibling
private modules in this package; the public surface is re-exported by
:mod:`operon.tools`."""

from __future__ import annotations

import copy
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from operon.config import Project
from operon.errors import ValidationError

from ._defaults import DEFAULT_TOOLS_CONFIG

ENVIRONMENT_POLICIES = ("ignore", "warn", "strict")

#: Parsed ``config/tools.yaml`` documents keyed by path, each held against the
#: ``(mtime_ns, size)`` it was parsed from.  A single Config-screen render reads
#: this file once per tool, per recipe and per analysis, so without the cache the
#: YAML scanner ran tens of times per screen; keying on the file's identity
#: rather than a clock keeps a just-saved edit visible on the very next read,
#: which a TTL cache could not promise for a file the TUI rewrites in place.
_TOOLS_CONFIG_CACHE: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def _file_identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return (stat.st_mtime_ns, stat.st_size)


def invalidate_tools_config_cache(project: Project) -> None:
    """Drop the cached document for one project's ``config/tools.yaml``."""
    _TOOLS_CONFIG_CACHE.pop(str(project.tools_config_path), None)


@dataclass
class ToolSpec:
    name: str
    executable: str
    run_method: str
    version_args: list[str]
    version_pattern: str
    description: str
    recipes: dict[str, dict[str, Any]]
    raw: dict[str, Any]


@dataclass
class Recipe:
    name: str
    tool_name: str
    description: str
    entity_type: str
    file_role: str
    fmt: str
    input_kind: str
    database: str
    database_version: str
    output_subdir: str
    output_kind: str
    output_name_template: str
    output_suffix: str
    arguments: list[str]
    parameters: dict[str, dict[str, Any]]
    result_parser: str
    max_hits_per_query: int
    raw: dict[str, Any]
    file_role_prefix: str = ""
    version: int = 1
    commands: list[RecipeCommand] = field(default_factory=list)


@dataclass
class RecipeCommand:
    """One command in a recipe chain, executed in the parent tool's environment."""

    arguments: list[str]
    version_args: list[str] | None = None
    version_pattern: str = ""


def ensure_tools_config(project: Project) -> Path:
    """Create config/tools.yaml when it does not exist (never overwrite)."""
    path = project.tools_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# Operon external tools configuration (YAML)\n"
            "# Edit paths, conda environments and recipe arguments here.\n"
            "# run_method examples:\n"
            '#   ""                         -> use executable directly\n'
            '#   "/opt/conda/bin/conda run --no-capture-output -n blast"\n'
            '#   "singularity exec blast.sif"\n'
            "# The rpsblast_cdd recipe chains rpsblast into rpsbproc; rpsbproc\n"
            "# flags differ between builds, so adjust the second command block\n"
            "# to your local rpsbproc version.\n"
            "# A commands chain runs every step in the parent tool's single\n"
            "# run_method environment (by design: one recipe, one environment).\n"
            "# The first command is the recipe's logical owner; later blocks may\n"
            "# declare their own version_args/version_pattern probes, and every\n"
            "# probed step version participates in the analysis cache identity.\n"
            + yaml.safe_dump(DEFAULT_TOOLS_CONFIG, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return path


def load_tools_config(project: Project) -> dict[str, Any]:
    """Parse ``config/tools.yaml``, reusing the last parse while it is unchanged.

    Callers own their result: the save paths mutate it in place (``setdefault``
    on a tool's ``recipes``, then a whole-document rewrite), so a cached hit is
    handed out as a deep copy.  The document is re-parsed the moment the file's
    mtime or size moves, and the write paths invalidate it explicitly, so a save
    is always visible to the round-trip read that follows it.
    """
    path = ensure_tools_config(project)
    key = str(path)
    identity = _file_identity(path)
    cached = _TOOLS_CONFIG_CACHE.get(key)
    if cached is not None and cached[0] == identity:
        return copy.deepcopy(cached[1])
    with open(path, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    if not isinstance(doc, dict) or "tools" not in doc:
        raise ValidationError(f"invalid tools config: {path}")
    _TOOLS_CONFIG_CACHE[key] = (identity, doc)
    return copy.deepcopy(doc)


def get_tool(project: Project, tool_name: str) -> ToolSpec:
    return _tool_from_config(project, tool_name, load_tools_config(project))


def _tool_from_config(
    project: Project, tool_name: str, config: dict[str, Any]
) -> ToolSpec:
    tools = config.get("tools", {})
    if tool_name not in tools:
        available = ", ".join(sorted(tools.keys())) or "(none)"
        raise ValidationError(
            f"unknown tool {tool_name!r} in {project.tools_config_path}; available: {available}"
        )
    raw = tools[tool_name]
    if not isinstance(raw, dict):
        raise ValidationError(f"tool {tool_name!r} in tools.yaml must be a mapping")
    executable = str(raw.get("executable", tool_name))
    run_method = raw.get("run_method", "")
    if isinstance(run_method, dict):
        mode = run_method.get("mode", "conda")
        conda = config.get("conda", {})
        if mode == "conda":
            env = run_method.get("env")
            if not env:
                raise ValidationError(
                    f"tool {tool_name}: conda launcher requires 'env'"
                )
            conda_bin = run_method.get("bin") or conda.get("bin", "conda")
            run_args = run_method.get("args") or conda.get(
                "run_args", ["run", "--no-capture-output"]
            )
            prefix = [str(conda_bin), *[str(x) for x in run_args], "-n", str(env)]
        elif mode == "prefix":
            prefix = [str(x) for x in run_method.get("prefix", [])]
        elif mode == "path":
            prefix = []
        else:
            raise ValidationError(
                f"tool {tool_name}: unsupported launcher mode {mode!r}"
            )
        return ToolSpec(
            name=tool_name,
            executable=executable,
            run_method=" ".join(prefix),
            version_args=[str(x) for x in raw.get("version_args", [])],
            version_pattern=str(raw.get("version_pattern", "")),
            description=str(raw.get("description", "")),
            recipes=raw.get("recipes", {}),
            raw=raw,
        )
    if not isinstance(run_method, str):
        raise ValidationError(
            f"tool {tool_name}: run_method must be a string or mapping"
        )
    return ToolSpec(
        name=tool_name,
        executable=executable,
        run_method=run_method,
        version_args=[str(x) for x in raw.get("version_args", [])],
        version_pattern=str(raw.get("version_pattern", "")),
        description=str(raw.get("description", "")),
        recipes=raw.get("recipes", {}),
        raw=raw,
    )


def get_recipe(project: Project, analysis_name: str) -> Recipe:
    return _recipe_from_config(analysis_name, load_tools_config(project))


def _recipe_from_config(analysis_name: str, config: dict[str, Any]) -> Recipe:
    for tool_name, raw_tool in config.get("tools", {}).items():
        if not isinstance(raw_tool, dict):
            continue
        recipes = raw_tool.get("recipes", {})
        if analysis_name in recipes:
            raw = recipes[analysis_name]
            if not isinstance(raw, dict):
                raise ValidationError(f"analysis {analysis_name!r} must be a mapping")
            raw_version = _validate_recipe_version(analysis_name, raw)
            kinds = _normalize_recipe_kinds(analysis_name, raw)
            parameters = _normalize_recipe_parameters(analysis_name, raw)
            commands = _normalize_recipe_commands(
                analysis_name, raw, raw_tool, tool_name
            )
            return _build_recipe(
                analysis_name, tool_name, raw_version, raw, kinds, commands, parameters
            )
    available = sorted(
        f"{tool}.{recipe}"
        for tool, tool_cfg in config.get("tools", {}).items()
        for recipe in (
            tool_cfg.get("recipes", {}) if isinstance(tool_cfg, dict) else {}
        )
    )
    raise ValidationError(
        f"unknown analysis {analysis_name!r}; available recipes: {', '.join(available) or '(none)'}"
    )


def _validate_recipe_version(analysis_name: str, raw: dict[str, Any]) -> int:
    raw_version = raw.get("version", 1)
    if (
        isinstance(raw_version, bool)
        or not isinstance(raw_version, int)
        or raw_version < 1
    ):
        raise ValidationError(
            f"analysis {analysis_name!r}: version must be a positive integer"
        )
    return raw_version


def _validate_recipe_environment_policy(
    analysis_name: str, raw: dict[str, Any]
) -> None:
    environment_policy = str(raw.get("environment_policy", "warn") or "warn").strip()
    if environment_policy not in ENVIRONMENT_POLICIES:
        raise ValidationError(
            f"analysis {analysis_name!r}: environment_policy must be one of "
            f"{', '.join(ENVIRONMENT_POLICIES)}; got {environment_policy!r}"
        )


def _normalize_recipe_kinds(
    analysis_name: str, raw: dict[str, Any]
) -> tuple[str, str, str, str, str, str]:
    """Normalize format/kind/role fields and derive the output suffix.

    Returns ``(fmt, file_role, file_role_prefix, input_kind, output_kind,
    output_suffix)``. The environment-policy check runs between the kind
    checks and the format-consistency check so that a config with several
    problems raises the same first error as before the split.
    """
    fmt = str(raw.get("format", "")).strip()
    file_role = str(raw.get("file_role", "")).strip()
    file_role_prefix = str(raw.get("file_role_prefix", "")).strip()
    if file_role and file_role_prefix:
        raise ValidationError(
            f"analysis {analysis_name!r}: 'file_role' and 'file_role_prefix' "
            "are mutually exclusive"
        )
    if file_role_prefix and any(char in file_role_prefix for char in "%*?"):
        raise ValidationError(
            f"analysis {analysis_name!r}: file_role_prefix {file_role_prefix!r} "
            "must not contain wildcard characters (% * ?); matching is an "
            "exact role or a prefix at a ':' boundary, not a pattern"
        )
    input_kind = str(
        raw.get("input_kind", "directory" if fmt == "directory" else "file")
    ).strip()
    output_kind = str(raw.get("output_kind", "file")).strip()
    if input_kind not in {"file", "directory"}:
        raise ValidationError(
            f"analysis {analysis_name!r}: input_kind must be 'file' or 'directory'"
        )
    if output_kind not in {"file", "directory"}:
        raise ValidationError(
            f"analysis {analysis_name!r}: output_kind must be 'file' or 'directory'"
        )
    _validate_recipe_environment_policy(analysis_name, raw)
    if fmt == "directory" and input_kind != "directory":
        raise ValidationError(
            f"analysis {analysis_name!r}: format=directory requires input_kind=directory"
        )
    default_suffix = "" if output_kind == "directory" else ".tsv"
    output_suffix = (
        str(raw["output_suffix"]) if "output_suffix" in raw else default_suffix
    )
    return fmt, file_role, file_role_prefix, input_kind, output_kind, output_suffix


def _normalize_recipe_parameters(
    analysis_name: str, raw: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    raw_parameters = raw.get("parameters", {}) or {}
    if not isinstance(raw_parameters, dict):
        raise ValidationError(
            f"analysis {analysis_name!r}: parameters must be a mapping"
        )
    parameters: dict[str, dict[str, Any]] = {}
    for parameter_name, parameter_spec in raw_parameters.items():
        name = str(parameter_name)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValidationError(
                f"analysis {analysis_name!r}: invalid parameter name {name!r}"
            )
        if parameter_spec is None:
            parameter_spec = {}
        if not isinstance(parameter_spec, dict):
            raise ValidationError(
                f"analysis {analysis_name!r}: parameter {name!r} must be a mapping"
            )
        parameters[name] = dict(parameter_spec)
    return parameters


def _normalize_recipe_commands(
    analysis_name: str, raw: dict[str, Any], raw_tool: dict[str, Any], tool_name: str
) -> list[RecipeCommand]:
    raw_commands = raw.get("commands")
    commands: list[RecipeCommand] = []
    if raw_commands is None:
        return commands
    if raw.get("arguments"):
        raise ValidationError(
            f"analysis {analysis_name!r}: 'commands' and 'arguments' are mutually exclusive"
        )
    if not isinstance(raw_commands, list) or not raw_commands:
        raise ValidationError(
            f"analysis {analysis_name!r}: commands must be a non-empty list of command blocks"
        )
    for block_index, block in enumerate(raw_commands, start=1):
        if (
            not isinstance(block, dict)
            or not isinstance(block.get("arguments"), list)
            or not block["arguments"]
        ):
            raise ValidationError(
                f"analysis {analysis_name!r}: each commands block requires "
                "a non-empty 'arguments' list"
            )
        unknown_keys = sorted(
            set(block) - {"arguments", "version_args", "version_pattern"}
        )
        if unknown_keys:
            raise ValidationError(
                f"analysis {analysis_name!r}: commands block {block_index} "
                f"has unsupported key(s): {', '.join(unknown_keys)}; every step "
                "of a chain runs in the parent tool's single run_method "
                "environment by deliberate limitation — per-step environments "
                "are not supported (use an external workflow engine such as "
                "Snakemake/Nextflow and 'adopt' for multi-environment pipelines)"
            )
        raw_version_args = block.get("version_args")
        if raw_version_args is not None and (
            not isinstance(raw_version_args, list) or not raw_version_args
        ):
            raise ValidationError(
                f"analysis {analysis_name!r}: commands block {block_index} "
                "version_args must be a non-empty list when configured"
            )
        raw_version_pattern = block.get("version_pattern", "")
        if not isinstance(raw_version_pattern, str):
            raise ValidationError(
                f"analysis {analysis_name!r}: commands block {block_index} "
                "version_pattern must be a string"
            )
        if raw_version_pattern and raw_version_args is None:
            raise ValidationError(
                f"analysis {analysis_name!r}: commands block {block_index} "
                "version_pattern requires version_args"
            )
        commands.append(
            RecipeCommand(
                arguments=[str(x) for x in block["arguments"]],
                version_args=(
                    [str(x) for x in raw_version_args]
                    if raw_version_args is not None
                    else None
                ),
                version_pattern=raw_version_pattern,
            )
        )
    # The first command is the recipe's logical owner: the job's
    # recorded tool_version and the version component of cache
    # identity describe its program. Without its own probe that
    # program must be the tool's executable, otherwise the
    # tool-level probe would silently describe a different binary.
    if commands and commands[0].version_args is None:
        tool_executable = str(raw_tool.get("executable", tool_name))
        first_executable = commands[0].arguments[0]
        if first_executable != tool_executable:
            raise ValidationError(
                f"analysis {analysis_name!r}: the first command "
                f"({first_executable!r}) is the recipe's logical owner but "
                f"does not match the tool's executable ({tool_executable!r}); "
                "declare version_args/version_pattern on the first command "
                "block, or point the tool's executable at it"
            )
    return commands


def _build_recipe(
    analysis_name: str,
    tool_name: str,
    raw_version: int,
    raw: dict[str, Any],
    kinds: tuple[str, str, str, str, str, str],
    commands: list[RecipeCommand],
    parameters: dict[str, dict[str, Any]],
) -> Recipe:
    fmt, file_role, file_role_prefix, input_kind, output_kind, output_suffix = kinds
    return Recipe(
        name=analysis_name,
        tool_name=tool_name,
        version=raw_version,
        description=str(raw.get("description", "")),
        entity_type=str(raw.get("entity_type", "")).strip(),
        file_role=file_role,
        file_role_prefix=file_role_prefix,
        fmt=fmt,
        input_kind=input_kind,
        database=str(raw.get("database", "") or ""),
        database_version=str(raw.get("database_version", "") or ""),
        output_subdir=str(raw.get("output_subdir", analysis_name) or analysis_name),
        output_kind=output_kind,
        output_name_template=str(raw.get("output_name", "") or ""),
        output_suffix=output_suffix,
        arguments=[str(x) for x in raw.get("arguments", [])],
        commands=commands,
        parameters=parameters,
        result_parser=str(raw.get("result_parser", "none") or "none"),
        max_hits_per_query=int(raw.get("max_hits_per_query", 5) or 5),
        raw=raw,
    )


def recipe_environment_policy(recipe: Recipe) -> str:
    """The recipe's cache-reuse policy; validated at load time in get_recipe."""
    return str(recipe.raw.get("environment_policy", "warn") or "warn")


def list_analyses(project: Project) -> list[Recipe]:
    config = load_tools_config(project)
    names: list[str] = []
    for tool_name, raw_tool in config.get("tools", {}).items():
        if not isinstance(raw_tool, dict):
            continue
        for recipe_name in raw_tool.get("recipes", {}):
            names.append(recipe_name)
    return [get_recipe(project, name) for name in sorted(names)]


def launcher_prefix(tool: ToolSpec, config: dict[str, Any]) -> list[str]:
    """Return the prefix that launches the executable (conda run / container)."""
    method = tool.run_method
    if not method:
        return []
    parts = shlex.split(method)
    if parts and parts[0] == "conda":
        conda_bin = str(config.get("conda", {}).get("bin", "conda"))
        if conda_bin and conda_bin != "conda":
            parts[0] = conda_bin
    return parts


def tool_command(tool: ToolSpec, config: dict[str, Any]) -> list[str]:
    return [*launcher_prefix(tool, config), tool.executable]
