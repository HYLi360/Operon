"""The Files screen's ``S`` entry for `operon standardize` (milestone M4).

``standardize`` stages a verified artifact into ``standardized/`` as an
independent copy (or an explicit hardlink/symlink).  The TUI entry follows the
same contract as every other write flow: form preview, the equivalent CLI
command, an explicit Confirm, and the core function underneath.

The demo project ships every file already staged and its entities in terminal
states (``RELEASED``/``ACCEPTED``), so a *fresh* staging is not possible there —
and the core rightly refuses it: ``standardize_file`` moves the entity to
``STANDARDIZED``, which the state machine only allows from ``CHECKSUM_VERIFIED``.
The link-kind semantics themselves are covered by the core tests
(``test_core_coverage_more.py``); what these tests pin is the TUI plumbing:
the preview, the pass-through of the chosen kind, the idempotent path, and the
inline reporting of the core's refusal.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import Button, DataTable, Select, Static

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ChecksumError
from operon.files import raw_bucket
from operon.tui import actions
from operon.tui.app import OperonApp
from operon.tui.screens.files import FilesPanel
from operon.tui.screens.files_ops import StandardizeModal

SCENARIO_TIMEOUT = 180.0
SETTLE_TIMEOUT = 30.0
#: Budget for a worker result crossing back from its thread to the UI, and for
#: the screen teardown that follows it (ODR-0046).
HANDOFF_TIMEOUT = 120.0


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-standardize-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
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


async def _wait_until(predicate: Callable[[], bool], description: str,
                      timeout: float = SETTLE_TIMEOUT) -> None:
    """Wait for an observable UI result; handoffs pass ``timeout=HANDOFF_TIMEOUT``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not {description} within {timeout}s")
        await asyncio.sleep(0.05)


async def _click(pilot, selector: str) -> None:
    """Click a widget, clearing a lingering press effect first (ODR-0024).

    A ``Button`` keeps its ``-active`` press effect for about 0.2 s and Textual
    drops a ``Button.Pressed`` raised inside that window, so a rapid second
    click would report ``landed=True`` and do nothing.
    """
    widget = pilot.app.screen.query_one(selector)
    widget.scroll_visible(animate=False)
    await pilot.pause()
    if isinstance(widget, Button) and widget.has_class("-active"):
        await _wait_until(lambda: not widget.has_class("-active"), f"{selector} to settle")
    await pilot.click(selector)


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


def _notifications(app) -> list[tuple[str, str]]:
    return [(notification.severity, notification.message) for notification in app._notifications]


def _query(project: Project, sql: str, params: tuple = ()) -> list[dict]:
    db = Database(project.db_path, read_only=True)
    try:
        return [dict(row) for row in db.query(sql, params)]
    finally:
        db.close()


def _file_rows(project: Project) -> list[dict]:
    return _query(project, "SELECT * FROM files ORDER BY file_id")


def _standardized_target(project: Project, row: dict) -> Path:
    return (project.standardized_root / raw_bucket(row["entity_type"]) / row["entity_id"]
            / Path(row["relative_path"]).name)


async def _open_modal(pilot, app, row_id: str | None = None) -> tuple[StandardizeModal, dict]:
    """Open the Files screen's ``S`` modal on one row, returning both."""
    app.action_switch_screen("files")
    await pilot.pause()
    await _settled(app)
    panel = app.query_one(FilesPanel)
    index = 0 if row_id is None else next(
        position for position, item in enumerate(panel.files) if item["file_id"] == row_id)
    table = panel.query_one("#files-table", DataTable)
    table.focus()
    table.move_cursor(row=index, animate=False)
    await pilot.pause()
    row = dict(panel.files[index])
    await pilot.press("S")
    await pilot.pause()
    modal = app.screen
    assert isinstance(modal, StandardizeModal), modal
    return modal, row


