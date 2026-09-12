"""Encapsulated external analysis tools.

External programs are configured in `config/tools.yaml` instead of being
passed as ad-hoc command strings.  A recipe declares:

  * which manifest file or directory artifacts are valid inputs;
  * how to launch the program (direct executable or `conda run -n <env>`),
    either as a single command or as a `commands` chain executed in order —
    every step of a chain shares the parent tool's single `run_method`
    environment by deliberate limitation;
  * how to detect and record program versions: the first command of a chain
    is the recipe's logical owner, later commands may declare their own
    probes, and every probed step version participates in cache identity;
  * whether the output is a file or directory and how arguments are rendered;
  * how to parse the output back into SQLite.

Runs are cached in `analysis_jobs`; a job is reused only when analysis name,
input artifact content hash, rendered parameters, tool version and database
identity all match. Raw file and directory inputs are never modified and are
re-verified against the manifest before every run.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from operon.config import Project
from operon.database import Database
from operon.errors import ExternalToolError, ValidationError
from operon.shutdown import ShutdownRequested, graceful_shutdown
from operon.utils import now_iso, sha256_file, sha256_path

_VERSION_CACHE: dict[str, tuple[str, str]] = {}
_DATABASE_IDENTITY_CACHE: dict[str, str] = {}

DEFAULT_TOOLS_CONFIG: dict[str, Any] = {
    "version": 1,
    "conda": {
        "bin": "conda",
        "run_args": ["run", "--no-capture-output"],
    },
    "tools": {
        "blastn": {
            "description": "NCBI BLAST+ nucleotide-nucleotide search",
            "executable": "blastn",
            "run_method": "conda run --no-capture-output -n blast",
            "version_args": ["-version"],
            "version_pattern": r"blastn:\s*([^\s]+)",
            "recipes": {
                "blastn_nt": {
                    "description": "Assembly FASTA against the NCBI nt database",
                    "entity_type": "assembly",
                    "file_role": "genome_fasta",
                    "format": "fasta",
                    "database": "/path/to/nt",
                    "database_version": "",
                    "output_subdir": "blastn_nt",
                    "output_suffix": ".blastn.tsv",
                    "arguments": [
                        "-db", "${database}",
                        "-query", "${input}",
                        "-out", "${output}",
                        "-outfmt",
                        "6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore",
                        "-max_target_seqs", "5",
                        "-evalue", "1e-5",
                        "-num_threads", "${threads}",
                    ],
                    "result_parser": "blast_tabular",
                    "result_columns": ["qseqid", "sseqid", "pident", "length", "mismatch", "gapopen", "qstart", "qend",
                                       "sstart", "send", "evalue", "bitscore"],
                    "hit_metric_columns": ["pident", "length", "evalue", "bitscore"],
                    "max_hits_per_query": 5,
                }
            },
        },
        "blastp": {
            "description": "NCBI BLAST+ protein-protein search",
            "executable": "blastp",
            "run_method": "conda run --no-capture-output -n blast",
            "version_args": ["-version"],
            "version_pattern": r"blastp:\s*([^\s]+)",
            "recipes": {
                "blastp_nr": {
                    "description": "Annotation protein FASTA against nr",
                    "entity_type": "annotation",
                    "file_role": "protein_fasta",
                    "format": "fasta",
                    "database": "/path/to/nr",
                    "database_version": "",
                    "output_subdir": "blastp_nr",
                    "output_suffix": ".blastp.tsv",
                    "arguments": [
                        "-db", "${database}",
                        "-query", "${input}",
                        "-out", "${output}",
                        "-outfmt",
                        "6 qseqid sseqid pident length mismatch gapopen qstart qend sstart send evalue bitscore",
                        "-max_target_seqs", "5",
                        "-evalue", "1e-5",
                        "-num_threads", "${threads}",
                    ],
                    "result_parser": "blast_tabular",
                    "result_columns": ["qseqid", "sseqid", "pident", "length", "mismatch", "gapopen", "qstart", "qend",
                                       "sstart", "send", "evalue", "bitscore"],
                    "hit_metric_columns": ["pident", "length", "evalue", "bitscore"],
                    "max_hits_per_query": 5,
                }
            },
        },
        "hmmsearch": {
            "description": "HMMER profile search against a protein FASTA",
            "executable": "hmmsearch",
            "run_method": "conda run --no-capture-output -n hmmer",
            "version_args": ["-h"],
            "version_pattern": r"hmmsearch\s*::\s*HMMER\s+([^\s]+)",
            "recipes": {
                "hmmsearch_pfam": {
                    "description": "Annotation proteins against Pfam-A.hmm",
                    "entity_type": "annotation",
                    "file_role": "protein_fasta",
                    "format": "fasta",
                    "database": "/path/to/Pfam-A.hmm",
                    "database_version": "",
                    "output_subdir": "hmmsearch_pfam",
                    "output_suffix": ".hmmsearch.tblout",
                    "arguments": [
                        "--tblout", "${output}",
                        "--cpu", "${threads}",
                        "${database}",
                        "${input}",
                    ],
                    "result_parser": "hmmer_tblout",
                    "hmmer_mode": "hmmsearch",
                    "max_hits_per_query": 5,
                }
            },
        },
        "busco": {
            "description": "Benchmarking Universal Single-Copy Ortholog assessment",
            "executable": "busco",
            "run_method": "mamba run -n busco_6.1.0",
            "version_args": ["--version"],
            "version_pattern": r"BUSCO\s+([^\s]+)",
            "recipes": {
                "busco_autolineage": {
                    "description": "BUSCO protein mode with automatic lineage selection",
                    "entity_type": "annotation",
                    "file_role": "protein_fasta",
                    "format": "fasta",
                    "input_kind": "file",
                    "database": "resources/busco_downloads",
                    "database_version": "odb12",
                    "database_mode": "mutable_cache",
                    "output_subdir": "busco",
                    "output_kind": "directory",
                    "output_name": "${file_id}.busco",
                    "arguments": [
                        "-m", "protein",
                        "-i", "${input}",
                        "-o", "${output_name}",
                        "--out_path", "${output_parent}",
                        "--download_path", "${database}",
                        "-c", "${threads}",
                        "--auto-lineage",
                        "--opt-out-run-stats",
                        "--tar",
                    ],
                    "result_parser": "busco_json",
                    "result_glob": "short_summary.specific.*.json",
                },
                "busco_lineage": {
                    "description": "BUSCO protein mode with an explicitly selected lineage",
                    "entity_type": "annotation",
                    "file_role": "protein_fasta",
                    "format": "fasta",
                    "input_kind": "file",
                    "parameters": {
                        "lineage_dataset": {
                            "description": "BUSCO lineage dataset name, e.g. fabales_odb12.2",
                            "required": True,
                            "pattern": r"[A-Za-z0-9][A-Za-z0-9_.-]*",
                        }
                    },
                    "database": "resources/busco_downloads",
                    "database_version": "odb12",
                    "database_mode": "mutable_cache",
                    "output_subdir": "busco_lineage",
                    "output_kind": "directory",
                    "output_name": "${file_id}.${lineage_dataset}.busco",
                    "arguments": [
                        "-m", "protein",
                        "-i", "${input}",
                        "-o", "${output_name}",
                        "--out_path", "${output_parent}",
                        "--download_path", "${database}",
                        "--lineage_dataset", "${lineage_dataset}",
                        "-c", "${threads}",
                        "--opt-out-run-stats",
                        "--tar",
                    ],
                    "result_parser": "busco_json",
                    "result_glob": "short_summary.specific.*.json",
                }
            },
        },
        "rpsblast": {
            "description": "NCBI RPS-BLAST against CDD, post-processed by rpsbproc",
            "executable": "rpsblast",
            "run_method": "conda run --no-capture-output -n blast",
            "version_args": ["-version"],
            "version_pattern": r"rpsblast:\s*([^\s]+)",
            "recipes": {
                "rpsblast_cdd": {
                    "description": "Annotation proteins against CDD via rpsblast + rpsbproc",
                    "entity_type": "annotation",
                    "file_role": "protein_fasta",
                    "format": "fasta",
                    "database": "/path/to/cdd/Cdd",
                    "database_version": "",
                    "output_subdir": "rpsblast_cdd",
                    "output_suffix": ".rpsbproc.tsv",
                    "commands": [
                        {
                            "arguments": [
                                "rpsblast",
                                "-query", "${input}",
                                "-db", "${database}",
                                "-out", "${work_dir}/hits.asn",
                                "-outfmt", "11",
                                "-evalue", "0.001",
                                "-num_threads", "${threads}",
                            ],
                        },
                        {
                            "arguments": [
                                "rpsbproc",
                                "-i", "${work_dir}/hits.asn",
                                "-o", "${output}",
                                "-e", "0.001",
                                "-m", "rep",
                            ],
                            "version_args": ["-version"],
                            "version_pattern": r"rpsbproc:\s*([^\s]+)",
                        },
                    ],
                    "result_parser": "rpsbproc_tabular",
                    "max_hits_per_query": 5,
                }
            },
        },
    },
}


def ensure_tools_config(project: Project) -> Path:
    """Create config/tools.yaml when it does not exist (never overwrite)."""
    path = project.tools_config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# Operon external tools configuration (YAML)\n"
            "# Edit paths, conda environments and recipe arguments here.\n"
            "# run_method examples:\n"
            "#   \"\"                         -> use executable directly\n"
            "#   \"/opt/conda/bin/conda run --no-capture-output -n blast\"\n"
            "#   \"singularity exec blast.sif\"\n"
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
    ensure_tools_config(project)
    with open(project.tools_config_path, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle) or {}
    if not isinstance(doc, dict) or "tools" not in doc:
        raise ValidationError(f"invalid tools config: {project.tools_config_path}")
    return doc


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


def get_tool(project: Project, tool_name: str) -> ToolSpec:
    config = load_tools_config(project)
    tools = config.get("tools", {})
    if tool_name not in tools:
        available = ", ".join(sorted(tools.keys())) or "(none)"
        raise ValidationError(f"unknown tool {tool_name!r} in {project.tools_config_path}; available: {available}")
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
                raise ValidationError(f"tool {tool_name}: conda launcher requires 'env'")
            conda_bin = run_method.get("bin") or conda.get("bin", "conda")
            run_args = run_method.get("args") or conda.get("run_args", ["run", "--no-capture-output"])
            prefix = [str(conda_bin), *[str(x) for x in run_args], "-n", str(env)]
        elif mode == "prefix":
            prefix = [str(x) for x in run_method.get("prefix", [])]
        elif mode == "path":
            prefix = []
        else:
            raise ValidationError(f"tool {tool_name}: unsupported launcher mode {mode!r}")
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
        raise ValidationError(f"tool {tool_name}: run_method must be a string or mapping")
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
    config = load_tools_config(project)
    for tool_name, raw_tool in config.get("tools", {}).items():
        if not isinstance(raw_tool, dict):
            continue
        recipes = raw_tool.get("recipes", {})
        if analysis_name in recipes:
            raw = recipes[analysis_name]
            if not isinstance(raw, dict):
                raise ValidationError(f"analysis {analysis_name!r} must be a mapping")
            raw_version = raw.get("version", 1)
            if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
                raise ValidationError(
                    f"analysis {analysis_name!r}: version must be a positive integer"
                )
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
                    "must not contain wildcard characters (% * ?); matching is a "
                    "plain character prefix, not a pattern"
                )
            input_kind = str(raw.get("input_kind", "directory" if fmt == "directory" else "file")).strip()
            output_kind = str(raw.get("output_kind", "file")).strip()
            if input_kind not in {"file", "directory"}:
                raise ValidationError(
                    f"analysis {analysis_name!r}: input_kind must be 'file' or 'directory'"
                )
            if output_kind not in {"file", "directory"}:
                raise ValidationError(
                    f"analysis {analysis_name!r}: output_kind must be 'file' or 'directory'"
                )
            if fmt == "directory" and input_kind != "directory":
                raise ValidationError(
                    f"analysis {analysis_name!r}: format=directory requires input_kind=directory"
                )
            default_suffix = "" if output_kind == "directory" else ".tsv"
            output_suffix = str(raw["output_suffix"]) if "output_suffix" in raw else default_suffix
            raw_parameters = raw.get("parameters", {}) or {}
            if not isinstance(raw_parameters, dict):
                raise ValidationError(f"analysis {analysis_name!r}: parameters must be a mapping")
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
            raw_commands = raw.get("commands")
            commands: list[RecipeCommand] = []
            if raw_commands is not None:
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
                    commands.append(RecipeCommand(
                        arguments=[str(x) for x in block["arguments"]],
                        version_args=(
                            [str(x) for x in raw_version_args]
                            if raw_version_args is not None else None
                        ),
                        version_pattern=raw_version_pattern,
                    ))
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
    available = sorted(
        f"{tool}.{recipe}" for tool, tool_cfg in config.get("tools", {}).items()
        for recipe in (tool_cfg.get("recipes", {}) if isinstance(tool_cfg, dict) else {})
    )
    raise ValidationError(
        f"unknown analysis {analysis_name!r}; available recipes: {', '.join(available) or '(none)'}"
    )


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


def _detect_version_record(command: list[str], pattern: str, label: str,
                           timeout: float = 120.0, executor: Any = None) -> tuple[str, str]:
    """Run a version command and return parsed and raw provenance values."""
    executor_identity = (
        executor.cache_identity() if executor is not None and hasattr(executor, "cache_identity")
        else (executor.describe() if executor is not None else "local")
    )
    cache_key = json.dumps(
        {"command": command, "pattern": pattern, "executor": executor_identity},
        sort_keys=True,
    )
    if cache_key in _VERSION_CACHE:
        return _VERSION_CACHE[cache_key]
    if executor is not None and executor.name != "local":
        combined = _version_output_via_executor(executor, command, timeout)
    else:
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise ExternalToolError(
                f"cannot launch {label}: {exc}; check config/tools.yaml launch and version settings"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ExternalToolError(f"{label} version detection timed out after {timeout}s") from exc
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if pattern:
        match = re.search(pattern, combined, flags=re.IGNORECASE)
        if match:
            _VERSION_CACHE[cache_key] = (match.group(1).strip(), combined.strip()[:4000])
            return _VERSION_CACHE[cache_key]
    for line in combined.splitlines():
        line = line.strip()
        if line:
            # Fallback: first plausible version-like token on the first line.
            m = re.search(r"([0-9]+(?:\.[0-9]+){1,}[^\s]*)", line)
            if m:
                _VERSION_CACHE[cache_key] = (m.group(1), combined.strip()[:4000])
                return _VERSION_CACHE[cache_key]
            _VERSION_CACHE[cache_key] = (line[:200], combined.strip()[:4000])
            return _VERSION_CACHE[cache_key]
    raise ExternalToolError(
        f"could not determine version of {label} (command: {' '.join(command)}); "
        f"set 'version_pattern' in config/tools.yaml"
    )


def detect_tool_version_record(tool: ToolSpec, config: dict[str, Any], timeout: float = 120.0,
                               executor: Any = None) -> tuple[str, str]:
    """Run a tool's version_args; return parsed and raw provenance values."""
    if not tool.version_args:
        return "unknown", "version_args not configured"
    command = [*tool_command(tool, config), *tool.version_args]
    return _detect_version_record(
        command, tool.version_pattern, tool.name, timeout=timeout, executor=executor,
    )


