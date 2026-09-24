"""Import-table TUI: actions.table_template/table_import_preview/import_table
and the Home screen's ImportTableModal.

The actions mirror ``operon import table`` (validation first, writes only
through the core, actor defaulting like the CLI), the preview is read-only,
and the modal gates Confirm behind the mandatory preview with stale-result
discard (the NcbiDatasetsModal/FanoutModal patterns).  The apply has no
cooperative cancel, so a running form refuses to close (ODR-0043 guard).
"""

from __future__ import annotations

import asyncio
import csv
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.pilot import OutOfBounds
from textual.widgets import Button, DataTable, Input, Select, Static

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ConflictError, ValidationError
from operon.tui import actions
from operon.tui.app import OperonApp
from operon.tui.screens.home import HomePanel
from operon.tui.screens.table_import import ImportTableModal
from tests import helpers

SCENARIO_TIMEOUT = 180.0
SETTLE_TIMEOUT = 30.0
#: Budget for a worker result crossing back from its thread to the UI, and for
#: the screen teardown that follows it (ODR-0046).  Those steps have no upper
#: bound a loaded machine cannot exceed: a busy runner once left the dismissal
#: of a cancelled run past the 30 s SETTLE_TIMEOUT and reddened the suite with
#: no product fault behind it.  The scenario cap above is three times this
#: budget so a wait may legitimately use all of it, and a real hang still fails
#: here instead of at a red suite on a busy CI runner.
HANDOFF_TIMEOUT = 120.0


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-table-import-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    """Each test gets its own writable copy of the demo project."""
    return Project.find(helpers.copy_project_tree(demo_template.root, tmp_path / "project"))


