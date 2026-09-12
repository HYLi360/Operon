"""Extra headless coverage for the TUI screens, modals, and read-only data layer.

The suites in ``test_tui.py`` / ``test_tui_writes.py`` drive the happy paths;
this module fills in the remaining state: modal plan/validation rendering,
filter retention across reloads, write-callback outcomes, shutdown guards,
and data-layer edge cases (metric de-duplication, corrupt snapshots, legacy
databases without the lifecycle schema).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    DataTable,
    Input,
    ListItem,
    ListView,
    Select,
    Static,
    Tree,
)
from textual.widgets.data_table import RowKey

from operon.config import Project
from operon.database import DDL, Database
from operon.demo import init_demo
from operon.tui import data
from operon.tui.app import HelpScreen, OperonApp
from operon.tui.screens import files_ops as files_ops_module
from operon.tui.screens.common import ErrorDialog, WriteModal, human_size
from operon.tui.screens.coverage import CoverageModal, CoveragePanel
from operon.tui.screens.decisions import CurateModal, DecisionsPanel, EvaluateModal
from operon.tui.screens.entities import (
    METRIC_ROW_LIMIT,
    EntitiesPanel,
    LifecycleModal,
    _metrics_section,
)
from operon.tui.screens.files import FilesPanel
from operon.tui.screens.files_ops import IngestModal, QcModal, VerifyModal
from operon.tui.screens.home import HomePanel
from operon.tui.screens.runs import RunDetailScreen, RunsPanel

SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 15.0


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-screens-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    """A writable copy of the demo project for tests that seed extra rows."""
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
        predicate: Callable[[], bool], description: str, timeout: float = SETTLE_TIMEOUT,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not {description} within {timeout}s")
        await asyncio.sleep(0.05)


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


def _screen_text(screen, selector: str) -> str:
    """Text of the first match, or "" while the widget is still mounting.

    ``query_one`` raises ``NoMatches`` when a just-pushed screen has not
    composed its children yet, which makes load-sensitive waits flaky; wait
    predicates must be total functions instead.
    """
    matches = screen.query(selector)
    return _static_text(matches.first()) if matches else ""


def _cell_text(table: DataTable, row: int, column: int) -> str:
    value = table.get_row_at(row)[column]
    return value.plain if isinstance(value, Text) else str(value)


def _form_text(modal) -> str:
    return "\n".join(_static_text(widget) for widget in modal.query(Static))


def _notifications(app) -> list[tuple[str, str]]:
    return [(notification.severity, notification.message) for notification in app._notifications]


def _shutdown_guard(app):
    """Patch ``is_running`` to False for one call, restoring it immediately."""
    return pytest.MonkeyPatch.context()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def test_human_size_and_metric_section_rendering() -> None:
    """Size formatting saturates at TiB; metric sections group, unit and truncate."""
    assert human_size(5 * 1024 ** 5) == "5.0 TiB"
    assert human_size(0) == "0 B"

    text = Text()
    rows = [
        {"analysis_name": "busco", "metric_name": "complete", "metric_value": "95.0",
         "metric_unit": "%"},
        {"analysis_name": "busco", "metric_name": "fragmented", "metric_value": "2.0"},
        {"analysis_name": "quast", "metric_name": "n50", "metric_value": "1000"},
    ]
    _metrics_section(text, "Analysis metrics", rows, "analysis_name", show_tool=False)
    rendered = text.plain
    assert "Analysis metrics" in rendered
    assert rendered.count("  busco\n") == 1  # one group heading per analysis
    assert "  quast\n" in rendered
    assert "    complete = 95.0 %" in rendered
    assert "    fragmented = 2.0\n" in rendered

    many = Text()
    metrics = [
        {"qc_stage": "assembly", "metric_name": f"m{index}", "metric_value": index,
         "metric_unit": "bp", "tool": "operon.builtin", "tool_version": "0.7"}
        for index in range(METRIC_ROW_LIMIT + 5)
    ]
    _metrics_section(many, "QC metrics", metrics, "qc_stage", show_tool=True)
    assert f"m{METRIC_ROW_LIMIT - 1} = " in many.plain
    assert f"m{METRIC_ROW_LIMIT} = " not in many.plain
    assert "… and 5 more" in many.plain
    assert "operon.builtin@0.7" in many.plain


def test_write_modal_base_contract(project: Project) -> None:
    """The base modal renders its defaults, dispatches buttons, and dismisses."""
    captured: list[Any] = []

    class PlainModal(WriteModal):
        def compose_form(self):
            yield Static("plain body", id="plain-body")

    class RecorderModal(PlainModal):
        confirmed = 0

        def confirm(self) -> None:
            type(self).confirmed += 1

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            assert list(WriteModal("contract").compose_form()) == []
            assert WriteModal("contract").command_text() == ""

            modal = PlainModal("Plain")
            app.push_screen(modal, captured.append)
            await pilot.pause()
            assert _static_text(modal.query_one("#modal-command", Static)) == ""
            modal.run_action(lambda: {"answer": 42})
            await _wait_until(lambda: captured, "base modal dismissal")
            assert captured == [{"answer": 42}]

            recorder = RecorderModal("Recorder")
            app.push_screen(recorder)
            await pilot.pause()
            recorder.on_button_pressed(Button.Pressed(recorder.query_one("#confirm", Button)))
            assert recorder.confirmed == 1
            recorder.on_button_pressed(Button.Pressed(Button("other", id="other")))
            assert recorder.confirmed == 1
            assert app.screen is recorder
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not recorder

    _run(scenario())


def test_error_dialog_shows_message_and_closes(project: Project) -> None:
    """OK closes the dialog; escape closing twice is a no-op, not a crash."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            dialog = ErrorDialog("Verify failed", "FIL_1: CHECKSUM_FAILED")
            app.push_screen(dialog)
            await pilot.pause()
            assert "FIL_1: CHECKSUM_FAILED" in _static_text(
                dialog.query_one("#error-dialog-body", Static))
            dialog.on_button_pressed(Button.Pressed(Button("other", id="other")))
            assert app.screen is dialog, "only the OK button closes the dialog"
            assert await pilot.click("#cancel")
            await pilot.pause()
            assert not isinstance(app.screen, ErrorDialog)

    _run(scenario())


# ---------------------------------------------------------------------------
# Data layer edge cases
# ---------------------------------------------------------------------------


