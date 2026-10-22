#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Flattened GFF3/GTF feature index: build, identity, query, and CLI."""

from __future__ import annotations

import inspect
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

_EXPECTED_FEATURES__ALL = [
    "FEATURES_CACHE_FORMAT",
    "FEATURES_SCHEMA",
    "FEATURE_FIELDS",
    "FEATURE_INDEX_DIR_SUFFIX",
    "FEATURE_ROW_GROUP_ROWS",
    "QUERY_DEFAULT_COLUMNS",
    "SUPPORTED_SOURCE_FORMATS",
    "TRANSCRIPT_TYPES",
    "AttributeFilter",
    "apply_feature_filters",
    "build_features_index",
    "ensure_features_index",
    "feature_ancestor_expr",
    "feature_bed_rows",
    "feature_bins_rows",
    "feature_descendant_expr",
    "feature_longest_rows",
    "feature_query_rows",
    "feature_stats",
    "features_index_dir",
    "features_scan",
]


def test_all_pins_the_public_surface():
    """``__all__`` is the deliberate facade: every name resolves and the list
    matches the pinned contract (RUF022 ordering)."""
    assert features_module.__all__ == _EXPECTED_FEATURES__ALL
    for name in features_module.__all__:
        assert hasattr(features_module, name), name
    for name, value in vars(features_module).items():
        if name.startswith("_"):
            continue
        if inspect.getmodule(value) is features_module:
            assert name in features_module.__all__, name


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
    assert meta["cache_format"] == "operon-features-v2"
    assert meta["sha256"] == record["sha256"].lower()
    assert directory == features_index_dir(project, record)
    assert directory.name.endswith(".v2")
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
    # The v2 flattener materializes the structural identity GTF implies:
    # transcript rows alias transcript_id with parent = gene_id; the CDS row
    # carries no own id and points at its transcript.
    assert rows[0]["id"] == "t1" and rows[0]["parent"] == "A"
    assert rows[1]["id"] is None and rows[1]["parent"] == "t1"
    assert rows[0]["phase"] == "." and rows[1]["phase"] == "0"


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


GFF3_HIERARCHY_TEXT = (
    "##gff-version 3\n"
    "chr1\tncbi\tgene\t100\t300\t.\t+\t.\tID=g1;Name=g1\n"
    "chr1\tncbi\tmRNA\t100\t300\t.\t+\t.\tID=t1;Parent=g1;protein_id=P1\n"
    "chr1\tncbi\tCDS\t100\t150\t.\t+\t0\tID=c1a;Parent=t1\n"
    "chr1\tncbi\tCDS\t200\t300\t.\t+\t0\tID=c1b;Parent=t1\n"
    "chr1\tncbi\texon\t100\t300\t.\t+\t.\tID=e1;Parent=t1\n"
    "chr1\tncbi\tmRNA\t100\t400\t.\t+\t.\tID=t2;Parent=g1;protein_id=P2\n"
    "chr1\tncbi\tCDS\t100\t120\t.\t+\t0\tID=c2;Parent=t2\n"
    "chr2\tncbi\tgene\t500\t700\t.\t-\t.\tID=g2\n"
)

GFF3_NO_MRNA_TEXT = (
    "##gff-version 3\n"
    "chr1\tncbi\tgene\t10\t40\t.\t+\t.\tID=gb1\n"
    "chr1\tncbi\tCDS\t10\t25\t.\t+\t0\tID=cb1;Parent=gb1\n"
    "chr1\tncbi\tgene\t50\t80\t.\t+\t.\tID=gb2\n"
    "chr1\tncbi\tCDS\t50\t60\t.\t+\t0\tID=cb2a;Parent=gb2\n"
    "chr1\tncbi\tCDS\t68\t80\t.\t+\t0\tID=cb2b;Parent=gb2\n"
)

