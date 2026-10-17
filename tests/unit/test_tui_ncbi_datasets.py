#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
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
    return Project.find(
        helpers.copy_project_tree(demo_template.root, tmp_path / "project")
    )


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


async def _wait_until(
    predicate: Callable[[], bool], description: str, timeout: float = SETTLE_TIMEOUT
) -> None:
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


async def _await_notification(app, needle: str = "", severity: str = "") -> None:
    """Wait for a raised notification matching ``needle`` and/or ``severity``.

    ``App.notify`` queues a message, so a callback that has already updated its
    widgets raises the notification a message-loop turn later: reading
    ``app._notifications`` straight after the widgets turns a correct flow into
    an intermittent failure on a slow interpreter (ODR-70).
    """
    await _wait_until(
        lambda: any(
            (not severity or sev == severity) and (not needle or needle in message)
            for sev, message in _notifications(app)
        ),
        f"the notification {needle or severity!r}",
    )


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


def test_dry_run_offline_import_writes_nothing(
    project: Project, tmp_path: Path
) -> None:
    report = _write_report(tmp_path / "report.jsonl")
    before = {
        table: _count(project, table)
        for table in ("workflow_runs", "changes", "assemblies", "organisms")
    }
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


def test_real_offline_import_records_completed_run(
    project: Project, tmp_path: Path
) -> None:
    report = _write_report(tmp_path / "report.jsonl")
    result = actions.ncbi_datasets(project, inputs=[str(report)])
    rows = _query(
        project,
        "SELECT step, status, exit_code FROM workflow_runs WHERE run_id=?",
        (result["run_id"],),
    )
    assert rows == [
        {"step": "ncbi_datasets_import", "status": "completed", "exit_code": 0}
    ]
    assert result["assembly_records"] == 1


def test_validation_requires_some_input(project: Project) -> None:
    before = _count(project, "workflow_runs")
    with pytest.raises(
        ValidationError,
        match="provide at least one --input, --accession, or --accession-file",
    ):
        actions.ncbi_datasets(project)
    assert _count(project, "workflow_runs") == before


def test_validation_plan_only_rejects_offline_inputs(
    project: Project, tmp_path: Path
) -> None:
    report = _write_report(tmp_path / "report.jsonl")
    with pytest.raises(
        ValidationError, match="--plan-only supports accession requests"
    ):
        actions.ncbi_datasets(project, inputs=[str(report)], plan_only=True)


def test_validation_rejects_bad_accession(project: Project) -> None:
    with pytest.raises(ValidationError, match="invalid NCBI assembly accession"):
        actions.ncbi_datasets(project, accessions=["not-an-accession"])


def test_validation_rejects_unknown_include(project: Project) -> None:
    with pytest.raises(ValidationError, match="unknown NCBI include type"):
        actions.ncbi_datasets(project, accessions=[ACCESSION], include=["bogus"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"batch_size": 0},
        {"batch_size": 101},
        {"download_workers": 0},
        {"download_workers": 11},
        {"retries": -1},
        {"retries": 11},
        {"retry_backoff": -0.5},
        {"timeout": 0},
        {"timeout": -1.0},
    ],
)
def test_validation_rejects_out_of_range_numbers(
    project: Project, kwargs: dict
) -> None:
    before = _count(project, "workflow_runs")
    with pytest.raises(ValidationError):
        actions.ncbi_datasets(project, accessions=[ACCESSION], plan_only=True, **kwargs)
    assert _count(project, "workflow_runs") == before


def test_cancel_event_records_interrupted_run(project: Project) -> None:
    cancel_event = threading.Event()
    cancel_event.set()
    before = _count(project, "workflow_runs")
    with pytest.raises(actions.NcbiDatasetsCancelled):
        actions.ncbi_datasets(
            project, accessions=[ACCESSION], cancel_event=cancel_event
        )
    rows = _query(
        project,
        "SELECT step, status, exit_code FROM workflow_runs "
        "WHERE step='ncbi_datasets_import'",
    )
    assert _count(project, "workflow_runs") == before + 1
    assert rows == [
        {"step": "ncbi_datasets_import", "status": "interrupted", "exit_code": 130}
    ]


