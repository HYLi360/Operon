#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""External tool commands: run-external, analyze, environments, and recipes."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from operon.cli._common import _print_snapshot_document
from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.utils import format_table
from operon.workflow import new_run_id


def add_run_external_parser(sub):
    p = sub.add_parser(
        "run-external",
        help="run an external tool with structured provenance (stdout/stderr, exit code, expected outputs)",
    )
    p.add_argument(
        "--step", required=True, help="workflow step name, e.g. busco / quast / fastp"
    )
    p.add_argument(
        "--command",
        dest="command_line",
        required=True,
        help="quoted command line, e.g. 'busco -i in.fa -o out -m genome'",
    )
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")
    p.add_argument("--parameter-set")
    p.add_argument(
        "--tool",
        help="tool name from config/tools.yaml; its version is detected and recorded",
    )
    p.add_argument(
        "--input",
        dest="inputs",
        action="append",
        default=[],
        help="declared input file/directory; hashed for provenance and staged for "
        "non-shared SSH execution",
    )
    p.add_argument(
        "--threads", type=int, help="threads requested from the execution backend"
    )
    p.add_argument(
        "--expected-output",
        action="append",
        default=[],
        help="path that must exist and be non-empty",
    )
    p.add_argument("--cwd")
    p.add_argument("--timeout", type=float)
    p.add_argument("--events", help="declare the plugin event JSONL output path")
    p.add_argument(
        "--backend",
        choices=["local", "slurm", "ssh"],
        help="execution backend (default: execution.backend in project.yaml)",
    )


def add_tools_parser(sub):
    p = sub.add_parser("tools", help="manage declarative external-tool presets")
    tools_sub = p.add_subparsers(dest="tools_command", required=True)
    tp = tools_sub.add_parser("add-preset", help="merge a plugin preset fragment")
    tp.add_argument("--file", required=True)
    tp.add_argument("--dry-run", action="store_true")


def add_tools_check_parser(sub):
    sub.add_parser(
        "tools-check", help="detect configured external tools and their versions"
    )


def add_analyze_parser(sub):
    p = sub.add_parser(
        "analyze",
        help="run a configured external-tool recipe over matching manifest artifacts",
    )
    p.add_argument(
        "--analysis",
        required=True,
        help="recipe name, e.g. blastn_nt / hmmsearch_pfam / busco_autolineage",
    )
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="set a runtime parameter declared by the recipe; repeat for multiple values",
    )
    p.add_argument("--entity-type", help="restrict to entity type")
    p.add_argument("--entity-id", help="restrict to one entity")
    p.add_argument("--threads", type=int, help="override default threads")
    p.add_argument("--limit", type=int, help="only process the first N matching files")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="show planned commands and cache status without executing",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="re-run even when a completed cached job exists",
    )
    p.add_argument(
        "--keep-partial",
        action="store_true",
        help="on Ctrl+C/SIGTERM, keep the interrupted step's partial output instead of deleting it",
    )
    p.add_argument(
        "--detach",
        action="store_true",
        help="relaunch this analysis as an independent background session and "
        "return immediately; console output goes to "
        "logs/analyze-detach-<timestamp>.log, progress to the recorded runs",
    )
    p.add_argument("--events", help="declare the plugin event JSONL output path")
    p.add_argument(
        "--backend",
        choices=["local", "slurm", "ssh"],
        help="execution backend (default: execution.backend in project.yaml)",
    )


def add_environments_parser(sub):
    p = sub.add_parser(
        "environments",
        help="inspect captured environments and export Conda specifications",
    )
    environments_sub = p.add_subparsers(dest="environments_command", required=True)
    environments_sub.add_parser("list", help="list captured environment IDs")
    ep = environments_sub.add_parser(
        "show", help="print a captured environment as JSON"
    )
    ep.add_argument("environment_id")
    ep = environments_sub.add_parser(
        "export", help="print a Conda reconstruction specification"
    )
    ep.add_argument("environment_id")
    ep.add_argument("--format", choices=["explicit", "yaml"], default="explicit")


def add_recipes_parser(sub):
    p = sub.add_parser(
        "recipes",
        help="list configured analysis recipes and inspect recorded recipe snapshots",
    )
    recipes_sub = p.add_subparsers(dest="recipes_command", required=True)
    recipes_sub.add_parser("list", help="list recipes from config/tools.yaml")
    rp = recipes_sub.add_parser(
        "history", help="show the recorded snapshot history of one recipe"
    )
    rp.add_argument("name")
    rp = recipes_sub.add_parser(
        "show",
        help="print a recorded recipe snapshot as YAML; to restore a version, copy the "
        "output back into config/tools.yaml manually (no in-place restore)",
    )
    rp.add_argument("name")
    rp.add_argument(
        "--snapshot-id", type=int, help="snapshot to print (default: latest)"
    )


