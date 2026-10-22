#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""``operon features``: query and summarize the flattened GFF3/GTF index.

The feature index is a rebuildable Parquet cache under ``index/features/``
(``operon/features.py``); this module is its thin CLI surface. ``operon.features``
is imported lazily inside the handlers so the rest of the CLI keeps its
startup cost.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from typing import Any

from operon.cli._common import _print_table
from operon.errors import EntityNotFoundError, FeatureIndexError, ValidationError
from operon.utils import escape_formula_text


def _add_selection(parser: argparse.ArgumentParser) -> None:
    chooser = parser.add_mutually_exclusive_group(required=True)
    chooser.add_argument("--file-id", help="query exactly one manifest file")
    chooser.add_argument(
        "--entity-type",
        help="query every annotation file of one entity (use with --entity-id)",
    )
    parser.add_argument(
        "--entity-id", help="entity identifier (required with --entity-type)"
    )


def _add_column_filters(parser: argparse.ArgumentParser) -> None:
    for flag, dest in (
        ("--type", "feature_types"),
        ("--seqid", "seqids"),
        ("--source", "sources"),
        ("--strand", "strands"),
    ):
        parser.add_argument(flag, dest=dest, action="append", default=[])
    parser.add_argument(
        "--start", type=int, help="region start (features overlapping [start, end])"
    )
    parser.add_argument(
        "--end", type=int, help="region end (features overlapping [start, end])"
    )


def _parse_attribute_filters(tokens: list[str]) -> list[tuple[str, str, bool]]:
    """``KEY=VALUE`` (exact) and ``KEY~=PATTERN`` (regex) tokens in order.

    A token carrying ``~=`` selects regex matching over the value; ``=`` in
    the value stays a literal (exact matching is escaped before use).
    """
    filters: list[tuple[str, str, bool]] = []
    for token in tokens:
        if "~=" in token:
            key, _, value = token.partition("~=")
            filters.append((key, value, True))
        elif "=" in token:
            key, _, value = token.partition("=")
            filters.append((key, value, False))
        else:
            raise ValidationError(
                f"--attribute must spell KEY=VALUE or KEY~=PATTERN, got {token!r}"
            )
    return filters


