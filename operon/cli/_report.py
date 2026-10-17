#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Report and taxonomy commands."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from operon.config import Project
from operon.coverage import report_coverage
from operon.database import Database
from operon.errors import ValidationError
from operon.reports import (
    export_metadata_report,
    export_qc_tsv,
    print_decisions,
    print_qc_table,
    render_report_rows,
)
from operon.taxonomy import (
    compile_reference_set,
    import_ncbi_taxonomy,
    list_reference_sets,
    list_taxonomy_snapshots,
)
from operon.utils import atomic_write_text, format_table


def add_taxonomy_parser(sub):
    p = sub.add_parser(
        "taxonomy", help="manage immutable NCBI Taxonomy snapshots and denominators"
    )
    taxonomy_sub = p.add_subparsers(dest="taxonomy_command", required=True)
    tp = taxonomy_sub.add_parser(
        "import", help="archive and import an NCBI Datasets taxonomy package"
    )
    tp.add_argument(
        "--input",
        required=True,
        help="taxonomy_report.jsonl/Datasets package or official NCBI taxdump archive",
    )
    tp.add_argument(
        "--version", required=True, help="explicit immutable taxonomy version label"
    )
    taxonomy_sub.add_parser("list", help="list imported NCBI Taxonomy snapshots")
    tp = taxonomy_sub.add_parser(
        "compile", help="compile a coverage profile into a frozen denominator"
    )
    tp.add_argument("--profile", required=True)
    tp.add_argument("--taxonomy-version", required=True)
    taxonomy_sub.add_parser(
        "reference-sets", help="list compiled taxonomy reference sets"
    )


def add_report_parser(sub):
    p = sub.add_parser("report", help="render queryable project and release reports")
    report_sub = p.add_subparsers(dest="report_kind", required=True)
    rp = report_sub.add_parser("view", help="export a read-only visualization bundle")
    rp.add_argument("--out", required=True)
    rp = report_sub.add_parser("qc", help="show or export long-form QC results")
    rp.add_argument("--entity-type")
    rp.add_argument("--entity-id")
    rp.add_argument("--wide", action="store_true", help="pivot QC metrics by entity")
    rp.add_argument("--format", choices=["text", "tsv", "json"], default="text")
    rp.add_argument("--out", help="write the QC report atomically")
    rp.add_argument(
        "--export", action="store_true", help="write qc/aggregate TSV files"
    )
    rp.add_argument("--include-retired", action="store_true")
    rp = report_sub.add_parser("decisions", help="show current QC decisions")
    rp.add_argument("--profile")
    rp.add_argument("--include-retired", action="store_true")
    rp = report_sub.add_parser(
        "analysis", help="show synchronized analysis summaries or alignment hits"
    )
    rp.add_argument("--analysis")
    rp.add_argument("--entity-type")
    rp.add_argument("--entity-id")
    rp.add_argument(
        "--hits",
        action="store_true",
        help="show structured alignment hit rows instead of job summaries",
    )
    rp.add_argument(
        "--format",
        choices=["text", "tsv", "json"],
        default="text",
        help="output format for --hits (default: aligned text table)",
    )
    rp.add_argument(
        "--out",
        metavar="PATH",
        help="write --hits output atomically to PATH instead of stdout",
    )
    rp.add_argument("--query-id", help="filter --hits rows by query id")
    rp.add_argument("--subject-id", help="filter --hits rows by subject id")
    rp.add_argument(
        "--evalue-max",
        type=float,
        metavar="VALUE",
        help="keep only --hits rows with evalue <= VALUE",
    )
    rp.add_argument("--limit", type=int, default=20)
    rp.add_argument("--include-retired", action="store_true")
    rp = report_sub.add_parser(
        "coverage",
        help="measure NCBI family/genus coverage against a frozen reference set",
    )
    rp.add_argument("--reference-set", required=True)
    scope = rp.add_mutually_exclusive_group()
    scope.add_argument("--scope", choices=["metadata"], default="metadata")
    scope.add_argument(
        "--release", help="restrict observations to one immutable release"
    )
    rp = report_sub.add_parser(
        "metadata", help="export a derived read-only metadata TSV snapshot"
    )
    rp.add_argument("--output", help="output directory (default: reports/metadata)")
    rp.add_argument("--include-retired", action="store_true")


