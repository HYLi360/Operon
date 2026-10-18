#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Flattened GFF3/GTF feature index: build, identity, query, and CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from operon import features as features_module
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import FeatureIndexError
from operon.features import (
    FEATURE_FIELDS,
    QUERY_DEFAULT_COLUMNS,
    apply_feature_filters,
    build_features_index,
    feature_query_rows,
    features_index_dir,
    features_scan,
)
from operon.files import ingest_file

GFF3_TEXT = (
    "##gff-version 3\n"
    "chr1\tncbi\tgene\t100\t300\t.\t+\t.\tID=g1;Name=g1\n"
    "chr1\tncbi\tmRNA\t100\t300\t.\t+\t.\tID=t1;Parent=g1\n"
    "chr1\tncbi\texon\t120\t180\t.\t+\t.\tID=e1;Parent=t1\n"
    "chr2\tncbi\texon\t200\t250\t12.5\t-\t.\tID=e2;Parent=t2\n"
)

GTF_TEXT = (
    'chr1\tsource\ttranscript\t50\t120\t.\t+\t.\tgene_id "A"; transcript_id "t1"\n'
    'chr1\tsource\tCDS\t60\t100\t.\t+\t0\tgene_id "A"; transcript_id "t1"\n'
)

FASTA_TAIL = ">chr1\nACGT\n"


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row(
        "organisms", {"organism_id": "ORG_000001", "scientific_name": "Example"}
    )
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row(
        "assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"}
    )
    db.insert_row(
        "annotations", {"annotation_id": "ANN_000001", "assembly_id": "ASM_000001"}
    )
    try:
        yield project, db
    finally:
        db.close()


def _add_gff3(project, db, tmp_path: Path, *, compress_archive=None):
    source = tmp_path / "ann.gff3"
    source.write_text(GFF3_TEXT, encoding="utf-8")
    kwargs = (
        {"compress_archive": True}
        if compress_archive is None
        else {"compress_archive": compress_archive}
    )
    return ingest_file(
        db, project, source, "annotation", "ANN_000001", "annotation_gff3", **kwargs
    )


def _record(db, file_id: str) -> dict:
    return dict(db.query("SELECT * FROM files WHERE file_id=?", (file_id,))[0])


def _rows(frame, filters=None):
    filtered = apply_feature_filters(frame, **(filters or {}))
    return feature_query_rows(filtered, limit=0, include_attributes=False)


def test_roundtrip_on_a_bgzip_archive(project_db, tmp_path):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    directory, meta = features_module.ensure_features_index(project, record)
    assert meta["status"] == "built"
    assert meta["feature_count"] == 4
    assert meta["cache_format"] == "operon-features-v1"
    assert meta["sha256"] == record["sha256"].lower()
    assert directory == features_index_dir(project, record)
    assert directory.name.endswith(".v1")
    assert (directory / "data.parquet").is_file()
    assert (directory / "meta.json").is_file()
    # The second visit reuses the index (hit).
    _, meta_hit = features_module.ensure_features_index(project, record)
    assert meta_hit["status"] == "hit"
    assert meta_hit["sha256"] == record["sha256"].lower()

    frame, _meta = features_scan(project, record)
    rows = _rows(frame)
    assert [row["id"] for row in rows] == ["g1", "t1", "e1", "e2"]
    assert rows[0]["parent"] is None and rows[1]["parent"] == "g1"
    assert rows[2]["phase"] == "." and rows[2]["strand"] == "+"
    full_rows = feature_query_rows(
        apply_feature_filters(frame), limit=0, include_attributes=True
    )
    assert full_rows[2]["score"] is None and full_rows[3]["score"] == 12.5
    assert list(full_rows[0]) == [name for name, _ in FEATURE_FIELDS]
    assert [row["line_number"] for row in rows] == [2, 3, 4, 5]
    assert list(rows[0]) == list(QUERY_DEFAULT_COLUMNS)


def test_region_overlap_and_column_filters(project_db, tmp_path):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    frame, _meta = features_scan(project, record)
    overlap = _rows(frame, {"region_start": 100, "region_end": 190})
    assert [row["id"] for row in overlap] == ["g1", "t1", "e1"]
    right_open = _rows(frame, {"region_start": 260})
    assert [row["id"] for row in right_open] == ["g1", "t1"]
    left_open = _rows(frame, {"region_end": 99})
    assert left_open == []
    genes = _rows(frame, {"types": ["gene"]})
    assert [row["type"] for row in genes] == ["gene"]
    both = _rows(frame, {"seqids": ["chr2"], "strands": ["-"]})
    assert [row["id"] for row in both] == ["e2"]


