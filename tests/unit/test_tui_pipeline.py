"""The Files screen's ``P`` entry for `operon run-pipeline` (milestone M4).

The dialog takes one source file through the pipeline's four stages
(ingest → standardize → QC → evaluate).  It follows the table-import shape — a
form, a **mandatory preview** that resolves the profile and checks the entity
without writing, and a Confirm that only unlocks once the preview passed — and
it replaces the CLI's ``--yes`` prompt with an explicit re-evaluation checkbox
when evaluation would overwrite a curated decision.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import Button, Checkbox, DataTable, Input, Static

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.tui.app import OperonApp
from operon.tui.screens.common import ErrorDialog
from operon.tui.screens.files import FilesPanel
from operon.tui.screens.files_ops import PipelineModal

SCENARIO_TIMEOUT = 180.0
SETTLE_TIMEOUT = 30.0
#: Budget for a worker result crossing back from its thread to the UI (ODR-0046).
HANDOFF_TIMEOUT = 120.0


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-pipeline-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    target = tmp_path / "project"
    shutil.copytree(demo_template.root, target)
    return Project.find(target)


def _fresh_assembly(project: Project, assembly_id: str = "ASM_000900") -> None:
    """A demo-shaped assembly with no entity_state row yet (a new entity)."""
    db = Database(project.db_path)
    try:
        db.insert_row("assemblies", {
            "assembly_id": assembly_id,
            "sample_id": "SMP_000001",
            "assembly_accession": f"GCA_{assembly_id[-6:]}",
            "assembly_version": 1,
            "assembly_level": "contig",
            "assembly_method": "test fixture",
            "reference_status": "representative",
        })
    finally:
        db.close()


def _curate(project: Project, assembly_id: str) -> None:
    db = Database(project.db_path)
    try:
        profile = project.config["qc"]["default_profile"]
        db.upsert_decision({
            "entity_type": "assembly", "entity_id": assembly_id, "profile": profile,
            "decision": "PASS", "curated_decision": "PASS", "curated_by": "reviewer",
            "reason_codes": "[]", "observed": "{}", "thresholds": "{}",
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        })
    finally:
        db.close()


def _source(tmp_path: Path, name: str = "pipeline.fasta") -> Path:
    path = tmp_path / name
    path.write_text(">ctg1\n" + "ACGTACGTACGT" * 25 + "\n", encoding="utf-8")
    return path


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
    """Click a widget, clearing a lingering press effect first (ODR-0024)."""
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
    return [(item.severity, item.message) for item in app._notifications]


def _query(project: Project, sql: str, params: tuple = ()) -> list[dict]:
    db = Database(project.db_path, read_only=True)
    try:
        return [dict(row) for row in db.query(sql, params)]
    finally:
        db.close()


async def _open_modal(pilot, app) -> PipelineModal:
    app.action_switch_screen("files")
    await pilot.pause()
    await _settled(app)
    app.query_one(FilesPanel).query_one("#files-table", DataTable).focus()
    await pilot.pause()
    await pilot.press("P")
    await pilot.pause()
    modal = app.screen
    assert isinstance(modal, PipelineModal), modal
    return modal


async def _fill(pilot, modal: PipelineModal, source: Path, entity_id: str,
                role: str = "genome_fasta") -> None:
    field = modal.query_one("#pipeline-source", Input)
    field.focus()
    await pilot.pause()
    field.value = str(source)
    modal.query_one("#pipeline-entity-id", Input).value = entity_id
    modal.query_one("#pipeline-role", Input).value = role
    await pilot.pause()


async def _preview(pilot, modal: PipelineModal) -> None:
    await _click(pilot, "#pipeline-preview-button")
    await _wait_until(
        lambda: not modal.preview_running and modal.query_one(
            "#pipeline-preview-button", Button).disabled is False,
        "finish the preview", timeout=HANDOFF_TIMEOUT)


def test_pipeline_preview_gates_confirm_and_runs_the_four_stages(
        project: Project, tmp_path: Path) -> None:
    """A full run through the dialog: preview, Confirm, and the same artifacts."""
    _fresh_assembly(project)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 50)) as pilot:
            modal = await _open_modal(pilot, app)
            assert modal.query_one("#confirm", Button).disabled

            await _fill(pilot, modal, _source(tmp_path), "ASM_000900")
            command = _static_text(modal.query_one("#modal-command", Static))
            assert "operon run-pipeline --source" in command, command
            assert "--entity-id ASM_000900" in command, command
            assert "--profile assembly_production_v1" in command, command

            await _preview(pilot, modal)
            status = _static_text(modal.query_one("#pipeline-status", Static))
            assert "profile: assembly_production_v1" in status, status
            assert "steps: ingest → standardize → qc → evaluate" in status, status
            assert "entity: assembly ASM_000900" in status, status
            assert not modal.query_one("#confirm", Button).disabled

            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: not isinstance(app.screen, PipelineModal),
                "pipeline modal closed", timeout=HANDOFF_TIMEOUT)
            await _settled(app)
            messages = [message for _severity, message in _notifications(app)]
            assert any(message.startswith("pipeline complete: FIL_") for message in messages), messages

    _run(scenario())

    files = _query(project, "SELECT * FROM files WHERE entity_id='ASM_000900'")
    assert len(files) == 1
    assert files[0]["status"] == "STANDARDIZED"
    assert _query(project, "SELECT 1 FROM qc_results WHERE file_id=?",
                  (files[0]["file_id"],))
    assert _query(project, "SELECT 1 FROM current_decisions WHERE entity_id='ASM_000900'")
    db = Database(project.db_path, read_only=True)
    try:
        assert db.get_entity_state("assembly", "ASM_000900") in {"ACCEPTED", "REVIEW", "REJECTED"}
    finally:
        db.close()


def test_pipeline_curated_decision_needs_the_explicit_rerun_box(
        project: Project, tmp_path: Path) -> None:
    """The CLI's `--yes` becomes an explicit checkbox, exactly like its prompt."""
    _fresh_assembly(project, "ASM_000901")
    _curate(project, "ASM_000901")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 50)) as pilot:
            modal = await _open_modal(pilot, app)
            await _fill(pilot, modal, _source(tmp_path, "curated.fasta"), "ASM_000901")
            await _preview(pilot, modal)
            status = _static_text(modal.query_one("#pipeline-status", Static))
            assert "curated decision" in status, status

            # Without the box the Confirm refuses and stays inline.
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "curated" in _static_text(modal.query_one("#modal-error", Static)),
                "the curated gate to appear inline", timeout=HANDOFF_TIMEOUT)
            assert isinstance(app.screen, PipelineModal)

            # With the box the run proceeds.
            modal.query_one("#pipeline-yes", Checkbox).value = True
            await pilot.pause()
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: not isinstance(app.screen, PipelineModal),
                "pipeline modal closed", timeout=HANDOFF_TIMEOUT)
            await _settled(app)
            messages = [message for _severity, message in _notifications(app)]
            assert any(message.startswith("pipeline complete:") for message in messages), messages

    _run(scenario())
    assert _query(project, "SELECT 1 FROM files WHERE entity_id='ASM_000901'")


