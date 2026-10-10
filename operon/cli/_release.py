"""Release, export, and adopt commands."""

from __future__ import annotations

import argparse
import json

from operon.config import Project, resolve_actor
from operon.database import Database
from operon.errors import ValidationError
from operon.release import create_release


def add_release_parser(sub):
    p = sub.add_parser("release", help="create an immutable dataset release snapshot")
    p.add_argument("--version", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument(
        "--link",
        choices=["hardlink", "copy"],
        default="copy",
        help="release file storage mode (default: copy)",
    )
    p.add_argument(
        "--copy-files", action="store_true", help="compatibility alias for --link copy"
    )


def add_export_parser(sub):
    p = sub.add_parser(
        "export",
        help="materialize a filtered subset of manifest files with manifest/QC/provenance sidecars",
    )
    p.add_argument(
        "--output",
        required=True,
        help="output directory (must not exist or must be empty; never overwritten)",
    )
    p.add_argument("--entity-type")
    p.add_argument("--entity-id", action="append", default=[])
    p.add_argument("--file-id", action="append", default=[])
    p.add_argument("--file-role")
    p.add_argument("--format", dest="fmt")
    p.add_argument("--state", help="only entities currently in this workflow state")
    p.add_argument(
        "--decision",
        help="only entities with this effective decision (requires --profile)",
    )
    p.add_argument("--profile", help="QC profile used together with --decision")
    p.add_argument(
        "--link",
        choices=["copy", "hardlink", "symlink"],
        default="copy",
        help="export file storage mode (default: copy)",
    )
    p.add_argument(
        "--no-qc", action="store_true", help="skip the qc.tsv metrics snapshot"
    )


def add_adopt_parser(sub):
    p = sub.add_parser(
        "adopt",
        help="register externally produced derived artifacts with lineage "
        "(complements export: workflow outputs re-enter the manifest)",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--file", dest="file", help="single derived artifact path to adopt"
    )
    mode.add_argument(
        "--from-manifest",
        dest="manifest",
        help="batch manifest: JSON list of items, or TSV with header "
        "path/entity_type/entity_id/role/format/compression/derived_from "
        "(derived_from is comma-separated; workflow_run_id column optional)",
    )
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")
    p.add_argument(
        "--role", help="derived file role, freely defined by the producing workflow"
    )
    p.add_argument("--format", dest="fmt")
    p.add_argument("--compression")
    p.add_argument(
        "--derived-from",
        action="append",
        default=[],
        metavar="FILE_ID",
        help="registered input file_id this artifact was derived from; repeatable",
    )
    p.add_argument("--workflow-run-id", help="workflow run that produced the artifact")
    p.add_argument(
        "--actor", help="recorded in the adopt workflow run (default: $USER or 'adopt')"
    )


def _cmd_release(args: argparse.Namespace, project: Project, db: Database) -> int:
    link_kind = "copy" if args.copy_files else args.link
    result = create_release(
        db, project, args.version, args.profile, link_kind=link_kind
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_export(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.export import export_files

    summary = export_files(
        db,
        project,
        output_dir=args.output,
        entity_type=args.entity_type,
        entity_ids=args.entity_id,
        file_ids=args.file_id,
        file_role=args.file_role,
        fmt=args.fmt,
        state=args.state,
        decision=args.decision,
        profile=args.profile,
        link_kind=args.link,
        include_qc=not args.no_qc,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _cmd_adopt(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.lineage import adopt_files, load_adopt_manifest

    if args.manifest:
        items = load_adopt_manifest(args.manifest)
    else:
        missing = [
            flag
            for flag, value in (
                ("--entity-type", args.entity_type),
                ("--entity-id", args.entity_id),
                ("--role", args.role),
            )
            if not value
        ]
        if missing:
            raise ValidationError(f"single-file adopt requires {', '.join(missing)}")
        if not args.derived_from:
            raise ValidationError(
                "single-file adopt requires at least one --derived-from FILE_ID"
            )
        items = [
            {
                "path": args.file,
                "entity_type": args.entity_type,
                "entity_id": args.entity_id,
                "role": args.role,
                "format": args.fmt,
                "compression": args.compression,
                "derived_from": args.derived_from,
                "workflow_run_id": args.workflow_run_id,
            }
        ]
    actor = resolve_actor(args.actor) or "adopt"
    results = adopt_files(db, project, items=items, actor=actor)
    print(
        json.dumps(
            {
                "registered": len(results),
                "file_ids": [result["file_id"] for result in results],
                "items": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
