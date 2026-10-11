"""Project administration commands: init, status, doctor, query, and friends."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from operon.cli._common import _nonnegative_int
from operon.config import Project, load_project, resolve_actor
from operon.database import Database
from operon.errors import OperonError, ValidationError
from operon.utils import format_table
from operon.workflow import set_state


def add_init_parser(sub):
    p = sub.add_parser("init", help="initialize a new Operon project")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--project-id", default="PRJ_000001")
    p.add_argument("--name", default="")


def add_init_demo_parser(sub):
    p = sub.add_parser(
        "init-demo",
        help="initialize a project with synthetic assemblies/annotations/reads and run the pipeline",
    )
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--project-id", default="PRJ_DEMO_001")


def add_status_parser(sub):
    p = sub.add_parser("status", help="show entity states")
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")
    p.add_argument("--include-retired", action="store_true")


def add_doctor_parser(sub):
    p = sub.add_parser(
        "doctor",
        help="report project-health attention items (read-only; exit code 0 = "
        "nothing needs attention, 1 = some items need attention, 2 = the check "
        "could not complete)",
    )
    p.add_argument(
        "--format",
        dest="fmt",
        choices=["text", "json"],
        default="text",
        help="output format (default: the human-readable report)",
    )
    p.add_argument(
        "--limit",
        type=_nonnegative_int,
        default=0,
        help="cap each source list (runs, decisions, files) at N items "
        "(default: 0 = every item; the summary always counts everything)",
    )


def add_schema_parser(sub):
    p = sub.add_parser("schema", help="show schema path or dump it")
    p.add_argument("--dump", action="store_true")


def add_migrate_parser(sub):
    sub.add_parser(
        "migrate",
        help="apply additive database schema migrations and report integrity",
    )


def add_next_id_parser(sub):
    p = sub.add_parser(
        "next-id", help="allocate the next stable internal ID for an entity type"
    )
    p.add_argument(
        "entity_type",
        choices=["organism", "sample", "run", "assembly", "annotation", "file"],
    )


def add_query_parser(sub):
    p = sub.add_parser(
        "query", help="run arbitrary read-only SQL against the file database"
    )
    p.add_argument("sql")


def add_set_state_parser(sub):
    p = sub.add_parser(
        "set-state",
        help="manually set workflow state (audited; use --force for non-standard transition)",
    )
    p.add_argument("--entity-type", required=True)
    p.add_argument("--entity-id", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--message")
    p.add_argument("--force", action="store_true")


def add_tui_parser(sub):
    sub.add_parser(
        "tui",
        help="open the terminal UI",
    )


def _cmd_init(args: argparse.Namespace) -> int:
    project = Project.init(args.path, project_id=args.project_id, name=args.name)
    print(f"initialized Operon project {project.project_id} at {project.root}")
    print("next steps:")
    print("  1. operon import dataset")
    print("  2. operon import table --table samples --template samples.xlsx")
    print("  3. operon report metadata")
    return 0


def _cmd_init_demo(args: argparse.Namespace) -> int:
    from operon.demo import init_demo

    result = init_demo(Path(args.path), project_id=args.project_id)
    print(f"demo project ready at {result.root}")
    print(
        "run `operon --project . status` or `operon --project . report qc` to inspect it"
    )
    return 0


def _cmd_status(args: argparse.Namespace, db: Database) -> int:
    fragments = [
        "SELECT entity_type, entity_id, state, message, updated_at FROM entity_state WHERE entity_type != 'database'"
    ]
    params: list[Any] = []
    if not getattr(args, "include_retired", False) and db.lifecycle_schema_available():
        fragments.append(
            "AND NOT EXISTS (SELECT 1 FROM effective_retired_entities r "
            "WHERE r.entity_type=entity_state.entity_type "
            "AND r.entity_id=entity_state.entity_id)"
        )
    if args.entity_type:
        fragments.append("AND entity_type=?")
        params.append(args.entity_type)
    if args.entity_id:
        fragments.append("AND entity_id=?")
        params.append(args.entity_id)
    fragments.append("ORDER BY entity_type, entity_id")
    rows = db.conn.execute(" ".join(fragments), params).fetchall()
    if not rows:
        print("no entity states recorded yet")
    else:
        print(
            format_table(
                ["entity_type", "entity_id", "state", "message", "updated_at"],
                (list(row) for row in rows),
            )
        )
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Report the project-health attention items (``operon doctor``).

    Read-only by construction: the shared aggregation in :mod:`operon.health`
    opens its own short-lived read-only connection, so no writable database is
    ever opened and no migration can run.  Exit status is part of the
    contract: ``0`` = nothing needs attention, ``1`` = at least one item needs
    attention, ``2`` = the check itself could not complete (the project or its
    database could not be read) — a failed check never reports as healthy.
    """
    from operon import health as health_module

    try:
        project = load_project(args.project)
        report = health_module.collect_attention_items(project, limit=args.limit)
    except OperonError:
        raise
    # A failed check must never look like a clean bill of health: anything
    # unexpected becomes an error (exit code 2), never an empty report.
    except Exception as exc:
        raise OperonError(f"doctor could not inspect the project: {exc}") from exc
    if args.fmt == "json":
        print(
            json.dumps(
                health_module.attention_payload(report),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(health_module.render_attention_text(report))
    return 0 if report.total == 0 else 1


def _cmd_schema(args: argparse.Namespace, project: Project) -> int:
    if args.dump:
        print(project.schema_path.read_text(encoding="utf-8"))
    else:
        print(project.schema_path)
    return 0


def _cmd_migrate(db: Database) -> int:
    from operon.database import SCHEMA_VERSION

    integrity = str(db.query("PRAGMA integrity_check")[0][0])
    foreign_key_violations = len(db.query("PRAGMA foreign_key_check"))
    migrations = [
        dict(row)
        for row in db.query(
            "SELECT migration_id, migration_sha256, applied_at, workflow_run_id "
            "FROM schema_migrations ORDER BY applied_at, migration_id"
        )
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
        "migrations": migrations,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if integrity == "ok" and foreign_key_violations == 0 else 1


def _cmd_next_id(args: argparse.Namespace, db: Database) -> int:
    print(db.next_id(args.entity_type))
    return 0


def _cmd_query(args: argparse.Namespace, db: Database) -> int:
    try:
        rows = db.readonly_query(args.sql)
    except Exception as exc:
        raise ValidationError(f"query failed: {exc}") from exc
    if rows:
        print(format_table(list(rows[0].keys()), (list(row) for row in rows)))
    else:
        print("(no rows)")
    return 0


def _cmd_set_state(args: argparse.Namespace, db: Database) -> int:
    db.require_active_entity(args.entity_type, args.entity_id)
    set_state(
        db,
        args.entity_type,
        args.entity_id,
        args.state,
        message=args.message,
        force=args.force,
        actor=resolve_actor(),
    )
    print(f"{args.entity_type} {args.entity_id} -> {args.state.upper()}")
    return 0


def _cmd_tui(args: argparse.Namespace) -> int:
    """Launch the read-only TUI without opening a writable database.

    The app manages its own short-lived database connections.  ``textual``
    is imported lazily so non-TUI commands do not initialize the UI stack.
    """
    try:
        from operon.tui.app import OperonApp
    except ModuleNotFoundError:
        print(
            "error: the OperonDBS installation is missing the required "
            "'textual' package; reinstall OperonDBS",
            file=sys.stderr,
        )
        return 2
    project = load_project(args.project)
    OperonApp(project).run()
    return 0