def recipe_version_probe_command(
        recipe: Recipe, tool: ToolSpec, config: dict[str, Any],
        rendered_commands: list[list[str]]) -> tuple[list[str], str, str] | None:
    """Version probe for the recipe's logical owner (the first chain command).

    Returns ``(command, pattern, label)`` when the first command block declares
    its own ``version_args``; otherwise ``None`` and the caller falls back to
    the tool-level probe, which recipe validation guarantees targets the first
    command's program.
    """
    if not rendered_commands or recipe.commands[0].version_args is None:
        return None
    first = recipe.commands[0]
    executable = rendered_commands[0][0]
    return (
        [*launcher_prefix(tool, config), executable, *first.version_args],
        first.version_pattern,
        executable,
    )


def command_step_provenance(
        recipe: Recipe,
        tool: ToolSpec,
        config: dict[str, Any],
        rendered_commands: list[list[str]],
        tool_version: str,
        tool_version_raw: str,
        *,
        executor: Any = None,
        dry_run: bool = False,
        timeout: float = 120.0,
) -> list[dict[str, Any]]:
    """Collect program identity and version provenance for a command chain.

    A step invoking the parent tool executable inherits its already-probed
    version. Other programs are probed only when their command block declares
    ``version_args``; no version flag is guessed. Every probed step version
    also enters the analysis cache fingerprint via ``parameter_fingerprint``.
    """
    if len(recipe.commands) != len(rendered_commands):
        raise ValidationError(f"{recipe.name}: rendered command count does not match recipe")
    provenance: list[dict[str, Any]] = []
    launcher = launcher_prefix(tool, config)
    skip_remote_probe = (
        dry_run and executor is not None and getattr(executor, "name", "local") != "local"
    )
    for command_spec, rendered in zip(recipe.commands, rendered_commands):
        executable = rendered[0]
        if command_spec.version_args is not None:
            source = "command"
            version_command = [*launcher, executable, *command_spec.version_args]
            if skip_remote_probe:
                version = f"not probed (backend={executor.describe()})"
                version_raw = ""
            else:
                try:
                    version, version_raw = _detect_version_record(
                        version_command, command_spec.version_pattern, executable,
                        timeout=timeout, executor=executor,
                    )
                except ExternalToolError as exc:
                    if not dry_run:
                        raise
                    version = f"unavailable ({exc})"
                    version_raw = str(exc)
        elif executable == tool.executable:
            source = "tool"
            version = tool_version
            version_raw = tool_version_raw
            version_command = (
                [*tool_command(tool, config), *tool.version_args]
                if tool.version_args else None
            )
        else:
            source = "unconfigured"
            version = "unknown"
            version_raw = "version_args not configured for command"
            version_command = None
        provenance.append({
            "executable": executable,
            "tool_version": version,
            "tool_version_raw": version_raw,
            "version_source": source,
            "version_command": version_command,
        })
    return provenance


