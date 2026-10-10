"""Sequence-level commands: domain extraction, selection, classify, fanout."""

from __future__ import annotations

import argparse
import shlex
import sys

from operon.cli._common import _nonnegative_int, _positive_int
from operon.config import Project, resolve_actor
from operon.database import Database
from operon.utils import format_table


def add_extract_domains_parser(sub):
    p = sub.add_parser(
        "extract-domains",
        help="extract alignment query regions (e.g. domains) from a manifest FASTA; "
        "register the result with 'operon adopt'",
    )
    p.add_argument("--file-id", required=True, help="manifest FASTA to extract from")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--analysis",
        help="completed analysis whose stored alignments define the regions",
    )
    source.add_argument(
        "--regions-tsv",
        help="external TSV with columns seqid,start,end (optional subject,evalue)",
    )
    p.add_argument(
        "--flank",
        type=_nonnegative_int,
        default=5,
        help="extend each region by N residues on both sides, truncated at sequence "
        "boundaries (default: 5)",
    )
    p.add_argument(
        "--min-length",
        type=_positive_int,
        default=30,
        help="discard regions shorter than N residues before flanking (default: 30)",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--best-only",
        action="store_true",
        help="keep only the best-evalue region per query, headers keep the seqid (default)",
    )
    mode.add_argument(
        "--all-regions",
        action="store_true",
        help="emit one record per region, headers become <seqid>|region:<start>-<end>",
    )
    p.add_argument(
        "--subject-like",
        metavar="PATTERN",
        help="SQL LIKE pattern matched against subject_id and the hit short_name",
    )
    p.add_argument(
        "--evalue-max",
        type=float,
        metavar="E",
        help="only use alignments with evalue <= E",
    )
    p.add_argument(
        "--out", required=True, help="output FASTA path (written atomically)"
    )
    p.add_argument(
        "--manifest", help="optional TSV manifest of extracted and excluded regions"
    )


def add_select_sequences_parser(sub):
    p = sub.add_parser(
        "select-sequences",
        help="write the subset of a manifest FASTA whose sequences have (or lack) "
        "matching analysis hits; register the result with 'operon adopt'",
    )
    p.add_argument("--file-id", required=True, help="manifest FASTA to subset")
    p.add_argument(
        "--analysis",
        action="append",
        default=[],
        help="completed analysis name; repeatable, names are OR-ed",
    )
    p.add_argument(
        "--subject-like",
        metavar="PATTERN",
        help="SQL LIKE pattern matched against subject_id and the hit short_name",
    )
    p.add_argument(
        "--evalue-max",
        type=float,
        metavar="E",
        help="only count alignments with evalue <= E",
    )
    p.add_argument(
        "--min-span",
        type=_positive_int,
        metavar="N",
        help="only count alignments spanning at least N query residues",
    )
    p.add_argument(
        "--hit-type", help="only count alignments whose hit_type matches exactly"
    )
    selection = p.add_mutually_exclusive_group()
    selection.add_argument(
        "--require-hit",
        action="store_true",
        help="keep sequences with at least one matching hit (default)",
    )
    selection.add_argument(
        "--require-no-hit",
        action="store_true",
        help="keep sequences without any matching hit",
    )
    p.add_argument(
        "--entity-type", help="only count alignments recorded for this entity type"
    )
    p.add_argument("--entity-id", help="only count alignments recorded for this entity")
    p.add_argument(
        "--out", required=True, help="output FASTA path (written atomically)"
    )
    p.add_argument(
        "--manifest",
        help="optional TSV manifest listing every sequence's selection state",
    )


def add_classify_sequences_parser(sub):
    p = sub.add_parser(
        "classify-sequences",
        help="label the sequences of the target files with a versioned "
        "sequence_classification profile and store the labels in the database",
    )
    p.add_argument(
        "--profile",
        required=True,
        help="profile name resolved from config/profiles/<name>.yaml "
        "(kind: sequence_classification)",
    )