def add_features_parser(sub) -> None:
    p = sub.add_parser(
        "features",
        help="query or summarize the flattened GFF3/GTF annotation feature index",
    )
    kind = p.add_subparsers(dest="features_kind", required=True)

    query = kind.add_parser(
        "query",
        help="filter flattened annotation features (region filters use tabix "
        "overlap semantics)",
    )
    _add_selection(query)
    for flag, dest in (
        ("--type", "feature_types"),
        ("--seqid", "seqids"),
        ("--source", "sources"),
        ("--strand", "strands"),
        ("--gene-id", "gene_ids"),
        ("--transcript-id", "transcript_ids"),
        ("--parent-id", "parent_ids"),
    ):
        query.add_argument(flag, dest=dest, action="append", default=[])
    query.add_argument(
        "--start", type=int, help="region start (features overlapping [start, end])"
    )
    query.add_argument(
        "--end", type=int, help="region end (features overlapping [start, end])"
    )
    query.add_argument(
        "--attribute",
        dest="attribute",
        action="append",
        default=[],
        help="attribute filter KEY=VALUE (exact) or KEY~=PATTERN (regex); "
        "repeatable, multiple filters join with OR",
    )
    query.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="maximum rows to print (0 = unlimited; default 1000)",
    )
    query.add_argument(
        "--format",
        dest="output_format",
        choices=["tsv", "json"],
        default="tsv",
        help="output rendering (default tsv)",
    )
    query.add_argument(
        "--rebuild",
        action="store_true",
        help="drop and rebuild the cached index before querying",
    )

    stats = kind.add_parser(
        "stats",
        help="index identity and per-type/source/seqid counts for one file or entity",
    )
    _add_selection(stats)
    stats.add_argument(
        "--format",
        dest="output_format",
        choices=["tsv", "json"],
        default="tsv",
    )
    stats.add_argument(
        "--rebuild",
        action="store_true",
        help="drop and rebuild the cached index first",
    )

    bed = kind.add_parser(
        "bed",
        help="export annotation features as BED6 (tools' coordinate format)",
    )
    _add_selection(bed)
    _add_column_filters(bed)
    bed.add_argument(
        "--attribute",
        dest="attribute",
        action="append",
        default=[],
        help="attribute filter KEY=VALUE or KEY~=PATTERN; repeatable, OR-joined",
    )
    bed.add_argument(
        "--longest-transcript",
        dest="longest_transcript",
        action="store_true",
        help="one row per gene naming its longest transcript instead of "
        "per-feature rows (the gene's span carries the coordinates)",
    )
    bed.add_argument(
        "--measure",
        choices=["CDS", "mRNA"],
        default="CDS",
        help="transcript ranking for --longest-transcript (default CDS sums)",
    )
    bed.add_argument(
        "--name-attr",
        help="name the exported rows with this attribute of the transcript "
        "(e.g. protein_id) before falling back to its id",
    )
    bed.add_argument(
        "--limit",
        type=int,
        default=0,
        help="maximum rows to write (default 0 = unlimited)",
    )
    bed.add_argument(
        "--output", type=_output_path, help="write BED6 here (default stdout)"
    )
    bed.add_argument(
        "--rebuild",
        action="store_true",
        help="drop and rebuild the cached index first",
    )

    parents = kind.add_parser(
        "parents",
        help="walk up the annotation hierarchy from feature ids "
        "(e.g. transcript -> gene)",
    )
    _add_selection(parents)
    parents.add_argument(
        "--id",
        dest="feature_ids",
        action="append",
        required=True,
        help="feature id to walk from (repeatable)",
    )
    parents.add_argument(
        "--until",
        dest="until_type",
        default="gene",
        help="stop the walk once this feature type is reached (default gene)",
    )
    parents.add_argument(
        "--max-depth", type=int, default=100, help="maximum hops (default 100)"
    )
    parents.add_argument(
        "--format",
        dest="output_format",
        choices=["tsv", "json"],
        default="tsv",
    )
    parents.add_argument(
        "--rebuild",
        action="store_true",
        help="drop and rebuild the cached index first",
    )

    children = kind.add_parser(
        "children",
        help="walk down the annotation hierarchy from feature ids "
        "(e.g. gene -> transcripts)",
    )
    _add_selection(children)
    children.add_argument(
        "--id",
        dest="feature_ids",
        action="append",
        required=True,
        help="feature id to walk from (repeatable)",
    )
    children.add_argument(
        "--recursive",
        action="store_true",
        help="collect every level below, not just direct children",
    )
    children.add_argument(
        "--type",
        dest="feature_types",
        action="append",
        default=[],
        help="restrict collected rows to these feature types (repeatable)",
    )
    children.add_argument(
        "--format",
        dest="output_format",
        choices=["tsv", "json"],
        default="tsv",
    )
    children.add_argument(
        "--rebuild",
        action="store_true",
        help="drop and rebuild the cached index first",
    )

    bins = kind.add_parser(
        "bins",
        help="per-bin feature counts and density (gene-density plotting)",
    )
    _add_selection(bins)
    bins.add_argument(
        "--bin-size",
        type=int,
        default=100_000,
        help="bin width in bases (default 100000)",
    )
    bins.add_argument(
        "--type",
        dest="feature_types",
        action="append",
        default=["gene"],
        help="feature types to count (repeatable; default gene)",
    )
    bins.add_argument(
        "--format",
        dest="output_format",
        choices=["tsv", "json"],
        default="tsv",
    )
    bins.add_argument(
        "--rebuild",
        action="store_true",
        help="drop and rebuild the cached index first",
    )


def _output_path(text: str) -> Any:
    from pathlib import Path

    return Path(text)


