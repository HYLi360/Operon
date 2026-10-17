#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Job-array orchestration paths for ``operon.tools._run``.

Split out of ``test_tools_execution_more`` so both modules stay inside the
file-length budget tracked by Codacy; doubles and assertions are unchanged.
"""

from __future__ import annotations

import shlex
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from operon import execution, tools
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import RemoteError, RemoteUnavailableError
from operon.shutdown import ShutdownRequested
from operon.tools import run_analysis
from tests.unit.test_tools_execution_more import (
    _stub_version_probe,
    ingest_assembly,
    write_tool_config,
)


@pytest.fixture
def operon_project(tmp_path):
    """A real initialised project plus an open database."""
    root = tmp_path / "proj"
    root.mkdir()
    assert (
        main(["--project", str(root), "init", str(root), "--project-id", "PRJ_T5_001"])
        == 0
    )
    project = load_project(root)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# tools: job-array orchestration (two-phase run_analysis)
# ---------------------------------------------------------------------------


class _SFTPSession:
    """SFTP handle double that only records its own close."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _TruncatedArrayResults:
    """A ``run_array`` result stream that breaks after its first item."""

    def __init__(self, first, expected: int = 2) -> None:
        self._first = first
        self._expected = expected

    def __len__(self) -> int:
        return self._expected

    def __iter__(self):
        yield self._first
        raise RuntimeError("array result stream broke")


class _ArrayExecutor:
    """Duck-typed array-capable backend bound to no scheduler.

    ``run_array`` synthesizes the per-task artifacts a real batch backend
    leaves behind (output, stdout/stderr logs, optionally the per-task
    exit-code file), records every call, and lets the test replace the
    returned result list -- including with objects that violate the
    ``run_array`` contract.  The SSH surface is reduced to recording stubs;
    ``_pull_outputs`` materializes the local output like a real pull does.
    """

    name = "test-array"
    scheduler = "none"

    def __init__(self, *, name: str | None = None, remote_root: str = ""):
        if name is not None:
            self.name = name
        self.remote_root = remote_root
        self.slurm = execution.SlurmConfig(array=True, array_concurrency=2)
        self.array_calls: list[dict] = []
        self.staged: list[str] = []
        self.pulled: list[list[str]] = []
        self.dropped: list[list[tuple[str, str]]] = []
        self.restored: list[list[tuple[str, str]]] = []
        self.closed = False
        self.write_outputs = True
        self.write_exitcodes = False
        self.array_results = None
        self.array_error: BaseException | None = None
        self.run_error: BaseException | None = None
        self.stage_error: str | None = None
        self.pull_error: str | None = None
        self.sftp = _SFTPSession()

    # -- executor protocol -----------------------------------------------

    def describe(self) -> str:
        return f"{self.name} tester"

    def cache_identity(self) -> str:
        return f"cache:{self.name}"

    def probe_environment(self):
        return None

    def close(self) -> None:
        self.closed = True

    def run(self, argv, **_kwargs):
        if self.run_error is not None:
            raise self.run_error
        raise AssertionError(f"unexpected per-file run: {argv}")

    def run_array(self, tasks, *, cwd=None, threads=None, array_concurrency=None):
        self.array_calls.append(
            {
                "run_ids": [task["run_id"] for task in tasks],
                "concurrency": array_concurrency,
            }
        )
        results = []
        for number, task in enumerate(tasks, start=1):
            if self.write_outputs:
                output = self._task_output(task)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(f"array output {number}\n", encoding="utf-8")
                Path(task["stdout_path"]).write_text(
                    f"array task {number}\n", encoding="utf-8"
                )
                Path(task["stderr_path"]).write_text("", encoding="utf-8")
            if self.write_exitcodes:
                exitcode_path = (
                    Path(task["stdout_path"]).parent / f"{task['run_id']}.exitcode"
                )
                exitcode_path.write_text("0\n", encoding="utf-8")
            results.append(
                execution.ExecResult(exit_code=0, scheduler_job_id=f"arr-{number}")
            )
        if self.array_error is not None:
            raise self.array_error
        if self.array_results is not None:
            return self.array_results(results)
        return results

    @staticmethod
    def _task_output(task) -> Path:
        argv = shlex.split(task["command"])
        return Path(argv[argv.index("--out") + 1])

    # -- SSH surface (staging and output backup/pull/restore) ------------

    def _connect(self):
        executor = self
        return SimpleNamespace(open_sftp=executor._open_sftp)

    def _open_sftp(self):
        return self.sftp

    def _stage_inputs(self, _client, _sftp, stage_inputs) -> None:
        for item in stage_inputs:
            if self.stage_error is not None:
                raise RemoteError(f"{self.stage_error}: {item}")
            self.staged.append(str(item))

    def _reset_outputs(self, _sftp, expected_outputs):
        return [(str(path), f"{path}.bak") for path in expected_outputs]

    def _pull_outputs(self, _client, _sftp, expected_outputs) -> None:
        if self.pull_error is not None:
            raise RemoteError(self.pull_error)
        self.pulled.append([str(path) for path in expected_outputs])
        for path in expected_outputs:
            local = Path(path)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text("pulled output\n", encoding="utf-8")

    def _drop_output_backups(self, _sftp, backups) -> None:
        self.dropped.append(list(backups))

    def _restore_output_backups(self, _sftp, backups) -> None:
        self.restored.append(list(backups))