def test_shutdown_without_cancel_event_propagates(
    project: Project, monkeypatch
) -> None:
    """A signal-style interrupt not caused by the TUI's event keeps its type."""

    def interrupted(*args, **kwargs):
        raise ShutdownRequested(signal.SIGINT)

    monkeypatch.setattr(
        "operon.adapters.ncbi_datasets.run_ncbi_datasets_adapter", interrupted
    )
    with pytest.raises(ShutdownRequested):
        actions.ncbi_datasets(
            project, accessions=[ACCESSION], cancel_event=threading.Event()
        )


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
            await _wait_until(
                lambda: isinstance(app.screen, NcbiDatasetsModal),
                "NCBI Datasets modal to open",
            )
            app.pop_screen()
            await _wait_until(
                lambda: not isinstance(app.screen, NcbiDatasetsModal), "modal to close"
            )

            await _click(pilot, "#home-import-wizard")
            await _wait_until(
                lambda: isinstance(app.screen, ImportWizardScreen),
                "import wizard to open",
            )

    _run(scenario())


@pytest.mark.bug("ODR-70")
def test_preflight_gate_unlocks_confirm_and_real_run_writes(
    project: Project, tmp_path: Path
) -> None:
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
            await _wait_until(
                lambda: (
                    "preflight clean"
                    in _log_text(modal.query_one("#ncbi-preview", RichLog))
                ),
                "preflight to finish",
            )
            assert not confirm.disabled

            # Any form change locks Confirm again until the next dry run.
            (await _q(modal, "#ncbi-include", Input)).value = "genome"
            await _wait_until(lambda: confirm.disabled, "form change to lock Confirm")
            assert "form changed" in _log_text(
                modal.query_one("#ncbi-preview", RichLog)
            )

            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(
                lambda: (
                    "preflight clean"
                    in _log_text(modal.query_one("#ncbi-preview", RichLog))
                ),
                "second preflight to finish",
            )
            assert not confirm.disabled

            await _click(pilot, "#confirm")
            await _wait_until(lambda: app.screen is not modal, "modal to dismiss")
            await _await_notification(app, needle="NCBI Datasets import")

    _run(scenario())
    rows = _query(
        project,
        "SELECT step, status FROM workflow_runs WHERE step='ncbi_datasets_import'",
    )
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
            await _wait_until(
                lambda: (
                    "preflight clean"
                    in _log_text(modal.query_one("#ncbi-preview", RichLog))
                ),
                "preflight preview",
            )

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


def test_invalid_number_shows_inline_error_before_preflight(
    project: Project, monkeypatch
) -> None:
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
                lambda: (
                    "timeout must be a number"
                    in _static_text(modal.query_one("#modal-error", Static))
                ),
                "inline error to appear",
            )
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
            await _wait_until(
                lambda: (
                    "dry run failed"
                    in _log_text(modal.query_one("#ncbi-preview", RichLog))
                ),
                "preflight failure preview",
            )
            assert "unknown NCBI include type" in _log_text(
                modal.query_one("#ncbi-preview", RichLog)
            )
            assert (await _q(modal, "#confirm", Button)).disabled

    _run(scenario())


@pytest.mark.bug("ODR-70")
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
            await _wait_until(
                lambda: (
                    "preflight clean"
                    in _log_text(modal.query_one("#ncbi-preview", RichLog))
                ),
                "preflight to finish",
            )

            await _click(pilot, "#confirm")
            await _wait_until(lambda: modal.running, "run to start")
            assert (await _q(modal, "#ncbi-accessions", Input)).disabled
            assert "running" in _static_text(modal.query_one("#ncbi-status", Static))

            await _click(pilot, "#cancel")
            await _wait_until(
                lambda: (
                    "cancelled" == _static_text(modal.query_one("#ncbi-status", Static))
                ),
                "cancelled status",
            )
            assert not modal.running
            assert not (await _q(modal, "#ncbi-accessions", Input)).disabled
            assert not (await _q(modal, "#confirm", Button)).disabled
            await _await_notification(app, needle="cancelled", severity="warning")

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
            (
                await _q(modal, "#ncbi-accessions", Input)
            ).value = f"{ACCESSION}, GCF_000008888.1"
            gate.set()
            await _wait_until(
                lambda: (
                    "form changed during the dry run"
                    in _log_text(modal.query_one("#ncbi-preview", RichLog))
                ),
                "stale preflight notice",
            )
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


