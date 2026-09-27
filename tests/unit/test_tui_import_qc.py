"""The Files screen's ``I`` entry for `operon import-qc` (milestone M4).

``import-qc`` brings external QC metrics into the manifest database: either a
``qc-measure`` JSON payload (the output of the project-independent measurement
path) or an external TSV table.  The dialog is the table-import shape — a path,
a **mandatory preview** that parses and validates against the manifest without
writing, and a Confirm that only unlocks once the preview passed.  Both the
preview and the write call :mod:`operon.qc.imports`, the same core the CLI
uses, so the rows and the recorded provenance are identical.
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
from textual.widgets import Button, DataTable, Input, Static

from operon import __version__
from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.qc import MEASURE_SCHEMA_VERSION, TOOL_NAME
from operon.tui import actions
from operon.tui.app import OperonApp
from operon.tui.screens.files import FilesPanel
from operon.tui.screens.files_ops import ImportQcModal
from tests.tui_helpers import click as _click

SCENARIO_TIMEOUT = 180.0
SETTLE_TIMEOUT = 30.0
#: Budget for a worker result crossing back from its thread to the UI (ODR-0046).
HANDOFF_TIMEOUT = 120.0

TSV_HEADER = ("entity_type\tentity_id\tqc_stage\tmetric_name\tmetric_value\t"
              "tool\ttool_version\tparameter_set\n")


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-import-qc-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    target = tmp_path / "project"
    shutil.copytree(demo_template.root, target)
    return Project.find(target)


@pytest.fixture
def inputs(tmp_path: Path) -> Path:
    directory = tmp_path / "inputs"
    directory.mkdir()
    (directory / "metrics.tsv").write_text(
        TSV_HEADER
        + "assembly\tASM_000001\tmapping\tcoverage_mean\t31.5\tbwa\t0.7.17\tdefault\n"
        + "assembly\tASM_000001\tmapping\tmapped_reads\t900000\tbwa\t0.7.17\tdefault\n",
        encoding="utf-8")
    (directory / "missing-columns.tsv").write_text(
        "entity_type\tentity_id\nassembly\tASM_000001\n", encoding="utf-8")
    return directory


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


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


def _query(project: Project, sql: str, params: tuple = ()) -> list[dict]:
    db = Database(project.db_path, read_only=True)
    try:
        return [dict(row) for row in db.query(sql, params)]
    finally:
        db.close()


def _qc_metric_count(project: Project) -> int:
    return _query(project, "SELECT COUNT(*) AS n FROM qc_results")[0]["n"]


def _import_runs(project: Project) -> list[dict]:
    return _query(project, "SELECT * FROM workflow_runs WHERE step='import-qc'")


def _import_run_count(project: Project) -> int:
    return len(_import_runs(project))


async def _open_modal(pilot, app) -> ImportQcModal:
    """Open the Files screen's ``I`` modal."""
    app.action_switch_screen("files")
    await pilot.pause()
    await _settled(app)
    app.query_one(FilesPanel).query_one("#files-table", DataTable).focus()
    await pilot.pause()
    await pilot.press("I")
    await pilot.pause()
    modal = app.screen
    assert isinstance(modal, ImportQcModal), modal
    return modal


async def _type_path(pilot, modal: ImportQcModal, path: Path) -> None:
    field = modal.query_one("#qc-import-path", Input)
    field.focus()
    await pilot.pause()
    field.value = str(path)
    await pilot.pause()


async def _preview(pilot, modal: ImportQcModal) -> None:
    await _click(pilot, "#qc-import-preview-button")
    await _wait_until(
        lambda: not modal.preview_running and modal.query_one(
            "#qc-import-preview-button", Button).disabled is False,
        "finish the preview", timeout=HANDOFF_TIMEOUT)


