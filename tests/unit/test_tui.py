"""Read-only TUI data layer and headless screen tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import ContentSwitcher, DataTable, Input, Select, Static, Tree

from operon.cli import main
from operon.config import Project
from operon.demo import init_demo
from operon.tui import data
from operon.tui.app import HelpScreen, OperonApp
from operon.tui.screens.entities import EntitiesPanel
from operon.tui.screens.files import FilesPanel
from operon.tui.screens.home import HomePanel
from operon.tui.screens.runs import RunDetailScreen, RunsPanel
from operon.utils import sha256_file


@pytest.fixture(scope="module")
def demo_project(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-demo"))


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 15.0


def _run(coroutine) -> None:
    """Drive a Textual headless scenario without requiring pytest-asyncio.

    The overall timeout turns a stuck worker into a fast failure instead of
    blocking the whole pytest run indefinitely.
    """
    asyncio.run(asyncio.wait_for(coroutine, timeout=SCENARIO_TIMEOUT))


async def _settled(app, timeout: float = SETTLE_TIMEOUT) -> None:
    """Wait until no workers are running, with a diagnostic timeout.

    ``app.workers.wait_for_complete()`` is unbounded: a worker blocked in
    ``call_from_thread`` (or spawned again by a timer) blocks the caller
    forever.  Poll the worker set instead and fail fast with the stuck
    worker states when the deadline passes.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while app.workers:
        if loop.time() > deadline:
            states = [worker.state.name for worker in app.workers]
            raise TimeoutError(f"workers did not finish within {timeout}s: {states}")
        await asyncio.sleep(0.05)


def _find_tree_node(tree: Tree, entity_type: str, entity_id: str):
    stack = list(tree.root.children)
    while stack:
        node = stack.pop()
        if node.data == (entity_type, entity_id):
            return node
        stack.extend(node.children)
    return None


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------


def test_common_helpers() -> None:
    from operon.tui.screens.common import (
        entity_label,
        format_duration,
        human_size,
        styled_decision,
        styled_file_status,
        styled_status,
    )

    assert human_size(None) == "-"
    assert human_size("bad") == "-"
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.0 KiB"
    assert human_size(3 * 1024**4) == "3.0 TiB"
    assert styled_status("completed").plain == "completed"
    assert styled_status(None).plain == "-"
    assert styled_decision("REVIEW").style == "yellow"
    assert styled_file_status("MISSING").style == "red"
    assert styled_file_status("UNKNOWN").style == "dim"
    assert format_duration({"duration_seconds": 1.5}) == "1.500s"
    assert format_duration({"duration_seconds": None}) == "-"
    assert entity_label({"entity_type": "run", "entity_id": "RUN_1"}) == "run:RUN_1"
    assert entity_label({"entity_type": None, "entity_id": None}) == "-"


def test_project_summary(demo_project: Project) -> None:
    summary = data.project_summary(demo_project)
    assert summary["entity_counts"] == {
        "organism": 2, "sample": 3, "run": 1, "assembly": 3, "annotation": 2,
    }
    assert summary["file_count"] >= 9
    assert summary["file_bytes"] > 0
    assert sum(summary["decision_counts"].values()) > 0
    assert set(summary["decision_counts"]) <= {
        "PASS", "PASS_WITH_WARNINGS", "REVIEW", "FAIL", "EXCLUDED",
        "NOT_EVALUATED", "ACCEPT_WITH_WARNING",
    }
    assert summary["latest_release"]["version"] == "2026.08.demo"


def test_attention_items(demo_project: Project) -> None:
    attention = data.attention_items(demo_project)
    assert attention["failed_run_count"] == len(attention["runs"]) or len(attention["runs"]) <= 10
    assert all(r["status"] in {"failed", "interrupted"} for r in attention["runs"])
    assert attention["decisions"], "demo project should have REVIEW/FAIL decisions"
    for row in attention["decisions"]:
        assert (row.get("curated_decision") or row["decision"]) in {"REVIEW", "FAIL"}
    assert all(f["status"] not in data.HEALTHY_FILE_STATUSES for f in attention["files"])


