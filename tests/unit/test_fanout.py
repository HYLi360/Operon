"""Unit tests for the data-derived fan-out primitive (operon fanout)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ConflictError, EntityNotFoundError, ValidationError
from operon.fanout import fanout_units, parse_assignments
from operon.files import ingest_file
from operon.lifecycle import apply_lifecycle_event
from operon.tools import Recipe, candidate_files


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
    db.insert_row("annotations", {
        "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
        "annotation_source": "test", "annotation_version": 1,
    })
    proteins = tmp_path / "proteins.faa"
    proteins.write_text(
        ">p1 description here\nMPEPTIDE\n>p2\nAAAAAA\n>p3\nCCCCCC\n>p4\nDDDDDD\n",
        encoding="utf-8")
    source = ingest_file(db, project, proteins, "annotation", "ANN_000001", "protein_fasta")
    for seqid, length in (("p1", 8), ("p2", 6), ("p3", 6), ("p4", 6)):
        db.insert_row("sequences", {
            "file_id": source["file_id"], "file_sha256": source["sha256"],
            "entity_type": "annotation", "entity_id": "ANN_000001",
            "seqid": seqid, "length": length,
        })
    try:
        yield project, db, source
    finally:
        db.close()


def _register_assignments(db, project, tmp_path: Path, text: str, name: str = "assign.tsv",
                          role: str = "subfamily_assignments"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return ingest_file(
        db, project, path, "annotation", "ANN_000001", role,
        fmt="tsv", compression="none")


def _fanout(db, project, assignments, source, **overrides):
    kwargs = {
        "assignments_file_id": assignments["file_id"],
        "source_file_ids": [source["file_id"]],
        "entity_type": "annotation",
        "entity_id": "ANN_000001",
        "role_prefix": "subfamily_alignment",
        "command": "operon fanout",
    }
    kwargs.update(overrides)
    return fanout_units(db, project, **kwargs)


def _files(db) -> list[dict]:
    return [dict(row) for row in db.conn.execute("SELECT * FROM files ORDER BY file_id")]


def test_parse_assignments_columns_duplicates_and_order(tmp_path):
    path = tmp_path / "a.tsv"
    path.write_text(
        "unit\tseqid\textra\n"
        "SF02\tp3\tx\nSF01\tp1 desc\tx\nSF01\tp2\tx\nSF02\tp4\tx\n"
        "SF01\tp1\tx\nSF02\tp3\tx\n",
        encoding="utf-8")
    units, duplicates = parse_assignments(path)
    assert duplicates == 2
    # Units appear in first-appearance order; row order within a unit holds.
    assert units == [
        {"unit": "SF02", "seqids": ["p3", "p4"]},
        {"unit": "SF01", "seqids": ["p1", "p2"]},
    ]
    units, duplicates = parse_assignments(path, unit_column="extra", seqid_column="seqid")
    assert [u["unit"] for u in units] == ["x"]
    with pytest.raises(ValidationError, match="missing columns"):
        parse_assignments(path, unit_column="cluster")
    empty = tmp_path / "empty.tsv"
    empty.write_text("unit\tseqid\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="nothing to fan out"):
        parse_assignments(empty)
    bad = tmp_path / "bad.tsv"
    bad.write_text("unit\tseqid\nSF01\t\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="non-empty"):
        parse_assignments(bad)


def test_fanout_registers_units_roles_and_lineage(project_db, tmp_path):
    project, db, source = project_db
    assignments = _register_assignments(
        db, project, tmp_path, "unit\tseqid\nSF01\tp1\nSF01\tp2\nSF02\tp3\n")
    result = _fanout(db, project, assignments, source, actor="tester",
                     parent_run_id="WF_PARENT_1")
    assert result["created"] == 2 and result["reused"] == 0
    by_unit = {u["unit"]: u for u in result["units"]}
    assert by_unit["SF01"]["role"] == "subfamily_alignment:SF01"
    assert by_unit["SF01"]["sequences"] == 2
    assert by_unit["SF02"]["role"] == "subfamily_alignment:SF02"

    for unit in result["units"]:
        assert unit["relative_path"].startswith("analysis/derived/ANN_000001/")
        archived = (project.root / unit["relative_path"]).read_text(encoding="utf-8")
        headers = [line[1:] for line in archived.splitlines() if line.startswith(">")]
        expected = {"SF01": ["p1", "p2"], "SF02": ["p3"]}[unit["unit"]]
        assert headers == expected
        edges = [dict(row) for row in db.conn.execute(
            "SELECT * FROM file_lineage WHERE derived_file_id=? ORDER BY input_file_id",
            (unit["file_id"],)).fetchall()]
        assert {e["input_file_id"] for e in edges} == {
            source["file_id"], assignments["file_id"]}
        assert {e["workflow_run_id"] for e in edges} == {result["run_id"]}

    runs = [dict(row) for row in db.conn.execute(
        "SELECT * FROM workflow_runs WHERE step='fanout'").fetchall()]
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["parent_run_id"] == "WF_PARENT_1"
    details = json.loads(runs[0]["execution_details"])
    assert details["assignments_file_id"] == assignments["file_id"]
    assert [u["unit"] for u in details["units"]] == ["SF01", "SF02"]
    ingest_logs = db.conn.execute(
        "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='ingest' AND parent_run_id=?",
        (result["run_id"],)).fetchone()["n"]
    assert ingest_logs == 2


def test_fanout_idempotent_reuse_and_conflict(project_db, tmp_path):
    project, db, source = project_db
    assignments = _register_assignments(
        db, project, tmp_path, "unit\tseqid\nSF01\tp1\nSF01\tp2\n")
    first = _fanout(db, project, assignments, source)
    files_before = _files(db)
    second = _fanout(db, project, assignments, source)
    assert second["reused"] == 1 and second["created"] == 0
    assert second["units"][0]["file_id"] == first["units"][0]["file_id"]
    assert _files(db) == files_before

    # Same entity+role with different bytes is a hard conflict, raised before
    # anything is written.
    other = tmp_path / "other.faa"
    other.write_text(">zzz\nTTTT\n", encoding="utf-8")
    ingest_file(db, project, other, "annotation", "ANN_000001", "subfamily_alignment:SF09")
    assignments_sf09 = _register_assignments(
        db, project, tmp_path, "unit\tseqid\nSF09\tp3\nSF08\tp4\n", name="assign2.tsv",
        role="subfamily_assignments_v2")
    before = list(db.conn.iterdump())
    with pytest.raises(ConflictError, match="SF09"):
        _fanout(db, project, assignments_sf09, source)
    assert list(db.conn.iterdump()) == before
    assert not (project.analysis_root / "derived" / "ANN_000001"
                / "ANN_000001.subfamily_alignment:SF08.fasta").exists()


def test_fanout_unresolvable_seqids_are_all_listed(project_db, tmp_path):
    project, db, source = project_db
    assignments = _register_assignments(
        db, project, tmp_path, "unit\tseqid\nSF01\tp1\nSF01\tunknownA\nSF02\tunknownB\n")
    with pytest.raises(ValidationError, match="unknownA") as excinfo:
        _fanout(db, project, assignments, source)
    assert "unknownB" in str(excinfo.value)
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='fanout'").fetchone()["n"] == 0


def test_fanout_ambiguous_seqid_across_sources(project_db, tmp_path):
    project, db, source = project_db
    second_fasta = tmp_path / "more.faa"
    second_fasta.write_text(">p1\nMPEPTIDE\n>p9\nGGGG\n", encoding="utf-8")
    second = ingest_file(db, project, second_fasta, "annotation", "ANN_000001",
                         "protein_fasta_extra")
    for seqid, length in (("p1", 8), ("p9", 4)):
        db.insert_row("sequences", {
            "file_id": second["file_id"], "file_sha256": second["sha256"],
            "entity_type": "annotation", "entity_id": "ANN_000001",
            "seqid": seqid, "length": length,
        })
    assignments = _register_assignments(db, project, tmp_path, "unit\tseqid\nSF01\tp1\n")
    with pytest.raises(ValidationError, match="narrow --source-file"):
        _fanout(db, project, assignments, source,
                source_file_ids=[source["file_id"], second["file_id"]])
    # Narrowing to the second source resolves the ambiguity.
    result = _fanout(db, project, assignments, source,
                     source_file_ids=[second["file_id"]])
    assert result["units"][0]["sequences"] == 1


def test_fanout_dry_run_writes_nothing(project_db, tmp_path):
    project, db, source = project_db
    assignments = _register_assignments(
        db, project, tmp_path, "unit\tseqid\nSF01\tp1\nSF02\tp2\nSF02\tp3\n")
    before = list(db.conn.iterdump())
    result = _fanout(db, project, assignments, source, dry_run=True)
    assert result["dry_run"] is True
    assert result["units"] == [
        {"unit": "SF01", "role": "subfamily_alignment:SF01", "sequences": 1},
        {"unit": "SF02", "role": "subfamily_alignment:SF02", "sequences": 2},
    ]
    assert list(db.conn.iterdump()) == before
    derived = project.analysis_root / "derived"
    assert not derived.exists() or not list(derived.rglob("*"))


def test_fanout_entity_and_manifest_validation(project_db, tmp_path):
    project, db, source = project_db
    assignments = _register_assignments(db, project, tmp_path, "unit\tseqid\nSF01\tp1\n")
    with pytest.raises(EntityNotFoundError):
        _fanout(db, project, assignments, source, entity_id="ANN_999999")
    with pytest.raises(ValidationError, match="not registered"):
        _fanout(db, project, {"file_id": "FIL_999999"}, source,
                assignments_file_id="FIL_999999")
    with pytest.raises(ValidationError, match="invalid role prefix"):
        _fanout(db, project, assignments, source, role_prefix="bad/prefix")
    with pytest.raises(ValidationError, match="at least one --source-file"):
        _fanout(db, project, assignments, source, source_file_ids=[])

    apply_lifecycle_event(
        db, "annotation", "ANN_000001", action="RETIRE",
        reason_code="accidental_import", reason="test", actor="tester")
    with pytest.raises(ValidationError, match="retired"):
        _fanout(db, project, assignments, source)


def test_fanout_prefix_candidates(project_db, tmp_path):
    project, db, source = project_db
    assignments = _register_assignments(
        db, project, tmp_path, "unit\tseqid\nSF01\tp1\nSF02\tp2\n")
    result = _fanout(db, project, assignments, source)
    recipe = Recipe(
        name="subfamily_tree", tool_name="tool", description="", entity_type="annotation",
        file_role="", file_role_prefix="subfamily_alignment:", fmt="fasta",
        input_kind="file", database="", database_version="",
        output_subdir="subfamily_tree", output_kind="file", output_name_template="",
        output_suffix=".tsv", arguments=[], parameters={}, result_parser="none",
        max_hits_per_query=5, raw={},
    )
    candidates = candidate_files(db, recipe)
    assert sorted(row["file_id"] for row in candidates) == sorted(
        unit["file_id"] for unit in result["units"])
    # A bare lookalike role (no colon) must not match the prefix with colon.
    stray = tmp_path / "stray.faa"
    stray.write_text(">s1\nMM\n", encoding="utf-8")
    ingest_file(db, project, stray, "annotation", "ANN_000001", "subfamily_alignmentX")
    assert len(candidate_files(db, recipe)) == 2
    # entity_id narrowing still applies.
    assert candidate_files(db, recipe, entity_id="ANN_999999") == []


def test_fanout_cli(project_db, tmp_path, capsys):
    project, db, source = project_db
    assignments = _register_assignments(
        db, project, tmp_path, "unit\tseqid\nSF01\tp1\nSF02\tp2\n")
    assert main([
        "--project", str(tmp_path), "fanout",
        "--assignments-file", assignments["file_id"],
        "--source-file", source["file_id"],
        "--entity-type", "annotation", "--entity-id", "ANN_000001",
        "--role-prefix", "subfamily_alignment", "--dry-run",
    ]) == 0
    out = capsys.readouterr().out
    assert "subfamily_alignment:SF01" in out and "dry-run" in out
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='fanout'").fetchone()["n"] == 0

    assert main([
        "--project", str(tmp_path), "fanout",
        "--assignments-file", assignments["file_id"],
        "--source-file", source["file_id"],
        "--entity-type", "annotation", "--entity-id", "ANN_000001",
        "--role-prefix", "subfamily_alignment",
    ]) == 0
    out = capsys.readouterr().out
    assert "2 created, 0 reused" in out

    # Validation failures exit non-zero.
    assert main([
        "--project", str(tmp_path), "fanout",
        "--assignments-file", "FIL_999999",
        "--source-file", source["file_id"],
        "--entity-type", "annotation", "--entity-id", "ANN_000001",
        "--role-prefix", "subfamily_alignment",
    ]) == 2