def test_pipeline_preview_requires_source_entity_and_role(project: Project,
                                                          tmp_path: Path) -> None:
    """Missing required fields are reported inline and never unlock Confirm."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 50)) as pilot:
            modal = await _open_modal(pilot, app)
            modal.query_one("#pipeline-entity-id", Input).value = "ASM_000900"
            modal.query_one("#pipeline-role", Input).value = "genome_fasta"
            await pilot.pause()
            await _click(pilot, "#pipeline-preview-button")
            await _wait_until(
                lambda: "source is required" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "the missing-source error", timeout=HANDOFF_TIMEOUT)
            assert modal.query_one("#confirm", Button).disabled

            modal.query_one("#pipeline-source", Input).value = str(_source(tmp_path))
            modal.query_one("#pipeline-entity-id", Input).value = "ASM_000000"
            await pilot.pause()
            await _preview(pilot, modal)
            assert _static_text(modal.query_one("#modal-error", Static))
            assert modal.query_one("#confirm", Button).disabled

    _run(scenario())


def test_pipeline_qc_failure_opens_the_error_dialog(project: Project, tmp_path: Path,
                                                    monkeypatch) -> None:
    """A QC stop is reported the way the CLI reports it: no evaluation, an error."""
    from operon.tui import actions

    _fresh_assembly(project, "ASM_000902")
    monkeypatch.setattr(actions, "run_pipeline", lambda *_a, **_k: {
        "qc_ok": False, "qc_error": "synthetic QC failure", "file_id": "FIL_TEST",
        "decision": None, "reason_codes": [], "profile": "assembly_production_v1",
        "steps": ["ingest", "standardize", "qc", "evaluate"],
    })

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 50)) as pilot:
            modal = await _open_modal(pilot, app)
            await _fill(pilot, modal, _source(tmp_path, "broken.fasta"), "ASM_000902")
            await _preview(pilot, modal)
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: isinstance(app.screen, ErrorDialog),
                "the QC error dialog", timeout=HANDOFF_TIMEOUT)
            body = _static_text(app.screen.query_one("#error-dialog-body", Static))
            assert "synthetic QC failure" in body, body
            assert not [message for _s, message in _notifications(app)
                        if message.startswith("pipeline complete:")]

    _run(scenario())
