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
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, Select, Static
from textual.widgets._select import SelectCurrent

from operon.tui.screens.common import (
    FittingSelect,
    WriteModal,
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
    assert (
        project_default_backend(
            SimpleNamespace(config={"execution": {"backend": "  SLURM "}})
        )
        == "slurm"
    )
    assert project_default_backend(SimpleNamespace(config={"execution": {}})) == "local"
    assert project_default_backend(SimpleNamespace()) == "local"  # no config attribute


def test_backend_select_options_mirror_the_cli_flag() -> None:
    """The first option is the project default and carries the empty value."""
    options = backend_select_options(
        SimpleNamespace(config={"execution": {"backend": "ssh"}})
    )
    assert options[0] == ("project default (ssh)", "")
    assert [value for _label, value in options] == ["", "local", "slurm", "ssh"]


def test_selected_backend_tolerates_a_widget_that_is_not_there() -> None:
    """A reader can run before the form mounted (ODR-23's shape)."""

    class ScreenWithoutTheWidget:
        def query_one(self, *_args: Any, **_kwargs: Any) -> Any:
            raise NoMatches("not composed yet")

    assert selected_backend(ScreenWithoutTheWidget(), "analyze-backend") == ""


def test_selected_backend_reads_a_blank_select_as_the_project_default() -> None:
    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            select: Select = Select(
                [("local", "local"), ("slurm", "slurm")],
                id="analyze-backend",
                allow_blank=True,
            )
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
            assert select.options_ready  # the direct path (ODR-38)
            select._options_ready = False
            select._init_options_when_composed(attempt=0)
            await pilot.pause()
            assert select.options_ready, "the retry tick must latch the same way"
            assert select.value == "y"

    _run(scenario())


def test_fitting_select_adopts_its_first_option_without_an_overlay() -> None:
    """A select that may not be blank resolves its value with no overlay.

    `allow_blank=False` is what makes the difference: the default constructor
    accepts a blank, so adoption is a no-op there (ODR-26's other half).
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
                await wait_until(
                    lambda: select.options_gave_up, "the mount retries to run out"
                )
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


@pytest.mark.bug("ODR-50")
def test_fitting_select_paint_gives_up_by_name() -> None:
    """A label that never composes is reported once, not raised every refresh."""
    recorder = _Recorder()

    async def scenario() -> None:
        app = App()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(FittingSelect, "log", property(lambda self: recorder))
            async with app.run_test(size=(80, 24)) as pilot:
                select = FittingSelect([("core", "core")], id="paint-probe")
                select._mount_retry_limit = 2
                await app.screen.mount(select)
                await wait_until(lambda: select.is_mounted, "the control to mount")
                current = select.query_one(SelectCurrent)
                await current.query_one("#label", Static).remove()  # never comes back
                select.value = "core"
                # Every retry rides on a refresh, and nothing else in this
                # headless app moves — pump it the way a live UI would.
                for _ in range(20):
                    await pilot.pause()
                    if recorder.messages:
                        break
                assert recorder.messages, "the give-up path must report itself"
                assert select.is_mounted, "the control outlives the failed paint"
                assert "paint-probe" in recorder.messages[0]
                assert str(select.value) == "core", "the value stands"

    _run(scenario())


@pytest.mark.bug("ODR-66")
def test_tui_constructs_no_bare_selects() -> None:
    """Every Select the TUI builds is a FittingSelect.

    A bare ``Select`` races its own compose under load: Textual's
    ``_on_mount`` reaches for the overlay, and ``_init_selected_option`` /
    ``_watch_value`` for the collapsed control's ``#label``, before those
    children exist (ODR-66).  ``FittingSelect`` guards exactly those lookups,
    so the guard is only worth its name if nothing constructs the unguarded
    widget any more.
    """
    import operon.tui

    offenders: list[str] = []
    for source in Path(operon.tui.__file__).parent.rglob("*.py"):
        if source.name == "common.py":
            continue  # FittingSelect itself wraps Select there
        for lineno, line in enumerate(source.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"(?<![\w.])Select\(", code):
                offenders.append(f"{source.name}:{lineno}")
    assert offenders == []


# -- the write-modal command preview survives a form that is not composed -----


class _ProbeModal(WriteModal):
    """Smallest WriteModal: one input the command text reads."""

    def __init__(self) -> None:
        super().__init__("probe")

    def compose_form(self):
        yield Input("seed", id="probe-input")

    def command_text(self) -> str:
        value = self.query_one("#probe-input", Input).value
        return f"operon probe {value}"

    def confirm(self) -> None:
        self.dismiss(True)


@pytest.mark.bug("ODR-65")
def test_write_modal_preview_defers_until_modal_command_composes() -> None:
    """on_mount's preview refresh must not raise while the form composes.

    The form lives two compose levels below the modal, so a Mount dispatched
    before the grandchildren landed raised NoMatches out of refresh_command
    (ODR-65).  Withhold #modal-command here so every attempt misses: the
    refresh must defer a turn at a time, run its budget out, and leave the
    modal — and the app — alive.
    """

    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            modal = _ProbeModal()
            modal._command_refresh_retry_limit = 3
            original_compose = modal.compose

            def without_command() -> Any:
                for child in original_compose():
                    if not (isinstance(child, Static) and child.id == "modal-command"):
                        yield child

            modal.compose = without_command
            await app.push_screen(modal)
            await wait_until(lambda: modal.is_mounted, "the modal to mount")
            for _ in range(6):  # let the retry budget run out
                await pilot.pause()
            assert modal.is_mounted
            assert app.is_running

    _run(scenario())


@pytest.mark.bug("ODR-65")
def test_write_modal_preview_defers_while_command_text_inputs_compose() -> None:
    """command_text() reads form inputs; those can be missing at Mount too.

    Same window as the sibling test, one lookup earlier: the subclass's
    command_text reached for its input before the form composed and raised
    NoMatches out of on_mount (ODR-65).  The refresh defers instead.
    """

    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            modal = _ProbeModal()
            modal._command_refresh_retry_limit = 3
            modal.compose_form = lambda: iter([])  # no #probe-input anywhere
            await app.push_screen(modal)
            await wait_until(lambda: modal.is_mounted, "the modal to mount")
            for _ in range(6):  # let the retry budget run out
                await pilot.pause()
            assert modal.is_mounted
            assert app.is_running

    _run(scenario())


@pytest.mark.bug("ODR-65")
def test_write_modal_on_form_ready_runs_after_the_command_anchor() -> None:
    """The form-composed hook fires once #modal-command is really mounted.

    Subclass work that reads the form (query_one, set_confirm_enabled, ...)
    belongs in on_form_ready, not on_mount: the base defers the hook until the
    anchor exists.  Querying #modal-command from inside the hook must succeed —
    were it to run a turn early the lookup would raise and the app would fail
    at teardown.
    """
    ran: list[str] = []

    class Probe(_ProbeModal):
        def on_form_ready(self) -> None:
            value = self.query_one("#probe-input", Input).value
            self.query_one("#modal-command", Static)  # the anchor is present
            ran.append(value)

    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(80, 24)):
            modal = Probe()
            await app.push_screen(modal)
            await wait_until(lambda: ran == ["seed"], "on_form_ready to run")
            assert modal.is_mounted

    _run(scenario())


@pytest.mark.bug("ODR-65")
def test_write_modal_on_form_ready_never_fires_without_the_anchor() -> None:
    """A form that never composes must not run the hook, and must not crash.

    Withhold #modal-command so the readiness probe never lands: on_form_ready
    must stay uncalled even though it would raise, and the modal — and the app
    — must survive the retry budget running out.
    """
    called: list[bool] = []

    class Probe(_ProbeModal):
        def on_form_ready(self) -> None:
            called.append(True)
            raise AssertionError("on_form_ready ran before the form composed")

    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            modal = Probe()
            modal._command_refresh_retry_limit = 3
            original_compose = modal.compose

            def without_command() -> Any:
                for child in original_compose():
                    if not (isinstance(child, Static) and child.id == "modal-command"):
                        yield child

            modal.compose = without_command
            await app.push_screen(modal)
            await wait_until(lambda: modal.is_mounted, "the modal to mount")
            for _ in range(6):  # let the retry budget run out
                await pilot.pause()
            assert not called, "on_form_ready must not run without #modal-command"
            assert modal.is_mounted
            assert app.is_running

    _run(scenario())


# -- classification rows route their own messages -----------------------------


@pytest.mark.parametrize(
    "row_class",
    [
        ConditionRow,
        ConditionEditor,
        BestByRow,
        SourceRow,
        ClassificationRuleRow,
    ],
)
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
            assert document == {
                "operator": "like",
                "field": "",
                "value": "x",
                "extra": 1,
            }

    _run(scenario())