def _version_output_via_executor(executor: Any, command: list[str], timeout: float) -> str:
    """Capture version output through a non-local execution backend."""
    temp_parent = getattr(getattr(executor, "project", None), "logs_root", None)
    if temp_parent is not None:
        Path(temp_parent).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="operon-version-", dir=temp_parent) as tmpdir:
        stdout_path = Path(tmpdir) / "stdout.log"
        stderr_path = Path(tmpdir) / "stderr.log"
        result = executor.run(command, cwd=None, stdout_path=stdout_path,
                              stderr_path=stderr_path, timeout=timeout)
        if result.exit_code != 0:
            raise ExternalToolError(
                f"version detection via {executor.describe()} failed: "
                f"{result.error or f'exit code {result.exit_code}'}"
            )
        return (stdout_path.read_text(encoding="utf-8", errors="replace")
                + "\n" + stderr_path.read_text(encoding="utf-8", errors="replace"))


def detect_tool_version(tool: ToolSpec, config: dict[str, Any], timeout: float = 120.0) -> str:
    """Run version_args and extract the version with the configured regex."""
    return detect_tool_version_record(tool, config, timeout=timeout)[0]


def candidate_files(db: Database, recipe: Recipe, entity_type: str | None = None,
                    entity_id: str | None = None) -> list[dict[str, Any]]:
    if recipe.file_role_prefix:
        sql = (
            "SELECT * FROM files WHERE substr(file_role, 1, ?)=? AND format=? AND NOT EXISTS ("
        )
        params: list[Any] = [len(recipe.file_role_prefix), recipe.file_role_prefix, recipe.fmt]
    else:
        sql = (
            "SELECT * FROM files WHERE file_role=? AND format=? AND NOT EXISTS ("
        )
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
    for file_path in sorted(p for p in path.rglob("*") if p.is_file()):
        try:
            stat = file_path.stat()
            entries.append(f"{file_path.relative_to(path)}:{stat.st_size}:{stat.st_mtime_ns}")
        except OSError:
            entries.append(f"{file_path.relative_to(path)}:unreadable")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def database_identity(project: Project, recipe: Recipe, location_identity: str = "") -> str:
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
    cache_key = json.dumps({
        "path": str(path) if path is not None else "",
        "checksum": str(recipe.raw.get("database_checksum", "") or ""),
        "version": recipe.database_version,
        "mode": database_mode,
        "location": location_identity,
    }, sort_keys=True)
    if cache_key in _DATABASE_IDENTITY_CACHE:
        return _DATABASE_IDENTITY_CACHE[cache_key]
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
    identity = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    _DATABASE_IDENTITY_CACHE[cache_key] = identity
    return identity


def resolve_runtime_parameters(recipe: Recipe,
                               supplied: dict[str, str] | None = None) -> dict[str, str]:
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
            raise ValidationError(f"{recipe.name}: parameter {name!r} must be a scalar value")
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


def render_arguments(recipe: Recipe, *, input_path: Path, output_path: Path,
                     database_path: Path | None, threads: int,
                     file_record: dict[str, Any],
                     runtime_parameters: dict[str, str] | None = None,
                     work_dir: Path | None = None,
                     arguments: list[str] | None = None) -> list[str]:
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
    for arg in (arguments if arguments is not None else recipe.arguments):
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


def parameter_fingerprint(recipe: Recipe, args: list[str], threads: int, tool_version: str,
                          runtime_parameters: dict[str, str] | None = None,
                          commands: list[list[str]] | None = None,
                          command_versions: list[list[str]] | None = None) -> str:
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
        "qstart_column", "qend_column", "sstart_column", "send_column",
        "evalue_column", "bitscore_column", "pident_column", "hmmer_mode",
    ):
        if recipe.raw.get(key) is not None:
            payload[key] = str(recipe.raw[key])
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _job_columns() -> list[str]:
    return [
        "analysis_name", "entity_type", "entity_id", "file_id",
        "tool", "tool_version", "tool_version_raw", "launcher", "command",
        "parameter_set", "parameter_sha256", "input_sha256", "database_identity",
        "status", "output_relative_path", "output_sha256", "stdout_file", "stderr_file",
        "started_at", "finished_at", "error", "workflow_run_id", "environment_id",
        "recipe_snapshot_id",
    ]


def find_cached_job(db: Database, analysis_name: str, file_id: str, parameter_sha: str,
                    input_sha: str, database_id: str) -> dict[str, Any] | None:
    row = db.conn.execute(
        "SELECT * FROM analysis_jobs WHERE analysis_name=? AND file_id=? AND parameter_sha256=? "
        "AND input_sha256=? AND database_identity=? AND status='completed' "
        "ORDER BY job_id DESC LIMIT 1",
        (analysis_name, file_id, parameter_sha, input_sha, database_id),
    ).fetchone()
    return dict(row) if row else None


def find_adoptable_job(db: Database, analysis_name: str, file_id: str) -> dict[str, Any] | None:
    """Latest completed job for (analysis, file), ignoring the cache fingerprint."""
    row = db.conn.execute(
        "SELECT * FROM analysis_jobs WHERE analysis_name=? AND file_id=? AND status='completed' "
        "ORDER BY job_id DESC LIMIT 1",
        (analysis_name, file_id),
    ).fetchone()
    return dict(row) if row else None


def _find_verified_adoptee(db: Database, project: Project, analysis_name: str,
                           file_id: str, input_sha: str) -> tuple[dict[str, Any], Path] | None:
    """Completed job whose recorded output still exists on disk, byte-identical.

    Resume tier 2: the exact cache fingerprint may change across versions or
    recipe edits, but a verified output for the same input content is still a
    valid result and can be adopted instead of recomputed.
    """
    adoptee = find_adoptable_job(db, analysis_name, file_id)
    if adoptee is None or adoptee["input_sha256"] != input_sha:
        return None
    if not adoptee["output_relative_path"] or not adoptee["output_sha256"]:
        return None
    output = project.root / adoptee["output_relative_path"]
    if not output.exists():
        return None
    if sha256_path(output).lower() != str(adoptee["output_sha256"]).lower():
        return None
    return adoptee, output


def _sweep_stale_running_jobs(db: Database) -> int:
    """Mark jobs left RUNNING by a killed process as interrupted.

    Resume only ever reuses ``completed`` rows, so this is bookkeeping
    hygiene for the crash-only case (e.g. SIGKILL) where the graceful
    shutdown path never got a chance to finalize the row.
    """
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE analysis_jobs SET status='interrupted', finished_at=?, error=? "
            "WHERE status='RUNNING'",
            (now_iso(), "swept at startup: previous run terminated abnormally"),
        )
        return cursor.rowcount