# ---------------------------------------------------------------------------
# Coverage: preview switches, guard rails, cancel actions, status ticks
# ---------------------------------------------------------------------------


def test_command_text_covers_every_optional_switch(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-accession-file")
            accession_file = str(project.root / "accs.txt")
            (await _q(modal, "#ncbi-accession-file", Input)).value = accession_file
            (await _q(modal, "#ncbi-api-key", Input)).value = "secret-key"
            (await _q(modal, "#ncbi-resume-run", Input)).value = "WF_000042"
            (await _q(modal, "#ncbi-no-archive-files", Checkbox)).value = True
            (await _q(modal, "#ncbi-dry-run", Checkbox)).value = True
            (await _q(modal, "#ncbi-no-preserve-source", Checkbox)).value = True
            (await _q(modal, "#ncbi-plan-only", Checkbox)).value = True

            command = modal.command_text()
            ns = parse_command_text(command)
            assert ns.accession_file == accession_file
            assert ns.api_key == "secret-key"
            assert ns.resume_run == "WF_000042"
            assert ns.no_archive_files is True
            assert ns.dry_run is True
            assert ns.no_preserve_source is True
            assert ns.plan_only is True
            assert ns.standardize is False

    _run(scenario())


def test_foreign_form_events_keep_the_preflight(project: Project, monkeypatch) -> None:
    monkeypatch.setattr(actions, "ncbi_datasets", lambda *a, **k: _preflight_payload())

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-accessions")
            (await _q(modal, "#ncbi-accessions", Input)).value = ACCESSION
            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(
                lambda: (
                    modal.preflight is not None
                    and "preflight clean"
                    in _log_text(modal.query_one("#ncbi-preview", RichLog))
                ),
                "preflight to land",
            )
            preview = _log_text(modal.query_one("#ncbi-preview", RichLog))
            command = modal.command_text()

            # Unrelated widgets never invalidate the stored preflight.
            modal.on_input_changed(Input.Changed(input=Input(id="other"), value="zzz"))
            modal.on_checkbox_changed(
                Checkbox.Changed(Checkbox("other", id="other-check"), True)
            )

            assert modal.preflight is not None
            assert _log_text(modal.query_one("#ncbi-preview", RichLog)) == preview
            assert modal.command_text() == command
            assert not (await _q(modal, "#confirm", Button)).disabled

    _run(scenario())


def test_preflight_empty_form_and_reentrancy_guard(
    project: Project, monkeypatch
) -> None:
    def stub(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("preflight must not reach the core")

    monkeypatch.setattr(actions, "ncbi_datasets", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-inputs")

            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(
                lambda: (
                    "provide at least one --input, --accession, or --accession-file"
                    in _static_text(modal.query_one("#modal-error", Static))
                ),
                "empty-form error",
            )
            assert not modal.preflight_running

            # A re-entrant call while another preflight is in flight is a no-op.
            modal.preflight_running = True
            modal.run_preflight()
            modal.preflight_running = False
            assert modal.preflight is None

    _run(scenario())


def test_preflight_preview_reports_skipped_and_messages(
    project: Project, monkeypatch
) -> None:
    def stub(*args, **kwargs):
        payload = _preflight_payload()
        payload["skipped_existing"] = ["GCF_000000001.1"]
        payload["messages"] = "note: cached metadata reused"
        return payload

    monkeypatch.setattr(actions, "ncbi_datasets", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-accessions")
            (await _q(modal, "#ncbi-accessions", Input)).value = ACCESSION
            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(
                lambda: (
                    "skipped (already archived): GCF_000000001.1"
                    in _log_text(modal.query_one("#ncbi-preview", RichLog))
                ),
                "skipped line in the preview",
            )
            assert "note: cached metadata reused" in _log_text(
                modal.query_one("#ncbi-preview", RichLog)
            )
            assert not (await _q(modal, "#confirm", Button)).disabled

    _run(scenario())


def test_confirm_guard_paths_keep_the_run_from_starting(
    project: Project, monkeypatch
) -> None:
    def stub(*args, **kwargs):  # pragma: no cover - guarded paths never call the core
        raise AssertionError("the core must not run")

    monkeypatch.setattr(actions, "ncbi_datasets", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-accessions")
            (await _q(modal, "#ncbi-accessions", Input)).value = ACCESSION

            # Already running: a second Confirm is a no-op.
            modal.running = True
            modal.confirm()
            modal.running = False
            assert modal._run_kwargs == {}

            # No preflight yet: Confirm refuses and says why.
            modal.confirm()
            await _wait_until(
                lambda: (
                    "run the dry run (preflight) first"
                    in _static_text(modal.query_one("#modal-error", Static))
                ),
                "missing-preflight error",
            )
            assert not modal.running
            assert modal._run_kwargs == {}

            # The form turned invalid after a clean preflight: the numbers are
            # re-validated when the run is confirmed.
            (await _q(modal, "#ncbi-timeout", Input)).value = "abc"
            await pilot.pause()
            modal.preflight = _preflight_payload()
            modal.confirm()
            await _wait_until(
                lambda: (
                    "timeout must be a number"
                    in _static_text(modal.query_one("#modal-error", Static))
                ),
                "late validation error",
            )
            assert not modal.running
            assert modal._run_kwargs == {}
            assert (await _q(modal, "#confirm", Button)).disabled

    _run(scenario())


def test_escape_cancels_a_running_import_cooperatively(
    project: Project, monkeypatch
) -> None:
    calls: list[dict] = []

    def stub(*args, **kwargs):
        calls.append(kwargs)
        if "cancel_event" not in kwargs:
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
            await _wait_until(
                lambda: (
                    "preflight clean"
                    in _log_text(modal.query_one("#ncbi-preview", RichLog))
                ),
                "preflight to finish",
            )
            await _click(pilot, "#confirm")
            await _wait_until(lambda: modal.running, "run to start")

            # A second Confirm while running must not spawn another worker.
            modal.confirm()
            assert len(calls) == 2

            # Escape while running asks the core to stop cooperatively.
            await pilot.press("escape")
            await _wait_until(
                lambda: (
                    "cancelled" == _static_text(modal.query_one("#ncbi-status", Static))
                ),
                "cancelled status",
            )
            assert not modal.running

            # A cancel request with no event yet (defensive arm) is a no-op.
            modal._cancel_event = None
            modal.running = True
            modal._request_cancel()
            modal.running = False
            assert "cancelling" in _static_text(modal.query_one("#ncbi-status", Static))
            assert modal._cancel_event is None

            # Escape again, now idle, dismisses the modal.
            await pilot.press("escape")
            await _wait_until(lambda: app.screen is not modal, "modal to dismiss")

    _run(scenario())


def test_tick_status_updates_elapsed_while_running(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-status")

            modal.running = True
            modal._started = time.monotonic()
            modal._tick_status()
            assert (
                _static_text(modal.query_one("#ncbi-status", Static))
                == "running… (0s elapsed)"
            )

            modal.running = False
            modal._tick_status()
            assert modal._status_timer is None

    _run(scenario())


def test_run_failure_is_shown_inline_and_unlocks_confirm(
    project: Project, monkeypatch
) -> None:
    def stub(*args, **kwargs):
        if "cancel_event" not in kwargs:
            return _preflight_payload()
        raise RuntimeError("download exploded")

    monkeypatch.setattr(actions, "ncbi_datasets", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = NcbiDatasetsModal(project)
            await _push(pilot, modal, "#ncbi-accessions")
            (await _q(modal, "#ncbi-accessions", Input)).value = ACCESSION
            await _click(pilot, "#ncbi-preflight-button")
            await _wait_until(
                lambda: (
                    "preflight clean"
                    in _log_text(modal.query_one("#ncbi-preview", RichLog))
                ),
                "preflight to finish",
            )
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: (
                    "download exploded"
                    in _static_text(modal.query_one("#modal-error", Static))
                ),
                "inline run failure",
            )
            assert not modal.running
            assert _static_text(modal.query_one("#ncbi-status", Static)) == ""
            assert not (await _q(modal, "#confirm", Button)).disabled
            assert app.screen is modal

    _run(scenario())