def _cmd_qc_table(args: argparse.Namespace, project: Project, db: Database) -> int:
    include_retired = getattr(args, "include_retired", False)
    if (
        getattr(args, "wide", False)
        or getattr(args, "format", "text") != "text"
        or getattr(args, "out", None)
    ):
        from operon.utils import atomic_write_text
        from operon.view_bundle import render_qc_report

        text = render_qc_report(
            db,
            args.format,
            wide=args.wide,
            entity_type=args.entity_type,
            entity_id=args.entity_id,
            include_retired=include_retired,
        )
        if args.out:
            atomic_write_text(args.out, text)
        else:
            print(text, end="")
        if args.export:
            print(
                f"wrote {export_qc_tsv(db, project, args.entity_type, include_retired=include_retired)}"
            )
        return 0
    if include_retired:
        table = print_qc_table(
            db,
            args.entity_type,
            args.entity_id,
            include_retired=True,
        )
    else:
        table = print_qc_table(db, args.entity_type, args.entity_id)
    print(table)
    if args.export:
        path = (
            export_qc_tsv(db, project, args.entity_type, include_retired=True)
            if include_retired
            else export_qc_tsv(db, project, args.entity_type)
        )
        print(f"wrote {path}")
    return 0


def _cmd_decisions(args: argparse.Namespace, project: Project, db: Database) -> int:
    if getattr(args, "include_retired", False):
        output = print_decisions(db, args.profile, include_retired=True)
    else:
        output = print_decisions(db, args.profile)
    print(output)
    return 0


def _cmd_taxonomy(args: argparse.Namespace, project: Project, db: Database) -> int:
    if args.taxonomy_command == "import":
        result = import_ncbi_taxonomy(db, project, args.input, args.version)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.taxonomy_command == "compile":
        result = compile_reference_set(db, project, args.profile, args.taxonomy_version)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.taxonomy_command == "list":
        rows = list_taxonomy_snapshots(db)
        print(
            format_table(
                [
                    "snapshot_id",
                    "source",
                    "version",
                    "nodes",
                    "status",
                    "sha256",
                    "imported_at",
                ],
                (
                    [
                        row["taxonomy_snapshot_id"],
                        row["source"],
                        row["taxonomy_version"],
                        row["node_count"],
                        row["status"],
                        row["source_sha256"],
                        row["imported_at"],
                    ]
                    for row in rows
                ),
            )
        )
        return 0
    if args.taxonomy_command == "reference-sets":
        rows = list_reference_sets(db)
        print(
            format_table(
                [
                    "reference_set",
                    "taxonomy",
                    "profile",
                    "family",
                    "genus",
                    "sha256",
                    "compiled_at",
                ],
                (
                    [
                        row["reference_set_id"],
                        row["taxonomy_version"],
                        row["profile_name"],
                        row["family_count"],
                        row["genus_count"],
                        row["tsv_sha256"],
                        row["compiled_at"],
                    ]
                    for row in rows
                ),
            )
        )
        return 0
    raise ValidationError(f"unknown taxonomy command {args.taxonomy_command!r}")


def _cmd_report(args: argparse.Namespace, project: Project, db: Database) -> int:
    if args.report_kind == "view":
        from operon.view_bundle import export_view_bundle

        print(f"wrote {export_view_bundle(db, args.out)}")
        return 0
    if args.report_kind == "qc":
        return _cmd_qc_table(args, project, db)
    if args.report_kind == "decisions":
        return _cmd_decisions(args, project, db)
    if args.report_kind == "analysis":
        return _cmd_analysis_results(args, db)
    if args.report_kind == "coverage":
        result = report_coverage(
            db, project, args.reference_set, release_version=args.release
        )
        scope_text = result["scope_kind"]
        if result.get("scope_value"):
            scope_text += f":{result['scope_value']}"
        print(f"reference set: {result['reference_set_id']}")
        print(f"scope: {scope_text}")
        print(
            format_table(
                [
                    "rank",
                    "numerator",
                    "denominator",
                    "coverage_pct",
                    "threshold_pct",
                    "decision",
                ],
                (
                    [
                        metric["rank"],
                        metric["numerator"],
                        metric["denominator"],
                        f"{float(metric['coverage_percent']):.4f}",
                        f"{float(metric['threshold_percent']):.4f}",
                        metric["decision"],
                    ]
                    for metric in result["metrics"]
                ),
            )
        )
        print(f"decision: {result['decision']}")
        print(f"report: {result['path']}")
        return int(result["exit_code"])
    if args.report_kind == "metadata":
        path = (
            export_metadata_report(db, project, args.output, include_retired=True)
            if getattr(args, "include_retired", False)
            else export_metadata_report(db, project, args.output)
        )
        print(f"wrote metadata report to {path}")
        return 0
    raise ValidationError(f"unknown report kind {args.report_kind!r}")