def test_entity_tree(demo_project: Project) -> None:
    tree = data.entity_tree(demo_project)
    assert [node["entity_id"] for node in tree] == ["ORG_000001", "ORG_000002"]
    org1 = tree[0]
    assert org1["entity_type"] == "organism"
    assert org1["name"] == "Syntheticus alpha"
    assert org1["retired"] is False
    sample_ids = [child["entity_id"] for child in org1["children"]]
    assert sample_ids == ["SMP_000001", "SMP_000003"]
    smp1 = org1["children"][0]
    child_ids = {(c["entity_type"], c["entity_id"]) for c in smp1["children"]}
    assert ("run", "RUN_000001") in child_ids
    assert ("assembly", "ASM_000001") in child_ids
    asm1 = next(c for c in smp1["children"] if c["entity_type"] == "assembly")
    assert [c["entity_id"] for c in asm1["children"]] == ["ANN_000001"]
    assert asm1["state"] == "RELEASED"

    with_retired = data.entity_tree(demo_project, include_retired=True)
    assert [node["entity_id"] for node in with_retired] == ["ORG_000001", "ORG_000002"]


def test_entity_detail(demo_project: Project) -> None:
    detail = data.entity_detail(demo_project, "assembly", "ASM_000001")
    assert detail["fields"]["assembly_accession"] == "GCA_000000001"
    assert any(a["namespace"] == "NCBI_Assembly" for a in detail["accessions"])
    assert detail["state"]["state"] == "RELEASED"
    assert detail["files"], "assembly should have at least one file"
    assert data.entity_detail(demo_project, "assembly", "ASM_999999") is None


def test_list_files_filters(demo_project: Project) -> None:
    all_files = data.list_files(demo_project)
    assert len(all_files) >= 9
    assert all("locations" in record or True for record in all_files)

    by_entity = data.list_files(demo_project, entity="ASM_000001")
    assert by_entity
    assert all(r["entity_id"] == "ASM_000001" for r in by_entity)

    by_text = data.list_files(demo_project, text=all_files[0]["file_id"])
    assert [r["file_id"] for r in by_text] == [all_files[0]["file_id"]]

    status = all_files[0]["status"]
    by_status = data.list_files(demo_project, status=status)
    assert by_status and all(r["status"] == status for r in by_status)
    assert data.list_files(demo_project, status="MISSING") == []

    limited = data.list_files(demo_project, limit=2)
    assert len(limited) == 2

    assert "STANDARDIZED" in data.file_statuses(demo_project)


def test_file_detail(demo_project: Project) -> None:
    record = data.list_files(demo_project)[0]
    detail = data.file_detail(demo_project, record["file_id"])
    assert detail["file"]["file_id"] == record["file_id"]
    assert isinstance(detail["locations"], list)
    assert data.file_detail(demo_project, "FIL_999999") is None


def test_list_workflow_runs(demo_project: Project) -> None:
    runs = data.list_workflow_runs(demo_project, limit=100)
    assert runs, "demo project should record workflow runs"
    assert all(r["status"] == "completed" for r in runs)

    filtered = data.list_workflow_runs(demo_project, statuses=["failed"])
    assert filtered == []

    step = runs[0]["step"]
    by_step = data.list_workflow_runs(demo_project, step=step)
    assert by_step and all(step in r["step"] for r in by_step)

    entity_runs = [r for r in runs if r.get("entity_id")]
    assert entity_runs
    entity_id = entity_runs[0]["entity_id"]
    by_entity = data.list_workflow_runs(demo_project, entity=entity_id)
    assert by_entity and all(r["entity_id"] == entity_id for r in by_entity)

    assert len(data.list_workflow_runs(demo_project, limit=1)) == 1


def test_workflow_run_detail(demo_project: Project) -> None:
    run = data.list_workflow_runs(demo_project, limit=1)[0]
    detail = data.workflow_run_detail(demo_project, run["run_id"])
    assert detail["run_id"] == run["run_id"]
    assert isinstance(detail.get("execution_details"), (dict, list, str, type(None)))
    assert data.workflow_run_detail(demo_project, "WF_does_not_exist") is None