def add_fanout_parser(sub):
    p = sub.add_parser(
        "fanout",
        help="materialize per-unit FASTA files from a registered assignment "
        "manifest (unit/seqid TSV) and register them under "
        "analysis/derived/ with lineage",
    )
    p.add_argument(
        "--assignments-file",
        required=True,
        metavar="FILE_ID",
        help="registered manifest file holding the unit/seqid assignment TSV",
    )
    p.add_argument(
        "--source-file",
        action="append",
        required=True,
        metavar="FILE_ID",
        help="registered source sequence file the seqids resolve against; repeatable",
    )
    p.add_argument(
        "--entity-type",
        required=True,
        help="existing active entity that owns the fanned-out unit files",
    )
    p.add_argument("--entity-id", required=True)
    p.add_argument(
        "--role-prefix",
        required=True,
        help="unit files are registered with role <prefix>:<unit>",
    )
    p.add_argument(
        "--unit-column",
        default="unit",
        help="assignment TSV column holding the unit name (default: unit)",
    )
    p.add_argument(
        "--seqid-column",
        default="seqid",
        help="assignment TSV column holding the sequence id (default: seqid)",
    )
    p.add_argument("--parent-run-id", help="workflow run that produced the assignments")
    p.add_argument(
        "--actor",
        help="recorded in the fanout workflow run (default: $USER or 'fanout')",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned units and write nothing (no run row either)",
    )


def _cmd_extract_domains(
    args: argparse.Namespace, project: Project, db: Database
) -> int:
    from operon.sequence_tools import extract_domains

    command_parts = ["operon", "extract-domains", "--file-id", args.file_id]
    if args.analysis:
        command_parts += ["--analysis", args.analysis]
    else:
        command_parts += ["--regions-tsv", args.regions_tsv]
    command_parts += ["--flank", str(args.flank), "--min-length", str(args.min_length)]
    command_parts.append("--all-regions" if args.all_regions else "--best-only")
    if args.subject_like:
        command_parts += ["--subject-like", args.subject_like]
    if args.evalue_max is not None:
        command_parts += ["--evalue-max", str(args.evalue_max)]
    command_parts += ["--out", args.out]
    if args.manifest:
        command_parts += ["--manifest", args.manifest]
    result = extract_domains(
        db,
        project,
        file_id=args.file_id,
        out=args.out,
        command=shlex.join(command_parts),
        analysis=args.analysis,
        regions_tsv=args.regions_tsv,
        flank=args.flank,
        min_length=args.min_length,
        best_only=not args.all_regions,
        subject_like=args.subject_like,
        evalue_max=args.evalue_max,
        manifest=args.manifest,
    )
    print(
        f"extracted {result['extracted']} region record(s), "
        f"excluded {result['excluded']} (see manifest)"
    )
    print(f"output: {result['output']}")
    return 0


def _cmd_select_sequences(
    args: argparse.Namespace, project: Project, db: Database
) -> int:
    from operon.sequence_tools import select_sequences

    command_parts = ["operon", "select-sequences", "--file-id", args.file_id]
    for name in args.analysis:
        command_parts += ["--analysis", name]
    if args.subject_like:
        command_parts += ["--subject-like", args.subject_like]
    if args.evalue_max is not None:
        command_parts += ["--evalue-max", str(args.evalue_max)]
    if args.min_span is not None:
        command_parts += ["--min-span", str(args.min_span)]
    if args.hit_type:
        command_parts += ["--hit-type", args.hit_type]
    command_parts.append("--require-no-hit" if args.require_no_hit else "--require-hit")
    if args.entity_type:
        command_parts += ["--entity-type", args.entity_type]
    if args.entity_id:
        command_parts += ["--entity-id", args.entity_id]
    command_parts += ["--out", args.out]
    if args.manifest:
        command_parts += ["--manifest", args.manifest]
    result = select_sequences(
        db,
        project,
        file_id=args.file_id,
        out=args.out,
        command=shlex.join(command_parts),
        analyses=args.analysis,
        subject_like=args.subject_like,
        evalue_max=args.evalue_max,
        min_span=args.min_span,
        hit_type=args.hit_type,
        require_hit=not args.require_no_hit,
        entity_type=args.entity_type,
        entity_id=args.entity_id,
        manifest=args.manifest,
    )
    print(
        f"selected {result['selected']} of {result['total']} sequence(s), "
        f"excluded {result['excluded']}"
    )
    print(f"output: {result['output']}")
    return 0


