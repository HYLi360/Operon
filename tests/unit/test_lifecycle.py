"""Logical retirement, restoration and active-consumer filtering."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from operon import cli, qc_module, reports
from operon.adapters.ncbi_datasets import _PlanBuilder, _find_archived_assembly
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.entity_view import entity_graph
from operon.errors import ValidationError
from operon.files import ingest_file
from operon.lifecycle import (
    apply_lifecycle_event, entity_subtree, lifecycle_plan, list_retired_entities,
)
from operon.release import release_files_for
from operon.tools import Recipe, candidate_files
from operon.workflow import run_external_command


@pytest.fixture
def lifecycle_project(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row("organisms", {
        "organism_id": "ORG_000001", "scientific_name": "Lifecycle testii",
    })
    db.insert_row("samples", {
        "sample_id": "SMP_000001", "organism_id": "ORG_000001",
    })
    for suffix in ("1", "2"):
        db.insert_row("assemblies", {
            "assembly_id": f"ASM_00000{suffix}", "sample_id": "SMP_000001",
        })
        db.insert_row("annotations", {
            "annotation_id": f"ANN_00000{suffix}",
            "assembly_id": f"ASM_00000{suffix}",
        })
        db.insert_row("files", {
            "file_id": f"FIL_00000{suffix}",
            "entity_type": "assembly",
            "entity_id": f"ASM_00000{suffix}",
            "file_role": "genome_fasta",
            "format": "fasta",
            "compression": "none",
            "relative_path": f"raw/assemblies/ASM_00000{suffix}/genome.fasta",
            "size_bytes": 1,
            "sha256": str(suffix) * 64,
            "status": "CHECKSUM_VERIFIED",
        })
    try:
        yield project, db
    finally:
        db.close()


def _retire(db: Database, entity_type: str, entity_id: str, reason="mistake"):
    return apply_lifecycle_event(
        db,
        entity_type,
        entity_id,
        action="RETIRE",
        reason_code="accidental_import",
        reason=reason,
        actor="tester",
    )


def test_retirement_is_inherited_and_restore_is_append_only(lifecycle_project):
    _project, db = lifecycle_project
    retired = _retire(db, "sample", "SMP_000001")
    assert retired["changed"] is True
    assert db.is_entity_retired("sample", "SMP_000001")
    assert db.is_entity_retired("assembly", "ASM_000001")
    assert db.is_entity_retired("annotation", "ANN_000001")

    with pytest.raises(ValidationError, match="restore that direct retirement root"):
        apply_lifecycle_event(
            db, "assembly", "ASM_000001", action="RESTORE",
            reason="cannot bypass parent", actor="tester",
        )

    restored = apply_lifecycle_event(
        db, "sample", "SMP_000001", action="RESTORE",
        reason="verified", actor="tester",
    )
    assert restored["event"]["reverts_event_id"] == retired["event"]["event_id"]
    assert not db.is_entity_retired("assembly", "ASM_000001")
    events = db.query(
        "SELECT action, reverts_event_id, change_id FROM entity_lifecycle_events "
        "WHERE object_type='sample' AND object_id='SMP_000001' ORDER BY event_id"
    )
    assert [row["action"] for row in events] == ["RETIRE", "RESTORE"]
    changes = db.query(
        "SELECT old_value, new_value, reverts_change_id FROM changes "
        "WHERE object_type='sample' AND object_id='SMP_000001' AND field='lifecycle' "
        "ORDER BY change_id"
    )
    assert [(row["old_value"], row["new_value"]) for row in changes] == [
        ("ACTIVE", "RETIRED"), ("RETIRED", "ACTIVE"),
    ]
    assert changes[1]["reverts_change_id"] is not None


def test_direct_child_retirement_survives_parent_restore(lifecycle_project):
    _project, db = lifecycle_project
    _retire(db, "sample", "SMP_000001", "parent")
    _retire(db, "assembly", "ASM_000001", "child")
    apply_lifecycle_event(
        db, "sample", "SMP_000001", action="RESTORE",
        reason="restore parent", actor="tester",
    )
    assert db.is_entity_retired("assembly", "ASM_000001")
    assert not db.is_entity_retired("assembly", "ASM_000002")
    roots = db.effective_retirements("assembly", "ASM_000001")
    assert {(row["retired_by_type"], row["retired_by_id"]) for row in roots} == {
        ("assembly", "ASM_000001"),
    }


def test_plan_is_read_only_and_reports_references(lifecycle_project):
    _project, db = lifecycle_project
    db.insert_row("accessions", {
        "internal_type": "assembly", "internal_id": "ASM_000001",
        "namespace": "TEST", "accession": "A1",
    })
    db.insert_row("file_locations", {
        "file_id": "FIL_000001", "location_name": "mirror",
        "location_type": "sftp", "uri": "remote://mirror/x",
        "relative_path": "raw/x", "sha256": "1" * 64,
        "size_bytes": 1, "status": "AVAILABLE",
    })
    db.insert_row("releases", {
        "version": "v1", "created_at": "2026-09-01T00:00:00+08:00",
        "profile": "p", "path": "releases/v1", "summary": "{}",
    })
    db.insert_row("release_members", {
        "release_version": "v1", "file_id": "FIL_000001",
        "entity_type": "assembly", "entity_id": "ASM_000001",
        "release_path": "data/assembly/ASM_000001/genome.fasta",
        "sha256": "1" * 64, "size_bytes": 1,
    })
    plan = lifecycle_plan(db, "TEST:A1", action="RETIRE")
    assert plan["entity_counts"]["assembly"] == 1
    assert plan["entity_counts"]["annotation"] == 1
    assert plan["reference_counts"]["files"] == 1
    assert plan["reference_counts"]["accessions"] == 1
    assert plan["reference_counts"]["remote_locations"] == 1
    assert plan["historical_release_versions"] == ["v1"]
    assert set(plan["physical_changes"].values()) == {0}
    assert db.query("SELECT COUNT(*) AS n FROM entity_lifecycle_events")[0]["n"] == 0


def test_ncbi_reimport_requires_explicit_restore(lifecycle_project):
    _project, db = lifecycle_project
    db.insert_row("accessions", {
        "internal_type": "assembly", "internal_id": "ASM_000001",
        "namespace": "NCBI_Assembly", "accession": "GCF_000001.1",
    })
    _retire(db, "assembly", "ASM_000001")
    with pytest.raises(ValidationError, match="retired assembly ASM_000001"):
        _find_archived_assembly(db, "GCF_000001.1")
    with pytest.raises(ValidationError, match="retired assembly ASM_000001"):
        _PlanBuilder(db)._find_assembly("GCF_000001.1")


def test_active_consumers_exclude_retired_entities(lifecycle_project, monkeypatch, tmp_path):
    project, db = lifecycle_project
    for suffix in ("1", "2"):
        db.insert_row("qc_results", {
            "entity_type": "assembly", "entity_id": f"ASM_00000{suffix}",
            "input_identity": f"entity:ASM_00000{suffix}",
            "qc_stage": "test", "metric_name": "ok", "metric_value": "1",
            "metric_numeric": 1, "tool": "test", "tool_version": "1",
            "parameter_set": "default", "evaluated_at": "2026-09-01T00:00:00+08:00",
        })
        db.insert_row("decisions", {
            "entity_type": "assembly", "entity_id": f"ASM_00000{suffix}",
            "profile": "p", "profile_version": 1, "decision": "PASS",
            "reason_codes": "[]", "observed": "{}", "thresholds": "{}",
            "evaluated_at": "2026-09-01T00:00:00+08:00",
        })
    _retire(db, "assembly", "ASM_000001")

    graph = entity_graph(db, "ORG_000001")
    assert [row["assembly_id"] for row in graph["assemblies"]] == ["ASM_000002"]
    assert entity_graph(db, "ASM_000001")["assemblies"][0]["assembly_id"] == "ASM_000001"
    assert [row["entity_id"] for row in reports.qc_rows(db)] == ["ASM_000002"]
    assert {row["entity_id"] for row in release_files_for(db, "p")} == {"ASM_000002"}

    recipe = Recipe(
        name="test", tool_name="test", description="", entity_type="assembly",
        file_role="genome_fasta", fmt="fasta", input_kind="file", database="",
        database_version="", output_subdir="", output_kind="file",
        output_name_template="", output_suffix="", arguments=[], parameters={},
        result_parser="", max_hits_per_query=1, raw={},
    )
    assert [row["entity_id"] for row in candidate_files(db, recipe)] == ["ASM_000002"]

    with pytest.raises(ValidationError, match="retired by assembly ASM_000001"):
        run_external_command(
            db, project, ["true"], step="manual_check",
            entity_type="assembly", entity_id="ASM_000001",
        )

    called: list[str] = []
    monkeypatch.setattr(qc_module, "qc_file", lambda _db, _project, file_id, **_kwargs: (
        called.append(file_id) or {"file_id": file_id, "ok": True, "error": None}
    ))
    qc_module.qc_all(db, project)
    assert called == ["FIL_000002"]

    output = reports.export_metadata_report(db, project, tmp_path / "active-report")
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tables"]["assemblies.tsv"]["row_count"] == 1
    history = reports.export_metadata_report(
        db, project, tmp_path / "history-report", include_retired=True,
    )
    history_manifest = json.loads((history / "manifest.json").read_text(encoding="utf-8"))
    assert history_manifest["tables"]["assemblies.tsv"]["row_count"] == 2


def test_cli_retire_restore_records_workflow(lifecycle_project, capsys):
    project, db = lifecycle_project
    db.close()
    assert main([
        "--project", str(project.root), "retire", "ASM_000001",
        "--reason-code", "wrong_source", "--reason", "wrong provider",
    ]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["will_change"] is True
    assert main([
        "--project", str(project.root), "retire", "ASM_000001",
        "--reason-code", "wrong_source", "--reason", "wrong provider",
        "--actor", "tester", "--apply", "--yes",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--project", str(project.root), "restore", "ASM_000001",
        "--reason", "correct source verified", "--actor", "tester",
        "--apply", "--yes",
    ]) == 0
    capsys.readouterr()
    check = Database(project.db_path)
    try:
        assert [row["step"] for row in check.query(
            "SELECT step FROM workflow_runs WHERE step LIKE 'lifecycle_%' ORDER BY rowid"
        )] == ["lifecycle_retire", "lifecycle_restore"]
        assert not check.is_entity_retired("assembly", "ASM_000001")
    finally:
        check.close()


def test_retire_same_entity_twice_is_idempotent(lifecycle_project):
    _project, db = lifecycle_project
    first = _retire(db, "assembly", "ASM_000001")
    second = _retire(db, "assembly", "ASM_000001")
    assert second["changed"] is False
    assert second["event"]["event_id"] == first["event"]["event_id"]
    assert db.query(
        "SELECT COUNT(*) AS n FROM entity_lifecycle_events "
        "WHERE object_type='assembly' AND object_id='ASM_000001'"
    )[0]["n"] == 1


def test_list_retired_entities_direct_only_excludes_inherited(lifecycle_project):
    _project, db = lifecycle_project
    _retire(db, "sample", "SMP_000001")
    rows = list_retired_entities(db, direct_only=True)
    assert [(row["entity_type"], row["entity_id"]) for row in rows] == [
        ("sample", "SMP_000001"),
    ]


def test_list_retired_entities_effective_includes_inherited(lifecycle_project):
    _project, db = lifecycle_project
    _retire(db, "sample", "SMP_000001")
    rows = list_retired_entities(db)
    assert {(row["entity_type"], row["entity_id"]) for row in rows} == {
        ("sample", "SMP_000001"),
        ("assembly", "ASM_000001"),
        ("assembly", "ASM_000002"),
        ("annotation", "ANN_000001"),
        ("annotation", "ANN_000002"),
    }
    assert {row["retired_by_id"] for row in rows} == {"SMP_000001"}


def test_require_active_entity_rejects_retired_entity(lifecycle_project):
    _project, db = lifecycle_project
    db.require_active_entity("assembly", "ASM_000001")
    _retire(db, "assembly", "ASM_000001")
    with pytest.raises(ValidationError, match="retired by assembly ASM_000001"):
        db.require_active_entity("assembly", "ASM_000001")
    with pytest.raises(ValidationError, match="retired by assembly ASM_000001"):
        db.require_active_entity("annotation", "ANN_000001")


def test_ingest_file_rejects_retired_entity(lifecycle_project, tmp_path):
    project, db = lifecycle_project
    _retire(db, "assembly", "ASM_000001")
    source = tmp_path / "genome.fa"
    source.write_text(">seq\nACGT\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="retired by assembly ASM_000001"):
        ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")


def test_entity_subtree_from_organism_cascades_to_descendants(lifecycle_project):
    _project, db = lifecycle_project
    db.insert_row("runs", {"run_id": "RUN_000001", "sample_id": "SMP_000001"})
    assert entity_subtree(db, "organism", "ORG_000001") == {
        "organism": ["ORG_000001"],
        "sample": ["SMP_000001"],
        "run": ["RUN_000001"],
        "assembly": ["ASM_000001", "ASM_000002"],
        "annotation": ["ANN_000001", "ANN_000002"],
    }


def test_entity_subtree_run_is_a_direct_target(lifecycle_project):
    _project, db = lifecycle_project
    db.insert_row("runs", {"run_id": "RUN_000001", "sample_id": "SMP_000001"})
    assert entity_subtree(db, "run", "RUN_000001") == {
        "organism": [], "sample": [], "run": ["RUN_000001"],
        "assembly": [], "annotation": [],
    }


def test_entity_subtree_annotation_is_a_direct_target(lifecycle_project):
    _project, db = lifecycle_project
    assert entity_subtree(db, "annotation", "ANN_000001") == {
        "organism": [], "sample": [], "run": [], "assembly": [],
        "annotation": ["ANN_000001"],
    }


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"action": "DELETE"}, "unsupported lifecycle action"),
        ({"reason": "  "}, "reason is required"),
        ({"actor": " "}, "actor is required"),
    ],
)
def test_apply_lifecycle_event_validates_action_reason_actor(
    lifecycle_project, override, message,
):
    _project, db = lifecycle_project
    kwargs = {
        "action": "RETIRE", "reason": "mistake", "actor": "tester",
        "reason_code": "accidental_import",
    }
    kwargs.update(override)
    with pytest.raises(ValidationError, match=message):
        apply_lifecycle_event(db, "assembly", "ASM_000001", **kwargs)


def test_restore_of_active_entity_reports_already_active(lifecycle_project):
    _project, db = lifecycle_project
    with pytest.raises(ValidationError, match="already active"):
        apply_lifecycle_event(
            db, "assembly", "ASM_000001", action="RESTORE",
            reason="not needed", actor="tester",
        )


def _lifecycle_args(**overrides):
    values = {
        "command": "retire", "identifier": "ASM_000001", "apply": True,
        "yes": True, "actor": "tester", "reason": "mistake",
        "reason_code": "accidental_import", "evidence": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_lifecycle_apply_raises_plan_blocker(lifecycle_project):
    project, db = lifecycle_project
    with pytest.raises(ValidationError, match="already active"):
        cli._cmd_lifecycle(_lifecycle_args(command="restore"), project, db)


def test_lifecycle_apply_requires_yes_outside_terminal(lifecycle_project):
    project, db = lifecycle_project
    with pytest.raises(ValidationError, match="requires --yes"):
        cli._cmd_lifecycle(_lifecycle_args(yes=False), project, db)


def test_lifecycle_apply_requires_actor_without_user_env(lifecycle_project, monkeypatch):
    project, db = lifecycle_project
    monkeypatch.delenv("USER", raising=False)
    with pytest.raises(ValidationError, match="--actor is required"):
        cli._cmd_lifecycle(_lifecycle_args(actor=None), project, db)


def _fake_terminal(monkeypatch, confirmed):
    class TTY:
        def __init__(self):
            self.writes = []

        def isatty(self):
            return True

        def write(self, value):
            self.writes.append(value)
            return len(value)

        def flush(self):
            return None

    out = TTY()
    monkeypatch.setattr(sys, "stdin", TTY())
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setitem(sys.modules, "questionary", SimpleNamespace(
        confirm=lambda *_a, **_k: SimpleNamespace(ask=lambda: confirmed),
    ))
    return out


def test_lifecycle_apply_cancelled_via_prompt(lifecycle_project, monkeypatch):
    project, db = lifecycle_project
    out = _fake_terminal(monkeypatch, False)
    assert cli._cmd_lifecycle(_lifecycle_args(yes=False), project, db) == 0
    assert any("cancelled" in line for line in out.writes)
    assert not db.is_entity_retired("assembly", "ASM_000001")
    assert db.query("SELECT COUNT(*) AS n FROM entity_lifecycle_events")[0]["n"] == 0


def test_lifecycle_apply_confirmed_via_prompt(lifecycle_project, monkeypatch):
    project, db = lifecycle_project
    _fake_terminal(monkeypatch, True)
    assert cli._cmd_lifecycle(_lifecycle_args(yes=False), project, db) == 0
    assert db.is_entity_retired("assembly", "ASM_000001")
