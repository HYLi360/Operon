"""Shared TUI helpers, the FittingSelect mount guard, and message routing.

The M5 close-out recorded this area as quality debt: the small shared helpers
in ``operon/tui/screens/common.py`` and the guard paths of
``FittingSelect`` — the retry tick that finds its overlay, the value adopted
while the overlay is missing, the overlay fit that runs too early — had no
test of their own, so a regression would only have shown up as a flakier
suite.  The classification rows' ``RemoveRequested.control`` getters, which
Textual uses to route a message back to its row, were likewise unexercised.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Select, Static

from operon.tui.screens.common import (
    FittingSelect,
    backend_select_options,
    project_default_backend,
    remount,
    selected_backend,
)
from operon.tui.screens.config_classification import (
    BestByRow,
    ClassificationRuleRow,
    ConditionEditor,
    ConditionRow,
    SourceRow,
)
from tests.tui_helpers import wait_until

SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 30.0


def _run(coroutine: Any) -> None:
    """Drive a Textual headless scenario without requiring pytest-asyncio."""
    asyncio.run(asyncio.wait_for(coroutine, timeout=SCENARIO_TIMEOUT))


# -- the shared helpers -------------------------------------------------------


def test_project_default_backend_reads_the_project_configuration() -> None:
    """`--backend` omitted means `execution.backend`, normalised."""
    assert project_default_backend(SimpleNamespace(config={})) == "local"
    assert project_default_backend(SimpleNamespace(config={})) == "local"
    assert project_default_backend(
        SimpleNamespace(config={"execution": {"backend": "  SLURM "}})) == "slurm"
    assert project_default_backend(SimpleNamespace(config={"execution": {}})) == "local"
    assert project_default_backend(SimpleNamespace()) == "local"  # no config attribute


def test_backend_select_options_mirror_the_cli_flag() -> None:
    """The first option is the project default and carries the empty value."""
    options = backend_select_options(SimpleNamespace(config={"execution": {"backend": "ssh"}}))
    assert options[0] == ("project default (ssh)", "")
    assert [value for _label, value in options] == ["", "local", "slurm", "ssh"]


def test_selected_backend_tolerates_a_widget_that_is_not_there() -> None:
    """A reader can run before the form mounted (ODR-0023's shape)."""
    class ScreenWithoutTheWidget:
        def query_one(self, *_args: Any, **_kwargs: Any) -> Any:
            raise NoMatches("not composed yet")

    assert selected_backend(ScreenWithoutTheWidget(), "analyze-backend") == ""


def test_selected_backend_reads_a_blank_select_as_the_project_default() -> None:
    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            select: Select = Select([("local", "local"), ("slurm", "slurm")],
                                    id="analyze-backend", allow_blank=True)
            await app.screen.mount(select)
            await pilot.pause()
            assert selected_backend(app.screen, "analyze-backend") == ""
            select.value = "slurm"
            await pilot.pause()
            assert selected_backend(app.screen, "analyze-backend") == "slurm"

    _run(scenario())


def test_remount_replaces_and_empties_a_plain_container() -> None:
    """A container that is not `MountTracked` still gets the deferred swap."""
    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            container = Vertical(id="plain-container")
            await app.screen.mount(container)
            remount(container, Static("one", id="row-one"))
            await pilot.pause()
            assert container.query("#row-one")
            remount(container, Static("two", id="row-two"))
            await pilot.pause()
            assert container.query("#row-two")
            assert not container.query("#row-one"), "the replaced row is gone"
            remount(container)
            await pilot.pause()
            assert not container.query("#row-two"), "an emptied remount mounts nothing"

    _run(scenario())


# -- the FittingSelect mount guard -------------------------------------------


def test_fitting_select_fits_an_overlay_that_is_not_there_yet() -> None:
    """Expanding before the overlay exists must be a no-op, not a crash."""
    select = FittingSelect([("short", "x"), ("a much longer label", "y")])
    select._fit_overlay()  # no overlay in an unmounted select's tree
    assert not select.options_ready


def test_fitting_select_retry_tick_marks_a_healthy_control_ready() -> None:
    """The retry's landing path: the overlay is there, so readiness latches."""
    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            select = FittingSelect([("short", "x"), ("longer", "y")], value="y")
            await app.screen.mount(select)
            await pilot.pause()
            assert select.options_ready  # the direct path (ODR-0038)
            select._options_ready = False
            select._init_options_when_composed(attempt=0)
            await pilot.pause()
            assert select.options_ready, "the retry tick must latch the same way"
            assert select.value == "y"

    _run(scenario())


def test_fitting_select_adopts_its_first_option_without_an_overlay() -> None:
    """A select that may not be blank resolves its value with no overlay.

    `allow_blank=False` is what makes the difference: the default constructor
    accepts a blank, so adoption is a no-op there (ODR-0026's other half).
    """
    select = FittingSelect([("short", "x"), ("longer", "y")], allow_blank=False)
    select._value = Select.NULL
    select._adopt_value_before_overlay()
    assert select.value == "x"

    blank_ok = FittingSelect([("short", "x")], allow_blank=True)
    blank_ok._value = Select.NULL
    blank_ok._adopt_value_before_overlay()
    assert blank_ok.value is Select.NULL, "a blank stays blank when allowed"


def test_fitting_select_gives_up_by_name_and_reports_it() -> None:
    """Out of retries the control says so — and names itself while doing it."""
    recorder = _Recorder()

    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(80, 24)):
            select = FittingSelect([("short", "x")], id="debt-probe")
            select._mount_retry_limit = 2
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(FittingSelect, "_overlay_present", lambda self: False)
                patch.setattr(FittingSelect, "log", property(lambda self: recorder))
                await app.screen.mount(select)
                await wait_until(lambda: select.options_gave_up,
                                 "the mount retries to run out")
            assert not select.options_ready
            assert recorder.messages, "the give-up path must report itself"
            assert "debt-probe" in recorder.messages[0]

    _run(scenario())


class _Recorder:
    """Stands in for a widget's logger so the give-up warning is observable."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.messages.append(message)


# -- classification rows route their own messages -----------------------------


@pytest.mark.parametrize("row_class", [
    ConditionRow, ConditionEditor, BestByRow, SourceRow, ClassificationRuleRow,
])
def test_classification_rows_route_removal_to_themselves(row_class: type) -> None:
    """Textual routes a message through `control`, so it must be the row itself."""
    sentinel = object()
    message = row_class.RemoveRequested(sentinel)
    assert message.control is sentinel


# -- the condition composer keeps its canonical shape -------------------------


def test_condition_document_adds_a_missing_field_first() -> None:
    """A hand-written condition without `field` gets the canonical key order."""
    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(120, 40)) as pilot:
            row = ConditionRow({"operator": "like", "value": "x", "extra": 1})
            await app.screen.mount(row)
            await pilot.pause()
            document = row.condition_document()
            # The original had no `field`, so it is appended after the keys the
            # original did carry — still before the operand and the extras.
            assert document == {"operator": "like", "field": "", "value": "x", "extra": 1}

    _run(scenario())
