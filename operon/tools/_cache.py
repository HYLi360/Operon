"""Completion-cache lookups and the environment-reuse decision.

Cached and adoptable job queries, the pre-job environment documents, the
recipe ``environment_policy`` check, the stale-``RUNNING`` sweep and the
cooperative cancellation guard."""

from __future__ import annotations

import json
import signal
import threading
from pathlib import Path
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.shutdown import ShutdownRequested
from operon.utils import now_iso, sha256_path

from ._config import Recipe, recipe_environment_policy


def _job_columns() -> list[str]:
    return [
        "analysis_name",
        "entity_type",
        "entity_id",
        "file_id",
        "tool",
        "tool_version",
        "tool_version_raw",
        "launcher",
        "command",
        "parameter_set",
        "parameter_sha256",
        "input_sha256",
        "database_identity",
        "status",
        "output_relative_path",
        "output_sha256",
        "stdout_file",
        "stderr_file",
        "started_at",
        "finished_at",
        "error",
        "workflow_run_id",
        "environment_id",
        "recipe_snapshot_id",
    ]


def find_cached_job(
    db: Database,
    analysis_name: str,
    file_id: str,
    parameter_sha: str,
    input_sha: str,
    database_id: str,
) -> dict[str, Any] | None:
    row = db.conn.execute(
        "SELECT * FROM analysis_jobs WHERE analysis_name=? AND file_id=? AND parameter_sha256=? "
        "AND input_sha256=? AND database_identity=? AND status='completed' "
        "ORDER BY job_id DESC LIMIT 1",
        (analysis_name, file_id, parameter_sha, input_sha, database_id),
    ).fetchone()
    return dict(row) if row else None


def find_adoptable_job(
    db: Database, analysis_name: str, file_id: str
) -> dict[str, Any] | None:
    """Latest completed job for (analysis, file), ignoring the cache fingerprint."""
    row = db.conn.execute(
        "SELECT * FROM analysis_jobs WHERE analysis_name=? AND file_id=? AND status='completed' "
        "ORDER BY job_id DESC LIMIT 1",
        (analysis_name, file_id),
    ).fetchone()
    return dict(row) if row else None


def _cached_environment_document(
    db: Database, environment_id: Any
) -> dict[str, Any] | None:
    if not environment_id:
        return None
    row = db.conn.execute(
        "SELECT document FROM execution_environments WHERE environment_id=?",
        (str(environment_id),),
    ).fetchone()
    if row is None:
        return None
    try:
        document = json.loads(row["document"])
    except (json.JSONDecodeError, TypeError):
        return None
    return document if isinstance(document, dict) else None


def _has_pre_job_probe(executor: Any) -> bool:
    # Slurm (direct or over SSH) probes the compute side inside the submitted
    # job, so no pre-job environment comparison is possible there.
    if executor.name == "slurm":
        return False
    if executor.name == "ssh" and getattr(executor, "scheduler", "none") == "slurm":
        return False
    return True


def _current_environment_document(
    executor: Any, command: list[str], cwd: Path
) -> dict[str, Any] | None:
    """Probe the current execution side; failures degrade to None, never raise."""
    try:
        if executor.name == "local":
            from operon.environment_capture import capture_local

            document = capture_local(command, cwd)
        else:
            probe = getattr(executor, "probe_environment", None)
            document = probe() if probe is not None else None
    except Exception:  # noqa: BLE001 - environment capture degrades to None, never raises  # pylint: disable=broad-exception-caught
        return None
    return document if isinstance(document, dict) and document else None


