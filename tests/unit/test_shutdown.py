"""Graceful-shutdown signal handling tests."""

from __future__ import annotations

import os
import signal
import threading

import pytest

import operon.shutdown as shutdown
from operon.shutdown import ShutdownRequested, graceful_shutdown


def test_sigterm_raises_shutdown_requested():
    with pytest.raises(ShutdownRequested) as caught:
        with graceful_shutdown():
            os.kill(os.getpid(), signal.SIGTERM)
    assert caught.value.signum == signal.SIGTERM


def test_sigint_raises_shutdown_requested_as_keyboard_interrupt():
    # ShutdownRequested subclasses KeyboardInterrupt, so generic
    # `except KeyboardInterrupt` cleanup paths also catch signal shutdowns.
    with pytest.raises(KeyboardInterrupt):
        with graceful_shutdown():
            os.kill(os.getpid(), signal.SIGINT)


def test_handlers_are_restored_after_context():
    before_int = signal.getsignal(signal.SIGINT)
    before_term = signal.getsignal(signal.SIGTERM)
    with graceful_shutdown():
        assert signal.getsignal(signal.SIGINT) is shutdown._handler
        assert signal.getsignal(signal.SIGTERM) is shutdown._handler
    assert signal.getsignal(signal.SIGINT) == before_int
    assert signal.getsignal(signal.SIGTERM) == before_term


def test_second_signal_forces_immediate_exit(monkeypatch):
    forced: list[int] = []
    monkeypatch.setattr(shutdown, "_force_exit", forced.append)
    with graceful_shutdown():
        with pytest.raises(ShutdownRequested):
            shutdown._handler(signal.SIGTERM, None)
        # The test double returns instead of exiting; the real _force_exit
        # (os._exit) would never reach the raise below.
        with pytest.raises(ShutdownRequested):
            shutdown._handler(signal.SIGINT, None)
    assert forced == [signal.SIGINT]


def test_noop_outside_main_thread():
    errors: list[BaseException] = []

    def worker():
        try:
            with graceful_shutdown():
                assert signal.getsignal(signal.SIGTERM) is not shutdown._handler
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert not errors