def run_analysis(project: Project, db: Database, analysis_name: str,
                 entity_type: str | None = None, entity_id: str | None = None,
                 dry_run: bool = False, force: bool = False, limit: int | None = None,
                 threads: int | None = None, backend: str | None = None,
                 keep_partial: bool = False,
                 runtime_parameters: dict[str, str] | None = None,
                 progress_callback: Callable[[int, int, str, str], None] | None = None,
                 ) -> list[dict[str, Any]]:
    """Execute one configured analysis over all matching manifest files.

    ``progress_callback``, when given, is invoked per file as
    ``progress_callback(index, total, file_id, phase)`` with a 1-based
    ``index``; ``phase`` is ``"start"`` before the file's job begins and the
    result status (``completed``/``cached``/``error``/...) after it ends.
    """
    recipe = get_recipe(project, analysis_name)
    resolved_parameters = resolve_runtime_parameters(recipe, runtime_parameters)
    tool = get_tool(project, recipe.tool_name)
    config = load_tools_config(project)
    threads = int(threads or project.config.get("resources", {}).get("default_threads", 4) or 4)
    files = candidate_files(db, recipe, entity_type=entity_type, entity_id=entity_id)
    if limit is not None:
        files = files[: max(0, int(limit))]
    if not files:
        role_selector = (
            f"file_role_prefix={recipe.file_role_prefix}"
            if recipe.file_role_prefix else f"file_role={recipe.file_role}"
        )
        print(f"no candidate files for {analysis_name} "
              f"(entity_type={recipe.entity_type or 'any'}, {role_selector}, format={recipe.fmt})")
        return []

    from operon.execution import get_executor
    recipe_slurm = recipe.raw.get("slurm")
    executor = get_executor(
        project, backend,
        slurm_overrides=recipe_slurm if isinstance(recipe_slurm, dict) else None,
    )
    results: list[dict[str, Any]] = []
    try:
        with graceful_shutdown():
            if not dry_run:
                _sweep_stale_running_jobs(db)
            total = len(files)
            for index, file_record in enumerate(files, start=1):
                if progress_callback is not None:
                    progress_callback(index, total, file_record["file_id"], "start")
                try:
                    result = run_analysis_for_file(
                        project, db, recipe, tool, config, file_record,
                        dry_run=dry_run, force=force, threads=threads, backend=backend,
                        executor=executor, keep_partial=keep_partial,
                        runtime_parameters=resolved_parameters,
                    )
                except ShutdownRequested:
                    # Bookkeeping already finalized in run_analysis_for_file;
                    # stop the batch here and let the CLI report exit 130.
                    raise
                except Exception as exc:
                    result = {
                        "file_id": file_record["file_id"],
                        "entity_type": file_record["entity_type"],
                        "entity_id": file_record["entity_id"],
                        "analysis": recipe.name,
                        "cached": False,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                results.append(result)
                if progress_callback is not None:
                    progress_callback(index, total, file_record["file_id"],
                                      str(result.get("status", "done")))
    finally:
        close = getattr(executor, "close", None)
        if close is not None:
            close()
    return results


def _require_artifact_kind(path: Path, kind: str, label: str) -> None:
    if kind == "file" and not path.is_file():
        raise ExternalToolError(f"{label} must be a regular file: {path}")
    if kind == "directory" and not path.is_dir():
        raise ExternalToolError(f"{label} must be a directory: {path}")


def _render_output_name(recipe: Recipe, file_record: dict[str, Any], input_path: Path,
                        runtime_parameters: dict[str, str] | None = None) -> str:
    if not recipe.output_name_template:
        role = recipe.file_role or str(file_record["file_role"])
        return f"{file_record['file_id']}.{role}{recipe.output_suffix}"
    context = {
        "file_id": str(file_record["file_id"]),
        "file_role": str(file_record["file_role"]),
        "entity_type": str(file_record["entity_type"]),
        "entity_id": str(file_record["entity_id"]),
        "input_name": input_path.name,
        "input_stem": input_path.stem,
    }
    context.update(runtime_parameters or {})
    output_name = recipe.output_name_template
    for key, replacement in context.items():
        output_name = output_name.replace("${" + key + "}", replacement)
    unresolved = re.findall(r"\$\{[^}]+\}", output_name)
    if unresolved:
        raise ValidationError(
            f"{recipe.name}: unsupported placeholder(s) in output_name: {', '.join(unresolved)}"
        )
    if not output_name or output_name in {".", ".."} or Path(output_name).name != output_name:
        raise ValidationError(
            f"{recipe.name}: output_name must render to one safe path component, got {output_name!r}"
        )
    return output_name


def _remove_output_artifact(project: Project, output_path: Path) -> None:
    """Remove only the exact computed analysis artifact before a fresh run."""
    if not output_path.exists() and not output_path.is_symlink():
        return
    analysis_root = project.analysis_root.resolve()
    resolved = output_path.resolve(strict=False)
    if resolved == analysis_root or not resolved.is_relative_to(analysis_root):
        raise ExternalToolError(f"refusing to remove output outside analysis root: {output_path}")
    if output_path.is_dir() and not output_path.is_symlink():
        shutil.rmtree(output_path)
    else:
        output_path.unlink(missing_ok=True)


def run_analysis_for_file(project: Project, db: Database, recipe: Recipe, tool: ToolSpec,
                          config: dict[str, Any], file_record: dict[str, Any],
                          dry_run: bool = False, force: bool = False,
                          threads: int = 4, backend: str | None = None,
                          executor: Any = None, keep_partial: bool = False,
                          runtime_parameters: dict[str, str] | None = None) -> dict[str, Any]:
    if executor is None:
        from operon.execution import get_executor
        recipe_slurm = recipe.raw.get("slurm")
        owned_executor = get_executor(
            project, backend,
            slurm_overrides=recipe_slurm if isinstance(recipe_slurm, dict) else None,
        )
        try:
            return run_analysis_for_file(
                project, db, recipe, tool, config, file_record,
                dry_run=dry_run, force=force, threads=threads, backend=backend,
                executor=owned_executor, keep_partial=keep_partial,
                runtime_parameters=runtime_parameters,
            )
        finally:
            close = getattr(owned_executor, "close", None)
            if close is not None:
                close()
    remote_only = executor.name == "ssh" and bool(getattr(executor, "remote_root", ""))
    input_rel = file_record["relative_path"]
    input_path = project.root / input_rel
    manifest_sha = str(file_record["sha256"]).lower()
    input_is_remote = False
    if not input_path.exists():
        storage_remote = str(getattr(executor, "storage_remote", "") or "")
        if executor.name != "ssh" or not storage_remote:
            raise ExternalToolError(
                f"{file_record['file_id']}: input missing at {input_path}; hydrate it locally or "
                "configure execution.ssh.storage_remote"
            )
        if not dry_run:
            from operon.remotes import _ensure_remote_only_schema, verify_remote_record
            verify_remote_record(
                project, storage_remote, file_record, db=db,
                client=getattr(executor, "client", None),
            )
            _ensure_remote_only_schema(project)
            db.set_file_status(
                file_record["file_id"], "REMOTE_ONLY",
                reason=f"local bytes absent; remote input verified for analysis on {storage_remote}",
                actor="operon analyze",
                evidence=f"remote://{storage_remote}/{input_rel}",
            )
        input_is_remote = True
        actual_sha = manifest_sha
    else:
        _require_artifact_kind(input_path, recipe.input_kind, f"{file_record['file_id']} input")
        actual_sha = sha256_path(input_path).lower()
        if actual_sha != manifest_sha:
            raise ExternalToolError(
                f"{file_record['file_id']}: input checksum mismatch "
                f"(manifest={manifest_sha[:12]}..., actual={actual_sha[:12]}...); raw data was modified"
            )

    database_path = _resolve_database_path(project, recipe)
    database_mode = str(recipe.raw.get("database_mode", "reference") or "reference")
    if database_path is not None and database_mode == "mutable_cache" and not dry_run and not remote_only:
        database_path.mkdir(parents=True, exist_ok=True)
    if recipe.database and database_path is not None and not database_path.exists() and not dry_run and not remote_only:
        raise ExternalToolError(
            f"{recipe.name}: reference database not found: {database_path}; edit config/tools.yaml"
        )
    if remote_only and recipe.database and database_mode == "reference" and not recipe.raw.get("database_checksum"):
        raise ValidationError(
            f"{recipe.name}: remote reference databases require database_checksum so cache identity "
            "does not depend on a missing local path"
        )
    if remote_only and database_path is not None and not dry_run:
        executor.prepare_database(
            database_path, mutable_cache=database_mode == "mutable_cache",
        )
    executor_identity = (
        executor.cache_identity() if hasattr(executor, "cache_identity") else executor.describe()
    )
    db_identity = database_identity(project, recipe, executor_identity if remote_only else "")
    output_dir = project.analysis_root / recipe.output_subdir / file_record["entity_id"]
    output_name = _render_output_name(
        recipe, file_record, input_path, runtime_parameters=runtime_parameters,
    )
    output_path = output_dir / output_name
    if (
            tool.name == "busco"
            and any(arg in {"--auto-lineage", "--auto-lineage-euk", "--auto-lineage-prok"}
                    for arg in recipe.arguments)
            and "fasta" in output_path.as_posix()
    ):
        raise ValidationError(
            f"{recipe.name}: BUSCO auto-lineage output path contains 'fasta', which SEPP "
            "rewrites to 'jplace' in the full path; set output_name: '${file_id}.busco' "
            f"and ensure parent directories also avoid that substring (rendered path: {output_path})"
        )
    output_rel = output_path.relative_to(project.root).as_posix()
    rendered_args = render_arguments(
        recipe, input_path=input_path, output_path=output_path,
        database_path=database_path, threads=threads, file_record=file_record,
        runtime_parameters=runtime_parameters,
    )
    work_dir: Path | None = None
    rendered_commands: list[list[str]] = []
    if recipe.commands:
        # Deterministic per-file scratch directory for intermediate artifacts:
        # the rendered path enters the cache fingerprint, so it must not
        # contain a random component.
        work_dir = output_dir / f"{output_name}.work"
        rendered_commands = [
            render_arguments(
                recipe, input_path=input_path, output_path=output_path,
                database_path=database_path, threads=threads, file_record=file_record,
                runtime_parameters=runtime_parameters, work_dir=work_dir,
                arguments=block.arguments,
            )
            for block in recipe.commands
        ]
    owner_probe = recipe_version_probe_command(recipe, tool, config, rendered_commands)
    try:
        if dry_run and executor.name != "local":
            # Do not submit cluster jobs or open SSH connections for a dry run;
            # the cache verdict below may be approximate without the version.
            version = f"not probed (backend={executor.describe()})"
            version_raw = ""
        elif owner_probe is not None:
            # The first chain command is the recipe's logical owner: its
            # declared probe, not the tool-level one, defines tool_version.
            version, version_raw = _detect_version_record(
                *owner_probe, executor=executor,
            )
        else:
            version, version_raw = detect_tool_version_record(tool, config, executor=executor)
    except ExternalToolError as exc:
        if not dry_run:
            raise
        version = f"unavailable ({exc})"
        version_raw = str(exc)
    step_provenance = (
        command_step_provenance(
            recipe, tool, config, rendered_commands, version, version_raw,
            executor=executor, dry_run=dry_run,
        )
        if rendered_commands else None
    )
    parameter_sha = parameter_fingerprint(
        recipe, rendered_args, threads, version, runtime_parameters=runtime_parameters,
        commands=rendered_commands or None,
        command_versions=(
            [[step["executable"], step["tool_version"]] for step in step_provenance]
            if step_provenance else None
        ),
    )
    cached = find_cached_job(
        db, recipe.name, file_record["file_id"], parameter_sha, actual_sha, db_identity
    )
    if rendered_commands:
        step_commands = [
            [*launcher_prefix(tool, config), *block] for block in rendered_commands
        ]
        command_display = " && ".join(" ".join(step) for step in step_commands)
    else:
        step_commands = None
        command = [*tool_command(tool, config), *rendered_args]
        command_display = " ".join(command)

    if dry_run:
        adoptee = (
            None if runtime_parameters
            else find_adoptable_job(db, recipe.name, file_record["file_id"])
        )
        adoptable = (cached is None and not force and adoptee is not None
                     and adoptee["input_sha256"] == actual_sha)
        if cached is not None and not force:
            status = "cached"
        elif adoptable:
            status = "adoptable"
        else:
            status = "planned"
        return {
            "file_id": file_record["file_id"], "entity_type": file_record["entity_type"],
            "entity_id": file_record["entity_id"], "analysis": recipe.name,
            "cached": cached is not None, "tool_version": version,
            "command": command_display, "adoptable": adoptable,
            "status": status, "output": output_rel,
            "dry_run": True,
        }

    # Snapshot the recipe together with its tool spec; any edit to either
    # produces a new content-addressed snapshot, keeping jobs traceable to
    # the exact configuration that produced them.
    recipe_snapshot_id = db.record_recipe(
        recipe.name, recipe.version, {"recipe": recipe.raw, "tool": tool.raw}
    )

    if cached is not None and not force:
        if output_path.exists() and sha256_path(output_path) == cached["output_sha256"]:
            return {
                "file_id": file_record["file_id"], "entity_type": file_record["entity_type"],
                "entity_id": file_record["entity_id"], "analysis": recipe.name,
                "cached": True, "job_id": cached["job_id"],
                "tool_version": cached["tool_version"], "command": command_display,
                "output": output_rel, "status": "cached",
            }
        # Cached row exists but its output was deleted/modified: re-run and record a new job.
        with db.transaction() as conn:
            conn.execute("UPDATE analysis_jobs SET status='superseded' WHERE job_id=?", (cached["job_id"],))
        cached = None

    if cached is not None and force:
        # Force re-run keeps the historical row but removes it from the completed cache.
        with db.transaction() as conn:
            conn.execute("UPDATE analysis_jobs SET status='superseded' WHERE job_id=?", (cached["job_id"],))
        cached = None

    if cached is None and not force and not runtime_parameters:
        # Resume tier 2: adopt a verified existing output instead of
        # recomputing when the exact cache fingerprint changed (version
        # upgrade, recipe rename) but the input content is unchanged.
        verified = _find_verified_adoptee(db, project, recipe.name, file_record["file_id"], actual_sha)
        if verified is not None:
            adoptee, _adoptee_output = verified
            finished = now_iso()
            columns = _job_columns()
            adopted_job = {
                "analysis_name": recipe.name,
                "entity_type": file_record["entity_type"],
                "entity_id": file_record["entity_id"],
                "file_id": file_record["file_id"],
                "tool": tool.name,
                "tool_version": version,
                "tool_version_raw": version_raw,
                "launcher": tool.run_method if executor.name == "local" else f"{tool.run_method} [{executor.describe()}]",
                "command": command_display,
                "parameter_set": json.dumps({
                    "arguments": rendered_args, "threads": threads,
                    "runtime_parameters": runtime_parameters or {},
                }, ensure_ascii=False),
                "parameter_sha256": parameter_sha,
                "input_sha256": actual_sha,
                "database_identity": db_identity,
                "status": "completed",
                "output_relative_path": adoptee["output_relative_path"],
                "output_sha256": adoptee["output_sha256"],
                "stdout_file": adoptee["stdout_file"],
                "stderr_file": adoptee["stderr_file"],
                "started_at": finished,
                "finished_at": finished,
                "workflow_run_id": adoptee["workflow_run_id"],
                "environment_id": adoptee["environment_id"],
                # Adopted results inherit the original job's recipe snapshot:
                # they were produced by that configuration, not today's.
                "recipe_snapshot_id": adoptee["recipe_snapshot_id"],
            }
            with db.transaction() as conn:
                cursor = conn.execute(
                    f"INSERT INTO analysis_jobs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    [adopted_job.get(c) for c in columns],
                )
                adopted_job_id = int(cursor.lastrowid)
                conn.execute(
                    "INSERT INTO changes(object_type, object_id, field, old_value, new_value, "
                    "reason, evidence, actor, changed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("analysis_job", str(adopted_job_id), "status", None, "completed",
                     f"adopted verified output from job {adoptee['job_id']} "
                     "after cache fingerprint change",
                     f"output_sha256={adoptee['output_sha256']}", "operon analyze", finished),
                )
            print(f"{file_record['file_id']}: adopting verified output from job "
                  f"{adoptee['job_id']} for {recipe.name} (cache fingerprint changed)")
            return {
                "file_id": file_record["file_id"], "entity_type": file_record["entity_type"],
                "entity_id": file_record["entity_id"], "analysis": recipe.name,
                "cached": True, "adopted": True, "job_id": adopted_job_id,
                "tool_version": version, "command": command_display,
                "output": adoptee["output_relative_path"], "status": "adopted",
            }

    output_dir.mkdir(parents=True, exist_ok=True)
    # Force/uncached runs must produce a fresh output; an old file from a
    # superseded job must not satisfy expected-output validation.
    _remove_output_artifact(project, output_path)
    started = now_iso()
    job = {
        "analysis_name": recipe.name,
        "entity_type": file_record["entity_type"],
        "entity_id": file_record["entity_id"],
        "file_id": file_record["file_id"],
        "tool": tool.name,
        "tool_version": version,
        "tool_version_raw": version_raw,
        "launcher": tool.run_method if executor.name == "local" else f"{tool.run_method} [{executor.describe()}]",
        "command": command_display,
        "parameter_set": json.dumps({
            "arguments": rendered_args, "threads": threads,
            "runtime_parameters": runtime_parameters or {},
            **({"commands": rendered_commands} if rendered_commands else {}),
        }, ensure_ascii=False),
        "parameter_sha256": parameter_sha,
        "input_sha256": actual_sha,
        "database_identity": db_identity,
        "status": "RUNNING",
        "started_at": started,
        "recipe_snapshot_id": recipe_snapshot_id,
    }
    columns = _job_columns()
    with db.transaction() as conn:
        cursor = conn.execute(
            f"INSERT INTO analysis_jobs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [job.get(c) for c in columns],
        )
        job_id = int(cursor.lastrowid)
    job["job_id"] = job_id

    try:
        from operon.workflow import run_external_command
        if work_dir is not None:
            # Drop stale intermediates from an earlier failed/interrupted run.
            _remove_output_artifact(project, work_dir)
            work_dir.mkdir(parents=True, exist_ok=True)
            prepare = getattr(executor, "prepare_database", None)
            if executor.name == "ssh" and getattr(executor, "remote_root", "") and prepare is not None:
                prepare(work_dir, mutable_cache=True)
        run_record = run_external_command(
            db, project, step_commands[0] if step_commands else command,
            step=f"analysis:{recipe.name}",
            entity_type=file_record["entity_type"],
            entity_id=file_record["entity_id"],
            parameter_set=f"{recipe.name}:{version}",
            expected_outputs=[output_path],
            cwd=project.root,
            tool=tool.name,
            tool_version=version,
            backend=backend,
            threads=threads,
            stage_inputs=[input_path] if executor.name == "ssh" and not input_is_remote else (),
            executor=executor,
            commands=step_commands,
            command_details=step_provenance,
        )
        _require_artifact_kind(output_path, recipe.output_kind, f"{recipe.name} output")
        output_sha = sha256_path(output_path)
        hit_count, query_count, query_with_hit_count, metric_count, alignment_count = (
            parse_and_store_results(
                db, project, recipe, tool, version, file_record, job_id, output_path, output_sha,
                runtime_parameters=runtime_parameters,
            )
        )
        if work_dir is not None and not keep_partial:
            _remove_output_artifact(project, work_dir)
        finished = now_iso()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE analysis_jobs SET status='completed', output_relative_path=?, output_sha256=?, "
                "stdout_file=?, stderr_file=?, finished_at=?, workflow_run_id=?, environment_id=? "
                "WHERE job_id=?",
                (output_rel, output_sha, run_record.get("stdout_file"), run_record.get("stderr_file"),
                 finished, run_record.get("run_id"), run_record.get("environment_id"), job_id),
            )
        return {
            "file_id": file_record["file_id"], "entity_type": file_record["entity_type"],
            "entity_id": file_record["entity_id"], "analysis": recipe.name,
            "cached": False, "job_id": job_id, "tool_version": version,
            "command": command_display, "output": output_rel, "status": "completed",
            "hit_count": hit_count, "query_count": query_count,
            "query_with_hit_count": query_with_hit_count,
            "metric_count": metric_count, "alignment_count": alignment_count,
        }
    except ShutdownRequested as exc:
        # Graceful shutdown: finalize the job row, drop the partial output
        # (unless --keep-partial), then abort the batch.  Partial stdout/
        # stderr logs are kept for diagnosis.
        with db.transaction() as conn:
            conn.execute(
                "UPDATE analysis_jobs SET status='interrupted', finished_at=?, error=? WHERE job_id=?",
                (now_iso(), f"interrupted by signal {exc.signum}", job_id),
            )
        if not keep_partial:
            _remove_output_artifact(project, output_path)
            if work_dir is not None:
                _remove_output_artifact(project, work_dir)
        raise
    except Exception as exc:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE analysis_jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                (now_iso(), f"{type(exc).__name__}: {exc}", job_id),
            )
        if work_dir is not None and not keep_partial:
            _remove_output_artifact(project, work_dir)
        raise


