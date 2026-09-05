"""Unit tests for derived-artifact adoption (operon adopt / file_lineage)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.files import ingest_file
from operon.lineage import adopt_files, load_adopt_manifest
from operon.tools import Recipe, candidate_files
from operon import lineage


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
    genome = tmp_path / "genome.fa"
    genome.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
    source = ingest_file(db, project, genome, "assembly", "ASM_000001", "genome_fasta")
    try:
        yield project, db, source
    finally:
        db.close()


def _write_derived(root: Path, name: str = "matrix.tsv", content: str = "id\tvalue\nctg1\t1\n") -> Path:
    path = root / name
    path.write_text(content, encoding="utf-8")
    return path


def _item(path: Path, source: dict, **overrides) -> dict:
    item = {
        "path": str(path),
        "entity_type": "assembly",
        "entity_id": "ASM_000001",
        "role": "pangenome_matrix",
        "format": "tsv",
        "compression": "none",
        "derived_from": [source["file_id"]],
    }
    item.update(overrides)
    return item


def _lineage_rows(db: Database) -> list[dict]:
    return [dict(row) for row in db.conn.execute("SELECT * FROM file_lineage").fetchall()]


def test_adopt_single_file_registers_lineage_and_run(project_db, tmp_path):
    project, db, source = project_db
    derived = _write_derived(tmp_path)
    results = adopt_files(db, project, items=[_item(derived, source)], actor="tester")
    assert len(results) == 1
    result = results[0]
    assert result["file_id"].startswith("FIL_")
    assert result["relative_path"].startswith("analysis/adopted/ASM_000001/")
    assert result["derived_from"] == [source["file_id"]]
    stored = project.root / result["relative_path"]
    assert stored.read_bytes() == derived.read_bytes()

    edges = _lineage_rows(db)
    assert len(edges) == 1
    assert edges[0]["derived_file_id"] == result["file_id"]
    assert edges[0]["input_file_id"] == source["file_id"]
    assert edges[0]["workflow_run_id"] is None

    runs = [dict(row) for row in db.conn.execute(
        "SELECT * FROM workflow_runs WHERE step='adopt'").fetchall()]
    assert len(runs) == 1
    details = json.loads(runs[0]["execution_details"])
    assert details["actor"] == "tester"
    assert details["items"][0]["file_id"] == result["file_id"]


def test_adopt_idempotent_same_bytes(project_db, tmp_path):
    project, db, source = project_db
    derived = _write_derived(tmp_path)
    first = adopt_files(db, project, items=[_item(derived, source)])
    files_before = db.conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    second = adopt_files(db, project, items=[_item(derived, source)])
    assert second[0]["file_id"] == first[0]["file_id"]
    assert db.conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == files_before
    assert len(_lineage_rows(db)) == 1


def test_adopt_conflicting_bytes_raise(project_db, tmp_path):
    project, db, source = project_db
    adopt_files(db, project, items=[_item(_write_derived(tmp_path), source)])
    changed = _write_derived(tmp_path, name="matrix2.tsv", content="id\tvalue\nctg1\t2\n")
    with pytest.raises(ConflictError):
        adopt_files(db, project, items=[_item(changed, source)])
    # The conflicting item was not registered and no extra lineage edge exists.
    assert len(_lineage_rows(db)) == 1


def test_adopt_unknown_derived_from_aborts_whole_batch(project_db, tmp_path):
    project, db, source = project_db
    good = _item(_write_derived(tmp_path, name="good.tsv"), source, role="derived_a")
    bad = _item(_write_derived(tmp_path, name="bad.tsv"), source, role="derived_b",
                derived_from=["FIL_999999"])
    files_before = db.conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"]
    with pytest.raises(ValidationError, match="FIL_999999"):
        adopt_files(db, project, items=[good, bad])
    assert db.conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == files_before
    assert _lineage_rows(db) == []
    assert db.conn.execute(
        "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='adopt'").fetchone()["n"] == 0


@pytest.mark.parametrize("existing_conflict", [False, True])
def test_adopt_content_conflicts_abort_before_materializing(project_db, tmp_path, existing_conflict):
    project, db, source = project_db
    first = _item(_write_derived(tmp_path, "first.tsv"), source, role="batch_a")
    second = _item(_write_derived(tmp_path, "second.tsv", "different"), source, role="batch_a")
    if existing_conflict:
        adopt_files(db, project, items=[first])
        first = _item(_write_derived(tmp_path, "new.tsv"), source, role="new_role")
    before = list(db.conn.iterdump())
    log = (project.logs_root / "workflow.jsonl").read_bytes()
    with pytest.raises(ConflictError):
        adopt_files(db, project, items=[first, second])
    assert list(db.conn.iterdump()) == before
    assert (project.logs_root / "workflow.jsonl").read_bytes() == log
    expected = "batch_a" if existing_conflict else None
    artifacts = list((project.analysis_root / "adopted").rglob("*.tsv"))
    assert [path.name for path in artifacts] == ([f"ASM_000001.{expected}.tsv"] if expected else [])


@pytest.mark.parametrize("failure", ["second_item", "batch_log", "interrupt"])
def test_adopt_late_failure_rolls_back_files_state_lineage_and_logs(project_db, tmp_path, monkeypatch, failure):
    project, db, source = project_db
    existing = _item(_write_derived(tmp_path, "existing.tsv"), source, role="existing")
    existing_record = adopt_files(db, project, items=[existing])[0]
    first = _item(_write_derived(tmp_path, "first.tsv"), source, role="batch_a")
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "data").write_text("directory bytes")
    second = _item(directory, source, role="batch_b", format="directory")
    before = list(db.conn.iterdump())
    log = (project.logs_root / "workflow.jsonl").read_bytes()
    ingest = lineage.ingest_file

    def fail_ingest(*args, **kwargs):
        result = ingest(*args, **kwargs)
        if args[5] == "batch_b":
            if failure == "interrupt":
                raise KeyboardInterrupt()
            raise OSError("late archive failure")
        return result

    if failure == "batch_log":
        log_run = lineage.log_run

        def fail_log(*args, **kwargs):
            log_run(*args, **kwargs)
            raise OSError("late batch log failure")

        monkeypatch.setattr(lineage, "log_run", fail_log)
    else:
        monkeypatch.setattr(lineage, "ingest_file", fail_ingest)
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else OSError):
        adopt_files(db, project, items=[existing, first, second])
    assert not db.conn.in_transaction
    assert list(db.conn.iterdump()) == before
    assert (project.logs_root / "workflow.jsonl").read_bytes() == log
    assert (project.root / existing_record["relative_path"]).read_bytes() == Path(existing["path"]).read_bytes()
    adopted = project.analysis_root / "adopted" / "ASM_000001"
    assert sorted(path.name for path in adopted.iterdir()) == ["ASM_000001.existing.tsv"]
    monkeypatch.undo()
    assert len(adopt_files(db, project, items=[existing, first, second])) == 3


def test_adopt_item_validation(project_db, tmp_path):
    project, db, source = project_db
    derived = _write_derived(tmp_path)
    with pytest.raises(ValidationError, match="at least one item"):
        adopt_files(db, project, items=[])
    with pytest.raises(ValidationError, match="'role' is required"):
        adopt_files(db, project, items=[_item(derived, source, role="")])
    with pytest.raises(ValidationError, match="'derived_from' is required"):
        adopt_files(db, project, items=[_item(derived, source, derived_from=[])])
    with pytest.raises(ValidationError, match="does not exist"):
        adopt_files(db, project, items=[_item(tmp_path / "missing.tsv", source)])
    with pytest.raises(ValidationError, match="must be a mapping"):
        adopt_files(db, project, items=[["not-a-mapping"]])


def test_adopt_preserves_conflicting_unregistered_destination(project_db, tmp_path):
    project, db, source = project_db
    target = project.analysis_root / "adopted" / "ASM_000001" / "ASM_000001.derived.tsv"
    target.parent.mkdir(parents=True)
    target.write_text("preexisting bytes")
    before = list(db.conn.iterdump())
    with pytest.raises(ConflictError, match="occupied"):
        adopt_files(db, project, items=[
            _item(_write_derived(tmp_path, "first.tsv"), source, role="new_role"),
            _item(_write_derived(tmp_path, "second.tsv"), source, role="derived"),
        ])
    assert target.read_text() == "preexisting bytes"
    assert list(target.parent.iterdir()) == [target]
    assert list(db.conn.iterdump()) == before


def test_adopt_directory_artifact(project_db, tmp_path):
    project, db, source = project_db
    tree = tmp_path / "roary_out"
    tree.mkdir()
    (tree / "gene_presence_absence.txt").write_text("gene\na\n", encoding="utf-8")
    results = adopt_files(db, project, items=[_item(
        tree, source, role="pangenome_dir", format="directory", compression="none",
        workflow_run_id="WF_EXTERNAL_1",
    )])
    record = results[0]
    assert record["format"] == "directory"
    assert (project.root / record["relative_path"] / "gene_presence_absence.txt").is_file()
    edge = _lineage_rows(db)[0]
    assert edge["workflow_run_id"] == "WF_EXTERNAL_1"


def test_adopted_file_is_analysis_candidate(project_db, tmp_path):
    project, db, source = project_db
    derived = _write_derived(tmp_path)
    adopted = adopt_files(db, project, items=[_item(derived, source)])[0]
    recipe = Recipe(
        name="downstream", tool_name="tool", description="", entity_type="assembly",
        file_role="pangenome_matrix", fmt="tsv", input_kind="file", database="",
        database_version="", output_subdir="downstream", output_kind="file",
        output_name_template="", output_suffix=".tsv", arguments=[], parameters={},
        result_parser="none", max_hits_per_query=2, raw={},
    )
    candidates = candidate_files(db, recipe)
    assert [row["file_id"] for row in candidates] == [adopted["file_id"]]


def test_load_adopt_manifest_json_and_tsv(project_db, tmp_path):
    _project, _db, source = project_db
    json_manifest = tmp_path / "adopt.json"
    json_manifest.write_text(json.dumps([{
        "path": "out/a.tsv", "entity_type": "assembly", "entity_id": "ASM_000001",
        "role": "derived_a", "format": "tsv", "compression": "none",
        "derived_from": [source["file_id"]], "workflow_run_id": "WF_1",
    }]), encoding="utf-8")
    items = load_adopt_manifest(json_manifest)
    assert items == [{
        "path": "out/a.tsv", "entity_type": "assembly", "entity_id": "ASM_000001",
        "role": "derived_a", "format": "tsv", "compression": "none",
        "derived_from": [source["file_id"]], "workflow_run_id": "WF_1",
    }]

    tsv_manifest = tmp_path / "adopt.tsv"
    tsv_manifest.write_text(
        "path\tentity_type\tentity_id\trole\tformat\tcompression\tderived_from\tworkflow_run_id\n"
        f"out/b.tsv\tassembly\tASM_000001\tderived_b\ttsv\tnone\t{source['file_id']},FIL_000002\t\n",
        encoding="utf-8",
    )
    items = load_adopt_manifest(tsv_manifest)
    assert items[0]["derived_from"] == [source["file_id"], "FIL_000002"]
    assert items[0]["workflow_run_id"] is None

    with pytest.raises(ValidationError, match="not found"):
        load_adopt_manifest(tmp_path / "missing.tsv")
    bad = tmp_path / "bad.tsv"
    bad.write_text("path\trole\nx\ty\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="missing columns"):
        load_adopt_manifest(bad)


def test_adopt_cli_single_file_and_manifest(project_db, tmp_path, capsys):
    project, db, source = project_db
    derived = _write_derived(tmp_path)
    assert main([
        "--project", str(tmp_path), "adopt",
        "--file", str(derived), "--entity-type", "assembly", "--entity-id", "ASM_000001",
        "--role", "cli_derived", "--format", "tsv", "--compression", "none",
        "--derived-from", source["file_id"], "--actor", "cli-tester",
    ]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["registered"] == 1
    assert summary["file_ids"][0].startswith("FIL_")

    second = _write_derived(tmp_path, name="second.tsv", content="x\n")
    manifest = tmp_path / "batch.json"
    manifest.write_text(json.dumps([_item(second, source, role="cli_batch")]), encoding="utf-8")
    assert main(["--project", str(tmp_path), "adopt", "--from-manifest", str(manifest)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["registered"] == 1
    assert db.conn.execute("SELECT COUNT(*) AS n FROM file_lineage").fetchone()["n"] == 2


def test_adopt_cli_mode_exclusivity(tmp_path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    with pytest.raises(SystemExit):
        main(["--project", str(tmp_path), "adopt"])
    with pytest.raises(SystemExit):
        main(["--project", str(tmp_path), "adopt", "--file", "x", "--from-manifest", "y"])
