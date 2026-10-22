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
import re
import shutil
import urllib.parse
from collections.abc import Iterable, Iterator
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

#: Feature types naming transcript rows across dialects (GFF3 ``mRNA``,
#: generic GTF ``transcript``).
TRANSCRIPT_TYPES = ("mRNA", "transcript")


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


# Attribute filters: (key, value, value_is_regex). Values match the verbatim
# ``attributes`` text kept in the index (percent-encoded GFF3 or quoted GTF),
# except for the four flattened keys that also have a decoded column.
AttributeFilter = tuple[str, str, bool]

#: Attribute keys mirrored by decoded columns (``id``/``parent`` are only
#: populated for GFF3; ``gene_id``/``transcript_id`` for both dialects).
_ATTRIBUTE_COLUMN_KEYS = {
    "id": "id",
    "parent": "parent",
    "gene_id": "gene_id",
    "transcript_id": "transcript_id",
}


def _attribute_column_key(source_format: str, key: str) -> str | None:
    lowered = key.lower()
    column = _ATTRIBUTE_COLUMN_KEYS.get(lowered)
    if column in {"id", "parent"} and source_format != "gff3":
        return None
    return column


def _attribute_expr(
    source_format: str, key: str, value: str, *, regex: bool
) -> pl.Expr:
    """One attribute predicate over the verbatim ``attributes`` text.

    GFF3 blocks delimit with ``;`` and ``key=value``; GTF blocks spell
    ``key "value";``. The four flattened keys short-circuit to their decoded
    columns (``ID``/``Parent`` values are stored percent-decoded); regex
    values keep their anchors at the matched text's own boundaries.
    """
    alias_column = _attribute_column_key(source_format, key)
    if alias_column is not None:
        column = pl.col(alias_column)
        if regex:
            return column.is_not_null() & column.str.contains(value)
        return column == value
    attribute_column = pl.col("attributes")
    key_pattern = re.escape(key)
    value_pattern = value if regex else re.escape(value)
    if source_format == "gtf":
        pattern = rf'(?:^|;)\s*{key_pattern}\s+"{value_pattern}"\s*(?:;|$)'
    else:
        pattern = rf"(?:^|;)\s*{key_pattern}\s*=\s*{value_pattern}\s*(?:;|$)"
    return attribute_column.is_not_null() & attribute_column.str.contains(pattern)


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
    attribute_filters: Iterable[AttributeFilter] | None = None,
    source_format: str | None = None,
) -> pl.LazyFrame:
    """Attach CLI predicates to a lazy feature frame.

    Region semantics follow tabix: a feature matches when its inclusive
    ``[start, end]`` span overlaps the requested region, so ``--start N``
    alone selects the right-open tail and ``--end M`` alone the left-open
    head.

    ``attribute_filters`` are ``(key, value, value_is_regex)`` triples and
    need the dialect via ``source_format`` (the raw ``attributes`` text is
    matched dialect-sensitively).
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
    if attribute_filters:
        if source_format not in SUPPORTED_SOURCE_FORMATS:
            raise FeatureIndexError(
                "attribute filters require a GFF3/GTF source format "
                f"(got {source_format!r})"
            )
        exprs = []
        for key, value, value_is_regex in attribute_filters:
            if not key or not value:
                raise FeatureIndexError(
                    "attribute filters require a non-empty KEY with a value "
                    '("KEY=VALUE" exact or "KEY~=PATTERN" regex)'
                )
            exprs.append(
                _attribute_expr(source_format, key, value, regex=value_is_regex)
            )
        # The repeated tokens form one OR group among themselves while every
        # other column filter stays AND-composed with the group.
        frame = frame.filter(pl.any_horizontal(exprs))
    return frame


def _feature_nodes(frame: pl.LazyFrame, columns: list[str]) -> list[tuple[Any, ...]]:
    """The compact node table a traversal needs (one collect per call)."""
    return frame.select(columns).collect().iter_rows()


def feature_ancestor_expr(
    frame: pl.LazyFrame,
    *,
    source_format: str,
    initial_ids: Iterable[str],
    until_type: str = "gene",
    max_depth: int = 100,
) -> pl.Expr | None:
    """Predicate selecting the row(s) reached walking up from feature ids.

    GFF3 climbs the ``parent`` chain (stop at ``until_type`` or the
    parent-less chain root); GTF has no ``Parent`` edges, so the "gene" of
    an id resolves through its ``gene_id`` attribute materialized on the
    nearest transcript row. Returns ``None`` when no id is present at all.
    """
    wanted: list[str] = []
    for identifier in initial_ids:
        if identifier and identifier not in wanted:
            wanted.append(identifier)
    if not wanted:
        return None
    if source_format == "gtf":
        transcript_gene: dict[str, str | None] = {}
        gene_present: set[str] = set()
        for feature_type, gene_id, transcript_id in _feature_nodes(
            frame, ["type", "gene_id", "transcript_id"]
        ):
            if gene_id:
                gene_present.add(str(gene_id))
            if transcript_id:
                transcript_gene.setdefault(str(transcript_id), gene_id)
        resolved: set[str] = set()
        for identifier in wanted:
            if identifier in transcript_gene:
                gene_of = transcript_gene[identifier]
                if gene_of:
                    resolved.add(gene_of)
            elif identifier in gene_present:
                resolved.add(identifier)
        if not resolved:
            return None
        return (pl.col("type") == until_type) & pl.col("gene_id").is_in(
            sorted(resolved)
        )
    parent_of: dict[str, str | None] = {}
    type_of: dict[str, str | None] = {}
    for feature_id, parent, feature_type in _feature_nodes(
        frame, ["id", "parent", "type"]
    ):
        if feature_id:
            parent_of[str(feature_id)] = parent
            type_of[str(feature_id)] = feature_type
    frontier = [identifier for identifier in wanted if identifier in parent_of]
    if not frontier:
        return None
    reached: set[str] = set()
    visited: set[str] = set(frontier)
    for _depth in range(max(1, int(max_depth))):
        next_ids: list[str] = []
        for identifier in frontier:
            parent = parent_of.get(identifier)
            if parent and parent not in visited:
                next_ids.append(str(parent))
        if not next_ids:
            break
        frontier = sorted(set(next_ids))
        visited.update(frontier)
        reached.update(frontier)
        if any(type_of.get(identifier) == until_type for identifier in frontier):
            break
    if not reached:
        return None
    return pl.col("id").is_in(sorted(reached)) & (
        (pl.col("type") == until_type) | pl.col("parent").is_null()
    )


def feature_descendant_expr(
    frame: pl.LazyFrame,
    *,
    source_format: str,
    initial_ids: Iterable[str],
    recursive: bool = False,
) -> pl.Expr | None:
    """Predicate selecting the child rows of the given feature ids.

    GFF3 children hang off ``parent`` (direct, or every level when
    ``recursive``); GTF children hang off ``gene_id`` (the transcript rows),
    and ``recursive`` adds every row referencing those transcripts through
    ``transcript_id``. Returns ``None`` when no id matches.
    """
    wanted: list[str] = []
    for identifier in initial_ids:
        if identifier and identifier not in wanted:
            wanted.append(identifier)
    if not wanted:
        return None
    if source_format == "gtf":
        gene_present: set[str] = set()
        transcripts_of_gene: dict[str, set[str]] = {}
        for gene_id, transcript_id, feature_type in _feature_nodes(
            frame, ["gene_id", "transcript_id", "type"]
        ):
            if gene_id:
                gene_present.add(str(gene_id))
            if gene_id and feature_type in TRANSCRIPT_TYPES and transcript_id:
                transcripts_of_gene.setdefault(str(gene_id), set()).add(
                    str(transcript_id)
                )
        resolved = [identifier for identifier in wanted if identifier in gene_present]
        if not resolved:
            return None
        predicates = [
            (pl.col("gene_id") == identifier)
            & pl.col("type").is_in(list(TRANSCRIPT_TYPES))
            for identifier in resolved
        ]
        if recursive:
            for identifier in resolved:
                transcripts = transcripts_of_gene.get(identifier)
                if transcripts:
                    predicates.append(
                        pl.col("transcript_id").is_in(sorted(transcripts))
                    )
        return pl.any_horizontal(predicates)
    child_of: dict[str, set[str]] = {}
    for feature_id, parent in _feature_nodes(frame, ["id", "parent"]):
        if parent:
            child_of.setdefault(str(parent), set()).add(
                str(feature_id) if feature_id else ""
            )
    frontier = [identifier for identifier in wanted if identifier in child_of]
    if not frontier:
        return None
    if not recursive:
        return pl.col("parent").is_in(sorted(frontier))
    reachable: set[str] = set(frontier)
    visited: set[str] = set()
    while frontier:
        frontier = sorted(
            {
                child
                for parent in frontier
                for child in child_of.get(parent, ())
                if child
            }
            - visited
        )
        visited.update(frontier)
        reachable.update(frontier)
    return pl.col("parent").is_in(sorted(reachable))


def feature_longest_rows(
    frame: pl.LazyFrame,
    *,
    source_format: str,
    measure: str = "CDS",
    name_attr: str | None = None,
) -> list[dict[str, Any]]:
    """One BED row per gene carrying its longest transcript.

    ``measure="CDS"`` ranks transcripts by the summed length of their CDS
    children; ``"mRNA"`` ranks by the transcript's own span. When no
    transcript rows exist at all (bacteria), CDS rows fall back to their
    direct gene (GFF3 ``parent``, GTF ``gene_id`` attribute) and the longest
    single CDS row becomes the feature. Gene coordinates, score and strand
    come from the gene row; the name is the transcript's ``id`` (GFF3), or
    its ``name_attr`` attribute value when given.
    """
    if measure not in {"CDS", "mRNA"}:
        raise FeatureIndexError(f"unsupported --measure {measure!r} (CDS|mRNA)")
    if measure == "mRNA":
        return _longest_rows_by_span(frame, source_format, name_attr)
    return _longest_rows_by_cds(frame, source_format, name_attr)


def _bed_row(gene: dict[str, Any], name: str | None) -> dict[str, Any]:
    return {
        "chrom": gene["seqid"],
        "chromStart": int(gene["start"]) - 1,
        "chromEnd": int(gene["end"]),
        "name": name or "",
        "score": 0 if gene["score"] is None else float(gene["score"]),
        "strand": gene["strand"] or ".",
    }


def _gene_rows(
    collected: pl.DataFrame, source_format: str
) -> dict[str, dict[str, Any]]:
    """Gene-row coordinates keyed by gene identifier (first wins)."""
    genes: dict[str, dict[str, Any]] = {}
    for seqid, start, end, score, strand, gene_id, row_id in collected.iter_rows():
        key = ""
        if gene_id:
            key = str(gene_id)
        elif source_format == "gff3" and row_id:
            key = str(row_id)
        if not key:
            continue
        genes.setdefault(
            key,
            {
                "seqid": seqid,
                "start": start,
                "end": end,
                "score": score,
                "strand": strand,
            },
        )
    return genes


def _transcript_rows(frame: pl.LazyFrame, source_format: str) -> list[dict[str, Any]]:
    """Transcript-level rows with their gene reference (per dialect)."""
    transcript = frame.filter(pl.col("type").is_in(list(TRANSCRIPT_TYPES)))
    if source_format == "gtf":
        rows = transcript.select(
            [
                "transcript_id",
                "gene_id",
                "seqid",
                "start",
                "end",
                "attributes",
            ]
        ).collect()
        result = [
            {
                "id": str(transcript_id) if transcript_id else "",
                "gene_ref": str(gene_id) if gene_id else None,
                "seqid": seqid,
                "start": start,
                "end": end,
                "attributes": attributes,
            }
            for transcript_id, gene_id, seqid, start, end, attributes in rows.iter_rows()
        ]
    else:
        rows = transcript.select(
            ["id", "parent", "seqid", "start", "end", "attributes"]
        ).collect()
        result = [
            {
                "id": str(row_id) if row_id else "",
                "gene_ref": str(parent) if parent else None,
                "seqid": seqid,
                "start": start,
                "end": end,
                "attributes": attributes,
            }
            for row_id, parent, seqid, start, end, attributes in rows.iter_rows()
        ]
    return [row for row in result if row["id"] and row["gene_ref"]]


def _cds_sum(
    cds_rows: list[tuple[Any, ...]],
) -> dict[str, int]:
    """Total CDS length per key (parent or transcript_id, per dialect)."""
    totals: dict[str, int] = {}
    for group_key, length in cds_rows:
        if not group_key:
            continue
        totals[str(group_key)] = totals.get(str(group_key), 0) + int(length)
    return totals


def _name_for(
    transcript: dict[str, Any], name_attr: str | None, source_format: str
) -> str:
    if name_attr:
        values = _parse_attributes(transcript["attributes"] or "", source_format)
        return values.get(name_attr) or transcript["id"]
    return transcript["id"]


def _longest_rows_by_cds(
    frame: pl.LazyFrame, source_format: str, name_attr: str | None
) -> list[dict[str, Any]]:
    gene_frame = frame.filter(pl.col("type") == "gene").select(
        ["seqid", "start", "end", "score", "strand", "gene_id", "id"]
    )
    genes = _gene_rows(gene_frame.collect(), source_format)
    cds_frame = frame.filter(pl.col("type") == "CDS")
    key_expr = pl.col("transcript_id") if source_format == "gtf" else pl.col("parent")
    links = cds_frame.select(
        key_expr.alias("group_key"),
        (pl.col("end") - pl.col("start") + 1).alias("length"),
        "id",
        "transcript_id",
        "gene_id",
        "attributes",
    ).collect()
    totals = _cds_sum([(row[0], row[1]) for row in links.iter_rows()])
    transcripts = _transcript_rows(frame, source_format)
    if not transcripts:
        # Bacteria-style sources: CDS rows hang directly off their gene; the
        # longest single CDS row is the feature (ties break alphabetically).
        best_per_gene: dict[str, tuple[int, str]] = {}
        for (
            group_key,
            length,
            cds_id,
            transcript_id,
            gene_id,
            attributes,
        ) in links.iter_rows():
            if not length or int(length) <= 0:
                continue
            if source_format == "gff3":
                gene_key = group_key
                name = cds_id
            else:
                gene_key = gene_id
                name = transcript_id or cds_id
            if not gene_key or not name:
                continue
            gene = genes.get(str(gene_key))
            if gene is None:
                continue
            candidate = (int(length), str(name))
            held = best_per_gene.get(str(gene_key))
            if (
                held is None
                or candidate[0] > held[0]
                or (candidate[0] == held[0] and candidate[1] < held[1])
            ):
                best_per_gene[str(gene_key)] = candidate
        rows = []
        for gene_key, (_weight, name) in best_per_gene.items():
            gene = genes.get(gene_key)
            if gene is not None:
                rows.append(_bed_row(gene, name))
        return _sorted_bed_rows(rows)
    per_gene: dict[str, dict[str, Any]] = {}
    for transcript in transcripts:
        gene_key = str(transcript["gene_ref"])
        weight = totals.get(transcript["id"], 0)
        holder = per_gene.get(gene_key)
        if (
            holder is None
            or weight > holder["weight"]
            or (weight == holder["weight"] and transcript["id"] < holder["id"])
        ):
            per_gene[gene_key] = {
                "id": transcript["id"],
                "weight": weight,
                "row": transcript,
            }
    rows = []
    for gene_key, holder in per_gene.items():
        gene = genes.get(gene_key)
        if gene is not None:
            rows.append(
                _bed_row(gene, _name_for(holder["row"], name_attr, source_format))
            )
    return _sorted_bed_rows(rows)
    per_gene: dict[str, dict[str, Any]] = {}
    for transcript in transcripts:
        gene_key = str(transcript["gene_ref"])
        weight = totals.get(transcript["id"], 0)
        holder = per_gene.get(gene_key)
        if (
            holder is None
            or weight > holder["weight"]
            or (weight == holder["weight"] and transcript["id"] < holder["id"])
        ):
            per_gene[gene_key] = {
                "id": transcript["id"],
                "weight": weight,
                "row": transcript,
            }
    rows = []
    for gene_key, holder in per_gene.items():
        gene = genes.get(gene_key)
        if gene is not None:
            rows.append(
                _bed_row(gene, _name_for(holder["row"], name_attr, source_format))
            )
    return _sorted_bed_rows(rows)


def _longest_rows_by_span(
    frame: pl.LazyFrame, source_format: str, name_attr: str | None
) -> list[dict[str, Any]]:
    transcripts = _transcript_rows(frame, source_format)
    if not transcripts:
        return _longest_rows_by_cds(frame, source_format, name_attr)
    per_gene: dict[str, dict[str, Any]] = {}
    for transcript in transcripts:
        gene_key = str(transcript["gene_ref"])
        weight = int(transcript["end"]) - int(transcript["start"]) + 1
        holder = per_gene.get(gene_key)
        if (
            holder is None
            or weight > holder["weight"]
            or (weight == holder["weight"] and transcript["id"] < holder["id"])
        ):
            per_gene[gene_key] = {
                "row": transcript,
                "weight": weight,
                "_gene_placeholder": None,
            }
    gene_frame = frame.filter(pl.col("type") == "gene").select(
        ["seqid", "start", "end", "score", "strand", "gene_id", "id"]
    )
    genes = _gene_rows(gene_frame.collect(), source_format)
    rows = []
    for gene_key, holder in per_gene.items():
        gene = genes.get(gene_key)
        if gene is not None:
            rows.append(
                _bed_row(gene, _name_for(holder["row"], name_attr, source_format))
            )
    return _sorted_bed_rows(rows)


def _sorted_bed_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (row["chrom"], row["chromStart"], row["chromEnd"], row["name"]),
    )


def feature_bed_rows(frame: pl.LazyFrame, *, limit: int = 0) -> list[dict[str, Any]]:
    """Plain BED rows (zero-based start, blank name for parent-less rows).

    BED is a tool coordinate format: no spreadsheet formula escaping is
    applied (``features query --format tsv`` keeps its ODR-83 contract).
    """
    frame = frame.select(["seqid", "start", "end", "score", "strand", "id"])
    if limit and limit > 0:
        frame = frame.limit(limit)
    rows = []
    for row in frame.collect().iter_rows(named=True):
        score = 0 if row["score"] is None else float(row["score"])
        rows.append(
            {
                "chrom": row["seqid"],
                "chromStart": int(row["start"]) - 1,
                "chromEnd": int(row["end"]),
                "name": row["id"] or "",
                "score": score,
                "strand": row["strand"] or ".",
            }
        )
    return rows


def feature_bins_rows(
    frame: pl.LazyFrame, *, bin_size: int, types: list[str]
) -> list[dict[str, Any]]:
    """Per-(seqid, bin) feature counts and density, one bin per row.

    Bins are 1-based genomic blocks (``bin_start = k*bin_size + 1``); the
    two coordinate columns report the min start / max end actually occupied
    by features inside the bin, mirroring the Genevue gene-density layout.
    """
    selected = frame.filter(pl.col("type").is_in(sorted(set(types))))
    aggregated = (
        selected.with_columns(((pl.col("start") - 1) // bin_size).alias("bin"))
        .group_by(["seqid", "bin"])
        .agg(
            pl.len().alias("count"),
            pl.col("start").min().alias("feature_start_min"),
            pl.col("end").max().alias("feature_end_max"),
        )
        .sort(["seqid", "bin"])
        .collect()
    )
    return [
        {
            "seqid": row["seqid"],
            "bin_start": int(row["bin"]) * bin_size + 1,
            "bin_end": (int(row["bin"]) + 1) * bin_size,
            "count": int(row["count"]),
            "feature_start_min": int(row["feature_start_min"]),
            "feature_end_max": int(row["feature_end_max"]),
            "features_per_bp": int(row["count"]) / bin_size,
        }
        for row in aggregated.to_dicts()
    ]
