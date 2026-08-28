"""CLI coverage for 0.4 import, backup and high-level entity lookup."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from operon.backup import create_backup, verify_backup
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.entity_view import organism_graph
from operon.import_wizard import _commit, _synchronize_new_entity_links, run_dataset_wizard


def _project(tmp_path: Path) -> tuple[object, Database]:
    assert main(["--project", str(tmp_path), "init", str(tmp_path), "--project-id", "PRJ_NEW_001"]) == 0
    project = load_project(tmp_path)
    return project, Database(project.db_path)


def test_show_resolves_organism_accession_and_descendants(tmp_path: Path):
    project, db = _project(tmp_path)
    try:
        db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Graphus testii"})
        db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        db.insert_row("runs", {"run_id": "RUN_000001", "sample_id": "SMP_000001"})
        db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
        db.insert_row("annotations", {"annotation_id": "ANN_000001", "assembly_id": "ASM_000001"})
        db.insert_row("accessions", {
            "internal_type": "organism", "internal_id": "ORG_000001",
            "namespace": "LAB", "accession": "ROOT-1",
        })
        graph = organism_graph(db, "LAB:ROOT-1")
        assert graph["organism"]["organism_id"] == "ORG_000001"
        assert [row["run_id"] for row in graph["runs"]] == ["RUN_000001"]
        assert [row["assembly_id"] for row in graph["assemblies"]] == ["ASM_000001"]
        assert [row["annotation_id"] for row in graph["annotations"]] == ["ANN_000001"]
    finally:
        db.close()


def test_backup_control_scope_is_consistent_and_detects_tampering(tmp_path: Path):
    project_root = tmp_path / "project"
    backup_root = tmp_path / "backup"
    project, db = _project(project_root)
    try:
        db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Backupus testii"})
        result = create_backup(db, project, backup_root, scope="control")
        assert result["file_count"] > 0
    finally:
        db.close()
    verified = verify_backup(backup_root)
    assert verified["ok"] is True
    (backup_root / "project.yaml").write_text("tampered\n", encoding="utf-8")
    assert verify_backup(backup_root)["ok"] is False


class _TTY:
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def isatty(self):
        return True

    def write(self, value):
        return self.wrapped.write(value)

    def flush(self):
        return self.wrapped.flush()


def test_wizard_review_edit_returns_directly_to_summary(tmp_path: Path, monkeypatch):
    project, db = _project(tmp_path)
    events: list[str] = []
    try:
        import operon.import_wizard as wizard

        names = ["source", "organism", "sample", "sequencing", "assembly", "annotation", "files"]
        for name in names:
            monkeypatch.setattr(wizard, f"_ask_{name}", lambda _db, _draft, name=name: events.append(name))
        monkeypatch.setattr(wizard, "_summary", lambda _db, _draft: events.append("summary") or "summary")
        actions = iter(["source", "cancel"])
        monkeypatch.setattr(wizard, "_select", lambda _message, _choices: next(actions))
        monkeypatch.setattr(wizard.sys, "stdin", _TTY(sys.stdin))
        monkeypatch.setattr(wizard.sys, "stdout", _TTY(sys.stdout))

        assert run_dataset_wizard(db, project) is None
        assert events == [*names, "summary", "source", "summary"]
    finally:
        db.close()


def test_wizard_commit_creates_entity_chain_and_links_annotation_files(tmp_path: Path):
    project, db = _project(tmp_path)
    gff = tmp_path / "input.gff3"
    cds = tmp_path / "input.cds.fna"
    protein = tmp_path / "input.faa"
    gff.write_text("##gff-version 3\n", encoding="utf-8")
    cds.write_text(">cds1\nATG\n", encoding="utf-8")
    protein.write_text(">p1\nM\n", encoding="utf-8")
    draft = {
        "source": {"provider": "Lab", "record_url": "https://example.invalid/dataset/1"},
        "organism": {"action": "create", "id": "ORG_000001", "row": {
            "organism_id": "ORG_000001", "scientific_name": "Wizardus testii", "taxonomy_source": "other",
        }},
        "sample": {"action": "create", "id": "SMP_000001", "row": {
            "sample_id": "SMP_000001", "organism_id": "ORG_000001", "isolate": "W1",
        }},
        "run": None,
        "assembly": {"action": "create", "id": "ASM_000001", "row": {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001", "assembly_version": "1",
        }},
        "annotation": {"action": "create", "id": "ANN_000001", "row": {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001", "annotation_version": "1",
        }},
        "files": [
            {"label": "GFF3", "role": "annotation_gff3", "entity_type": "annotation", "path": str(gff)},
            {"label": "CDS FASTA", "role": "cds_fasta", "entity_type": "annotation", "path": str(cds)},
            {"label": "Protein FASTA", "role": "protein_fasta", "entity_type": "annotation", "path": str(protein)},
        ],
    }
    try:
        result = _commit(db, project, draft)
        assert len(result["files"]) == 3
        annotation = db.query(
            "SELECT gff_file_id, cds_file_id, protein_file_id FROM annotations WHERE annotation_id='ANN_000001'"
        )[0]
        assert all(annotation[column] for column in ("gff_file_id", "cds_file_id", "protein_file_id"))
        ingest_runs = db.query("SELECT run_id, parent_run_id FROM workflow_runs WHERE step='ingest'")
        assert len(ingest_runs) == 3
        assert len({row["run_id"] for row in ingest_runs}) == 3
    finally:
        db.close()


def test_wizard_review_edit_relinks_new_descendants():
    draft = {
        "organism": {"action": "create", "id": "ORG_000002", "row": {"organism_id": "ORG_000002"}},
        "sample": {"action": "create", "id": "SMP_000002", "row": {
            "sample_id": "SMP_000002", "organism_id": "ORG_000001",
        }},
        "run": {"action": "create", "id": "RUN_000002", "row": {
            "run_id": "RUN_000002", "sample_id": "SMP_000001",
        }},
        "assembly": {"action": "create", "id": "ASM_000002", "row": {
            "assembly_id": "ASM_000002", "sample_id": "SMP_000001",
        }},
        "annotation": {"action": "create", "id": "ANN_000002", "row": {
            "annotation_id": "ANN_000002", "assembly_id": "ASM_000001",
        }},
    }
    _synchronize_new_entity_links(draft)
    assert draft["sample"]["row"]["organism_id"] == "ORG_000002"
    assert draft["run"]["row"]["sample_id"] == "SMP_000002"
    assert draft["assembly"]["row"]["sample_id"] == "SMP_000002"
    assert draft["annotation"]["row"]["assembly_id"] == "ASM_000002"