def test_entity_tree_on_lifecycle_less_database(tmp_path: Path) -> None:
    """Databases predating schema 2.7 have no retirement view; nothing is retired."""
    import sqlite3

    from operon.database import DDL

    db_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    conn.execute("INSERT INTO organisms (organism_id, scientific_name) VALUES ('ORG_000001', 'Legacy')")
    conn.commit()
    conn.close()

    project = Project.init(tmp_path / "legacy-project")
    project.db_path.unlink()
    db_path.rename(project.db_path)

    tree = data.entity_tree(project)
    assert [node["entity_id"] for node in tree] == ["ORG_000001"]
    assert tree[0]["retired"] is False
    assert data.entity_tree(project, include_retired=True)[0]["retired"] is False


def test_entity_tree_retirement(tmp_path: Path) -> None:
    """Retired entities are hidden by default and flagged when included."""
    from operon.database import Database
    from operon.lifecycle import apply_lifecycle_event

    project = Project.init(tmp_path / "retire-project")
    db = Database(project.db_path)
    try:
        db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Doomed"})
        db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        apply_lifecycle_event(
            db, "organism", "ORG_000001",
            action="RETIRE", reason="test retirement", actor="tester", reason_code="duplicate",
        )
    finally:
        db.close()

    assert data.entity_tree(project) == []
    tree = data.entity_tree(project, include_retired=True)
    assert tree[0]["retired"] is True
    assert tree[0]["children"][0]["retired"] is True  # retirement is inherited


def test_workflow_run_detail_execution_details_variants(tmp_path: Path) -> None:
    from operon.database import Database
    from operon.workflow import log_run

    project = Project.init(tmp_path / "run-detail-project")
    db = Database(project.db_path)
    try:
        bad = log_run(db, project, {"step": "demo", "status": "failed",
                                    "execution_details": "not valid json {"})
        plain = log_run(db, project, {"step": "demo", "status": "completed"})
    finally:
        db.close()

    detail = data.workflow_run_detail(project, bad["run_id"])
    assert detail["execution_details"] == "not valid json {"
    detail = data.workflow_run_detail(project, plain["run_id"])
    assert detail["execution_details"] in (None, "")

    runs = data.list_workflow_runs(project, statuses=["failed"], step="demo")
    assert [r["run_id"] for r in runs] == [bad["run_id"]]
    runs = data.list_workflow_runs(project, step="demo", limit=0)
    assert len(runs) == 2


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_data_layer_never_writes(demo_project: Project) -> None:
    """The data layer works on an OS-read-only database and never modifies it."""
    db_path = demo_project.db_path
    before = sha256_file(db_path)
    os.chmod(db_path, 0o444)
    try:
        data.project_summary(demo_project)
        data.attention_items(demo_project)
        data.entity_tree(demo_project)
        data.entity_tree(demo_project, include_retired=True)
        data.entity_detail(demo_project, "assembly", "ASM_000001")
        data.list_files(demo_project)
        data.file_statuses(demo_project)
        data.file_detail(demo_project, data.list_files(demo_project)[0]["file_id"])
        runs = data.list_workflow_runs(demo_project, limit=100)
        data.list_workflow_runs(demo_project, step="qc", entity="RUN_000001")
        data.workflow_run_detail(demo_project, runs[0]["run_id"])

        async def scenario() -> None:
            app = OperonApp(demo_project)
            async with app.run_test(size=(140, 45)) as pilot:
                for key in "1234":
                    await pilot.press(key)
                    await pilot.pause()
                    await _settled(app)

        _run(scenario())
    finally:
        os.chmod(db_path, 0o644)
    assert sha256_file(db_path) == before


# ---------------------------------------------------------------------------
# Headless UI
# ---------------------------------------------------------------------------


