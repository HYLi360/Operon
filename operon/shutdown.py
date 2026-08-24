"""Graceful-shutdown signal handling for long-running batch commands.

Analysis runs can take hours.  This module converts SIGINT/SIGTERM into a
``ShutdownRequested`` exception raised in the main thread, so the normal
exception/finally machinery can stop the current step, terminate its
subprocesses (local process group, Slurm job, or remote SSH payload) and
finalize bookkeeping (``analysis_jobs`` status, partial output removal)
before the process exits.

A second signal received while shutdown cleanup is still in progress forces
an immediate exit, so a stuck cleanup path can never trap the user.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from contextlib import contextmanager
from typing import Iterator


class ShutdownRequested(KeyboardInterrupt):
    """Raised in the main thread when a shutdown signal (SIGINT/SIGTERM) arrives."""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(signal.Signals(signum).name)


_cleanup_in_progress = False


def _force_exit(signum: int) -> None:
    """Immediate exit for a second signal during cleanup (never returns)."""
    os._exit(128 + signum)


def _handler(signum: int, frame: object) -> None:
    global _cleanup_in_progress
    if _cleanup_in_progress:
        # Second signal during cleanup: force an immediate exit.
        _force_exit(signum)
    _cleanup_in_progress = True
    name = signal.Signals(signum).name
    print(f"\nreceived {name}: shutting down gracefully "
          "(send the signal again to force immediate exit)", file=sys.stderr)
    raise ShutdownRequested(signum)


@contextmanager
def graceful_shutdown() -> Iterator[None]:
    """Install SIGINT/SIGTERM handlers that raise :class:`ShutdownRequested`.

    Restores the previous handlers on exit.  Outside the main thread this is
    a no-op, because Python signal handlers are process-global and can only
    be registered from the main thread.
    """
    global _cleanup_in_progress
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    _cleanup_in_progress = False
    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
        _cleanup_in_progress = False
