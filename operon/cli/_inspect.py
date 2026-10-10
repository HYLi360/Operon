"""Inspection commands: workflow runs, entity graphs, and lifecycle."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import textwrap
from datetime import datetime
from typing import Any

from operon.cli._common import _nonnegative_int, _workflow_time
from operon.config import Project, load_project, resolve_actor
from operon.database import Database
from operon.entity_view import entity_graph, sequence_hits
from operon.errors import EntityNotFoundError, ValidationError
from operon.utils import format_table
from operon.workflow import (
    flush_run_log,
    follow_run_logs,
    get_run,
    list_runs,
    log_run,
    new_run_id,
)


def add_workflow_parser(sub):
    p = sub.add_parser("workflow", help="search and inspect recorded workflow runs")
    workflow_sub = p.add_subparsers(dest="workflow_command", required=True)
    wp = workflow_sub.add_parser(
        "list", help="list workflow runs with optional filters"
    )
    wp.add_argument(
        "--from",
        dest="started_from",
        type=_workflow_time,
        help="inclusive started_at boundary (ISO-8601; local time if no offset)",
    )
    wp.add_argument(
        "--to",
        dest="started_to",
        type=_workflow_time,
        help="exclusive started_at boundary (ISO-8601; local time if no offset)",
    )
    wp.add_argument("--run-id")
    wp.add_argument(
        "--step",
        action="append",
        default=[],
        help="exact step name; repeat to match any of several steps",
    )
    wp.add_argument(
        "--status",
        action="append",
        default=[],
        help="exact status; repeat to match any of several statuses",
    )
    wp.add_argument("--entity-type")
    wp.add_argument("--entity-id")
    wp.add_argument("--parent-run-id")
    wp.add_argument("--resumes-run-id")
    wp.add_argument("--tool")
    wp.add_argument("--executor")
    wp.add_argument(
        "--limit",
        type=_nonnegative_int,
        default=50,
        help="maximum rows (default: 50; 0 means all)",
    )
    wp.add_argument("--offset", type=_nonnegative_int, default=0)
    wp.add_argument("--oldest-first", action="store_true")
    wp.add_argument("--format", choices=["table", "json", "jsonl"], default="table")
    wp = workflow_sub.add_parser("show", help="show one complete workflow run")
    wp.add_argument("run_id")
    wp.add_argument("--format", choices=["text", "json"], default="text")
    wp.add_argument(
        "--follow",
        action="store_true",
        help="stream the run's stdout/stderr logs to the terminal until the run ends",
    )


def add_show_parser(sub):
    p = sub.add_parser("show", help="show a matched entity lineage and its descendants")
    p.add_argument("identifier", help="internal ID, accession, or NAMESPACE:ACCESSION")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.add_argument(
        "--scope",
        choices=["matched", "organism"],
        default="matched",
        help="display only the matched lineage/subtree, or the complete organism graph",
    )
    p.add_argument(
        "--include-superseded",
        action="store_true",
        help="include logically superseded descendant entities and their files",
    )
    p.add_argument(
        "--include-retired",
        action="store_true",
        help="include logically retired descendants and their files",
    )


def add_retire_parser(sub):
    p = sub.add_parser(
        "retire",
        help="preview or apply an audited logical entity retirement",
    )
    p.add_argument("identifier", help="internal ID, accession, or NAMESPACE:ACCESSION")
    p.add_argument(
        "--reason-code",
        required=True,
        choices=[
            "accidental_import",
            "wrong_source",
            "duplicate",
            "withdrawn_upstream",
            "policy_exclusion",
            "metadata_error",
            "other",
        ],
    )
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence")
    p.add_argument("--actor")
    p.add_argument("--apply", action="store_true", help="append the RETIRE event")
    p.add_argument(
        "--yes", action="store_true", help="apply without interactive confirmation"
    )


def add_restore_parser(sub):
    p = sub.add_parser(
        "restore",
        help="preview or reverse a target's direct logical retirement",
    )
    p.add_argument("identifier", help="internal ID, accession, or NAMESPACE:ACCESSION")
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence")
    p.add_argument("--actor")
    p.add_argument("--apply", action="store_true", help="append the RESTORE event")
    p.add_argument(
        "--yes", action="store_true", help="apply without interactive confirmation"
    )


def add_retired_parser(sub):
    p = sub.add_parser("retired", help="list current logical retirements")
    p.add_argument("--direct-only", action="store_true")
    p.add_argument("--json", action="store_true")


def _workflow_json_record(record: dict[str, Any]) -> dict[str, Any]:
    """Expose JSON stored in SQLite as structured output when it is valid."""
    result = dict(record)
    details = result.get("execution_details")
    if isinstance(details, str):
        try:
            result["execution_details"] = json.loads(details)
        except json.JSONDecodeError:
            pass
    return result


def _workflow_duration(record: dict[str, Any]) -> str:
    duration = record.get("duration_seconds")
    if duration is not None:
        return f"{float(duration):.3f}s"
    started = record.get("started_at")
    finished = record.get("finished_at")
    if not started or not finished:
        return "-"
    try:
        seconds = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return "-"
    return f"{max(seconds.total_seconds(), 0.0):.3f}s"


def _compact_workflow_value(value: Any, width: int) -> str:
    text = "-" if value in (None, "") else str(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def _workflow_started_local(value: Any) -> str:
    if not isinstance(value, str):
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return _compact_workflow_value(value, 19)


def _print_workflow_field(label: str, value: Any) -> None:
    text = "-" if value in (None, "") else str(value)
    prefix = f"  {label:<18} "
    available = max(40, shutil.get_terminal_size(fallback=(100, 24)).columns)
    print(
        textwrap.fill(
            text,
            width=available,
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


def _print_workflow_section(title: str, fields: list[tuple[str, Any]]) -> None:
    print(title)
    for label, value in fields:
        _print_workflow_field(label, value)


def _environment_summary_text(db: Database, environment_id: str) -> str | None:
    """Render the stored environment document; missing/corrupt rows yield None."""
    from operon.environment import environment_summary

    row = db.conn.execute(
        "SELECT document FROM execution_environments WHERE environment_id=?",
        (environment_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        summary = environment_summary(json.loads(row["document"]))
    except json.JSONDecodeError:
        return None
    return summary or None


def _cmd_workflow(args: argparse.Namespace, db: Database) -> int:
    if args.workflow_command == "list":
        if (
            args.started_from is not None
            and args.started_to is not None
            and datetime.fromisoformat(args.started_from)
            >= datetime.fromisoformat(args.started_to)
        ):
            raise ValidationError("--from must be earlier than --to")
        records = list_runs(
            db,
            started_from=args.started_from,
            started_to=args.started_to,
            run_id=args.run_id,
            steps=args.step,
            statuses=args.status,
            entity_type=args.entity_type,
            entity_id=args.entity_id,
            parent_run_id=args.parent_run_id,
            resumes_run_id=args.resumes_run_id,
            tool=args.tool,
            executor=args.executor,
            limit=args.limit,
            offset=args.offset,
            oldest_first=args.oldest_first,
        )
        serialized = [_workflow_json_record(record) for record in records]
        if args.format == "json":
            print(json.dumps(serialized, ensure_ascii=False, indent=2, sort_keys=True))
        elif args.format == "jsonl":
            for record in serialized:
                print(json.dumps(record, ensure_ascii=False, sort_keys=True))
        elif not records:
            print("no workflow runs matched")
        else:
            print(
                format_table(
                    ["started_local", "status", "step", "entity", "duration", "run_id"],
                    (
                        [
                            _workflow_started_local(record["started_at"]),
                            _compact_workflow_value(record["status"], 11),
                            _compact_workflow_value(record["step"], 16),
                            _compact_workflow_value(
                                ":".join(
                                    filter(
                                        None,
                                        (
                                            record.get("entity_type"),
                                            record.get("entity_id"),
                                        ),
                                    )
                                ),
                                20,
                            ),
                            _compact_workflow_value(_workflow_duration(record), 10),
                            record["run_id"],
                        ]
                        for record in records
                    ),
                )
            )
        return 0

    if args.workflow_command == "show":
        record = get_run(db, args.run_id)
        if record is None:
            raise ValidationError(f"workflow run does not exist: {args.run_id}")
        if args.follow and args.format == "json":
            raise ValidationError("--follow cannot be combined with --format json")
        serialized = _workflow_json_record(record)
        if args.format == "json":
            print(json.dumps(serialized, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        entity = ":".join(
            filter(None, (record.get("entity_type"), record.get("entity_id")))
        )
        _print_workflow_section(
            "Workflow run",
            [
                ("run_id", record["run_id"]),
                ("status", record["status"]),
                ("step", record["step"]),
                ("entity", entity or None),
                ("parent_run_id", record.get("parent_run_id")),
                ("resumes_run_id", record.get("resumes_run_id")),
            ],
        )
        print()
        _print_workflow_section(
            "Timing and resources",
            [
                ("started_at", record.get("started_at")),
                ("finished_at", record.get("finished_at")),
                ("duration", _workflow_duration(record)),
                ("threads", record.get("threads")),
                ("max_rss_mb", record.get("max_rss_mb")),
                ("avg_rss_mb", record.get("avg_rss_mb")),
                ("cpu_seconds", record.get("cpu_seconds")),
            ],
        )
        print()
        environment_id = record.get("environment_id")
        _print_workflow_section(
            "Execution",
            [
                ("command", record.get("command")),
                ("tool", record.get("tool")),
                ("tool_version", record.get("tool_version")),
                ("parameter_set", record.get("parameter_set")),
                ("executor", record.get("executor")),
                ("scheduler_job_id", record.get("scheduler_job_id")),
                ("exit_code", record.get("exit_code")),
                ("environment_id", environment_id),
                (
                    "environment",
                    _environment_summary_text(db, environment_id)
                    if environment_id
                    else None,
                ),
            ],
        )
        print()
        _print_workflow_section(
            "Artifacts and logs",
            [
                ("input_sha256", record.get("input_sha256")),
                ("output_sha256", record.get("output_sha256")),
                ("log_file", record.get("log_file")),
                ("stdout_file", record.get("stdout_file")),
                ("stderr_file", record.get("stderr_file")),
            ],
        )
        print()
        _print_workflow_section("Outcome", [("error", record.get("error"))])
        print("\nExecution details")
        details = serialized.get("execution_details")
        if isinstance(details, (dict, list)):
            print(json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("-" if details in (None, "") else details)
        if args.follow:
            print()
            return follow_run_logs(
                lambda: get_run(db, args.run_id),
                load_project(args.project).logs_root,
                args.run_id,
            )
        return 0

    raise ValidationError(f"unknown workflow command {args.workflow_command!r}")


def _cmd_show(args: argparse.Namespace, db: Database) -> int:
    try:
        graph = entity_graph(
            db,
            args.identifier,
            scope=getattr(args, "scope", "matched"),
            include_superseded=getattr(args, "include_superseded", False),
            include_retired=getattr(args, "include_retired", False),
        )
    except EntityNotFoundError:
        hits = sequence_hits(db, args.identifier)
        if not hits:
            raise
        if args.json:
            print(
                json.dumps(
                    {"query": args.identifier, "match": "sequence", "sequences": hits},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(f"Matched:  sequence {args.identifier}")
        print(f"\nSequences ({len(hits)})")
        print(
            format_table(
                [
                    "seqid",
                    "length",
                    "entity_type",
                    "entity_id",
                    "file_id",
                    "relative_path",
                ],
                (
                    [
                        row[column]
                        for column in (
                            "seqid",
                            "length",
                            "entity_type",
                            "entity_id",
                            "file_id",
                            "relative_path",
                        )
                    ]
                    for row in hits
                ),
            )
        )
        return 0
    if args.json:
        print(json.dumps(graph, ensure_ascii=False, indent=2))
        return 0
    organism = graph["organism"]
    print(f"Organism: {organism['organism_id']}  {organism['scientific_name']}")
    print(
        f"Matched:  {graph['matched']['entity_type']} {graph['matched']['entity_id']}"
    )
    print(f"Scope:    {graph['scope']}")
    accession_rows = [
        row
        for row in graph["accessions"]
        if row["internal_id"] == organism["organism_id"]
    ]
    if accession_rows:
        print(
            "Accessions: "
            + ", ".join(
                f"{row['namespace']}:{row['accession']}" for row in accession_rows
            )
        )
    sections = [
        (
            "Sources",
            graph["sources"],
            [
                "source_id",
                "source_type",
                "database_name",
                "provider",
                "citation",
                "license_name",
            ],
        ),
        (
            "Samples",
            graph["samples"],
            ["sample_id", "isolate", "strain", "biosample_accession"],
        ),
        (
            "Runs",
            graph["runs"],
            ["run_id", "sample_id", "run_accession", "platform", "instrument_model"],
        ),
        (
            "Assemblies",
            graph["assemblies"],
            [
                "assembly_id",
                "sample_id",
                "assembly_accession",
                "assembly_name",
                "assembly_level",
            ],
        ),
        (
            "Annotations",
            graph["annotations"],
            ["annotation_id", "assembly_id", "annotation_source", "annotation_version"],
        ),
        (
            "Files",
            graph["files"],
            [
                "file_id",
                "entity_type",
                "entity_id",
                "file_role",
                "status",
                "relative_path",
            ],
        ),
    ]
    if graph["supersessions"]:
        sections.append(
            (
                "Supersessions",
                graph["supersessions"],
                [
                    "object_type",
                    "object_id",
                    "superseded_by_type",
                    "superseded_by_id",
                    "reason",
                    "superseded_at",
                ],
            )
        )
    if graph["retirements"]:
        sections.append(
            (
                "Retirements",
                graph["retirements"],
                [
                    "entity_type",
                    "entity_id",
                    "retired_by_type",
                    "retired_by_id",
                    "reason_code",
                    "reason",
                    "actor",
                    "retired_at",
                ],
            )
        )
    for title, rows, columns in sections:
        print(f"\n{title} ({len(rows)})")
        print(
            format_table(
                columns, ([row.get(column) for column in columns] for row in rows)
            )
            if rows
            else "(none)"
        )
    return 0


def _cmd_lifecycle(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.lifecycle import apply_lifecycle_event, lifecycle_plan
    from operon.utils import now_iso

    action = "RETIRE" if args.command == "retire" else "RESTORE"
    plan = lifecycle_plan(db, args.identifier, action=action)
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not plan["will_change"]:
        if plan["blocker"]:
            raise ValidationError(plan["blocker"])
        print(json.dumps({**plan, "applied": False}, ensure_ascii=False, indent=2))
        return 0
    if not args.yes:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValidationError(
                f"{args.command} --apply requires --yes outside an interactive terminal"
            )
        import questionary

        target = plan["target"]
        confirmed = questionary.confirm(
            f"Apply {action} to {target['entity_type']} {target['entity_id']}? ",
            default=False,
        ).ask()
        if not confirmed:
            print(f"{args.command.capitalize()} cancelled; no rows were changed.")
            return 0
    actor = resolve_actor(args.actor) or ""
    if not actor:
        raise ValidationError(
            "--actor is required when no user identity is available (--actor, "
            "OPERON_ACTOR, USER, or identity.actor in the user configuration)"
        )
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
            reason=args.reason,
            reason_code=getattr(args, "reason_code", None),
            evidence=args.evidence,
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
                "command": f"operon {args.command} {args.identifier}",
                "tool": "operon",
                "parameter_set": json.dumps(
                    {
                        "reason_code": getattr(args, "reason_code", "manual_restore"),
                        "reason": args.reason,
                        "actor": actor,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "execution_details": json.dumps(
                    {
                        "entity_counts": plan["entity_counts"],
                        "reference_counts": plan["reference_counts"],
                        "physical_changes": plan["physical_changes"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
            jsonl_buffer=jsonl_buffer,
        )
    flush_run_log(project, jsonl_buffer)
    refreshed = lifecycle_plan(db, args.identifier, action=action)
    print(
        json.dumps(
            {
                "applied": True,
                "action": action,
                "target": target,
                "event": result["event"],
                "effectively_retired": db.is_entity_retired(
                    target["entity_type"], target["entity_id"]
                ),
                "plan": refreshed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _cmd_retired(args: argparse.Namespace, db: Database) -> int:
    from operon.lifecycle import list_retired_entities

    rows = list_retired_entities(db, direct_only=args.direct_only)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif not rows:
        print("no retired entities")
    else:
        columns = [
            "entity_type",
            "entity_id",
            "retired_by_type",
            "retired_by_id",
            "reason_code",
            "reason",
            "actor",
            "retired_at",
        ]
        print(
            format_table(
                columns,
                ([row.get(column) for column in columns] for row in rows),
            )
        )
    return 0