def test_gtf_dialect_flattens_quoted_attributes(project_db, tmp_path):
    project, db = project_db
    source = tmp_path / "ann.gtf"
    source.write_text(GTF_TEXT, encoding="utf-8")
    record = ingest_file(
        db, project, source, "annotation", "ANN_000001", "annotation_gff3"
    )
    assert record["format"] == "gtf" and record["compression"] == "bgzip"
    _directory, meta = features_module.ensure_features_index(project, record)
    assert meta["feature_count"] == 2 and meta["source_format"] == "gtf"
    frame, _meta = features_scan(project, record)
    rows = _rows(frame)
    assert rows[0]["gene_id"] == "A" and rows[0]["transcript_id"] == "t1"
    assert rows[0]["phase"] == "." and rows[1]["phase"] == "0"
    assert rows[0]["id"] is None and rows[0]["parent"] is None


def test_fasta_tail_terminates_the_stream(project_db, tmp_path):
    project, db = project_db
    source = tmp_path / "embedded.gff3"
    source.write_text(GFF3_TEXT + "##FASTA\n" + FASTA_TAIL, encoding="utf-8")
    record = ingest_file(
        db, project, source, "annotation", "ANN_000001", "annotation_gff3"
    )
    _directory, meta = features_module.ensure_features_index(project, record)
    assert meta["feature_count"] == 4


def test_malformed_row_aborts_and_leaves_no_commit_marker(project_db, tmp_path):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    with pytest.raises(FeatureIndexError, match="2 tab-separated columns"):
        broken = dict(record)
        broken_path = project.root / "broken.gff3"
        broken_path.write_text(GFF3_TEXT + "chr1\tncbi\n", encoding="utf-8")
        broken["file_id"] = record["file_id"]
        broken["relative_path"] = str(broken_path.relative_to(project.root))
        build_features_index(project, broken)
    directory = features_index_dir(project, record)
    assert not (directory / "meta.json").exists()
    assert not list(directory.glob(".*.tmp"))
    # Fix the source and build: no stale artifacts get in the way.
    source_path = project.root / record["relative_path"]
    source_path.write_text(GFF3_TEXT, encoding="utf-8")
    _directory, meta = features_module.ensure_features_index(project, record)
    assert meta["status"] == "rebuilt" and meta["feature_count"] == 4


def test_bad_coordinates_aborts(project_db, tmp_path):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    bad_path = project.root / "coords.gff3"
    bad_path.write_text("chrA\tncbi\tgene\t300\t100\t.\t+\t.\tID=b\n", encoding="utf-8")
    broken = dict(record)
    broken["relative_path"] = str(bad_path.relative_to(project.root))
    with pytest.raises(FeatureIndexError, match="invalid coordinates"):
        build_features_index(project, broken)


def test_unscored_text_aborts(project_db, tmp_path):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    bad_path = project.root / "score.gff3"
    bad_path.write_text(
        "chrA\tncbi\tgene\t10\t20\tnanish\t+\t.\tID=b\n", encoding="utf-8"
    )
    broken = dict(record)
    broken["relative_path"] = str(bad_path.relative_to(project.root))
    with pytest.raises(FeatureIndexError, match="invalid score"):
        build_features_index(project, broken)


def test_tampered_meta_is_rebuilt(project_db, tmp_path):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    directory, meta = features_module.ensure_features_index(project, record)
    assert meta["status"] == "built"
    meta_path = directory / "meta.json"
    meta_path.write_text(
        json.dumps(
            json.loads(meta_path.read_text(encoding="utf-8")) | {"sha256": "0" * 64}
        ),
        encoding="utf-8",
    )
    _directory, rebuilt = features_module.ensure_features_index(project, record)
    assert rebuilt["status"] == "rebuilt"
    assert rebuilt["sha256"] == record["sha256"].lower()


def test_corrupt_meta_is_rebuilt(project_db, tmp_path):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    directory, _meta = features_module.ensure_features_index(project, record)
    (directory / "meta.json").write_text("not-json\n", encoding="utf-8")
    _directory, rebuilt = features_module.ensure_features_index(project, record)
    assert rebuilt["status"] == "rebuilt"


def test_rebuild_flag_replaces_a_valid_index(project_db, tmp_path):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    _directory, first = features_module.ensure_features_index(project, record)
    assert first["status"] == "built"
    _directory, again = features_module.ensure_features_index(
        project, record, rebuild=True
    )
    assert again["status"] == "rebuilt" and again["feature_count"] == 4


