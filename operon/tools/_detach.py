#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Detached analysis launches: re-run ``analyze`` in an independent session.

A long batch should not have to own the terminal (or the TUI) that started it.
:func:`spawn_detached_analysis` builds the equivalent ``operon analyze`` argv,
relaunches it as its own OS session (``start_new_session``, so it survives the
terminal and the session that spawned it), and streams the child's console
output into a log file under the project logs directory. The caller gets the
handle — PID plus log path — immediately, and progress remains visible through
the per-file ``workflow_runs`` rows the child records (INV-1), e.g.
``operon workflow list --step analysis:<recipe> --status running``.

One detached child per recipe: the spawn claims a liveness marker under
``.operon/analysis-locks/`` before it launches. A marker whose pid is alive
refuses the duplicate; an exited pid (crashed or finished child — the marker
is deliberately not unlinked by the child) is stale and the spawn takes it
over. Concurrent claims serialize on the exclusive claim file, so two
simultaneous spawns cannot both pass the guard.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from operon.config import Project
from operon.errors import ValidationError
from operon.utils import now_iso

_LOG_TEMPLATE = "analyze-detach-{stamp}.log"
_MARKER_DIR_SUFFIX = (".operon", "analysis-locks")

#: A claim whose pid was never published (crash between claim and launch) is
#: stale once older than this; inside the window it reads as "another operon
#: session is launching right now" and refuses, so two simultaneous spawns
#: cannot both race past the guard.
_CLAIM_GRACE_SECONDS = 10.0


def detached_analysis_marker_path(project: Project, analysis: str) -> Path:
    """Marker file path keying one analysis name (collision-safe, no paths)."""
    digest = hashlib.sha256(analysis.encode("utf-8")).hexdigest()[:16]
    return project.root.joinpath(*_MARKER_DIR_SUFFIX, f"{digest}.json")


def _marker_message(analysis: str, marker: dict[str, Any] | None) -> str:
    pid = str(marker.get("pid")) if marker and marker.get("pid") else "?"
    started_at = str(marker.get("started_at", "unknown time")) if marker else "?"
    return (
        f"a detached analysis for {analysis!r} is already running "
        f"(pid {pid}, started {started_at}); stop: kill {pid} — progress: "
        f"operon workflow list --step analysis:{analysis}"
    )


def _load_marker(path: Path) -> dict[str, Any] | None:
    """Parse a marker file, or ``None`` when it is unreadable/garbage."""
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return marker if isinstance(marker, dict) else None


def _refuse_live_marker(path: Path, analysis: str) -> None:
    """Remove stale markers; refuse the spawn while a live pid holds one.

    A marker is live exactly when its pid can still be signalled by this
    session (``os.kill(pid, 0)``): ``ProcessLookupError`` means the child is
    gone (crashed or finished — the marker is deliberately left behind) and
    the claim is cleared for the next spawn; ``PermissionError`` and any
    other inability to conclude count as alive. A pid-less claim (the crash
    window between claim and launch) is live inside
    :data:`_CLAIM_GRACE_SECONDS` of its start — another session may be
    publishing its child right now — and stale after that.
    """
    marker = _load_marker(path)
    if marker is not None:
        pid = marker.get("pid")
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise ValidationError(_marker_message(analysis, marker)) from None
    if _claim_recent(marker or {}):
        raise ValidationError(
            _marker_message(analysis, {**(marker or {}), "pid": "publishing"})
        ) from None
    path.unlink(missing_ok=True)


def _claim_recent(marker: dict[str, Any]) -> bool:
    """Whether a pid-less claim was written within the grace window."""
    if not marker or marker.get("pid") is not None:
        return False
    started_at = marker.get("started_at")
    if not isinstance(started_at, str) or not started_at:
        return False
    try:
        claimed_at = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.astimezone(timezone.utc)
    return datetime.now(timezone.utc) - claimed_at < timedelta(
        seconds=_CLAIM_GRACE_SECONDS
    )


