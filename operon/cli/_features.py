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
from operon.errors import EntityNotFoundError, FeatureIndexError


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


def _features_query(args: argparse.Namespace, project, db) -> int:
    from operon import features

    records, explicit = _resolve_records(db, args)
    if not records:
        return 1
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

    columns = list(features.QUERY_DEFAULT_COLUMNS)
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
        )
        rows = features.feature_query_rows(
            frame, limit=args.limit, include_attributes=False
        )
        for row in rows:
            writer.writerow(
                ["" if row.get(column) is None else row[column] for column in columns]
            )
            returned_any = True
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


def _cmd_features(args, project, db) -> int:
    if args.features_kind == "query":
        return _features_query(args, project, db)
    return _features_stats(args, project, db)
