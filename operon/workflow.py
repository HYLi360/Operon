"""Deterministic workflow state machine and machine-readable run logs.

Completion markers are only written after expected outputs exist and validate,
not guessed from a folder or screen log.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from operon import __version__
from operon.config import Project
from operon.database import Database
from operon.errors import ConflictError, OperonError, ValidationError
from operon.utils import append_jsonl, now_iso, path_is_nonempty, sha256_path

VALID_STATES = {
    "DISCOVERED",
    "METADATA_FETCHED",
    "METADATA_VALIDATED",
    "DOWNLOAD_PENDING",
    "DOWNLOADED",
    "CHECKSUM_VERIFIED",
    "STANDARDIZED",
    "QC_RUNNING",
    "QC_COMPLETE",
    "ACCEPTED",
    "REVIEW",
    "REJECTED",
    "RELEASED",
    # explicit failure states
    "DOWNLOAD_FAILED",
    "CHECKSUM_FAILED",
    "FORMAT_INVALID",
    "METADATA_INVALID",
    "STANDARDIZATION_FAILED",
    "QC_FAILED",
}

# Strict transitions.  Force is available for manual corrections, which are
# always recorded in the changes audit table.
TRANSITIONS: dict[str, set[str]] = {
    "DISCOVERED": {"METADATA_FETCHED", "METADATA_INVALID"},
    "METADATA_FETCHED": {"METADATA_VALIDATED", "METADATA_INVALID", "DISCOVERED"},
    "METADATA_VALIDATED": {"DOWNLOAD_PENDING", "METADATA_INVALID", "DISCOVERED"},
    "DOWNLOAD_PENDING": {"DOWNLOADED", "DOWNLOAD_FAILED"},
    "DOWNLOADED": {"CHECKSUM_VERIFIED", "CHECKSUM_FAILED", "DOWNLOAD_FAILED"},
    "CHECKSUM_VERIFIED": {"STANDARDIZED", "CHECKSUM_FAILED", "DOWNLOADED"},
    "STANDARDIZED": {"QC_RUNNING", "STANDARDIZATION_FAILED", "CHECKSUM_VERIFIED"},
    "QC_RUNNING": {"QC_COMPLETE", "QC_FAILED"},
    "QC_COMPLETE": {"ACCEPTED", "REVIEW", "REJECTED", "QC_FAILED"},
    "ACCEPTED": {"RELEASED", "REVIEW", "REJECTED"},
    "REVIEW": {"ACCEPTED", "REJECTED", "QC_COMPLETE"},
    "REJECTED": {"REVIEW", "ACCEPTED", "QC_COMPLETE"},
    "RELEASED": set(),
    "DOWNLOAD_FAILED": {"DOWNLOAD_PENDING"},
    "CHECKSUM_FAILED": {"DOWNLOAD_PENDING", "DOWNLOADED"},
    "FORMAT_INVALID": {"STANDARDIZED", "DOWNLOADED"},
    "METADATA_INVALID": {"METADATA_FETCHED", "METADATA_VALIDATED"},
    "STANDARDIZATION_FAILED": {"STANDARDIZED"},
    "QC_FAILED": {"QC_RUNNING"},
}


def set_state(db: Database, entity_type: str, entity_id: str, state: str,
              message: str | None = None, force: bool = False, actor: str | None = None) -> None:
    state = state.upper()
    if state not in VALID_STATES:
        raise ValueError(f"unknown state {state!r}; valid: {sorted(VALID_STATES)}")
    old = db.get_entity_state(entity_type, entity_id)
    if old and not force and state != old and state not in TRANSITIONS.get(old, set()):
        raise ConflictError(
            f"illegal transition {old} -> {state} for {entity_type} {entity_id}; use --force for a manual, audited transition"
        )
    db.set_entity_state(entity_type, entity_id, state, message)
    if old != state:
        db.record_change(
            "entity_state", f"{entity_type}:{entity_id}", "state", old, state,
            reason=message or ("forced transition" if force else "workflow transition"),
            actor=actor,
        )


def set_state_bulk(db: Database, entity_type: str, entity_id: str, state: str,
                   message: str | None = None, actor: str | None = None) -> None:
    """Same as set_state but tolerant for batch QC loops."""
    set_state(db, entity_type, entity_id, state, message, force=True, actor=actor)


def new_run_id() -> str:
    """Unique workflow run ID (time plus random suffix, safe across rapid runs)."""
    return f"WF_{now_iso().replace('-', '').replace(':', '').replace('T', '_')}_{uuid.uuid4().hex[:8]}"


_WORKFLOW_RUN_COLUMNS = [
    "run_id", "parent_run_id", "resumes_run_id", "entity_type", "entity_id", "step", "status",
    "started_at", "finished_at", "exit_code", "command", "tool", "tool_version",
    "parameter_set", "input_sha256", "output_sha256", "threads", "max_rss_mb",
    "duration_seconds", "avg_rss_mb", "cpu_seconds",
    "log_file", "stdout_file", "stderr_file", "error",
    "executor", "scheduler_job_id", "execution_details", "environment_id",
]


def list_runs(
        db: Database,
        *,
        started_from: str | None = None,
        started_to: str | None = None,
        run_id: str | None = None,
        steps: Iterable[str] = (),
        statuses: Iterable[str] = (),
        entity_type: str | None = None,
        entity_id: str | None = None,
        parent_run_id: str | None = None,
        resumes_run_id: str | None = None,
        tool: str | None = None,
        executor: str | None = None,
        limit: int = 50,
        offset: int = 0,
        oldest_first: bool = False,
) -> list[dict[str, Any]]:
    """Return workflow runs matching stable, read-only CLI filters.

    Time bounds apply to ``started_at`` and use a half-open interval. SQLite's
    Julian-day conversion keeps comparisons correct when records use different
    UTC offsets. ``limit=0`` means no row limit.
    """
    conditions: list[str] = []
    parameters: list[Any] = []

    def exact(column: str, value: str | None) -> None:
        if value is not None:
            conditions.append(f"{column}=?")
            parameters.append(value)

    if started_from is not None:
        conditions.append("julianday(started_at) >= julianday(?)")
        parameters.append(started_from)
    if started_to is not None:
        conditions.append("julianday(started_at) < julianday(?)")
        parameters.append(started_to)
    exact("run_id", run_id)
    exact("entity_type", entity_type)
    exact("entity_id", entity_id)
    exact("parent_run_id", parent_run_id)
    exact("resumes_run_id", resumes_run_id)
    exact("tool", tool)
    exact("executor", executor)

    for column, values in (("step", list(steps)), ("status", list(statuses))):
        if values:
            conditions.append(f"{column} IN ({', '.join('?' for _ in values)})")
            parameters.extend(values)

    sql = "SELECT * FROM workflow_runs"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    direction = "ASC" if oldest_first else "DESC"
    sql += f" ORDER BY julianday(started_at) {direction}, rowid {direction}"
    if limit:
        sql += " LIMIT ? OFFSET ?"
        parameters.extend((limit, offset))
    elif offset:
        sql += " LIMIT -1 OFFSET ?"
        parameters.append(offset)
    return [dict(row) for row in db.conn.execute(sql, parameters).fetchall()]


def get_run(db: Database, run_id: str) -> dict[str, Any] | None:
    """Return one workflow run without modifying project state."""
    row = db.conn.execute(
        "SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def flush_run_log(project: Project, records: Iterable[dict[str, Any]]) -> None:
    """Append workflow records after the transaction that produced them is final."""
    for record in records:
        append_jsonl(project.logs_root / "workflow.jsonl", record)


def log_run(
        db: Database,
        project: Project,
        record: dict[str, Any],
        *,
        jsonl_buffer: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Store a workflow run and append JSONL only after its DB write succeeds.

    Transactional callers can supply ``jsonl_buffer`` and flush it only after
    their outer transaction commits.  If that transaction rolls back, its
    buffered completed records must be discarded.
    """
    record = dict(record)
    record.setdefault("run_id", new_run_id())
    record.setdefault("status", "completed")
    record.setdefault("started_at", now_iso())
    record.setdefault("finished_at", now_iso())
    record.setdefault("tool_version", __version__)
    columns = _WORKFLOW_RUN_COLUMNS
    with db.transaction():
        db.conn.execute(
            f"INSERT INTO workflow_runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [record.get(c) for c in columns],
        )
    if jsonl_buffer is None:
        flush_run_log(project, [record])
    else:
        jsonl_buffer.append(record)
    return record


