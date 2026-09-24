"""Guarded button dispatch in the TUI dialogs (ODR-0047).

Textual dispatches a ``Button.Pressed`` message to *every* class in the MRO that
defines ``on_button_pressed``, so a subclass handler that also delegates with
``super().on_button_pressed(event)`` makes the base ``WriteModal`` handler run a
second time — and with it ``confirm()`` — for a single click.  A dialog whose
action is not idempotent then writes, adopts or executes twice (adopt, fan-out,
run-external, taxonomy import, reference-set compilation, table import, add
record, analysis run).

The guard is one ``event.prevent_default()`` on the path that delegates: it
stops the base class's own copy of the dispatch without affecting the explicit
``super()`` call, exactly as the ODR-0043 cancel branches already do.  This
module states that contract once for the whole ``operon/tui/screens`` tree, so
a new dialog cannot reintroduce the double dispatch unnoticed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCREENS = Path(__file__).resolve().parents[2] / "operon" / "tui" / "screens"
SUPER_CALL = "super().on_button_pressed(event)"
HANDLER = "def on_button_pressed"
GUARD = "event.prevent_default()"


def _handler_bodies(path: Path) -> list[tuple[int, list[str]]]:
    """Every ``on_button_pressed`` body in one module, with the line it starts on."""
    lines = path.read_text(encoding="utf-8").splitlines()
    bodies: list[tuple[int, list[str]]] = []
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(HANDLER):
            continue
        indented = (line[: len(line) - len(stripped)] + " ", "\t")
        body = []
        for follow in lines[index + 1:]:
            if follow.strip() and not follow.startswith(indented):
                break
            body.append(follow)
        bodies.append((index + 1, body))
    return bodies


def _unguarded_delegations() -> list[str]:
    problems: list[str] = []
    for path in sorted(SCREENS.glob("*.py")):
        for line_number, body in _handler_bodies(path):
            if not any(SUPER_CALL in line for line in body):
                continue
            target = next(index for index, line in enumerate(body) if SUPER_CALL in line)
            previous = ""
            for earlier in reversed(body[:target]):
                if earlier.strip() and not earlier.strip().startswith("#"):
                    previous = earlier.strip()
                    break
            if previous != GUARD:
                problems.append(
                    f"{path.name}:{line_number} delegates to {SUPER_CALL} without "
                    f"{GUARD} above it"
                )
    return problems


@pytest.mark.bug("ODR-0047")
def test_every_delegating_button_handler_guards_the_mro_dispatch() -> None:
    """A handler that calls super() must stop the base class's own dispatch first."""
    problems = _unguarded_delegations()
    assert problems == [], (
        "unguarded super().on_button_pressed(event): one Confirm click would run "
        "the action twice (ODR-0047):\n  " + "\n  ".join(problems)
    )
