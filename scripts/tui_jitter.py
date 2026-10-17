#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Opt-in timing-jitter harness for the Textual test suite.

A fast workstation steps over the windows these tests are meant to guard: the
app's own message loop finishes the next turn before the test's predicate looks
again, so a test whose wait names the wrong observable stays green here and goes
red on a loaded CI runner.  Slowing the CPU down uniformly does not reproduce
that: every thread slows by the same factor, so the *interleaving* the flake
needs is preserved.  What does reproduce it is widening the gap between one
participant's turns and another's — the app's deferred mounts, and the worker
threads that hand results back.

Load it explicitly, and tune it with environment variables::

    PYTHONPATH=scripts OPERON_TUI_JITTER_MS=1.5 \
        python -m pytest tests/unit/test_tui_writes.py -p tui_jitter

``scripts/tui-race-stress.sh`` wraps the randomized-order loop the project
requires after TUI changes.

Knobs (all delays are drawn uniformly from ``[0, ms]``, so a run is a *sample*
of interleavings rather than one fixed slowdown):

``OPERON_TUI_JITTER_MS``
    Delay before every message dispatch on every message pump.  This is the
    "the machine is slow, and the app feels it more than the test does" knob.
``OPERON_TUI_PUMP_EVERY``
    Only jitter one dispatch in *N* (default 1).  The cost knob: a Textual test
    dispatches thousands of messages.
``OPERON_TUI_FWD_JITTER_MS``
    Delay before a pump flushes its ``call_after_refresh`` callbacks.  Deferred
    mounts, splash-screen handoffs and ``_mount_after_refresh`` work all live
    here, so this is the knob for "the replacement landed a turn later".
``OPERON_TUI_MOUNT_JITTER_MS``
    Delay before an ``events.Mount`` is dispatched.  *This* is the asymmetric
    knob: a mount is not something the pilot's quiescence waits are registered
    on, so the test's gate can be satisfied while the widget is still mounting —
    the window ODR-29 fell into.  The symmetric knobs above do not open it,
    because a test gate that waits for "the app is idle" simply waits longer.
``OPERON_TUI_WORKER_JITTER_MS``
    Delay inside ``WorkerResults.post_to_ui``, i.e. between a worker thread
    finishing its read and its result reaching the UI thread.  This is the knob
    for "the older read came back after the newer one".
``OPERON_TUI_SEED``
    Seed for the jitter RNG (default 0).  Fix it to make a run reproducible; vary
    it to sample different interleavings.

The harness prints what it injected at the end of the session (``-s`` or a
failure shows it), so a red run can be replayed with the same seed.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from typing import Any

import pytest

pytest.importorskip("textual")

_INJECTED = {"dispatch": 0, "flush": 0, "mount": 0, "worker": 0}
_INJECTED_MS = {"dispatch": 0.0, "flush": 0.0, "mount": 0.0, "worker": 0.0}


def _ms(name: str, default: float = 0.0) -> float:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    try:
        return max(0.0, float(raw))
    except ValueError as error:  # pragma: no cover - bad operator input
        raise pytest.UsageError(f"{name}={raw!r} is not a number") from error


class _Jitter:
    """One RNG per process, so a seed reproduces a whole session."""

    def __init__(self) -> None:
        seed = int(os.environ.get("OPERON_TUI_SEED") or 0)
        self.rng = random.Random(seed)
        self.dispatch_ms = _ms("OPERON_TUI_JITTER_MS")
        self.every = max(1, int(os.environ.get("OPERON_TUI_PUMP_EVERY") or 1))
        self.flush_ms = _ms("OPERON_TUI_FWD_JITTER_MS")
        self.mount_ms = _ms("OPERON_TUI_MOUNT_JITTER_MS")
        self.worker_ms = _ms("OPERON_TUI_WORKER_JITTER_MS")
        self.dispatch_calls = 0

    @property
    def active(self) -> bool:
        return bool(
            self.dispatch_ms or self.flush_ms or self.mount_ms or self.worker_ms
        )

    def draw(self, kind: str, ceiling_ms: float) -> float:
        seconds = self.rng.uniform(0.0, ceiling_ms) / 1000.0
        _INJECTED[kind] += 1
        _INJECTED_MS[kind] += seconds * 1000.0
        return seconds


JITTER = _Jitter()


def pytest_configure(config: pytest.Config) -> None:
    if not JITTER.active:
        return

    from textual import events
    from textual.message_pump import MessagePump

    original_dispatch = MessagePump._dispatch_message
    original_flush = MessagePump._flush_next_callbacks

    async def jittered_dispatch(self: MessagePump, message: Any) -> None:
        JITTER.dispatch_calls += 1
        if JITTER.dispatch_ms and JITTER.dispatch_calls % JITTER.every == 0:
            await asyncio.sleep(JITTER.draw("dispatch", JITTER.dispatch_ms))
        if JITTER.mount_ms and isinstance(message, events.Mount):
            await asyncio.sleep(JITTER.draw("mount", JITTER.mount_ms))
        await original_dispatch(self, message)

    async def jittered_flush(self: MessagePump) -> None:
        await asyncio.sleep(JITTER.draw("flush", JITTER.flush_ms))
        await original_flush(self)

    MessagePump._dispatch_message = jittered_dispatch  # type: ignore[method-assign]
    MessagePump._flush_next_callbacks = jittered_flush  # type: ignore[method-assign]

    if JITTER.worker_ms:
        from operon.tui.screens.common import WorkerResults

        original_post = WorkerResults.post_to_ui

        def jittered_post(self: WorkerResults, callback: Any, *args: Any, **kwargs: Any) -> None:
            # Called from the worker thread, so a plain sleep is the right tool.
            time.sleep(JITTER.draw("worker", JITTER.worker_ms))
            original_post(self, callback, *args, **kwargs)

        WorkerResults.post_to_ui = jittered_post  # type: ignore[method-assign]


def pytest_report_header(config: pytest.Config) -> str:
    if not JITTER.active:
        return "tui jitter: off"
    return (
        f"tui jitter: dispatch<= {JITTER.dispatch_ms}ms every {JITTER.every} dispatch(es), "
        f"flush<= {JITTER.flush_ms}ms, mount<= {JITTER.mount_ms}ms, "
        f"worker<= {JITTER.worker_ms}ms "
        f"(seed {os.environ.get('OPERON_TUI_SEED') or 0})"
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not JITTER.active:
        return
    total = sum(_INJECTED_MS.values())
    print(
        "\n[tui jitter] injected "
        f"{_INJECTED['dispatch']} dispatch delays ({_INJECTED_MS['dispatch']:.1f}ms), "
        f"{_INJECTED['flush']} callback-flush delays ({_INJECTED_MS['flush']:.1f}ms), "
        f"{_INJECTED['mount']} mount delays ({_INJECTED_MS['mount']:.1f}ms), "
        f"{_INJECTED['worker']} worker handoffs ({_INJECTED_MS['worker']:.1f}ms) "
        f"= {total / 1000.0:.1f}s of added latency"
    )
