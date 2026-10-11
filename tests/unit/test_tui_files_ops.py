"""Coverage for the Files screen: panel callbacks and write-modal guards.

The Files panel and its write modals carry small branches the end-to-end
flows never reach: a payload that arrives empty (a dialog cancelled back to
its opener), a form edited after its preview passed, a ``Changed`` message
from a control the dialog does not own, and a foreign ``Button.Pressed`` that
falls through a handler's chain.  Every branch runs against the mounted
widgets — misrouted messages are delivered by calling the handler with the
event a stray control would raise — and nothing is asserted after a bare
pause: every wait is a predicate wait on the observable result.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.css.query import NoMatches
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select, Static

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.qc import MEASURE_SCHEMA_VERSION, TOOL_NAME
from operon.tui import actions
from operon.tui.app import OperonApp
from operon.tui.screens.common import ErrorDialog
from operon.tui.screens.derived_ops import (
    AdoptModal,
    FanoutModal,
    SelectSequencesModal,
)
from operon.tui.screens.files import FilesPanel
from operon.tui.screens.files_ops import (
    ImportQcModal,
    PipelineModal,
    QcModal,
    StandardizeModal,
)
from tests.tui_helpers import click as _click

SCENARIO_TIMEOUT = 180.0
SETTLE_TIMEOUT = 30.0
#: Budget for a worker result crossing back from its thread to the UI (ODR-46).
HANDOFF_TIMEOUT = 120.0


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-files-ops-demo"))


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


async def _wait_until(
    predicate: Callable[[], bool], description: str, timeout: float = SETTLE_TIMEOUT
) -> None:
    """Wait for an observable UI result; handoffs pass ``timeout=HANDOFF_TIMEOUT``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not {description} within {timeout}s")
        await asyncio.sleep(0.05)


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


def _query(project: Project, sql: str, params: tuple = ()) -> list[dict]:
    db = Database(project.db_path, read_only=True)
    try:
        return [dict(row) for row in db.query(sql, params)]
    finally:
        db.close()


def _notify_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every notification text instead of reading the toast rack."""
    seen: list[str] = []
    real_notify = OperonApp.notify

    def spy(self, message, **kwargs):
        seen.append(str(message))
        return real_notify(self, message, **kwargs)

    monkeypatch.setattr(OperonApp, "notify", spy)
    return seen


async def _files_panel(pilot, app) -> FilesPanel:
    app.action_switch_screen("files")
    await pilot.pause()
    await _settled(app)
    panel = app.query_one(FilesPanel)
    panel.query_one("#files-table", DataTable).focus()
    await pilot.pause()
    return panel


# ---------------------------------------------------------------------------
# Files panel: modal entry points and their dismiss callbacks
# ---------------------------------------------------------------------------


def test_files_panel_opens_and_cancels_every_write_modal(project: Project) -> None:
    """`a`/`f`/`s`/`S`/`I`/`P` each open their dialog; cancelling hands back None."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            panel = await _files_panel(pilot, app)

            for key, modal_type in (
                ("a", AdoptModal),
                ("f", FanoutModal),
                ("s", SelectSequencesModal),
                ("S", StandardizeModal),
                ("I", ImportQcModal),
                ("P", PipelineModal),
            ):
                panel.query_one("#files-table", DataTable).focus()
                await pilot.pause()
                await pilot.press(key)
                await _wait_until(
                    lambda modal_type=modal_type: isinstance(app.screen, modal_type),
                    f"the {key!r} dialog to open",
                )
                await pilot.press("escape")
                await _wait_until(
                    lambda modal_type=modal_type: (
                        not isinstance(app.screen, modal_type)
                    ),
                    f"the {key!r} dialog to close",
                )

    _run(scenario())


