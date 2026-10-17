#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""QC decision commands: evaluate, curate, profiles, and run-pipeline."""

from __future__ import annotations

import argparse
import sys

from operon.cli._common import _print_snapshot_document
from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.rules import (
    curate_decision,
    curated_evaluation_targets,
    evaluate_all,
    evaluate_entity,
)
from operon.utils import format_table


def add_evaluate_parser(sub):
    p = sub.add_parser(
        "evaluate", help="apply a versioned QC profile and record decisions"
    )
    p.add_argument("--profile")
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")
    p.add_argument(
        "--yes",
        action="store_true",
        help="continue without confirmation when existing curated decisions will be re-evaluated",
    )


def add_curate_parser(sub):
    p = sub.add_parser(
        "curate", help="record an audited human override of an automatic decision"
    )
    p.add_argument("--entity-type", required=True)
    p.add_argument("--entity-id", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--decision", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence")


def add_profiles_parser(sub):
    p = sub.add_parser("profiles", help="inspect recorded QC profile snapshots")
    profiles_sub = p.add_subparsers(dest="profiles_command", required=True)
    pp = profiles_sub.add_parser(
        "history",
        help="show recorded profile snapshot history (all profiles when NAME is omitted)",
    )
    pp.add_argument("name", nargs="?")
    pp = profiles_sub.add_parser(
        "show",
        help="print a recorded profile snapshot as YAML; to restore a version, copy the "
        "output back into config/profiles/ manually (no in-place restore)",
    )
    pp.add_argument("name")
    pp.add_argument(
        "--snapshot-id", type=int, help="snapshot to print (default: latest)"
    )


