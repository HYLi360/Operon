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
``operon inspect workflow list --step analysis:<recipe> --status running``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from operon.config import Project
from operon.errors import ValidationError
from operon.utils import now_iso

_LOG_TEMPLATE = "analyze-detach-{stamp}.log"


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
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = logs_root / _LOG_TEMPLATE.format(stamp=stamp)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"# {now_iso()} operon analyze (detached): {shlex.join(argv)}\n")
        handle.flush()
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    return process.pid, log_path