def test_entity_metrics_keep_newest_measurement_per_metric(project: Project) -> None:
    """Repeated QC and analysis rows collapse onto the newest measurement."""
    file_id = data.list_files(project)[0]["file_id"]
    db = Database(project.db_path)
    try:
        for parameter_set, value, evaluated_at in (
            ("old-run", "100", "2026-01-01T00:00:00+00:00"),
            ("new-run", "250", "2026-02-01T00:00:00+00:00"),
        ):
            db.insert_row("qc_results", {
                "entity_type": "assembly", "entity_id": "ASM_000001", "file_id": file_id,
                "file_sha256": "ab" * 32, "input_identity": "asm-1",
                "qc_stage": "assembly", "metric_name": "test_metric_n50", "metric_value": value,
                "metric_numeric": float(value), "metric_unit": "bp", "tool": "operon.builtin",
                "tool_version": "0.7", "parameter_set": parameter_set, "evaluated_at": evaluated_at,
            })
        for index, (analysis_name, finished_at, metric_name, value, unit) in enumerate((
            ("busco", "2026-01-01T00:00:00+00:00", "complete", "90.0", "%"),
            ("busco", "2026-03-01T00:00:00+00:00", "complete", "95.0", "%"),
            ("busco", "2026-03-01T00:00:00+00:00", "fragmented", "2.0", "%"),
        )):
            db.insert_row("analysis_jobs", {
                "analysis_name": analysis_name, "entity_type": "assembly",
                "entity_id": "ASM_000001", "file_id": file_id, "tool": analysis_name,
                "tool_version": "1", "parameter_set": "default",
                "parameter_sha256": f"{index:02d}" * 32, "input_sha256": "ef" * 32,
                "database_identity": "db", "status": "completed", "started_at": finished_at,
                "finished_at": finished_at,
            })
            job_id = int(db.query("SELECT MAX(job_id) AS j FROM analysis_jobs")[0]["j"])
            db.insert_row("analysis_results", {
                "job_id": job_id, "entity_type": "assembly", "entity_id": "ASM_000001",
                "file_id": file_id, "analysis_name": analysis_name,
                "metric_name": metric_name, "metric_value": value,
                "metric_numeric": float(value), "metric_unit": unit,
            })
    finally:
        db.close()

    metrics = data.entity_metrics(project, "assembly", "ASM_000001")
    qc_rows = [row for row in metrics["qc"] if row["metric_name"] == "test_metric_n50"]
    assert len(qc_rows) == 1, "one row per metric name"
    assert qc_rows[0]["metric_value"] == "250", "newest QC measurement wins"

    assert [
        (row["analysis_name"], row["metric_name"], row["metric_value"])
        for row in metrics["analysis"]
    ] == [("busco", "complete", "95.0"), ("busco", "fragmented", "2.0")]
    assert all("finished_at" not in row and "result_id" not in row for row in metrics["analysis"])


def test_list_decisions_without_limit(demo_template: Project) -> None:
    """``limit=0`` means "no row limit" rather than "no rows"."""
    rows = data.list_decisions(demo_template, limit=0)
    assert rows
    assert rows == data.list_decisions(demo_template, limit=1000)


def test_list_tools_surfaces_invalid_recipe(project: Project) -> None:
    """A malformed tools.yaml entry is reported instead of breaking the list."""
    import yaml

    config = yaml.safe_load(project.tools_config_path.read_text(encoding="utf-8"))
    assert config.get("tools"), "demo tools.yaml should define tools"
    config["tools"]["broken_tool"] = {"run_method": {"mode": "conda"}}
    project.tools_config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    rows = {row["name"]: row for row in data.list_tools(project)}
    broken = rows["broken_tool"]
    assert broken["executable"] == "" and broken["run_method"] == "" and broken["recipes"] == []
    assert broken["description"] == (
        "invalid: tool broken_tool: conda launcher requires 'env'")
    valid = [row for name, row in rows.items() if name != "broken_tool"]
    assert valid and all(row["executable"] for row in valid)


def test_workflow_run_detail_survives_corrupt_environment_document(project: Project) -> None:
    """A corrupt environment document leaves the run readable with no summary."""
    from operon.workflow import log_run

    db = Database(project.db_path)
    try:
        db.insert_row("execution_environments", {
            "environment_id": "env_corrupt", "document": "{not json",
            "created_at": "2026-01-01T00:00:00+00:00",
        })
        run = log_run(db, project, {
            "step": "qc", "status": "completed", "environment_id": "env_corrupt",
        })
    finally:
        db.close()

    detail = data.workflow_run_detail(project, run["run_id"])
    assert detail["run_id"] == run["run_id"]
    assert detail["environment_summary"] is None


def test_entity_tree_hides_retired_runs(tmp_path: Path) -> None:
    """A retired sample hides its runs, which are still listed when included."""
    from operon.lifecycle import apply_lifecycle_event

    project = Project.init(tmp_path / "retired-runs")
    db = Database(project.db_path)
    try:
        db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Doomed"})
        db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        db.insert_row("runs", {"run_id": "RUN_000001", "sample_id": "SMP_000001"})
        apply_lifecycle_event(
            db, "sample", "SMP_000001",
            action="RETIRE", reason="test retirement", actor="tester", reason_code="duplicate",
        )
    finally:
        db.close()

    hidden = data.entity_tree(project)
    assert [node["entity_id"] for node in hidden] == ["ORG_000001"]
    assert hidden[0]["children"] == [], "the retired sample and its run are hidden"
    tree = data.entity_tree(project, include_retired=True)
    sample = tree[0]["children"][0]
    assert sample["entity_id"] == "SMP_000001"
    assert [child["entity_id"] for child in sample["children"]] == ["RUN_000001"]
    assert sample["children"][0]["retired"] is True  # retirement is inherited


def test_pickers_work_without_lifecycle_schema(tmp_path: Path) -> None:
    """Databases predating retirement have no view to filter on; all rows show."""
    import sqlite3

    db_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    conn.execute("INSERT INTO organisms (organism_id, scientific_name) VALUES ('ORG_000001', 'Legacy')")
    conn.execute("INSERT INTO samples (sample_id, organism_id, strain) "
                 "VALUES ('SMP_000001', 'ORG_000001', 'legacy strain')")
    conn.commit()
    conn.close()

    project = Project.init(tmp_path / "legacy-project")
    project.db_path.unlink()
    db_path.rename(project.db_path)

    assert [row["organism_id"] for row in data.list_organisms_for_picker(project)] == ["ORG_000001"]
    assert [row["sample_id"] for row in data.list_samples_for_picker(project, "ORG_000001")] == ["SMP_000001"]


def test_list_releases_tolerates_empty_and_corrupt_summaries(project: Project) -> None:
    """Undecodable or empty release summaries are returned verbatim."""
    db = Database(project.db_path)
    try:
        db.insert_row("releases", {
            "version": "v-empty", "created_at": "2026-01-01T00:00:00+00:00",
            "profile": "p", "path": "/tmp/v-empty", "manifest_sha256": "ab", "summary": "",
        })
        db.insert_row("releases", {
            "version": "v-corrupt", "created_at": "2026-02-01T00:00:00+00:00",
            "profile": "p", "path": "/tmp/v-corrupt", "manifest_sha256": "cd",
            "summary": "{not json",
        })
    finally:
        db.close()

    rows = {row["version"]: row for row in data.list_releases(project)}
    assert rows["v-empty"]["summary"] == ""
    assert rows["v-corrupt"]["summary"] == "{not json"
    assert isinstance(rows["2026.08.demo"]["summary"], dict)