def parse_and_store_results(db: Database, project: Project, recipe: Recipe, tool: ToolSpec,
                            tool_version: str, file_record: dict[str, Any], job_id: int,
                            output_path: Path, output_sha: str,
                            runtime_parameters: dict[str, str] | None = None
                            ) -> tuple[int, int, int, int, int]:
    """Parse tool output and synchronize summary + top hits + alignments into SQLite."""
    hits, alignments = parse_hits(output_path, recipe)
    metrics: list[dict[str, Any]] = []
    if recipe.result_parser == "busco_json":
        metrics = _parse_busco_json(output_path, recipe)
    elif recipe.result_parser != "none":
        queries = sorted({h["query_id"] for h in hits})
        query_with_hit = sorted({h["query_id"] for h in hits if h.get("rank") == 1})
        hit_pairs = sorted({(h["query_id"], h["subject_id"]) for h in hits})
        metrics = [
            _result_metric("query_count", len(queries)),
            _result_metric("query_with_hit_count", len(query_with_hit)),
            _result_metric("hit_count", len(hit_pairs)),
        ]
        best_evalue = None
        for hit in hits:
            if hit["metric_name"] in {"evalue", "E-value"} and hit["metric_numeric"] is not None:
                best_evalue = hit["metric_numeric"] if best_evalue is None else min(best_evalue, hit["metric_numeric"])
        if best_evalue is not None:
            metrics.append(_result_metric("best_evalue", best_evalue))

    qc_stage = f"analysis:{recipe.name}"
    if runtime_parameters:
        suffix = ",".join(
            f"{name}={runtime_parameters[name]}" for name in sorted(runtime_parameters)
        )
        qc_stage = f"{qc_stage}:{suffix}"

    with db.transaction() as conn:
        conn.execute("DELETE FROM analysis_results WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM analysis_hits WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM analysis_alignments WHERE job_id=?", (job_id,))
    for hit in hits:
        db.conn.execute(
            "INSERT INTO analysis_hits(job_id, entity_type, entity_id, file_id, analysis_name, "
            "query_id, subject_id, metric_name, metric_value, metric_numeric, metric_unit, hit_rank) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, file_record["entity_type"], file_record["entity_id"], file_record["file_id"],
                recipe.name, hit["query_id"], hit["subject_id"], hit["metric_name"],
                str(hit["metric_value"]), hit["metric_numeric"], hit.get("metric_unit"),
                hit["rank"],
            ),
        )
    for alignment in alignments:
        db.conn.execute(
            "INSERT INTO analysis_alignments(job_id, entity_type, entity_id, file_id, analysis_name, "
            "query_id, subject_id, hit_rank, query_start, query_end, subject_start, subject_end, "
            "evalue, bitscore, percent_identity, extra_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, file_record["entity_type"], file_record["entity_id"], file_record["file_id"],
                recipe.name, alignment["query_id"], alignment["subject_id"], alignment["rank"],
                alignment["qstart"], alignment["qend"], alignment["sstart"], alignment["send"],
                alignment["evalue"], alignment["bitscore"], alignment["pident"],
                json.dumps(alignment["extra"], ensure_ascii=False, sort_keys=True)
                if alignment["extra"] else None,
            ),
        )
    db.conn.commit()

    with db.transaction() as conn:
        for metric in metrics:
            conn.execute(
                "INSERT INTO analysis_results(job_id, entity_type, entity_id, file_id, analysis_name, "
                "metric_name, metric_value, metric_numeric, metric_unit) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    job_id, file_record["entity_type"], file_record["entity_id"], file_record["file_id"],
                    recipe.name, metric["metric_name"], metric["metric_value"],
                    metric["metric_numeric"], metric.get("metric_unit"),
                ),
            )
            db.insert_qc_result({
                "entity_type": file_record["entity_type"],
                "entity_id": file_record["entity_id"],
                "file_id": file_record["file_id"],
                "file_sha256": file_record["sha256"],
                "qc_stage": qc_stage,
                "metric_name": metric["metric_name"],
                "metric_value": metric["metric_value"],
                "metric_numeric": metric["metric_numeric"],
                "metric_unit": metric.get("metric_unit"),
                "tool": tool.name,
                "tool_version": tool_version,
                "parameter_set": f"{recipe.name}:{output_sha[:16]}",
                "evaluated_at": now_iso(),
            })
    queries = {h["query_id"] for h in hits}
    query_with_hit = {h["query_id"] for h in hits if h.get("rank") == 1}
    hit_pairs = {(h["query_id"], h["subject_id"]) for h in hits}
    return len(hit_pairs), len(queries), len(query_with_hit), len(metrics), len(alignments)


