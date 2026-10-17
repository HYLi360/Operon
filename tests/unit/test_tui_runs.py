#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Coverage for the Tasks screen: run listing, run detail and the hit browser.

The Tasks panel, its dialogs and the run-detail follow loop carry branches the
end-to-end flows never reach: a dialog cancelled back to its opener (a ``None``
payload), ``Pressed``/``Changed`` messages from controls a widget does not own,
a followed run that leaves ``running`` or is deleted mid-follow, and the hits
browser's reload-error and export guards.  Everything runs against mounted
widgets — misrouted messages are delivered by calling the handler with the
event the stray control would raise — and nothing is asserted after a bare
pause: every wait is a predicate wait on the observable result.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import Button, Checkbox, Input, RichLog, Select, Static

from operon.config import Project
from operon.database import Database
from operon.tui import data
from operon.tui.app import OperonApp
from operon.tui.screens.hits import AnalysisHitsModal
from operon.tui.screens.run_external import RunExternalModal
from operon.tui.screens.runs import (
    AnalysisJobsModal,
    RunDetailScreen,
    RunsFiltersModal,
    RunsPanel,
)
from operon.workflow import log_run
from tests.tui_helpers import click as _click

SCENARIO_TIMEOUT = 180.0
SETTLE_TIMEOUT = 30.0


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project.init(tmp_path / "project")


def _run(coroutine) -> None:
    asyncio.run(asyncio.wait_for(coroutine, timeout=SCENARIO_TIMEOUT))


async def _settled(app, timeout: float = SETTLE_TIMEOUT) -> None:
    """Wait until no workers are running, with a diagnostic timeout."""
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
    """Wait for an observable UI result instead of a fixed number of pauses."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not {description} within {timeout}s")
        await asyncio.sleep(0.05)


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


def _log_text(log: RichLog) -> str:
    """The plain text a RichLog currently holds."""
    return "\n".join(strip.text for strip in log.lines)


def _running_run(project: Project) -> str:
    """Seed one ``running`` workflow run; returns its run id."""
    db = Database(project.db_path)
    try:
        run = log_run(
            db,
            project,
            {
                "step": "qc",
                "status": "running",
                "entity_type": "run",
                "entity_id": "RUN_000001",
            },
        )
    finally:
        db.close()
    return run["run_id"]


def _set_run_status(project: Project, run_id: str, status: str = "completed") -> None:
    db = Database(project.db_path)
    try:
        with db.transaction():
            db.conn.execute(
                "UPDATE workflow_runs SET status=?, exit_code=? WHERE run_id=?",
                (status, 0 if status == "completed" else 1, run_id),
            )
    finally:
        db.close()


def _delete_run(project: Project, run_id: str) -> None:
    db = Database(project.db_path)
    try:
        with db.transaction():
            db.conn.execute("DELETE FROM workflow_runs WHERE run_id=?", (run_id,))
    finally:
        db.close()


async def _runs_panel(pilot, app) -> RunsPanel:
    app.action_switch_screen("runs")
    await pilot.pause()
    await _settled(app)
    return app.query_one(RunsPanel)


# ---------------------------------------------------------------------------
# Tasks panel: the More… filters dialog and the panel's dispatch chain
# ---------------------------------------------------------------------------


def test_runs_filters_cancel_keeps_the_previous_filters(project: Project) -> None:
    """Cancel dismisses with None: the panel callback keeps the old filters."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            panel = await _runs_panel(pilot, app)
            panel.advanced = {"tool": "preset"}
            await _click(pilot, "#runs-more")
            await _wait_until(
                lambda: isinstance(app.screen, RunsFiltersModal),
                "the advanced filters dialog",
            )
            modal = app.screen
            assert modal.query_one("#runs-filter-tool", Input).value == "preset"

            # A Button.Pressed from a control the dialog does not own falls
            # off the handler chain and leaves the dialog open.
            modal.on_button_pressed(Button.Pressed(Button("Stray", id="stray")))
            assert isinstance(app.screen, RunsFiltersModal)

            # The Cancel button dismisses with None; the panel callback then
            # returns before touching the stored filters.
            await _click(pilot, "#runs-filter-cancel")
            await _wait_until(
                lambda: not isinstance(app.screen, RunsFiltersModal),
                "the advanced filters dialog to close",
            )
            assert panel.advanced == {"tool": "preset"}
            assert str(panel.query_one("#runs-more", Button).label) == "More…"

            # A Button.Pressed on the panel itself runs the whole if/elif
            # chain on button ids to its end and opens nothing.
            panel.on_button_pressed(Button.Pressed(Button("Stray", id="stray")))
            assert not isinstance(app.screen, RunsFiltersModal)

    _run(scenario())


