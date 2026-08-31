"""Conservative-warning and path-repair branches for NCBI reconciliation."""

from __future__ import annotations

from pathlib import Path

import pytest

from operon import ncbi_reconcile
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ConflictError


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def _file(file_id, entity_type, entity_id, role, rel, sha, source_url=""):
    return {
        "file_id": file_id, "entity_type": entity_type, "entity_id": entity_id,
        "file_role": role, "format": "fasta", "compression": "none",
        "relative_path": rel, "source_url": source_url, "size_bytes": 1,
        "sha256": sha, "status": "CHECKSUM_VERIFIED",
    }


def test_plan_reconciliation_duplicate_warnings_roles_paths_primary_and_state(project_db):
    _project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "O"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("assemblies", {
        "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
        "assembly_accession": "GCA_000000001.1", "source_database": "GenBank",
        "fasta_file_id": None,
    })
    for annotation_id in ("ANN_000001", "ANN_000002", "ANN_000003", "ANN_000004", "ANN_000005"):
        db.insert_row("annotations", {
            "annotation_id": annotation_id, "assembly_id": "ASM_000001",
            "annotation_source": "NCBI", "annotation_version": 2 if annotation_id == "ANN_000005" else 1,
        })
    db.insert_row("files", _file(
        "FIL_000001", "annotation", "ANN_000001", "annotation_gff3", "raw/a", "a" * 64
    ))
    db.insert_row("files", _file(
        "FIL_000002", "annotation", "ANN_000002", "annotation_gff3", "raw/b", "b" * 64
    ))
    # ANN_3 and ANN_4 have no overlapping roles, so they can be superseded conservatively.
    db.insert_row("files", _file(
        "FIL_000003", "annotation", "ANN_000003", "annotation_gff3", "raw/c", "c" * 64
    ))
    db.insert_row("files", _file(
        "FIL_000004", "annotation", "ANN_000004", "protein_fasta", "raw/d", "d" * 64
    ))
    for accession, namespace, primary in (
        ("GCF_000000001.1", "NCBI_RefSeq_Assembly", 0),
        ("GCA_000000001.1", "NCBI_GenBank_Assembly", 1),
        ("GCF_000000001.1", "NCBI_Assembly", 0),
        ("GCA_000000001.1", "NCBI_Assembly", 1),
    ):
        db.insert_row("accessions", {
            "internal_type": "assembly", "internal_id": "ASM_000001",
            "namespace": namespace, "accession": accession, "is_primary": primary,
        })
    db.insert_row("files", _file(
        "FIL_000008", "assembly", "ASM_000001", "genome_fasta", "raw/plain.fa", "e" * 64,
        "https://x/GCA_000000001.1/file",
    ))
    db.insert_row("files", _file(
        "FIL_000009", "assembly", "ASM_000001", "genome_fasta_genbank",
        "raw/wrong-name.fa", "f" * 64,
    ))
    # A historical RefSeq URL chooses GCF as canonical even though the assembly row says GCA.
    db.insert_row("files", _file(
        "FIL_000007", "annotation", "ANN_000005", "protein_fasta", "raw/history", "7" * 64,
        "https://x/GCF_000000001.1/file",
    ))
    db.insert_qc_result({
        "entity_type": "annotation", "entity_id": "ANN_000005", "qc_stage": "s",
        "metric_name": "m", "metric_value": "1", "metric_numeric": 1,
        "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
    })
    db.set_entity_state("annotation", "ANN_000005", "DOWNLOADED", "old")

    plan = ncbi_reconcile.plan_ncbi_reconciliation(db)
    warning_kinds = {item["kind"] for item in plan["warnings"]}
    assert "annotation_bytes_differ" in warning_kinds
    assert "alternate_role_conflict" in warning_kinds
    assert plan["annotation_supersessions"]
    assert plan["assembly_updates"][0]["new_accession"].startswith("GCF_")
    assert plan["file_path_repairs"]
    assert plan["accession_primary_updates"]
    assert any(item["annotation_id"] == "ANN_000005" for item in plan["state_restorations"])


def test_apply_path_move_equal_missing_conflict_and_success(project_db):
    project, db = project_db
    assert ncbi_reconcile._apply_path_move(
        db, project, "F", "same", "same", actor=None, run_id="R", reason="x"
    ) is True
    assert ncbi_reconcile._apply_path_move(
        db, project, "F", "missing", "new", actor=None, run_id="R", reason="x"
    ) is False

    old = project.root / "old"
    new = project.root / "new"
    old.write_text("a", encoding="utf-8")
    new.write_text("b", encoding="utf-8")
    with pytest.raises(ConflictError, match="different bytes"):
        ncbi_reconcile._apply_path_move(
            db, project, "F", "old", "new", actor=None, run_id="R", reason="x"
        )
    new.unlink()
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.insert_row("files", _file("FIL_000001", "organism", "ORG_000001", "other", "old", "x" * 64))
    assert ncbi_reconcile._apply_path_move(
        db, project, "FIL_000001", "old", "nested/new", actor="a", run_id="R", reason="x"
    ) is True
    assert (project.root / "nested" / "new").read_text() == "a"


def test_apply_reconciliation_blocks_alternate_role_conflicts(project_db, monkeypatch):
    project, db = project_db
    plan = {
        "warnings": [{"kind": "alternate_role_conflict"}],
        "annotation_supersessions": [], "assembly_updates": [], "file_role_updates": [],
        "file_path_repairs": [], "accession_primary_updates": [], "state_restorations": [],
        "summary": {},
    }
    monkeypatch.setattr(ncbi_reconcile, "plan_ncbi_reconciliation", lambda _db: plan)
    with pytest.raises(ConflictError, match="alternate-role byte conflicts"):
        ncbi_reconcile.apply_ncbi_reconciliation(db, project)