def _install_array_executor(monkeypatch, executor: _ArrayExecutor) -> None:
    monkeypatch.setattr("operon.execution.get_executor", lambda *_a, **_k: executor)


def test_array_planning_shutdown_is_reraised_and_closes_the_executor(
    operon_project, monkeypatch
):
    project, db = operon_project
    ingest_assembly(project, db, 1, compress_archive=False)
    ingest_assembly(project, db, 2, compress_archive=False)
    write_tool_config(project)
    executor = _ArrayExecutor()
    _install_array_executor(monkeypatch, executor)

    def interrupted_probe(*_args, **_kwargs):
        raise ShutdownRequested(signal.SIGINT)

    monkeypatch.setattr(tools._probe, "_detect_version_record", interrupted_probe)
    with pytest.raises(ShutdownRequested):
        run_analysis(project, db, "fake_recipe", backend="test-array")

    # The batch aborted in phase 1: nothing was submitted, nothing was
    # recorded, and the owned executor was still closed.
    assert executor.array_calls == []
    assert executor.closed
    assert db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"] == 0


def test_array_planning_failure_reports_error_and_plans_the_rest(
    operon_project, monkeypatch
):
    project, db = operon_project
    records = sorted(
        (
            ingest_assembly(project, db, number, compress_archive=False)
            for number in (1, 2, 3)
        ),
        key=lambda record: record["file_id"],
    )
    write_tool_config(project)
    _stub_version_probe(monkeypatch)
    executor = _ArrayExecutor()
    _install_array_executor(monkeypatch, executor)
    missing = records[0]
    (project.root / missing["relative_path"]).unlink()

    phases: list[tuple[str, str]] = []
    results = run_analysis(
        project,
        db,
        "fake_recipe",
        backend="test-array",
        progress_callback=lambda _i, _t, file_id, phase: phases.append(
            (file_id, phase)
        ),
    )

    assert [result["file_id"] for result in results] == [
        record["file_id"] for record in records
    ]
    assert [result["status"] for result in results] == [
        "error",
        "completed",
        "completed",
    ]
    assert "input missing" in results[0]["error"]
    assert (missing["file_id"], "start") in phases
    assert (missing["file_id"], "error") in phases
    # The two computable files still formed one array submission.
    assert len(executor.array_calls) == 1
    assert len(executor.array_calls[0]["run_ids"]) == 2


def test_array_cache_hits_report_status_without_resubmitting(
    operon_project, monkeypatch
):
    project, db = operon_project
    ingest_assembly(project, db, 1, compress_archive=False)
    ingest_assembly(project, db, 2, compress_archive=False)
    write_tool_config(project)
    _stub_version_probe(monkeypatch)
    executor = _ArrayExecutor()
    _install_array_executor(monkeypatch, executor)

    first = run_analysis(project, db, "fake_recipe", backend="test-array")
    assert [result["status"] for result in first] == ["completed", "completed"]
    assert len(executor.array_calls) == 1

    phases: list[str] = []
    cached = run_analysis(
        project,
        db,
        "fake_recipe",
        backend="test-array",
        progress_callback=lambda _i, _t, _f, phase: phases.append(phase),
    )

    assert [result["status"] for result in cached] == ["cached", "cached"]
    assert phases == ["start", "cached", "start", "cached"]
    assert len(executor.array_calls) == 1  # cache hits never resubmit


def test_two_phase_single_plan_shutdown_interrupts_the_running_job(
    operon_project, monkeypatch
):
    project, db = operon_project
    ingest_assembly(project, db, 1, compress_archive=False)
    write_tool_config(project)
    _stub_version_probe(monkeypatch)
    executor = _ArrayExecutor()
    executor.run_error = ShutdownRequested(signal.SIGINT)
    _install_array_executor(monkeypatch, executor)

    with pytest.raises(ShutdownRequested):
        run_analysis(project, db, "fake_recipe", backend="test-array")

    # One plan is below the array threshold, so it ran through the
    # sequential fallback and its interrupt bookkeeping is per-file.
    assert executor.array_calls == []
    jobs = db.query("SELECT * FROM analysis_jobs ORDER BY job_id")
    assert [job["status"] for job in jobs] == ["interrupted"]
    assert "interrupted by signal" in jobs[0]["error"]
    assert (
        db.query(
            "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='analysis:fake_recipe'"
        )[0]["n"]
        == 0
    )


