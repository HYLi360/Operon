"""NCBI Datasets TUI import: ``actions.ncbi_datasets`` and the ``NcbiDatasetsModal``.

The action mirrors ``operon ncbi-datasets`` (validation first, writes only
through the core) and the modal gates Confirm behind a mandatory dry-run
preflight and cancels cooperatively through a ``threading.Event`` — the run is
then recorded as ``interrupted`` and stays resumable, exactly like a SIGINT.
"""

from __future__ import annotations

import asyncio
import json
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import Button, Checkbox, Input, RichLog, Static

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ValidationError
from operon.shutdown import ShutdownRequested
from operon.tui import actions
from operon.tui.app import OperonApp
from operon.tui.screens.home import HomePanel
from operon.tui.screens.import_wizard import ImportWizardScreen
from operon.tui.screens.ncbi_datasets import NcbiDatasetsModal
from tests import helpers
from tests.tui_helpers import click as _click

SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 30.0

ACCESSION = "GCF_000009999.1"  # not one of the demo project's own accessions


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-ncbi-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    """Each test gets its own writable copy of the demo project."""
    return Project.find(helpers.copy_project_tree(demo_template.root, tmp_path / "project"))


def _report(accession: str = ACCESSION) -> dict:
    return {
        "accession": accession,
        "currentAccession": accession,
        "organism": {
            "organismName": "Homo sapiens",
            "taxId": 9606,
            "infraspecificNames": {"strain": "GRCh38", "sex": "male"},
        },
        "assemblyInfo": {
            "assemblyLevel": "Chromosome",
            "assemblyMethod": "multiple methods",
            "biosample": {
                "accession": "SAMN00000001",
                "attributes": [
                    {"name": "collection_date", "value": "2020-02-03"},
                    {"name": "geo_loc_name", "value": "USA"},
                    {"name": "lat_lon", "value": "38.9 N 77.0 W"},
                ],
            },
            "bioprojectAccession": "PRJNA31257",
            "pairedAssembly": {"accession": "GCA_000001405.29"},
            "refseqCategory": "reference genome",
            "releaseDate": "2022-02-03T00:00:00Z",
            "submitter": "Genome Reference Consortium",
        },
        "annotationInfo": {
            "provider": "NCBI RefSeq",
            "version": 110,
            "releaseDate": "2023-10-01",
        },
    }


def _write_report(path: Path, accession: str = ACCESSION) -> Path:
    path.write_text(json.dumps(_report(accession)) + "\n", encoding="utf-8")
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


def _log_text(log: RichLog) -> str:
    """The plain text a RichLog currently holds."""
    return "\n".join(strip.text for strip in log.lines)


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
# actions.ncbi_datasets
# ---------------------------------------------------------------------------


def test_dry_run_offline_import_writes_nothing(project: Project, tmp_path: Path) -> None:
    report = _write_report(tmp_path / "report.jsonl")
    before = {table: _count(project, table)
              for table in ("workflow_runs", "changes", "assemblies", "organisms")}
    result = actions.ncbi_datasets(project, inputs=[str(report)], dry_run=True)
    assert result["run_id"]
    assert len(result["sources"]) == 1
    assert result["assembly_records"] == 1
    assert result["download_plan"] == []
    assert result["skipped_existing"] == []
    assert "messages" in result
    for table, count in before.items():
        assert _count(project, table) == count, f"dry run wrote into {table}"


def test_plan_only_accession_is_offline_and_write_free(project: Project) -> None:
    before_runs = _count(project, "workflow_runs")
    result = actions.ncbi_datasets(project, accessions=[ACCESSION], plan_only=True)
    assert result["plan_only"] is True
    groups = result["download_plan"]
    assert [a for group in groups for a in group["accessions"]] == [ACCESSION]
    assert _count(project, "workflow_runs") == before_runs


def test_real_offline_import_records_completed_run(project: Project, tmp_path: Path) -> None:
    report = _write_report(tmp_path / "report.jsonl")
    result = actions.ncbi_datasets(project, inputs=[str(report)])
    rows = _query(project,
                  "SELECT step, status, exit_code FROM workflow_runs WHERE run_id=?",
                  (result["run_id"],))
    assert rows == [{"step": "ncbi_datasets_import", "status": "completed", "exit_code": 0}]
    assert result["assembly_records"] == 1


