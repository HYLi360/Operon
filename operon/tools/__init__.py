"""External tool configuration, execution and result parsing.

Reads ``config/tools.yaml``; wraps external program launch, version probing,
input validation, cached execution, and result write-back.

This module is the facade of the external-tool subsystem: the implementation
is split by dependency chain across the private modules of this package
(``_defaults`` -> ``_config`` -> ``_probe`` -> ``_inputs`` ->
``_cache`` -> ``_plan`` -> ``_execute`` -> ``_run`` / ``_results``), and
everything previously importable from here is re-exported below."""

from __future__ import annotations

from operon.config import Project
from operon.errors import ExternalToolError

from ._cache import (
    _cache_environment_decision,
    _cached_environment_document,
    _current_environment_document,
    _find_verified_adoptee,
    find_adoptable_job,
    find_cached_job,
)
from ._config import (
    ENVIRONMENT_POLICIES,
    Recipe,
    RecipeCommand,
    ToolSpec,
    ensure_tools_config,
    get_recipe,
    get_tool,
    invalidate_tools_config_cache,
    launcher_prefix,
    list_analyses,
    load_tools_config,
    recipe_environment_policy,
    tool_command,
)
from ._defaults import DEFAULT_TOOLS_CONFIG
from ._execute import run_analysis_for_file
from ._inputs import (
    _DATABASE_IDENTITY_CACHE,
    _directory_fingerprint,
    candidate_files,
    database_identity,
    parameter_fingerprint,
    render_arguments,
    resolve_runtime_parameters,
)
from ._plan import (
    _remove_output_artifact,
    _render_output_name,
    _require_artifact_kind,
    plan_analysis_for_file,
)
from ._probe import (
    _VERSION_CACHE,
    _version_output_via_executor,
    command_step_provenance,
    detect_tool_version,
    detect_tool_version_record,
    recipe_version_probe_command,
)
from ._results import (
    _parse_blast_tabular,
    _parse_busco_json,
    _result_metric,
    _select_busco_json,
    parse_and_store_results,
    parse_hits,
)
from ._run import run_analysis

__all__ = [
    "DEFAULT_TOOLS_CONFIG",
    "ENVIRONMENT_POLICIES",
    "_DATABASE_IDENTITY_CACHE",
    "_VERSION_CACHE",
    "Recipe",
    "RecipeCommand",
    "ToolSpec",
    "_cache_environment_decision",
    "_cached_environment_document",
    "_current_environment_document",
    "_directory_fingerprint",
    "_find_verified_adoptee",
    "_parse_blast_tabular",
    "_parse_busco_json",
    "_remove_output_artifact",
    "_render_output_name",
    "_require_artifact_kind",
    "_result_metric",
    "_select_busco_json",
    "_version_output_via_executor",
    "candidate_files",
    "command_step_provenance",
    "database_identity",
    "detect_tool_version",
    "detect_tool_version_record",
    "ensure_tools_config",
    "find_adoptable_job",
    "find_cached_job",
    "get_recipe",
    "get_tool",
    "invalidate_tools_config_cache",
    "launcher_prefix",
    "list_analyses",
    "load_tools_config",
    "parameter_fingerprint",
    "parse_and_store_results",
    "parse_hits",
    "plan_analysis_for_file",
    "print_tools_table",
    "recipe_environment_policy",
    "recipe_version_probe_command",
    "render_arguments",
    "resolve_runtime_parameters",
    "run_analysis",
    "run_analysis_for_file",
    "tool_command",
]


def print_tools_table(project: Project) -> tuple[str, bool]:
    from operon.utils import format_table

    config = load_tools_config(project)
    rows: list[list[str]] = []
    all_ok = True
    for tool_name, raw in config.get("tools", {}).items():
        if not isinstance(raw, dict):
            continue
        tool = get_tool(project, tool_name)
        try:
            version = detect_tool_version(tool, config)
        except ExternalToolError as exc:
            version = f"ERROR: {exc}"
            all_ok = False
        recipes = ", ".join(sorted(raw.get("recipes", {}).keys()))
        rows.append([tool_name, tool.run_method or "(direct)", version, recipes])
    return format_table(
        ["tool", "run_method", "detected_version", "recipes"], rows
    ), all_ok
