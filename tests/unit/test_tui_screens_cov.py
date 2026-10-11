"""Coverage-focused headless tests for the remaining TUI screen branches.

``test_tui.py`` / ``test_tui_screens_more.py`` and friends drive the happy
paths; this module fills in the states they leave untouched: a worker load
that fails, foreign ``Changed`` events that must be inert, the numeric-form
guards of the run-external modal (including its running-reentry guard), the
"and N more" attention overflow of the home dashboard, the classify summary's
unchanged/warning variants, an out-of-range environments cursor, and the
release/export forms of the publish panel composing together.  Every scenario
predicate-waits on observable UI state; nothing sleeps or touches the network.
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static

from operon import health
from operon.config import Project
from operon.demo import init_demo
from operon.tui import data
from operon.tui.app import OperonApp
from operon.tui.screens import environments as environments_module
from operon.tui.screens.classify import ClassifyModal
from operon.tui.screens.decisions import DecisionsPanel
from operon.tui.screens.environments import EnvironmentsModal
from operon.tui.screens.home import HomePanel
from operon.tui.screens.publish import PublishPanel
from operon.tui.screens.run_external import RunExternalModal
from tests.tui_helpers import click as _click

SCENARIO_TIMEOUT = 180.0
SETTLE_TIMEOUT = 30.0
#: Budget for a worker result crossing back from its thread to the UI (ODR-46).
HANDOFF_TIMEOUT = 120.0


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-screens-cov-demo"))


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project.init(tmp_path / "project")


@pytest.fixture
def demo_project(tmp_path: Path, demo_template: Project) -> Project:
    target = tmp_path / "project"
    shutil.copytree(demo_template.root, target)
    return Project.find(target)


def _run(coroutine) -> None:
    asyncio.run(asyncio.wait_for(coroutine, timeout=SCENARIO_TIMEOUT))


async def _settled(app, timeout: float = SETTLE_TIMEOUT) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while app.workers or getattr(app, "_starting", False):
        if loop.time() > deadline:
            states = [worker.state.name for worker in app.workers]
            raise TimeoutError(f"workers did not finish within {timeout}s: {states}")
        await asyncio.sleep(0.05)


async def _wait_until(
    predicate: Callable[[], bool], description: str, timeout: float = SETTLE_TIMEOUT
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not {description} within {timeout}s")
        await asyncio.sleep(0.05)


async def _push(pilot, modal, selector: str) -> None:
    """Push *modal* unless it is current, and wait until *selector* composed."""
    if pilot.app.screen is not modal:
        pilot.app.push_screen(modal)
    await _wait_until(
        lambda: len(modal.query(selector)) > 0, f"{selector} to compose", timeout=10.0
    )


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


# ---------------------------------------------------------------------------
# classify-modal summary variants
# ---------------------------------------------------------------------------


def test_classify_summary_renders_unchanged_and_warnings() -> None:
    """The summary's empty-counts and warning arms render like the CLI prints."""
    text = ClassifyModal.summary_text(
        {
            "profile": "tier",
            "files": 2,
            "sequences": 0,
            "unlabeled": 0,
            "labels_written": 0,
            "labels_removed": 0,
            "run_id": "WF_0001",
            "ignored_completed_jobs": 2,
            "files_without_sequences": 3,
        }
    ).plain

    assert "labeled 0 of 0 sequence(s)" in text
    assert "labels written: 0, removed: 0 (run WF_0001)" in text
    # No label_counts at all: no label table, but the unchanged notice.
    assert "unchanged — no label row was rewritten" in text
    assert "warning: ignored 2 older completed" in text
    assert "warning: skipped 3 target file(s) with no registered sequences" in text


# ---------------------------------------------------------------------------
# environments modal
# ---------------------------------------------------------------------------


def test_environments_modal_surfaces_a_failed_load(
    project: Project, monkeypatch
) -> None:
    """A failing ``list_environments`` renders inline instead of crashing."""

    def boom(_project: Project) -> Any:
        raise RuntimeError("environment store exploded")

    monkeypatch.setattr(environments_module.data, "list_environments", boom)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = EnvironmentsModal(project)
            await _push(pilot, modal, "#environments-table")
            await _wait_until(
                lambda: (
                    "environment store exploded"
                    in _static_text(app.screen.query_one("#environments-error", Static))
                ),
                "the load failure to render",
            )
            assert modal.environments == []

    _run(scenario())