def test_coverage_report_discovery_and_parsing(project: Project) -> None:
    """Report discovery skips non-reports and tolerates missing/corrupt provenance."""
    root = project.reports_root / "coverage"
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.txt").write_text("not a report", encoding="utf-8")
    (root / "COV_nothex").mkdir()
    (root / "COV_0BAD").mkdir()
    (root / "COV_0BAD" / "provenance.json").write_text("{not json", encoding="utf-8")
    (root / "COV_0C3D").mkdir()  # report directory without any provenance
    complete = root / "COV_0FED"
    complete.mkdir()
    (complete / "provenance.json").write_text(json.dumps({
        "reference_set_id": "RS_1", "scope_kind": "metadata", "scope_value": None,
        "decision": "PASS", "created_at": "2026-04-01T00:00:00+00:00",
    }), encoding="utf-8")
    (complete / "coverage_summary.tsv").write_text(
        "family\tcoverage_percent\nfamA\t100.0\n", encoding="utf-8")

    listed = {row["report_id"]: row for row in data.list_coverage_reports(project)}
    assert set(listed) == {"COV_0BAD", "COV_0C3D", "COV_0FED"}
    assert listed["COV_0FED"]["reference_set_id"] == "RS_1"
    assert listed["COV_0FED"]["decision"] == "PASS"
    assert listed["COV_0BAD"]["reference_set_id"] == "?"
    assert listed["COV_0BAD"]["decision"] == "?"
    assert listed["COV_0C3D"]["scope_kind"] == "?"
    assert listed["COV_0C3D"]["scope_value"] is None

    assert data.read_coverage_report(project, "COV_0C3D") == {
        "report_id": "COV_0C3D", "path": str(root / "COV_0C3D"),
        "provenance": {}, "tables": {},
    }
    report = data.read_coverage_report(project, "COV_0BAD")
    assert report["provenance"] == {}
    assert report["tables"] == {}
    parsed = data.read_coverage_report(project, "COV_0FED")
    assert parsed["provenance"]["reference_set_id"] == "RS_1"
    assert parsed["tables"]["coverage_summary"]["columns"] == ["family", "coverage_percent"]
    assert parsed["tables"]["coverage_summary"]["rows"] == [["famA", "100.0"]]
    assert parsed["tables"]["coverage_summary"]["truncated"] is False
    assert set(parsed["tables"]) == {"coverage_summary"}


# ---------------------------------------------------------------------------
# Entities screen
# ---------------------------------------------------------------------------


def test_entities_lifecycle_action_guards(demo_template: Project) -> None:
    """`x` explains a missing selection and opens the modal for a chosen entity."""

    async def scenario() -> None:
        app = OperonApp(demo_template)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("entities")
            await _settled(app)
            panel = app.query_one(EntitiesPanel)
            assert panel.detail is None
            app.clear_notifications()
            panel.action_lifecycle()
            await pilot.pause()
            assert not isinstance(app.screen, LifecycleModal)
            assert ("warning", "select an entity first") in _notifications(app)

            panel._apply_detail(data.entity_detail(demo_template, "assembly", "ASM_000001"))
            panel.action_lifecycle()
            await pilot.pause()
            assert isinstance(app.screen, LifecycleModal)
            assert (app.screen.entity_type, app.screen.entity_id) == ("assembly", "ASM_000001")
            assert app.screen.action == "RETIRE"
            await pilot.press("escape")
            await _settled(app)
            assert not isinstance(app.screen, LifecycleModal)

            panel._after_lifecycle(None)
            await pilot.pause()
            assert not app.workers

    _run(scenario())


def test_entities_tree_highlight_loads_detail(demo_template: Project) -> None:
    """Highlighting a data-less root clears nothing; a real node loads its detail."""

    async def scenario() -> None:
        app = OperonApp(demo_template)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("entities")
            await _settled(app)
            panel = app.query_one(EntitiesPanel)
            tree = panel.query_one("#entities-tree", Tree)

            tree.move_cursor(tree.root.children[0])
            await _wait_until(lambda: panel.detail is not None, "detail after highlight")
            assert panel.detail["entity_id"] == "ORG_000001"
            assert "Syntheticus alpha" in _static_text(panel.query_one("#entity-detail", Static))

            panel.detail = None
            tree.move_cursor(tree.root)
            await pilot.pause()
            assert panel.detail is None

            # Selecting the data-less root must not load a detail either.
            tree.select_node(tree.root)
            await pilot.pause()
            assert panel.detail is None

    _run(scenario())


def test_entities_detail_error_paths(demo_template: Project, monkeypatch) -> None:
    """A failing entity read and a late error payload both render inline."""

    def boom(*args, **kwargs):
        raise RuntimeError("entity read failed")

    async def scenario() -> None:
        app = OperonApp(demo_template)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("entities")
            await _settled(app)
            panel = app.query_one(EntitiesPanel)

            monkeypatch.setattr(data, "entity_detail", boom)
            panel._load_detail("assembly", "ASM_000001")
            await _wait_until(
                lambda: "entity read failed" in _static_text(
                    panel.query_one("#entity-detail", Static)),
                "inline entity error",
            )
            assert panel.detail is None

            panel._apply_detail(RuntimeError("late failure"))
            assert "late failure" in _static_text(panel.query_one("#entity-detail", Static))

            with _shutdown_guard(app) as patch:
                patch.setattr(type(app), "is_running", property(lambda self: False))
                EntitiesPanel._load_detail.__wrapped__(panel, "assembly", "ASM_000001")
            assert panel.detail is None

    _run(scenario())


def test_lifecycle_preview_failure_is_shown_inline(demo_template: Project, monkeypatch) -> None:
    """A preview that cannot be computed is reported inside the modal."""

    def boom(*args, **kwargs):
        raise RuntimeError("preview unavailable")

    async def scenario() -> None:
        app = OperonApp(demo_template)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            monkeypatch.setattr(
                "operon.tui.screens.entities.actions.lifecycle_preview", boom)
            modal = LifecycleModal(demo_template, "assembly", "ASM_000001", False)
            app.push_screen(modal)
            await pilot.pause()
            await _wait_until(
                lambda: "preview unavailable" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "inline preview error",
            )
            assert modal.plan is None
            assert modal.query_one("#confirm", Button).disabled
            await pilot.press("escape")

    _run(scenario())