def test_run_external_cancel_and_jobs_modal_ignore_foreign_events(
    project: Project,
) -> None:
    """Cancelling Run external hands None back; stray jobs events are inert."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await _runs_panel(pilot, app)

            # Cancelling the external-run form dismisses with None: the
            # panel's callback's falsy-payload guard returns (nothing opens).
            await _click(pilot, "#runs-external")
            await _wait_until(
                lambda: isinstance(app.screen, RunExternalModal),
                "the run-external dialog",
            )
            await pilot.press("escape")
            await _wait_until(
                lambda: not isinstance(app.screen, RunExternalModal),
                "the run-external dialog to close",
            )
            assert not isinstance(app.screen, RunDetailScreen)

            # The jobs browser: Pressed/Changed from stray controls must not
            # touch it; the empty project reports no matching jobs.
            await _click(pilot, "#runs-jobs")
            await _wait_until(
                lambda: isinstance(app.screen, AnalysisJobsModal),
                "the analysis-jobs dialog",
            )
            modal = app.screen
            await _wait_until(
                lambda: len(modal.query("#jobs-detail")) > 0, "the jobs form"
            )
            await _wait_until(
                lambda: (
                    "no matching analysis jobs"
                    in _static_text(modal.query_one("#jobs-detail", Static))
                ),
                "the jobs detail",
            )
            modal.on_input_changed(Input.Changed(Input(id="stray-input"), "x"))
            modal.on_select_changed(
                Select.Changed(Select([("ALL", "ALL")], id="stray-select"), "ALL")
            )
            modal.on_button_pressed(Button.Pressed(Button("Stray", id="stray")))
            assert isinstance(app.screen, AnalysisJobsModal)

            await _click(pilot, "#cancel")
            await _wait_until(
                lambda: not isinstance(app.screen, AnalysisJobsModal),
                "the jobs dialog to close",
            )

    _run(scenario())


# ---------------------------------------------------------------------------
# Run detail: the follow loop's edge states
# ---------------------------------------------------------------------------


def test_run_detail_follow_state_edges_and_disappearing_run(project: Project) -> None:
    """Follow reuse, a finished record and a deleted run each stop cleanly."""
    run_id = _running_run(project)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)):
            await _settled(app)
            screen = RunDetailScreen(project, run_id)
            app.push_screen(screen)
            await _wait_until(lambda: screen.record is not None, "the run record")
            follow = screen.query_one("#run-follow", Checkbox)
            assert not follow.disabled
            log = screen.query_one("#run-follow-log", RichLog)

            # A Changed from a checkbox the screen does not own is inert.
            screen.on_checkbox_changed(Checkbox.Changed(Checkbox(id="stray"), True))
            assert not screen._following

            # The first start arms the interval; a second start reuses it.
            screen._start_following()
            timer = screen._follow_timer
            assert timer is not None
            screen._start_following()
            assert screen._follow_timer is timer

            # The run leaving "running" while followed stops the follow and
            # refreshes the record (no awaits between these calls, so the
            # interval tick cannot interleave).
            _set_run_status(project, run_id)
            record = data.workflow_run_detail(project, run_id)
            assert record is not None and record["status"] == "completed"
            screen._apply(record)
            assert not screen._following
            assert screen._follow_timer is None
            assert follow.disabled is True
            await _settled(app)

            # A run row deleted mid-follow is reported by the next tick and
            # the follow stops without arming the interval.
            _delete_run(project, run_id)
            follow.disabled = False
            screen._start_following()
            await _wait_until(
                lambda: (
                    f"workflow run disappeared while following: {run_id}"
                    in _log_text(log)
                ),
                "the disappeared-run notice",
            )
            assert not screen._following
            assert screen._follow_timer is None
            await _settled(app)

    _run(scenario())


# ---------------------------------------------------------------------------
# Analysis hits browser: reload errors and the export guard
# ---------------------------------------------------------------------------


def test_hits_browser_reload_error_and_export_guard(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing reload renders inline; a second export start is a no-op."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = AnalysisHitsModal(project)
            app.push_screen(modal)
            await _wait_until(
                lambda: len(modal.query("#hits-status")) > 0, "the hits form"
            )
            await _wait_until(
                lambda: (
                    "0 hit row(s)"
                    in _static_text(modal.query_one("#hits-status", Static))
                ),
                "the empty hit list",
            )

            # Toggling include-retired reloads; the failing worker surfaces
            # its exception inline instead of raising.
            def broken_hits(*args, **kwargs):
                raise RuntimeError("hits backend down")

            monkeypatch.setattr(data, "analysis_hits", broken_hits)
            modal.on_checkbox_changed(
                Checkbox.Changed(
                    modal.query_one("#hits-include-retired", Checkbox), True
                )
            )
            await _wait_until(
                lambda: (
                    "error: hits backend down"
                    in _static_text(modal.query_one("#hits-status", Static))
                ),
                "the reload error",
            )

            # While an export is in flight, a second start returns at once.
            modal._exporting = True
            modal._start_export()
            assert modal._exporting is True

            # A Button.Pressed from a stray control leaves the modal open.
            modal.on_button_pressed(Button.Pressed(Button("Stray", id="stray")))
            assert isinstance(app.screen, AnalysisHitsModal)

            await _click(pilot, "#cancel")
            await _wait_until(
                lambda: not isinstance(app.screen, AnalysisHitsModal),
                "the hits dialog to close",
            )
            await _settled(app)

    _run(scenario())
