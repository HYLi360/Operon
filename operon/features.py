#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Flattened GFF3/GTF feature index: per-file Parquet under ``index/features``.

Every annotation manifest row may own a queryable feature cache at
``{index_root}/features/{file_id}.{sha256}.v1/`` holding ``data.parquet`` and
a ``meta.json`` identity/commit marker. The archive bytes stay immutable;
the index is unregistered rebuildable derived data (INV-2), keyed by the
manifest identity (INV-3), and therefore never a member of the manifest, a
release snapshot or a backup. A missing, corrupt or stale index is rebuilt
from the archived bytes on the next use.

Both GFF3 and GTF sources flatten into one schema; the dialect only changes
attribute parsing (``key=value;`` vs ``key "value";``). Rows are consumed
streaming in bounded batches, so index building never holds a whole large
annotation in memory; queries run through polars ``scan_parquet`` with
projection and predicate pushdown.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.parse
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from operon.config import Project, project_rel
from operon.errors import FeatureIndexError
from operon.utils import now_iso, open_maybe_gzip

FEATURES_CACHE_FORMAT = "operon-features-v1"
FEATURE_INDEX_DIR_SUFFIX = ".v1"
#: Source lines buffered per row group; one flush is one row group, so
#: region filters prune on row-group statistics without a global sort.
FEATURE_ROW_GROUP_ROWS = 100_000

FEATURE_FIELDS = (
    ("seqid", pa.string()),
    ("source", pa.string()),
    ("type", pa.string()),
    ("start", pa.int64()),
    ("end", pa.int64()),
    ("score", pa.float64()),
    ("strand", pa.string()),
    ("phase", pa.string()),
    ("id", pa.string()),
    ("parent", pa.string()),
    ("gene_id", pa.string()),
    ("transcript_id", pa.string()),
    ("attributes", pa.string()),
    ("line_number", pa.int64()),
)

#: Compact query output; ``attributes`` is only shown with JSON output.
QUERY_DEFAULT_COLUMNS = (
    "seqid",
    "source",
    "type",
    "start",
    "end",
    "strand",
    "phase",
    "id",
    "parent",
    "gene_id",
    "transcript_id",
    "line_number",
)

FEATURES_SCHEMA = pa.schema(
    [(name, field_type) for name, field_type in FEATURE_FIELDS],
    metadata={"cache_format": FEATURES_CACHE_FORMAT},
)

#: The annotation formats the flattener accepts.
SUPPORTED_SOURCE_FORMATS = ("gff3", "gtf")


def features_index_dir(project: Project, record: dict[str, Any]) -> Path:
    """Directory holding the feature index for one manifest record."""
    name = f"{record['file_id']}.{str(record['sha256']).lower()}"
    return project.index_root / "features" / f"{name}{FEATURE_INDEX_DIR_SUFFIX}"


def _data_path(directory: Path) -> Path:
    return directory / "data.parquet"


def _meta_path(directory: Path) -> Path:
    return directory / "meta.json"