def test_ssh_array_staging_failures_skip_submission_entirely(
    operon_project, tmp_path, monkeypatch
):
    project, db = operon_project
    ingest_assembly(project, db, 1, compress_archive=False)
    ingest_assembly(project, db, 2, compress_archive=False)
    write_tool_config(project)
    _stub_version_probe(monkeypatch)
    executor = _ArrayExecutor(name="ssh", remote_root=str(tmp_path / "mirror"))
    executor.write_outputs = False
    executor.stage_error = "cannot stage input"
    _install_array_executor(monkeypatch, executor)

    results = run_analysis(project, db, "fake_recipe", backend="ssh")

    assert [result["status"] for result in results] == ["error", "error"]
    assert all("cannot stage input" in result["error"] for result in results)
    assert executor.array_calls == []
    assert [job["status"] for job in db.query("SELECT * FROM analysis_jobs")] == [
        "failed",
        "failed",
    ]
    assert executor.sftp.closed


@pytest.mark.bug("ODR-76")
def test_sequential_ssh_batch_aborts_when_reconnect_fails(operon_project, monkeypatch):
    """A lost SSH re-connect stops the batch instead of failing every file.

    In the field (535-file rpsblast_cdd_archive) one dropped connection made
    all 533 remaining files fail identically; the loop must abort after the
    in-flight file is recorded, not convert a connection condition into
    per-file errors.
    """
    project, db = operon_project
    ingest_assembly(project, db, 1, compress_archive=False)
    ingest_assembly(project, db, 2, compress_archive=False)
    write_tool_config(project)
    _stub_version_probe(monkeypatch)
    executor = _ArrayExecutor()
    # A chain recipe on a non-array executor keeps the sequential per-file loop.
    executor.slurm = execution.SlurmConfig()
    calls: list[list[str]] = []

    def scripted_run(
        argv,
        *,
        stdout_path=None,
        stderr_path=None,
        expected_outputs=(),
        **_kwargs,
    ):
        calls.append([str(arg) for arg in argv])
        if len(calls) == 1:
            for path in expected_outputs:
                output = Path(path)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("sequential output\n", encoding="utf-8")
            return execution.ExecResult(exit_code=0, details={})
        raise RemoteUnavailableError(
            "connection to ssh:test lost and re-connect failed: network unreachable"
        )

    executor.run = scripted_run
    _install_array_executor(monkeypatch, executor)

    with pytest.raises(RemoteUnavailableError):
        run_analysis(project, db, "fake_recipe", backend="ssh")

    statuses = [
        job["status"] for job in db.query("SELECT * FROM analysis_jobs ORDER BY job_id")
    ]
    assert statuses == ["completed", "failed"]
    assert len(calls) == 2


def test_array_result_count_mismatch_fails_the_whole_batch(operon_project, monkeypatch):
    project, db = operon_project
    ingest_assembly(project, db, 1, compress_archive=False)
    ingest_assembly(project, db, 2, compress_archive=False)
    write_tool_config(project)
    _stub_version_probe(monkeypatch)
    executor = _ArrayExecutor()
    executor.array_results = lambda results: results[:1]
    _install_array_executor(monkeypatch, executor)

    phases: list[str] = []
    results = run_analysis(
        project,
        db,
        "fake_recipe",
        backend="test-array",
        progress_callback=lambda _i, _t, _f, phase: phases.append(phase),
    )

    assert [result["status"] for result in results] == ["error", "error"]
    assert all(
        "run_array returned 1 results for 2 tasks" in result["error"]
        for result in results
    )
    assert [job["status"] for job in db.query("SELECT * FROM analysis_jobs")] == [
        "failed",
        "failed",
    ]
    # Both planned files were reported through the callback as errors.
    assert phases.count("error") == 2


