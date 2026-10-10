"""NCBI Datasets adapter and legacy reconciliation commands."""

from __future__ import annotations

import argparse
import json
import sys

from operon.config import Project, resolve_ncbi_email
from operon.database import Database
from operon.secrets import resolve_secret


def add_ncbi_datasets_parser(sub):
    p = sub.add_parser(
        "ncbi-datasets",
        help="offline-first import/download adapter for NCBI Datasets genome packages",
    )
    p.add_argument(
        "--input",
        dest="inputs",
        action="append",
        default=[],
        metavar="PATH",
        help="existing assembly_data_report JSON/JSONL, Datasets ZIP, or unpacked directory (repeatable)",
    )
    p.add_argument(
        "--accession",
        action="append",
        default=[],
        metavar="GCF_OR_GCA",
        help="download an assembly through the NCBI Datasets API, then import and archive it (repeatable)",
    )
    p.add_argument(
        "--accession-file",
        metavar="FILE",
        help="text file containing one GCF/GCA accession per line",
    )
    p.add_argument(
        "--include",
        action="append",
        choices=["genome", "gff3", "protein", "cds", "sequence-report"],
        help="downloaded package content; repeatable (default: all supported types)",
    )
    p.add_argument(
        "--no-archive-files",
        action="store_true",
        help="import metadata but do not ingest sequence/annotation files found in packages",
    )
    p.add_argument(
        "--standardize",
        action="store_true",
        help="also create standardized/ copies for automatically archived files",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="parse, normalize and show the plan without changing the project",
    )
    p.add_argument(
        "--no-preserve-source",
        action="store_true",
        help="do not preserve input reports/downloaded ZIPs under raw/metadata/ncbi_datasets",
    )
    p.add_argument("--email", help="NCBI contact email (or set NCBI_EMAIL)")
    p.add_argument("--api-key", help="NCBI API key (or set NCBI_API_KEY)")
    p.add_argument(
        "--timeout", type=float, default=300.0, help="download read timeout in seconds"
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="accessions per Datasets request (default: 10; max: 100)",
    )
    p.add_argument(
        "--download-workers",
        type=int,
        default=3,
        help="parallel download workers (default: 3; max: 10)",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=4,
        help="retries per batch for SSL/transient failures (default: 4; max: 10)",
    )
    p.add_argument(
        "--retry-backoff",
        type=float,
        default=1.0,
        help="initial retry backoff in seconds, doubled each attempt (default: 1.0)",
    )
    p.add_argument(
        "--resume-run",
        metavar="WF_ID",
        help="link this attempt to a previous failed/interrupted NCBI import; request must match",
    )
    p.add_argument(
        "--plan-only",
        action="store_true",
        help="show missing-include download groups without downloading or writing workflow rows",
    )


def add_ncbi_reconcile_parser(sub):
    p = sub.add_parser(
        "ncbi-reconcile",
        help="preview or apply an audited repair of legacy NCBI adapter duplicates and paired-accession drift",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="apply the freshly computed plan; default is database-only dry-run",
    )
    p.add_argument("--actor", help="actor recorded in changes (default: current user)")


def _cmd_ncbi_datasets(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.adapters.ncbi_datasets import (
        DEFAULT_INCLUDES,
        run_ncbi_datasets_adapter,
    )
    from operon.shutdown import graceful_shutdown

    with graceful_shutdown():
        result = run_ncbi_datasets_adapter(
            db,
            project,
            inputs=args.inputs,
            accessions=args.accession,
            accession_file=args.accession_file,
            includes=args.include or DEFAULT_INCLUDES,
            archive_files=not args.no_archive_files,
            standardize=args.standardize,
            dry_run=args.dry_run,
            preserve_sources=not args.no_preserve_source,
            email=resolve_ncbi_email(args.email),
            api_key=resolve_secret("ncbi.api_key", args.api_key),
            timeout=args.timeout,
            batch_size=args.batch_size,
            download_workers=args.download_workers,
            max_retries=args.retries,
            retry_backoff=args.retry_backoff,
            resume_run_id=args.resume_run,
            plan_only=args.plan_only,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_ncbi_reconcile(
    args: argparse.Namespace, project: Project, db: Database
) -> int:
    from operon.ncbi_reconcile import (
        apply_ncbi_reconciliation,
        plan_ncbi_reconciliation,
    )

    result = (
        apply_ncbi_reconciliation(db, project, actor=args.actor)
        if args.apply
        else plan_ncbi_reconciliation(db)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if not args.apply:
        print(
            "dry-run: no business row or file was changed; rerun with --apply after review",
            file=sys.stderr,
        )
    return 0
