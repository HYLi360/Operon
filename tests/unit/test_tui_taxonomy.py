"""Taxonomy TUI: ``actions.import_taxonomy`` and the Coverage-screen import modal.

The action mirrors ``operon taxonomy import`` (the core archives the
content-addressed source, imports nodes/aliases in one transaction and
records the same audit/run rows), and the modal is the RunExternalModal
pattern: the core has no cooperative cancel, so a running form refuses to
close instead of pretending.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.pilot import OutOfBounds
from textual.widgets import Button, DataTable, Input, Static

from operon.config import Project
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.tui import actions
from operon.tui.app import OperonApp
from operon.tui.screens.coverage import CoveragePanel
from operon.tui.screens.taxonomy import TaxonomyImportModal

SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 30.0


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """Each test gets its own freshly initialized empty project."""
    return Project.init(tmp_path / "project")


def _taxonomy_records() -> list[dict]:
    return [
        {"taxId": 1, "rank": "no rank", "taxName": "root"},
        {"taxId": 10, "parents": [1], "rank": "family", "taxName": "Fam"},
        {"taxId": 20, "parents": [10], "rank": "genus", "taxName": "Gen"},
    ]


def _write_taxonomy_jsonl(path: Path, records: list[dict] | None = None) -> Path:
    if records is None:
        records = _taxonomy_records()
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    return path


def _query(project: Project, sql: str, params: tuple = ()) -> list[dict]:
    db = Database(project.db_path, read_only=True)
    try:
        return [dict(row) for row in db.query(sql, params)]
    finally:
        db.close()


def _count(project: Project, table: str) -> int:
    return _query(project, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


def _run(coroutine) -> None:
    asyncio.run(asyncio.wait_for(coroutine, timeout=SCENARIO_TIMEOUT))


async def _settled(app, timeout: float = SETTLE_TIMEOUT) -> None:
    """Wait until no workers are running, with a diagnostic timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # The splash screen blocks key bindings until the startup worker finishes;
    # gate on _starting as well so a modal pushed during startup is not popped
    # by the splash's own pop_screen.
    while app.workers or getattr(app, "_starting", False):
        if loop.time() > deadline:
            states = [worker.state.name for worker in app.workers]
            raise TimeoutError(f"workers did not finish within {timeout}s: {states}")
        await asyncio.sleep(0.05)


async def _wait_until(predicate: Callable[[], bool], description: str,
                      timeout: float = SETTLE_TIMEOUT) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not {description} within {timeout}s")
        await asyncio.sleep(0.05)


