"""Deterministic workflow state machine and machine-readable run logs.

Completion markers are only written after expected outputs exist and validate,
not guessed from a folder or screen log.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from operon import __version__
from operon.config import Project
from operon.database import Database
from operon.errors import ConflictError
from operon.utils import append_jsonl, now_iso, path_is_nonempty

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


def log_run(db: Database, project: Project, record: dict[str, Any]) -> None:
    """Append a machine-readable workflow record to logs/workflow.jsonl and SQLite."""
    record = dict(record)
    record.setdefault("run_id", new_run_id())
    record.setdefault("status", "completed")
    record.setdefault("started_at", now_iso())
    record.setdefault("finished_at", now_iso())
    record.setdefault("tool_version", __version__)
    append_jsonl(project.logs_root / "workflow.jsonl", record)
    columns = [
        "run_id", "parent_run_id", "entity_type", "entity_id", "step", "status",
        "started_at", "finished_at", "exit_code", "command", "tool", "tool_version",
        "parameter_set", "input_sha256", "output_sha256", "threads", "max_rss_mb",
        "log_file", "stdout_file", "stderr_file", "error",
    ]
    with db.transaction():
        db.conn.execute(
            f"INSERT OR REPLACE INTO workflow_runs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [record.get(c) for c in columns],
        )


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
) -> dict[str, Any]:
    """Run an external QC/analysis tool deterministically.

    stdout/stderr are preserved as files, exit code is captured, and the run is
    recorded as structured JSON.  Completion is only recorded after all expected
    outputs exist and are non-empty.
    """
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
        "command": " ".join(str(a) for a in argv),
        "parameter_set": parameter_set,
        "started_at": started,
        "tool": tool,
        "tool_version": tool_version,
    }
    try:
        with open(stdout_file, "wb") as out, open(stderr_file, "wb") as err:
            completed = subprocess.run(argv, cwd=str(cwd) if cwd else None, stdout=out, stderr=err, timeout=timeout)
        record.update(exit_code=completed.returncode, status="completed" if completed.returncode == 0 else "failed")
        if completed.returncode != 0:
            record["error"] = f"exit code {completed.returncode}"
            record["status"] = "failed"
        base = Path(cwd) if cwd else project.root
        for output in expected_outputs or []:
            path = Path(output)
            if not path.is_absolute():
                path = base / path
            if not path_is_nonempty(path):
                record["status"] = "failed"
                record["error"] = f"expected output missing or empty: {path}"
                break
    except subprocess.TimeoutExpired as exc:
        record.update(status="failed", error=f"timeout after {timeout}s", exit_code=None)
    except OSError as exc:
        record.update(status="failed", error=str(exc), exit_code=None)
    record.update(
        finished_at=now_iso(),
        stdout_file=str(stdout_file),
        stderr_file=str(stderr_file),
        max_rss_mb=None,
    )
    duration = time.monotonic() - started_monotonic
    record["duration_seconds"] = round(duration, 3)
    log_run(db, project, record)
    if record.get("status") != "completed":
        raise RuntimeError(f"{step} failed: {record.get('error') or 'unknown error'}")
    return record
