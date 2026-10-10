"""Shared argparse types, project opening, and small print helpers."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

from operon import __version__
from operon.config import Project, load_project
from operon.database import SCHEMA_VERSION as DATABASE_SCHEMA_VERSION
from operon.database import Database
from operon.schema import METADATA_SCHEMA_VERSION
from operon.utils import format_table

MANUAL_METADATA_ENTITIES = ["organism", "sample", "run", "assembly", "annotation"]


class ShowOperonVersion(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        sys.stdout.write(
            "Operon the Database System\n"
            f"Main program version:    {__version__}\n"
            f"Database schema version: {DATABASE_SCHEMA_VERSION}\n"
            f"Metadata schema version: {METADATA_SCHEMA_VERSION}\n"
        )
        parser.exit()


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _workflow_time(value: str) -> str:
    """Normalize a CLI ISO-8601 time, interpreting naive values locally."""
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be an ISO-8601 date or timestamp"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat()


def _open_project(args: argparse.Namespace) -> tuple[Project, Database]:
    project = load_project(args.project)
    read_only = (
        (args.command == "tools" and args.dry_run)
        or (args.command in {"import-hits", "import-events"} and args.dry_run)
        or args.command == "query"
        or args.command == "environments"
        or args.command == "workflow"
        or args.command == "show"
        or args.command == "retired"
        or args.command == "status"
        or (
            args.command == "report"
            and args.report_kind in {"qc", "decisions", "analysis", "metadata", "view"}
        )
        or (args.command in {"retire", "restore"} and not args.apply)
        or (args.command == "backup" and args.backup_command == "create")
        or (args.command == "ncbi-datasets" and (args.dry_run or args.plan_only))
        or (args.command == "ncbi-reconcile" and not args.apply)
    )
    db = Database(project.db_path, read_only=read_only)
    return project, db


def _print_table(headers: list[str], rows: list[list[Any]]) -> None:
    print(format_table(headers, rows))


def _print_snapshot_document(document: str) -> None:
    """Print a stored snapshot document as YAML (fall back to the raw text)."""
    import yaml

    try:
        parsed = json.loads(document)
    except json.JSONDecodeError:
        print(document)
        return
    print(yaml.safe_dump(parsed, sort_keys=False, allow_unicode=True).rstrip("\n"))