def _write_marker(path: Path, marker: dict[str, Any]) -> None:
    """Publish a marker atomically (claim staging and pid publication)."""
    handle_fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(marker, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def analysis_tokens(
    project: Project,
    *,
    analysis: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int | None = None,
    threads: int | None = None,
    backend: str | None = None,
    force: bool = False,
    keep_partial: bool = False,
    events: str | None = None,
    parameters: dict[str, str] | None = None,
) -> list[str]:
    """Build the ``operon analyze`` argument tokens a detached child runs.

    ``--project`` is always included as an absolute path, so the child resolves
    the same project regardless of its working directory. ``--detach`` itself is
    never part of the output: the child is the real, foreground analysis
    process.
    """
    if not analysis or not analysis.strip():
        raise ValidationError("detached analyze requires --analysis")
    tokens = ["--project", os.path.abspath(project.root), "analyze"]
    tokens += ["--analysis", analysis]
    for name, value in (parameters or {}).items():
        tokens += ["--param", f"{name}={value}"]
    if entity_type:
        tokens += ["--entity-type", entity_type]
    if entity_id:
        tokens += ["--entity-id", entity_id]
    if backend:
        tokens += ["--backend", backend]
    if threads is not None:
        tokens += ["--threads", str(int(threads))]
    if limit is not None:
        tokens += ["--limit", str(int(limit))]
    if force:
        tokens.append("--force")
    if keep_partial:
        tokens.append("--keep-partial")
    if events:
        tokens += ["--events", events]
    return tokens


def spawn_detached_analysis(
    project: Project,
    *,
    analysis: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int | None = None,
    threads: int | None = None,
    backend: str | None = None,
    force: bool = False,
    keep_partial: bool = False,
    events: str | None = None,
    parameters: dict[str, str] | None = None,
) -> tuple[int, Path]:
    """Relaunch ``operon analyze`` as an independent session; return handle.

    The child inherits the caller's environment (launchers, secrets, SSH keys)
    but nothing of its controlling terminal: stdin is ``/dev/null``, both output
    streams go to ``<logs_root>/analyze-detach-<timestamp>.log`` after a header
    recording the full command, and its own process group is detached. Returns
    ``(child_pid, log_path)``; any launch failure propagates.

    Duplicate guard: before launching, the liveness marker for this analysis
    (``.operon/analysis-locks/``) is consulted — a live pid refuses with
    ``ValidationError`` (the child still running), stale markers are claimed
    anew, and the claim itself is written exclusively so concurrent spawns
    serialize (ODR-85).
    """
    tokens = analysis_tokens(
        project,
        analysis=analysis,
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
        threads=threads,
        backend=backend,
        force=force,
        keep_partial=keep_partial,
        events=events,
        parameters=parameters,
    )
    argv = [sys.executable, "-m", "operon", *tokens]
    logs_root = Path(project.logs_root)
    logs_root.mkdir(parents=True, exist_ok=True)
    marker_path = detached_analysis_marker_path(project, analysis)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    _refuse_live_marker(marker_path, analysis)
    claimed = False
    try:
        try:
            # Exclusive claim: a concurrent duplicate loses here, and a claim
            # that dies before publishing its pid reads as pid-less — stale
            # on the next attempt.
            with open(marker_path, "x", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "analysis": analysis,
                            "pid": None,
                            "started_at": now_iso(),
                            "argv": shlex.join(argv),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
            claimed = True
        except FileExistsError as exc:
            raise ValidationError(
                _marker_message(analysis, _load_marker(marker_path))
            ) from exc
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = logs_root / _LOG_TEMPLATE.format(stamp=stamp)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(
                f"# {now_iso()} operon analyze (detached): {shlex.join(argv)}\n"
            )
            handle.flush()
            process = subprocess.Popen(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit.dangerous-subprocess-use-audit # argv[0] is sys.executable running operon itself; no shell
                argv,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=handle,
                start_new_session=True,
            )
    except BaseException:
        if claimed:
            marker_path.unlink(missing_ok=True)
        raise
    _write_marker(
        marker_path,
        {
            "analysis": analysis,
            "pid": process.pid,
            "started_at": now_iso(),
            "argv": shlex.join(argv),
        },
    )
    return process.pid, log_path
