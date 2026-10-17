#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Built-in QC measurement and external QC/hit/event import commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from operon.cli._common import _nonnegative_int, _positive_int
from operon.config import Project
from operon.database import Database
from operon.utils import atomic_write_text


def add_qc_parser(sub):
    p = sub.add_parser(
        "qc",
        help="run built-in file/assembly/annotation/read QC and write long metrics",
    )
    p.add_argument("--file-id")
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")
    p.add_argument("--sample-size", type=_positive_int, default=1000000)
    p.add_argument(
        "--phred-offset",
        choices=["33", "64", "auto"],
        default="33",
        help="FASTQ quality offset (default: 33; auto assumes 33 when ambiguous)",
    )
    p.add_argument(
        "--rehash",
        action="store_true",
        help="ignore the unchanged-file verification cache and recompute every input SHA-256",
    )


def add_import_qc_parser(sub):
    p = sub.add_parser(
        "import-qc",
        help="import external QC metrics (e.g. BUSCO, QUAST, FastQC) from TSV, "
        "or built-in metrics measured remotely via 'qc-measure' from JSON",
    )
    p.add_argument("--file", dest="tsv_file", required=True)


def add_import_hits_parser(sub):
    p = sub.add_parser(
        "import-hits", help="import independent parser evidence for a completed job"
    )
    p.add_argument("--file", required=True)
    p.add_argument("--dry-run", action="store_true")