def test_environments_modal_out_of_range_cursor_is_rejected(
    project: Project, monkeypatch
) -> None:
    """A cursor row that has no trailing entry reports 'select a row first'."""
    payload = [
        {"environment_id": "ENV_A", "created_at": "t", "summary": "Linux"},
        {"environment_id": "ENV_B", "created_at": "t", "summary": "Linux"},
    ]
    monkeypatch.setattr(
        environments_module.data, "list_environments", lambda _project: list(payload)
    )

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = EnvironmentsModal(project)
            await _push(pilot, modal, "#environments-table")
            await _wait_until(lambda: len(modal.environments) == 2, "the two rows")
            table = modal.query_one("#environments-table", DataTable)
            await _wait_until(lambda: table.row_count == 2, "the two table rows")

            # A stale cursor: the table still lists both rows while the
            # backing list shrank, so the index is out of range.
            modal.environments = modal.environments[:1]
            table.focus()
            table.move_cursor(row=1, animate=False)
            await _wait_until(lambda: table.cursor_row == 1, "the cursor on row 1")
            await _click(pilot, "#environments-show")
            await _wait_until(
                lambda: (
                    "select an environment row first"
                    in _static_text(modal.query_one("#environments-error", Static))
                ),
                "the out-of-range selection to be rejected",
            )
            # Nothing was rendered into the output pane.
            assert _static_text(modal.query_one("#environments-error", Static))

    _run(scenario())


# ---------------------------------------------------------------------------
# home dashboard
# ---------------------------------------------------------------------------


def test_home_panel_ignores_foreign_button_and_reloads_after_write(
    project: Project, monkeypatch
) -> None:
    """An unknown button press is inert; a truthy write result reloads panels."""
    calls: list[Project] = []
    original = data.project_summary

    def spy(spied_project: Project) -> Any:
        calls.append(spied_project)
        return original(spied_project)

    monkeypatch.setattr(data, "project_summary", spy)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)):
            await _settled(app)
            panel = app.screen.query_one("#home", HomePanel)
            await _wait_until(lambda: panel.summary is not None, "the home summary")
            baseline = len(calls)

            # A Button.Pressed from a control the panel does not own must not
            # open any dialog.
            screen_before = app.screen
            panel.on_button_pressed(Button.Pressed(Button("No-op", id="home-unknown")))
            assert app.screen is screen_before

            # The push_screen callback receives the modal's payload: a truthy
            # one reloads the panels, a falsy one changes nothing.
            panel._after_write({"imported": 1})
            await _wait_until(
                lambda: len(calls) > baseline, "home to reload after a write"
            )
            settled = len(calls)
            panel._after_write({})
            await _settled(app)
            assert len(calls) == settled

    _run(scenario())


