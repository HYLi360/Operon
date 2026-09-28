"""Shared helpers for the headless TUI tests.

Every TUI test module used to carry its own copy of :func:`click` — and four of
them a weaker one — so the traps it works around were only fully handled in the
copies that happened to be fixed last: a deferred editor rebuild moving the
target (ODR-0023), a ``Button`` keeping its 0.2 s ``-active`` press effect and
Textual dropping a ``Pressed`` raised inside that window (ODR-0024), and a
clipped or obscured target that ``Pilot.click`` either reports as ``False`` or
raises ``OutOfBounds`` for.

Import it as ``_click`` so call sites stay as they are::

    from tests.tui_helpers import click as _click
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("textual")

from textual.pilot import OutOfBounds
from textual.widgets import Button

#: How long a helper waits for a UI condition before failing loudly.
SETTLE_TIMEOUT = 30.0


async def wait_until(
    predicate: Callable[[], bool], description: str, timeout: float = SETTLE_TIMEOUT
) -> None:
    """Wait for a real UI condition instead of a fixed number of pause() cycles."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not reach {description} within {timeout}s")
        await asyncio.sleep(0.02)


async def click(pilot: Any, selector: str) -> None:
    """Activate a widget, tolerating a rebuilt layout and a lingering press effect.

    ``Pilot.click`` returns False when the target is clipped or obscured and
    *raises* ``OutOfBounds`` when the target's centre is still outside the screen
    region, which is what a deferred editor rebuild produces (ODR-0023).  A
    ``Button`` also keeps its ``-active`` press effect for about 0.2 s and
    Textual drops a ``Button.Pressed`` raised inside that window, so a rapid
    second click reported ``landed=True`` and did nothing (ODR-0024): wait for
    the effect to clear first.  An enabled button is then pressed directly when
    the positional click cannot land — the same activation a landed click
    produces — and anything else is retried until it lands or the budget runs
    out, so a rebuilt layout fails loudly instead of silently.
    """
    widget = pilot.app.screen.query_one(selector)
    widget.scroll_visible(animate=False)
    await pilot.pause()
    if isinstance(widget, Button) and widget.has_class("-active"):
        await wait_until(lambda: not widget.has_class("-active"),
                         f"{selector} to settle")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_TIMEOUT
    while True:
        try:
            landed = await pilot.click(selector)
        except OutOfBounds:
            landed = False
        if landed:
            return
        if isinstance(widget, Button) and not widget.disabled:
            widget.press()
            await pilot.pause()
            return
        if loop.time() > deadline:
            raise AssertionError(f"click did not land on {selector}")
        await pilot.pause()
        await asyncio.sleep(0.02)