def start_run(db: Database, record: dict[str, Any]) -> dict[str, Any]:
    """Create a durable running workflow row before any item can commit."""
    record = dict(record)
    record.setdefault("run_id", new_run_id())
    record.setdefault("status", "running")
    record.setdefault("started_at", now_iso())
    record.setdefault("tool_version", __version__)
    columns = _WORKFLOW_RUN_COLUMNS
    with db.transaction():
        db.conn.execute(
            f"INSERT INTO workflow_runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [record.get(column) for column in columns],
        )
    return record


def finish_run(
        db: Database,
        project: Project,
        run_id: str,
        *,
        status: str,
        finished_at: str | None = None,
        exit_code: int | None = None,
        error: str | None = None,
        output_sha256: str | None = None,
        execution_details: str | None = None,
        environment_id: str | None = None,
) -> dict[str, Any]:
    """Finalize a previously started run and append its immutable JSONL record."""
    finished_at = finished_at or now_iso()
    with db.transaction():
        db.conn.execute(
            "UPDATE workflow_runs SET status=?, finished_at=?, exit_code=?, error=?, "
            "output_sha256=COALESCE(?, output_sha256), "
            "execution_details=COALESCE(?, execution_details), "
            "environment_id=COALESCE(?, environment_id) WHERE run_id=?",
            (status, finished_at, exit_code, error, output_sha256, execution_details,
             environment_id, run_id),
        )
        row = db.conn.execute(
            "SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    if row is None:
        raise ValueError(f"workflow run {run_id} does not exist")
    record = dict(row)
    flush_run_log(project, [record])
    return record


def run_external_command(
        db: Database,
        project: Project,
        argv: list[str],
        step: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        parameter_set: str | None = None,
        expected_outputs: Iterable[str | Path] | None = None,
        cwd: str | Path | None = None,
        timeout: float | None = None,
        tool: str | None = None,
        tool_version: str | None = None,
        backend: str | None = None,
        threads: int | None = None,
        inputs: Iterable[str | Path] = (),
        extra_details: dict[str, Any] | None = None,
        stage_inputs: Iterable[str | Path] = (),
        executor: Any = None,
) -> dict[str, Any]:
    """Run an external QC/analysis tool deterministically.

    stdout/stderr are preserved as files, exit code is captured, and the run is
    recorded as structured JSON.  Completion is only recorded after all expected
    outputs exist and are non-empty.

    ``inputs`` declares input artifacts: each must exist, is hashed, and the
    sorted ``path:sha256`` lines are combined into the run's ``input_sha256``.
    With SSH and a non-empty remote root, declared inputs are also staged in
    the remote project mirror. ``extra_details`` are merged into the recorded
    ``execution_details``.

    Execution goes through the configured backend (`execution.backend` in
    project.yaml, overridable per call): `local` subprocess, `slurm` job
    submission, or `ssh` remote execution (HPC/cloud host). All backends keep
    the same provenance contract.
    """
    if entity_type and entity_id:
        db.require_not_retired(entity_type, entity_id)
    started = now_iso()
    started_monotonic = time.monotonic()
    logs = project.logs_root
    logs.mkdir(parents=True, exist_ok=True)
    run_id = new_run_id()
    stdout_file = logs / f"{run_id}.stdout.log"
    stderr_file = logs / f"{run_id}.stderr.log"
    record: dict[str, Any] = {
        "run_id": run_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "step": step,
        "command": shlex.join(str(a) for a in argv),
        "parameter_set": parameter_set,
        "started_at": started,
        "tool": tool,
        "tool_version": tool_version,
        "threads": threads,
    }
    base = Path(cwd) if cwd else project.root
    resolved_outputs: list[Path] = []
    for output in expected_outputs or []:
        path = Path(output)
        if not path.is_absolute():
            path = base / path
        resolved_outputs.append(path)
    input_entries: list[dict[str, Any]] = []
    resolved_inputs: list[Path] = []
    for raw_input in inputs:
        path = Path(raw_input)
        if not path.is_absolute():
            path = base / path
        if not path.exists():
            raise ValidationError(f"declared input does not exist: {path}")
        resolved_inputs.append(path)
        input_entries.append({"path": str(path), "sha256": sha256_path(path)})
    if input_entries:
        combined = "\n".join(
            f"{entry['path']}:{entry['sha256']}"
            for entry in sorted(input_entries, key=lambda entry: entry["path"])
        )
        record["input_sha256"] = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    owns_executor = executor is None
    environment: dict[str, Any] | None = None
    try:
        if executor is None:
            from operon.execution import get_executor
            executor = get_executor(project, backend)
        record["executor"] = executor.describe()
        resolved_stage_inputs: list[Path] = []
        for raw_input in stage_inputs:
            path = Path(raw_input)
            if not path.is_absolute():
                path = base / path
            resolved_stage_inputs.append(path)
        if getattr(executor, "name", None) == "ssh" and getattr(
                executor, "remote_root", ""):
            # A non-shared SSH project needs every declared local input in its
            # remote mirror. Preserve explicit staging for analysis callers and
            # de-duplicate paths without changing their user-facing spelling.
            staged: dict[str, Path] = {}
            for path in [*resolved_inputs, *resolved_stage_inputs]:
                staged.setdefault(str(path.resolve(strict=False)), path)
            resolved_stage_inputs = list(staged.values())
        probe = getattr(executor, "probe_environment", None)
        if probe is not None:
            try:
                environment = probe()
            except Exception:
                environment = None  # probe failures must never affect the run
        result = executor.run(
            argv, cwd=cwd, stdout_path=stdout_file, stderr_path=stderr_file,
            timeout=timeout, threads=threads, run_id=run_id,
            stage_inputs=resolved_stage_inputs, expected_outputs=resolved_outputs,
        )
        record.update(exit_code=result.exit_code, status="completed" if result.exit_code == 0 else "failed")
        record["scheduler_job_id"] = result.scheduler_job_id
        # Duck-typed executors may predate the resources field.
        resources = getattr(result, "resources", None)
        if not isinstance(resources, dict):
            resources = {}
        record["max_rss_mb"] = resources.get("max_rss_mb")
        record["avg_rss_mb"] = resources.get("avg_rss_mb")
        record["cpu_seconds"] = resources.get("cpu_seconds")
        details = dict(result.details)
        if input_entries:
            details["inputs"] = input_entries
        if extra_details:
            details.update(extra_details)
        record["execution_details"] = json.dumps(details, ensure_ascii=False, sort_keys=True)
        # Slurm probes the compute side inside the job; prefer that document.
        if result.details.get("environment"):
            environment = result.details["environment"]
        if result.exit_code != 0:
            record["error"] = result.error or f"exit code {result.exit_code}"
            record["status"] = "failed"
        elif result.error:
            record["status"] = "failed"
            record["error"] = result.error
        else:
            for path in resolved_outputs:
                if not path_is_nonempty(path):
                    record["status"] = "failed"
                    record["error"] = f"expected output missing or empty: {path}"
                    break
    except subprocess.TimeoutExpired as exc:
        record.update(status="failed", error=f"timeout after {timeout}s", exit_code=None)
    except OSError as exc:
        record.update(status="failed", error=str(exc), exit_code=None)
    except OperonError as exc:
        record.update(status="failed", error=str(exc), exit_code=None)
    except Exception as exc:
        record.update(
            status="failed", error=f"{type(exc).__name__}: {exc}", exit_code=None,
        )
    finally:
        if owns_executor and executor is not None:
            close = getattr(executor, "close", None)
            if close is not None:
                close()
    record.update(
        finished_at=now_iso(),
        stdout_file=str(stdout_file),
        stderr_file=str(stderr_file),
    )
    for key in ("max_rss_mb", "avg_rss_mb", "cpu_seconds"):
        record.setdefault(key, None)
    if environment:
        with db.transaction():
            record["environment_id"] = db.record_environment(environment)
    duration = time.monotonic() - started_monotonic
    record["duration_seconds"] = round(duration, 3)
    log_run(db, project, record)
    if record.get("status") != "completed":
        raise RuntimeError(f"{step} failed: {record.get('error') or 'unknown error'}")
    return record
