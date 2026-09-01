"""Branch-focused coverage for project discovery and workflow failures."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from operon import workflow
from operon.cli import main
from operon.config import Project, load_project
from operon.database import Database
from operon.errors import ConfigError, ConflictError


@pytest.fixture
def project_db(tmp_path: Path):
    root = tmp_path / "project"
    assert main(["--project", str(root), "init", str(root)]) == 0
    project = load_project(root)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def test_project_resolution_initialization_and_discovery_edges(tmp_path):
    root = tmp_path / "project"
    project = Project.init(root)
    absolute = tmp_path / "absolute"
    assert project._resolve(absolute) == absolute
    with pytest.raises(ConfigError, match="already initialized"):
        Project.init(root)

    nested_file = root / "nested" / "input.txt"
    nested_file.parent.mkdir()
    nested_file.write_text("x", encoding="utf-8")
    assert Project.find(nested_file).root == root
    assert load_project(root / "project.yaml").root == root
    with pytest.raises(ConfigError, match="no project.yaml found"):
        Project.find(tmp_path / "uninitialized" / "child")


def test_state_validation_illegal_transition_and_missing_run(project_db):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Example"})
    with pytest.raises(ValueError, match="unknown state"):
        workflow.set_state(db, "organism", "ORG_000001", "not-a-state")
    workflow.set_state(db, "organism", "ORG_000001", "METADATA_FETCHED")
    with pytest.raises(ConflictError, match="illegal transition"):
        workflow.set_state(db, "organism", "ORG_000001", "RELEASED")
    with pytest.raises(ValueError, match="does not exist"):
        workflow.finish_run(db, project, "WF_MISSING", status="failed")


@pytest.mark.parametrize(
    ("exit_code", "error", "expected"),
    [(3, None, "exit code 3"), (0, "reported failure", "reported failure")],
)
def test_external_command_result_failures_are_recorded(project_db, exit_code, error, expected):
    project, db = project_db

    class Executor:
        def describe(self):
            return "fake"

        def run(self, *_a, **_k):
            return SimpleNamespace(
                exit_code=exit_code,
                error=error,
                scheduler_job_id=None,
                details={"backend": "fake"},
            )

    with pytest.raises(RuntimeError, match=expected):
        workflow.run_external_command(db, project, ["fake"], step="edge", executor=Executor())


def test_failed_exit_code_is_not_masked_by_missing_output_check(project_db):
    project, db = project_db

    class Executor:
        def describe(self):
            return "fake"

        def run(self, *_a, **_k):
            return SimpleNamespace(
                exit_code=3,
                error=None,
                scheduler_job_id=None,
                details={"backend": "fake"},
            )

    with pytest.raises(RuntimeError, match="edge failed: exit code 3"):
        workflow.run_external_command(
            db, project, ["fake"], step="edge",
            expected_outputs=[project.root / "missing.out"], executor=Executor(),
        )
    row = db.query("SELECT status, error FROM workflow_runs WHERE step='edge'")[0]
    assert row["status"] == "failed"
    assert row["error"] == "exit code 3"


@pytest.mark.parametrize(
    ("exc", "timeout", "expected_error"),
    [
        (subprocess.TimeoutExpired(["fake"], 5), 5, "timeout after 5s"),
        (OSError("no such device"), None, "no such device"),
        (ValueError("bad response"), None, "ValueError: bad response"),
    ],
)
def test_external_command_executor_exceptions_are_recorded_then_raised(
    project_db, exc, timeout, expected_error,
):
    project, db = project_db

    class Executor:
        def describe(self):
            return "fake"

        def run(self, *_a, **_k):
            raise exc

    with pytest.raises(RuntimeError, match=re.escape(f"edge failed: {expected_error}")):
        workflow.run_external_command(
            db, project, ["fake"], step="edge", timeout=timeout, executor=Executor(),
        )
    row = db.query("SELECT status, error FROM workflow_runs WHERE step='edge'")[0]
    assert row["status"] == "failed"
    assert row["error"] == expected_error


def test_owned_executor_is_closed(project_db, monkeypatch):
    project, db = project_db

    class Executor:
        closed = False

        def describe(self):
            return "fake"

        def run(self, *_a, **_k):
            return SimpleNamespace(
                exit_code=0, error=None, scheduler_job_id=None, details={"backend": "fake"}
            )

        def close(self):
            self.closed = True

    executor = Executor()
    monkeypatch.setattr("operon.execution.get_executor", lambda *_a, **_k: executor)
    assert workflow.run_external_command(db, project, ["fake"], step="edge")["status"] == "completed"
    assert executor.closed