def test_array_collection_failure_keeps_finished_tasks(operon_project, monkeypatch):
    project, db = operon_project
    records = sorted(
        (
            ingest_assembly(project, db, number, compress_archive=False)
            for number in (1, 2)
        ),
        key=lambda record: record["file_id"],
    )
    write_tool_config(project)
    _stub_version_probe(monkeypatch)
    executor = _ArrayExecutor()
    executor.array_results = lambda results: _TruncatedArrayResults(results[0])
    _install_array_executor(monkeypatch, executor)

    phases: list[str] = []
    results = run_analysis(
        project,
        db,
        "fake_recipe",
        backend="test-array",
        progress_callback=lambda _i, _t, _f, phase: phases.append(phase),
    )

    # The already-collected task keeps its completed row; the stream failure
    # fails only the tasks that were never finalized.
    assert [result["status"] for result in results] == ["completed", "error"]
    assert results[0]["file_id"] == records[0]["file_id"]
    jobs = db.query("SELECT * FROM analysis_jobs ORDER BY job_id")
    assert [job["status"] for job in jobs] == ["completed", "failed"]
    assert "array result stream broke" in jobs[1]["error"]
    assert phases.count("error") == 1


def test_array_collection_shutdown_interrupts_unfinished_tasks(
    operon_project, monkeypatch
):
    project, db = operon_project
    ingest_assembly(project, db, 1, compress_archive=False)
    ingest_assembly(project, db, 2, compress_archive=False)
    write_tool_config(project)
    _stub_version_probe(monkeypatch)
    executor = _ArrayExecutor()
    _install_array_executor(monkeypatch, executor)

    def interrupted_finalize(*_args, **_kwargs):
        raise ShutdownRequested(signal.SIGINT)

    monkeypatch.setattr(
        tools._run, "_finalize_analysis_execution", interrupted_finalize
    )
    with pytest.raises(ShutdownRequested):
        run_analysis(project, db, "fake_recipe", backend="test-array")

    # The KeyboardInterrupt during collection keeps the interrupt path: the
    # in-flight task and every uncollected task are interrupted, not failed.
    jobs = db.query("SELECT * FROM analysis_jobs ORDER BY job_id")
    assert [job["status"] for job in jobs] == ["interrupted", "interrupted"]
    assert executor.closed


def test_ssh_array_callback_abort_restores_pending_backups(
    operon_project, tmp_path, monkeypatch
):
    project, db = operon_project
    records = sorted(
        (
            ingest_assembly(project, db, number, compress_archive=False)
            for number in (1, 2)
        ),
        key=lambda record: record["file_id"],
    )
    write_tool_config(project)
    _stub_version_probe(monkeypatch)
    executor = _ArrayExecutor(name="ssh", remote_root=str(tmp_path / "mirror"))
    executor.write_outputs = False
    _install_array_executor(monkeypatch, executor)

    class Cancelled(Exception):
        pass

    def cancelling(_index, _total, _file_id, phase):
        if phase == "completed":
            raise Cancelled()

    with pytest.raises(Cancelled):
        run_analysis(
            project, db, "fake_recipe", backend="ssh", progress_callback=cancelling
        )

    jobs = db.query("SELECT * FROM analysis_jobs ORDER BY job_id")
    assert [job["status"] for job in jobs] == ["completed", "interrupted"]
    assert jobs[0]["file_id"] == records[0]["file_id"]
    assert len(executor.pulled) == 1
    # The never-collected task's remote output backup was restored before its
    # job was marked interrupted.
    pending_output = (
        project.analysis_root
        / "fake"
        / records[1]["entity_id"]
        / f"{records[1]['file_id']}.genome_fasta.out.tsv"
    )
    assert executor.restored == [[(str(pending_output), f"{pending_output}.bak")]]
    assert executor.sftp.closed


def test_ssh_array_interrupt_finalize_failure_restores_backups_and_fails_tasks(
    operon_project, tmp_path, monkeypatch
):
    project, db = operon_project
    records = [
        ingest_assembly(project, db, number, compress_archive=False)
        for number in (1, 2)
    ]
    write_tool_config(project)
    _stub_version_probe(monkeypatch)
    executor = _ArrayExecutor(name="ssh", remote_root=str(tmp_path / "mirror"))
    executor.write_outputs = False
    executor.write_exitcodes = True
    executor.pull_error = "output pull failed"
    executor.array_error = ShutdownRequested(signal.SIGINT)
    _install_array_executor(monkeypatch, executor)

    with pytest.raises(ShutdownRequested):
        run_analysis(project, db, "fake_recipe", backend="ssh")

    # Every finished task had written its exit code, but the pull (and with it
    # the finalization) failed: the task is failed, not completed, and its
    # remote backup was restored.
    jobs = db.query("SELECT * FROM analysis_jobs ORDER BY job_id")
    assert [job["status"] for job in jobs] == ["failed", "failed"]
    assert all("output pull failed" in job["error"] for job in jobs)
    assert executor.pulled == []
    assert len(executor.restored) == 2
    restored_targets = {
        Path(backup[0]).name for backups in executor.restored for backup in backups
    }
    assert restored_targets == {
        f"{record['file_id']}.genome_fasta.out.tsv" for record in records
    }
    assert executor.sftp.closed