def add_import_events_parser(sub):
    p = sub.add_parser(
        "import-events", help="import plugin facts and generate adopt drafts"
    )
    p.add_argument("--run", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--out", help="adopt draft destination")
    p.add_argument("--dry-run", action="store_true")


def add_qc_measure_parser(sub):
    p = sub.add_parser(
        "qc-measure",
        help="measure built-in QC metrics for one file without a project "
        "(runs anywhere, e.g. on an HPC node; JSON payload for 'import-qc')",
    )
    p.add_argument("--file", dest="measure_file", required=True, help="file to measure")
    p.add_argument(
        "--format",
        dest="fmt",
        required=True,
        choices=["fasta", "fastq", "gff3", "other"],
        help="file format as recorded in the manifest",
    )
    p.add_argument(
        "--role", required=True, help="file role as recorded in the manifest"
    )
    p.add_argument(
        "--sha256",
        required=True,
        help="manifest SHA-256 of the file (verified before parsing)",
    )
    p.add_argument(
        "--size-bytes",
        type=_nonnegative_int,
        required=True,
        help="manifest size in bytes (verified before parsing)",
    )
    p.add_argument(
        "--file-id", help="manifest file_id, embedded in the payload for import-qc"
    )
    p.add_argument(
        "--assembly-fasta",
        help="related assembly FASTA for gff3 seqid/coordinate metrics",
    )
    p.add_argument(
        "--protein-fasta",
        help="related protein FASTA for gff3 protein cross-check metrics",
    )
    p.add_argument(
        "--paired-read",
        help="sibling FASTQ for the reads_r1/reads_r2 read-count cross-check",
    )
    p.add_argument("--sample-size", type=_positive_int, default=1000000)
    p.add_argument(
        "--phred-offset",
        choices=["33", "64", "auto"],
        default="33",
        help="FASTQ quality offset (default: 33; auto assumes 33 when ambiguous)",
    )
    p.add_argument(
        "--parameter-set",
        default=None,
        help="parameter-set label embedded in the payload (default: builtin_v2)",
    )
    p.add_argument(
        "--out", help="write the JSON payload here (atomically) instead of stdout"
    )


def add_alignment_qc_parser(sub):
    p = sub.add_parser(
        "alignment-qc",
        help="measure per-sequence and per-column QC metrics for an aligned FASTA "
        "without a project (runs anywhere, like 'qc-measure')",
    )
    p.add_argument("--alignment", required=True, help="aligned FASTA file")
    p.add_argument(
        "--outdir",
        required=True,
        help="output directory for sequence_qc.tsv, column_qc.tsv and alignment_qc.json",
    )


def _cmd_qc(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.qc import qc_all

    def progress(index: int, total: int, result: dict[str, Any]) -> None:
        if result.get("skipped"):
            state = f"SKIPPED ({result.get('error')})"
        elif result["ok"]:
            state = "OK"
        else:
            state = f"FAILED ({result.get('error')})"
        print(f"[{index}/{total}] {result['file_id']}: {state}", flush=True)

    results = qc_all(
        db,
        project,
        entity_type=args.entity_type,
        entity_id=args.entity_id,
        file_id=args.file_id,
        sample_size=args.sample_size,
        phred_offset=args.phred_offset,
        force_checksum=args.rehash,
        progress_callback=progress,
    )
    ok = sum(1 for r in results if r["ok"])
    skipped = [r for r in results if r.get("skipped")]
    failed = [r for r in results if not r["ok"] and not r.get("skipped")]
    for r in results:
        print(
            f"{r['file_id']}: {r.get('file_qc_state', 'QC_UNKNOWN')} "
            f"(entity {r.get('entity_qc_state', 'QC_UNKNOWN')})"
        )
        if r.get("skipped"):
            print(f"{r['file_id']}: SKIPPED {r['error']}", file=sys.stderr)
        elif not r["ok"]:
            print(f"{r['file_id']}: FAILED {r['error']}", file=sys.stderr)
        for sibling in r.get("file_statuses", []):
            if sibling["file_id"] != r["file_id"]:
                print(
                    f"  {sibling['file_id']} ({sibling['file_role']}): {sibling['qc_state']}"
                )
    summary = f"QC complete: {ok}/{len(results)} file(s) passed built-in stages"
    if skipped:
        summary += f", {len(skipped)} skipped (REMOTE_ONLY)"
    print(summary)
    entity_failed = any(
        not r.get("skipped") and r.get("entity_qc_state") == "QC_FAILED"
        for r in results
    )
    if failed or entity_failed:
        return 1
    if skipped and args.file_id and len(skipped) == len(results):
        print(
            f"error: requested file {args.file_id} was skipped: {skipped[0]['error']}",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_qc_measure(args: argparse.Namespace) -> int:
    from operon.qc import DEFAULT_PARAMETER_SET, measure_file

    try:
        payload = measure_file(
            args.measure_file,
            file_format=args.fmt,
            file_role=args.role,
            sha256=args.sha256,
            size_bytes=args.size_bytes,
            file_id=args.file_id,
            sample_size=args.sample_size,
            phred_offset=args.phred_offset,
            parameter_set=args.parameter_set or DEFAULT_PARAMETER_SET,
            assembly_fasta=args.assembly_fasta,
            protein_fasta=args.protein_fasta,
            paired_read=args.paired_read,
        )
    except Exception as exc:  # noqa: BLE001 - top-level CLI boundary: any error becomes exit 1  # pylint: disable=broad-exception-caught
        print(f"error: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        try:
            atomic_write_text(args.out, text)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        sys.stdout.write(text)
    return 0


def _cmd_alignment_qc(args: argparse.Namespace) -> int:
    from operon.qc import (
        alignment_qc,
        render_summary_json,
        write_alignment_qc,
    )

    try:
        result = alignment_qc(args.alignment)
        write_alignment_qc(result, args.outdir)
    except Exception as exc:  # noqa: BLE001 - top-level CLI boundary: any error becomes exit 1  # pylint: disable=broad-exception-caught
        print(f"error: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(render_summary_json(result.summary))
    return 0


def _cmd_import_events(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.events import import_events, plan_events_import

    result = (
        plan_events_import(db, project, args.run, args.file, output=args.out)
        if args.dry_run
        else import_events(db, project, args.run, args.file, output=args.out)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_import_hits(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.hits_import import import_hits, plan_hits_import

    result = (
        plan_hits_import(db, args.file)
        if args.dry_run
        else import_hits(db, project, args.file)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_import_qc(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.qc.imports import import_qc

    result = import_qc(db, project, Path(args.tsv_file), started_at=_now_for_cli())
    warning = result.get("warning")
    if warning:
        print(f"warning: {warning}", file=sys.stderr)
    if result["format"] == "json":
        print(
            f"imported {result['metric_count']} built-in QC metric(s) for {result['file_id']}"
        )
    else:
        print(f"imported {result['metric_count']} external QC metric(s)")
    return 0


def _now_for_cli() -> str:
    from operon.utils import now_iso

    return now_iso()