def test_lifecycle_modal_plan_validation_and_render(demo_template: Project) -> None:
    """Plan preview, blocker rendering, validation, and result notification."""
    captured: list[Any] = []

    async def scenario() -> None:
        app = OperonApp(demo_template)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = LifecycleModal(demo_template, "assembly", "ASM_000001", False)
            app.push_screen(modal, captured.append)
            await _wait_until(lambda: modal.plan is not None, "lifecycle plan preview")
            assert modal.plan["action"] == "RETIRE"
            assert modal.plan["will_change"] is True

            plan_text = _static_text(modal.query_one("#lifecycle-plan", Static))
            assert "action   RETIRE" in plan_text
            assert "target   assembly ASM_000001" in plan_text
            assert "entities assembly:1  annotation:1" in plan_text
            assert "files    3 affected" in plan_text
            assert "refs     accessions:1" in plan_text
            assert not modal.query_one("#confirm", Button).disabled
            assert modal._reason_code() == "other"

            modal.query_one("#lifecycle-reason", Input).value = "duplicate"
            modal.query_one("#lifecycle-actor", Input).value = "tester"
            modal.query_one("#lifecycle-evidence", Input).value = "ticket-42"
            assert modal.command_text() == (
                "operon retire ASM_000001 --reason duplicate --reason-code other "
                "--actor tester --evidence ticket-42 --apply --yes"
            )
            modal.query_one("#lifecycle-actor", Input).value = ""
            modal.query_one("#lifecycle-evidence", Input).value = ""
            assert modal.command_text() == (
                "operon retire ASM_000001 --reason duplicate --reason-code other --apply --yes"
            )

            modal._apply_plan(RuntimeError("preview exploded"))
            assert "preview exploded" in _static_text(modal.query_one("#modal-error", Static))

            blocked = dict(modal.plan)
            blocked["will_change"] = False
            blocked["blocker"] = "release members reference this entity"
            blocked["physical_changes"] = {
                "metadata_rows_deleted": 0, "artifact_paths_moved": 2,
            }
            modal._apply_plan(blocked)
            plan_text = _static_text(modal.query_one("#lifecycle-plan", Static))
            assert "release members reference this entity" in plan_text
            assert "artifact_paths_moved=2" in plan_text
            assert modal.query_one("#confirm", Button).disabled

            modal.plan = None
            modal.clear_error()
            modal.confirm()
            assert app.screen is modal
            assert _static_text(modal.query_one("#modal-error", Static)) == ""

            modal.plan = dict(blocked, will_change=True)
            modal.query_one("#lifecycle-reason", Input).value = ""
            modal.confirm()
            assert "reason is required" in _static_text(modal.query_one("#modal-error", Static))
            modal.query_one("#lifecycle-reason", Input).value = "duplicate"
            modal.query_one("#lifecycle-actor", Input).value = ""
            modal.confirm()
            assert "actor is required" in _static_text(modal.query_one("#modal-error", Static))

            # A foreign widget's change event must not disturb the command line.
            command = modal.command_text()
            modal.on_input_changed(Input.Changed(input=Input(id="other-input"), value="zzz"))
            modal.on_select_changed(Select.Changed(Select([("A", "a")], id="other-select"), "a"))
            assert modal.command_text() == command

            app.clear_notifications()
            modal.on_action_success({"applied": False})
            await pilot.pause()
            assert captured == [{"applied": False}]
            assert ("information", "no change: assembly ASM_000001") in _notifications(app)

    _run(scenario())


def test_lifecycle_plan_ignores_late_preview_during_shutdown(demo_template: Project) -> None:
    """A preview that finishes while the app is closing is never applied."""
    captured: list[Any] = []

    async def scenario() -> None:
        app = OperonApp(demo_template)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = LifecycleModal(demo_template, "assembly", "ASM_000001", False)
            app.push_screen(modal, captured.append)
            await _wait_until(lambda: modal.plan is not None, "lifecycle plan preview")

            modal.plan = None
            with _shutdown_guard(app) as patch:
                patch.setattr(type(app), "is_running", property(lambda self: False))
                LifecycleModal._load_plan.__wrapped__(modal)
            assert modal.plan is None
            assert _static_text(modal.query_one("#modal-error", Static)) == ""
            assert captured == []

            await pilot.press("escape")

    _run(scenario())


# ---------------------------------------------------------------------------
# Coverage screen
# ---------------------------------------------------------------------------


def _write_coverage_report(project: Project, report_id: str, **provenance: Any) -> Path:
    path = project.reports_root / "coverage" / report_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    return path


def _reference_set_row(reference_set_id: str = "RS_1") -> dict[str, Any]:
    return {
        "reference_set_id": reference_set_id, "profile_name": "coverage_v1",
        "profile_version": 1, "taxonomy_version": "2025-01-01",
        "family_count": 3, "genus_count": 7, "compiled_at": "2026-03-01T00:00:00+00:00",
    }


