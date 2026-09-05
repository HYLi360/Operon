"""Entity lookup, release storage, and report formatting edge cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from operon import entity_view, release, reports
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import EntityNotFoundError, ValidationError
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


@pytest.mark.parametrize(
    ("entity_type", "entity_id"),
    [
        ("organism", "ORG_000001"), ("sample", "SMP_000001"),
        ("run", "RUN_000001"), ("assembly", "ASM_000001"),
        ("annotation", "ANN_000001"),
    ],
)
def test_resolve_organism_for_every_entity_type(project_db, entity_type, entity_id):
    _project, db = project_db
    _insert_graph(db)
    assert entity_view._organism_for(db, entity_type, entity_id) == "ORG_000001"


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

    monkeypatch.setattr(release, "release_files_for", lambda *_a: [_member("raw/missing", "a" * 64)])
    with pytest.raises(FileNotFoundError, match="release member missing"):
        release.create_release(db, project, "missing", "p")
    monkeypatch.setattr(release, "release_files_for", lambda *_a: [
        _member("raw/missing", "a" * 64, status="REMOTE_ONLY")
    ])
    with pytest.raises(FileNotFoundError, match="remote-only"):
        release.create_release(db, project, "remote", "p")


def test_release_checksum_directory_copy_and_hardlink_fallback(project_db, monkeypatch):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "O"})
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


def test_release_rolls_back_published_tree_when_state_commit_fails(project_db, monkeypatch):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "O"})
    source = project.root / "raw" / "file"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("y", encoding="utf-8")
    monkeypatch.setattr(release, "release_files_for", lambda *_a: [_member("raw/file", sha256_path(source))])
    monkeypatch.setattr(release, "set_state", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("state failure")))
    with pytest.raises(RuntimeError, match="state failure"):
        release.create_release(db, project, "state-failure", "p")
    assert not (project.releases_root / "state-failure").exists()
    assert db.conn.execute("SELECT 1 FROM releases WHERE version='state-failure'").fetchone() is None


def test_report_filters_wide_rows_and_decision_reason_formats(project_db):
    project, db = project_db
    db.insert_qc_result({
        "entity_type": "organism", "entity_id": "ORG_000001", "qc_stage": "s",
        "metric_name": "m", "metric_value": "1", "metric_numeric": 1,
        "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
    })
    assert len(reports.qc_rows(db, entity_type="organism", entity_id="ORG_000001")) == 1
    columns, rows = reports.qc_wide(db, "organism")
    assert "m" in columns and rows[0]["m"] == 1
    assert "ORG_000001" in reports.print_qc_table(db, "organism", "ORG_000001")
    reports.export_qc_tsv(db, project, "organism")

    db.upsert_decision({
        "entity_type": "organism", "entity_id": "ORG_000001", "profile": "p",
        "profile_version": 1, "decision": "PASS", "reason_codes": json.dumps(["A", "B"]),
        "observed": "{}", "thresholds": "{}", "evaluated_at": "now",
    })
    text = reports.print_decisions(db, "p")
    assert "A, B" in text
    db.conn.execute("UPDATE decisions SET reason_codes='not-json'")
    assert "not-json" in reports.print_decisions(db)