GTF_HIERARCHY_TEXT = (
    "##gt-version 3\n"
    'chr1\tsrc\tgene\t100\t300\t.\t+\t.\tgene_id "G1";\n'
    'chr1\tsrc\ttranscript\t100\t300\t.\t+\t.\tgene_id "G1"; transcript_id "T1"; protein_id "P1";\n'
    'chr1\tsrc\tCDS\t100\t150\t.\t+\t0\tgene_id "G1"; transcript_id "T1";\n'
    'chr1\tsrc\tCDS\t200\t300\t.\t+\t0\tgene_id "G1"; transcript_id "T1";\n'
    'chr1\tsrc\ttranscript\t100\t400\t.\t+\t.\tgene_id "G1"; transcript_id "T2";\n'
    'chr1\tsrc\tCDS\t100\t120\t.\t+\t0\tgene_id "G1"; transcript_id "T2";\n'
    'chr2\tsrc\tgene\t500\t700\t.\t-\t.\tgene_id "G2";\n'
    'chr2\tsrc\ttranscript\t500\t700\t.\t-\t.\tgene_id "G2"; transcript_id "T3";\n'
)

_BED_COLUMNS = ["chrom", "chromStart", "chromEnd", "name", "score", "strand"]


def _bed_lines(text: str) -> list[list[str]]:
    return [line.split("\t") for line in text.strip().splitlines() if line.strip()]


def _add_annotation(project, db, tmp_path: Path, name: str, text: str) -> dict:
    source = tmp_path / name
    source.write_text(text, encoding="utf-8")
    return ingest_file(
        db, project, source, "annotation", "ANN_000001", "annotation_gff3"
    )


def test_bed_cli_by_type(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_annotation(project, db, tmp_path, "hier.gff3", GFF3_HIERARCHY_TEXT)
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "bed",
                "--file-id",
                record["file_id"],
                "--type",
                "gene",
            ]
        )
        == 0
    )
    lines = _bed_lines(capsys.readouterr().out)
    assert lines == [
        ["chr1", "99", "300", "g1", "0", "+"],
        ["chr2", "499", "700", "g2", "0", "-"],
    ]
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "bed",
                "--file-id",
                record["file_id"],
                "--type",
                "mRNA",
                "--limit",
                "1",
            ]
        )
        == 0
    )
    assert _bed_lines(capsys.readouterr().out) == [
        ["chr1", "99", "300", "t1", "0", "+"],
    ]


def test_bed_cli_writes_zero_rows_exit_and_output_path(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_annotation(project, db, tmp_path, "hier.gff3", GFF3_HIERARCHY_TEXT)
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "bed",
                "--file-id",
                record["file_id"],
                "--type",
                "antenna",
            ]
        )
        == 1
    )
    assert capsys.readouterr().out == ""
    target = tmp_path / "out.bed"
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "bed",
                "--file-id",
                record["file_id"],
                "--type",
                "exon",
                "--output",
                str(target),
            ]
        )
        == 0
    )
    assert target.read_text(encoding="utf-8") == "chr1\t99\t300\te1\t0\t+\n"


def test_bed_cli_does_not_escape_formula_triggers(project_db, tmp_path, capsys):
    """BED is a coordinate tool format: raw cells, unlike ``query`` TSV."""
    project, db = project_db
    record = _add_annotation(
        project,
        db,
        tmp_path,
        "formula.gff3",
        "##gff-version 3\n=1+1\tncbi\tgene\t10\t20\t.\t+\t.\tID=g1\n",
    )
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "bed",
                "--file-id",
                record["file_id"],
            ]
        )
        == 0
    )
    assert _bed_lines(capsys.readouterr().out) == [
        ["=1+1", "9", "20", "g1", "0", "+"],
    ]