def test_import_qc_requires_the_preview_then_writes_with_provenance(
        project: Project, inputs: Path) -> None:
    """Confirm stays locked until the preview passed; Confirm then imports."""
    before = _qc_metric_count(project)
    runs_before = _import_run_count(project)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            modal = await _open_modal(pilot, app)
            assert modal.query_one("#confirm", Button).disabled

            await _type_path(pilot, modal, inputs / "metrics.tsv")
            command = _static_text(modal.query_one("#modal-command", Static))
            assert "operon import-qc --file" in command and "metrics.tsv" in command, command

            await _preview(pilot, modal)
            status = _static_text(modal.query_one("#qc-import-status", Static))
            assert "format: tsv" in status, status
            assert "metrics: 2" in status, status
            assert "assembly ASM_000001" in status, status
            assert not modal.query_one("#confirm", Button).disabled

            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: not isinstance(app.screen, ImportQcModal),
                "import modal closed", timeout=HANDOFF_TIMEOUT)
            await _settled(app)
            assert ("information", "imported 2 external QC metric(s)") in [
                (notification.severity, notification.message)
                for notification in app._notifications]

    _run(scenario())

    rows = _query(project, "SELECT * FROM qc_results WHERE tool='bwa'")
    assert len(rows) == 2
    assert {row["qc_stage"] for row in rows} == {"mapping"}
    assert {row["input_identity"] for row in rows} == {"entity:assembly:ASM_000001"}
    assert _qc_metric_count(project) == before + 2
    runs = _import_runs(project)
    assert len(runs) == runs_before + 1
    assert json.loads(runs[-1]["execution_details"])["metric_count"] == 2
    assert json.loads(runs[-1]["execution_details"])["format"] == "tsv"


def test_import_qc_path_edit_invalidates_the_preview(project: Project, inputs: Path) -> None:
    """Editing the path after a preview locks Confirm again."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            modal = await _open_modal(pilot, app)
            await _type_path(pilot, modal, inputs / "metrics.tsv")
            await _preview(pilot, modal)
            assert not modal.query_one("#confirm", Button).disabled

            field = modal.query_one("#qc-import-path", Input)
            field.value = str(inputs / "missing-columns.tsv")
            await pilot.pause()
            assert modal.query_one("#confirm", Button).disabled
            status = _static_text(modal.query_one("#qc-import-status", Static))
            assert "run the preview again" in status, status

    _run(scenario())
    assert _query(project, "SELECT 1 FROM qc_results WHERE tool='bwa'") == []


def test_import_qc_invalid_input_stays_inline(project: Project, inputs: Path) -> None:
    """A table the core refuses is reported inline and writes nothing."""
    before = _qc_metric_count(project)
    runs_before = _import_run_count(project)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            modal = await _open_modal(pilot, app)
            await _type_path(pilot, modal, inputs / "missing-columns.tsv")
            await _preview(pilot, modal)
            error = _static_text(modal.query_one("#modal-error", Static))
            assert "missing columns" in error, error
            assert modal.query_one("#confirm", Button).disabled
            assert modal.preview is None

    _run(scenario())
    assert _qc_metric_count(project) == before
    assert _import_run_count(project) == runs_before


def test_import_qc_preview_writes_nothing(project: Project, inputs: Path) -> None:
    """The preview is read-only: no metrics and no run record."""
    before = _qc_metric_count(project)
    runs_before = _import_run_count(project)
    result = actions.qc_import_preview(project, str(inputs / "metrics.tsv"))

    assert result["format"] == "tsv"
    assert result["metric_count"] == 2
    assert result["entities"] == [("assembly", "ASM_000001")]
    assert _qc_metric_count(project) == before
    assert _import_run_count(project) == runs_before


def test_import_qc_json_payload_reports_a_version_mismatch(project: Project,
                                                           tmp_path: Path) -> None:
    """A qc-measure payload imports by file identity and warns on tool drift."""
    file_row = _query(project, "SELECT * FROM files WHERE status='STANDARDIZED' "
                               "ORDER BY file_id")[0]
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
            {"qc_stage": "assembled", "metric_name": "tui_probe_n50", "metric_value": "1234",
             "metric_numeric": 1234.0, "metric_unit": "bp"},
            {"qc_stage": "assembled", "metric_name": "tui_probe_total_length",
             "metric_value": "5000", "metric_numeric": 5000.0, "metric_unit": "bp"},
        ],
    }
    source = tmp_path / "measured.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    before = _qc_metric_count(project)

    preview = actions.qc_import_preview(project, str(source))
    assert preview["format"] == "json"
    assert preview["file_id"] == file_row["file_id"]
    assert preview["metric_count"] == 2
    assert preview["stages"] == ["assembled"]
    assert f"this installation is {__version__}" in preview["warning"]
    assert _qc_metric_count(project) == before

    result = actions.import_qc(project, str(source))

    assert result["metric_count"] == 2
    assert "0.0.0" in result["warning"]
    rows = _query(project, "SELECT * FROM qc_results WHERE file_id=? AND "
                           "metric_name LIKE 'tui_probe_%'", (file_row["file_id"],))
    assert len(rows) == 2
    assert _qc_metric_count(project) == before + 2
    assert {row["tool"] for row in rows} == {TOOL_NAME}
    assert {row["metric_name"] for row in rows} == {"tui_probe_n50", "tui_probe_total_length"}
