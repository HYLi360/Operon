#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Manual metadata commands: record creation and dataset/table import."""

from __future__ import annotations

import argparse
import json
import sys

from operon.cli._common import MANUAL_METADATA_ENTITIES
from operon.config import Project, resolve_actor
from operon.database import Database
from operon.errors import ValidationError
from operon.schema import (
    MetadataRecordError,
    Schema,
    add_accession_record,
    add_metadata_record,
)
from operon.table_import import (
    IMPORTABLE_TABLES,
    apply_table_import,
    preview_table_import,
    write_table_template,
)
from operon.utils import format_table, parse_key_values


def add_import_parser(sub):
    p = sub.add_parser(
        "import",
        help="import a dataset interactively or load one controlled metadata table",
    )
    import_sub = p.add_subparsers(dest="import_kind", required=True)
    import_sub.add_parser(
        "dataset", help="launch the English interactive dataset-import wizard"
    )
    ip = import_sub.add_parser(
        "table", help="generate or import a CSV/XLSX metadata table"
    )
    ip.add_argument("--table", required=True, choices=IMPORTABLE_TABLES)
    source = ip.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--template", metavar="OUTPUT", help="write an empty .csv or .xlsx template"
    )
    source.add_argument(
        "--file",
        metavar="INPUT",
        help="preview and import an existing .csv or .xlsx file",
    )
    ip.add_argument(
        "--on-conflict",
        choices=["error", "skip", "update"],
        help="how to handle existing rows after preview",
    )
    ip.add_argument(
        "--yes",
        action="store_true",
        help="apply the preview without an interactive confirmation",
    )


def add_add_parser(sub):
    p = sub.add_parser("add", help="add one metadata record")
    p.add_argument("entity_type", choices=MANUAL_METADATA_ENTITIES)
    p.add_argument(
        "--id",
        dest="record_id",
        help="explicit internal ID (otherwise next ID is allocated)",
    )
    p.add_argument(
        "--field",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="repeatable field values",
    )


def add_add_accession_parser(sub):
    p = sub.add_parser(
        "add-accession", help="map an external accession to an internal stable ID"
    )
    p.add_argument("--internal-type", required=True, choices=MANUAL_METADATA_ENTITIES)
    p.add_argument("--internal-id", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--accession", required=True)
    p.add_argument("--version", dest="acc_version")
    p.add_argument("--primary", action="store_true")


def _cmd_import(args: argparse.Namespace, project: Project, db: Database) -> int:
    if args.import_kind == "dataset":
        from operon.import_wizard import run_dataset_wizard

        result = run_dataset_wizard(db, project)
        if result is None:
            print("Import cancelled; no project data was changed.")
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.import_kind != "table":
        raise ValidationError(f"unknown import kind {args.import_kind!r}")
    schema = Schema.from_file(project.schema_path)
    if args.template:
        path = write_table_template(schema, args.table, args.template)
        print(f"wrote {args.table} template to {path}")
        return 0
    preview = preview_table_import(db, schema, args.table, args.file)
    print(
        format_table(
            ["key", "action", "changed_fields"],
            (
                [
                    ": ".join(str(value) for value in item["key"]),
                    item["action"],
                    ", ".join(item["differences"]),
                ]
                for item in preview["items"]
            ),
        )
    )
    print(
        f"preview: {preview['insert']} insert, {preview['update']} update, "
        f"{preview['unchanged']} unchanged"
    )
    on_conflict = args.on_conflict
    if preview["update"] and on_conflict is None:
        if args.yes:
            raise ValidationError(
                "--on-conflict is required with --yes when existing rows would change"
            )
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValidationError(
                "existing rows would change; pass --on-conflict error, skip or update"
            )
        import questionary

        on_conflict = questionary.select(
            "Existing rows differ. How should they be handled?",
            choices=[
                questionary.Choice("Cancel", value="cancel"),
                questionary.Choice("Skip existing rows", value="skip"),
                questionary.Choice("Update existing rows (audited)", value="update"),
            ],
        ).ask()
        if on_conflict in {None, "cancel"}:
            print("Import cancelled; no rows were changed.")
            return 0
    on_conflict = on_conflict or "error"
    if not args.yes:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValidationError(
                "table import requires --yes outside an interactive terminal"
            )
        import questionary

        confirmed = questionary.confirm(
            f"Apply this {args.table} import with on-conflict={on_conflict}?",
            default=False,
        ).ask()
        if not confirmed:
            print("Import cancelled; no rows were changed.")
            return 0
    result = apply_table_import(
        db, schema, preview, on_conflict=on_conflict, actor=resolve_actor()
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_add(args: argparse.Namespace, project: Project, db: Database) -> int:
    fields = parse_key_values(args.field)
    try:
        result = add_metadata_record(
            db,
            project,
            args.entity_type,
            fields,
            record_id=args.record_id,
            actor=resolve_actor(),
        )
    except MetadataRecordError as exc:
        # The core collected the unknown-field warnings before refusing the
        # row; keep the historical "warn, then refuse" stderr shape.
        for warning in exc.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        raise
    for warning in result["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"added {result['entity_type']} {result['entity_id']}")
    return 0


def _cmd_add_accession(args: argparse.Namespace, project: Project, db: Database) -> int:
    row = add_accession_record(
        db,
        internal_type=args.internal_type,
        internal_id=args.internal_id,
        namespace=args.namespace,
        accession=args.accession,
        version=args.acc_version,
        primary=args.primary,
        actor=resolve_actor(),
    )
    print(
        f"mapped {row['namespace']}:{row['accession']} -> "
        f"{row['internal_type']} {row['internal_id']}"
    )
    return 0