def _result_metric(name: str, value: Any, unit: str | None = None) -> dict[str, Any]:
    numeric: float | None = None
    if not isinstance(value, bool):
        try:
            candidate = float(value)
            if math.isfinite(candidate):
                numeric = candidate
        except (TypeError, ValueError):
            pass
    if value is None:
        rendered = ""
    elif isinstance(value, (dict, list)):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        rendered = str(value)
    return {
        "metric_name": name,
        "metric_value": rendered,
        "metric_numeric": numeric,
        "metric_unit": unit,
    }


def parse_hits(output_path: Path, recipe: Recipe) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse tool output into (EAV hit rows, structured alignment rows)."""
    parser = recipe.result_parser
    if parser in {"none", "busco_json"}:
        return [], []
    if parser == "blast_tabular":
        return _parse_blast_tabular(output_path, recipe)
    if parser == "hmmer_tblout":
        return _parse_hmmer_tblout(output_path, recipe), []
    if parser == "hmmer_domtblout":
        return _parse_hmmer_domtblout(output_path, recipe)
    if parser == "rpsbproc_tabular":
        return _parse_rpsbproc_tabular(output_path, recipe)
    raise ExternalToolError(f"unsupported result_parser {parser!r} for {recipe.name}")


def _select_busco_json(output_path: Path, recipe: Recipe) -> Path:
    if output_path.is_file():
        return output_path
    result_glob = str(recipe.raw.get("result_glob", "short_summary*.json") or "short_summary*.json")
    if Path(result_glob).is_absolute() or ".." in Path(result_glob).parts:
        raise ValidationError(f"{recipe.name}: result_glob must stay within the output directory")
    candidates = sorted(p for p in output_path.glob(result_glob) if p.is_file())
    if not candidates:
        raise ExternalToolError(
            f"{recipe.name}: no BUSCO JSON matched {result_glob!r} under {output_path}"
        )
    if len(candidates) == 1:
        return candidates[0]
    specific = [p for p in candidates if ".specific." in p.name]
    if len(specific) == 1:
        return specific[0]
    names = ", ".join(p.relative_to(output_path).as_posix() for p in candidates)
    raise ExternalToolError(
        f"{recipe.name}: result_glob matched multiple ambiguous BUSCO JSON files: {names}; "
        "narrow result_glob to the final specific summary"
    )


def _first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _parse_busco_json(output_path: Path, recipe: Recipe) -> list[dict[str, Any]]:
    summary_path = _select_busco_json(output_path, recipe)
    try:
        with open(summary_path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalToolError(f"{recipe.name}: invalid BUSCO JSON {summary_path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("results"), dict):
        raise ExternalToolError(f"{recipe.name}: BUSCO JSON has no results object: {summary_path}")

    results = document["results"]
    parameters = document.get("parameters") if isinstance(document.get("parameters"), dict) else {}
    lineage = document.get("lineage_dataset") if isinstance(document.get("lineage_dataset"), dict) else {}
    versions = document.get("versions") if isinstance(document.get("versions"), dict) else {}
    metrics: list[dict[str, Any]] = []

    result_fields = [
        ("busco_complete_percent", ("Complete percentage",), "percent"),
        ("busco_complete_count", ("Complete BUSCOs",), "count"),
        ("busco_single_copy_percent", ("Single copy percentage", "Single-copy percentage"), "percent"),
        ("busco_single_copy_count", ("Single copy BUSCOs", "Single-copy BUSCOs"), "count"),
        ("busco_duplicated_percent", ("Multi copy percentage", "Duplicated percentage"), "percent"),
        ("busco_duplicated_count", ("Multi copy BUSCOs", "Duplicated BUSCOs"), "count"),
        ("busco_fragmented_percent", ("Fragmented percentage",), "percent"),
        ("busco_fragmented_count", ("Fragmented BUSCOs",), "count"),
        ("busco_missing_percent", ("Missing percentage",), "percent"),
        ("busco_missing_count", ("Missing BUSCOs",), "count"),
        ("busco_n_markers", ("n_markers",), "count"),
        ("busco_domain", ("domain",), None),
        ("busco_one_line_summary", ("one_line_summary",), None),
    ]
    for metric_name, keys, unit in result_fields:
        value = _first_value(results, *keys)
        if value is not None:
            metrics.append(_result_metric(metric_name, value, unit))

    metadata_fields = [
        ("busco_lineage_dataset", lineage, ("name",), None),
        ("busco_dataset_creation_date", lineage, ("creation_date",), None),
        ("busco_dataset_buscos", lineage, ("number_of_buscos",), "count"),
        ("busco_dataset_species", lineage, ("number_of_species",), "count"),
        ("busco_datasets_version", parameters, ("datasets_version",), None),
        ("busco_orthodb_version", parameters, ("orthodb_version",), None),
        ("busco_dataset_version", parameters, ("dataset_version",), None),
        ("busco_ncbi_taxid", parameters, ("ncbi_taxid",), None),
        ("busco_reported_version", versions, ("busco",), None),
    ]
    for metric_name, source, keys, unit in metadata_fields:
        value = _first_value(source, *keys)
        if value is not None:
            metrics.append(_result_metric(metric_name, value, unit))

    metric_names = {m["metric_name"] for m in metrics}
    required = {"busco_complete_percent", "busco_n_markers"}
    missing = sorted(required - metric_names)
    if missing:
        raise ExternalToolError(
            f"{recipe.name}: BUSCO JSON {summary_path} is missing required metrics: {', '.join(missing)}"
        )
    return metrics


_ALIGNMENT_FIELD_KEYS = (
    ("qstart", "qstart_column", ("qstart",), True),
    ("qend", "qend_column", ("qend",), True),
    ("sstart", "sstart_column", ("sstart",), True),
    ("send", "send_column", ("send",), True),
    ("evalue", "evalue_column", ("evalue",), False),
    ("bitscore", "bitscore_column", ("bitscore",), False),
    ("pident", "pident_column", ("pident",), False),
)


def _alignment_number(raw: Any, integer: bool = False) -> int | float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return int(value) if integer else value


def _parse_blast_tabular(path: Path, recipe: Recipe) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    columns = [str(c) for c in recipe.raw.get("result_columns", [])]
    if len(columns) < 2:
        raise ValidationError(f"{recipe.name}: result_columns must contain at least query and subject")
    metric_columns = [str(c) for c in recipe.raw.get("hit_metric_columns", columns[2:])]
    numeric_columns = {str(c) for c in recipe.raw.get("numeric_columns", metric_columns)}
    query_index = columns.index(recipe.raw.get("query_column", columns[0]))
    subject_index = columns.index(recipe.raw.get("subject_column", columns[1]))
    metric_indexes = [columns.index(c) for c in metric_columns if c in columns]
    alignment_indexes: dict[str, tuple[int, bool]] = {}
    for field, recipe_key, common_names, integer in _ALIGNMENT_FIELD_KEYS:
        declared = recipe.raw.get(recipe_key)
        if declared is not None and str(declared) in columns:
            alignment_indexes[field] = (columns.index(str(declared)), integer)
            continue
        for name in common_names:
            if name in columns:
                alignment_indexes[field] = (columns.index(name), integer)
                break
    structured = {index for index, _ in alignment_indexes.values()} | {query_index, subject_index}
    rank: dict[str, int] = {}
    hits: list[dict[str, Any]] = []
    alignments: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n\r")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) != len(columns):
                continue
            query_id = fields[query_index].strip()
            subject_id = fields[subject_index].strip()
            if not query_id or not subject_id:
                continue
            rank[query_id] = rank.get(query_id, 0) + 1
            alignment: dict[str, Any] = {
                "query_id": query_id,
                "subject_id": subject_id,
                "rank": rank[query_id],
                "extra": {
                    columns[i]: fields[i].strip()
                    for i in range(len(columns)) if i not in structured
                },
            }
            for field in ("qstart", "qend", "sstart", "send", "evalue", "bitscore", "pident"):
                mapped = alignment_indexes.get(field)
                alignment[field] = (
                    _alignment_number(fields[mapped[0]], integer=mapped[1])
                    if mapped is not None else None
                )
            alignments.append(alignment)
            if rank[query_id] > recipe.max_hits_per_query:
                continue
            for metric_index, metric_name in zip(metric_indexes, [columns[i] for i in metric_indexes], strict=True):
                raw_value = fields[metric_index].strip()
                if raw_value == "":
                    continue
                try:
                    numeric = float(raw_value)
                except ValueError:
                    numeric = None
                hits.append({
                    "query_id": query_id,
                    "subject_id": subject_id,
                    "metric_name": metric_name,
                    "metric_value": raw_value,
                    "metric_numeric": numeric if metric_name in numeric_columns else None,
                    "metric_unit": None,
                    "rank": rank[query_id],
                })
    return hits, alignments


_HMMER_PROGRAM_HEADER = re.compile(r"^#\s*(\S+)\s*::")


def _hmmer_swap(path: Path, recipe: Recipe) -> bool:
    """Whether to swap query/subject: True normalizes hmmsearch output so
    query_id is the searched sequence and subject_id the HMM profile.

    An explicit recipe ``hmmer_mode`` wins; otherwise the program name in the
    first ``# <program> ::`` header line decides; files without either keep
    the hmmscan mapping.
    """
    mode = recipe.raw.get("hmmer_mode")
    if mode:
        if mode == "hmmsearch":
            return True
        if mode == "hmmscan":
            return False
        raise ValidationError(
            f"{recipe.name}: hmmer_mode must be 'hmmsearch' or 'hmmscan', got {mode!r}"
        )
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n\r")
            if not line.startswith("#"):
                break
            match = _HMMER_PROGRAM_HEADER.match(line)
            if match:
                return match.group(1) == "hmmsearch"
    return False


def _parse_hmmer_tblout(path: Path, recipe: Recipe) -> list[dict[str, Any]]:
    swap = _hmmer_swap(path, recipe)
    rank: dict[str, int] = {}
    hits: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n\r")
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) < 6:
                continue
            target_name = fields[0]
            query_name = fields[2]
            if swap:
                target_name, query_name = query_name, target_name
            if not target_name or not query_name:
                continue
            rank[query_name] = rank.get(query_name, 0) + 1
            if rank[query_name] > recipe.max_hits_per_query:
                continue
            for metric_name, raw_value in (("evalue", fields[4]), ("score", fields[5])):
                try:
                    numeric = float(raw_value)
                except ValueError:
                    numeric = None
                hits.append({
                    "query_id": query_name,
                    "subject_id": target_name,
                    "metric_name": metric_name,
                    "metric_value": raw_value,
                    "metric_numeric": numeric,
                    "metric_unit": None,
                    "rank": rank[query_name],
                })
    return hits


def _parse_hmmer_domtblout(path: Path, recipe: Recipe) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    swap = _hmmer_swap(path, recipe)
    rank: dict[str, int] = {}
    hits: list[dict[str, Any]] = []
    alignments: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n\r")
            if not line.strip() or line.startswith("#"):
                continue
            # domtblout has 22 fixed whitespace-separated columns; the
            # description column may contain spaces, so cap the split.
            fields = line.split(None, 22)
            if len(fields) < 22:
                continue
            target_name = fields[0]
            query_name = fields[3]
            if swap:
                target_name, query_name = query_name, target_name
            if not target_name or not query_name:
                continue
            rank[query_name] = rank.get(query_name, 0) + 1
            try:
                evalue_numeric: float | None = float(fields[12])
            except ValueError:
                evalue_numeric = None
            try:
                score_numeric: float | None = float(fields[13])
            except ValueError:
                score_numeric = None
            alignments.append({
                "query_id": query_name,
                "subject_id": target_name,
                "rank": rank[query_name],
                "qstart": _alignment_number(fields[17], integer=True),
                "qend": _alignment_number(fields[18], integer=True),
                "sstart": None,
                "send": None,
                "evalue": evalue_numeric,
                "bitscore": score_numeric,
                "pident": None,
                "extra": {
                    "hmm_from": fields[15], "hmm_to": fields[16],
                    "env_from": fields[19], "env_to": fields[20],
                },
            })
            if rank[query_name] > recipe.max_hits_per_query:
                continue
            for metric_name, raw_value, numeric in (
                ("evalue", fields[12], evalue_numeric),
                ("score", fields[13], score_numeric),
            ):
                hits.append({
                    "query_id": query_name,
                    "subject_id": target_name,
                    "metric_name": metric_name,
                    "metric_value": raw_value,
                    "metric_numeric": numeric,
                    "metric_unit": None,
                    "rank": rank[query_name],
                })
    return hits, alignments


def _parse_rpsbproc_tabular(path: Path, recipe: Recipe) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse rpsbproc tabular output (DATA/SESSION/QUERY/DOMAINS blocks).

    Domain rows are attributed to their enclosing QUERY block; the alignment
    query_id is the QUERY definition line. SITES/MOTIFS blocks and anything
    after ENDDATA are ignored.
    """
    session = ""
    query_id = ""
    definition = ""
    mode = ""
    query_keys: set[tuple[str, str]] = set()
    rank: dict[tuple[str, str], int] = {}
    hits: list[dict[str, Any]] = []
    alignments: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for lineno, raw_line in enumerate(handle, 1):
            line = raw_line.rstrip("\n\r")
            if not line.strip() or line.startswith("#"):
                continue
            if line == "ENDDATA":
                break
            if line == "DATA":
                continue
            if line.startswith("SESSION"):
                parts = line.split("\t")
                session = parts[1].strip() if len(parts) > 1 else ""
                mode = ""
                continue
            if line.startswith("QUERY\t"):
                parts = line.split("\t", 4)
                if len(parts) < 5:
                    raise ExternalToolError(
                        f"{recipe.name}: malformed rpsbproc QUERY record at {path}:{lineno}"
                    )
                query_id = parts[1].strip()
                definition = parts[4]
                key = (session, query_id)
                if key in query_keys:
                    raise ExternalToolError(
                        f"{recipe.name}: duplicate rpsbproc query "
                        f"(session={session!r}, query={query_id!r}) at {path}:{lineno}"
                    )
                query_keys.add(key)
                mode = ""
                continue
            if line == "DOMAINS":
                mode = "domains"
                continue
            if line == "SITES":
                mode = "sites"
                continue
            if line == "MOTIFS":
                mode = "motifs"
                continue
            if line.startswith("END"):
                mode = ""
                continue
            if mode != "domains":
                continue
            fields = line.split("\t")
            if len(fields) < 12:
                raise ExternalToolError(
                    f"{recipe.name}: malformed rpsbproc domain record at {path}:{lineno}"
                )
            if not query_id:
                raise ExternalToolError(
                    f"{recipe.name}: rpsbproc domain record without a preceding QUERY "
                    f"at {path}:{lineno}"
                )
            key = (session, query_id)
            rank[key] = rank.get(key, 0) + 1
            evalue_numeric = _alignment_number(fields[6])
            bitscore_numeric = _alignment_number(fields[7])
            alignments.append({
                "query_id": definition,
                "subject_id": fields[8].strip(),
                "rank": rank[key],
                "qstart": _alignment_number(fields[4], integer=True),
                "qend": _alignment_number(fields[5], integer=True),
                "sstart": None,
                "send": None,
                "evalue": evalue_numeric,
                "bitscore": bitscore_numeric,
                "pident": None,
                "extra": {
                    "hit_type": fields[2].strip(),
                    "pssm_id": fields[3].strip(),
                    "short_name": fields[9].strip(),
                    "incomplete": fields[10].strip(),
                    "superfamily_pssm": fields[11].strip(),
                    "session": session,
                    "rps_query_id": fields[1].strip(),
                },
            })
            if rank[key] > recipe.max_hits_per_query:
                continue
            for metric_name, raw_value, numeric in (
                ("evalue", fields[6].strip(), evalue_numeric),
                ("bitscore", fields[7].strip(), bitscore_numeric),
            ):
                hits.append({
                    "query_id": definition,
                    "subject_id": fields[8].strip(),
                    "metric_name": metric_name,
                    "metric_value": raw_value,
                    "metric_numeric": numeric,
                    "metric_unit": None,
                    "rank": rank[key],
                })
    return hits, alignments


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
    return format_table(["tool", "run_method", "detected_version", "recipes"], rows), all_ok