def test_validation_requires_some_input(project: Project) -> None:
    before = _count(project, "workflow_runs")
    with pytest.raises(ValidationError,
                       match="provide at least one --input, --accession, or --accession-file"):
        actions.ncbi_datasets(project)
    assert _count(project, "workflow_runs") == before


def test_validation_plan_only_rejects_offline_inputs(project: Project, tmp_path: Path) -> None:
    report = _write_report(tmp_path / "report.jsonl")
    with pytest.raises(ValidationError,
                       match="--plan-only supports accession requests"):
        actions.ncbi_datasets(project, inputs=[str(report)], plan_only=True)


def test_validation_rejects_bad_accession(project: Project) -> None:
    with pytest.raises(ValidationError, match="invalid NCBI assembly accession"):
        actions.ncbi_datasets(project, accessions=["not-an-accession"])


def test_validation_rejects_unknown_include(project: Project) -> None:
    with pytest.raises(ValidationError, match="unknown NCBI include type"):
        actions.ncbi_datasets(project, accessions=[ACCESSION], include=["bogus"])


@pytest.mark.parametrize("kwargs", [
    {"batch_size": 0},
    {"batch_size": 101},
    {"download_workers": 0},
    {"download_workers": 11},
    {"retries": -1},
    {"retries": 11},
    {"retry_backoff": -0.5},
    {"timeout": 0},
    {"timeout": -1.0},
])
def test_validation_rejects_out_of_range_numbers(project: Project, kwargs: dict) -> None:
    before = _count(project, "workflow_runs")
    with pytest.raises(ValidationError):
        actions.ncbi_datasets(project, accessions=[ACCESSION], plan_only=True, **kwargs)
    assert _count(project, "workflow_runs") == before


def test_cancel_event_records_interrupted_run(project: Project) -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    before = _count(project, "workflow_runs")
    with pytest.raises(actions.NcbiDatasetsCancelled):
        actions.ncbi_datasets(project, accessions=[ACCESSION], cancel_event=cancel_event)
    rows = _query(project,
                  "SELECT step, status, exit_code FROM workflow_runs "
                  "WHERE step='ncbi_datasets_import'")
    assert _count(project, "workflow_runs") == before + 1
    assert rows == [{"step": "ncbi_datasets_import", "status": "interrupted",
                     "exit_code": 130}]


def test_shutdown_without_cancel_event_propagates(project: Project, monkeypatch) -> None:
    """A signal-style interrupt not caused by the TUI's event keeps its type."""

    def interrupted(*args, **kwargs):
        raise ShutdownRequested(signal.SIGINT)

    monkeypatch.setattr("operon.adapters.ncbi_datasets.run_ncbi_datasets_adapter", interrupted)
    with pytest.raises(ShutdownRequested):
        actions.ncbi_datasets(project, accessions=[ACCESSION],
                              cancel_event=threading.Event())


# ---------------------------------------------------------------------------
# NcbiDatasetsModal
# ---------------------------------------------------------------------------


def _preflight_payload() -> dict:
    return {
        "run_id": "WF_000099",
        "sources": [],
        "assembly_records": 0,
        "download_plan": [{"includes": ["genome"], "accessions": [ACCESSION]}],
        "skipped_existing": [],
        "messages": "",
    }