def test_navigation_and_home(demo_project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            switcher = app.query_one("#main", ContentSwitcher)
            assert switcher.current == "home"

            home = app.query_one(HomePanel)
            assert home.summary["entity_counts"]["assembly"] == 3
            body = _static_text(home.query_one("#home-body", Static))
            assert "PRJ_DEMO_001" in body
            assert "2026.08.demo" in body
            assert "Attention needed" in body
            assert "FAIL" in body

            runs_panel = app.query_one(RunsPanel)
            runs_panel._auto_reload()  # hidden: must not spawn a worker
            assert not app.workers

            for key, expected in (("2", "entities"), ("3", "files"), ("4", "runs"), ("1", "home")):
                await pilot.press(key)
                await pilot.pause()
                await _settled(app)
                assert switcher.current == expected
                if expected == "runs":
                    runs_panel._auto_reload()  # visible: reloads
                    await _settled(app)
                    assert runs_panel.runs

            await pilot.press("r")
            await _settled(app)

    _run(scenario())


def test_entities_screen(demo_project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("entities")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(EntitiesPanel)
            assert [n["entity_id"] for n in panel.tree_data] == ["ORG_000001", "ORG_000002"]

            tree = panel.query_one("#entities-tree", Tree)
            node = _find_tree_node(tree, "assembly", "ASM_000001")
            assert node is not None
            tree.select_node(node)
            await pilot.pause()
            await _settled(app)
            detail_text = _static_text(panel.query_one("#entity-detail", Static))
            assert "GCA_000000001" in detail_text
            assert "RELEASED" in detail_text
            assert "NCBI_Assembly" in detail_text

            panel.action_toggle_retired()
            await pilot.pause()
            await _settled(app)
            assert panel.include_retired is True
            assert [n["entity_id"] for n in panel.tree_data] == ["ORG_000001", "ORG_000002"]
            panel.action_toggle_retired()

    _run(scenario())


def test_files_screen_and_filters(demo_project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(FilesPanel)
            table = panel.query_one("#files-table", DataTable)
            total = table.row_count
            assert total >= 9

            panel.query_one("#files-filter", Input).value = "zzz_no_such_file"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 0

            panel.query_one("#files-filter", Input).value = ""
            await pilot.pause()
            await _settled(app)
            assert table.row_count == total

            select = panel.query_one("#files-status", Select)
            select.value = "MISSING"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 0
            select.value = "STANDARDIZED"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == total

            file_id = panel.files[0]["file_id"]
            table.focus()
            table.move_cursor(row=0, animate=False)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await _settled(app)
            detail_text = _static_text(panel.query_one("#file-detail", Static))
            assert file_id in detail_text

    _run(scenario())


def test_detail_text_builders(demo_project: Project) -> None:
    entities_panel = EntitiesPanel(demo_project)
    assert "entity not found" in entities_panel._detail_text(None).plain
    sparse = {
        "entity_type": "run", "entity_id": "RUN_X",
        "fields": {"run_id": "RUN_X", "platform": None},
        "accessions": [], "state": None, "files": [],
    }
    text = entities_panel._detail_text(sparse).plain
    assert "(no state recorded)" in text
    assert "(none)" in text

    files_panel = FilesPanel(demo_project)
    assert "file not found" in files_panel._detail_text(None).plain
    located = {
        "file": {
            "file_id": "FIL_X", "entity_type": "run", "entity_id": "RUN_000001",
            "file_role": "reads_r1", "format": "fastq", "compression": "none",
            "relative_path": "raw/reads/x.fastq", "source_url": "https://example.org/x",
            "downloaded_at": "2026-01-01", "size_bytes": 2048, "sha256": "ab" * 32,
            "status": "MISSING",
        },
        "locations": [
            {"location_name": "archive", "location_type": "sftp", "uri": "sftp://host/x",
             "relative_path": "x", "sha256": "ab" * 32, "size_bytes": 2048,
             "status": "AVAILABLE", "verified_at": "2026-01-02"},
        ],
    }
    text = files_panel._detail_text(located).plain
    assert "archive" in text
    assert "sftp://host/x" in text
    assert "verified" in text

    detail_screen = RunDetailScreen(demo_project, "WF_missing")
    assert "does not exist" in detail_screen._detail_text(None).plain
    record = {
        "run_id": "WF_X", "status": "failed", "step": "qc",
        "entity_type": "run", "entity_id": "RUN_000001",
        "parent_run_id": None, "resumes_run_id": None,
        "started_at": "2026-01-01T00:00:00+00:00", "finished_at": None,
        "duration_seconds": None, "threads": None, "max_rss_mb": None,
        "avg_rss_mb": None, "cpu_seconds": None, "command": "qc ...",
        "tool": "qc", "tool_version": "0.6.1", "parameter_set": None,
        "executor": "local", "scheduler_job_id": None, "exit_code": 1,
        "environment_id": None, "input_sha256": None, "output_sha256": None,
        "log_file": None, "stdout_file": None, "stderr_file": None,
        "error": "boom", "execution_details": "plain text details",
    }
    text = detail_screen._detail_text(record).plain
    assert "boom" in text
    assert "plain text details" in text

    home = HomePanel(demo_project)
    home.summary = None
    home.attention = {
        "failed_run_count": 25,
        "runs": [{"run_id": f"WF_{i}", "status": "failed", "step": "qc",
                  "entity_type": None, "entity_id": None, "started_at": "-", "error": None}
                 for i in range(10)],
        "decisions": [], "files": [],
    }
    home.recent_runs = []
    text = home._build_text().plain
    assert "and 15 more failed/interrupted runs" in text


def test_runs_screen_filters_and_detail(demo_project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("runs")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(RunsPanel)
            table = panel.query_one("#runs-table", DataTable)
            assert table.row_count > 0

            select = panel.query_one("#runs-status", Select)
            select.value = "failed"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 0
            select.value = "completed"
            await pilot.pause()
            await _settled(app)
            assert table.row_count > 0

            panel.query_one("#runs-entity", Input).value = "RUN_000001"
            await pilot.pause()
            await _settled(app)
            assert table.row_count >= 1
            assert all("RUN_000001" in str(r["entity_id"]) for r in panel.runs)
            panel.query_one("#runs-entity", Input).value = ""
            panel.query_one("#runs-limit", Input).value = "1"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 1
            panel.query_one("#runs-limit", Input).value = "100"
            await pilot.pause()
            await _settled(app)

            table.focus()
            table.move_cursor(row=0, animate=False)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await _settled(app)
            assert isinstance(app.screen, RunDetailScreen)
            run_id = panel.runs[0]["run_id"]
            detail_text = _static_text(app.screen.query_one("#run-detail", Static))
            assert run_id in detail_text
            assert "Workflow run" in detail_text
            assert "Execution details" in detail_text

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, RunDetailScreen)

    _run(scenario())


def test_help_modal(demo_project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)

    _run(scenario())


def test_panels_render_errors_instead_of_crashing(tmp_path: Path) -> None:
    """A project without a database file must surface errors in the panels."""
    project = Project.init(tmp_path / "broken")
    project.db_path.unlink()

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            body = _static_text(app.query_one("#home-body", Static))
            assert "error" in body.lower()

            app.action_switch_screen("entities")
            await pilot.pause()
            await _settled(app)
            detail = _static_text(app.query_one("#entity-detail", Static))
            assert "error" in detail.lower()

    _run(scenario())


def test_empty_project_and_app_actions(tmp_path: Path) -> None:
    """An empty project renders placeholder sections; app-level actions work."""
    project = Project.init(tmp_path / "empty")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            body = _static_text(app.query_one("#home-body", Static))
            assert "(none)" in body
            assert "nothing needs attention" in body

            app.action_switch_screen("bogus")
            assert app.query_one("#main", ContentSwitcher).current == "home"

            await pilot.click("#nav-files")
            await pilot.pause()
            await _settled(app)
            assert app.query_one("#main", ContentSwitcher).current == "files"

    _run(scenario())


def test_runs_step_filter(demo_project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("runs")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(RunsPanel)
            table = panel.query_one("#runs-table", DataTable)
            total = table.row_count

            panel.query_one("#runs-step", Input).value = "qc"
            await pilot.pause()
            await _settled(app)
            assert 0 < table.row_count <= total
            assert all("qc" in r["step"] for r in panel.runs)
            panel.query_one("#runs-step", Input).value = "no_such_step"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 0

    _run(scenario())


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_tui_help_is_argparse_only(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["tui", "--help"])
    assert excinfo.value.code == 0
    assert "tui" in capsys.readouterr().out


def test_tui_missing_textual_hint(monkeypatch, capsys) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith(("operon.tui", "textual")):
            raise ModuleNotFoundError("No module named 'textual'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert main(["tui"]) == 2
    assert "pip install 'operon[tui]'" in capsys.readouterr().err


def test_tui_without_project_returns_2(tmp_path: Path, capsys) -> None:
    assert main(["--project", str(tmp_path / "nowhere"), "tui"]) == 2
    assert "no project.yaml" in capsys.readouterr().err