def _read_meta(directory: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    try:
        meta = json.loads(_meta_path(directory).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(meta, dict) or not _data_path(directory).is_file():
        return None
    expected = {
        "cache_format": FEATURES_CACHE_FORMAT,
        "file_id": str(record["file_id"]),
        "sha256": str(record["sha256"]).lower(),
        "size_bytes": int(record["size_bytes"]),
        "source_format": str(record["format"]),
    }
    if any(meta.get(key) != value for key, value in expected.items()):
        return None
    if not isinstance(meta.get("feature_count"), int):
        return None
    return meta


def _decode_gff3_value(value: str) -> str:
    return urllib.parse.unquote(value) if "%" in value else value


def _parse_attributes(attributes: str, source_format: str) -> dict[str, str]:
    """Attribute map for either dialect.

    GFF3 blocks are ``key=value`` and values are percent-decoded (mirroring
    the QC parser's contract); GTF blocks are ``key "value"`` with quotes
    stripped. ``=``-shaped blocks are accepted inside either dialect so mixed
    files stay parseable.
    """
    values: dict[str, str] = {}
    for part in attributes.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, _, value = part.partition("=")
            key = key.strip()
            if key:
                values[key] = _decode_gff3_value(value.strip())
        elif source_format == "gtf":
            key, _, value = part.partition(" ")
            key = key.strip()
            if key:
                values[key] = value.strip().strip('"')
    return values


def _attribute_value(values: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = values.get(key)
        if value:
            return value
    return None


def _strict_value(
    converter: type, column: str, text: str, file_id: str, line_number: int
) -> Any:
    try:
        return converter(text)
    except ValueError as exc:
        raise FeatureIndexError(
            f"{file_id}: feature index input line {line_number} has an invalid "
            f"{column} value {text!r}"
        ) from exc


def _parse_feature_row(
    line: str, source_format: str, file_id: str, line_number: int
) -> dict[str, Any]:
    columns = line.split("\t")
    if len(columns) != 9:
        raise FeatureIndexError(
            f"{file_id}: feature index input line {line_number} has "
            f"{len(columns)} tab-separated columns; a flattened row requires 9"
        )
    (
        seqid,
        source,
        feature_type,
        start_text,
        end_text,
        score_text,
        strand,
        phase,
        attributes,
    ) = columns
    if not seqid:
        raise FeatureIndexError(
            f"{file_id}: feature index input line {line_number} has an empty seqid"
        )
    start = _strict_value(int, "start", start_text, file_id, line_number)
    end = _strict_value(int, "end", end_text, file_id, line_number)
    if start < 1 or end < start:
        raise FeatureIndexError(
            f"{file_id}: feature index input line {line_number} has invalid "
            f"coordinates ({start_text!r}..{end_text!r}); positive start must "
            "not exceed end"
        )
    if score_text == ".":
        score: float | None = None
    else:
        score = _strict_value(float, "score", score_text, file_id, line_number)
    values = _parse_attributes(attributes, source_format)
    return {
        "seqid": seqid,
        "source": source or None,
        "type": feature_type or None,
        "start": start,
        "end": end,
        "score": score,
        "strand": strand or None,
        "phase": phase or None,
        "id": _attribute_value(values, "ID"),
        "parent": _attribute_value(values, "Parent"),
        "gene_id": _attribute_value(values, "gene_id"),
        "transcript_id": _attribute_value(values, "transcript_id"),
        "attributes": attributes,
        "line_number": line_number,
    }


def _source_lines(path: Path, file_id: str) -> Iterator[tuple[str, int]]:
    """Yield feature-bearing source lines; ``##FASTA`` terminates the stream."""
    line_number = 0
    with open_maybe_gzip(path, "rt", encoding="utf-8") as handle:
        for raw_line in handle:
            line_number += 1
            line = raw_line.rstrip("\r\n")
            if not line or line.startswith("#"):
                if line == "##FASTA":
                    return
                continue
            yield line, line_number


def _flush_batch(writer: pq.ParquetWriter, buffers: dict[str, list[Any]]) -> None:
    arrays = [
        pa.array(buffers[name], type=field_type) for name, field_type in FEATURE_FIELDS
    ]
    writer.write_batch(pa.RecordBatch.from_arrays(arrays, schema=FEATURES_SCHEMA))


def build_features_index(
    project: Project, record: dict[str, Any], *, force_rebuild: bool = False
) -> dict[str, Any]:
    """Build (or rebuild) the feature index for one annotation manifest row.

    Returns status ``built`` for a fresh index and ``rebuilt`` when a stale
    or corrupt existing one was replaced. The source archive path comes from
    the manifest row; a missing archive raises :class:`FeatureIndexError`.
    """
    source_format = str(record["format"])
    if source_format not in SUPPORTED_SOURCE_FORMATS:
        raise FeatureIndexError(
            f"{record['file_id']}: format {source_format!r} has no flattened "
            f"feature index (supported: {', '.join(SUPPORTED_SOURCE_FORMATS)})"
        )
    source_path = project.root / str(record["relative_path"])
    if not source_path.is_file():
        raise FeatureIndexError(
            f"{record['file_id']}: local bytes absent "
            f"({record['relative_path']}); pull the file before building its "
            "feature index"
        )
    directory = features_index_dir(project, record)
    status = "rebuilt" if force_rebuild or directory.exists() else "built"
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    data_staging = directory / f".data.parquet.{os.getpid()}.tmp"
    meta_staging = directory / f".meta.json.{os.getpid()}.tmp"
    feature_count = 0
    try:
        buffers: dict[str, list[Any]] = {name: [] for name, _ in FEATURE_FIELDS}
        with pq.ParquetWriter(str(data_staging), FEATURES_SCHEMA) as writer:
            for line, line_number in _source_lines(source_path, str(record["file_id"])):
                row = _parse_feature_row(
                    line, source_format, str(record["file_id"]), line_number
                )
                for name, _ in FEATURE_FIELDS:
                    buffers[name].append(row[name])
                feature_count += 1
                if feature_count % FEATURE_ROW_GROUP_ROWS == 0:
                    _flush_batch(writer, buffers)
                    for values in buffers.values():
                        values.clear()
            if feature_count % FEATURE_ROW_GROUP_ROWS != 0:
                _flush_batch(writer, buffers)
                for values in buffers.values():
                    values.clear()
        os.replace(data_staging, _data_path(directory))
        meta = {
            "cache_format": FEATURES_CACHE_FORMAT,
            "file_id": str(record["file_id"]),
            "sha256": str(record["sha256"]).lower(),
            "size_bytes": int(record["size_bytes"]),
            "source_format": source_format,
            "feature_count": feature_count,
            "built_at": now_iso(),
        }
        meta_staging.write_text(
            json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(meta_staging, _meta_path(directory))
    except BaseException:
        data_staging.unlink(missing_ok=True)
        meta_staging.unlink(missing_ok=True)
        raise
    return {
        "status": status,
        "cache_format": FEATURES_CACHE_FORMAT,
        "file_id": str(record["file_id"]),
        "sha256": str(record["sha256"]).lower(),
        "size_bytes": int(record["size_bytes"]),
        "source_format": source_format,
        "feature_count": feature_count,
        "relative_path": project_rel(project, directory),
    }


def ensure_features_index(
    project: Project, record: dict[str, Any], *, rebuild: bool = False
) -> tuple[Path, dict[str, Any]]:
    """Return the index directory and its meta, building when stale.

    A meta that fails the identity check, a corrupted ``data.parquet`` or a
    missing commit marker all count as stale and trigger a transparent
    rebuild. ``rebuild=True`` skips validation and rebuilds unconditionally.
    """
    directory = features_index_dir(project, record)
    if not rebuild:
        meta = _read_meta(directory, record)
        if meta is not None:
            return directory, {
                **meta,
                "status": "hit",
                "relative_path": project_rel(project, directory),
            }
    forced_rebuild = rebuild
    info = build_features_index(project, record, force_rebuild=forced_rebuild)
    status = "rebuilt" if forced_rebuild else info["status"]
    return directory, {**info, "status": status}


def features_scan(
    project: Project, record: dict[str, Any], *, rebuild: bool = False
) -> tuple[pl.LazyFrame, dict[str, Any]]:
    """Lazy polars scan of the file's feature index, building on demand.

    Rebuilds once when the on-disk index is missing or stale; the returned
    frame filters nothing itself — callers attach predicates.
    """
    directory, meta = ensure_features_index(project, record, rebuild=rebuild)
    frame = pl.scan_parquet(_data_path(directory))
    return frame, {**meta, "relative_path": project_rel(project, directory)}


def apply_feature_filters(
    frame: pl.LazyFrame,
    *,
    types: list[str] | None = None,
    seqids: list[str] | None = None,
    sources: list[str] | None = None,
    strands: list[str] | None = None,
    gene_ids: list[str] | None = None,
    transcript_ids: list[str] | None = None,
    parent_ids: list[str] | None = None,
    region_start: int | None = None,
    region_end: int | None = None,
) -> pl.LazyFrame:
    """Attach CLI predicates to a lazy feature frame.

    Region semantics follow tabix: a feature matches when its inclusive
    ``[start, end]`` span overlaps the requested region, so ``--start N``
    alone selects the right-open tail and ``--end M`` alone the left-open
    head.
    """
    if types:
        frame = frame.filter(pl.col("type").is_in(list(types)))
    if seqids:
        frame = frame.filter(pl.col("seqid").is_in(list(seqids)))
    if sources:
        frame = frame.filter(pl.col("source").is_in(list(sources)))
    if strands:
        frame = frame.filter(pl.col("strand").is_in(list(strands)))
    if gene_ids:
        frame = frame.filter(pl.col("gene_id").is_in(list(gene_ids)))
    if transcript_ids:
        frame = frame.filter(pl.col("transcript_id").is_in(list(transcript_ids)))
    if parent_ids:
        frame = frame.filter(pl.col("parent").is_in(list(parent_ids)))
    if region_start is not None:
        frame = frame.filter(pl.col("end") >= region_start)
    if region_end is not None:
        frame = frame.filter(pl.col("start") <= region_end)
    return frame


def feature_query_rows(
    frame: pl.LazyFrame, *, limit: int, include_attributes: bool
) -> list[dict[str, Any]]:
    """Collect filtered rows in the fixed output column order.

    TSV queries use the compact default columns; JSON queries carry the full
    schema order including ``score`` and ``attributes``.
    """
    columns = (
        [name for name, _field_type in FEATURE_FIELDS]
        if include_attributes
        else list(QUERY_DEFAULT_COLUMNS)
    )
    frame = frame.select(columns)
    if limit and limit > 0:
        frame = frame.limit(limit)
    return frame.collect().to_dicts()


def feature_stats(frame: pl.LazyFrame) -> dict[str, list[dict[str, Any]]]:
    """Per-type, per-source and per-seqid aggregates for one index."""
    by_type = (
        frame.group_by("type")
        .agg(pl.len().alias("count"))
        .sort("type")
        .collect()
        .to_dicts()
    )
    by_source = (
        frame.group_by("source")
        .agg(pl.len().alias("count"))
        .sort("source")
        .collect()
        .to_dicts()
    )
    by_seqid = (
        frame.group_by("seqid")
        .agg(
            pl.len().alias("count"),
            pl.col("start").min().alias("min_start"),
            pl.col("end").max().alias("max_end"),
        )
        .sort("seqid")
        .collect()
        .to_dicts()
    )
    return {"by_type": by_type, "by_source": by_source, "by_seqid": by_seqid}
