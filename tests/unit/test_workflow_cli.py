"""Workflow history query and terminal rendering tests."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from operon import cli
from operon.cli import main
from operon.config import Project
from operon.database import Database
from operon.workflow import get_run, list_runs, log_run


@pytest.fixture
def workflow_project(tmp_path: Path):
    project = Project.init(tmp_path)
    db = Database(project.db_path)
    records = [
        {
            "run_id": "WF_PARENT",
            "entity_type": "assembly",
            "entity_id": "ASM_000001",
            "step": "ingest",
            "status": "completed",
            "started_at": "2026-09-01T23:00:00+00:00",
            "finished_at": "2026-09-01T23:00:03+00:00",
            "tool": "archive",
            "executor": "local",
        },
        {
            "run_id": "WF_FAILED",
            "parent_run_id": "WF_PARENT",
            "entity_type": "assembly",
            "entity_id": "ASM_000001",
            "step": "qc",
            "status": "failed",
            "started_at": "2026-09-02T00:30:00+00:00",
            "finished_at": "2026-09-02T00:30:02+00:00",
            "duration_seconds": 2.5,
            "command": "qc-tool --input genome.fa --output report.json",
            "tool": "qc-tool",
            "tool_version": "1.2.3",
            "executor": "local",
            "exit_code": 7,
            "error": "input could not be parsed",
            "execution_details": json.dumps({"backend": "local", "attempt": 1}),
        },
        {
            "run_id": "WF_RESUMED",
            "resumes_run_id": "WF_FAILED",
            "entity_type": "assembly",
            "entity_id": "ASM_000001",
            "step": "qc",
            "status": "completed",
            "started_at": "2026-09-03T01:00:00+02:00",
            "finished_at": "2026-09-03T01:00:04+02:00",
            "tool": "qc-tool",
            "executor": "slurm",
            "scheduler_job_id": "12345",
            "execution_details": "legacy plain text",
        },
        {
            "run_id": "WF_RELEASE",
            "entity_type": "release",
            "entity_id": "2026.09",
            "step": "release",
            "status": "completed",
            "started_at": "2026-09-04T08:00:00+00:00",
            "finished_at": None,
        },
    ]
    for record in records:
        log_run(db, project, record)
    db.close()
    return project


def test_list_runs_supports_filters_timezones_and_unlimited_offset(workflow_project):
    db = Database(workflow_project.db_path, read_only=True)
    try:
        filtered = list_runs(
            db,
            started_from="2026-09-02T00:00:00+00:00",
            started_to="2026-09-03T00:00:00+00:00",
            steps=["qc", "verify"],
            statuses=["failed", "completed"],
            entity_type="assembly",
            entity_id="ASM_000001",
            tool="qc-tool",
            limit=0,
            oldest_first=True,
        )
        assert [record["run_id"] for record in filtered] == ["WF_FAILED", "WF_RESUMED"]
        assert [record["run_id"] for record in list_runs(
            db, parent_run_id="WF_PARENT", executor="local",
        )] == ["WF_FAILED"]
        assert [record["run_id"] for record in list_runs(
            db, resumes_run_id="WF_FAILED", executor="slurm",
        )] == ["WF_RESUMED"]
        assert [record["run_id"] for record in list_runs(
            db, run_id="WF_RELEASE", limit=0, offset=1,
        )] == []
        assert get_run(db, "WF_FAILED")["error"] == "input could not be parsed"
        assert get_run(db, "WF_MISSING") is None
    finally:
        db.close()


def test_workflow_list_table_filters_and_order(workflow_project, capsys):
    root = str(workflow_project.root)
    assert main(["--project", root, "workflow", "list", "--limit", "0"]) == 0
    output = capsys.readouterr().out
    assert "started_local" in output and "duration" in output
    assert output.index("WF_RELEASE") < output.index("WF_RESUMED") < output.index("WF_FAILED")

    assert main([
        "--project", root, "workflow", "list",
        "--from", "2026-09-02T00:00:00Z",
        "--to", "2026-09-03T00:00:00Z",
        "--step", "qc",
        "--status", "failed",
        "--entity-type", "assembly",
        "--entity-id", "ASM_000001",
        "--parent-run-id", "WF_PARENT",
        "--tool", "qc-tool",
        "--executor", "local",
        "--oldest-first",
    ]) == 0
    output = capsys.readouterr().out
    assert "WF_FAILED" in output
    assert "WF_RESUMED" not in output
    assert "2.500s" in output

    assert main([
        "--project", root, "workflow", "list", "--run-id", "missing",
    ]) == 0
    assert capsys.readouterr().out.strip() == "no workflow runs matched"


def test_workflow_list_json_jsonl_and_pagination(workflow_project, capsys):
    root = str(workflow_project.root)
    assert main([
        "--project", root, "workflow", "list", "--format", "json",
        "--oldest-first", "--limit", "1", "--offset", "1",
    ]) == 0
    records = json.loads(capsys.readouterr().out)
    assert [record["run_id"] for record in records] == ["WF_FAILED"]
    assert records[0]["execution_details"] == {"attempt": 1, "backend": "local"}

    assert main([
        "--project", root, "workflow", "list", "--format", "jsonl",
        "--resumes-run-id", "WF_FAILED", "--limit", "0",
    ]) == 0
    line = json.loads(capsys.readouterr().out)
    assert line["run_id"] == "WF_RESUMED"
    assert line["execution_details"] == "legacy plain text"

    assert main([
        "--project", root, "workflow", "list", "--format", "json",
        "--run-id", "missing",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_workflow_show_text_json_and_missing(workflow_project, capsys):
    root = str(workflow_project.root)
    assert main(["--project", root, "workflow", "show", "WF_FAILED"]) == 0
    output = capsys.readouterr().out
    assert "Workflow run" in output
    assert "Timing and resources" in output
    assert "Artifacts and logs" in output
    assert "input could not be parsed" in output
    assert '"attempt": 1' in output

    assert main([
        "--project", root, "workflow", "show", "WF_RESUMED", "--format", "json",
    ]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["run_id"] == "WF_RESUMED"
    assert record["execution_details"] == "legacy plain text"

    assert main(["--project", root, "workflow", "show", "WF_MISSING"]) == 2
    assert "workflow run does not exist" in capsys.readouterr().err


def test_workflow_time_validation_and_rendering_edges(workflow_project, capsys):
    assert cli._workflow_time("2026-09-02T00:00:00Z").endswith("+00:00")
    assert cli._workflow_time("2026-09-02").endswith(
        datetime_local_suffix()
    )
    with pytest.raises(argparse.ArgumentTypeError, match="ISO-8601"):
        cli._workflow_time("yesterday afternoon")
    with pytest.raises(argparse.ArgumentTypeError, match="non-negative"):
        cli._nonnegative_int("not-a-number")
    with pytest.raises(argparse.ArgumentTypeError, match="non-negative"):
        cli._nonnegative_int("-1")

    root = str(workflow_project.root)
    assert main([
        "--project", root, "workflow", "list",
        "--from", "2026-09-03", "--to", "2026-09-02",
    ]) == 2
    assert "--from must be earlier than --to" in capsys.readouterr().err

    assert cli._workflow_duration({"started_at": None, "finished_at": None}) == "-"
    assert cli._workflow_duration({
        "started_at": "invalid", "finished_at": "also-invalid",
    }) == "-"
    assert cli._workflow_duration({
        "started_at": "2026-09-02T00:00:03+00:00",
        "finished_at": "2026-09-02T00:00:01+00:00",
    }) == "0.000s"
    assert cli._compact_workflow_value("x" * 20, 10) == "xxxxxxx..."
    assert cli._workflow_started_local("invalid timestamp") == "invalid timestamp"
    assert cli._workflow_started_local(None) == "-"


def datetime_local_suffix() -> str:
    """Return the offset suffix used by the CLI for a naive timestamp."""
    return datetime.fromisoformat("2026-09-02").astimezone().isoformat()[-6:]


def test_workflow_command_uses_read_only_database(workflow_project, capsys):
    root = workflow_project.root
    original_dir_mode = root.stat().st_mode
    original_db_mode = workflow_project.db_path.stat().st_mode
    try:
        os.chmod(workflow_project.db_path, 0o444)
        os.chmod(root, 0o555)
        assert main([
            "--project", str(root), "workflow", "show", "WF_PARENT",
        ]) == 0
        assert "WF_PARENT" in capsys.readouterr().out
    finally:
        os.chmod(root, original_dir_mode)
        os.chmod(workflow_project.db_path, original_db_mode)
