"""CLI coverage for 0.4 import, backup and high-level entity lookup."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from operon.backup import create_backup, verify_backup
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.entity_view import entity_graph, organism_graph
from operon.errors import EntityNotFoundError
from operon.import_wizard import (
    _ask_organism,
    _commit,
    _source_validation_errors,
    _synchronize_new_entity_links,
    run_dataset_wizard,
)
from operon.workflow import log_run


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


def test_show_matched_scope_excludes_siblings_and_superseded_descendants(tmp_path: Path):
    _project_config, db = _project(tmp_path)
    try:
        db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Graphus testii",
        })
        for suffix in ("1", "2"):
            db.insert_row("samples", {
                "sample_id": f"SMP_00000{suffix}", "organism_id": "ORG_000001",
            })
            db.insert_row("assemblies", {
                "assembly_id": f"ASM_00000{suffix}", "sample_id": f"SMP_00000{suffix}",
            })
        db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
        })
        db.insert_row("annotations", {
            "annotation_id": "ANN_000002", "assembly_id": "ASM_000001",
        })
        db.insert_row("annotations", {
            "annotation_id": "ANN_000003", "assembly_id": "ASM_000002",
        })
        db.supersede_entity(
            "annotation", "ANN_000001", "annotation", "ANN_000002",
            reason="duplicate annotation",
        )

        graph = entity_graph(db, "ASM_000001")
        assert graph["scope"] == "matched"
        assert [row["sample_id"] for row in graph["samples"]] == ["SMP_000001"]
        assert [row["assembly_id"] for row in graph["assemblies"]] == ["ASM_000001"]
        assert [row["annotation_id"] for row in graph["annotations"]] == ["ANN_000002"]
        assert [row["object_id"] for row in graph["supersessions"]] == ["ANN_000001"]

        organism_scope = entity_graph(db, "ASM_000001", scope="organism")
        assert [row["assembly_id"] for row in organism_scope["assemblies"]] == [
            "ASM_000001", "ASM_000002",
        ]
        assert [row["annotation_id"] for row in organism_scope["annotations"]] == [
            "ANN_000002", "ANN_000003",
        ]

        history = entity_graph(
            db, "ASM_000001", scope="organism", include_superseded=True,
        )
        assert [row["annotation_id"] for row in history["annotations"]] == [
            "ANN_000001", "ANN_000002", "ANN_000003",
        ]
    finally:
        db.close()


def test_show_opens_database_read_only(tmp_path: Path, capsys):
    project, db = _project(tmp_path)
    try:
        db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Readonly testii",
        })
    finally:
        db.close()

    original_dir_mode = tmp_path.stat().st_mode
    original_db_mode = project.db_path.stat().st_mode
    try:
        os.chmod(project.db_path, 0o444)
        os.chmod(tmp_path, 0o555)
        assert main(["--project", str(tmp_path), "show", "ORG_000001"]) == 0
        assert "Scope:    matched" in capsys.readouterr().out
    finally:
        os.chmod(tmp_path, original_dir_mode)
        os.chmod(project.db_path, original_db_mode)


def test_organism_selection_uses_scientific_name_autocomplete(tmp_path: Path, monkeypatch):
    _project_config, db = _project(tmp_path)
    captured: dict[str, object] = {}

    class Prompt:
        def ask(self):
            return "Arabidopsis thaliana"

    def fake_autocomplete(message, **kwargs):
        captured["message"] = message
        captured.update(kwargs)
        return Prompt()

    try:
        db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Arabidopsis thaliana",
            "taxon_id": 3702,
        })
        db.insert_row("organisms", {
            "organism_id": "ORG_000002", "scientific_name": "Oryza sativa",
            "taxon_id": 4530,
        })
        monkeypatch.setattr("operon.import_wizard.questionary.autocomplete", fake_autocomplete)
        draft: dict[str, object] = {}
        _ask_organism(db, draft)
        assert captured["message"] == "Select the organism:"
        assert captured["choices"] == [
            "Create a new organism", "Arabidopsis thaliana", "Oryza sativa",
        ]
        assert captured["meta_information"]["Arabidopsis thaliana"] == (
            "ORG_000001 | TaxID 3702"
        )
        assert draft["organism"] == {"action": "reuse", "id": "ORG_000001"}
    finally:
        db.close()


def test_show_reports_orphaned_organism_reference(tmp_path: Path):
    _project_config, db = _project(tmp_path)
    try:
        db.conn.execute("PRAGMA foreign_keys=OFF")
        db.conn.execute(
            "INSERT INTO samples(sample_id, organism_id) VALUES(?, ?)",
            ("SMP_000001", "ORG_999999"),
        )
        db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(EntityNotFoundError, match=(
            "sample SMP_000001 refers to missing organism ORG_999999"
        )):
            organism_graph(db, "SMP_000001")
    finally:
        db.close()


def test_show_reports_null_organism_in_malformed_schema():
    class MalformedDatabase:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:")
            self.conn.row_factory = sqlite3.Row
            self.conn.executescript(
                "CREATE TABLE samples(sample_id TEXT PRIMARY KEY, organism_id TEXT);"
                "INSERT INTO samples(sample_id, organism_id) VALUES('SMP_000001', NULL);"
            )

        def require_entity(self, _entity_type: str, _entity_id: str) -> None:
            return None

    db = MalformedDatabase()
    try:
        with pytest.raises(
            EntityNotFoundError, match="sample SMP_000001 has no organism reference"
        ):
            organism_graph(db, "SMP_000001")
    finally:
        db.conn.close()


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
    unexpected = backup_root / "unexpected.txt"
    unexpected.write_text("not in manifest\n", encoding="utf-8")
    extra_result = verify_backup(backup_root)
    assert extra_result["ok"] is False
    assert extra_result["unexpected"] == 1
    assert {tuple(item.values()) for item in extra_result["failures"]} >= {
        ("unexpected.txt", "unexpected file")
    }
    unexpected.unlink()
    (backup_root / "project.yaml").write_text("tampered\n", encoding="utf-8")
    assert verify_backup(backup_root)["ok"] is False


def test_log_run_rejects_duplicate_without_appending_phantom_jsonl(tmp_path: Path):
    project, db = _project(tmp_path)
    run_id = "WF_DUPLICATE_TEST"
    try:
        record = {"run_id": run_id, "step": "duplicate_test", "status": "completed"}
        log_run(db, project, record)
        with pytest.raises(sqlite3.IntegrityError, match="workflow_runs.run_id"):
            log_run(db, project, record)
        assert db.conn.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 1
        records = [
            json.loads(line)
            for line in (project.logs_root / "workflow.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert sum(item.get("run_id") == run_id for item in records) == 1
    finally:
        db.close()


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
        "source": {
            "source_type": "non_insdc", "database_name": "Lab Genome Portal",
            "provider": "Lab", "record_url": "https://example.invalid/dataset/1",
            "citation": "doi:10.0000/example", "license_name": "CC-BY-4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
        },
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
        assert {row["parent_run_id"] for row in ingest_runs} == {result["run_id"]}
        parent = db.query(
            "SELECT status FROM workflow_runs WHERE run_id=?", (result["run_id"],)
        )[0]
        assert parent["status"] == "completed"
        source = dict(db.query(
            "SELECT source_id, source_type, database_name, citation, license_name "
            "FROM data_sources WHERE source_id=?", (result["source_id"],)
        )[0])
        assert source == {
            "source_id": result["source_id"],
            "source_type": "non_insdc",
            "database_name": "Lab Genome Portal",
            "citation": "doi:10.0000/example",
            "license_name": "CC-BY-4.0",
        }
        links = db.query(
            "SELECT object_type, object_id FROM source_links WHERE source_id=?",
            (result["source_id"],),
        )
        assert len(links) == 7
        graph = organism_graph(db, "ORG_000001")
        assert [row["source_id"] for row in graph["sources"]] == [result["source_id"]]
        assert len(graph["source_links"]) == 7
    finally:
        db.close()


def test_non_insdc_source_requires_citation_and_license():
    errors = _source_validation_errors({"source": {
        "source_type": "non_insdc", "database_name": "Institutional repository",
        "provider": "Example Institute",
    }})
    assert errors == [
        "Non-INSDC data requires a reference citation or DOI.",
        "Non-INSDC data requires a License name or SPDX identifier.",
    ]


def test_wizard_failure_discards_completed_child_provenance(tmp_path: Path, monkeypatch):
    project, db = _project(tmp_path)
    gff = tmp_path / "input.gff3"
    cds = tmp_path / "input.cds.fna"
    gff.write_text("##gff-version 3\n", encoding="utf-8")
    cds.write_text(">cds1\nATG\n", encoding="utf-8")
    draft = {
        "source": {
            "source_type": "non_insdc", "database_name": "Lab delivery",
            "provider": "Lab", "record_url": "",
            "citation": "Internal delivery protocol v1", "license_name": "Proprietary",
        },
        "organism": {"action": "create", "id": "ORG_000001", "row": {
            "organism_id": "ORG_000001", "scientific_name": "Rollbackus testii",
        }},
        "sample": {"action": "create", "id": "SMP_000001", "row": {
            "sample_id": "SMP_000001", "organism_id": "ORG_000001",
        }},
        "run": None,
        "assembly": {"action": "create", "id": "ASM_000001", "row": {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
        }},
        "annotation": {"action": "create", "id": "ANN_000001", "row": {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
        }},
        "files": [
            {"label": "GFF3", "role": "annotation_gff3", "entity_type": "annotation", "path": str(gff)},
            {"label": "CDS FASTA", "role": "cds_fasta", "entity_type": "annotation", "path": str(cds)},
        ],
    }
    import operon.import_wizard as wizard

    original_ingest = wizard.ingest_file
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("forced second-file failure")
        return original_ingest(*args, **kwargs)

    monkeypatch.setattr(wizard, "ingest_file", fail_second)
    try:
        with pytest.raises(RuntimeError, match="forced second-file failure"):
            _commit(db, project, draft)
        assert db.conn.execute(
            "SELECT COUNT(*) FROM organisms WHERE organism_id='ORG_000001'"
        ).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM files WHERE entity_id='ANN_000001'"
        ).fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM data_sources").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM source_links").fetchone()[0] == 0
        runs = [dict(row) for row in db.conn.execute(
            "SELECT step, status, error FROM workflow_runs ORDER BY started_at"
        ).fetchall()]
        assert runs == [{
            "step": "interactive_dataset_import",
            "status": "failed",
            "error": "RuntimeError: forced second-file failure",
        }]
        jsonl_records = [
            json.loads(line)
            for line in (project.logs_root / "workflow.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [(item["step"], item["status"]) for item in jsonl_records] == [
            ("interactive_dataset_import", "failed")
        ]
        assert not any(path.is_file() for path in project.raw_root.rglob("*"))
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
