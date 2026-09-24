"""Taxonomy TUI: import/compile actions and the Coverage-screen modals.

The actions mirror ``operon taxonomy import``/``operon taxonomy compile``
(the core archives/imports or freezes the denominator in one transaction and
records the same audit/run rows), and the modals are the RunExternalModal
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

import yaml
from rich.text import Text
from textual.pilot import OutOfBounds
from textual.widgets import Button, DataTable, Input, Select, Static

from operon import taxonomy
from operon.config import Project
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.tui import actions, data
from operon.tui.app import OperonApp
from operon.tui.screens.coverage import CoveragePanel
from operon.tui.screens.taxonomy import CompileReferenceSetModal, TaxonomyImportModal

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


# ---------------------------------------------------------------------------
# actions.compile_reference_set
# ---------------------------------------------------------------------------


def _coverage_profile() -> dict:
    return {
        "kind": "taxonomy_coverage",
        "version": 1,
        "name": "cov",
        "taxonomy": {"source": "NCBI"},
        "scope": {"root_taxids": [1]},
        "targets": {"ranks": ["family", "genus"]},
        "thresholds": {
            "family": {"min_coverage_percent": 0},
            "genus": {"min_coverage_percent": 0},
        },
    }


def _seed_taxonomy(project: Project, tmp_path: Path) -> None:
    """One READY snapshot (cov.1) plus the ``cov`` taxonomy_coverage profile."""
    source = _write_taxonomy_jsonl(tmp_path / "taxonomy.jsonl")
    db = Database(project.db_path)
    try:
        taxonomy.import_ncbi_taxonomy(db, project, source, "cov.1")
    finally:
        db.close()
    (project.profiles_dir / "cov.yaml").write_text(
        yaml.safe_dump(_coverage_profile(), sort_keys=False), encoding="utf-8"
    )


def test_compile_reference_set_freezes_denominator(
        project: Project, tmp_path: Path) -> None:
    _seed_taxonomy(project, tmp_path)
    result = actions.compile_reference_set(project, "cov", "cov.1")
    assert result["reused"] is False
    assert result["reference_set_id"] == "cov@cov.1"
    assert result["family_count"] == 1
    assert result["genus_count"] == 1

    rows = _query(project, "SELECT * FROM taxonomy_reference_sets")
    assert len(rows) == 1
    row = rows[0]
    assert row["reference_set_id"] == "cov@cov.1"
    assert row["profile_name"] == "cov"
    assert row["taxonomy_version"] == "cov.1"

    tsv = project.root / row["relative_path"]
    assert tsv.is_file()
    lines = tsv.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "rank\ttaxid\tscientific_name"
    assert len(lines) == 3  # header + one family + one genus
    sidecar = tsv.with_suffix(".provenance.json")
    assert sidecar.is_file()
    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    assert provenance["reference_set_id"] == "cov@cov.1"
    assert provenance["row_counts"] == {"family": 1, "genus": 1}

    changes = _query(
        project, "SELECT * FROM changes WHERE object_type='taxonomy_reference_set'")
    assert len(changes) == 1
    assert changes[0]["object_id"] == "cov@cov.1"

    runs = _query(
        project, "SELECT * FROM workflow_runs WHERE step='taxonomy_compile'")
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["entity_id"] == "cov@cov.1"
    assert runs[0]["error"] is None


def test_compile_reference_set_reuses_identical_inputs(
        project: Project, tmp_path: Path) -> None:
    _seed_taxonomy(project, tmp_path)
    first = actions.compile_reference_set(project, "cov", "cov.1")
    second = actions.compile_reference_set(project, "cov", "cov.1")
    assert first["reused"] is False
    assert second["reused"] is True
    assert _count(project, "taxonomy_reference_sets") == 1
    compile_runs = _query(
        project,
        "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='taxonomy_compile'")
    assert compile_runs[0]["n"] == 1


def test_compile_reference_set_unknown_profile_records_failed_run(
        project: Project, tmp_path: Path) -> None:
    _seed_taxonomy(project, tmp_path)
    with pytest.raises(ValidationError, match="profile 'nope' not found"):
        actions.compile_reference_set(project, "nope", "cov.1")
    assert _count(project, "taxonomy_reference_sets") == 0
    runs = _query(
        project, "SELECT * FROM workflow_runs WHERE step='taxonomy_compile'")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "ValidationError" in runs[0]["error"]


def test_list_coverage_profiles(project: Project) -> None:
    (project.profiles_dir / "qc_extra.yaml").write_text(
        yaml.safe_dump({"kind": "qc", "version": 1, "rules": []}, sort_keys=False),
        encoding="utf-8",
    )
    (project.profiles_dir / "cov.yaml").write_text(
        yaml.safe_dump(_coverage_profile(), sort_keys=False), encoding="utf-8"
    )
    rows = data.list_coverage_profiles(project)
    # The default qc profiles and the qc-only file are excluded; the seeded
    # ``cov`` profile joins the default taxonomy_coverage profile.
    assert [row["name"] for row in rows] == ["cov", "coverage_viridiplantae_v1"]
    assert all(row["kind"] == "taxonomy_coverage" for row in rows)
    cov = {row["name"]: row for row in rows}["cov"]
    assert cov["version"] == 1


# ---------------------------------------------------------------------------
# CompileReferenceSetModal
# ---------------------------------------------------------------------------


def test_compile_modal_confirm_calls_action_with_selected_values(
        project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_taxonomy(project, tmp_path)
    payload = {
        "reference_set_id": "cov@cov.1", "profile_name": "cov",
        "taxonomy_version": "cov.1", "family_count": 1, "genus_count": 1,
        "reused": True,
    }
    calls: list[tuple[tuple, dict]] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return payload

    monkeypatch.setattr(actions, "compile_reference_set", stub)
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = CompileReferenceSetModal(project)
            app.push_screen(modal, dismissed.append)
            await _push(pilot, modal, "#taxonomy-compile-profile")

            # Selects start blank (Textual does not auto-select the first option).
            profile_select = await _q(modal, "#taxonomy-compile-profile", Select)
            version_select = await _q(modal, "#taxonomy-compile-taxonomy-version", Select)
            assert profile_select.value is Select.NULL
            assert version_select.value is Select.NULL
            profile_select.value = "cov"
            version_select.value = "cov.1"
            await pilot.pause()

            ns = parse_command_text(modal.command_text())
            assert ns.profile == "cov"
            assert ns.taxonomy_version == "cov.1"

            await _click(pilot, "#confirm")
            await _wait_until(lambda: len(calls) == 1, "reference set compile call")
            await _settled(app)

    _run(scenario())
    args, kwargs = calls[0]
    assert args == (project, "cov", "cov.1")
    assert kwargs == {}
    assert dismissed == [payload]


def test_compile_modal_requires_selections(
        project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_taxonomy(project, tmp_path)
    calls: list[tuple[tuple, dict]] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(actions, "compile_reference_set", stub)
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = CompileReferenceSetModal(project)
            app.push_screen(modal, dismissed.append)
            await _push(pilot, modal, "#taxonomy-compile-profile")
            profile_select = await _q(modal, "#taxonomy-compile-profile", Select)
            version_select = await _q(modal, "#taxonomy-compile-taxonomy-version", Select)

            # Nothing selected: an inline error, no action call.
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "select a coverage profile first" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "profile-required inline error",
            )
            assert calls == []

            # Profile only: the version error, still no call.
            profile_select.value = "cov"
            await pilot.pause()
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "select a taxonomy version first" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "version-required inline error",
            )
            assert calls == []

            # Version only: the profile error again.
            profile_select.value = Select.NULL
            version_select.value = "cov.1"
            await pilot.pause()
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "select a coverage profile first" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "profile-required inline error again",
            )
            assert calls == []

            # Select events from widgets the modal does not own leave the
            # command preview alone.
            modal.on_select_changed(Select.Changed(Select([]), Select.NULL))
            await pilot.pause()

            # Cancel with no run in flight dismisses normally.
            modal.action_cancel()
            await pilot.pause()
            assert dismissed == [None]

    _run(scenario())


def test_compile_modal_real_run_notifies_and_reloads(
        project: Project, tmp_path: Path) -> None:
    _seed_taxonomy(project, tmp_path)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            app.action_switch_screen("coverage")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(CoveragePanel)
            assert panel.query_one("#reference-sets-table", DataTable).row_count == 0

            panel.on_button_pressed(Button.Pressed(
                panel.query_one("#coverage-compile-reference-set", Button)))
            await pilot.pause()
            await _wait_until(
                lambda: isinstance(app.screen, CompileReferenceSetModal),
                "compile modal to open",
            )
            modal = app.screen
            await _push(pilot, modal, "#taxonomy-compile-profile")
            (await _q(modal, "#taxonomy-compile-profile", Select)).value = "cov"
            (await _q(modal, "#taxonomy-compile-taxonomy-version", Select)).value = "cov.1"
            await pilot.pause()
            await _click(pilot, "#confirm")
            await _wait_until(lambda: app.screen is not modal, "compile modal to close")
            await _settled(app)
            assert any("reference set cov@cov.1: family 1 / genus 1 row(s)" in message
                       for _severity, message in _notifications(app))
            await _wait_until(
                lambda: panel.query_one("#reference-sets-table", DataTable).row_count == 1,
                "reference set table reload",
            )

    _run(scenario())
    assert _count(project, "taxonomy_reference_sets") == 1


def test_compile_modal_refuses_cancel_while_running(
        project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_taxonomy(project, tmp_path)
    released = threading.Event()
    calls: list[tuple] = []

    def blocking_compile(*args, **kwargs):
        calls.append((args, kwargs))
        released.wait(30)
        return {"reference_set_id": "cov@cov.1", "profile_name": "cov",
                "taxonomy_version": "cov.1", "family_count": 1, "genus_count": 1,
                "reused": False}

    monkeypatch.setattr(actions, "compile_reference_set", blocking_compile)
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = CompileReferenceSetModal(project)
            app.push_screen(modal, dismissed.append)
            await _push(pilot, modal, "#taxonomy-compile-profile")
            (await _q(modal, "#taxonomy-compile-profile", Select)).value = "cov"
            (await _q(modal, "#taxonomy-compile-taxonomy-version", Select)).value = "cov.1"
            await pilot.pause()
            modal.confirm()
            await _wait_until(lambda: modal.running, "reference set compile to start")

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
            assert all(widget.disabled for widget in modal.query("Select"))

            released.set()
            await _wait_until(lambda: bool(dismissed), "compile modal dismissal")
            await _settled(app)

    try:
        _run(scenario())
    finally:
        released.set()


def test_compile_modal_empty_project_shows_hints(
        project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple, dict]] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(actions, "compile_reference_set", stub)
    # Project.init seeds default profiles (including a taxonomy_coverage
    # example); strip them so the dialog's empty state is exercised honestly.
    for path in project.profiles_dir.glob("*.yaml"):
        path.unlink()

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = CompileReferenceSetModal(project)
            app.push_screen(modal)
            await _push(pilot, modal, "#taxonomy-compile-profile")

            profile_select = await _q(modal, "#taxonomy-compile-profile", Select)
            version_select = await _q(modal, "#taxonomy-compile-taxonomy-version", Select)
            assert profile_select.value is Select.NULL
            assert version_select.value is Select.NULL
            form_text = " ".join(
                _static_text(widget) for widget in modal.query("#modal-form Static"))
            assert "no taxonomy_coverage profiles" in form_text
            assert "no READY NCBI taxonomy snapshots" in form_text

            # Confirm without anything to select: inline error, no action call.
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "select a coverage profile first" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "inline error",
            )
            assert calls == []

    _run(scenario())


@pytest.mark.bug("ODR-0043")
def test_import_modal_real_cancel_click_stays_open_while_running(
        project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real Cancel click must not dismiss the modal mid-run (MRO dispatch)."""
    released = threading.Event()

    def blocking_import(*args, **kwargs):
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

            # Button.press() posts Button.Pressed through the real pump; before
            # ODR-0043 the base WriteModal handler dismissed the modal here.
            modal.query_one("#cancel", Button).press()
            await pilot.pause()
            assert app.screen is modal
            assert dismissed == []

            released.set()
            await _wait_until(lambda: bool(dismissed), "import modal dismissal")
            await _settled(app)

    try:
        _run(scenario())
    finally:
        released.set()