def test_coverage_panel_report_browsing(project: Project) -> None:
    """A discovered report renders its scope, tables, and truncation notice."""
    report = _write_coverage_report(
        project, "COV_00A1", reference_set_id="RS_1", scope_kind="release",
        scope_value="2026.08.demo", decision="FAIL",
        created_at="2026-05-01T00:00:00+00:00",
    )
    rows = [["family", "coverage_percent"]]
    rows += [[f"fam{index}", "1.0"] for index in range(data.COVERAGE_REPORT_LIMIT + 3)]
    (report / "coverage_summary.tsv").write_text(
        "\n".join("\t".join(row) for row in rows) + "\n", encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("coverage")
            await _settled(app)
            panel = app.query_one(CoveragePanel)
            table = panel.query_one("#coverage-reports-table", DataTable)
            assert table.row_count == 1
            assert _cell_text(table, 0, 2) == "release:2026.08.demo"
            assert _cell_text(table, 0, 3) == "FAIL"

            panel.on_data_table_row_selected(
                DataTable.RowSelected(panel.query_one("#taxonomy-snapshots-table", DataTable),
                                      0, RowKey("other")))
            panel.on_button_pressed(Button.Pressed(Button("other", id="other")))
            assert panel.report is None

            panel._apply_report(data.read_coverage_report(project, "COV_00A1"))
            await pilot.pause()
            headline = _static_text(panel.query_one("#coverage-report-headline", Static))
            assert "COV_00A1" in headline
            assert "reference set RS_1" in headline
            assert "scope release:2026.08.demo" in headline
            assert "decision FAIL" in headline

            summary = panel.query_one("#coverage-table-coverage_summary", DataTable)
            assert summary.row_count == data.COVERAGE_REPORT_LIMIT + 1
            assert "… 3 more rows" in _cell_text(summary, summary.row_count - 1, 0)
            targets = panel.query_one("#coverage-table-coverage_targets", DataTable)
            assert [str(column.label) for column in targets.columns.values()] == ["(no data)"]

    _run(scenario())


def test_coverage_panel_report_load_failure(project: Project, monkeypatch) -> None:
    """An unreadable report is reported in the headline, not raised."""

    def boom(*args, **kwargs):
        raise RuntimeError("report unreadable")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("coverage")
            await _settled(app)
            panel = app.query_one(CoveragePanel)
            monkeypatch.setattr(data, "read_coverage_report", boom)
            panel._load_report("COV_0000")
            await _wait_until(
                lambda: "report unreadable" in _static_text(
                    panel.query_one("#coverage-report-headline", Static)),
                "report error headline",
            )
            assert panel.report is None

            panel._apply_report(RuntimeError("late failure"))
            assert "late failure" in _static_text(
                panel.query_one("#coverage-report-headline", Static))

            with _shutdown_guard(app) as patch:
                patch.setattr(type(app), "is_running", property(lambda self: False))
                CoveragePanel._load_report.__wrapped__(panel, "COV_0000")
            assert panel.report is None

    _run(scenario())


def test_coverage_generate_scope_guards(project: Project) -> None:
    """Generation requires a reference set and, for release scope, a release."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("coverage")
            await _settled(app)
            panel = app.query_one(CoveragePanel)
            panel.render_data({
                "snapshots": [],
                "reference_sets": [_reference_set_row()],
                "releases": [{"version": "2026.08.demo"}],
                "reports": [],
            })
            reference = panel.query_one("#coverage-reference-set", Select)
            error = panel.query_one("#coverage-error", Static)

            reference.value = Select.NULL
            panel._generate()
            assert "select a reference set first" in _static_text(error)
            assert not isinstance(app.screen, CoverageModal)

            reference.value = "RS_1"
            scope = panel.query_one("#coverage-scope", Select)
            scope.value = "release"
            await pilot.pause()
            assert panel.query_one("#coverage-release", Select).display is True

            panel.query_one("#coverage-release", Select).value = Select.NULL
            panel._generate()
            assert "select a release for release-scope coverage" in _static_text(error)
            assert not isinstance(app.screen, CoverageModal)

            panel.query_one("#coverage-release", Select).value = "2026.08.demo"
            panel._generate()
            await pilot.pause()
            assert isinstance(app.screen, CoverageModal)
            assert app.screen.release_version == "2026.08.demo"
            assert app.screen.command_text() == (
                "operon report coverage --reference-set RS_1 --release 2026.08.demo")
            assert "frozen release 2026.08.demo" in _form_text(app.screen)
            await pilot.press("escape")
            await pilot.pause()

            panel._after_generate(None)
            await pilot.pause()
            assert not app.workers
            panel._after_generate({"decision": "PASS"})
            assert app.workers, "a successful generation reloads every panel"
            await _settled(app)

    _run(scenario())


def test_coverage_modal_warns_on_below_threshold_result(project: Project) -> None:
    """A FAIL result (CLI exit 1) is a warning notification naming the report."""
    payload = {
        "decision": "FAIL", "exit_code": 1, "path": "/tmp/reports/coverage/COV_00A1",
        "reused": True,
        "metrics": [{"rank": "family", "coverage_percent": 12.5, "threshold_percent": 80.0}],
    }
    captured: list[Any] = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = CoverageModal(project, "RS_1", "2026.08.demo")
            app.push_screen(modal, captured.append)
            await pilot.pause()
            app.clear_notifications()
            modal.on_action_success(payload)
            await pilot.pause()
            assert captured == [payload]
            assert ("warning", "coverage FAIL: family 12.50% (min 80.00%) "
                               "(reused cached report) — report /tmp/reports/coverage/COV_00A1"
                    ) in _notifications(app)

    _run(scenario())


# ---------------------------------------------------------------------------
# Files screen and file write modals
# ---------------------------------------------------------------------------


def test_files_panel_filters_and_stale_selection(demo_template: Project) -> None:
    """The status filter survives a reload; a stale cursor selects nothing."""

    async def scenario() -> None:
        app = OperonApp(demo_template)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await _settled(app)
            panel = app.query_one(FilesPanel)
            table = panel.query_one("#files-table", DataTable)
            total = table.row_count
            assert total >= 9

            select = panel.query_one("#files-status", Select)
            select.value = "STANDARDIZED"
            await pilot.pause()
            await _settled(app)
            assert 0 < table.row_count <= total

            # A reload that discovers statuses keeps the active status filter.
            panel.statuses = ["MISSING"]
            panel.render_data(panel._fetch())
            select = panel.query_one("#files-status", Select)
            assert select.value == "STANDARDIZED"
            assert all(record["status"] == "STANDARDIZED" for record in panel.files)

            # The cursor can outlive a reload that shrank the file list.
            panel.render_data({"files": panel.files[:1], "statuses": panel.statuses})
            table.add_row("stale", "x", "y", "z", "0 B", "0", "MISSING", key="stale-row")
            table.move_cursor(row=table.row_count - 1, animate=False)
            assert panel._selected_record() is None

            panel.files = []
            table.clear()
            assert panel._selected_record() is None

            # Unrelated widgets never trigger the panel's own handlers.
            panel.on_input_changed(Input.Changed(input=Input(id="other-input"), value="zzz"))
            panel.on_select_changed(Select.Changed(Select([("A", "a")], id="other-select"), "a"))
            other = DataTable(id="other-table")
            panel.detail = None
            panel.on_data_table_row_highlighted(
                DataTable.RowHighlighted(other, 0, RowKey("k")))
            panel.on_data_table_row_selected(DataTable.RowSelected(other, 0, RowKey("k")))
            panel.on_input_changed(Input.Changed(input=Input(id="other-input"), value="zzz"))
            panel.on_select_changed(Select.Changed(Select([("A", "a")], id="other-select"), "a"))
            assert panel.detail is None

    _run(scenario())


def test_files_detail_rendering_and_errors(demo_template: Project, monkeypatch) -> None:
    """Residency rows render verification provenance; read failures stay inline."""

    def boom(*args, **kwargs):
        raise RuntimeError("file read failed")

    async def scenario() -> None:
        app = OperonApp(demo_template)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await _settled(app)
            panel = app.query_one(FilesPanel)

            detail = {
                "file": {"file_id": "FIL_X", "entity_type": "run", "entity_id": "RUN_1",
                         "file_role": "reads_r1", "format": "fastq", "compression": "none",
                         "relative_path": "raw/reads/x.fastq", "size_bytes": 2048,
                         "sha256": "ab" * 32, "status": "MISSING"},
                "locations": [
                    {"location_name": "archive", "location_type": "sftp", "uri": "sftp://h/x",
                     "status": "AVAILABLE", "verified_at": "2026-01-02"},
                    {"location_name": "mirror", "location_type": "sftp", "uri": "sftp://h/y",
                     "status": "REMOTE_UNVERIFIED"},
                ],
            }
            text = panel._detail_text(detail).plain
            assert text.count("verified ") == 1
            assert "archive" in text and "mirror" in text

            monkeypatch.setattr(data, "file_detail", boom)
            panel.detail = None
            panel._load_detail("FIL_000001")
            await _wait_until(
                lambda: "file read failed" in _static_text(
                    panel.query_one("#file-detail", Static)),
                "inline file error",
            )
            assert panel.detail is None

            panel._apply_detail(RuntimeError("late failure"))
            assert "late failure" in _static_text(panel.query_one("#file-detail", Static))

            with _shutdown_guard(app) as patch:
                patch.setattr(type(app), "is_running", property(lambda self: False))
                FilesPanel._load_detail.__wrapped__(panel, "FIL_000001")
            assert panel.detail is None

    _run(scenario())


def test_files_write_callbacks_report_outcomes(demo_template: Project) -> None:
    """Verify/QC callbacks summarise results, open an error dialog, or do nothing."""

    async def scenario() -> None:
        app = OperonApp(demo_template)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await _settled(app)
            panel = app.query_one(FilesPanel)

            # A None result is a no-op: no worker is started, no dialog appears.
            panel._after_verify(None)
            assert not app.workers, "an empty result must not start a worker"
            await pilot.pause()
            assert not isinstance(app.screen, ErrorDialog)

            app.clear_notifications()
            panel._after_verify([{"file_id": "FIL_1", "status": "CHECKSUM_VERIFIED"}])
            await _wait_until(
                lambda: ("information", "verified 1 file(s)") in _notifications(app),
                "verify summary notification",
            )

            panel._after_verify([
                {"file_id": "FIL_1", "status": "CHECKSUM_FAILED", "error": "digest mismatch"},
                {"file_id": "FIL_2", "status": "CHECKSUM_VERIFIED"},
            ])
            # Wait on the dialog content, not just its type: a pushed screen
            # mounts its children a few frames later.
            await _wait_until(
                lambda: "1 of 2 file(s) failed verification"
                in _screen_text(app.screen, "#modal-title"),
                "verify error dialog",
            )
            body = _screen_text(app.screen, "#error-dialog-body")
            assert "FIL_1: CHECKSUM_FAILED — digest mismatch" in body
            assert isinstance(app.screen, ErrorDialog)
            await pilot.press("escape")
            await _wait_until(
                lambda: not isinstance(app.screen, ErrorDialog), "verify dialog closed")

            panel._after_qc({"cancelled": True})
            await pilot.pause()
            assert not isinstance(app.screen, ErrorDialog)

            panel._after_qc({"ok": 1, "total": 2, "failures": [{"file_id": "FIL_9"}]})
            await _wait_until(
                lambda: "1 of 2 file(s) failed QC" in _screen_text(app.screen, "#modal-title"),
                "QC error dialog",
            )
            body = _screen_text(app.screen, "#error-dialog-body")
            assert "FIL_9: failed" in body
            await pilot.press("escape")
            await _wait_until(
                lambda: not isinstance(app.screen, ErrorDialog), "QC dialog closed")
            await _settled(app)

    _run(scenario())


def test_files_actions_default_to_every_file(tmp_path: Path) -> None:
    """With no rows selected, verify/QC target every manifest file."""
    empty = Project.init(tmp_path / "empty-files")

    async def scenario() -> None:
        app = OperonApp(empty)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await _settled(app)
            panel = app.query_one(FilesPanel)
            assert panel.files == []

            panel.action_verify()
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, VerifyModal)
            assert (modal.file_id, modal.total) == (None, 0)
            assert "Verify all 0 files?" in _form_text(modal)
            assert modal.command_text() == "operon verify"
            await pilot.press("escape")
            await pilot.pause()

            panel.action_qc()
            await pilot.pause()
            assert isinstance(app.screen, QcModal)
            assert app.screen.command_text() == "operon qc"
            assert "Run built-in QC stages for all 0 files?" in _form_text(app.screen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, QcModal)

    _run(scenario())


def test_ingest_modal_command_and_failed_action(project: Project, monkeypatch) -> None:
    """Ingest previews every flag and keeps a failed action inside the modal."""
    captured: list[Any] = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = IngestModal(project, {
                "entity_type": "assembly", "entity_id": "ASM_000001",
                "file_role": "genome_fasta",
            })
            app.push_screen(modal, captured.append)
            await pilot.pause()
            modal.query_one("#ingest-source", Input).value = "/data/asm.fasta.gz"
            modal.query_one("#ingest-format", Input).value = "fasta"
            modal.query_one("#ingest-compression", Input).value = "gzip"
            modal.query_one("#ingest-source-url", Input).value = "https://example.org/asm"
            modal.query_one("#ingest-move", Checkbox).value = True
            command = modal.command_text()
            assert command.startswith("operon ingest --source /data/asm.fasta.gz ")
            assert "--entity-type assembly --entity-id ASM_000001 --role genome_fasta" in command
            assert "--format fasta --compression gzip --source-url https://example.org/asm" in command
            assert command.endswith("--move")

            # Unrelated widgets never rewrite the preview.
            modal.on_input_changed(Input.Changed(input=Input(id="other-input"), value="zzz"))
            modal.on_select_changed(Select.Changed(Select([("A", "a")], id="other"), "a"))
            modal.on_checkbox_changed(Checkbox.Changed(Checkbox("other", id="other-check"), True))
            assert modal.command_text() == command

            def failing_ingest(*args, **kwargs):
                raise RuntimeError("source not found")

            monkeypatch.setattr(files_ops_module.actions, "ingest", failing_ingest)
            modal.confirm()
            await _wait_until(
                lambda: "source not found" in _static_text(
                    modal.query_one("#modal-error", Static)),
                "inline ingest error",
            )
            assert app.screen is modal
            assert not modal.query_one("#confirm", Button).disabled
            assert captured == []

            app.clear_notifications()
            modal.query_one("#ingest-source", Input).value = ""
            modal.confirm()
            assert "source is required" in _static_text(modal.query_one("#modal-error", Static))
            modal.query_one("#ingest-source", Input).value = "/data/asm.fasta.gz"
            modal.query_one("#ingest-entity-id", Input).value = ""
            modal.confirm()
            assert "entity id is required" in _static_text(modal.query_one("#modal-error", Static))
            await pilot.press("escape")

    _run(scenario())


def test_qc_modal_cancel_and_failure_paths(project: Project, monkeypatch) -> None:
    """Cancelling a running batch reports partial progress; crashes stay inline."""
    released = threading.Event()
    cancelled_payloads: list[Any] = []

    def blocking_run_qc(project_arg, *, file_id=None, progress=None):
        if not released.wait(10):
            raise AssertionError("test never released the QC stub")
        progress(1, 2, {"ok": True, "file_id": "FIL_000001"})
        return []

    def failing_run_qc(project_arg, *, file_id=None, progress=None):
        raise RuntimeError("qc exploded")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            monkeypatch.setattr(files_ops_module.actions, "run_qc", blocking_run_qc)
            modal = QcModal(project, None, 2)
            app.push_screen(modal, cancelled_payloads.append)
            await pilot.pause()
            modal.confirm()
            await pilot.pause()
            assert modal.running
            assert modal.query_one("#qc-progress").display is True

            modal.on_button_pressed(Button.Pressed(modal.query_one("#cancel", Button)))
            assert modal._worker.is_cancelled
            modal.action_cancel()  # a queued escape cancels again without dismissing
            assert app.screen is modal
            released.set()
            await _wait_until(lambda: cancelled_payloads, "cancelled QC dismissal")
            assert cancelled_payloads == [{"cancelled": True, "done": 0, "total": 2}]
            assert any("QC cancelled after 0/2 file(s)" in message
                       for _severity, message in _notifications(app))

            # Without a live worker, cancelling leaves the modal open.
            modal = QcModal(project, None, 2)
            app.push_screen(modal)
            await pilot.pause()
            modal.running = True
            modal._worker = None
            modal.on_button_pressed(Button.Pressed(modal.query_one("#cancel", Button)))
            modal.action_cancel()
            assert app.screen is modal
            modal.running = False

            monkeypatch.setattr(files_ops_module.actions, "run_qc", failing_run_qc)
            failing = QcModal(project, "FIL_000001", 1)
            app.push_screen(failing)
            await pilot.pause()
            failing.confirm()
            await _wait_until(lambda: not failing.running, "QC failure handling")
            assert "qc exploded" in _static_text(failing.query_one("#modal-error", Static))
            assert not failing.query_one("#confirm", Button).disabled
            assert app.screen is failing

            with _shutdown_guard(app) as patch:
                patch.setattr(type(app), "is_running", property(lambda self: False))
                patch.setattr("textual.worker.get_current_worker",
                              lambda: SimpleNamespace(is_cancelled=False))
                app.clear_notifications()
                QcModal._run_qc.__wrapped__(failing)
            assert app.screen is failing
            assert _notifications(app) == []
            await pilot.press("escape")

    try:
        _run(scenario())
    finally:
        released.set()


# ---------------------------------------------------------------------------
# Decisions screen
# ---------------------------------------------------------------------------


def test_decisions_render_data_markers_and_reasons(project: Project) -> None:
    """Curated rows are marked, undecodable reason codes fall back to raw text."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            panel = app.query_one(DecisionsPanel)
            rows = [
                {"entity_type": "assembly", "entity_id": "ASM_000001", "profile": "reads_qc_v1",
                 "decision": "REVIEW", "curated_decision": "PASS", "curated_by": "tester",
                 "curated_reason": "manual", "curated_at": "2026-01-01T00:00:00+00:00",
                 "reason_codes": '["low_n50", "many_contigs"]',
                 "evaluated_at": "2026-01-01T00:00:00+00:00"},
                {"entity_type": "run", "entity_id": "RUN_000001", "profile": "reads_qc_v1",
                 "decision": "FAIL", "curated_decision": None, "curated_by": None,
                 "curated_reason": None, "curated_at": None, "reason_codes": "not json",
                 "evaluated_at": None},
            ]
            panel.profiles = []
            panel.render_data({"decisions": rows, "profiles": ["reads_qc_v1", "assembly_qc_v1"]})
            table = panel.query_one("#decisions-table", DataTable)
            assert table.row_count == 2
            assert _cell_text(table, 0, 2) == "PASS ✎curated"
            assert _cell_text(table, 0, 3) == "low_n50, many_contigs"
            assert _cell_text(table, 1, 2) == "FAIL"
            assert _cell_text(table, 1, 3) == "not json"
            assert _cell_text(table, 1, 4) == "-"
            assert panel.profiles == ["reads_qc_v1", "assembly_qc_v1"]

            panel.on_select_changed(Select.Changed(Select([("A", "a")], id="other"), "a"))

    _run(scenario())


def test_decisions_profile_filter_survives_new_profiles(project: Project) -> None:
    """A selected profile filter is kept when the screen reloads the profile list."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("decisions")
            await _settled(app)
            panel = app.query_one(DecisionsPanel)
            select = panel.query_one("#decisions-profile", Select)
            recorded = {row["profile"] for row in panel.decisions}
            assert recorded
            available = [value for _label, value in select._options if value in recorded]
            assert available
            before = list(panel.profiles)
            select.value = available[0]
            # Wait for the reload to actually reflect the selected profile
            # instead of assuming a fixed number of pause() cycles.
            await _wait_until(
                lambda: panel.decisions
                and all(row["profile"] == available[0] for row in panel.decisions),
                "profile filter applied",
            )

            (project.profiles_dir / "extra_profile.yaml").write_text(
                "kind: qc\nversion: 1\n", encoding="utf-8")
            panel.reload()
            await _wait_until(
                lambda: panel.profiles == sorted([*before, "extra_profile"])
                and any(value == available[0] for _label, value in select._options),
                "profile list reloaded",
            )
            assert select.value == available[0], "the active filter survives the refresh"

            panel.query_one("#decisions-filter", Input).value = panel.decisions[0]["entity_id"]
            entity_id = panel.decisions[0]["entity_id"]
            await _wait_until(
                lambda: panel.decisions
                and all(row["entity_id"] == entity_id for row in panel.decisions),
                "entity filter applied",
            )
            assert all(row["entity_id"] == entity_id for row in panel.decisions)
            panel.on_input_changed(Input.Changed(input=Input(id="other-input"), value="zzz"))
            assert all(row["entity_id"] == entity_id for row in panel.decisions)

    _run(scenario())


def test_decisions_actions_require_a_selected_row(project: Project) -> None:
    """Curate without a usable row warns instead of opening a modal."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("decisions")
            await _settled(app)
            panel = app.query_one(DecisionsPanel)
            table = panel.query_one("#decisions-table", DataTable)

            panel.decisions = []
            table.clear()
            assert panel._selected_row() is None
            app.clear_notifications()
            panel.action_curate()
            await pilot.pause()
            assert not isinstance(app.screen, CurateModal)
            assert ("warning", "select a decision row first") in _notifications(app)

            panel.render_data({"decisions": panel.decisions or [], "profiles": []})
            single = {
                "entity_type": "assembly", "entity_id": "ASM_000001", "profile": "reads_qc_v1",
                "decision": "REVIEW", "curated_decision": None, "reason_codes": "[]",
                "evaluated_at": "2026-01-01T00:00:00+00:00",
            }
            panel.render_data({"decisions": [single], "profiles": []})
            table.add_row("stale", "row", "y", "z", "w", key="stale-row")
            table.move_cursor(row=table.row_count - 1, animate=False)
            assert panel._selected_row() is None
            app.clear_notifications()
            panel.action_curate()
            await pilot.pause()
            assert not isinstance(app.screen, CurateModal)
            assert ("warning", "select a decision row first") in _notifications(app)

            table.move_cursor(row=0, animate=False)
            assert panel._selected_row() == single
            panel.action_curate()
            await pilot.pause()
            assert isinstance(app.screen, CurateModal)
            await pilot.press("escape")

    _run(scenario())


def test_curate_modal_validation_and_command(project: Project) -> None:
    """Curate requires a decision, reviewer, and reason before it can run."""
    row = {"entity_type": "assembly", "entity_id": "ASM_000001", "profile": "reads_qc_v1",
           "decision": "NOT_EVALUATED", "curated_decision": None}

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = CurateModal(project, row)
            app.push_screen(modal)
            await pilot.pause()
            assert modal._current() == "NOT_EVALUATED"
            assert modal._decision() == ""
            modal.confirm()
            assert "choose a new decision" in _static_text(modal.query_one("#modal-error", Static))

            modal.query_one("#curate-decision", Select).value = "PASS"
            modal.query_one("#curate-reviewer", Input).value = ""
            modal.confirm()
            assert "reviewer is required" in _static_text(modal.query_one("#modal-error", Static))

            modal.query_one("#curate-reviewer", Input).value = "tester"
            modal.confirm()
            assert "reason is required for a curated decision" in _static_text(
                modal.query_one("#modal-error", Static))

            modal.query_one("#curate-reason", Input).value = "manual override"
            modal.query_one("#curate-evidence", Input).value = "see ticket-7"
            assert modal._preview_text() == "NOT_EVALUATED → PASS"
            assert modal.command_text() == (
                "operon curate --entity-type assembly --entity-id ASM_000001 "
                "--profile reads_qc_v1 --decision PASS --reviewer tester "
                "--reason 'manual override' --evidence 'see ticket-7'"
            )
            modal.on_select_changed(Select.Changed(Select([("A", "a")], id="other"), "a"))
            modal.on_input_changed(Input.Changed(input=Input(id="other-input"), value="zzz"))
            assert modal.command_text().endswith("--evidence 'see ticket-7'")
            await pilot.press("escape")

    _run(scenario())


def test_evaluate_modal_scope_options(project: Project) -> None:
    """Without a selected row Evaluate only offers the all-entities scope."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = EvaluateModal(project, None)
            app.push_screen(modal)
            await pilot.pause()
            scope = modal.query_one("#evaluate-scope", Select)
            assert [value for _label, value in scope._options if value != Select.NULL] == ["all"]
            assert modal.command_text().startswith("operon evaluate --profile ")
            modal.on_select_changed(Select.Changed(Select([("A", "a")], id="other"), "a"))
            await pilot.press("escape")

            selected = EvaluateModal(project, {"entity_type": "assembly",
                                               "entity_id": "ASM_000001"})
            app.push_screen(selected)
            await pilot.pause()
            options = [value for _label, value in
                       selected.query_one("#evaluate-scope", Select)._options]
            assert options == ["all", "selected"]
            selected.query_one("#evaluate-scope", Select).value = "selected"
            assert "--entity-type assembly --entity-id ASM_000001" in selected.command_text()
            await pilot.press("escape")

    _run(scenario())


# ---------------------------------------------------------------------------
# Runs screen and app shell
# ---------------------------------------------------------------------------


def test_run_detail_error_paths_and_back_guard(demo_template: Project, monkeypatch) -> None:
    """A failed run read renders inline; a queued escape never pops a lower screen."""
    run_id = data.list_workflow_runs(demo_template, limit=1)[0]["run_id"]

    def boom(*args, **kwargs):
        raise RuntimeError("run read failed")

    async def scenario() -> None:
        app = OperonApp(demo_template)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)

            first = RunDetailScreen(demo_template, run_id)
            app.push_screen(first)
            await _wait_until(lambda: first.record is not None, "first run detail")
            await _wait_until(lambda: app.screen is first, "first screen active")
            second = RunDetailScreen(demo_template, run_id)
            app.push_screen(second)
            await _wait_until(lambda: second.record is not None, "second run detail")
            # The back-guard only applies once the second screen is on top.
            await _wait_until(lambda: app.screen is second, "second screen active")
            first.action_back()
            await pilot.pause()
            assert app.screen is second, "the inactive screen must not pop the active one"

            monkeypatch.setattr(data, "workflow_run_detail", boom)
            failing = RunDetailScreen(demo_template, run_id)
            app.push_screen(failing)
            await _wait_until(
                lambda: "run read failed" in _screen_text(failing, "#run-detail"),
                "inline run error",
            )
            assert failing.record is None

            failing._apply(RuntimeError("late failure"))
            assert "late failure" in _screen_text(failing, "#run-detail")

            shown = _screen_text(failing, "#run-detail")
            with _shutdown_guard(app) as patch:
                patch.setattr(type(app), "is_running", property(lambda self: False))
                failing.record = None
                RunDetailScreen._load.__wrapped__(failing)
            assert failing.record is None
            assert _screen_text(failing, "#run-detail") == shown
            failing.action_back()
            await _wait_until(lambda: app.screen is second, "back to the second screen")
            second.action_back()
            await _wait_until(lambda: app.screen is first, "back to the first screen")
            first.action_back()
            await _wait_until(
                lambda: not isinstance(app.screen, RunDetailScreen), "run detail popped")

    _run(scenario())


def test_runs_panel_ignores_foreign_events(demo_template: Project) -> None:
    """Handlers only react to their own widgets."""

    async def scenario() -> None:
        app = OperonApp(demo_template)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("runs")
            await _settled(app)
            panel = app.query_one(RunsPanel)
            table = panel.query_one("#runs-table", DataTable)
            before = table.row_count

            panel.on_input_changed(Input.Changed(input=Input(id="other-input"), value="zzz"))
            panel.on_select_changed(Select.Changed(Select([("A", "a")], id="other-select"), "a"))
            other = DataTable(id="other-table")
            panel.on_data_table_row_selected(DataTable.RowSelected(other, 0, RowKey("k")))
            await pilot.pause()
            assert table.row_count == before
            assert not isinstance(app.screen, RunDetailScreen)

            panel.query_one("#runs-limit", Input).value = ""
            await pilot.pause()
            await _settled(app)
            assert panel.runs, "an empty limit falls back to the default"

    _run(scenario())


def test_app_action_guards(project: Project) -> None:
    """Refresh/help are inert during startup and for non-panel content."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            switcher = app.query_one("#main", ContentSwitcher)
            nav = app.query_one("#nav", ListView)

            # A list item that is not a navigation entry must not switch panels.
            app.on_list_view_selected(ListView.Selected(nav, ListItem(id="plain"), 0))
            assert switcher.current == "home"

            app._starting = True
            try:
                app.action_refresh()
                app.action_help()
                assert not app.workers, "startup refresh must not reload panels"
                assert not isinstance(app.screen, HelpScreen)
            finally:
                app._starting = False

            # Content that is not a Panel is left alone by refresh.
            await switcher.mount(Static("plain", id="plain-content"))
            await pilot.pause()
            switcher.current = "plain-content"
            app.action_refresh()
            assert switcher.current == "plain-content"
            switcher.current = "home"
            await pilot.pause()

            app.action_help()
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)

    _run(scenario())