def _cmd_run_external(args: argparse.Namespace, project: Project, db: Database) -> int:
    import shlex

    from operon.workflow import run_external_command

    argv = shlex.split(args.command_line)
    if not argv:
        raise ValidationError("--command must not be empty")
    tool_version = None
    extra_details: dict[str, Any] | None = None
    if args.tool:
        from operon.tools import detect_tool_version_record, get_tool, load_tools_config

        try:
            tool_spec = get_tool(project, args.tool)
        except ValidationError:
            tool_spec = None  # unconfigured name: record the name, no version
        if tool_spec is not None:
            try:
                config = load_tools_config(project)
                tool_version, raw_output = detect_tool_version_record(tool_spec, config)
                extra_details = {"tool_version_raw": raw_output}
            except Exception as exc:  # noqa: BLE001 - version detection is best-effort and must never block the command  # pylint: disable=broad-exception-caught
                # Version detection must never block the actual command.
                print(
                    f"warning: version detection for {args.tool!r} failed: {exc}",
                    file=sys.stderr,
                )
    run_id = new_run_id()
    print(
        f"run {run_id}: logs {project.logs_root / (run_id + '.stdout.log')} / "
        f"{project.logs_root / (run_id + '.stderr.log')}; "
        f"watch: operon workflow show {run_id} --follow",
        flush=True,
    )
    record = run_external_command(
        db,
        project,
        argv,
        step=args.step,
        entity_type=args.entity_type,
        entity_id=args.entity_id,
        parameter_set=args.parameter_set,
        expected_outputs=args.expected_output,
        cwd=args.cwd,
        timeout=args.timeout,
        tool=args.tool,
        tool_version=tool_version,
        backend=args.backend,
        threads=args.threads,
        inputs=args.inputs,
        extra_details=extra_details,
        run_id=run_id,
        events=args.events,
    )
    print(
        json.dumps(
            {
                k: record.get(k)
                for k in ("run_id", "step", "status", "exit_code", "finished_at")
            },
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_tools(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.tools._presets import add_preset, plan_preset_import

    result = (
        plan_preset_import(project, args.file)
        if args.dry_run
        else add_preset(project, db, args.file)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_tools_check(project: Project) -> int:
    from operon.tools import print_tools_table

    table, all_ok = print_tools_table(project)
    print(table)
    return 0 if all_ok else 1


def _parse_runtime_parameters(items: list[str]) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValidationError(f"--param must use NAME=VALUE syntax, got {item!r}")
        name, value = item.split("=", 1)
        name = name.strip()
        if not name or not value:
            raise ValidationError(
                f"--param must use non-empty NAME=VALUE syntax, got {item!r}"
            )
        if name in parameters:
            raise ValidationError(f"duplicate --param {name!r}")
        parameters[name] = value
    return parameters


def _cmd_analyze(args: argparse.Namespace, project: Project, db: Database) -> int:
    if args.detach:
        from operon.tools import get_recipe
        from operon.tools._detach import spawn_detached_analysis

        if args.dry_run:
            raise ValidationError("--detach cannot be combined with --dry-run")
        get_recipe(project, args.analysis)  # fail fast on an unknown recipe
        pid, log_path = spawn_detached_analysis(
            project,
            analysis=args.analysis,
            entity_type=args.entity_type,
            entity_id=args.entity_id,
            limit=args.limit,
            threads=args.threads,
            backend=args.backend,
            force=args.force,
            keep_partial=args.keep_partial,
            events=args.events,
            parameters=_parse_runtime_parameters(args.param),
        )
        print(f"detached analyze: pid {pid}")
        print(f"console log: {log_path}")
        print(f"watch: operon inspect workflow list --step analysis:{args.analysis}")
        print(f"stop: kill {pid}")
        return 0
    from operon.tools import run_analysis

    phase_labels = {
        "start": "running",
        "completed": "done",
        "adopted": "done",
        "error": "failed",
        "failed": "failed",
    }

    def progress(index: int, total: int, file_id: str, phase: str) -> None:
        print(
            f"[{index}/{total}] {file_id}: {phase_labels.get(phase, phase)}", flush=True
        )

    results = run_analysis(
        project,
        db,
        args.analysis,
        entity_type=args.entity_type,
        entity_id=args.entity_id,
        dry_run=args.dry_run,
        force=args.force,
        limit=args.limit,
        threads=args.threads,
        backend=args.backend,
        keep_partial=args.keep_partial,
        runtime_parameters=_parse_runtime_parameters(args.param),
        progress_callback=None if args.dry_run else progress,
        events=args.events,
    )
    headers = [
        "file_id",
        "entity",
        "analysis",
        "status",
        "tool_version",
        "output",
        "error",
    ]
    rows = []
    errors = 0
    planned = 0
    for r in results:
        if r.get("status") in {"error", "failed"}:
            errors += 1
        if r.get("status") == "planned":
            planned += 1
        rows.append(
            [
                r.get("file_id", ""),
                f"{r.get('entity_type', '')} {r.get('entity_id', '')}",
                r.get("analysis", ""),
                r.get("status", ""),
                r.get("tool_version", ""),
                r.get("output", ""),
                r.get("error", ""),
            ]
        )
    print(format_table(headers, rows))
    if args.dry_run:
        print(
            f"dry-run: {len(results)} planned job(s), {planned} job(s) left. no command executed"
        )
    else:
        print(
            f"analysis {args.analysis}: {len(results) - errors}/{len(results)} succeeded"
        )
    return 1 if errors else 0


def _cmd_environments(args: argparse.Namespace, db: Database) -> int:
    from operon.environment import environment_summary
    from operon.environment_capture import export_conda

    if args.environments_command == "list":
        rows = db.conn.execute(
            "SELECT environment_id, document, created_at FROM execution_environments "
            "ORDER BY created_at, environment_id"
        ).fetchall()

        def _summary(row: Any) -> str:
            try:
                return environment_summary(json.loads(row["document"])) or "-"
            except json.JSONDecodeError:
                return "-"

        print(
            format_table(
                ["environment_id", "created_at", "summary"],
                (
                    [row["environment_id"], row["created_at"], _summary(row)]
                    for row in rows
                ),
            )
        )
        return 0
    row = db.conn.execute(
        "SELECT document FROM execution_environments WHERE environment_id=?",
        (args.environment_id,),
    ).fetchone()
    if row is None:
        raise ValidationError(f"unknown environment: {args.environment_id}")
    document = json.loads(row["document"])
    if args.environments_command == "show":
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(export_conda(document, args.format), end="")
    return 0


def _cmd_recipes(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.tools import list_analyses

    if args.recipes_command == "list":
        recipes = list_analyses(project)
        print(
            format_table(
                ["name", "version", "tool", "entity_type", "file_role", "format"],
                (
                    [
                        recipe.name,
                        recipe.version,
                        recipe.tool_name,
                        recipe.entity_type or "*",
                        recipe.file_role or f"{recipe.file_role_prefix}*",
                        recipe.fmt,
                    ]
                    for recipe in recipes
                ),
            )
        )
        return 0
    if args.recipes_command == "history":
        rows = db.conn.execute(
            "SELECT s.recipe_snapshot_id, s.recipe_version, s.recipe_sha256, s.recorded_at, "
            "(SELECT COUNT(*) FROM analysis_jobs j WHERE j.recipe_snapshot_id = s.recipe_snapshot_id) "
            "AS jobs FROM recipe_snapshots s WHERE s.recipe_name=? ORDER BY s.recipe_snapshot_id",
            (args.name,),
        ).fetchall()
        if not rows:
            print(f"no snapshots recorded for recipe {args.name!r}")
            return 0
        print(
            format_table(
                ["snapshot_id", "version", "sha256", "recorded_at", "jobs"],
                (
                    [
                        row["recipe_snapshot_id"],
                        row["recipe_version"],
                        str(row["recipe_sha256"])[:12],
                        row["recorded_at"],
                        row["jobs"],
                    ]
                    for row in rows
                ),
            )
        )
        return 0
    if args.recipes_command == "show":
        if args.snapshot_id is not None:
            row = db.conn.execute(
                "SELECT recipe_document FROM recipe_snapshots "
                "WHERE recipe_name=? AND recipe_snapshot_id=?",
                (args.name, args.snapshot_id),
            ).fetchone()
        else:
            row = db.conn.execute(
                "SELECT recipe_document FROM recipe_snapshots WHERE recipe_name=? "
                "ORDER BY recipe_snapshot_id DESC LIMIT 1",
                (args.name,),
            ).fetchone()
        if row is None:
            detail = (
                f" with snapshot id {args.snapshot_id}"
                if args.snapshot_id is not None
                else ""
            )
            raise ValidationError(
                f"no snapshot recorded for recipe {args.name!r}{detail}"
            )
        _print_snapshot_document(str(row["recipe_document"]))
        return 0
    raise ValidationError(f"unknown recipes command {args.recipes_command!r}")