@pytest.mark.bug("ODR-0043")
def test_compile_modal_real_cancel_click_stays_open_while_running(
        project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real Cancel click must not dismiss the modal mid-run (MRO dispatch)."""
    _seed_taxonomy(project, tmp_path)
    released = threading.Event()

    def blocking_compile(*args, **kwargs):
        released.wait(30)
        return {"reference_set_id": "cov@cov.1", "profile_name": "cov",
                "taxonomy_version": "cov.1", "family_count": 1, "genus_count": 1,
                "reused": False}

    monkeypatch.setattr(actions, "compile_reference_set", blocking_compile)
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = CompileReferenceSetModal(project)
            app.push_screen(modal, dismissed.append)
            await _push(pilot, modal, "#taxonomy-compile-profile")
            (await _q(modal, "#taxonomy-compile-profile", Select)).value = "cov"
            (await _q(modal, "#taxonomy-compile-taxonomy-version", Select)).value = "cov.1"
            await pilot.pause()
            modal.confirm()
            await _wait_until(lambda: modal.running, "reference set compile to start")

            modal.query_one("#cancel", Button).press()
            await pilot.pause()
            assert app.screen is modal
            assert dismissed == []

            released.set()
            await _wait_until(lambda: bool(dismissed), "compile modal dismissal")
            await _settled(app)

    try:
        _run(scenario())
    finally:
        released.set()