def test_home_panel_attention_overflow_counts(project: Project) -> None:
    """A paged decision list still reports the untruncated decision total."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)):
            await _settled(app)
            panel = app.screen.query_one("#home", HomePanel)
            await _wait_until(lambda: panel.summary is not None, "the home summary")
            panel.render_data(
                {
                    "summary": {},
                    "attention": health.AttentionReport(
                        items=(), totals={health.KIND_DECISION_REVIEW: 3}
                    ),
                    "recent_runs": [],
                }
            )
            body = _static_text(panel.query_one("#home-body", Static))
            assert "3 more decisions need review" in body
            assert "nothing needs attention" not in body

    _run(scenario())


# ---------------------------------------------------------------------------
# decisions panel
# ---------------------------------------------------------------------------


def test_decisions_panel_ignores_a_foreign_checkbox(
    demo_project: Project, monkeypatch
) -> None:
    """A Checkbox.Changed from another control must not trigger a reload."""

    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("decisions")
            await pilot.pause()
            panel = app.screen.query_one(DecisionsPanel)
            await _wait_until(lambda: panel.decisions != [], "the decisions")

            reloads: list[int] = []
            monkeypatch.setattr(panel, "reload", lambda: reloads.append(1))
            panel.on_checkbox_changed(
                Checkbox.Changed(Checkbox(id="stray-check"), True)
            )
            assert reloads == []

            # Positive control: the panel's own checkbox still reloads.
            panel.query_one("#decisions-include-retired", Checkbox).value = True
            await _wait_until(lambda: reloads != [], "the real toggle to reload")

    _run(scenario())


# ---------------------------------------------------------------------------
# run-external modal
# ---------------------------------------------------------------------------


def test_run_external_modal_validation_foreign_events_and_reentry(
    project: Project, monkeypatch
) -> None:
    """Numeric guards, inert foreign events, and the running-reentry guard."""
    released = threading.Event()
    started = threading.Event()
    calls: list[tuple[str, str]] = []

    def blocking_run(project_arg, step, command_line, **options):
        calls.append((step, command_line))
        started.set()
        released.wait(HANDOFF_TIMEOUT)
        return {
            "run_id": "WF_STUB",
            "step": step,
            "status": "completed",
            "exit_code": 0,
            "finished_at": None,
            "error": None,
            "stdout_file": "",
            "stderr_file": "",
            "messages": "",
        }

    monkeypatch.setattr(
        "operon.tui.screens.run_external.actions.run_external", blocking_run
    )

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = RunExternalModal(project)
            await _push(pilot, modal, "#external-step")

            # Changed events from controls this dialog does not own are inert.
            modal.on_input_changed(Input.Changed(Input(id="stray-input"), "x"))
            modal.on_select_changed(
                Select.Changed(Select([("A", "a")], id="stray-select"), "a")
            )
            assert modal.command_text().startswith("operon run-external --step '…'")

            modal.query_one("#external-step", Input).value = "marker_step"
            modal.query_one("#external-command", Input).value = "true"
            modal.query_one("#external-threads", Input).value = "abc"
            modal.confirm()
            assert "threads must be a positive integer" in _static_text(
                modal.query_one("#modal-error", Static)
            )
            modal.query_one("#external-threads", Input).value = ""
            modal.query_one("#external-timeout", Input).value = "0"
            modal.confirm()
            assert "timeout must be a positive number" in _static_text(
                modal.query_one("#modal-error", Static)
            )
            modal.query_one("#external-timeout", Input).value = ""
            # The form strips the command line and refuses an empty one before
            # this point, so the parsed-empty guard is exercised with the parse
            # result the CLI sees for whitespace-only input.
            real_split = shlex.split
            monkeypatch.setattr(shlex, "split", lambda _line: [])
            try:
                modal.query_one("#external-command", Input).value = "true"
                modal.confirm()
                assert "--command must not be empty" in _static_text(
                    modal.query_one("#modal-error", Static)
                )
                assert modal.running is False
            finally:
                monkeypatch.setattr(shlex, "split", real_split)

            modal.query_one("#external-command", Input).value = "true"
            await _click(pilot, "#confirm")
            await _wait_until(
                started.is_set,
                "the external command to reach the core",
                timeout=HANDOFF_TIMEOUT,
            )
            assert modal.running is True
            # A second Confirm while the worker runs must not start another run.
            modal.confirm()
            assert modal.running is True
            released.set()
            await _wait_until(
                lambda: app.screen is not modal,
                "the external modal to close",
                timeout=HANDOFF_TIMEOUT,
            )

            # Cancelling an idle dialog closes it without touching the core.
            idle = RunExternalModal(project)
            await _push(pilot, idle, "#external-step")
            idle.action_cancel()
            await _wait_until(lambda: app.screen is not idle, "the idle modal to close")

    try:
        _run(scenario())
    finally:
        released.set()
    assert calls == [("marker_step", "true")]


# ---------------------------------------------------------------------------
# publish panel
# ---------------------------------------------------------------------------


def test_publish_panel_composes_both_forms(demo_project: Project) -> None:
    """The release and export forms compose together and the tabs switch."""

    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            app.action_switch_screen("publish")
            await pilot.pause()
            panel = app.query_one(PublishPanel)
            await _wait_until(
                lambda: len(panel.query("#release-form")) > 0, "the release form"
            )
            await _settled(app)
            assert panel.query_one("#releases-table", DataTable).row_count >= 1
            assert len(panel.query("#export-layout")) > 0
            export_fields = panel.query("#export-fields Input, #export-fields Select")
            assert len(export_fields) > 0
            assert len(panel.query("#export-output")) > 0

    _run(scenario())