def test_home_panel_attention_files_render(demo_template: Project) -> None:
    """Unhealthy files appear in the Home attention list."""
    home = HomePanel(demo_template)
    home.summary = None
    home.recent_runs = []
    home.attention = {
        "failed_run_count": 0, "runs": [], "decisions": [],
        "files": [{"file_id": "FIL_7", "status": "MISSING",
                   "relative_path": "raw/reads/missing.fastq"}],
    }
    text = home._build_text().plain
    assert "file FIL_7  raw/reads/missing.fastq" in text
    assert "nothing needs attention" not in text


# ---------------------------------------------------------------------------
# Splash artwork
# ---------------------------------------------------------------------------


def test_lake_pixels_rejects_short_asset(monkeypatch) -> None:
    """A truncated asset raises instead of rendering a corrupt raster."""
    from operon.tui import splash

    splash.lake_pixels.cache_clear()
    monkeypatch.setattr(splash, "zlib", SimpleNamespace(decompress=lambda _data: b"short"))
    try:
        with pytest.raises(ValueError, match="Invalid splash pixel data"):
            splash.lake_pixels.__wrapped__()
    finally:
        monkeypatch.undo()
        splash.lake_pixels.cache_clear()
    assert len(splash.lake_pixels()) == 256 * 192 * 3


def test_lake_art_hide_image_survives_terminal_loss(monkeypatch) -> None:
    """Losing the driver or the terminal during shutdown never raises."""
    from textual.app import App

    from operon.tui.splash import LakeArt, SplashScreen

    monkeypatch.setenv("OPERON_SPLASH", "kitty")

    def disconnected(_data):
        raise OSError("terminal disconnected")

    async def scenario() -> None:
        app = App()
        async with app.run_test() as pilot:
            await app.push_screen(SplashScreen())
            await pilot.pause()
            art = app.screen.query_one(LakeArt)
            driver = app._driver

            art._uploaded = True
            app._driver = None
            try:
                art.hide_image()
            finally:
                app._driver = driver
            assert art._uploaded is False

            art._uploaded = True
            app._driver = SimpleNamespace(is_headless=False, write=disconnected)
            try:
                art.hide_image()
            finally:
                app._driver = driver
            assert art._uploaded is False

    asyncio.run(scenario())