def test_bed_cli_longest_transcript_ranks_and_names(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_annotation(project, db, tmp_path, "hier.gff3", GFF3_HIERARCHY_TEXT)
    ours = [
        "--project",
        str(project.root),
        "features",
        "bed",
        "--file-id",
        record["file_id"],
        "--longest-transcript",
    ]
    # CDS measure: t1 carries 152 bases of CDS (152 > 21), t2 wins only by span.
    assert main(ours) == 0
    assert _bed_lines(capsys.readouterr().out) == [
        ["chr1", "99", "300", "t1", "0", "+"],
    ]
    # Span measure uses the transcript's own coordinates: t2 (301) beats t1.
    assert main(ours + ["--measure", "mRNA"]) == 0
    assert _bed_lines(capsys.readouterr().out) == [
        ["chr1", "99", "300", "t2", "0", "+"],
    ]
    # protein_id of the winning transcript names the row instead of its id.
    assert main(ours + ["--measure", "mRNA", "--name-attr", "protein_id"]) == 0
    assert _bed_lines(capsys.readouterr().out) == [
        ["chr1", "99", "300", "P2", "0", "+"],
    ]


def test_bed_cli_longest_transcript_without_mrnas_falls_back(
    project_db, tmp_path, capsys
):
    project, db = project_db
    record = _add_annotation(project, db, tmp_path, "prok.gff3", GFF3_NO_MRNA_TEXT)
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "bed",
                "--file-id",
                record["file_id"],
                "--longest-transcript",
            ]
        )
        == 0
    )
    assert _bed_lines(capsys.readouterr().out) == [
        ["chr1", "9", "40", "cb1", "0", "+"],
        ["chr1", "49", "80", "cb2b", "0", "+"],
    ]


def test_bed_cli_longest_transcript_on_gtf(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_annotation(project, db, tmp_path, "hier.gtf", GTF_HIERARCHY_TEXT)
    ours = [
        "--project",
        str(project.root),
        "features",
        "bed",
        "--file-id",
        record["file_id"],
        "--longest-transcript",
    ]
    # GTF transcripts reference genes via the gene_id attribute; the gene row
    # supplies chrom coordinates, score and strand.
    assert main(ours) == 0
    assert _bed_lines(capsys.readouterr().out) == [
        ["chr1", "99", "300", "T1", "0", "+"],
        ["chr2", "499", "700", "T3", "0", "-"],
    ]
    assert main(ours + ["--measure", "mRNA"]) == 0
    assert _bed_lines(capsys.readouterr().out) == [
        ["chr1", "99", "300", "T2", "0", "+"],
        ["chr2", "499", "700", "T3", "0", "-"],
    ]
    assert main(ours + ["--name-attr", "protein_id"]) == 0
    assert _bed_lines(capsys.readouterr().out) == [
        ["chr1", "99", "300", "P1", "0", "+"],
        ["chr2", "499", "700", "T3", "0", "-"],
    ]


def test_traversal_gff3_parents_and_children(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_annotation(project, db, tmp_path, "hier.gff3", GFF3_HIERARCHY_TEXT)
    ours = [
        "--project",
        str(project.root),
        "features",
        "parents",
        "--file-id",
        record["file_id"],
    ]
    # CDS/exon -> mRNA -> gene: the gene row is the terminal answer.
    assert main(ours + ["--id", "e1"]) == 0
    text = capsys.readouterr().out.strip().splitlines()
    assert len(text) == 2 and text[1].split("\t")[7] == "g1"
    assert main(ours + ["--id", "t1", "--until", "gene"]) == 0
    assert capsys.readouterr().out.strip().splitlines()[1].split("\t")[7] == "g1"
    # An unknown id yields no rows and fails when explicitly selected.
    assert main(ours + ["--id", "nosuch"]) == 1
    capsys.readouterr()
    # children: direct mRNAs only; --type filters the reachable set in this
    # mode (exons hang off the transcripts, so the gene's direct children
    # have none), and --recursive covers all levels.
    walk = main(
        [
            "--project",
            str(project.root),
            "features",
            "children",
            "--file-id",
            record["file_id"],
            "--id",
            "g1",
        ]
    )
    assert walk == 0
    rows = capsys.readouterr().out.strip().splitlines()[1:]
    assert [row.split("\t")[7] for row in rows] == ["t1", "t2"]
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "children",
                "--file-id",
                record["file_id"],
                "--id",
                "g1",
                "--type",
                "exon",
            ]
        )
        == 1
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "children",
                "--file-id",
                record["file_id"],
                "--id",
                "g1",
                "--type",
                "exon",
                "--recursive",
            ]
        )
        == 0
    )
    rows = capsys.readouterr().out.strip().splitlines()[1:]
    assert [row.split("\t")[7] for row in rows] == ["e1"]
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "children",
                "--file-id",
                record["file_id"],
                "--id",
                "g1",
                "--recursive",
            ]
        )
        == 0
    )
    rows = capsys.readouterr().out.strip().splitlines()[1:]
    # Row order follows the flattened file; the recursive sweep visits all
    # six child levels: mRNAs, then their exons and CDS segments.
    assert [row.split("\t")[7] for row in rows] == [
        "t1",
        "c1a",
        "c1b",
        "e1",
        "t2",
        "c2",
    ]