def _csv(path: Path, columns: list[str], rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _inserts_csv(path: Path) -> Path:
    return _csv(path, ["organism_id", "scientific_name", "taxon_id"], [
        {"organism_id": "ORG_000010", "scientific_name": "Tableius gamma",
         "taxon_id": 100010},
        {"organism_id": "ORG_000011", "scientific_name": "Tableius delta",
         "taxon_id": 100011},
    ])


def _update_csv(path: Path) -> Path:
    return _csv(path, ["organism_id", "scientific_name"], [
        {"organism_id": "ORG_000001", "scientific_name": "Syntheticus alpha updated"},
    ])


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
# actions: table_template / table_import_preview / import_table
# ---------------------------------------------------------------------------


def test_table_template_writes_csv_and_xlsx_and_validates(
        project: Project, tmp_path: Path) -> None:
    csv_result = actions.table_template(project, "organisms", str(tmp_path / "t.csv"))
    assert Path(csv_result["path"]).read_text(encoding="utf-8-sig").splitlines() == [
        "organism_id,scientific_name,taxon_id,taxonomic_rank,taxonomy_source,"
        "taxonomy_version",
    ]
    xlsx_result = actions.table_template(project, "samples", str(tmp_path / "s.xlsx"))
    assert Path(xlsx_result["path"]).is_file()

    with pytest.raises(ValidationError, match="a template output path is required"):
        actions.table_template(project, "organisms", "  ")
    with pytest.raises(ValidationError, match="must end in .csv or .xlsx"):
        actions.table_template(project, "organisms", str(tmp_path / "t.txt"))
    with pytest.raises(ValidationError, match="is not importable"):
        actions.table_template(project, "files", str(tmp_path / "t.csv"))


def test_table_import_preview_is_read_only(project: Project, tmp_path: Path) -> None:
    source = _inserts_csv(tmp_path / "rows.csv")
    before = {table: _count(project, table)
              for table in ("organisms", "changes", "workflow_runs", "entity_state")}
    preview = actions.table_import_preview(project, "organisms", str(source))
    assert preview["table"] == "organisms"
    assert preview["insert"] == 2 and preview["update"] == 0
    assert [item["action"] for item in preview["items"]] == ["insert", "insert"]
    assert preview["items"][0]["key"] == ("ORG_000010",)
    for table, count in before.items():
        assert _count(project, table) == count, f"preview wrote into {table}"

    with pytest.raises(ValidationError, match="a table input path is required"):
        actions.table_import_preview(project, "organisms", "")
    with pytest.raises(ValidationError, match="table input does not exist"):
        actions.table_import_preview(project, "organisms", str(tmp_path / "nope.csv"))


def test_import_table_inserts_with_audit_state_and_idempotency(
        project: Project, tmp_path: Path) -> None:
    source = _inserts_csv(tmp_path / "rows.csv")
    result = actions.import_table(
        project, table="organisms", path=str(source), on_conflict="update")
    assert result["inserted"] == 2 and result["updated"] == 0
    assert result["unchanged"] == 0 and result["skipped"] == 0
    assert result["table"] == "organisms" and result["source"] == str(source)

    rows = _query(project, "SELECT * FROM organisms WHERE organism_id IN "
                           "('ORG_000010', 'ORG_000011') ORDER BY organism_id")
    assert [row["scientific_name"] for row in rows] == [
        "Tableius gamma", "Tableius delta"]

    states = _query(project,
                    "SELECT entity_id, state FROM entity_state "
                    "WHERE entity_type='organism' AND entity_id='ORG_000010'")
    assert states == [{"entity_id": "ORG_000010", "state": "METADATA_VALIDATED"}]

    changes = _query(project,
                     "SELECT field, reason, evidence, actor FROM changes "
                     "WHERE object_type='organisms' AND object_id='ORG_000010'")
    assert len(changes) == 1
    assert changes[0]["field"] is None
    assert changes[0]["reason"] == "table import insert"
    assert changes[0]["evidence"] == str(source)
    assert changes[0]["actor"] is not None  # $USER, like the CLI

    audited = _count(project, "changes")
    again = actions.import_table(
        project, table="organisms", path=str(source), on_conflict="update")
    assert again["inserted"] == 0 and again["unchanged"] == 2
    assert _count(project, "changes") == audited, "re-import must not re-audit"


def test_import_table_on_conflict_gate_and_policies(
        project: Project, tmp_path: Path) -> None:
    _inserts_csv(tmp_path / "rows.csv")
    actions.import_table(project, table="organisms",
                         path=str(tmp_path / "rows.csv"), on_conflict="update")

    # No on_conflict with rows that would change: the CLI's own gate message,
    # and nothing is written.
    audited = _count(project, "changes")
    with pytest.raises(ValidationError,
                       match="existing rows would change; pass --on-conflict"):
        actions.import_table(project, table="organisms",
                             path=str(_update_csv(tmp_path / "update.csv")))
    assert _count(project, "changes") == audited

    # error: the core's ConflictError; skip: counted, not written.
    with pytest.raises(ConflictError, match="1 existing row\\(s\\) would be changed"):
        actions.import_table(project, table="organisms",
                             path=str(tmp_path / "update.csv"), on_conflict="error")
    skipped = actions.import_table(project, table="organisms",
                                   path=str(tmp_path / "update.csv"), on_conflict="skip")
    assert skipped["skipped"] == 1 and skipped["updated"] == 0
    assert _query(project, "SELECT scientific_name FROM organisms "
                           "WHERE organism_id='ORG_000001'") == [
        {"scientific_name": "Syntheticus alpha"}]

    updated = actions.import_table(project, table="organisms",
                                   path=str(tmp_path / "update.csv"), on_conflict="update")
    assert updated["updated"] == 1
    changes = _query(project,
                     "SELECT field, old_value, new_value, reason FROM changes "
                     "WHERE object_type='organisms' AND object_id='ORG_000001' "
                     "AND reason='table import update'")
    assert [(c["field"], c["old_value"], c["new_value"]) for c in changes] == [
        ("scientific_name", "Syntheticus alpha", "Syntheticus alpha updated")]

    with pytest.raises(ValidationError, match="on_conflict must be error, skip or update"):
        actions.import_table(project, table="organisms",
                             path=str(tmp_path / "update.csv"), on_conflict="bogus")
    with pytest.raises(ValidationError, match="a table input path is required"):
        actions.import_table(project, table="organisms", path="  ")


def test_table_import_preview_surfaces_core_errors(
        project: Project, tmp_path: Path) -> None:
    unknown = _csv(tmp_path / "unknown.csv", ["organism_id", "mystery"],
                   [{"organism_id": "ORG_000020", "mystery": "x"}])
    with pytest.raises(ValidationError, match="unknown field"):
        actions.table_import_preview(project, "organisms", str(unknown))

    dangling = _csv(tmp_path / "dangling.csv", ["sample_id", "organism_id"],
                    [{"sample_id": "SMP_000099", "organism_id": "ORG_999999"}])
    with pytest.raises(ValidationError, match="organism_id ORG_999999 does not exist"):
        actions.table_import_preview(project, "samples", str(dangling))


# ---------------------------------------------------------------------------
# ImportTableModal
# ---------------------------------------------------------------------------


def test_home_screen_opens_import_table_modal(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            assert isinstance(app.screen.query_one("#home", HomePanel), HomePanel)
            await _click(pilot, "#home-import-table")
            await _wait_until(lambda: isinstance(app.screen, ImportTableModal),
                              "Import table modal to open")

    _run(scenario())


def test_template_mode_command_text_and_confirm(
        project: Project, tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[tuple, dict]] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return {"table": args[1], "path": str(tmp_path / "t.csv")}

    monkeypatch.setattr(actions, "table_template", stub)
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = ImportTableModal(project)
            app.push_screen(modal, dismissed.append)
            await _push(pilot, modal, "#table-template-out")
            (await _q(modal, "#table-table", Select)).value = "samples"
            (await _q(modal, "#table-mode", Select)).value = "template"
            await _wait_until(
                lambda: modal.query_one("#table-preview-button", Button).disabled,
                "preview button to disable in template mode")
            (await _q(modal, "#table-template-out", Input)).value = str(
                tmp_path / "t.csv")
            await pilot.pause()

            ns = parse_command_text(modal.command_text())
            assert ns.table == "samples"
            assert ns.template == str(tmp_path / "t.csv")
            assert ns.file is None and ns.on_conflict is None

            await _click(pilot, "#confirm")
            await _wait_until(lambda: bool(dismissed), "template modal dismissal",
                              timeout=HANDOFF_TIMEOUT)
            assert any("template written to" in message
                       for _severity, message in _notifications(app))

    _run(scenario())
    assert calls == [((project, "samples", str(tmp_path / "t.csv")), {})]


def test_import_preview_gate_unlocks_confirm_and_writes(
        project: Project, tmp_path: Path) -> None:
    source = _inserts_csv(tmp_path / "rows.csv")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = ImportTableModal(project)
            app.push_screen(modal)
            await _push(pilot, modal, "#table-file")
            confirm = await _q(modal, "#confirm", Button)
            assert confirm.disabled, "Confirm must be gated behind the preview"

            (await _q(modal, "#table-file", Input)).value = str(source)
            await _click(pilot, "#table-preview-button")
            await _wait_until(
                lambda: "2 insert" in _static_text(
                    modal.query_one("#table-status", Static)),
                "preview summary")
            table = modal.query_one("#table-preview-table", DataTable)
            assert table.row_count == 2
            assert not (await _q(modal, "#confirm", Button)).disabled

            # Editing the file path re-locks Confirm until the next preview.
            # (The form strips inputs, so a trailing-space edit would be a no-op.)
            (await _q(modal, "#table-file", Input)).value = str(source) + "x"
            await _wait_until(lambda: modal.query_one("#confirm", Button).disabled,
                              "form change to lock Confirm")
            # Undoing the edit does not resurrect the discarded preview: a
            # fresh preview is the only way back to Confirm.
            (await _q(modal, "#table-file", Input)).value = str(source)
            await _wait_until(
                lambda: "run the preview again" in _static_text(
                    modal.query_one("#table-status", Static)),
                "invalidation notice after the edit")
            assert modal.query_one("#confirm", Button).disabled
            await _click(pilot, "#table-preview-button")
            await _wait_until(
                lambda: "2 insert" in _static_text(
                    modal.query_one("#table-status", Static)),
                "second preview summary")
            assert not modal.query_one("#confirm", Button).disabled
            # on-conflict may be chosen after the preview without re-running it.
            (await _q(modal, "#table-on-conflict", Select)).value = "update"
            await _wait_until(
                lambda: not modal.query_one("#confirm", Button).disabled,
                "on-conflict choice keeps the preview valid")

            await _click(pilot, "#confirm")
            await _wait_until(lambda: app.screen is not modal, "modal to dismiss")
            assert any("table import (organisms): 2 inserted" in message
                       for _severity, message in _notifications(app))

    _run(scenario())
    assert _count(project, "organisms") == 4  # 2 demo + 2 imported
    assert _query(project,
                  "SELECT state FROM entity_state WHERE entity_type='organism' "
                  "AND entity_id='ORG_000010'") == [{"state": "METADATA_VALIDATED"}]


def test_import_command_text_matches_action_kwargs(
        project: Project, tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[tuple, dict]] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return {"inserted": 0, "updated": 1, "unchanged": 0, "skipped": 0,
                "table": "organisms", "source": kwargs["path"]}

    monkeypatch.setattr(actions, "import_table", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = ImportTableModal(project)
            app.push_screen(modal)
            await _push(pilot, modal, "#table-file")
            (await _q(modal, "#table-file", Input)).value = "/tmp/rows.csv"
            (await _q(modal, "#table-on-conflict", Select)).value = "update"
            await pilot.pause()

            ns = parse_command_text(modal.command_text())
            assert ns.table == "organisms"
            assert ns.file == "/tmp/rows.csv"
            assert ns.on_conflict == "update"
            assert ns.template is None and ns.yes is False

            # Drive the real preview through a stubbed action so the gate passes.
            monkeypatch.setattr(actions, "table_import_preview", lambda *a, **k: {
                "table": "organisms", "source": "/tmp/rows.csv",
                "columns": ["organism_id"],
                "items": [{"key": ("ORG_000001",), "action": "update",
                           "differences": ["scientific_name"], "row": {},
                           "current": {}, "supplied_columns": ["scientific_name"]}],
                "insert": 0, "update": 1, "unchanged": 0})
            await _click(pilot, "#table-preview-button")
            await _wait_until(lambda: modal.preview is not None, "preview to land")
            await _click(pilot, "#confirm")
            await _wait_until(lambda: len(calls) == 1, "import call")

    _run(scenario())
    assert calls[0][0] == (project,)
    assert calls[0][1] == {"table": "organisms", "path": "/tmp/rows.csv",
                           "on_conflict": "update"}


def test_preview_failure_keeps_confirm_locked(project: Project, tmp_path: Path) -> None:
    bad = _csv(tmp_path / "bad.csv", ["organism_id", "mystery"],
               [{"organism_id": "ORG_000020", "mystery": "x"}])

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = ImportTableModal(project)
            app.push_screen(modal)
            await _push(pilot, modal, "#table-file")
            (await _q(modal, "#table-file", Input)).value = str(bad)
            await _click(pilot, "#table-preview-button")
            await _wait_until(
                lambda: "preview failed" in _static_text(
                    modal.query_one("#table-status", Static)),
                "preview failure message")
            assert "unknown field" in _static_text(
                modal.query_one("#table-status", Static))
            assert (await _q(modal, "#confirm", Button)).disabled

    _run(scenario())


def test_stale_preview_discarded(project: Project, tmp_path: Path, monkeypatch) -> None:
    gate = threading.Event()

    def blocking_preview(*args, **kwargs):
        assert gate.wait(timeout=20.0), "test never released the preview"
        return {"table": "organisms", "source": "x", "columns": [],
                "items": [], "insert": 0, "update": 0, "unchanged": 0}

    monkeypatch.setattr(actions, "table_import_preview", blocking_preview)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = ImportTableModal(project)
            app.push_screen(modal)
            await _push(pilot, modal, "#table-file")
            (await _q(modal, "#table-file", Input)).value = "/tmp/rows.csv"
            await _click(pilot, "#table-preview-button")
            await _wait_until(lambda: modal.preview_running, "preview to start")

            # The form moves on while the preview is still in flight.
            (await _q(modal, "#table-file", Input)).value = "/tmp/other.csv"
            gate.set()
            await _wait_until(
                lambda: "form changed during the preview" in _static_text(
                    modal.query_one("#table-status", Static)),
                "stale preview notice")
            assert modal.preview is None
            assert (await _q(modal, "#confirm", Button)).disabled

    _run(scenario())


def test_on_conflict_gate_then_conflict_error_then_retry(
        project: Project, tmp_path: Path) -> None:
    source = _update_csv(tmp_path / "update.csv")

    async def pause_until_confirm_enabled(modal) -> None:
        await _wait_until(
            lambda: not modal.query_one("#confirm", Button).disabled,
            "confirm to re-enable")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = ImportTableModal(project)
            app.push_screen(modal)
            await _push(pilot, modal, "#table-file")
            (await _q(modal, "#table-file", Input)).value = str(source)
            await _click(pilot, "#table-preview-button")
            await _wait_until(lambda: modal.preview is not None, "preview to land")
            assert "would change" in _static_text(
                modal.query_one("#table-status", Static))

            # Blank on-conflict with rows that would change: the CLI's gate
            # message inline, no worker started.
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "existing rows would change; pass --on-conflict"
                        in _static_text(modal.query_one("#modal-error", Static)),
                "on-conflict gate error")
            assert app.screen is modal

            # error: the core ConflictError lands inline; the modal stays open.
            (await _q(modal, "#table-on-conflict", Select)).value = "error"
            await pilot.pause()
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "would be changed" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "conflict error")
            assert app.screen is modal

            # Choosing update re-enables the still-valid preview: retry applies.
            (await _q(modal, "#table-on-conflict", Select)).value = "update"
            await pause_until_confirm_enabled(modal)
            await _click(pilot, "#confirm")
            await _wait_until(lambda: app.screen is not modal, "modal to dismiss")

    _run(scenario())
    assert _query(project, "SELECT scientific_name FROM organisms "
                           "WHERE organism_id='ORG_000001'") == [
        {"scientific_name": "Syntheticus alpha updated"}]


def test_modal_refuses_cancel_while_running(project: Project, monkeypatch) -> None:
    released = threading.Event()
    started = threading.Event()

    def blocking_import(*args, **kwargs):
        started.set()
        released.wait(HANDOFF_TIMEOUT)
        return {"inserted": 2, "updated": 0, "unchanged": 0, "skipped": 0,
                "table": "organisms", "source": kwargs["path"]}

    monkeypatch.setattr(actions, "import_table", blocking_import)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = ImportTableModal(project)
            app.push_screen(modal)
            await _push(pilot, modal, "#table-file")
            (await _q(modal, "#table-file", Input)).value = "/tmp/rows.csv"
            monkeypatch.setattr(actions, "table_import_preview", lambda *a, **k: {
                "table": "organisms", "source": "x", "columns": [],
                "items": [], "insert": 2, "update": 0, "unchanged": 0})
            await _click(pilot, "#table-preview-button")
            await _wait_until(lambda: modal.preview is not None, "preview to land")
            await _click(pilot, "#confirm")
            await _wait_until(lambda: modal.running, "import to start")

            # A real Cancel click is refused while the import runs (ODR-0043
            # guard): the modal stays open, the worker keeps running.
            await _wait_until(started.is_set, "import to have reached the core",
                              timeout=HANDOFF_TIMEOUT)
            await _click(pilot, "#cancel")
            await _wait_until(
                lambda: any(severity == "warning" and "cannot be interrupted" in m
                            for severity, m in _notifications(app)),
                "refusal notification")
            assert app.screen is modal

            released.set()
            await _wait_until(lambda: app.screen is not modal, "modal to close",
                              timeout=HANDOFF_TIMEOUT)

    try:
        _run(scenario())
    finally:
        released.set()


def test_modal_drops_result_after_teardown(project: Project, monkeypatch) -> None:
    released = threading.Event()

    def blocking_import(*args, **kwargs):
        released.wait(HANDOFF_TIMEOUT)
        return {"inserted": 1, "updated": 0, "unchanged": 0, "skipped": 0,
                "table": "organisms", "source": kwargs["path"]}

    monkeypatch.setattr(actions, "import_table", blocking_import)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = ImportTableModal(project)
            app.push_screen(modal)
            await _push(pilot, modal, "#table-file")
            (await _q(modal, "#table-file", Input)).value = "/tmp/rows.csv"
            monkeypatch.setattr(actions, "table_import_preview", lambda *a, **k: {
                "table": "organisms", "source": "x", "columns": [],
                "items": [], "insert": 1, "update": 0, "unchanged": 0})
            await _click(pilot, "#table-preview-button")
            await _wait_until(lambda: modal.preview is not None, "preview to land")
            await _click(pilot, "#confirm")
            await _wait_until(lambda: modal.running, "import to start")

            # The raw callback on the torn-down modal raises, the guarded
            # delivery drops it quietly and the app survives.
            app.pop_screen()
            await pilot.pause()
            with pytest.raises(Exception):
                modal._action_done({"inserted": 1, "updated": 0, "unchanged": 0,
                                    "skipped": 0, "table": "organisms"})
            modal.apply_from_worker(modal._action_done,
                                    {"inserted": 1, "updated": 0, "unchanged": 0,
                                     "skipped": 0, "table": "organisms"})
            await pilot.pause()
            assert app.is_running

    try:
        _run(scenario())
    finally:
        released.set()


def test_modal_layout_contains_controls(project: Project) -> None:
    """The preview table and buttons stay inside the box (ODR-0020 shape)."""
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = ImportTableModal(project)
            app.push_screen(modal, dismissed.append)
            await _push(pilot, modal, "#table-preview-table")
            box = modal.query_one("#modal-box")
            panes = [widget for widget in box.children if widget.display]
            for pane in panes:
                assert pane.region.y >= box.region.y
                assert pane.region.y + pane.region.height <= \
                    box.region.y + box.region.height
            for index, first in enumerate(panes):
                for second in panes[index + 1:]:
                    a, b = first.region, second.region
                    assert not (a.x < b.x + b.width and b.x < a.x + a.width
                                and a.y < b.y + b.height and b.y < a.y + a.height)
            table = modal.query_one("#table-preview-table", DataTable)
            assert table.region.height >= 5

            # Idle Cancel dismisses normally.
            modal.action_cancel()
            await pilot.pause()
            assert dismissed == [None]

    _run(scenario())


def test_confirm_gates_require_template_output_and_fresh_preview(
        project: Project, tmp_path: Path) -> None:
    """confirm()'s inline gates: template output path, fresh preview, file path."""
    source = _inserts_csv(tmp_path / "rows.csv")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = ImportTableModal(project)
            app.push_screen(modal)
            await _push(pilot, modal, "#table-file")

            # Import mode with a file but no preview: the gate names the preview.
            (await _q(modal, "#table-file", Input)).value = str(source)
            await pilot.pause()
            modal.confirm()
            await pilot.pause()
            assert "run the preview first" in _static_text(
                modal.query_one("#modal-error", Static))

            # Preview, then clear the file input: the path is required again.
            (await _q(modal, "#table-file", Input)).value = str(source)
            await _click(pilot, "#table-preview-button")
            await _wait_until(lambda: modal.preview is not None, "preview to land")
            (await _q(modal, "#table-file", Input)).value = ""
            await pilot.pause()
            modal.confirm()
            await pilot.pause()
            assert "a table input path is required" in _static_text(
                modal.query_one("#modal-error", Static))

            # Preview with an empty file path: the button names the input.
            (await _q(modal, "#table-file", Input)).value = ""
            await pilot.pause()
            await _click(pilot, "#table-preview-button")
            await pilot.pause()
            assert "a table input path is required" in _static_text(
                modal.query_one("#modal-error", Static))

            # Template mode without an output path: --template is required.
            (await _q(modal, "#table-mode", Select)).value = "template"
            await pilot.pause()
            modal.confirm()
            await pilot.pause()
            assert "a template output path is required" in _static_text(
                modal.query_one("#modal-error", Static))

    _run(scenario())
