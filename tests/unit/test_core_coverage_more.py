"""Contracts not exercised elsewhere: CLI dispatch/reporting, migration and
retirement database paths, file standardization fallbacks, the workflow state
machine, rule-engine policies and built-in QC aggregation.

Every test drives the real public surface (``operon.cli.main`` or the module
API) and asserts observable results: exit codes, stdout/stderr, database rows,
audit records and files on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from operon import cli, qc, rules
from operon import files as files_mod
from operon.cli import main
from operon.config import Project, load_project
from operon.database import Database
from operon.errors import QCError, ValidationError
from operon.files import ingest_file, standardize_file, verify_local_file_identity
from operon.workflow import run_external_command


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def project(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    return load_project(tmp_path)


@pytest.fixture
def project_db(project):
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def _organism(db: Database, organism_id: str = "ORG_000001") -> None:
    db.insert_row("organisms", {"organism_id": organism_id, "scientific_name": "Example"})


def _graph(db: Database) -> None:
    _organism(db)
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("runs", {"run_id": "RUN_000001", "sample_id": "SMP_000001"})
    db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
    db.insert_row("annotations", {"annotation_id": "ANN_000001", "assembly_id": "ASM_000001"})


def _member(relative_path: str, sha: str) -> dict:
    return {
        "file_id": "FIL_000001", "entity_type": "organism", "entity_id": "ORG_000001",
        "file_role": "other", "format": "other", "compression": "none",
        "relative_path": relative_path, "source_url": None, "size_bytes": 1,
        "sha256": sha, "status": "CHECKSUM_VERIFIED", "effective_decision": "PASS",
    }


class _TTY:
    """Stand-in for a terminal; optionally forwards writes to a real stream."""

    def __init__(self, interactive: bool = True, sink=None) -> None:
        self.interactive = interactive
        self.sink = sink

    def isatty(self) -> bool:
        return self.interactive

    def write(self, value: str) -> int:
        if self.sink is not None:
            return self.sink.write(value)
        return len(value)

    def flush(self) -> None:
        if self.sink is not None:
            self.sink.flush()


class _Prompt:
    def __init__(self, answer):
        self.answer = answer

    def ask(self):
        return self.answer


def _fake_questionary(monkeypatch, *, select=None, confirm=None) -> None:
    monkeypatch.setitem(sys.modules, "questionary", SimpleNamespace(
        Choice=lambda label, value: (label, value),
        select=lambda *_a, **_k: _Prompt(select),
        confirm=lambda *_a, **_k: _Prompt(confirm),
    ))


# --------------------------------------------------------------------------- #
# cli.py - manual state, metadata warnings, dry-run reporting
# --------------------------------------------------------------------------- #


def test_set_state_cli_audits_transition_and_rejects_illegal(project, capsys):
    db = Database(project.db_path)
    _organism(db)
    db.close()
    root = str(project.root)

    assert main([
        "--project", root, "set-state", "--entity-type", "organism",
        "--entity-id", "ORG_000001", "--state", "METADATA_VALIDATED",
        "--message", "manually validated",
    ]) == 0
    assert "organism ORG_000001 -> METADATA_VALIDATED" in capsys.readouterr().out

    db = Database(project.db_path)
    try:
        state = db.query(
            "SELECT state, message FROM entity_state "
            "WHERE entity_type='organism' AND entity_id='ORG_000001'"
        )[0]
        assert state["state"] == "METADATA_VALIDATED"
        assert state["message"] == "manually validated"
        change = db.query(
            "SELECT field, old_value, new_value, reason FROM changes "
            "WHERE object_type='entity_state' AND object_id='organism:ORG_000001'"
        )[0]
        assert (change["field"], change["old_value"], change["new_value"]) == (
            "state", None, "METADATA_VALIDATED")
        assert change["reason"] == "manually validated"
    finally:
        db.close()

    # METADATA_VALIDATED -> RELEASED is not a declared transition.
    assert main([
        "--project", root, "set-state", "--entity-type", "organism",
        "--entity-id", "ORG_000001", "--state", "RELEASED",
    ]) == 2
    error = capsys.readouterr().err
    assert "illegal transition METADATA_VALIDATED -> RELEASED" in error
    assert "--force" in error

    assert main([
        "--project", root, "set-state", "--entity-type", "organism",
        "--entity-id", "ORG_000001", "--state", "RELEASED", "--force",
        "--message", "curator override",
    ]) == 0
    db = Database(project.db_path)
    try:
        assert db.get_entity_state("organism", "ORG_000001") == "RELEASED"
        forced = db.query(
            "SELECT reason FROM changes WHERE old_value='METADATA_VALIDATED' "
            "AND new_value='RELEASED'"
        )[0]
        assert forced["reason"] == "curator override"
    finally:
        db.close()


def test_add_warns_about_fields_the_schema_does_not_know(project, capsys):
    root = str(project.root)
    # The CLI warns about the unknown field and then refuses to store it.
    assert main([
        "--project", root, "add", "organism", "--id", "ORG_000001",
        "--field", "scientific_name=Example", "--field", "mystery_field=1",
    ]) == 2
    captured = capsys.readouterr()
    assert "warning: unknown field 'mystery_field'" in captured.err
    assert "add it to" in captured.err
    assert "unknown field" in captured.err
    db = Database(project.db_path)
    try:
        assert not db.entity_exists("organism", "ORG_000001")
    finally:
        db.close()


def test_ncbi_reconcile_cli_reports_dry_run(project, monkeypatch, capsys):
    monkeypatch.setattr(
        "operon.ncbi_reconcile.plan_ncbi_reconciliation",
        lambda _db: {"duplicates": [], "planned": 0},
    )
    assert main(["--project", str(project.root), "ncbi-reconcile"]) == 0
    captured = capsys.readouterr()
    assert '"planned": 0' in captured.out
    assert "dry-run: no business row or file was changed" in captured.err

    # --apply must not print the dry-run notice.
    monkeypatch.setattr(
        "operon.ncbi_reconcile.apply_ncbi_reconciliation",
        lambda _db, _project, actor=None: {"applied": 1, "actor": actor},
    )
    assert main([
        "--project", str(project.root), "ncbi-reconcile", "--apply", "--actor", "tester",
    ]) == 0
    captured = capsys.readouterr()
    assert '"applied": 1' in captured.out
    assert "dry-run" not in captured.err


def test_environment_summary_ignores_corrupt_documents(project, capsys):
    db = Database(project.db_path)
    try:
        with db.transaction():
            db.record_environment({"system": {"os": "Linux"}, "capture_status": "complete"})
            db.record_environment({"system": {"os": "Darwin"}, "capture_status": "complete"})
        rows = db.query(
            "SELECT environment_id FROM execution_environments ORDER BY environment_id"
        )
        healthy, broken = rows[0]["environment_id"], rows[1]["environment_id"]
        with db.transaction():
            db.conn.execute(
                "UPDATE execution_environments SET document='{not json' WHERE environment_id=?",
                (broken,),
            )
            db.conn.execute(
                "INSERT INTO workflow_runs(run_id, step, status, started_at, environment_id) "
                "VALUES('WF_ENV_BAD','qc','completed','2026-01-01T00:00:00+00:00',?)",
                (broken,),
            )
    finally:
        db.close()

    assert main(["--project", str(project.root), "workflow", "show", "WF_ENV_BAD"]) == 0
    out = capsys.readouterr().out
    assert broken in out
    assert "Darwin" not in out

    assert main(["--project", str(project.root), "environments", "list"]) == 0
    out = capsys.readouterr().out
    assert healthy in out and broken in out


def test_unknown_subcommands_raise_validation_errors(project_db, capsys):
    project, db = project_db
    with pytest.raises(ValidationError, match="unknown recipes command"):
        cli._cmd_recipes(SimpleNamespace(recipes_command="nope"), project, db)
    with pytest.raises(ValidationError, match="unknown profiles command"):
        cli._cmd_profiles(SimpleNamespace(profiles_command="nope"), project, db)
    with pytest.raises(ValidationError, match="unknown workflow command"):
        cli._cmd_workflow(SimpleNamespace(workflow_command="nope"), db)


# --------------------------------------------------------------------------- #
# cli.py - QC per-item reporting
# --------------------------------------------------------------------------- #


def test_qc_command_reports_progress_failures_and_siblings(project_db, monkeypatch, capsys):
    project, db = project_db
    results = [
        {
            "file_id": "FIL_1", "ok": False, "error": "bad bytes",
            "file_qc_state": "QC_FAILED", "entity_qc_state": "QC_FAILED",
            "file_statuses": [
                {"file_id": "FIL_1", "file_role": "reads_r1", "qc_state": "QC_FAILED"},
                {"file_id": "FIL_9", "file_role": "reads_r2", "qc_state": "QC_COMPLETE"},
            ],
        },
        {
            "file_id": "FIL_2", "ok": False, "skipped": True, "error": "REMOTE_ONLY",
            "file_qc_state": "QC_UNKNOWN", "entity_qc_state": "QC_UNKNOWN",
            "file_statuses": [],
        },
        {
            "file_id": "FIL_3", "ok": True, "file_qc_state": "QC_COMPLETE",
            "entity_qc_state": "QC_COMPLETE", "file_statuses": [],
        },
    ]

    def fake_qc_all(_db, _project, **kwargs):
        callback = kwargs["progress_callback"]
        for index, result in enumerate(results, start=1):
            callback(index, len(results), result)
        return results

    monkeypatch.setattr("operon.qc.qc_all", fake_qc_all)
    args = SimpleNamespace(
        entity_type=None, entity_id=None, file_id=None, sample_size=None,
        phred_offset=None, rehash=False,
    )
    assert cli._cmd_qc(args, project, db) == 1
    captured = capsys.readouterr()
    assert "[1/3] FIL_1: FAILED (bad bytes)" in captured.out
    assert "[2/3] FIL_2: SKIPPED (REMOTE_ONLY)" in captured.out
    assert "[3/3] FIL_3: OK" in captured.out
    assert "FIL_9 (reads_r2): QC_COMPLETE" in captured.out
    assert "FIL_1 (reads_r1): QC_FAILED" not in captured.out  # own file is not a sibling
    assert "FIL_1: FAILED bad bytes" in captured.err
    assert "FIL_2: SKIPPED REMOTE_ONLY" in captured.err
    assert "QC complete: 1/3 file(s) passed built-in stages, 1 skipped (REMOTE_ONLY)" in captured.out


def test_qc_command_fails_when_only_requested_file_was_skipped(project_db, monkeypatch, capsys):
    project, db = project_db
    monkeypatch.setattr("operon.qc.qc_all", lambda *_a, **_k: [{
        "file_id": "FIL_1", "ok": False, "skipped": True, "error": "REMOTE_ONLY",
        "file_qc_state": "QC_UNKNOWN", "entity_qc_state": "QC_UNKNOWN", "file_statuses": [],
    }])
    args = SimpleNamespace(
        entity_type=None, entity_id=None, file_id="FIL_1", sample_size=None,
        phred_offset=None, rehash=False,
    )
    assert cli._cmd_qc(args, project, db) == 1
    captured = capsys.readouterr()
    assert "error: requested file FIL_1 was skipped: REMOTE_ONLY" in captured.err


def test_qc_command_reports_failed_entity_state_without_failed_files(project_db, monkeypatch):
    project, db = project_db
    monkeypatch.setattr("operon.qc.qc_all", lambda *_a, **_k: [{
        "file_id": "FIL_1", "ok": True, "file_qc_state": "QC_COMPLETE",
        "entity_qc_state": "QC_FAILED", "file_statuses": [],
    }])
    args = SimpleNamespace(
        entity_type=None, entity_id=None, file_id=None, sample_size=None,
        phred_offset=None, rehash=False,
    )
    assert cli._cmd_qc(args, project, db) == 1


# --------------------------------------------------------------------------- #
# cli.py - eval/curate/release/export/adopt/fanout rendering
# --------------------------------------------------------------------------- #


def test_evaluate_cli_warns_and_confirms_curated_reevaluation(project, monkeypatch, capsys):
    # Keep a handle on the captured stream: replacing sys.stdout below must not
    # hide the text the CLI prints.
    captured_stdout = sys.stdout
    db = Database(project.db_path)
    profile = project.config["qc"]["default_profile"]
    try:
        _graph(db)
        db.insert_qc_result({
            "entity_type": "assembly", "entity_id": "ASM_000001", "file_id": None,
            "file_sha256": None, "input_identity": "entity:assembly:ASM_000001",
            "qc_stage": "external", "metric_name": "score", "metric_value": "1",
            "metric_numeric": 1.0, "metric_unit": None, "tool": "tool",
            "tool_version": "1", "parameter_set": "external",
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        })
        db.upsert_decision({
            "entity_type": "assembly", "entity_id": "ASM_000001", "profile": profile,
            "decision": "PASS", "curated_decision": "PASS", "curated_by": "reviewer",
            "curated_reason": "checked", "reason_codes": "[]", "observed": "{}",
            "thresholds": "{}", "evaluated_at": "2026-01-01T00:00:00+00:00",
        })
    finally:
        db.close()

    # Non-interactive: refuses before any write.
    real_evaluate_all = cli.evaluate_all
    monkeypatch.setattr(cli.sys, "stdin", _TTY(False))
    monkeypatch.setattr(cli.sys, "stdout", _TTY(False))
    monkeypatch.setattr(cli, "evaluate_all", lambda *_a: pytest.fail("evaluated without confirmation"))
    assert main(["--project", str(project.root), "evaluate", "--entity-type", "assembly"]) == 2
    assert "pass --yes to confirm" in capsys.readouterr().err

    # Interactive decline: prints the cancellation notice and exits 0.
    _fake_questionary(monkeypatch, confirm=False)
    tty_out = _TTY(True, sink=captured_stdout)
    monkeypatch.setattr(cli.sys, "stdin", _TTY(True))
    monkeypatch.setattr(cli.sys, "stdout", tty_out)
    assert main(["--project", str(project.root), "evaluate", "--entity-type", "assembly"]) == 0
    assert "Evaluation cancelled; no rows were changed." in capsys.readouterr().out

    # --yes proceeds through the real evaluation.
    monkeypatch.setattr(cli, "evaluate_all", real_evaluate_all)
    monkeypatch.setattr(cli.sys, "stdin", _TTY(False))
    monkeypatch.setattr(cli.sys, "stdout", _TTY(False, sink=captured_stdout))
    assert main([
        "--project", str(project.root), "evaluate", "--yes", "--entity-type", "assembly",
    ]) == 0
    out = capsys.readouterr().out
    assert "assembly" in out and "decision" in out


def test_curate_cli_records_audited_override(project, capsys):
    db = Database(project.db_path)
    profile = project.config["qc"]["default_profile"]
    try:
        _organism(db)
        db.upsert_decision({
            "entity_type": "organism", "entity_id": "ORG_000001", "profile": profile,
            "decision": "NOT_EVALUATED", "reason_codes": "[]", "observed": "{}",
            "thresholds": "{}", "evaluated_at": "2026-01-01T00:00:00+00:00",
        })
    finally:
        db.close()

    assert main([
        "--project", str(project.root), "curate",
        "--entity-type", "organism", "--entity-id", "ORG_000001",
        "--profile", profile, "--decision", "pass", "--reviewer", "curator",
        "--reason", "manual inspection", "--evidence", "report.tsv",
    ]) == 0
    assert (
        f"recorded curated decision pass for organism ORG_000001"
        in capsys.readouterr().out
    )

    db = Database(project.db_path)
    try:
        row = db.query(
            "SELECT curated_decision, curated_by, curated_reason, curated_evidence "
            "FROM decisions WHERE entity_type='organism' AND entity_id='ORG_000001'"
        )[0]
        assert (row["curated_decision"], row["curated_by"]) == ("PASS", "curator")
        assert row["curated_reason"] == "manual inspection"
        assert row["curated_evidence"] == "report.tsv"
        assert db.effective_decision("organism", "ORG_000001", profile) == "PASS"
        change = db.query(
            "SELECT field, old_value, new_value, reason, actor FROM changes "
            "WHERE object_type='decision'"
        )[0]
        assert (change["field"], change["old_value"], change["new_value"]) == (
            "curated_decision", "NOT_EVALUATED", "PASS")
        assert change["actor"] == "curator"
        assert db.get_entity_state("organism", "ORG_000001") == "ACCEPTED"
    finally:
        db.close()

    # Unknown profile: the audit-less path is rejected.
    assert main([
        "--project", str(project.root), "curate",
        "--entity-type", "organism", "--entity-id", "ORG_000001",
        "--profile", "does_not_exist", "--decision", "PASS", "--reviewer", "r",
        "--reason", "why",
    ]) == 2
    assert "no automatic decision" in capsys.readouterr().err


def test_release_cli_creates_snapshot_and_copy_alias(project, monkeypatch, capsys):
    import operon.release as release_mod

    source = project.root / "raw" / "file"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("y", encoding="utf-8")
    monkeypatch.setattr(
        release_mod, "release_files_for",
        lambda *_a, **_k: [_member("raw/file", hashlib.sha256(b"y").hexdigest())],
    )

    assert main([
        "--project", str(project.root), "release", "--version", "2026.09",
        "--profile", "p", "--copy-files",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["version"] == "2026.09"
    published = Path(result["path"]) / "data" / "organism" / "ORG_000001" / "file"
    assert published.read_text(encoding="utf-8") == "y"

    db = Database(project.db_path)
    try:
        assert db.query("SELECT version FROM releases")[0]["version"] == "2026.09"
    finally:
        db.close()


def test_adopt_cli_requires_single_file_identifiers(project, capsys):
    root = str(project.root)
    derived = project.root / "derived.fa"
    derived.write_text(">x\nAC\n", encoding="utf-8")
    assert main([
        "--project", root, "adopt", "--file", str(derived),
    ]) == 2
    assert "single-file adopt requires --entity-type, --entity-id, --role" in capsys.readouterr().err

    assert main([
        "--project", root, "adopt", "--file", str(derived),
        "--entity-type", "assembly", "--entity-id", "ASM_000001", "--role", "protein_fasta",
    ]) == 2
    assert "requires at least one --derived-from FILE_ID" in capsys.readouterr().err


def test_classify_sequences_cli_prints_label_counts(project_db, monkeypatch, capsys):
    project, db = project_db
    monkeypatch.setattr("operon.classify.classify_sequences", lambda *_a, **_k: {
        "sequences": 5, "unlabeled": 2, "files": 1, "profile": "demo",
        "label_counts": {"ribosomal": 3}, "labels_written": 3, "labels_removed": 0,
        "run_id": "WF_CLASSIFY",
    })
    assert cli._cmd_classify_sequences(
        SimpleNamespace(profile="demo"), project, db) == 0
    out = capsys.readouterr().out
    assert "labeled 3 of 5 sequence(s)" in out
    assert "ribosomal" in out
    assert "labels written: 3, removed: 0 (run WF_CLASSIFY)" in out


def test_select_sequences_cli_records_entity_scope(project_db, monkeypatch, capsys):
    project, db = project_db
    captured: dict = {}

    def fake_select(_db, _project, **kwargs):
        captured.update(kwargs)
        return {"selected": 1, "total": 4, "excluded": 3, "output": "out.fa"}

    monkeypatch.setattr("operon.sequence_tools.select_sequences", fake_select)
    args = SimpleNamespace(
        file_id="FIL_1", analysis=[], subject_like=None, evalue_max=None,
        min_span=None, hit_type=None, require_no_hit=False,
        entity_type="assembly", entity_id="ASM_000001", out="out.fa", manifest=None,
    )
    assert cli._cmd_select_sequences(args, project, db) == 0
    assert captured["entity_id"] == "ASM_000001"
    assert "--entity-id ASM_000001" in captured["command"]
    assert "selected 1 of 4 sequence(s), excluded 3" in capsys.readouterr().out


def test_fanout_cli_records_parent_run(project_db, monkeypatch, capsys):
    project, db = project_db
    captured: dict = {}

    def fake_fanout(_db, _project, **kwargs):
        captured.update(kwargs)
        return {
            "dry_run": False, "units": [], "created": 0, "reused": 0,
            "run_id": "WF_FANOUT",
        }

    monkeypatch.setattr("operon.fanout.fanout_units", fake_fanout)
    args = SimpleNamespace(
        assignments_file="FIL_1", source_file=["FIL_2"], entity_type="assembly",
        entity_id="ASM_000001", role_prefix="unit", unit_column="unit",
        seqid_column="seqid", parent_run_id="WF_PARENT", actor="tester", dry_run=False,
    )
    assert cli._cmd_fanout(args, project, db) == 0
    assert captured["parent_run_id"] == "WF_PARENT"
    assert "--parent-run-id WF_PARENT" in captured["command"]
    assert "fanout: 0 created, 0 reused (run WF_FANOUT)" in capsys.readouterr().out


def test_snapshot_document_falls_back_to_raw_text(project, capsys):
    db = Database(project.db_path)
    try:
        with db.transaction():
            db.conn.execute(
                "INSERT INTO recipe_snapshots(recipe_name, recipe_version, recipe_sha256, "
                "recipe_document, recorded_at) VALUES('legacy', 1, 'sha', "
                "'not: [valid: json', '2026-01-01T00:00:00+00:00')"
            )
    finally:
        db.close()
    assert main(["--project", str(project.root), "recipes", "show", "legacy"]) == 0
    assert "not: [valid: json" in capsys.readouterr().out


def test_profiles_show_snapshot_by_id(project, capsys):
    db = Database(project.db_path)
    try:
        document = json.dumps({"kind": "qc", "version": 1, "required": [], "warnings": []})
        first = db.record_profile("demo", 1, "sha-1", document, "2026-01-01T00:00:00+00:00")
        db.record_profile("demo", 2, "sha-2", document, "2026-01-02T00:00:00+00:00")
    finally:
        db.close()

    assert main([
        "--project", str(project.root), "profiles", "show", "demo",
        "--snapshot-id", str(first),
    ]) == 0
    assert "kind: qc" in capsys.readouterr().out
    assert main(["--project", str(project.root), "profiles", "show", "demo"]) == 0
    assert "kind: qc" in capsys.readouterr().out
    assert main([
        "--project", str(project.root), "profiles", "show", "demo", "--snapshot-id", "99999",
    ]) == 2
    assert "no snapshot recorded for profile 'demo' with snapshot id 99999" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# cli.py - show/lifecycle/retired/backup dispatch
# --------------------------------------------------------------------------- #


def test_show_cli_renders_supersessions_and_retirements(project, capsys):
    db = Database(project.db_path)
    try:
        _graph(db)
        db.supersede_entity(
            "assembly", "ASM_000001", "assembly", "ASM_000002",
            reason="duplicate import",
        )
    finally:
        db.close()
    assert main(["--project", str(project.root), "show", "ORG_000001"]) == 0
    assert "Supersessions (1)" in capsys.readouterr().out

    assert main([
        "--project", str(project.root), "retire", "SMP_000001",
        "--reason-code", "accidental_import", "--reason", "mistake",
        "--actor", "tester", "--apply", "--yes",
    ]) == 0
    capsys.readouterr()
    assert main([
        "--project", str(project.root), "show", "ASM_000001", "--include-retired",
    ]) == 0
    # Retirement is inherited: sample root plus its assembly and annotation.
    assert "Retirements (3)" in capsys.readouterr().out

    # Unknown identifiers with no sequence hits propagate as a CLI error.
    assert main(["--project", str(project.root), "show", "NOPE"]) == 2
    assert "was not found" in capsys.readouterr().err


def test_retire_twice_reports_already_applied(project, capsys):
    db = Database(project.db_path)
    try:
        _graph(db)
    finally:
        db.close()
    first = [
        "--project", str(project.root), "retire", "ASM_000001",
        "--reason-code", "duplicate", "--reason", "duplicate row",
        "--actor", "tester", "--apply", "--yes",
    ]
    assert main(first) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is True
    assert main(first) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["applied"] is False
    assert second["will_change"] is False


def test_retired_cli_table_json_and_direct_only(project, capsys):
    db = Database(project.db_path)
    try:
        _graph(db)
    finally:
        db.close()
    assert main(["--project", str(project.root), "retired"]) == 0
    assert capsys.readouterr().out.strip() == "no retired entities"

    assert main([
        "--project", str(project.root), "retire", "SMP_000001",
        "--reason-code", "accidental_import", "--reason", "mistake",
        "--actor", "tester", "--apply", "--yes",
    ]) == 0
    capsys.readouterr()

    assert main(["--project", str(project.root), "retired"]) == 0
    out = capsys.readouterr().out
    assert "SMP_000001" in out and "ASM_000001" in out  # inherited retirement
    assert main(["--project", str(project.root), "retired", "--direct-only"]) == 0
    out = capsys.readouterr().out
    assert "SMP_000001" in out and "ASM_000001" not in out
    assert main(["--project", str(project.root), "retired", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {row["entity_id"] for row in rows} >= {"SMP_000001", "ASM_000001"}


def test_backup_create_and_verify_cli(project, capsys):
    destination = project.root.parent / f"{project.root.name}-backup-out"
    assert main([
        "--project", str(project.root), "backup", "create", "--output", str(destination),
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["file_count"] >= 1
    assert (destination / "backup-manifest.json").is_file()

    assert main([
        "backup", "verify", "--input", str(destination),
    ]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True

    (destination / "project.yaml").write_text("tampered\n", encoding="utf-8")
    assert main(["backup", "verify", "--input", str(destination)]) == 1
    tampered = json.loads(capsys.readouterr().out)
    assert tampered["ok"] is False
    assert tampered["failures"]


def test_tui_command_launches_app_without_writable_database(project, monkeypatch):
    import operon.tui.app as tui_app

    launched: list = []

    class FakeApp:
        def __init__(self, loaded):
            launched.append(loaded.root)

        def run(self):
            launched.append("run")

    monkeypatch.setattr(tui_app, "OperonApp", FakeApp)
    assert cli._cmd_tui(SimpleNamespace(project=str(project.root))) == 0
    assert launched == [project.root, "run"]


# Database.__init__ does not close its sqlite connection when schema setup
# fails, so finalization of the abandoned connection emits a ResourceWarning.
@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_main_reports_corrupt_database_as_database_error(tmp_path, capsys):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    project.db_path.write_bytes(b"this is not a sqlite database")
    for sidecar in ("-wal", "-shm"):
        Path(f"{project.db_path}{sidecar}").unlink(missing_ok=True)
    assert main(["--project", str(tmp_path), "status"]) == 1
    assert "error: database error:" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# cli.py - import-qc JSON/TSV payload handling
# --------------------------------------------------------------------------- #


def _seed_fasta_file(project: Project, db: Database, name: str = "assembly.fa") -> dict:
    source = project.root / name
    source.write_text(">ctg1\nACGT\n", encoding="utf-8")
    return ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")


def test_import_qc_json_without_sequences_leaves_sequences_untouched(project, tmp_path, capsys):
    db = Database(project.db_path)
    try:
        _graph(db)
        record = _seed_fasta_file(project, db)
    finally:
        db.close()
    payload = {
        "schema_version": 1, "tool": "operon.builtin",
        "tool_version": __import__("operon").__version__,
        "parameter_set": "builtin_v2",
        "file": {
            "file_id": record["file_id"], "sha256": record["sha256"],
            "size_bytes": record["size_bytes"], "format": "fasta",
            "file_role": "genome_fasta",
        },
        "metrics": [{
            "qc_stage": "file_integrity", "metric_name": "parseable",
            "metric_value": "1", "metric_numeric": 1.0, "metric_unit": None,
            "parameter_set": "builtin_v2",
        }],
    }
    document = tmp_path / "payload.json"
    document.write_text(json.dumps(payload), encoding="utf-8")

    assert main([
        "--project", str(project.root), "import-qc", "--file", str(document),
    ]) == 0
    assert f"imported 1 built-in QC metric(s) for {record['file_id']}" in capsys.readouterr().out
    db = Database(project.db_path)
    try:
        assert db.query("SELECT COUNT(*) AS n FROM sequences")[0]["n"] == 0
    finally:
        db.close()


def test_import_qc_tsv_deduplicates_entities(project, tmp_path, capsys):
    db = Database(project.db_path)
    try:
        _graph(db)
    finally:
        db.close()
    table = tmp_path / "qc.tsv"
    table.write_text(
        "entity_type\tentity_id\tqc_stage\tmetric_name\tmetric_value\ttool\t"
        "tool_version\tparameter_set\n"
        "assembly\tASM_000001\texternal\tmetric_a\t1\ttool\t1\tp\n"
        "assembly\tASM_000001\texternal\tmetric_b\t2\ttool\t1\tp\n",
        encoding="utf-8",
    )
    assert main(["--project", str(project.root), "import-qc", "--file", str(table)]) == 0
    assert "imported 2 external QC metric(s)" in capsys.readouterr().out
    db = Database(project.db_path)
    try:
        assert db.query("SELECT COUNT(*) AS n FROM qc_results")[0]["n"] == 2
        details = json.loads(db.query(
            "SELECT execution_details FROM workflow_runs WHERE step='import-qc'"
        )[0]["execution_details"])
        assert details["entities"] == ["assembly:ASM_000001"]
    finally:
        db.close()


def test_analysis_report_filters_json_tsv_and_output_file(project, tmp_path, capsys):
    db = Database(project.db_path)
    try:
        _graph(db)
        db.conn.execute("PRAGMA foreign_keys=OFF")
        db.conn.execute(
            "INSERT INTO analysis_jobs(job_id, analysis_name, entity_type, entity_id, file_id, "
            "tool, tool_version, parameter_set, parameter_sha256, input_sha256, "
            "database_identity, status, started_at) "
            "VALUES(1,'blast','assembly','ASM_000001','FIL_1','blastn','2','p','s','i','d',"
            "'completed','2026-01-01')"
        )
        db.conn.execute(
            "INSERT INTO analysis_results(job_id, analysis_name, entity_type, entity_id, file_id, "
            "metric_name, metric_value) VALUES(1,'blast','assembly','ASM_000001','FIL_1','score','1')"
        )
        db.conn.execute(
            "INSERT INTO analysis_alignments(job_id, analysis_name, entity_type, entity_id, "
            "query_id, subject_id, file_id, hit_rank, evalue, bitscore) "
            "VALUES(1,'blast','assembly','ASM_000001','q1','s1','FIL_1',1,1e-5,10.0),"
            "(1,'blast','assembly','ASM_000001','q2','s2','FIL_1',1,5.0,1.0)"
        )
        db.conn.commit()
    finally:
        db.close()
    root = str(project.root)

    assert main([
        "--project", root, "report", "analysis", "--hits", "--format", "json",
        "--query-id", "q1", "--subject-id", "s1", "--evalue-max", "0.001",
        "--include-retired",
    ]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["query_id"] for row in rows] == ["q1"]
    assert rows[0]["bitscore"] == 10.0

    assert main([
        "--project", root, "report", "analysis", "--hits", "--format", "tsv",
        "--include-retired", "--limit", "1",
    ]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].split("\t")[0] == "analysis_name"
    assert len(lines) == 2

    target = tmp_path / "hits.json"
    assert main([
        "--project", root, "report", "analysis", "--hits", "--format", "json",
        "--out", str(target), "--analysis", "missing",
    ]) == 0
    assert capsys.readouterr().out == ""
    assert json.loads(target.read_text(encoding="utf-8")) == []

    assert main(["--project", root, "report", "analysis", "--limit", "5"]) == 0
    assert "score" in capsys.readouterr().out
    assert main([
        "--project", root, "report", "analysis", "--analysis", "missing", "--limit", "5",
    ]) == 0
    assert "(no analysis results)" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# database.py - read-only fallback, legacy migrations, lifecycle guards
# --------------------------------------------------------------------------- #


def test_open_read_only_propagates_unexpected_driver_errors(project, monkeypatch):
    closed: list[bool] = []

    class BrokenConnection:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "operon.database.sqlite3.connect", lambda *_a, **_k: BrokenConnection()
    )
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        Database(project.db_path, read_only=True)
    assert closed == [True]


def test_open_read_only_rejects_non_empty_wal_without_writable_side_files(project):
    db = Database(project.db_path)
    _organism(db)
    db.close()
    for suffix in ("-wal", "-shm"):
        Path(f"{project.db_path}{suffix}").unlink(missing_ok=True)
    Path(f"{project.db_path}-wal").write_bytes(b"\x00" * 64)
    original_mode = project.root.stat().st_mode
    os.chmod(project.root, 0o555)
    try:
        with pytest.raises(sqlite3.OperationalError, match="non-empty WAL"):
            Database(project.db_path, read_only=True)
    finally:
        os.chmod(project.root, original_mode)


def test_pre_1_0_database_is_reshaped_without_losing_rows(tmp_path):
    legacy_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(legacy_path))
    try:
        conn.executescript(
            """
            CREATE TABLE qc_results (
                qc_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, qc_stage TEXT NOT NULL,
                metric_name TEXT NOT NULL, metric_value TEXT NOT NULL, metric_numeric REAL,
                metric_unit TEXT, tool TEXT NOT NULL, tool_version TEXT NOT NULL,
                parameter_set TEXT NOT NULL, evaluated_at TEXT NOT NULL
            );
            INSERT INTO qc_results (entity_type, entity_id, qc_stage, metric_name,
                                    metric_value, metric_numeric, metric_unit, tool,
                                    tool_version, parameter_set, evaluated_at)
            VALUES ('assembly', 'ASM_1', 'basic', 'score', '1', 1.0, NULL, 't', '1', 'p',
                    '2026-01-01T00:00:00+00:00');
            CREATE TABLE decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, profile TEXT NOT NULL,
                profile_version INTEGER, decision TEXT NOT NULL, curated_decision TEXT,
                reason_codes TEXT NOT NULL, observed TEXT NOT NULL, thresholds TEXT NOT NULL,
                evaluated_at TEXT NOT NULL, curated_by TEXT, curated_reason TEXT,
                curated_evidence TEXT, curated_at TEXT
            );
            INSERT INTO decisions (entity_type, entity_id, profile, decision, reason_codes,
                                   observed, thresholds, evaluated_at)
            VALUES ('assembly', 'ASM_1', 'p', 'PASS', '[]', '{}', '{}',
                    '2026-01-01T00:00:00+00:00');
            """
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(legacy_path)
    try:
        # Legacy rows survive the reshape and get a synthetic input identity.
        assert db.query("SELECT input_identity FROM qc_results")[0]["input_identity"] == (
            "legacy:assembly:ASM_1")
        assert db.query("SELECT decision FROM decisions")[0]["decision"] == "PASS"
        assert "profile_snapshot_id" in db.table_columns("decisions")
        assert "input_identity" in db.table_columns("qc_results")
    finally:
        db.close()


def test_reopening_database_restores_dropped_columns(project):
    db = Database(project.db_path)
    try:
        with db.transaction():
            db.conn.execute('ALTER TABLE assemblies DROP COLUMN "assembly_type"')
            db.conn.execute('ALTER TABLE workflow_runs DROP COLUMN executor')
            db.conn.execute('ALTER TABLE workflow_runs DROP COLUMN scheduler_job_id')
            db.conn.execute('ALTER TABLE workflow_runs DROP COLUMN execution_details')
            db.conn.execute('ALTER TABLE workflow_runs DROP COLUMN resumes_run_id')
            db.conn.execute('ALTER TABLE changes DROP COLUMN workflow_run_id')
            db.conn.execute('ALTER TABLE changes DROP COLUMN reverts_change_id')
    finally:
        db.close()

    db = Database(project.db_path)
    try:
        assert "assembly_type" in db.table_columns("assemblies")
        workflow_columns = set(db.table_columns("workflow_runs"))
        assert {"executor", "scheduler_job_id", "execution_details", "resumes_run_id"} <= workflow_columns
        change_columns = set(db.table_columns("changes"))
        assert {"workflow_run_id", "reverts_change_id"} <= change_columns
    finally:
        db.close()


def test_lifecycle_helpers_tolerate_unknown_types_and_absent_schema(project_db):
    _project, db = project_db
    assert db.effective_retirements("unknown", "X") == []
    assert db.is_entity_retired("unknown", "X") is False
    assert db.metadata_change_id("unknown", "X") == 0
    assert db.current_lifecycle_event("organism", "ORG_000001") is None

    # Simulate a pre-2.7 database whose lifecycle view is absent.
    with db.transaction():
        db.conn.execute("DROP VIEW IF EXISTS effective_retired_entities")
    assert db.lifecycle_schema_available() is False
    assert db.current_lifecycle_event("organism", "ORG_000001") is None
    assert db.effective_retirements("organism", "ORG_000001") == []
    assert db.is_entity_retired("organism", "ORG_000001") is False
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "E"})
    assert db.export_active_rows("organisms") == db.export_rows("organisms")


def test_max_id_number_handles_missing_tables_and_foreign_ids(project_db):
    _project, db = project_db
    assert Database._max_id_number(db.conn, "organism", "ORG", "missing_table", "organism_id") == 0
    # Rows whose IDs do not carry the requested prefix contribute nothing.
    _organism(db)
    db.insert_row("samples", {"sample_id": "SMP_000042", "organism_id": "ORG_000001"})
    assert Database._max_id_number(db.conn, "organism", "ORG", "samples", "sample_id") == 0
    assert Database._max_id_number(db.conn, "organism", "ORG", "samples", "sample_id") == 0

    # A manifest row whose ID does not look like FIL_<n> is ignored.
    db.insert_row("files", {
        "file_id": "LEGACY_FILE", "entity_type": "assembly", "entity_id": "ASM_000001",
        "file_role": "other", "format": "other", "compression": "none",
        "relative_path": "raw/legacy.bin", "size_bytes": 1, "sha256": "b" * 64,
        "status": "CHECKSUM_VERIFIED",
    })
    assert Database._max_id_number(db.conn, "file", "FIL", None, None) == 0

    bare = sqlite3.connect(":memory:")
    try:
        # Neither the table nor the files table exists: both probes degrade to 0.
        assert Database._max_id_number(bare, "file", "FIL", None, None) == 0
    finally:
        bare.close()


def test_next_id_rolls_back_when_reservation_fails(project_db, monkeypatch):
    _project, db = project_db

    def boom(*_a, **_k):
        raise RuntimeError("counter failure")

    monkeypatch.setattr(Database, "_reserve_id", boom)
    with pytest.raises(RuntimeError, match="counter failure"):
        db.next_id("organism")
    assert db.conn.in_transaction is False
    assert db.query("SELECT COUNT(*) AS n FROM id_counters WHERE entity_type='organism'")[0]["n"] == 0


def test_register_data_source_validates_required_fields(project_db):
    _project, db = project_db
    with pytest.raises(ValidationError, match="source_type must be"):
        db.register_data_source({"source_type": "other"})
    with pytest.raises(ValidationError, match="provider is required"):
        db.register_data_source({"source_type": "insdc", "database_name": "D"})
    with pytest.raises(ValidationError, match="database or repository is required"):
        db.register_data_source({"source_type": "insdc", "provider": "P"})
    with pytest.raises(ValidationError, match="reference citation or DOI"):
        db.register_data_source({
            "source_type": "non_insdc", "provider": "P", "database_name": "D",
        })
    with pytest.raises(ValidationError, match="License name or SPDX"):
        db.register_data_source({
            "source_type": "non_insdc", "provider": "P", "database_name": "D",
            "citation": "doi:10.1/x",
        })


def test_export_active_rows_handles_unknown_columns_and_source_links(project_db):
    _project, db = project_db
    _organism(db)
    assert db.export_active_rows("organisms", ["does_not_exist"]) == []
    source = db.register_data_source({
        "source_type": "insdc", "provider": "NCBI", "database_name": "Assembly",
    })
    db.link_data_source(source["source_id"], [("organism", "ORG_000001")])
    exported = db.export_active_rows("data_sources")
    assert [row["source_id"] for row in exported] == [source["source_id"]]
    # Tables outside the special-cased set take the plain projection query.
    assert db.export_active_rows("qc_results") == []


def test_effective_decision_prefers_curated_value(project_db):
    _project, db = project_db
    _organism(db)
    assert db.effective_decision("organism", "ORG_000001", "p") is None
    db.upsert_decision({
        "entity_type": "organism", "entity_id": "ORG_000001", "profile": "p",
        "decision": "FAIL", "curated_decision": "PASS", "reason_codes": "[]",
        "observed": "{}", "thresholds": "{}", "evaluated_at": "2026-01-01T00:00:00+00:00",
    })
    assert db.effective_decision("organism", "ORG_000001", "p") == "PASS"


# --------------------------------------------------------------------------- #
# cli.py - remote push/pull/evict dispatch and remaining confirmation paths
# --------------------------------------------------------------------------- #


def test_pull_and_evict_cli_print_per_status_summaries(project, monkeypatch, capsys):
    import operon.remotes as remotes

    seen: list = []
    monkeypatch.setattr(remotes, "pull", lambda _db, _p, remote, file_ids=None: (
        seen.append(("pull", remote, file_ids)) or [
            {"file_id": "FIL_1", "relative_path": "a.fa", "status": "restored"},
        ]
    ))
    monkeypatch.setattr(remotes, "evict_local", lambda _db, _p, remote, file_ids=None: (
        seen.append(("evict", remote, file_ids)) or [
            {"file_id": "FIL_2", "relative_path": "b.fa", "status": "error", "error": "busy"},
        ]
    ))

    assert main([
        "--project", str(project.root), "pull", "--remote", "mirror", "--file-id", "FIL_1",
    ]) == 0
    out = capsys.readouterr().out
    assert "pull mirror: restored: 1" in out
    assert seen[-1] == ("pull", "mirror", ["FIL_1"])

    assert main(["--project", str(project.root), "evict", "--remote", "mirror"]) == 1
    out = capsys.readouterr().out
    assert "evict mirror: error: 1" in out
    assert seen[-1] == ("evict", "mirror", None)


def test_confirm_curated_evaluation_truncates_long_previews(monkeypatch, capsys):
    prompts: list[str] = []
    monkeypatch.setitem(sys.modules, "questionary", SimpleNamespace(
        confirm=lambda message, default=False: (
            prompts.append(message) or _Prompt(True)
        ),
    ))
    captured_stdout = sys.stdout
    monkeypatch.setattr(cli.sys, "stdin", _TTY(True))
    monkeypatch.setattr(cli.sys, "stdout", _TTY(True, sink=captured_stdout))
    targets = [(f"assembly", f"ASM_{index:06d}") for index in range(1, 8)]
    assert cli._confirm_curated_evaluation(targets, False) is True
    assert len(prompts) == 1
    assert prompts[0].startswith("Re-evaluate 7 curated entity/entities")
    assert "ASM_000005, ..." in prompts[0]
    assert "ASM_000006" not in prompts[0]


def test_run_pipeline_honours_interactive_cancellation(project, monkeypatch):
    db = Database(project.db_path)
    profile = project.config["qc"]["default_profile"]
    try:
        _graph(db)
        db.upsert_decision({
            "entity_type": "assembly", "entity_id": "ASM_000001", "profile": profile,
            "decision": "PASS", "curated_decision": "PASS", "curated_by": "reviewer",
            "reason_codes": "[]", "observed": "{}", "thresholds": "{}",
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        })
    finally:
        db.close()

    _fake_questionary(monkeypatch, confirm=False)
    monkeypatch.setattr(cli.sys, "stdin", _TTY(True))
    monkeypatch.setattr(cli.sys, "stdout", _TTY(True))
    monkeypatch.setattr(cli, "ingest_file", lambda *_a, **_k: pytest.fail("ingest ran after declining"))
    assert main([
        "--project", str(project.root), "run-pipeline", "--source", "x.fa",
        "--entity-type", "assembly", "--entity-id", "ASM_000001", "--role", "genome_fasta",
    ]) == 0


def test_classify_sequences_without_labels_prints_no_table(project_db, monkeypatch, capsys):
    project, db = project_db
    monkeypatch.setattr("operon.classify.classify_sequences", lambda *_a, **_k: {
        "sequences": 2, "unlabeled": 2, "files": 1, "profile": "demo",
        "label_counts": {}, "labels_written": 0, "labels_removed": 0, "run_id": "WF_EMPTY",
    })
    assert cli._cmd_classify_sequences(SimpleNamespace(profile="demo"), project, db) == 0
    out = capsys.readouterr().out
    assert "labels written: 0, removed: 0 (run WF_EMPTY)" in out
    assert "sequences\n" not in out  # no label table when nothing was labelled


# --------------------------------------------------------------------------- #
# files.py - verification cache, remote fallbacks, standardize cleanup
# --------------------------------------------------------------------------- #


def test_verify_local_file_identity_survives_unreadable_path(project_db, tmp_path, monkeypatch):
    _project, db = project_db

    class _Unreadable(Path):
        def exists(self, **_kwargs):
            raise OSError("permission denied")

    record = {"file_id": "FIL_1", "sha256": "a" * 64, "size_bytes": 4}
    ok, info = verify_local_file_identity(db, record, _Unreadable(tmp_path / "blocked"))
    assert ok is False
    assert info["exists"] is False
    assert info["verification_method"] == "missing"


def test_verify_local_file_identity_rehashes_after_stat_change(project_db):
    project, db = project_db
    _graph(db)
    path = project.root / "raw" / "payload.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("payload", encoding="utf-8")
    record = {
        "file_id": "FIL_000001", "entity_type": "assembly", "entity_id": "ASM_000001",
        "file_role": "other", "format": "other", "compression": "none",
        "relative_path": "raw/payload.bin", "status": "CHECKSUM_VERIFIED",
        "sha256": hashlib.sha256(b"payload").hexdigest(), "size_bytes": len(b"payload"),
    }
    db.insert_row("files", record)
    ok, info = verify_local_file_identity(db, record, path)
    assert ok is True and info["verification_method"] == "full_sha256"

    ok, info = verify_local_file_identity(db, record, path)
    assert ok is True and info["verification_method"] == "cached_stat_fingerprint"

    stat = path.stat()
    os.utime(path, (stat.st_atime + 60, stat.st_mtime + 60))
    ok, info = verify_local_file_identity(db, record, path)
    assert ok is True and info["verification_method"] == "full_sha256"


def test_ingest_accepts_binary_formats_with_gzip_compression(project_db, tmp_path):
    project, db = project_db
    _graph(db)
    source = tmp_path / "reads.bam.gz"
    source.write_bytes(b"BAM\x01binary")
    row = ingest_file(
        db, project, source, "assembly", "ASM_000001", "other",
        fmt="bam", compression="gzip",
    )
    assert row["format"] == "bam"
    assert row["compression"] == "gzip"
    assert (project.root / row["relative_path"]).read_bytes() == b"BAM\x01binary"


def test_occupied_target_that_is_its_own_canonical_path_is_a_conflict(project_db):
    project, db = project_db
    _graph(db)
    source = project.root / "assembly.fa"
    source.write_text(">ctg\nACGT\n", encoding="utf-8")
    row = ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    target = project.root / row["relative_path"]
    with pytest.raises(files_mod.ConflictError, match="canonical path is the same"):
        files_mod._resolve_occupied_target(db, project, target, row["sha256"])


def _remote_only_file(db: Database, file_id: str, relative_path: str, location: str) -> None:
    db.insert_row("files", {
        "file_id": file_id, "entity_type": "assembly", "entity_id": "ASM_000001",
        "file_role": "other", "format": "other", "compression": "none",
        "relative_path": relative_path, "size_bytes": 4, "sha256": "a" * 64,
        "status": "REMOTE_ONLY",
    })
    with db.transaction():
        db.conn.execute(
            "INSERT INTO file_locations(file_id, location_name, location_type, uri, "
            "relative_path, sha256, size_bytes, status, verified_at) "
            "VALUES(?,?,'sftp','sftp://h/r',?,'a',4,'AVAILABLE','2026-01-01')",
            (file_id, location, relative_path),
        )


def test_verify_files_reports_remote_unverified_when_store_cannot_open(project_db, monkeypatch, capsys):
    project, db = project_db
    _graph(db)
    _remote_only_file(db, "FIL_000001", "raw/gone1.fa", "mirror")
    _remote_only_file(db, "FIL_000002", "raw/gone2.fa", "mirror")

    attempts: list[str] = []

    class _UnreachableStore:
        def __init__(self, remote):
            attempts.append(remote["name"])
            raise OSError("connection refused")

    monkeypatch.setattr("operon.remotes.get_remote", lambda _p, name: {"name": name})
    monkeypatch.setattr("operon.remotes.SFTPStore", _UnreachableStore)

    assert main(["--project", str(project.root), "verify"]) == 1
    out = capsys.readouterr().out
    assert out.count("REMOTE_UNVERIFIED") == 2
    # The failed connection is recorded once and reused for the second file.
    assert attempts == ["mirror"]
    assert "connection refused" in out


def test_verify_files_reuses_open_store_and_manifest(project_db, monkeypatch):
    project, db = project_db
    _graph(db)
    _remote_only_file(db, "FIL_000001", "raw/gone1.fa", "mirror")
    _remote_only_file(db, "FIL_000002", "raw/gone2.fa", "mirror")

    events: list[str] = []

    class _Store:
        def __init__(self, remote):
            events.append(f"open:{remote['name']}")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read_manifest(self):
            events.append("manifest")
            return {"files": []}

    monkeypatch.setattr("operon.remotes.get_remote", lambda _p, name: {"name": name})
    monkeypatch.setattr("operon.remotes.SFTPStore", _Store)
    monkeypatch.setattr("operon.remotes.verify_remote_record", lambda *_a, **_k: None)
    monkeypatch.setattr("operon.remotes._ensure_remote_only_schema", lambda _p: None)

    results = files_mod.verify_files(db, project)
    assert [row["status"] for row in results] == ["REMOTE_ONLY", "REMOTE_ONLY"]
    assert [row["remote"] for row in results] == ["mirror", "mirror"]
    assert events == ["open:mirror", "manifest"]


def test_standardize_hardlink_publishes_and_falls_back(project_db, monkeypatch):
    project, db = project_db
    _graph(db)
    first = project.root / "first.fa"
    first.write_text(">a\nAC\n", encoding="utf-8")
    row = ingest_file(db, project, first, "assembly", "ASM_000001", "genome_fasta")
    result = standardize_file(db, project, row["file_id"], link_kind="hardlink")
    assert result["action"] == "hardlink"
    target = Path(result["target"])
    assert target.read_text(encoding="utf-8") == ">a\nAC\n"

    second = project.root / "second.fa"
    second.write_text(">b\nGT\n", encoding="utf-8")
    row2 = ingest_file(db, project, second, "assembly", "ASM_000001", "protein_fasta")

    def interrupted_link(source, destination):
        # Simulate a link that leaves a partial temporary file behind.
        Path(destination).write_text("partial", encoding="utf-8")
        raise OSError("cross-device link")

    monkeypatch.setattr(files_mod.os, "link", interrupted_link)
    result2 = standardize_file(db, project, row2["file_id"], link_kind="hardlink")
    target2 = Path(result2["target"])
    assert result2["action"] == "hardlink"
    assert target2.read_text(encoding="utf-8") == ">b\nGT\n"
    assert not list(target2.parent.glob(".*.operon-*"))


def test_standardize_removes_temporary_link_when_publish_fails(project_db, monkeypatch):
    project, db = project_db
    _graph(db)
    source = project.root / "third.fa"
    source.write_text(">c\nTT\n", encoding="utf-8")
    row = ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    target = (
        project.standardized_root / files_mod.raw_bucket("assembly") / "ASM_000001"
        / Path(row["relative_path"]).name
    )
    monkeypatch.setattr(
        files_mod.os, "replace",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("device gone")),
    )
    # Symlink publishing has no copy fallback, so the interrupted temporary
    # link must be removed by the outer cleanup handler.
    with pytest.raises(OSError, match="device gone"):
        standardize_file(db, project, row["file_id"], link_kind="symlink")
    assert not target.exists()
    assert not list(target.parent.glob(".*.operon-*"))


def test_standardize_directory_copy_and_rollback(project_db, monkeypatch):
    project, db = project_db
    _graph(db)
    tree = project.root / "tree"
    tree.mkdir()
    (tree / "part.fa").write_text(">x\nAC\n", encoding="utf-8")
    row = ingest_file(db, project, tree, "assembly", "ASM_000001", "other", fmt="directory")
    result = standardize_file(db, project, row["file_id"])
    target = Path(result["target"])
    assert target.is_dir()
    assert (target / "part.fa").read_text(encoding="utf-8") == ">x\nAC\n"

    # A checksum mismatch after publishing removes the copied directory again.
    tree2 = project.root / "tree2"
    tree2.mkdir()
    (tree2 / "part.fa").write_text(">y\nGT\n", encoding="utf-8")
    row2 = ingest_file(db, project, tree2, "assembly", "ASM_000001", "protein_fasta", fmt="directory")
    target2 = (
        project.standardized_root / files_mod.raw_bucket("assembly") / "ASM_000001"
        / Path(row2["relative_path"]).name
    )

    def bad_copytree(source, destination):
        Path(destination).mkdir(parents=True)
        (Path(destination) / "part.fa").write_text("tampered", encoding="utf-8")

    monkeypatch.setattr(files_mod, "atomic_copytree", bad_copytree)
    with pytest.raises(files_mod.ChecksumError, match="standardized target checksum mismatch"):
        standardize_file(db, project, row2["file_id"])
    assert not target2.exists()


def test_standardize_file_removes_target_when_checksum_mismatch(project_db, monkeypatch):
    project, db = project_db
    _graph(db)
    source = project.root / "fourth.fa"
    source.write_text(">d\nAA\n", encoding="utf-8")
    row = ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    target = (
        project.standardized_root / files_mod.raw_bucket("assembly") / "ASM_000001"
        / Path(row["relative_path"]).name
    )
    monkeypatch.setattr(
        files_mod, "atomic_copy",
        lambda _source, destination: Path(destination).write_text("tampered", encoding="utf-8"),
    )
    with pytest.raises(files_mod.ChecksumError, match="standardized target checksum mismatch"):
        standardize_file(db, project, row["file_id"])
    assert not target.exists()


# --------------------------------------------------------------------------- #
# workflow.py - log following, staged inputs, executor failures
# --------------------------------------------------------------------------- #


def test_follow_run_logs_normalizes_stderr_and_reports_failure(tmp_path):
    import io

    from operon.workflow import follow_run_logs

    run_id = "WF_FOLLOW"
    (tmp_path / f"{run_id}.stdout.log").write_text("hello\n", encoding="utf-8")
    (tmp_path / f"{run_id}.stderr.log").write_text("boom", encoding="utf-8")
    statuses = [{"status": "running"}, {"status": "failed", "exit_code": 7}]
    out = io.StringIO()
    code = follow_run_logs(
        lambda: statuses.pop(0), tmp_path, run_id, out=out, poll_interval=0,
    )
    text = out.getvalue()
    assert code == 1
    assert "hello" in text
    assert "stderr: boom\n" in text
    assert f"run {run_id} finished: status=failed exit_code=7" in text


def _stage_executor(observed: dict, *, probe_error: bool = False, run_error: str | None = None):
    from operon.execution import ExecResult

    class _Executor:
        name = "local"

        def describe(self):
            return "stage-executor"

        def probe_environment(self):
            if probe_error:
                raise RuntimeError("probe unavailable")
            return {"system": {"os": "Linux"}}

        def run(self, argv, **kwargs):
            observed.setdefault("calls", []).append((list(argv), kwargs.get("stage_inputs")))
            return ExecResult(exit_code=0, error=run_error, details={})

    return _Executor()


def test_run_external_command_resolves_stage_inputs_and_survives_probe_failure(project):
    db = Database(project.db_path)
    try:
        _graph(db)
        (project.root / "relative.fa").write_text(">x\nAC\n", encoding="utf-8")
        observed: dict = {}
        executor = _stage_executor(observed, probe_error=True)
        record = run_external_command(
            db, project, [sys.executable, "-c", "pass"], step="stage-inputs",
            entity_type="assembly", entity_id="ASM_000001",
            stage_inputs=["relative.fa"], executor=executor,
        )
        assert record["status"] == "completed"
        assert observed["calls"][0][1] == [project.root / "relative.fa"]
        row = db.query(
            "SELECT environment_id FROM workflow_runs WHERE run_id=?", (record["run_id"],)
        )[0]
        assert row["environment_id"] is None
    finally:
        db.close()


def test_run_external_command_reports_failing_step_of_chain(project):
    from operon.execution import ExecResult

    db = Database(project.db_path)
    try:
        _graph(db)
        calls: list = []

        class _ChainExecutor:
            name = "local"

            def describe(self):
                return "chain"

            def run(self, argv, **_kwargs):
                calls.append(list(argv))
                if len(calls) == 2:
                    return ExecResult(exit_code=0, error="tolerant failure", details={})
                return ExecResult(exit_code=0, error=None, details={})

        with pytest.raises(RuntimeError, match=r"chain failed: step 2/2 failed: tolerant failure"):
            run_external_command(
                db, project, ["unused"], step="chain", commands=[["a"], ["b"]],
                executor=_ChainExecutor(),
            )
        assert calls == [["a"], ["b"]]
        row = db.query(
            "SELECT status, error, execution_details FROM workflow_runs WHERE step='chain'"
        )[0]
        assert row["status"] == "failed"
        assert row["error"] == "step 2/2 failed: tolerant failure"
        details = json.loads(row["execution_details"])
        assert [step["index"] for step in details["steps"]] == [1, 2]
        assert details["steps"][1]["exit_code"] == 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# rules.py - target discovery and unknown-policy fallthrough
# --------------------------------------------------------------------------- #


def _rules_profile(project, name: str, **extra) -> str:
    document = {"kind": "qc", "version": 1, "applies_to": ["assembly"],
                "required": [], "warnings": []}
    document.update(extra)
    (project.profiles_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return name


def _assembly_metrics(db: Database, *, score: int = 50, lineage: str = "unknown") -> None:
    for name, value, numeric in (("score", str(score), float(score)),
                                 ("lineage", lineage, None)):
        db.insert_qc_result({
            "entity_type": "assembly", "entity_id": "ASM_000001", "qc_stage": "analysis:x",
            "metric_name": name, "metric_value": value, "metric_numeric": numeric,
            "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
        })


def test_evaluation_targets_and_curated_targets(project_db):
    project, db = project_db
    _graph(db)
    _assembly_metrics(db)
    _rules_profile(project, "targets")

    assert rules.evaluation_targets(db, project, "targets") == [("assembly", "ASM_000001")]
    assert rules.evaluation_targets(
        db, project, "targets", entity_type="assembly", entity_id="ASM_000001",
    ) == [("assembly", "ASM_000001")]
    assert rules.evaluation_targets(
        db, project, "targets", entity_type="assembly", entity_id="ASM_MISSING",
    ) == []
    # A filter that matches nothing short-circuits curated discovery.
    assert rules.curated_evaluation_targets(
        db, project, "targets", entity_type="assembly", entity_id="ASM_MISSING",
    ) == []

    db.upsert_decision({
        "entity_type": "assembly", "entity_id": "ASM_000001", "profile": "targets",
        "decision": "PASS", "curated_decision": "PASS", "curated_by": "reviewer",
        "reason_codes": "[]", "observed": "{}", "thresholds": "{}",
        "evaluated_at": "2026-01-01T00:00:00+00:00",
    })
    assert rules.curated_evaluation_targets(db, project, "targets") == [("assembly", "ASM_000001")]


def test_warning_rule_with_failing_unknown_policy_is_skipped(project_db):
    project, db = project_db
    _graph(db)
    _assembly_metrics(db)
    _rules_profile(project, "warn_fail", warnings=[{
        "metric": "score", "operator": ">=", "source": {"qc_stage": "analysis:x"},
        "value_by": {"metric": "lineage", "values": {"known": 80}, "unknown": "fail"},
    }])
    result = rules.evaluate_entity(db, project, "assembly", "ASM_000001", "warn_fail")
    assert result["decision"] == "PASS"
    assert json.loads(result["reason_codes"]) == []


# --------------------------------------------------------------------------- #
# qc/__init__.py and qc/measure.py - aggregate status and metric coercion
# --------------------------------------------------------------------------- #


def _qc_metric(db: Database, name: str, value: str, numeric, file_id: str = "FIL_000001") -> None:
    db.insert_qc_result({
        "entity_type": "assembly", "entity_id": "ASM_000001", "file_id": file_id,
        "file_sha256": "a" * 64, "input_identity": f"file:{file_id}:{'a' * 64}",
        "qc_stage": "file_integrity", "metric_name": name, "metric_value": value,
        "metric_numeric": numeric, "metric_unit": None, "tool": "t", "tool_version": "1",
        "parameter_set": "p", "evaluated_at": "now",
    })


def test_file_qc_status_coerces_text_booleans(project_db):
    project, db = project_db
    _graph(db)
    db.insert_row("files", {
        "file_id": "FIL_000001", "entity_type": "assembly", "entity_id": "ASM_000001",
        "file_role": "genome_fasta", "format": "fasta", "compression": "none",
        "relative_path": "raw/assembly.fa", "size_bytes": 4, "sha256": "a" * 64,
        "status": "CHECKSUM_VERIFIED",
    })
    assert qc.file_qc_status(db, "FIL_000001") == "QC_PENDING"

    _qc_metric(db, "parseable", "no", None)
    assert qc.file_qc_status(db, "FIL_000001") == "QC_FAILED"
    _qc_metric(db, "parseable", "0", None)
    assert qc.file_qc_status(db, "FIL_000001") == "QC_FAILED"
    _qc_metric(db, "parseable", "true", None)
    assert qc.file_qc_status(db, "FIL_000001") == "QC_COMPLETE"
    _qc_metric(db, "parseable", "1", 0.0)
    assert qc.file_qc_status(db, "FIL_000001") == "QC_FAILED"


def test_fasta_length_cache_tolerates_blank_lines_and_directory_paths(project_db, tmp_path, monkeypatch):
    _project, db = project_db
    record = {"file_id": "FIL_1", "sha256": "a" * 64, "size_bytes": 4}
    cache = tmp_path / "cache.tsv"
    qc._write_fasta_length_cache(cache, record, {"ctg1": 4, "ctg2": 8})
    lines = cache.read_text(encoding="utf-8").splitlines()
    cache.write_text("\n".join([lines[0], "", *lines[1:]]) + "\n", encoding="utf-8")
    assert qc._load_fasta_length_cache(cache, record) == {"ctg1": 4, "ctg2": 8}

    # A directory where a cache file is expected is discarded, not deleted.
    directory = tmp_path / "cache-dir"
    directory.mkdir()
    assert qc._load_fasta_length_cache(directory, record) is None
    assert directory.is_dir()

    # Corrupt content whose deletion fails is still discarded, and the failure
    # to remove it never escapes.
    class _UndeletableCache(Path):
        def unlink(self, missing_ok=False):
            raise OSError("read-only filesystem")

    broken = tmp_path / "broken.tsv"
    broken.write_text("not-json\n", encoding="utf-8")
    assert qc._load_fasta_length_cache(_UndeletableCache(broken), record) is None
    assert broken.is_file()


def test_fasta_length_cache_write_failure_preserves_original_error(tmp_path, monkeypatch):
    record = {"file_id": "FIL_1", "sha256": "a" * 64, "size_bytes": 4}
    monkeypatch.setattr(
        qc.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(
        qc.os, "unlink", lambda *_a, **_k: (_ for _ in ()).throw(OSError("missing")))
    with pytest.raises(OSError, match="disk full"):
        qc._write_fasta_length_cache(tmp_path / "cache.tsv", record, {"ctg1": 4})
    assert not (tmp_path / "cache.tsv").exists()


def test_annotation_metrics_without_manifest_row_and_without_assembly_fasta(project_db):
    project, db = project_db
    _graph(db)
    missing = qc._annotation_metrics(db, project, {"entity_id": "ANN_999999"}, "builtin_v2")
    assert [(metric["metric_name"], metric["metric_value"]) for metric in missing] == [
        ("parseable", "0")
    ]

    gff3 = project.root / "annotation.gff3"
    gff3.write_text(
        "##gff-version 3\n"
        "ctg1\ttest\tgene\t1\t12\t.\t+\t.\tID=gene1\n"
        "ctg1\ttest\tCDS\t1\t12\t.\t+\t0\tID=cds1;Parent=gene1\n",
        encoding="utf-8",
    )
    metrics = qc._annotation_metrics(db, project, {
        "entity_id": "ANN_000001", "relative_path": "annotation.gff3",
        "file_id": "FIL_000009", "sha256": "a" * 64, "size_bytes": 4,
    }, "builtin_v2")
    by_name = {metric["metric_name"]: metric for metric in metrics if metric}
    # The annotation's assembly has no fasta file, so no seqid cross-check ran.
    assert by_name["parseable"]["metric_numeric"] == 1.0
    assert by_name["gene_count"]["metric_numeric"] == 1.0
    assert by_name["cds_count"]["metric_numeric"] == 1.0


def test_measure_metric_specs_drop_none_and_skip_unpaired_roles(tmp_path):
    from operon.qc import measure

    assert measure._payload_metric("stage", "name", None, None, "p") is None
    specs = measure._gff3_metric_specs(
        {"gene_count": 1, "unknown_metric": 2},
        {"protein_count": None, "protein_x_percent": 5.0},
    )
    names = {(stage, name) for stage, name, _v, _u in specs}
    assert ("annotation_basic", "protein_count") not in names
    assert ("annotation_basic", "unknown_metric") not in names
    assert ("annotation_basic", "protein_x_percent") in names
    units = {name: unit for _stage, name, _v, unit in specs}
    assert units["protein_x_percent"] == "percent"
    assert units["gene_count"] is None

    fastq = tmp_path / "reads.fastq"
    fastq.write_text("@r1\nACGT\n+\nIIII\n", encoding="utf-8")
    paired = tmp_path / "mate.fastq"
    paired.write_text("@r1\nACGT\n+\nIIII\n", encoding="utf-8")
    payload = measure.measure_file(
        fastq, file_format="fastq", file_role="other", paired_read=paired,
        sha256=hashlib.sha256(fastq.read_bytes()).hexdigest(),
        size_bytes=fastq.stat().st_size,
    )
    metric_names = {item["metric_name"] for item in payload["metrics"]}
    assert "paired_read_count_match" not in metric_names
    assert payload["file"]["file_role"] == "other"


# --------------------------------------------------------------------------- #
# qc/parsers.py - the pure-Python reference parsers (parity contract)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 4096])
def test_reference_binary_line_iterator_handles_every_line_ending(tmp_path, chunk_size):
    from operon.qc import parsers as reference

    path = tmp_path / "lines.bin"
    path.write_bytes(b"alpha\r\nbeta\rgamma\ndelta")
    assert list(reference._iter_binary_lines(path, chunk_size=chunk_size)) == [
        b"alpha", b"beta", b"gamma", b"delta",
    ]


def test_reference_fasta_reading_errors_and_empty_input(tmp_path):
    from operon.qc import parsers as reference

    empty = tmp_path / "empty.fa"
    empty.write_text("", encoding="utf-8")
    assert list(reference.iter_fasta(empty)) == []
    assert reference.fasta_lengths(empty) == {}
    assert reference.fasta_record_count(empty) == 0

    blank_header = tmp_path / "blank.fa"
    blank_header.write_text(">   \nACGT\n", encoding="utf-8")
    with pytest.raises(QCError, match="empty FASTA header"):
        list(reference.iter_fasta(blank_header))

    orphan_sequence = tmp_path / "orphan.fa"
    orphan_sequence.write_text("ACGT\n", encoding="utf-8")
    with pytest.raises(QCError, match="sequence data before first FASTA header"):
        list(reference.iter_fasta(orphan_sequence))

    bad_utf8 = tmp_path / "bad-utf8.fa"
    bad_utf8.write_bytes(b">\xff\xfe\nACGT\n")
    with pytest.raises(QCError, match="invalid UTF-8 in FASTA header"):
        list(reference.iter_fasta(bad_utf8))

    non_ascii = tmp_path / "non-ascii.fa"
    non_ascii.write_bytes(">ctg\nAC\u00e9\n".encode("utf-8"))
    with pytest.raises(QCError, match="non-ASCII data in FASTA sequence"):
        list(reference.iter_fasta(non_ascii))


def test_reference_fasta_stats_counts_bases_gaps_and_duplicates(tmp_path):
    from operon.qc import parsers as reference

    path = tmp_path / "stats.fa"
    path.write_text(
        ">ctg1 circular topologY=circular\n"
        "ACGTN-ACGTN--\n"
        ">ctg1 duplicate header\n"
        "RYSWKM\n"
        ">ctg3\n"
        "\n",
        encoding="utf-8",
    )
    stats = reference.fasta_stats(path)
    assert stats["sequence_count"] == 3
    assert stats["total_length"] == 19
    assert stats["gap_count"] == 2
    assert stats["gap_percent"] > 0
    assert stats["duplicate_sequence_id_count"] == 1
    assert stats["duplicate_header_count"] == 0
    assert stats["circular_sequence_count"] == 1
    assert stats["empty_sequence_count"] == 1
    assert stats["invalid_base_count"] == 0
    assert stats["ambiguous_base_percent"] > 0
    assert stats["min_sequence_length"] == 0
    assert stats["max_sequence_length"] == 13

    invalid = tmp_path / "invalid.fa"
    invalid.write_text(">ctg\nACGTZ\n", encoding="utf-8")
    assert reference.fasta_stats(invalid)["invalid_base_count"] == 1

    duplicate_header = tmp_path / "dup.fa"
    duplicate_header.write_text(">same\nAC\n>same\nGT\n", encoding="utf-8")
    assert reference.fasta_stats(duplicate_header)["duplicate_header_count"] == 1

    empty_input = tmp_path / "empty.fa"
    empty_input.write_text("", encoding="utf-8")
    empty_stats = reference.fasta_stats(empty_input)
    assert empty_stats["sequence_count"] == 0
    assert empty_stats["contig_n50"] == 0.0
    assert empty_stats["contig_l90"] == 0

    # Duplicate identifiers accumulate: 13 + 6 bases under the same seqid.
    assert reference.fasta_lengths(path)["ctg1"] == 19
    assert reference.fasta_record_count(path) == 3


def test_reference_fastq_records_and_structural_errors(tmp_path):
    from operon.qc import parsers as reference

    good = tmp_path / "good.fastq"
    good.write_text(
        "@r1 description\nACGTACGT\n+\nIIIIIIII\n"
        "@r2\nNNNN\n+\n!!!!\n",
        encoding="utf-8",
    )
    records = list(reference.iter_fastq(good))
    assert [record["id"] for record in records] == ["r1", "r2"]
    assert records[0]["header"] == "r1 description"
    assert records[1]["sequence"] == "NNNN"
    assert reference.fastq_record_count(good) == 2

    cases = {
        "truncated.fastq": ("@r1\nACGT\n", "truncated FASTQ record"),
        "blank.fastq": ("\nACGT\n+\nIIII\n", "blank line where FASTQ header"),
        "no-at.fastq": ("r1\nACGT\n+\nIIII\n", r"header does not start with '@'"),
        "empty-id.fastq": ("@ \nACGT\n+\nIIII\n", "empty FASTQ identifier"),
        "bad-plus.fastq": ("@r1\nACGT\n-\nIIII\n", "plus line malformed"),
        "mismatch.fastq": ("@r1\nACGT\n+\nII\n", "sequence/quality length mismatch"),
        "bad-quality.fastq": ("@r1\nACGT\n+\n\x1f\x1f\x1f\x1f\n", "quality character outside"),
    }
    for name, (text, message) in cases.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        with pytest.raises(QCError, match=message):
            list(reference.iter_fastq(path))

    empty = tmp_path / "empty.fastq"
    empty.write_text("", encoding="utf-8")
    assert reference.fastq_record_count(empty) == 0


def test_reference_fastq_stats_encodings_and_sampling(tmp_path):
    from operon.qc import parsers as reference

    path = tmp_path / "reads.fastq"
    path.write_text(
        "@r1\nACGTACGT\n+\nIIIIIIII\n"
        "@r2\nACGTACGT\n+\nIIIIIIII\n"
        "@r3\nNNNN\n+\n!!!!\n",
        encoding="utf-8",
    )
    stats = reference.fastq_stats(path)
    assert stats["read_count"] == 3
    assert stats["total_bases"] == 20
    assert stats["quality_encoding"] == "sanger_phred33"
    assert stats["duplicate_sampled_read_count"] == 3
    assert stats["duplicate_is_sampled"] is False
    assert stats["duplicate_sampling_strategy"] == "first_n"
    # Two of the three sampled reads exceed 1% of the sample.
    assert stats["overrepresented_sequence_count"] == 2
    assert stats["adapter_contamination_percent"] is None
    assert stats["read_length_min"] == 4
    assert stats["read_length_max"] == 8
    assert stats["read_length_mean"] == 20 / 3
    assert stats["read_length_l50"] >= 1
    assert stats["q20_percent"] > 0 and stats["q30_percent"] > 0
    assert 0 <= stats["gc_percent"] <= 100

    sampled = reference.fastq_stats(path, sample_size=1)
    assert sampled["duplicate_is_sampled"] is True
    assert sampled["duplicate_sampled_read_count"] == 1

    auto = reference.fastq_stats(path, phred_offset="auto")
    assert auto["quality_encoding"] == "sanger_phred33"

    high = tmp_path / "high.fastq"
    high.write_text("@r1\nACGT\n+\nhhhh\n", encoding="utf-8")
    phred64 = reference.fastq_stats(high, phred_offset=64)
    assert phred64["quality_encoding"] == "illumina_phred64"
    assert reference.fastq_stats(high, phred_offset="auto")["quality_encoding"] == (
        "ambiguous_assumed_phred33")

    low = tmp_path / "low.fastq"
    low.write_text("@r1\nACGT\n+\n!!!!\n", encoding="utf-8")
    with pytest.raises(QCError, match="phred\\+64 minimum"):
        reference.fastq_stats(low, phred_offset=64)

    empty = tmp_path / "empty.fastq"
    empty.write_text("", encoding="utf-8")
    empty_stats = reference.fastq_stats(empty)
    assert empty_stats["read_count"] == 0
    assert empty_stats["read_length_mean"] == 0.0
    assert empty_stats["quality_encoding"] == "sanger_phred33"

    for bad_size in (0, -1, True, "10"):
        with pytest.raises(ValueError, match="sample_size must be a positive integer"):
            reference.fastq_stats(path, sample_size=bad_size)
    with pytest.raises(ValueError, match="phred_offset must be 33, 64, or 'auto'"):
        reference.fastq_stats(path, phred_offset="unknown")


def test_reference_nx_histogram_paths():
    from collections import Counter

    from operon.qc import parsers as reference

    assert reference._nx_from_histogram(Counter(), 100, 0.5) == (0.0, 0)
    assert reference._nx_from_histogram(Counter({0: 2}), 0, 0.5) == (0.0, 1)
    assert reference._nx_from_histogram(Counter({10: 2}), 20, 0.5) == (10.0, 1)
    # The requested fraction is never reached: the smallest length is reported.
    assert reference._nx_from_histogram(Counter({5: 1}), 100, 0.5) == (5.0, 1)
    assert reference._n50([]) == (0.0, 0)
    assert reference._n50([4]) == (4.0, 1)


def test_reference_gff3_attribute_parsing_and_stats(tmp_path):
    from operon.qc import parsers as reference

    assert reference.parse_attributes("") == {}
    assert reference.parse_attributes(".") == {}
    assert reference.parse_attributes(";=;ID=gene1;Note=a%20b;no_equals") == {
        "ID": "gene1", "Note": "a b",
    }

    fasta = tmp_path / "assembly.fa"
    fasta.write_text(">ctg1\n" + "ACGT" * 10 + "\n", encoding="utf-8")
    gff3 = tmp_path / "annotation.gff3"
    gff3.write_text(
        "##gff-version 3\n"
        "##sequence-region ctg1 1 40\n"
        ">embedded\n"
        "ctg1\ttest\tgene\t1\t30\t.\t+\t.\tID=gene1\n"
        "ctg1\ttest\tmRNA\t1\t30\t.\t+\t.\tID=mrna1;Parent=gene1\n"
        "ctg1\ttest\tCDS\t1\t30\t.\t+\t0\tID=cds1;Parent=mrna1\n"
        "ctg1\ttest\tCDS\t1\t4\t.\t+\t0\tID=cds4\n"
        "missing\ttest\tCDS\t1\t30\t.\t+\t0\tID=cds2\n"
        "ctg1\ttest\tCDS\t38\t46\t.\t+\tbad\tID=cds3;Parent=absent\n"
        "ctg1\ttest\tgene\t1\t30\t.\t+\t.\tID=gene1\n"
        "ctg1\ttest\tgene\t1\t30\t.\t+\t.\tno_id_here\n"
        "ctg1\ttest\tbroken\n"
        "ctg1\ttest\tgene\tnot-a-number\t30\t.\t+\t.\tID=gene9\n"
        "ctg1\ttest\tgene\t0\t30\t.\t+\t.\tID=gene10\n"
        "##FASTA\n"
        ">ignored\n"
        "ACGT\n",
        encoding="utf-8",
    )
    timings: dict[str, float] = {}
    stats = reference.gff3_stats(gff3, fasta_path=fasta, timings=timings)
    assert stats["directive_count"] == 2
    assert stats["gene_count"] == 5
    assert stats["mrna_count"] == 1
    assert stats["cds_count"] == 4
    assert stats["exon_count"] == 0
    assert stats["feature_count"] == 10
    assert stats["feature_type_count"] == 3
    assert stats["seqid_count"] == 2
    assert stats["seqid_mismatch_count"] == 1
    assert stats["end_beyond_sequence_count"] == 1
    assert stats["coordinate_error_count"] == 4
    assert stats["missing_id_count"] == 1
    assert stats["duplicate_id_count"] == 1
    assert stats["missing_parent_count"] == 1
    assert stats["cds_not_multiple3_count"] == 1
    assert stats["cds_length_multiple3_percent"] == 75.0
    assert stats["cds_phase0_percent"] == 75.0
    assert {"assembly_fasta_lengths", "assembly_fasta_length_map_prepare",
            "gff3_scan", "gff3_finalize"} <= set(timings)

    plain = reference.gff3_stats(gff3)
    assert plain["seqid_mismatch_count"] == 0
    assert plain["cds_count"] == 4

    with pytest.raises(ValueError, match="either fasta_path or fasta_lengths_map"):
        reference.gff3_stats(gff3, fasta_path=fasta, fasta_lengths_map={"ctg1": 40})


def test_reference_protein_stats(tmp_path):
    from operon.qc import parsers as reference

    path = tmp_path / "proteins.fa"
    path.write_text(
        ">p1\nMXXAA*\n"
        ">p1\nMAA*AA*\n"
        ">p3\n\n"
        ">p4\nAA\n",
        encoding="utf-8",
    )
    stats = reference.protein_stats(path, cds_count=4)
    assert stats["protein_count"] == 4
    assert stats["protein_duplicate_id_count"] == 1
    assert stats["protein_empty_count"] == 1
    assert stats["protein_internal_stop_count"] == 1
    assert stats["protein_missing_start_count"] == 1
    assert stats["protein_missing_stop_count"] == 1
    assert stats["protein_x_percent"] > 0
    assert stats["cds_protein_count_match"] == 1

    no_cds = reference.protein_stats(path)
    assert "cds_protein_count_match" not in no_cds


def test_reference_gff3_without_timings_and_n50_progression(tmp_path):
    from operon.qc import parsers as reference

    fasta = tmp_path / "assembly.fa"
    fasta.write_text(">ctg1\n" + "ACGT" * 10 + "\n", encoding="utf-8")
    gff3 = tmp_path / "annotation.gff3"
    gff3.write_text(
        "##gff-version 3\n"
        "ctg1\ttest\tgene\t1\t30\t.\t+\t.\tID=gene1\n",
        encoding="utf-8",
    )
    # No timings mapping: every diagnostic timer stays untouched.
    stats = reference.gff3_stats(gff3, fasta_path=fasta)
    assert stats["gene_count"] == 1
    assert stats["seqid_mismatch_count"] == 0

    # The N50 loop walks past contigs that do not reach half of the assembly.
    assert reference._n50([4, 3, 3]) == (3.0, 2)


def test_reference_fastq_reader_close_without_underlying_close(tmp_path):
    from operon.qc import parsers as reference

    path = tmp_path / "reads.fastq"
    path.write_text("@r1\nACGT\n+\nIIII\n", encoding="utf-8")
    reader = reference._FastqReader(path)
    reader.close()
    reader.lines = iter([b"@r1", b"ACGT", b"+", b"IIII"])
    reader.close()  # iterators without close() are tolerated
    assert reader.next_record() == ("@r1", b"ACGT", b"IIII")
