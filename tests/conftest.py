"""Suite-wide pytest configuration for Operon's tests.

Fixtures only one module needs stay next to their tests (``tests/helpers.py``,
``tests/tui_helpers.py``, the module's own ``project``); the premises the whole
suite depends on are installed here, once.
"""

from __future__ import annotations

import pytest

#: How long a raised notification stays readable inside a test.  Textual's
#: default is five seconds, which is a UI lifetime, not a test one.
NOTIFICATION_LIFETIME_SECONDS = 10_000.0


@pytest.fixture(autouse=True)
def durable_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every notification an app raises readable for the whole test.

    Textual stamps a notification with a five-second lifetime when ``notify()``
    is called — before the message is dispatched — and ``Notifications._reap``
    deletes an expired entry on every iteration of ``app._notifications``.  A
    test that waits for the text ("saved <name> version <n>", say) therefore
    times out on a loaded runner although the app raised it *and* the write
    succeeded: the toast expired while the app's own turns were still busy
    (ODR-53).  The tests assert the text and severity the app reported, so the
    lifetime is widened for the whole suite rather than teaching every reader
    to keep its own record.
    """

    try:
        from textual.app import App
    except ImportError:  # the optional ``tui`` extra is not installed
        return
    monkeypatch.setattr(App, "NOTIFICATION_TIMEOUT", NOTIFICATION_LIFETIME_SECONDS)