def add_run_pipeline_parser(sub):
    p = sub.add_parser(
        "run-pipeline", help="ingest -> standardize -> QC -> evaluate for one file"
    )
    p.add_argument("--source", required=True)
    p.add_argument(
        "--entity-type", required=True, choices=["run", "assembly", "annotation"]
    )
    p.add_argument("--entity-id", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--format", dest="fmt")
    p.add_argument("--compression")
    p.add_argument("--source-url")
    p.add_argument(
        "--compress-archive",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="archive plain FASTA/FASTQ sources as deterministic BGZF "
        "(default: the project's storage.compress_archive setting)",
    )
    p.add_argument("--profile")
    p.add_argument(
        "--yes",
        action="store_true",
        help="continue without confirmation when evaluation will reuse curated decisions",
    )


def _cmd_evaluate(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.pipeline import reason_list

    profile_name = args.profile or project.config["qc"]["default_profile"]
    if args.entity_id:
        if not args.entity_type:
            raise ValidationError("--entity-type is required when --entity-id is given")
        row = db.conn.execute(
            "SELECT curated_decision FROM current_decisions "
            "WHERE entity_type=? AND entity_id=? AND profile=?",
            (args.entity_type, args.entity_id, profile_name),
        ).fetchone()
        curated_targets = (
            [(args.entity_type, args.entity_id)]
            if row is not None and row["curated_decision"] is not None
            else []
        )
    elif (project.profiles_dir / f"{profile_name}.yaml").exists():
        curated_targets = curated_evaluation_targets(
            db,
            project,
            profile_name,
            getattr(args, "entity_type", None),
            None,
        )
    else:
        curated_targets = []
    if not _confirm_curated_evaluation(curated_targets, getattr(args, "yes", False)):
        return 0
    if args.entity_id:
        result = evaluate_entity(
            db, project, args.entity_type, args.entity_id, args.profile
        )
        results = [result]
    else:
        results = evaluate_all(db, project, args.profile, args.entity_type)
    print(
        format_table(
            ["entity_type", "entity_id", "profile", "decision", "reasons"],
            (
                [
                    r["entity_type"],
                    r["entity_id"],
                    r["profile"],
                    r["decision"],
                    ", ".join(reason_list(r["reason_codes"])) or "-",
                ]
                for r in results
            ),
        )
    )
    return 0


def _confirm_curated_evaluation(
    targets: list[tuple[str, str]],
    yes: bool,
    *,
    label: str = "evaluation",
) -> bool:
    """Confirm a curated re-evaluation before any command stage mutates data."""
    if not targets or yes:
        return True
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValidationError(
            f"{label} will re-run {len(targets)} entity/entities with curated decisions; "
            "pass --yes to confirm"
        )
    import questionary

    preview = ", ".join(
        f"{entity_type}:{entity_id}" for entity_type, entity_id in targets[:5]
    )
    if len(targets) > 5:
        preview += ", ..."
    confirmed = questionary.confirm(
        f"Re-evaluate {len(targets)} curated entity/entities ({preview})?",
        default=False,
    ).ask()
    if not confirmed:
        print("Evaluation cancelled; no rows were changed.")
    return bool(confirmed)


def _cmd_curate(args: argparse.Namespace, project: Project, db: Database) -> int:
    curate_decision(
        db,
        args.entity_type,
        args.entity_id,
        args.profile,
        args.decision,
        reviewer=args.reviewer,
        reason=args.reason,
        evidence=args.evidence,
    )
    print(
        f"recorded curated decision {args.decision} for {args.entity_type} {args.entity_id}"
    )
    return 0


def _cmd_profiles(args: argparse.Namespace, project: Project, db: Database) -> int:
    if args.profiles_command == "history":
        if not args.name:
            rows = db.conn.execute(
                "SELECT profile_name, COUNT(*) AS snapshots, MAX(recorded_at) AS latest "
                "FROM qc_profiles GROUP BY profile_name ORDER BY profile_name",
            ).fetchall()
            if not rows:
                print("no profile snapshots recorded")
                return 0
            print(
                format_table(
                    ["name", "snapshots", "latest_recorded_at"],
                    (
                        [row["profile_name"], row["snapshots"], row["latest"]]
                        for row in rows
                    ),
                )
            )
            return 0
        rows = db.conn.execute(
            "SELECT s.profile_snapshot_id, s.profile_version, s.profile_sha256, s.recorded_at, "
            "(SELECT COUNT(*) FROM decisions d WHERE d.profile_snapshot_id = s.profile_snapshot_id) "
            "AS decisions FROM qc_profiles s WHERE s.profile_name=? ORDER BY s.profile_snapshot_id",
            (args.name,),
        ).fetchall()
        if not rows:
            print(f"no snapshots recorded for profile {args.name!r}")
            return 0
        print(
            format_table(
                ["snapshot_id", "version", "sha256", "recorded_at", "decisions"],
                (
                    [
                        row["profile_snapshot_id"],
                        row["profile_version"],
                        str(row["profile_sha256"])[:12],
                        row["recorded_at"],
                        row["decisions"],
                    ]
                    for row in rows
                ),
            )
        )
        return 0
    if args.profiles_command == "show":
        if args.snapshot_id is not None:
            row = db.conn.execute(
                "SELECT profile_document FROM qc_profiles "
                "WHERE profile_name=? AND profile_snapshot_id=?",
                (args.name, args.snapshot_id),
            ).fetchone()
        else:
            row = db.conn.execute(
                "SELECT profile_document FROM qc_profiles WHERE profile_name=? "
                "ORDER BY profile_snapshot_id DESC LIMIT 1",
                (args.name,),
            ).fetchone()
        if row is None:
            detail = (
                f" with snapshot id {args.snapshot_id}"
                if args.snapshot_id is not None
                else ""
            )
            raise ValidationError(
                f"no snapshot recorded for profile {args.name!r}{detail}"
            )
        _print_snapshot_document(str(row["profile_document"]))
        return 0
    raise ValidationError(f"unknown profiles command {args.profiles_command!r}")


def _cmd_run_pipeline(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.pipeline import curated_targets, resolve_profile, run_pipeline

    profile_name = resolve_profile(project, args.profile)
    targets = curated_targets(db, args.entity_type, args.entity_id, profile_name)
    if not _confirm_curated_evaluation(
        targets, getattr(args, "yes", False), label="pipeline"
    ):
        return 0
    result = run_pipeline(
        db,
        project,
        source=args.source,
        entity_type=args.entity_type,
        entity_id=args.entity_id,
        role=args.role,
        profile=args.profile,
        fmt=args.fmt,
        compression=args.compression,
        source_url=args.source_url,
        compress_archive=getattr(args, "compress_archive", None),
        progress=print,
    )
    if not result["qc_ok"]:
        print(f"       -> QC FAILED: {result['qc_error']}", file=sys.stderr)
        return 1
    return 0