def test_missing_and_unsupported_sources_raise(project_db, tmp_path):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    absent = dict(record)
    absent["relative_path"] = "raw/annotations/ANN_000001/missing.gff3"
    with pytest.raises(FeatureIndexError, match="local bytes absent"):
        build_features_index(project, absent)
    wrong_format = dict(record)
    wrong_format["format"] = "bam"
    with pytest.raises(FeatureIndexError, match="no flattened feature index"):
        build_features_index(project, wrong_format)


def test_query_cli_filters_regions_and_json(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    arguments = [
        "--project",
        str(project.root),
        "features",
        "query",
        "--file-id",
        record["file_id"],
        "--seqid",
        "chr1",
        "--start",
        "150",
        "--end",
        "200",
    ]
    assert main(arguments) == 0
    text = capsys.readouterr().out
    lines = text.strip().splitlines()
    assert lines[0].split("\t") == list(QUERY_DEFAULT_COLUMNS)
    assert [row.split("\t")[7] for row in lines[1:]] == ["g1", "t1", "e1"]

    assert main(arguments[:6] + ["--format", "json", "--limit", "2"]) == 0
    payload = json.loads(capsys.readouterr().out)
    (document,) = payload["files"]
    assert document["returned"] == 2 and document["index_feature_count"] == 4
    assert "attributes" in document["rows"][0]
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "query",
                "--file-id",
                "FIL_999999",
            ]
        )
        == 2
    )


def test_query_cli_entity_selection_and_rebuild(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "query",
                "--entity-type",
                "annotation",
                "--entity-id",
                "ANN_000001",
                "--type",
                "exon",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    exon_rows = [row for row in text.strip().splitlines()[1:]]
    assert len(exon_rows) == 2
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "query",
                "--file-id",
                record["file_id"],
                "--rebuild",
            ]
        )
        == 0
    )
    capsys.readouterr()


def test_query_cli_skips_unusable_selection(project_db, tmp_path, capsys):
    project, db = project_db
    _add_gff3(project, db, tmp_path)
    source = tmp_path / "notes.txt"
    source.write_text("notes\n", encoding="utf-8")
    ingest_file(db, project, source, "annotation", "ANN_000001", "other")
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "query",
                "--entity-type",
                "annotation",
                "--entity-id",
                "ANN_000001",
                "--limit",
                "1",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "not an annotation" in captured.err
    assert captured.out.count("\n") == 2  # header + one row


def test_query_cli_remote_only_explicit_selection(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    (project.root / record["relative_path"]).unlink()
    db.conn.execute(
        "UPDATE files SET status='REMOTE_ONLY' WHERE file_id=?", (record["file_id"],)
    )
    db.conn.commit()
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "query",
                "--file-id",
                record["file_id"],
            ]
        )
        == 1
    )
    assert "remote-only" in capsys.readouterr().err


def test_stats_cli_counts(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_gff3(project, db, tmp_path)
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "stats",
                "--file-id",
                record["file_id"],
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "features: 4" in text
    assert "## by type" in text
    assert "## by source" in text and "## by seqid" in text
    assert "gene" in text and "chr2" in text

    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "stats",
                "--file-id",
                record["file_id"],
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    (document,) = payload["files"]
    assert document["feature_count"] == 4
    assert {"seqid": "chr1", "count": 3, "min_start": 100, "max_end": 300} in [
        {
            "seqid": row["seqid"],
            "count": row["count"],
            "min_start": row["min_start"],
            "max_end": row["max_end"],
        }
        for row in document["by_seqid"]
    ]
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "stats",
                "--file-id",
                "FIL_999999",
            ]
        )
        == 2
    )


@pytest.mark.bug("ODR-83")
def test_query_cli_escapes_formula_trigger_cells(project_db, tmp_path, capsys):
    """A TSV cell beginning with a spreadsheet formula trigger is escaped."""
    project, db = project_db
    source = tmp_path / "formula.gff3"
    source.write_text(
        "##gff-version 3\n=1+1\tncbi\tgene\t10\t20\t.\t+\t.\tID=g1;Name=g1\n",
        encoding="utf-8",
    )
    record = ingest_file(
        db, project, source, "annotation", "ANN_000001", "annotation_gff3"
    )
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "query",
                "--file-id",
                record["file_id"],
            ]
        )
        == 0
    )
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[1].split("\t")[0] == "'=1+1"