async def _click(pilot, selector: str) -> None:
    """Activate a widget, tolerating clipping and a lingering press effect."""
    widget = pilot.app.screen.query_one(selector)
    widget.scroll_visible(animate=False)
    await pilot.pause()
    if isinstance(widget, Button) and widget.has_class("-active"):
        await _wait_until(lambda: not widget.has_class("-active"),
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


async def _push(pilot, modal, selector: str | None = None) -> None:
    """Push a screen and wait until its form **and** buttons have composed."""
    for attempt in range(2):
        if pilot.app.screen is not modal:
            pilot.app.push_screen(modal)

        def ready() -> bool:
            if len(modal.query("#confirm")) == 0:
                return False
            if selector is None:
                return bool(modal.query("#modal-form > *"))
            return len(modal.query(selector)) > 0

        try:
            await _wait_until(ready, f"{type(modal).__name__} to compose", timeout=10.0)
        except TimeoutError:
            if attempt:
                raise
            await pilot.pause()
            continue
        await pilot.pause()
        return


async def _q(modal, selector: str, *types):
    await _wait_until(lambda: len(modal.query(selector)) > 0, f"{selector} to exist")
    return modal.query_one(selector, *types)


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


def _notifications(app) -> list[tuple[str, str]]:
    return [(n.severity, str(n.message)) for n in app._notifications]


def parse_command_text(text: str):
    """Parse a modal's equivalent-command preview with the real CLI parser."""
    import shlex

    from operon.cli import _parser

    argv = shlex.split(text)
    assert argv and argv[0] == "operon", f"unexpected command preview: {text!r}"
    return _parser().parse_args(argv[1:])


# ---------------------------------------------------------------------------
# actions.import_taxonomy
# ---------------------------------------------------------------------------


def test_import_taxonomy_records_snapshot_and_provenance(
        project: Project, tmp_path: Path) -> None:
    source = _write_taxonomy_jsonl(tmp_path / "taxonomy.jsonl")
    result = actions.import_taxonomy(project, str(source), "cov.1")
    assert result["reused"] is False
    assert result["taxonomy_version"] == "cov.1"
    assert result["node_count"] == 3
    assert result["status"] == "READY"
    snapshot_id = result["taxonomy_snapshot_id"]

    snapshots = _query(project, "SELECT * FROM taxonomy_snapshots")
    assert len(snapshots) == 1
    assert snapshots[0]["taxonomy_version"] == "cov.1"
    assert snapshots[0]["status"] == "READY"
    assert snapshots[0]["node_count"] == 3

    files = _query(
        project, "SELECT * FROM files WHERE entity_type='taxonomy_snapshot'")
    assert len(files) == 1
    assert files[0]["entity_id"] == snapshot_id
    assert files[0]["sha256"] == result["source_sha256"]
    archived = project.root / files[0]["relative_path"]
    assert archived.is_file()
    assert archived.parent == project.raw_root / "metadata" / "ncbi_taxonomy"

    states = _query(
        project,
        "SELECT * FROM entity_state WHERE entity_type='taxonomy_snapshot'")
    assert [row["entity_id"] for row in states] == [snapshot_id]

    changes = _query(
        project, "SELECT * FROM changes WHERE object_type='taxonomy_snapshot'")
    assert len(changes) == 1
    assert changes[0]["object_id"] == snapshot_id
    assert "imported_snapshot" == changes[0]["field"]

    runs = _query(
        project, "SELECT * FROM workflow_runs WHERE step='taxonomy_import'")
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["entity_id"] == snapshot_id
    assert runs[0]["error"] is None


def test_import_taxonomy_reuses_identical_bytes(
        project: Project, tmp_path: Path) -> None:
    source = _write_taxonomy_jsonl(tmp_path / "taxonomy.jsonl")
    first = actions.import_taxonomy(project, str(source), "cov.1")
    second = actions.import_taxonomy(project, str(source), "cov.1")
    assert second["reused"] is True
    assert second["taxonomy_snapshot_id"] == first["taxonomy_snapshot_id"]
    assert _count(project, "taxonomy_snapshots") == 1
    assert _count(project, "files") == 1


def test_import_taxonomy_rejects_same_version_with_different_bytes(
        project: Project, tmp_path: Path) -> None:
    source = _write_taxonomy_jsonl(tmp_path / "taxonomy.jsonl")
    actions.import_taxonomy(project, str(source), "cov.1")
    other = _write_taxonomy_jsonl(
        tmp_path / "other.jsonl",
        [{"taxId": 1, "rank": "no rank", "taxName": "other root"}],
    )
    with pytest.raises(ConflictError, match="different bytes"):
        actions.import_taxonomy(project, str(other), "cov.1")
    assert _count(project, "taxonomy_snapshots") == 1


def test_import_taxonomy_missing_file_writes_nothing(
        project: Project, tmp_path: Path) -> None:
    runs_before = _count(project, "workflow_runs")
    with pytest.raises(ValidationError, match="must be a file"):
        actions.import_taxonomy(project, str(tmp_path / "missing.jsonl"), "cov.1")
    assert _count(project, "workflow_runs") == runs_before
    assert _count(project, "taxonomy_snapshots") == 0
    assert _count(project, "changes") == 0


def test_import_taxonomy_failed_import_records_failed_run(
        project: Project, tmp_path: Path) -> None:
    source = _write_taxonomy_jsonl(
        tmp_path / "broken.jsonl",
        [{"taxId": 10, "parents": [999], "rank": "family", "taxName": "Fam"}],
    )
    with pytest.raises(ValidationError, match="missing parent"):
        actions.import_taxonomy(project, str(source), "cov.1")
    assert _count(project, "taxonomy_snapshots") == 0
    runs = _query(
        project, "SELECT * FROM workflow_runs WHERE step='taxonomy_import'")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "ValidationError" in runs[0]["error"]


# ---------------------------------------------------------------------------
# TaxonomyImportModal
# ---------------------------------------------------------------------------


def test_import_modal_confirm_calls_action_with_form_values(
        project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _write_taxonomy_jsonl(tmp_path / "taxonomy.jsonl")
    payload = {
        "taxonomy_snapshot_id": "TAX_000001", "taxonomy_version": "cov.1",
        "node_count": 3, "reused": True,
    }
    calls: list[tuple[tuple, dict]] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return payload

    monkeypatch.setattr(actions, "import_taxonomy", stub)
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = TaxonomyImportModal(project)
            app.push_screen(modal, dismissed.append)
            await _push(pilot, modal, "#taxonomy-import-input")
            (await _q(modal, "#taxonomy-import-input", Input)).value = str(source)
            (await _q(modal, "#taxonomy-import-version", Input)).value = "cov.1"
            await pilot.pause()

            preview = _static_text(modal.query_one("#modal-command", Static))
            ns = parse_command_text(preview.removeprefix("$ "))
            assert ns.input == str(source)
            assert ns.version == "cov.1"

            await _click(pilot, "#confirm")
            await _wait_until(lambda: len(calls) == 1, "taxonomy import call")
            await _settled(app)

    _run(scenario())
    args, kwargs = calls[0]
    assert args == (project, str(source), "cov.1")
    assert kwargs == {}
    assert dismissed == [payload]


def test_import_modal_real_run_notifies_and_reloads(
        project: Project, tmp_path: Path) -> None:
    source = _write_taxonomy_jsonl(tmp_path / "taxonomy.jsonl")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            app.action_switch_screen("coverage")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(CoveragePanel)
            assert panel.query_one("#taxonomy-snapshots-table", DataTable).row_count == 0

            panel.on_button_pressed(Button.Pressed(
                panel.query_one("#coverage-import-taxonomy", Button)))
            await pilot.pause()

            await _wait_until(
                lambda: isinstance(app.screen, TaxonomyImportModal),
                "import modal to open",
            )
            modal = app.screen
            await _push(pilot, modal, "#taxonomy-import-input")
            (await _q(modal, "#taxonomy-import-input", Input)).value = str(source)
            (await _q(modal, "#taxonomy-import-version", Input)).value = "cov.1"
            await pilot.pause()
            await _click(pilot, "#confirm")
            await _wait_until(lambda: app.screen is not modal, "import modal to close")
            await _settled(app)
            assert any("node(s) imported as" in message
                       for _severity, message in _notifications(app))
            await _wait_until(
                lambda: panel.query_one("#taxonomy-snapshots-table", DataTable).row_count == 1,
                "snapshot table reload",
            )

    _run(scenario())
    assert _count(project, "taxonomy_snapshots") == 1


def test_import_modal_error_stays_open(project: Project, tmp_path: Path) -> None:
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = TaxonomyImportModal(project)
            app.push_screen(modal, dismissed.append)
            await _push(pilot, modal, "#taxonomy-import-input")

            # Form-level validation errors stay inline and keep the modal open.
            (await _q(modal, "#taxonomy-import-version", Input)).value = "cov.1"
            await pilot.pause()
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "--input is required" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "input-required inline error",
            )
            version_input = await _q(modal, "#taxonomy-import-version", Input)
            version_input.value = ""
            (await _q(modal, "#taxonomy-import-input", Input)).value = str(
                tmp_path / "missing.jsonl")
            await pilot.pause()
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "--version is required" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "version-required inline error",
            )
            version_input.value = "cov.1"
            await pilot.pause()

            # A core failure is also shown inline and the modal stays open.
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "must be a file" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "inline error",
            )
            await _settled(app)
            assert app.screen is modal
            assert modal.running is False

            # Input events from widgets the modal does not own leave the
            # command preview alone.
            modal.on_input_changed(Input.Changed(Input(), "noise"))
            await pilot.pause()

            # Cancel with no run in flight dismisses normally.
            modal.action_cancel()
            await pilot.pause()
            assert dismissed == [None]

    _run(scenario())