def test_home_screen_opens_modal_and_wizard(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            assert isinstance(app.screen.query_one("#home", HomePanel), HomePanel)

            await _click(pilot, "#home-ncbi-datasets")
            await _wait_until(lambda: isinstance(app.screen, NcbiDatasetsModal),
                              "NCBI Datasets modal to open")
            app.pop_screen()
            await _wait_until(lambda: not isinstance(app.screen, NcbiDatasetsModal),
                              "modal to close")

            await _click(pilot, "#home-import-wizard")
            await _wait_until(lambda: isinstance(app.screen, ImportWizardScreen),
                              "import wizard to open")

    _run(scenario())


def test_preflight_gate_unlocks_confirm_and_real_run_writes(project: Project,
                                                            tmp_path: Path) -> None:
    report = _write_report(tmp_path / "report.jsonl")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-inputs")

            confirm = await _q(modal, "#confirm", Button)
            assert confirm.disabled, "Confirm must be gated behind the preflight"

            (await _q(modal, "#ncbi-inputs", Input)).value = str(report)
            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(lambda: "preflight clean" in _log_text(
                modal.query_one("#ncbi-preview", RichLog)), "preflight to finish")
            assert not confirm.disabled

            # Any form change locks Confirm again until the next dry run.
            (await _q(modal, "#ncbi-include", Input)).value = "genome"
            await _wait_until(lambda: confirm.disabled, "form change to lock Confirm")
            assert "form changed" in _log_text(modal.query_one("#ncbi-preview", RichLog))

            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(lambda: "preflight clean" in _log_text(
                modal.query_one("#ncbi-preview", RichLog)), "second preflight to finish")
            assert not confirm.disabled

            await _click(pilot, "#confirm")
            await _wait_until(lambda: app.screen is not modal, "modal to dismiss")
            assert any("NCBI Datasets import" in message
                       for _, message in _notifications(app))

    _run(scenario())
    rows = _query(project,
                  "SELECT step, status FROM workflow_runs WHERE step='ncbi_datasets_import'")
    assert [row["status"] for row in rows] == ["completed"]


def test_command_text_matches_action_kwargs(project: Project, monkeypatch) -> None:
    calls: list[tuple[tuple, dict]] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return _preflight_payload()

    monkeypatch.setattr(actions, "ncbi_datasets", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-accessions")
            (await _q(modal, "#ncbi-accessions", Input)).value = ACCESSION
            (await _q(modal, "#ncbi-include", Input)).value = "genome, gff3"
            (await _q(modal, "#ncbi-email", Input)).value = "me@example.org"
            (await _q(modal, "#ncbi-timeout", Input)).value = "60"
            (await _q(modal, "#ncbi-batch-size", Input)).value = "5"
            (await _q(modal, "#ncbi-download-workers", Input)).value = "2"
            (await _q(modal, "#ncbi-retries", Input)).value = "1"
            (await _q(modal, "#ncbi-retry-backoff", Input)).value = "0.5"
            (await _q(modal, "#ncbi-standardize", Checkbox)).value = True
            await pilot.pause()

            ns = parse_command_text(modal.command_text())
            assert ns.accession == [ACCESSION]
            assert ns.include == ["genome", "gff3"]
            assert ns.email == "me@example.org"
            assert ns.timeout == 60.0
            assert ns.batch_size == 5 and ns.download_workers == 2
            assert ns.retries == 1 and ns.retry_backoff == 0.5
            assert ns.standardize is True and ns.dry_run is False
            assert ns.plan_only is False and ns.no_archive_files is False

            # Accession-only form: the preflight is the online plan, no writes.
            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(lambda: bool(calls), "preflight call")
            await _wait_until(lambda: "preflight clean" in _log_text(
                modal.query_one("#ncbi-preview", RichLog)), "preflight preview")

            await _click(pilot, "#confirm")
            await _wait_until(lambda: len(calls) == 2, "confirm call")
            await _wait_until(lambda: app.screen is not modal, "modal to dismiss")

    _run(scenario())
    preflight_kwargs = calls[0][1]
    assert preflight_kwargs["plan_only"] is True
    assert preflight_kwargs["dry_run"] is False
    assert "cancel_event" not in preflight_kwargs
    confirm_kwargs = calls[1][1]
    assert isinstance(confirm_kwargs.pop("cancel_event"), threading.Event)
    assert confirm_kwargs == {
        "inputs": [],
        "accessions": [ACCESSION],
        "accession_file": None,
        "include": ["genome", "gff3"],
        "archive_files": True,
        "standardize": True,
        "dry_run": False,
        "preserve_sources": True,
        "email": "me@example.org",
        "api_key": None,
        "resume_run_id": None,
        "plan_only": False,
        "timeout": 60.0,
        "batch_size": 5,
        "download_workers": 2,
        "retries": 1,
        "retry_backoff": 0.5,
    }


def test_invalid_number_shows_inline_error_before_preflight(project: Project,
                                                            monkeypatch) -> None:
    def stub(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("preflight must not run with an invalid form")

    monkeypatch.setattr(actions, "ncbi_datasets", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-inputs")
            (await _q(modal, "#ncbi-inputs", Input)).value = "/tmp/whatever.jsonl"
            (await _q(modal, "#ncbi-timeout", Input)).value = "abc"
            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(
                lambda: "timeout must be a number"
                        in _static_text(modal.query_one("#modal-error", Static)),
                "inline error to appear")
            assert (await _q(modal, "#confirm", Button)).disabled

    _run(scenario())


def test_failed_preflight_keeps_confirm_locked(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-accessions")
            (await _q(modal, "#ncbi-accessions", Input)).value = ACCESSION
            (await _q(modal, "#ncbi-include", Input)).value = "bogus"
            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(lambda: "dry run failed" in _log_text(
                modal.query_one("#ncbi-preview", RichLog)), "preflight failure preview")
            assert "unknown NCBI include type" in _log_text(
                modal.query_one("#ncbi-preview", RichLog))
            assert (await _q(modal, "#confirm", Button)).disabled

    _run(scenario())


def test_cancel_sets_event_and_unlocks_controls(project: Project, monkeypatch) -> None:
    def stub(*args, **kwargs):
        if kwargs.get("plan_only"):
            return _preflight_payload()
        cancel_event = kwargs["cancel_event"]
        deadline = time.monotonic() + 20.0
        while not cancel_event.is_set():
            if time.monotonic() > deadline:
                raise AssertionError("cancel_event was never set")
            time.sleep(0.05)
        raise actions.NcbiDatasetsCancelled()

    monkeypatch.setattr(actions, "ncbi_datasets", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-accessions")
            (await _q(modal, "#ncbi-accessions", Input)).value = ACCESSION
            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(lambda: "preflight clean" in _log_text(
                modal.query_one("#ncbi-preview", RichLog)), "preflight to finish")

            await _click(pilot, "#confirm")
            await _wait_until(lambda: modal.running, "run to start")
            assert (await _q(modal, "#ncbi-accessions", Input)).disabled
            assert "running" in _static_text(modal.query_one("#ncbi-status", Static))

            await _click(pilot, "#cancel")
            await _wait_until(
                lambda: "cancelled" == _static_text(modal.query_one("#ncbi-status", Static)),
                "cancelled status")
            assert not modal.running
            assert not (await _q(modal, "#ncbi-accessions", Input)).disabled
            assert not (await _q(modal, "#confirm", Button)).disabled
            assert any(severity == "warning" and "cancelled" in message
                       for severity, message in _notifications(app))

    _run(scenario())


def test_stale_preflight_result_is_discarded(project: Project, monkeypatch) -> None:
    gate = threading.Event()

    def stub(*args, **kwargs):
        assert gate.wait(timeout=20.0), "test never released the preflight"
        return _preflight_payload()

    monkeypatch.setattr(actions, "ncbi_datasets", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-accessions")
            (await _q(modal, "#ncbi-accessions", Input)).value = ACCESSION
            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(lambda: modal.preflight_running, "preflight to start")

            # The form moves on while the preflight is still in flight.
            (await _q(modal, "#ncbi-accessions", Input)).value = f"{ACCESSION}, GCF_000008888.1"
            gate.set()
            await _wait_until(lambda: "form changed during the dry run" in _log_text(
                modal.query_one("#ncbi-preview", RichLog)), "stale preflight notice")
            assert modal.preflight is None
            assert (await _q(modal, "#confirm", Button)).disabled

    _run(scenario())


def test_tick_status_stops_timer_once_not_running(project: Project) -> None:
    modal = NcbiDatasetsModal(project)
    modal.running = False

    class _Timer:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    timer = _Timer()
    modal._status_timer = timer
    modal._tick_status()
    assert timer.stopped
    assert modal._status_timer is None