def test_traversal_gtf_parents_and_children(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_annotation(project, db, tmp_path, "hier.gtf", GTF_HIERARCHY_TEXT)
    # parents --until gene resolves the gene row by its gene_id attribute.
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "parents",
                "--file-id",
                record["file_id"],
                "--id",
                "T1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)["files"][0]
    (row,) = document["rows"]
    # v2 fills the GTF gene row's own id with its gene_id alias.
    assert row["type"] == "gene" and row["gene_id"] == "G1" and row["id"] == "G1"
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "children",
                "--file-id",
                record["file_id"],
                "--id",
                "G1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)["files"][0]
    assert [row["type"] for row in document["rows"]] == ["transcript", "transcript"]
    assert {row["transcript_id"] for row in document["rows"]} == {"T1", "T2"}
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "children",
                "--file-id",
                record["file_id"],
                "--id",
                "G1",
                "--recursive",
                "--format",
                "json",
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)["files"][0]
    # Row order follows the flattened file; compare as a multiset.
    assert sorted(row["type"] for row in document["rows"]) == [
        "CDS",
        "CDS",
        "CDS",
        "transcript",
        "transcript",
    ]
    assert "T2" in {row["transcript_id"] for row in document["rows"]}
    # An empty scrambled traversal also reports no-row failure explicitly.
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "parents",
                "--file-id",
                record["file_id"],
                "--id",
                "G_nonexistent",
            ]
        )
        == 1
    )