def test_import_modal_refuses_cancel_while_running(
        project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    released = threading.Event()
    calls: list[tuple] = []

    def blocking_import(*args, **kwargs):
        calls.append((args, kwargs))
        released.wait(30)
        return {"taxonomy_snapshot_id": "TAX_000001", "taxonomy_version": "cov.1",
                "node_count": 3, "reused": False}

    monkeypatch.setattr(actions, "import_taxonomy", blocking_import)
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = TaxonomyImportModal(project)
            app.push_screen(modal, dismissed.append)
            await _push(pilot, modal, "#taxonomy-import-input")
            (await _q(modal, "#taxonomy-import-input", Input)).value = "taxonomy.jsonl"
            (await _q(modal, "#taxonomy-import-version", Input)).value = "cov.1"
            await pilot.pause()
            modal.confirm()
            await _wait_until(lambda: modal.running, "taxonomy import to start")

            # A second Confirm while running is ignored.
            modal.confirm()
            await pilot.pause()
            assert len(calls) == 1

            # Cancel and escape must not dismiss the modal mid-run.
            modal.on_button_pressed(Button.Pressed(modal.query_one("#cancel", Button)))
            modal.action_cancel()
            await pilot.pause()
            assert app.screen is modal
            assert any(severity == "warning" and "cannot be interrupted" in message
                       for severity, message in _notifications(app))
            assert dismissed == []
            assert all(widget.disabled for widget in modal.query("Input"))

            released.set()
            await _wait_until(lambda: bool(dismissed), "import modal dismissal")
            await _settled(app)

    try:
        _run(scenario())
    finally:
        released.set()