def _analysis_results_query(
    args: argparse.Namespace,
    db: Database,
    query_id: Any,
    subject_id: Any,
    evalue_max: Any,
) -> tuple[str, list[Any]]:
    """Build the hits/results query; fragments stay constant, values are bound."""
    if args.hits:
        sql = """
            SELECT a.analysis_name, a.entity_type, a.entity_id, a.query_id, a.subject_id,
                   a.hit_rank, a.query_start, a.query_end, a.subject_start, a.subject_end,
                   a.evalue, a.bitscore, a.percent_identity
            FROM analysis_alignments a
            JOIN analysis_jobs j ON j.job_id = a.job_id
            JOIN (
                SELECT analysis_name, file_id, MAX(job_id) AS latest_job_id
                FROM analysis_jobs WHERE status='completed'
                GROUP BY analysis_name, file_id
            ) latest ON latest.latest_job_id = a.job_id
            WHERE j.status='completed'
        """
        if (
            not getattr(args, "include_retired", False)
            and db.lifecycle_schema_available()
        ):
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM effective_retired_entities er "
                "WHERE er.entity_type=a.entity_type AND er.entity_id=a.entity_id)"
            )
    else:
        sql = """
            SELECT r.entity_type, r.entity_id, r.analysis_name, r.metric_name,
                   r.metric_value, r.metric_unit, j.tool_version, j.job_id
            FROM analysis_results r
            JOIN analysis_jobs j ON j.job_id = r.job_id
            WHERE j.status='completed'
        """
        if (
            not getattr(args, "include_retired", False)
            and db.lifecycle_schema_available()
        ):
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM effective_retired_entities er "
                "WHERE er.entity_type=r.entity_type AND er.entity_id=r.entity_id)"
            )
    params: list[Any] = []
    if args.analysis:
        if args.hits:
            sql += " AND a.analysis_name=?"
        else:
            sql += " AND r.analysis_name=?"
        params.append(args.analysis)
    if args.entity_type:
        if args.hits:
            sql += " AND a.entity_type=?"
        else:
            sql += " AND r.entity_type=?"
        params.append(args.entity_type)
    if args.entity_id:
        if args.hits:
            sql += " AND a.entity_id=?"
        else:
            sql += " AND r.entity_id=?"
        params.append(args.entity_id)
    if args.hits:
        if query_id:
            sql += " AND a.query_id=?"
            params.append(query_id)
        if subject_id:
            sql += " AND a.subject_id=?"
            params.append(subject_id)
        if evalue_max is not None:
            sql += " AND a.evalue IS NOT NULL AND a.evalue<=?"
            params.append(float(evalue_max))
        sql += " ORDER BY a.entity_id, a.query_id, a.hit_rank LIMIT ?"
    else:
        sql += " ORDER BY r.entity_id, r.metric_name, j.job_id DESC LIMIT ?"
    params.append(int(args.limit))
    return sql, params


def _cmd_analysis_results(args: argparse.Namespace, db: Database) -> int:
    out_format = getattr(args, "format", "text") or "text"
    out_path = getattr(args, "out", None)
    query_id = getattr(args, "query_id", None)
    subject_id = getattr(args, "subject_id", None)
    evalue_max = getattr(args, "evalue_max", None)
    if not args.hits and (
        out_path
        or out_format != "text"
        or query_id
        or subject_id
        or evalue_max is not None
    ):
        raise ValidationError(
            "--format/--out/--query-id/--subject-id/--evalue-max require --hits"
        )
    sql, params = _analysis_results_query(args, db, query_id, subject_id, evalue_max)
    rows = db.conn.execute(sql, params).fetchall()
    if args.hits:
        headers = (
            list(rows[0].keys())
            if rows
            else [
                "analysis_name",
                "entity_type",
                "entity_id",
                "query_id",
                "subject_id",
                "hit_rank",
                "query_start",
                "query_end",
                "subject_start",
                "subject_end",
                "evalue",
                "bitscore",
                "percent_identity",
            ]
        )
        text = (
            render_report_rows(rows, out_format, headers=headers)
            or "(no analysis results)\n"
        )
        if out_path:
            atomic_write_text(out_path, text)
        else:
            sys.stdout.write(text)
        return 0
    if not rows:
        print("(no analysis results)")
    else:
        print(format_table(list(rows[0].keys()), (list(row) for row in rows)))
    return 0