def test_query_cli_attribute_filters(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_annotation(project, db, tmp_path, "hier.gff3", GFF3_HIERARCHY_TEXT)
    ours = [
        "--project",
        str(project.root),
        "features",
        "query",
        "--file-id",
        record["file_id"],
    ]
    assert main(ours + ["--attribute", "Name=g1"]) == 0
    rows = capsys.readouterr().out.strip().splitlines()[1:]
    assert [row.split("\t")[7] for row in rows] == ["g1"]
    # Flattened keys short-circuit: ID/gene_id/transcript_id/parent columns
    # carry decoded values, so regex matching lands there.
    assert main(ours + ["--attribute", "ID~=^t\\d"]) == 0
    rows = capsys.readouterr().out.strip().splitlines()[1:]
    assert [row.split("\t")[7] for row in rows] == ["t1", "t2"]
    # The repeated --attribute tokens join with OR; other column filters
    # (e.g. --type) stay AND-composed with the attribute group.
    assert main(ours + ["--attribute", "ID~=^t\\d", "--attribute", "ID~=^e1$"]) == 0
    rows = capsys.readouterr().out.strip().splitlines()[1:]
    assert sorted([row.split("\t")[7] for row in rows]) == ["e1", "t1", "t2"]
    # A malformed token fails with validation guidance.
    assert main(ours + ["--attribute", "nonsense"]) == 2


def test_query_cli_attribute_filters_on_gtf(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_annotation(project, db, tmp_path, "hier.gtf", GTF_HIERARCHY_TEXT)
    ours = [
        "--project",
        str(project.root),
        "features",
        "query",
        "--file-id",
        record["file_id"],
    ]
    assert main(ours + ["--attribute", "gene_id=G1"]) == 0
    rows = capsys.readouterr().out.strip().splitlines()[1:]
    counts = [row.split("\t")[9] for row in rows]
    # gene G1 row plus its transcripts/CDS rows carry the gene_id column key.
    assert counts.count("G1") == 6
    assert main(ours + ["--attribute", "protein_id~=P\\d"]) == 0
    rows = capsys.readouterr().out.strip().splitlines()[1:]
    assert len(rows) == 1 and rows[0].split("\t")[10] == "T1"


def test_bins_cli_counts_and_density(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_annotation(project, db, tmp_path, "hier.gff3", GFF3_HIERARCHY_TEXT)
    ours = [
        "--project",
        str(project.root),
        "features",
        "bins",
        "--file-id",
        record["file_id"],
    ]
    assert main(ours) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].split("\t") == [
        "seqid",
        "bin_start",
        "bin_end",
        "count",
        "feature_start_min",
        "feature_end_max",
        "features_per_bp",
    ]
    # The default bin (100kb) puts both genes into their own first bins.
    assert [row.split("\t") for row in lines[1:]] == [
        ["chr1", "1", "100000", "1", "100", "300", "1e-05"],
        ["chr2", "1", "100000", "1", "500", "700", "1e-05"],
    ]
    # Feature coordinates land in the 1-based bin that contains their start:
    # --bin-size 100 puts chr1's three 100-starting features (gene g1 and the
    # two mRNAs t1/t2) into bin 0, and chr2's g2 (start 500) into bin 4.
    assert main(ours + ["--bin-size", "100", "--type", "gene", "--type", "mRNA"]) == 0
    rows = [row.split("\t") for row in capsys.readouterr().out.strip().splitlines()[1:]]
    assert rows == [
        ["chr1", "1", "100", "3", "100", "400", "0.03"],
        ["chr2", "401", "500", "1", "500", "700", "0.01"],
    ]


def test_bins_cli_json(project_db, tmp_path, capsys):
    project, db = project_db
    record = _add_annotation(project, db, tmp_path, "hier.gff3", GFF3_HIERARCHY_TEXT)
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "bins",
                "--file-id",
                record["file_id"],
                "--format",
                "json",
            ]
        )
        == 0
    )
    (document,) = json.loads(capsys.readouterr().out)["files"]
    assert document["bin_size"] == 100_000 and document["returned"] == 2
    assert document["bins"][0]["seqid"] == "chr1" and document["bins"][0]["count"] == 1
    assert document["bins"][0]["features_per_bp"] == 1 / 100_000


def test_bed_cli_longest_transcript_keeps_parentless_genes(
    project_db, tmp_path, capsys
):
    """A gene with no transcripts at all is not emitted by --longest-transcript."""
    project, db = project_db
    record = _add_annotation(
        project,
        db,
        tmp_path,
        "hier-with-gaps.gff3",
        GFF3_HIERARCHY_TEXT + "chr3\tncbi\tgene\t900\t950\t.\t+\t.\tID=g3\n",
    )
    assert (
        main(
            [
                "--project",
                str(project.root),
                "features",
                "bed",
                "--file-id",
                record["file_id"],
                "--longest-transcript",
            ]
        )
        == 0
    )
    chroms = [cells[0] for cells in _bed_lines(capsys.readouterr().out)]
    assert "chr3" not in chroms
    assert "chr1" in chroms and "chr2" not in chroms