def test_import_qc_json_payload_notifies_through_the_panel(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JSON import reports built-in metrics plus the tool-version warning."""
    file_row = _query(
        project, "SELECT * FROM files WHERE status='STANDARDIZED' ORDER BY file_id"
    )[0]
    payload = {
        "schema_version": MEASURE_SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "tool_version": "0.0.0",
        "parameter_set": "default",
        "file": {
            "file_id": file_row["file_id"],
            "sha256": file_row["sha256"],
            "size_bytes": file_row["size_bytes"],
        },
        "metrics": [
            {
                "qc_stage": "assembled",
                "metric_name": "tui_file_ops_n50",
                "metric_value": "1234",
                "metric_numeric": 1234.0,
                "metric_unit": "bp",
            },
            {
                "qc_stage": "assembled",
                "metric_name": "tui_file_ops_total_length",
                "metric_value": "5000",
                "metric_numeric": 5000.0,
                "metric_unit": "bp",
            },
        ],
    }
    source = tmp_path / "measured.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    seen = _notify_spy(monkeypatch)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            await _files_panel(pilot, app)
            await pilot.press("I")
            await _wait_until(
                lambda: isinstance(app.screen, ImportQcModal), "the import dialog"
            )
            modal = app.screen
            modal.query_one("#qc-import-path", Input).value = str(source)
            await pilot.pause()
            await _click(pilot, "#qc-import-preview-button")
            await _wait_until(
                lambda: (
                    not modal.preview_running
                    and modal.query_one("#qc-import-preview-button", Button).disabled
                    is False
                ),
                "the preview to finish",
                timeout=HANDOFF_TIMEOUT,
            )
            # The JSON preview names the file and appends the version warning.
            status = _static_text(modal.query_one("#qc-import-status", Static))
            assert f"file: {file_row['file_id']} (" in status, status
            assert "warning:" in status, status

            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: not isinstance(app.screen, ImportQcModal),
                "the import dialog to close",
                timeout=HANDOFF_TIMEOUT,
            )
            await _settled(app)
            await _wait_until(
                lambda: any(
                    message.startswith(
                        f"imported 2 built-in QC metric(s) for {file_row['file_id']} ("
                    )
                    for message in seen
                ),
                "the import notification",
            )

    _run(scenario())
    (message,) = [
        item for item in seen if item.startswith("imported 2 built-in QC metric(s)")
    ]
    assert "0.0.0" in message
    assert (
        _query(
            project,
            "SELECT COUNT(*) AS n FROM qc_results "
            "WHERE metric_name LIKE 'tui_file_ops_%'",
        )[0]["n"]
        == 2
    )


def test_standardize_errors_open_the_error_dialog(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bulk standardize with per-file errors reports them in the error dialog."""
    monkeypatch.setattr(
        actions,
        "standardize",
        lambda *args, **kwargs: {
            "file_id": None,
            "link_kind": "copy",
            "total": 2,
            "results": [{"file_id": "FIL_000001", "action": "skipped"}],
            "errors": [{"file_id": "FIL_000002", "error": "source vanished"}],
        },
    )

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            await _files_panel(pilot, app)
            await pilot.press("S")
            await _wait_until(
                lambda: isinstance(app.screen, StandardizeModal),
                "the standardize dialog",
            )
            await _click(pilot, "#confirm")

            def dialog_ready() -> bool:
                screen = app.screen
                if not isinstance(screen, ErrorDialog):
                    return False
                try:
                    body = _static_text(screen.query_one("#error-dialog-body", Static))
                except NoMatches:
                    return False
                return "source vanished" in body

            await _wait_until(
                dialog_ready, "the standardize error dialog", timeout=HANDOFF_TIMEOUT
            )
            title = _static_text(app.screen.query_one("#modal-title", Label))
            assert "1 of 2 file(s) failed to standardize" in title, title
            await pilot.press("escape")
            await _wait_until(
                lambda: not isinstance(app.screen, ErrorDialog), "the dialog to close"
            )

    _run(scenario())


def test_pipeline_completion_without_reason_codes(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing pipeline run notifies without the reason-code suffix."""
    source = tmp_path / "assembly.fasta"
    source.write_text(">ctg1\nACGTACGTACGT\n", encoding="utf-8")
    monkeypatch.setattr(
        actions,
        "run_pipeline",
        lambda *args, **kwargs: {
            "qc_ok": True,
            "file_id": "FIL_TEST",
            "decision": "PASS",
        },
    )
    seen = _notify_spy(monkeypatch)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            await _files_panel(pilot, app)
            await pilot.press("P")
            await _wait_until(
                lambda: isinstance(app.screen, PipelineModal), "the pipeline dialog"
            )
            modal = app.screen
            modal.query_one("#pipeline-entity-type", Select).value = "assembly"
            modal.query_one("#pipeline-source", Input).value = str(source)
            modal.query_one("#pipeline-entity-id", Input).value = "ASM_000001"
            modal.query_one("#pipeline-role", Input).value = "genome_fasta"
            await pilot.pause()
            await _click(pilot, "#pipeline-preview-button")
            await _wait_until(
                lambda: (
                    not modal.preview_running
                    and modal.query_one("#pipeline-preview-button", Button).disabled
                    is False
                ),
                "the preview to finish",
                timeout=HANDOFF_TIMEOUT,
            )
            assert not modal.query_one("#confirm", Button).disabled
            modal.query_one("#pipeline-yes", Checkbox).value = True
            await pilot.pause()
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: not isinstance(app.screen, PipelineModal),
                "the pipeline dialog to close",
                timeout=HANDOFF_TIMEOUT,
            )
            await _settled(app)
            await _wait_until(
                lambda: "pipeline complete: FIL_TEST -> PASS" in seen,
                "the pipeline notification",
            )

    _run(scenario())
    completion = [item for item in seen if item.startswith("pipeline complete:")]
    assert completion == ["pipeline complete: FIL_TEST -> PASS"]


def test_file_detail_renders_the_overflow_hint_for_large_label_sets(
    project: Project,
) -> None:
    """500 label rows render the summary plus the "first 500 rows" hint."""
    panel = FilesPanel(project)
    detail = {
        "file": {
            "file_id": "FIL_000001",
            "entity_type": "annotation",
            "entity_id": "ANN_000001",
            "file_role": "protein_fasta",
            "format": "fasta",
            "compression": "none",
            "relative_path": "labels.faa",
            "size_bytes": 12,
            "sha256": "0" * 64,
            "status": "CHECKSUM_VERIFIED",
        },
        "locations": [],
        "labels": [{"label": "A", "profile_name": "bhlh"} for _ in range(500)],
    }
    text = panel._detail_text(detail).plain
    assert "(first 500 rows; press l for the full browser)" in text
    assert "(no residency records)" in text


# ---------------------------------------------------------------------------
# Write-modal guards: stray events and preview/confirm gates
# ---------------------------------------------------------------------------


def test_standardize_modal_for_all_files_ignores_other_selects(
    project: Project,
) -> None:
    """The no-file-id form stages everything; a foreign Select.Changed is inert."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)):
            await _settled(app)
            modal = StandardizeModal(project, None)
            app.push_screen(modal)
            await _wait_until(
                lambda: len(modal.query("#standardize-link")) > 0,
                "the standardize form",
            )
            info = _static_text(modal.query_one(".modal-info", Static))
            assert "every verified file" in info, info
            assert modal.command_text() == "operon standardize --link copy"

            # A Changed from a Select this dialog does not own must not
            # overwrite the chosen link kind.
            modal.on_select_changed(
                Select.Changed(Select([("copy", "copy")], id="stray"), "copy")
            )
            assert modal.link_kind == "copy"
            modal.dismiss(None)
            await _wait_until(
                lambda: not isinstance(app.screen, StandardizeModal),
                "the standardize dialog to close",
            )

    _run(scenario())


def test_import_qc_modal_guards_preview_and_confirm(
    project: Project, tmp_path: Path
) -> None:
    """Empty-path previews, foreign input events and the confirm gates stay inline."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = ImportQcModal(project)
            app.push_screen(modal)
            await _wait_until(
                lambda: len(modal.query("#qc-import-preview-button")) > 0,
                "the import form",
            )

            # Previewing with an empty path reports inline and starts no worker.
            await _click(pilot, "#qc-import-preview-button")
            await _wait_until(
                lambda: (
                    "--file" in _static_text(modal.query_one("#modal-error", Static))
                ),
                "the missing-path error",
            )
            assert modal.preview_running is False

            # An Input.Changed from a foreign control changes nothing.
            modal.on_input_changed(Input.Changed(Input(id="stray-input"), "x"))
            assert modal.preview is None

            # Confirm guards: an empty path, then a path without a preview.
            modal.confirm()
            assert "--file" in _static_text(modal.query_one("#modal-error", Static))
            modal.query_one("#qc-import-path", Input).value = str(
                tmp_path / "whatever.json"
            )
            await pilot.pause()
            modal.confirm()
            assert "run the preview first" in _static_text(
                modal.query_one("#modal-error", Static)
            )

            modal.dismiss(None)
            await _wait_until(
                lambda: not isinstance(app.screen, ImportQcModal),
                "the import dialog to close",
            )

    _run(scenario())


def test_pipeline_modal_guards_stray_events_and_missing_source_warning(
    project: Project, tmp_path: Path
) -> None:
    """Foreign events are inert; a missing source previews with a warning."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = PipelineModal(project)
            app.push_screen(modal)
            await _wait_until(
                lambda: len(modal.query("#pipeline-preview-button")) > 0,
                "the pipeline form",
            )

            # Confirm before any preview refuses.
            modal.confirm()
            assert "run the preview first" in _static_text(
                modal.query_one("#modal-error", Static)
            )

            # Changed events from foreign controls must not touch the modal.
            modal.on_input_changed(Input.Changed(Input(id="stray-input"), "x"))
            modal.on_select_changed(
                Select.Changed(
                    Select([("assembly", "assembly")], id="stray-select"), "assembly"
                )
            )
            modal.on_checkbox_changed(
                Checkbox.Changed(Checkbox(id="stray-check"), True)
            )
            assert modal.preview is None

            # A source that does not exist yet previews with a warning.
            modal.query_one("#pipeline-source", Input).value = str(
                tmp_path / "absent.fasta"
            )
            modal.query_one("#pipeline-entity-id", Input).value = "ASM_000001"
            modal.query_one("#pipeline-role", Input).value = "genome_fasta"
            await pilot.pause()
            await _click(pilot, "#pipeline-preview-button")
            await _wait_until(
                lambda: (
                    not modal.preview_running
                    and modal.query_one("#pipeline-preview-button", Button).disabled
                    is False
                ),
                "the preview to finish",
                timeout=HANDOFF_TIMEOUT,
            )
            status = _static_text(modal.query_one("#pipeline-status", Static))
            assert "warning: the source path does not exist yet" in status, status
            assert not modal.query_one("#confirm", Button).disabled

            # Editing the form after a preview invalidates it again.
            modal.query_one("#pipeline-role", Input).value = "other_role"
            await _wait_until(
                lambda: (
                    "run the preview again"
                    in _static_text(modal.query_one("#pipeline-status", Static))
                ),
                "the preview invalidation",
            )
            assert modal.preview is None
            assert modal.query_one("#confirm", Button).disabled

            modal.dismiss(None)
            await _wait_until(
                lambda: not isinstance(app.screen, PipelineModal),
                "the pipeline dialog to close",
            )

    _run(scenario())


def test_qc_modal_ignores_stray_events_and_confirm_while_running(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Foreign events are inert; a second Confirm mid-run is a no-op."""
    import threading

    released = threading.Event()
    started = threading.Event()

    def blocking_run_qc(*args, **kwargs):
        started.set()
        released.wait(HANDOFF_TIMEOUT)
        return [{"file_id": "FIL_000001", "ok": True}]

    monkeypatch.setattr(actions, "run_qc", blocking_run_qc)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)):
            await _settled(app)
            modal = QcModal(project, "FIL_000001", 1)
            app.push_screen(modal)
            await _wait_until(
                lambda: len(modal.query("#qc-progress")) > 0, "the QC form"
            )

            modal.on_input_changed(Input.Changed(Input(id="stray-input"), "x"))
            modal.on_select_changed(
                Select.Changed(Select([("33", "33")], id="stray-select"), "33")
            )
            modal.on_checkbox_changed(
                Checkbox.Changed(Checkbox(id="stray-check"), True)
            )
            assert modal.running is False

            modal.confirm()
            await _wait_until(lambda: modal.running, "the QC worker to start")
            await _wait_until(
                started.is_set,
                "the QC worker to reach the core",
                timeout=HANDOFF_TIMEOUT,
            )
            worker = modal._worker
            modal.confirm()  # a second Confirm while running must be a no-op
            assert modal.running is True
            assert modal._worker is worker

            released.set()
            await _wait_until(
                lambda: not isinstance(app.screen, QcModal),
                "the QC dialog to close",
                timeout=HANDOFF_TIMEOUT,
            )
            await _settled(app)

    try:
        _run(scenario())
    finally:
        released.set()
