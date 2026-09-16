"""Entity lookup, release storage, and report formatting edge cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from operon import entity_view, release, reports
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import EntityNotFoundError, ValidationError
from operon.files import ingest_file
from operon.rules import evaluate_entity
from operon.utils import sha256_path


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def _write_empty_scope_profile(project, name: str = "p") -> None:
    (project.profiles_dir / f"{name}.yaml").write_text(
        yaml.safe_dump({"kind": "qc", "version": 1, "applies_to": [],
                        "required": [], "warnings": []}, sort_keys=False),
        encoding="utf-8",
    )


def _insert_graph(db):
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "O"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("runs", {"run_id": "RUN_000001", "sample_id": "SMP_000001"})
    db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
    db.insert_row("annotations", {"annotation_id": "ANN_000001", "assembly_id": "ASM_000001"})


def test_identifier_resolution_missing_ambiguous_and_namespaced(project_db):
    _project, db = project_db
    _insert_graph(db)
    db.insert_row("accessions", {
        "internal_type": "sample", "internal_id": "SMP_000001",
        "namespace": "BioSample", "accession": "SAME",
    })
    db.insert_row("accessions", {
        "internal_type": "assembly", "internal_id": "ASM_000001",
        "namespace": "Assembly", "accession": "SAME",
    })
    assert entity_view.resolve_identifier(db, "BioSample:SAME") == ("sample", "SMP_000001")
    with pytest.raises(ValidationError, match="ambiguous"):
        entity_view.resolve_identifier(db, "SAME")
    with pytest.raises(EntityNotFoundError, match="was not found"):
        entity_view.resolve_identifier(db, "MISSING")


def test_organism_resolution_and_graph_empty_branches(project_db):
    _project, db = project_db
    with pytest.raises(EntityNotFoundError, match="cannot resolve organism"):
        entity_view._organism_for(db, "unknown", "X")
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "O"})
    graph = entity_view.organism_graph(db, "ORG_000001")
    assert graph["samples"] == [] and graph["sources"] == [] and graph["files"] == []
    class Result:
        @staticmethod
        def fetchone():
            return {"organism_id": None}
    class Conn:
        @staticmethod
        def execute(*_args):
            return Result()
    fake_db = type("DB", (), {"conn": Conn()})()
    with pytest.raises(EntityNotFoundError, match="no organism reference"):
        entity_view._organism_for(fake_db, "sample", "SMP_000001")


def _member(relative_path, sha, *, status="CHECKSUM_VERIFIED"):
    return {
        "file_id": "FIL_000001", "entity_type": "organism", "entity_id": "ORG_000001",
        "file_role": "other", "format": "other", "compression": "none",
        "relative_path": relative_path, "source_url": None, "size_bytes": 1,
        "sha256": sha, "status": status, "effective_decision": "PASS",
    }


def test_release_validates_link_kind_existing_and_missing_members(project_db, monkeypatch):
    project, db = project_db
    with pytest.raises(ValueError, match="unsupported release link"):
        release.create_release(db, project, "bad", "p", link_kind="symlink")
    existing = project.releases_root / "existing"
    existing.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        release.create_release(db, project, "existing", "p")

    _write_empty_scope_profile(project)
    monkeypatch.setattr(release, "release_files_for", lambda *_a: [_member("raw/missing", "a" * 64)])
    with pytest.raises(FileNotFoundError, match="release member missing"):
        release.create_release(db, project, "missing", "p")
    monkeypatch.setattr(release, "release_files_for", lambda *_a: [
        _member("raw/missing", "a" * 64, status="REMOTE_ONLY")
    ])
    with pytest.raises(FileNotFoundError, match="remote-only"):
        release.create_release(db, project, "remote", "p")


def test_release_rejects_unevaluated_entities_before_creating_output(project_db):
    project, db = project_db
    _insert_graph(db)
    (project.profiles_dir / "release_preflight.yaml").write_text(
        yaml.safe_dump({
            "kind": "qc", "version": 1, "applies_to": ["assembly"],
            "required": [], "warnings": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    source = project.root / "assembly.fa"
    source.write_text(">ctg1\nACGT\n", encoding="utf-8")
    ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    with pytest.raises(ValidationError, match="unevaluated/stale"):
        release.create_release(db, project, "blocked", "release_preflight")
    assert not (project.releases_root / "blocked").exists()


@pytest.mark.bug("ODR-0005")
def test_release_and_export_reject_an_unknown_profile(project_db, tmp_path):
    project, db = project_db
    _insert_graph(db)
    source = project.root / "assembly.fa"
    source.write_text(">ctg1\nACGT\n", encoding="utf-8")
    ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    with pytest.raises(ValidationError, match="profile 'typo' not found"):
        release.create_release(db, project, "zero-member", "typo")
    assert not (project.releases_root / "zero-member").exists()

    from operon.export import export_files
    with pytest.raises(ValidationError, match="profile 'typo' not found"):
        export_files(db, project, output_dir=tmp_path / "out",
                     decision="PASS", profile="typo")
    assert not (tmp_path / "out").exists()


def test_release_rejects_metadata_changed_after_evaluation(project_db):
    project, db = project_db
    _insert_graph(db)
    profile_name = "release_stale"
    (project.profiles_dir / f"{profile_name}.yaml").write_text(
        yaml.safe_dump({
            "kind": "qc", "version": 1, "applies_to": ["assembly"],
            "required": [], "warnings": [],
        }, sort_keys=False),
        encoding="utf-8",
    )
    source = project.root / "assembly.fa"
    source.write_text(">ctg1\nACGT\n", encoding="utf-8")
    ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    evaluate_entity(db, project, "assembly", "ASM_000001", profile_name)
    db.record_change("assemblies", "ASM_000001", "assembly_name", None, "updated",
                     "metadata changed after evaluation")
    with pytest.raises(ValidationError, match="unevaluated/stale"):
        release.create_release(db, project, "stale", profile_name)
    assert not (project.releases_root / "stale").exists()


def test_release_checksum_directory_copy_and_hardlink_fallback(project_db, monkeypatch):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "O"})
    _write_empty_scope_profile(project)
    bad = project.root / "raw" / "bad"
    bad.parent.mkdir(exist_ok=True)
    bad.write_text("x", encoding="utf-8")
    monkeypatch.setattr(release, "release_files_for", lambda *_a: [_member("raw/bad", "0" * 64)])
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        release.create_release(db, project, "checksum", "p")

    tree = project.root / "raw" / "tree"
    tree.mkdir()
    (tree / "x").write_text("x", encoding="utf-8")
    member = _member("raw/tree", sha256_path(tree))
    member["size_bytes"] = 1
    monkeypatch.setattr(release, "release_files_for", lambda *_a: [member])
    result = release.create_release(db, project, "directory", "p", copy_files=True, link_kind="hardlink")
    assert (Path(result["path"]) / "data" / "organism" / "ORG_000001" / "tree" / "x").is_file()

    source = project.root / "raw" / "file"
    source.write_text("y", encoding="utf-8")
    member = _member("raw/file", sha256_path(source))
    monkeypatch.setattr(release, "release_files_for", lambda *_a: [member])
    monkeypatch.setattr(release.os, "link", lambda *_a: (_ for _ in ()).throw(OSError("unsupported")))
    result = release.create_release(db, project, "hardlink", "p", link_kind="hardlink")
    assert (Path(result["path"]) / "data" / "organism" / "ORG_000001" / "file").read_text() == "y"


@pytest.mark.bug("ODR-0006")
def test_release_recovers_an_interrupted_publication_on_retry(project_db):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "O"})
    _write_empty_scope_profile(project)
    # Simulate the crash window: a published tree whose releases row never
    # committed must be removed by the retry, not block it with FileExistsError.
    orphan = project.releases_root / "v-orphan"
    orphan.mkdir(parents=True)
    (orphan / "provenance.json").write_text(
        json.dumps({"schema": "operon-2.0", "release_version": "v-orphan"}),
        encoding="utf-8",
    )
    (orphan / "manifest.tsv").write_text("stale\n", encoding="utf-8")
    result = release.create_release(db, project, "v-orphan", "p")
    assert Path(result["path"]).is_dir()
    assert (Path(result["path"]) / "manifest.tsv").read_text(encoding="utf-8") != "stale\n"
    assert db.conn.execute(
        "SELECT 1 FROM releases WHERE version='v-orphan'").fetchone() is not None

    # A tree that is not an interrupted publication of this version is
    # operator data and stays put.
    foreign = project.releases_root / "v-foreign"
    foreign.mkdir(parents=True)
    (foreign / "provenance.json").write_text(
        json.dumps({"release_version": "someone-else"}), encoding="utf-8")
    with pytest.raises(FileExistsError, match="not an interrupted publication"):
        release.create_release(db, project, "v-foreign", "p")
    assert (foreign / "provenance.json").exists()


def test_release_rolls_back_published_tree_when_state_commit_fails(project_db, monkeypatch):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "O"})
    _write_empty_scope_profile(project)
    source = project.root / "raw" / "file"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("y", encoding="utf-8")
    monkeypatch.setattr(release, "release_files_for", lambda *_a: [_member("raw/file", sha256_path(source))])
    monkeypatch.setattr(release, "set_state", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("state failure")))
    with pytest.raises(RuntimeError, match="state failure"):
        release.create_release(db, project, "state-failure", "p")
    assert not (project.releases_root / "state-failure").exists()
    assert db.conn.execute("SELECT 1 FROM releases WHERE version='state-failure'").fetchone() is None


def test_decision_reason_code_formats(project_db):
    # The QC report/wide-pivot paths are asserted by
    # tests/unit/test_support_edges.py::test_report_queries_wide_pivot_and_reason_rendering.
    _project, db = project_db
    db.upsert_decision({
        "entity_type": "organism", "entity_id": "ORG_000001", "profile": "p",
        "profile_version": 1, "decision": "PASS", "reason_codes": json.dumps(["A", "B"]),
        "observed": "{}", "thresholds": "{}", "evaluated_at": "now",
    })
    text = reports.print_decisions(db, "p")
    assert "A, B" in text
    db.conn.execute("UPDATE decisions SET reason_codes='not-json'")
    assert "not-json" in reports.print_decisions(db)