def _resolve_records(db: Any, args: argparse.Namespace) -> tuple[list[dict], bool]:
    """Manifest rows backing the selection (usable, explicit_single_file).

    An explicit ``--file-id`` answers for exactly that row; a wrong format or
    remote-only bytes then fails the command (mirroring ``operon qc``). A
    batch selection skips unusable rows with a stderr note and keeps going.
    """
    from operon import features

    if args.file_id:
        row = db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (args.file_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError(
                f"file {args.file_id} does not exist in the manifest"
            )
        record = dict(row)
        if record["format"] not in features.SUPPORTED_SOURCE_FORMATS:
            print(
                f"skipped {record['file_id']}: format {record['format']!r} has "
                "no flattened feature index",
                file=sys.stderr,
            )
            return [], True
        if str(record.get("status", "")) == "REMOTE_ONLY":
            print(
                f"skipped {record['file_id']}: remote-only; pull the file first",
                file=sys.stderr,
            )
            return [], True
        return [record], True
    if not args.entity_type or not args.entity_id:
        raise FeatureIndexError("--entity-type and --entity-id must be given together")
    rows = db.conn.execute(
        "SELECT * FROM files WHERE entity_type=? AND entity_id=? ORDER BY file_id",
        (args.entity_type, args.entity_id),
    ).fetchall()
    if not rows:
        raise EntityNotFoundError(
            f"{args.entity_type} {args.entity_id} has no manifest files"
        )
    usable: list[dict] = []
    for raw in rows:
        record = dict(raw)
        if record["format"] not in features.SUPPORTED_SOURCE_FORMATS:
            print(
                f"skipped {record['file_id']}: format {record['format']!r} is "
                "not an annotation",
                file=sys.stderr,
            )
            continue
        if str(record.get("status", "")) == "REMOTE_ONLY":
            print(f"skipped {record['file_id']}: remote-only", file=sys.stderr)
            continue
        usable.append(record)
    return usable, False


def _selection_identity(record: dict) -> dict[str, Any]:
    return {
        "file_id": record["file_id"],
        "sha256": record["sha256"],
        "size_bytes": record["size_bytes"],
        "format": record["format"],
        "compression": record["compression"],
    }


def _write_default_rows(rows: list[dict[str, Any]], writer, columns: list[str]) -> None:
    for row in rows:
        writer.writerow(
            [
                escape_formula_text("" if row.get(column) is None else row[column])
                for column in columns
            ]
        )


def _features_query(args: argparse.Namespace, project, db) -> int:
    from operon import features

    records, explicit = _resolve_records(db, args)
    if not records:
        return 1
    attribute_filters = _parse_attribute_filters(list(args.attribute or []))
    if args.output_format == "json":
        documents = []
        for record in records:
            frame, info = features.features_scan(project, record, rebuild=args.rebuild)
            frame = features.apply_feature_filters(
                frame,
                types=args.feature_types,
                seqids=args.seqids,
                sources=args.sources,
                strands=args.strands,
                gene_ids=args.gene_ids,
                transcript_ids=args.transcript_ids,
                parent_ids=args.parent_ids,
                region_start=args.start,
                region_end=args.end,
                attribute_filters=attribute_filters or None,
                source_format=record["format"] if attribute_filters else None,
            )
            rows = features.feature_query_rows(
                frame, limit=args.limit, include_attributes=True
            )
            documents.append(
                {
                    **_selection_identity(record),
                    "index_relative_path": info["relative_path"],
                    "index_feature_count": info["feature_count"],
                    "returned": len(rows),
                    "rows": rows,
                }
            )
        print(json.dumps({"files": documents}, ensure_ascii=False))
        return 0

    from operon.features import QUERY_DEFAULT_COLUMNS

    columns = list(QUERY_DEFAULT_COLUMNS)
    writer = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    writer.writerow(columns)
    returned_any = False
    for record in records:
        frame, _info = features.features_scan(project, record, rebuild=args.rebuild)
        frame = features.apply_feature_filters(
            frame,
            types=args.feature_types,
            seqids=args.seqids,
            sources=args.sources,
            strands=args.strands,
            gene_ids=args.gene_ids,
            transcript_ids=args.transcript_ids,
            parent_ids=args.parent_ids,
            region_start=args.start,
            region_end=args.end,
            attribute_filters=attribute_filters or None,
            source_format=record["format"] if attribute_filters else None,
        )
        rows = features.feature_query_rows(
            frame, limit=args.limit, include_attributes=False
        )
        _write_default_rows(rows, writer, columns)
        returned_any = returned_any or bool(rows)
    return 0 if returned_any or not explicit else 1


def _features_stats(args: argparse.Namespace, project, db) -> int:
    from operon import features

    records, _explicit = _resolve_records(db, args)
    if not records:
        return 1
    if args.output_format == "json":
        documents = []
        for record in records:
            frame, info = features.features_scan(project, record, rebuild=args.rebuild)
            documents.append(
                {
                    **_selection_identity(record),
                    "index_relative_path": info["relative_path"],
                    "feature_count": info["feature_count"],
                    **features.feature_stats(frame),
                }
            )
        print(json.dumps({"files": documents}, ensure_ascii=False))
        return 0

    for record in records:
        frame, info = features.features_scan(project, record, rebuild=args.rebuild)
        stats = features.feature_stats(frame)
        print(f"{record['file_id']}\tfeatures: {info['feature_count']}")
        for title, headers, rows in (
            ("by type", ["type", "count"], stats["by_type"]),
            ("by source", ["source", "count"], stats["by_source"]),
            (
                "by seqid",
                ["seqid", "count", "min_start", "max_end"],
                stats["by_seqid"],
            ),
        ):
            print(f"## {title}")
            _print_table(headers, [[row.get(c) for c in headers] for row in rows])
    return 0


def _features_bed(args: argparse.Namespace, project, db) -> int:
    from operon import features

    records, explicit = _resolve_records(db, args)
    if not records:
        return 1
    attribute_filters = _parse_attribute_filters(list(args.attribute or []))
    handle = (
        args.output.open("w", encoding="utf-8", newline="") if args.output else None
    )
    try:
        writer = csv.writer(
            handle if handle is not None else sys.stdout,
            delimiter="\t",
            lineterminator="\n",
        )
        returned_any = False
        for record in records:
            source_format = record["format"]
            frame, _info = features.features_scan(project, record, rebuild=args.rebuild)
            frame = features.apply_feature_filters(
                frame,
                types=args.feature_types or None,
                seqids=args.seqids or None,
                sources=args.sources or None,
                strands=args.strands or None,
                region_start=args.start,
                region_end=args.end,
                attribute_filters=attribute_filters or None,
                source_format=source_format,
            )
            if args.longest_transcript:
                rows = features.feature_longest_rows(
                    frame,
                    source_format=source_format,
                    measure=args.measure,
                    name_attr=args.name_attr,
                )
                if args.limit and args.limit > 0:
                    rows = rows[: args.limit]
            else:
                rows = features.feature_bed_rows(frame, limit=args.limit)
            for row in rows:
                writer.writerow(
                    [
                        row["chrom"],
                        row["chromStart"],
                        row["chromEnd"],
                        row["name"],
                        f"{row['score']:g}",
                        row["strand"],
                    ]
                )
                returned_any = True
    finally:
        if handle is not None:
            handle.close()
    return 0 if returned_any or not explicit else 1


def _features_traversal(
    args: argparse.Namespace, project, db, *, direction: str
) -> int:
    from operon import features

    records, explicit = _resolve_records(db, args)
    if not records:
        return 1
    tsv = args.output_format == "tsv"
    columns = list(features.QUERY_DEFAULT_COLUMNS)
    writer = (
        csv.writer(sys.stdout, delimiter="\t", lineterminator="\n") if tsv else None
    )
    if writer is not None:
        writer.writerow(columns)
    returned_any = False
    documents: list[dict[str, Any]] = []
    for record in records:
        frame, info = features.features_scan(project, record, rebuild=args.rebuild)
        if direction == "parents":
            expr = features.feature_ancestor_expr(
                frame,
                initial_ids=args.feature_ids,
                until_type=args.until_type or "gene",
                max_depth=args.max_depth,
            )
        else:
            expr = features.feature_descendant_expr(
                frame,
                initial_ids=args.feature_ids,
                recursive=args.recursive,
            )
        if expr is not None:
            if direction == "children" and args.feature_types:
                import polars as pl

                expr = expr & pl.col("type").is_in(list(args.feature_types))
            rows = features.feature_query_rows(
                frame.filter(expr), limit=0, include_attributes=True
            )
        else:
            rows = []
        returned_any = returned_any or bool(rows)
        if writer is not None:
            _write_default_rows(rows, writer, columns)
        else:
            documents.append(
                {
                    **_selection_identity(record),
                    "index_relative_path": info["relative_path"],
                    "index_feature_count": info["feature_count"],
                    "returned": len(rows),
                    "rows": rows,
                }
            )
    if not tsv:
        print(json.dumps({"files": documents}, ensure_ascii=False))
    return 0 if returned_any or not explicit else 1


def _features_bins(args: argparse.Namespace, project, db) -> int:
    from operon import features

    records, explicit = _resolve_records(db, args)
    if not records:
        return 1
    headers = (
        "seqid",
        "bin_start",
        "bin_end",
        "count",
        "feature_start_min",
        "feature_end_max",
        "features_per_bp",
    )
    documents: list[dict[str, Any]] = []
    writer: csv.writer | None = None
    if args.output_format == "tsv":
        writer = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
        writer.writerow(headers)
    returned_any = False
    for record in records:
        frame, info = features.features_scan(project, record, rebuild=args.rebuild)
        rows = features.feature_bins_rows(
            frame, bin_size=args.bin_size, types=args.feature_types
        )
        returned_any = returned_any or bool(rows)
        if writer is not None:
            for row in rows:
                writer.writerow([row.get(column) for column in headers])
        else:
            documents.append(
                {
                    **_selection_identity(record),
                    "index_relative_path": info["relative_path"],
                    "index_feature_count": info["feature_count"],
                    "bin_size": args.bin_size,
                    "returned": len(rows),
                    "bins": rows,
                }
            )
    if writer is None:
        print(json.dumps({"files": documents}, ensure_ascii=False))
    return 0 if returned_any or not explicit else 1


def _cmd_features(args, project, db) -> int:
    if args.features_kind == "query":
        return _features_query(args, project, db)
    if args.features_kind == "stats":
        return _features_stats(args, project, db)
    if args.features_kind == "bed":
        return _features_bed(args, project, db)
    if args.features_kind in {"parents", "children"}:
        return _features_traversal(args, project, db, direction=args.features_kind)
    return _features_bins(args, project, db)