def _cache_environment_decision(
    db: Database,
    recipe: Recipe,
    executor: Any,
    cached: dict[str, Any],
    command: list[str],
    cwd: Path,
) -> dict[str, Any]:
    """Judge an exact cache hit against the recipe's ``environment_policy``.

    Returns a dict with ``reuse`` (False means treat the hit as a miss and
    recompute), ``details`` for a run record (None when the environments match
    and the reuse stays silent), ``warning`` text for the CLI, and the probed
    current ``environment`` document when one was captured.  Probe and
    comparison failures never raise and never block a reuse.
    """
    from operon.environment import relevance_fingerprint

    policy = recipe_environment_policy(recipe)
    decision: dict[str, Any] = {
        "policy": policy,
        "reuse": True,
        "details": None,
        "warning": None,
        "environment": None,
    }
    if policy == "ignore":
        return decision
    details: dict[str, Any] = {
        "environment_policy": policy,
        "cached_environment_id": cached.get("environment_id"),
    }
    if not _has_pre_job_probe(executor):
        if policy == "strict":
            details["environment_compare"] = "unavailable"
            details["environment_policy_degraded"] = "strict->warn"
            decision["details"] = details
        return decision
    current_document = _current_environment_document(executor, command, cwd)
    current = relevance_fingerprint(current_document) if current_document else None
    cached_document = _cached_environment_document(db, cached.get("environment_id"))
    cached_relevance = (
        relevance_fingerprint(cached_document) if cached_document else None
    )
    if current is None or cached_relevance is None:
        # An incomparable side must not silently defeat strict, nor punish warn.
        details["environment_compare"] = "unavailable"
        if policy == "strict":
            details["environment_policy_degraded"] = "strict->warn"
        decision["details"] = details
        decision["environment"] = current_document
        return decision
    details["cached_relevance_fingerprint"] = cached_relevance
    details["current_relevance_fingerprint"] = current
    decision["environment"] = current_document
    if current == cached_relevance:
        return decision
    details["environment_compare"] = "mismatch"
    if policy == "strict":
        decision["reuse"] = False
        decision["details"] = details
        return decision
    details["environment_warning"] = {
        "message": "cached result was produced under a different execution environment",
        "cached_relevance_fingerprint": cached_relevance,
        "current_relevance_fingerprint": current,
    }
    decision["details"] = details
    decision["warning"] = (
        f"warning: reusing cached {recipe.name} result although the execution environment "
        f"changed (environment_policy=warn; cached {cached_relevance[:12]}..., "
        f"current {current[:12]}...)"
    )
    return decision


def _find_verified_adoptee(
    db: Database, project: Project, analysis_name: str, file_id: str, input_sha: str
) -> tuple[dict[str, Any], Path] | None:
    """Completed job whose recorded output still exists on disk, byte-identical.

    Resume tier 2: the exact cache fingerprint may change across versions or
    recipe edits, but a verified output for the same input content is still a
    valid result and can be adopted instead of recomputed.
    """
    adoptee = find_adoptable_job(db, analysis_name, file_id)
    if adoptee is None or adoptee["input_sha256"] != input_sha:
        return None
    if not adoptee["output_relative_path"] or not adoptee["output_sha256"]:
        return None
    output = project.root / adoptee["output_relative_path"]
    if not output.exists():
        return None
    if sha256_path(output).lower() != str(adoptee["output_sha256"]).lower():
        return None
    return adoptee, output


def _sweep_stale_running_jobs(db: Database, analysis_name: str) -> int:
    """Mark this analysis's jobs left RUNNING by a killed process as interrupted.

    Resume only ever reuses ``completed`` rows, so this is bookkeeping
    hygiene for the crash-only case (e.g. SIGKILL) where the graceful
    shutdown path never got a chance to finalize the row. The sweep is
    scoped to ``analysis_name`` so a concurrent run of a different
    analysis keeps its live rows untouched.
    """
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE analysis_jobs SET status='interrupted', finished_at=?, error=? "
            "WHERE status='RUNNING' AND analysis_name=?",
            (
                now_iso(),
                "swept at startup: previous run terminated abnormally",
                analysis_name,
            ),
        )
        return cursor.rowcount


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    """Cooperative cancellation: a set event raises ``ShutdownRequested`` so
    the existing interrupt bookkeeping (scancel, ``interrupted`` rows,
    partial-output removal) runs exactly as for a signal."""
    if cancel_event is not None and cancel_event.is_set():
        raise ShutdownRequested(signal.SIGINT)