def _cmd_classify_sequences(
    args: argparse.Namespace, project: Project, db: Database
) -> int:
    from operon.classify import classify_sequences

    command = shlex.join(["operon", "classify-sequences", "--profile", args.profile])
    result = classify_sequences(db, project, profile_name=args.profile, command=command)
    labeled = result["sequences"] - result["unlabeled"]
    print(
        f"labeled {labeled} of {result['sequences']} sequence(s) "
        f"across {result['files']} file(s) with profile {result['profile']}; "
        f"unlabeled: {result['unlabeled']}"
    )
    if result["label_counts"]:
        print(
            format_table(
                ["label", "sequences"],
                ([label, count] for label, count in result["label_counts"].items()),
            )
        )
    print(
        f"labels written: {result['labels_written']}, "
        f"removed: {result['labels_removed']} (run {result['run_id']})"
    )
    if result["ignored_completed_jobs"]:
        print(
            f"warning: ignored {result['ignored_completed_jobs']} older completed "
            f"analysis job(s); only the latest completed job per analysis and file "
            f"contributes hits",
            file=sys.stderr,
        )
    if result["files_without_sequences"]:
        print(
            f"warning: skipped {result['files_without_sequences']} target file(s) "
            f"with no registered sequences",
            file=sys.stderr,
        )
    return 0


def _cmd_fanout(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.fanout import fanout_units

    actor = resolve_actor(args.actor) or "fanout"
    command_parts = [
        "operon",
        "fanout",
        "--assignments-file",
        args.assignments_file,
        "--entity-type",
        args.entity_type,
        "--entity-id",
        args.entity_id,
        "--role-prefix",
        args.role_prefix,
        "--unit-column",
        args.unit_column,
        "--seqid-column",
        args.seqid_column,
    ]
    for source_file in args.source_file:
        command_parts += ["--source-file", source_file]
    if args.parent_run_id:
        command_parts += ["--parent-run-id", args.parent_run_id]
    result = fanout_units(
        db,
        project,
        assignments_file_id=args.assignments_file,
        source_file_ids=args.source_file,
        entity_type=args.entity_type,
        entity_id=args.entity_id,
        role_prefix=args.role_prefix,
        unit_column=args.unit_column,
        seqid_column=args.seqid_column,
        parent_run_id=args.parent_run_id,
        actor=actor,
        dry_run=args.dry_run,
        command=shlex.join(command_parts),
    )
    if result["dry_run"]:
        print(
            format_table(
                ["unit", "sequences", "role", "status"],
                (
                    [unit["unit"], unit["sequences"], unit["role"], unit["status"]]
                    for unit in result["units"]
                ),
            )
        )
        created = sum(1 for unit in result["units"] if unit["status"] == "would_create")
        print(
            f"dry-run: {len(result['units'])} planned unit(s) "
            f"({created} would_create, {len(result['units']) - created} would_reuse); "
            "nothing was written"
        )
        return 0
    print(
        format_table(
            ["unit", "sequences", "role", "file_id", "status"],
            (
                [
                    unit["unit"],
                    unit["sequences"],
                    unit["role"],
                    unit["file_id"],
                    unit["status"],
                ]
                for unit in result["units"]
            ),
        )
    )
    print(
        f"fanout: {result['created']} created, {result['reused']} reused "
        f"(run {result['run_id']})"
    )
    return 0
