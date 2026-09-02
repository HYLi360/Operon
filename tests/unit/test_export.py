"""Unit tests for the selective file export command."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ValidationError
from operon.export import MANIFEST_COLUMNS, export_files
from operon.files import ingest_file
from operon.lifecycle import apply_lifecycle_event
from operon.utils import sha256_file


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    for suffix in ("1", "2"):
        db.insert_row("assemblies", {
            "assembly_id": f"ASM_00000{suffix}", "sample_id": "SMP_000001",
        })
    genome1 = tmp_path / "genome1.fa"
    genome1.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
    genome2 = tmp_path / "genome2.fa"
    genome2.write_text(">ctg2\nTTTTGGGG\n", encoding="utf-8")
    proteins = tmp_path / "proteins.faa"
    proteins.write_text(">p1\nMAAA\n", encoding="utf-8")
    files = {
        "genome1": ingest_file(db, project, genome1, "assembly", "ASM_000001", "genome_fasta"),
        "genome2": ingest_file(db, project, genome2, "assembly", "ASM_000002", "genome_fasta"),
        "proteins1": ingest_file(db, project, proteins, "assembly", "ASM_000001", "protein_fasta"),
    }
    try:
        yield project, db, files
    finally:
        db.close()


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def test_export_requires_a_selection_criterion(project_db, tmp_path):
    _project, db, _files = project_db
    with pytest.raises(ValidationError, match="at least one selection criterion"):
        export_files(db, _project, output_dir=tmp_path / "out")
    with pytest.raises(ValidationError, match="--decision requires --profile"):
        export_files(db, _project, output_dir=tmp_path / "out", decision="PASS")
    with pytest.raises(ValidationError, match="unsupported export link kind"):
        export_files(db, _project, output_dir=tmp_path / "out", entity_type="assembly",
                     link_kind="weird")


def test_export_filters_by_entity_type_and_role(project_db, tmp_path):
    project, db, files = project_db
    out = tmp_path / "export"
    summary = export_files(db, project, output_dir=out,
                           entity_type="assembly", file_role="genome_fasta")
    assert summary["file_count"] == 2
    rows = _read_tsv(out / "manifest.tsv")
    assert list(rows[0].keys()) == MANIFEST_COLUMNS
    assert {row["file_id"] for row in rows} == {files["genome1"]["file_id"], files["genome2"]["file_id"]}
    for row in rows:
        exported = out / row["export_relative_path"]
        assert row["export_relative_path"].startswith(f"data/{row['entity_type']}/{row['entity_id']}/")
        # The manifest hash is recomputed on the materialized target.
        assert sha256_file(exported) == row["sha256"]
        assert exported.read_bytes() == (project.root / row["original_relative_path"]).read_bytes()
    assert summary["manifest_sha256"] == sha256_file(out / "manifest.tsv")
    provenance = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["selection"]["entity_type"] == "assembly"
    assert provenance["selection"]["file_role"] == "genome_fasta"
    assert provenance["file_count"] == 2
    assert provenance["manifest_sha256"] == summary["manifest_sha256"]
    checksums = (out / "checksums.sha256").read_text(encoding="utf-8").strip().splitlines()
    assert len(checksums) == 2


def test_export_filters_by_decision_and_profile(project_db, tmp_path):
    project, db, _files = project_db
    for entity_id, decision in (("ASM_000001", "PASS"), ("ASM_000002", "FAIL")):
        db.conn.execute(
            "INSERT INTO decisions(entity_type, entity_id, profile, decision, reason_codes, "
            "observed, thresholds, evaluated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("assembly", entity_id, "p1", decision, "[]", "{}", "{}", "now"),
        )
    db.conn.commit()
    out = tmp_path / "export"
    summary = export_files(db, project, output_dir=out, decision="pass", profile="p1")
    # Both files of the PASSed assembly are exported, none of the FAILed one.
    assert summary["file_count"] == 2
    rows = _read_tsv(out / "manifest.tsv")
    assert {row["entity_id"] for row in rows} == {"ASM_000001"}


def test_export_excludes_retired_entities(project_db, tmp_path):
    project, db, _files = project_db
    apply_lifecycle_event(
        db, "assembly", "ASM_000002", action="RETIRE",
        reason_code="accidental_import", reason="mistake", actor="tester",
    )
    out = tmp_path / "export"
    summary = export_files(db, project, output_dir=out, entity_type="assembly")
    assert summary["file_count"] == 2
    rows = _read_tsv(out / "manifest.tsv")
    assert {row["entity_id"] for row in rows} == {"ASM_000001"}


def test_export_qc_snapshot_and_no_qc(project_db, tmp_path):
    project, db, files = project_db
    db.insert_qc_result({
        "entity_type": "assembly", "entity_id": "ASM_000001",
        "file_id": files["genome1"]["file_id"], "file_sha256": files["genome1"]["sha256"],
        "qc_stage": "file", "metric_name": "total_length", "metric_value": "8",
        "metric_numeric": 8.0, "metric_unit": "bp", "tool": "operon",
        "tool_version": "0", "parameter_set": "default", "evaluated_at": "now",
    })
    out = tmp_path / "export"
    export_files(db, project, output_dir=out, entity_type="assembly")
    qc_rows = _read_tsv(out / "qc.tsv")
    assert {row["entity_id"] for row in qc_rows} == {"ASM_000001"}
    assert qc_rows[0]["metric_name"] == "total_length"

    out_no_qc = tmp_path / "export-no-qc"
    export_files(db, project, output_dir=out_no_qc, entity_type="assembly", include_qc=False)
    assert not (out_no_qc / "qc.tsv").exists()


def test_export_never_overrides_output_directory(project_db, tmp_path):
    project, db, _files = project_db
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "stale.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        export_files(db, project, output_dir=occupied, entity_type="assembly")
    with pytest.raises(FileExistsError):
        export_files(db, project, output_dir=tmp_path / "genome1.fa", entity_type="assembly")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert export_files(db, project, output_dir=empty, entity_type="assembly")["file_count"] == 3


def test_export_checksum_mismatch_and_missing_source(project_db, tmp_path):
    project, db, files = project_db
    target = project.root / files["genome1"]["relative_path"]
    target.write_text(">ctg1\nGGGG\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        export_files(db, project, output_dir=tmp_path / "out",
                     file_ids=[files["genome1"]["file_id"]])
    target.unlink()
    with pytest.raises(FileNotFoundError):
        export_files(db, project, output_dir=tmp_path / "out",
                     file_ids=[files["genome1"]["file_id"]])


def test_export_link_kinds(project_db, tmp_path):
    project, db, files = project_db
    hard = tmp_path / "hard"
    export_files(db, project, output_dir=hard, file_ids=[files["genome1"]["file_id"]],
                 link_kind="hardlink", include_qc=False)
    rows = _read_tsv(hard / "manifest.tsv")
    assert sha256_file(hard / rows[0]["export_relative_path"]) == files["genome1"]["sha256"]
    sym = tmp_path / "sym"
    export_files(db, project, output_dir=sym, file_ids=[files["genome1"]["file_id"]],
                 link_kind="symlink", include_qc=False)
    rows = _read_tsv(sym / "manifest.tsv")
    exported = sym / rows[0]["export_relative_path"]
    assert exported.is_symlink()
    assert exported.resolve() == (project.root / files["genome1"]["relative_path"]).resolve()


def test_export_logs_workflow_run(project_db, tmp_path):
    project, db, _files = project_db
    out = tmp_path / "export"
    summary = export_files(db, project, output_dir=out, entity_type="assembly")
    runs = db.query("SELECT * FROM workflow_runs WHERE step='export'")
    assert len(runs) == 1
    run = runs[0]
    assert run["status"] == "completed"
    assert run["output_sha256"] == summary["manifest_sha256"]
    assert run["environment_id"] is None
    details = json.loads(run["execution_details"])
    assert details["selection"]["entity_type"] == "assembly"
    assert details["output_dir"] == str(out)
    assert details["file_count"] == 3