def test_files_screen_standardize_previews_the_cli_and_reports_already_staged(
        project: Project) -> None:
    """``S`` shows the equivalent command; Confirm runs it and reports the outcome."""
    opened: list[dict] = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            modal, row = await _open_modal(pilot, app)
            opened.append(row)
            file_id = row["file_id"]

            command = _static_text(modal.query_one("#modal-command", Static))
            assert f"operon standardize --file-id {file_id}" in command, command
            assert "--link copy" in command, command

            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: not isinstance(app.screen, StandardizeModal),
                "standardize modal closed", timeout=HANDOFF_TIMEOUT)
            await _settled(app)
            assert ("information", "standardized 0 file(s); 1 already staged") in _notifications(app)

    _run(scenario())

    row = _query(project, "SELECT * FROM files WHERE file_id=?", (opened[0]["file_id"],))[0]
    assert row["status"] == "STANDARDIZED"
    target = _standardized_target(project, row)
    assert target.exists() and not target.is_symlink()


def test_standardize_modal_passes_the_chosen_link_kind_through(project: Project,
                                                              monkeypatch) -> None:
    """The link-kind control updates the shown command and reaches the action."""
    seen: list[tuple] = []

    def fake_standardize(project_arg, file_id=None, link_kind="copy"):
        seen.append((file_id, link_kind))
        return {"file_id": file_id, "link_kind": link_kind, "total": 1,
                "results": [{"file_id": file_id, "action": "created", "target": "x"}],
                "errors": []}

    monkeypatch.setattr(actions, "standardize", fake_standardize)
    opened: list[dict] = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            modal, row = await _open_modal(pilot, app)
            opened.append(row)
            select = modal.query_one("#standardize-link", Select)
            select.value = "hardlink"
            await pilot.pause()
            command = _static_text(modal.query_one("#modal-command", Static))
            assert "--link hardlink" in command, command

            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: not isinstance(app.screen, StandardizeModal),
                "standardize modal closed", timeout=HANDOFF_TIMEOUT)

    _run(scenario())
    assert seen == [(opened[0]["file_id"], "hardlink")]


def test_standardize_reports_a_refused_transition_inline(project: Project) -> None:
    """The core's refusal (a RELEASED entity) stays inline; nothing is staged."""
    released = _query(project, "SELECT f.* FROM files f JOIN entity_state s "
                               "ON s.entity_type=f.entity_type AND s.entity_id=f.entity_id "
                               "WHERE s.state='RELEASED' ORDER BY f.file_id")[0]
    _standardized_target(project, released).unlink()

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            modal, row = await _open_modal(pilot, app, row_id=released["file_id"])
            assert row["file_id"] == released["file_id"], row
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "illegal transition" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "the refusal to appear inline", timeout=HANDOFF_TIMEOUT)
            assert isinstance(app.screen, StandardizeModal)

    _run(scenario())

    row = _query(project, "SELECT * FROM files WHERE file_id=?", (released["file_id"],))[0]
    assert row["status"] == "STANDARDIZED"
    assert not _standardized_target(project, row).exists()
    state = _query(project, "SELECT state FROM entity_state WHERE entity_type=? AND entity_id=?",
                   (row["entity_type"], row["entity_id"]))[0]
    assert state["state"] == "RELEASED"


def test_standardize_action_walks_every_file_and_reports_skips(project: Project) -> None:
    """The no-file-id form visits every manifest entry and is idempotent."""
    rows = _file_rows(project)
    assert rows, "the demo project has no files"

    result = actions.standardize(project)

    assert result["total"] == len(rows)
    assert result["errors"] == []
    assert {item["file_id"] for item in result["results"]} == {row["file_id"] for row in rows}
    assert all(item["action"] == "skipped" for item in result["results"])
    for row in rows:
        assert _standardized_target(project, row).exists()
        assert _query(project, "SELECT status FROM files WHERE file_id=?",
                      (row["file_id"],))[0]["status"] == "STANDARDIZED"


def test_standardize_reports_a_missing_source_by_raising(project: Project) -> None:
    """A missing source raises before any state work, which the modal shows inline."""
    row = _file_rows(project)[0]
    (project.root / row["relative_path"]).unlink()
    _standardized_target(project, row).unlink()

    with pytest.raises(ChecksumError):
        actions.standardize(project, row["file_id"])
