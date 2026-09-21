"""Read-only TUI data layer and headless screen tests."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.css.query import NoMatches
from textual.pilot import OutOfBounds
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    DataTable,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    Tree,
)
from textual.widgets.data_table import RowKey

from operon.cli import main
from operon.config import Project
from operon.demo import init_demo
from operon.errors import ValidationError
from operon.tui import actions, data
from operon.tui.app import HelpScreen, OperonApp
from operon.tui.screens.entities import EntitiesPanel
from operon.tui.screens.files import FilesPanel
from operon.tui.screens.home import HomePanel
from operon.tui.screens.runs import (
    ALL_STATUSES,
    AnalysisJobsModal,
    RunDetailScreen,
    RunsPanel,
)
from operon.utils import sha256_file
from tests.helpers import copy_project_tree


@pytest.fixture(scope="module")
def demo_project(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-demo"))


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 30.0


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
    # The splash screen blocks key bindings until the startup worker finishes;
    # that worker is scheduled via call_after_refresh, so the worker set can
    # be momentarily empty before it starts — gate on _starting as well.
    while app.workers or getattr(app, "_starting", False):
        if loop.time() > deadline:
            states = [worker.state.name for worker in app.workers]
            raise TimeoutError(f"workers did not finish within {timeout}s: {states}")
        await asyncio.sleep(0.05)


async def _click(pilot, selector: str) -> None:
    """Activate a widget, tolerating a rebuilt layout and a lingering press effect.

    ``Pilot.click`` returns False when the target is clipped or obscured and
    *raises* ``OutOfBounds`` when the target's centre is still outside the screen
    region, which is what a deferred editor rebuild produces (ODR-0023).  A
    ``Button`` also keeps its ``-active`` press effect for about 0.2 s and
    Textual drops a ``Button.Pressed`` raised inside that window, so a rapid
    second click reported ``landed=True`` and did nothing (ODR-0024): wait for
    the effect to clear first.  An enabled button is then pressed directly when
    the positional click cannot land — the same activation a landed click
    produces — and anything else is retried until it lands or the budget runs
    out, so a rebuilt layout fails loudly instead of silently.
    """
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



async def _wait_until(
    predicate: Callable[[], bool],
    description: str,
    timeout: float = SETTLE_TIMEOUT,
) -> None:
    """Wait for an observable UI result after a thread worker completes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not {description} within {timeout}s")
        await asyncio.sleep(0.05)


def _detail_text(app) -> str:
    """Text of the open screen's ``#run-detail``, or "" while it is still composing."""
    try:
        return _static_text(app.screen.query_one("#run-detail", Static))
    except NoMatches:
        return ""


async def _await_detail_text(app, needle: str) -> str:
    """Wait for the run-detail screen to carry *needle*, and return its text.

    ``_settled`` only says that no worker is running *at that instant*: the
    detail screen starts its read from ``on_mount``, which needs a message-loop
    turn of its own, so a read straight afterwards can land on the ``loading…``
    placeholder — the window a slow runner stops on (ODR-0029).
    """
    await _wait_until(lambda: needle in _detail_text(app), f"run detail to show {needle!r}")
    return _detail_text(app)


async def _tick_until(
    predicate: Callable[[], bool],
    clock: list[float],
    *,
    step: float = 2.0,
    description: str,
    timeout: float = SETTLE_TIMEOUT,
) -> None:
    """Wait for *predicate*, advancing a fake ``monotonic`` clock as it waits.

    The startup worker captures its start time on its first turn, so a clock
    that is set once and then frozen can never show elapsed time — the wait
    would run forever against a deadline that is never reached.  Stepping the
    clock keeps the difference growing no matter when the first turn happened.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not {description} within {timeout}s")
        clock[0] += step
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


def test_attention_items(demo_project: Project, tmp_path: Path) -> None:
    # The demo run history is clean, but its decisions still need review.
    demo_attention = data.attention_items(demo_project)
    assert demo_attention["failed_run_count"] == 0
    assert demo_attention["runs"] == []
    assert demo_attention["decisions"], "demo project should have REVIEW/FAIL decisions"
    for row in demo_attention["decisions"]:
        assert (row.get("curated_decision") or row["decision"]) in {"REVIEW", "FAIL"}

    # A project with real failures surfaces them: the count is the full total,
    # while the runs page honours the limit; healthy files stay out.
    from operon.database import Database
    from operon.workflow import log_run

    project = Project.init(tmp_path / "attention-project")
    db = Database(project.db_path)
    try:
        for file_id, status in (("FIL_000001", "CORRUPT"), ("FIL_000002", "CHECKSUM_VERIFIED")):
            db.insert_row("files", {
                "file_id": file_id, "entity_type": "assembly", "entity_id": "ASM_000001",
                "file_role": "genome_fasta", "relative_path": f"raw/{file_id}.fa",
                "sha256": "a" * 64, "size_bytes": 1, "status": status, "format": "fasta",
                "compression": "none",
            })
        log_run(db, project, {"step": "qc", "status": "failed", "error": "injected"})
        log_run(db, project, {"step": "qc", "status": "interrupted"})
        log_run(db, project, {"step": "qc", "status": "completed"})
    finally:
        db.close()

    attention = data.attention_items(project)
    assert attention["failed_run_count"] == 2
    assert len(attention["runs"]) == 2
    assert all(r["status"] in {"failed", "interrupted"} for r in attention["runs"])
    assert [f["file_id"] for f in attention["files"]] == ["FIL_000001"]
    assert all(f["status"] not in data.HEALTHY_FILE_STATUSES for f in attention["files"])

    page = data.attention_items(project, limit=1)
    assert page["failed_run_count"] == 2, "the count is the total, not the page"
    assert len(page["runs"]) == 1, "the page honours the limit"


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
    assert detail["metrics"]["qc"], "assembly should have QC metrics"
    assert data.entity_detail(demo_project, "assembly", "ASM_999999") is None


def test_entity_metrics(demo_project: Project) -> None:
    metrics = data.entity_metrics(demo_project, "assembly", "ASM_000001")
    qc = metrics["qc"]
    assert qc, "demo assemblies should have built-in QC metrics"
    names = [row["metric_name"] for row in qc]
    assert len(names) == len(set(names)), "collapsed to one row per metric name"
    assert {"contig_n50", "parseable"} <= set(names)
    assert [row["qc_stage"] for row in qc] == sorted(row["qc_stage"] for row in qc)
    n50 = next(row for row in qc if row["metric_name"] == "contig_n50")
    assert n50["metric_unit"] == "bp"
    assert n50["tool"] == "operon.builtin"
    assert metrics["analysis"] == []

    assert data.entity_metrics(demo_project, "organism", "ORG_000001") == {
        "qc": [], "analysis": [],
    }


def test_list_files_filters(demo_project: Project) -> None:
    all_files = data.list_files(demo_project)
    assert len(all_files) >= 9
    # Every row carries the residency aggregate; the demo has no mirrors, so
    # the aggregate is NULL rather than a missing or empty string.
    assert all(record["locations"] is None for record in all_files)

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


def test_workflow_run_detail(demo_project: Project, tmp_path: Path) -> None:
    run = data.list_workflow_runs(demo_project, limit=1)[0]
    detail = data.workflow_run_detail(demo_project, run["run_id"])
    assert detail["run_id"] == run["run_id"]
    # Stored execution_details JSON is decoded into a mapping.
    assert isinstance(detail["execution_details"], dict)
    assert detail["execution_details"]["input"]["kind"] == "primary"
    assert data.workflow_run_detail(demo_project, "WF_does_not_exist") is None

    # Unparseable stored details degrade to the raw string instead of raising.
    from operon.database import Database
    from operon.workflow import log_run

    project = Project.init(tmp_path / "corrupt-details-project")
    db = Database(project.db_path)
    try:
        broken = log_run(db, project, {
            "step": "demo", "status": "completed", "execution_details": "not json",
        })
    finally:
        db.close()
    assert data.workflow_run_detail(project, broken["run_id"])["execution_details"] == "not json"


def test_workflow_run_detail_environment_summary(tmp_path: Path) -> None:
    from operon.database import Database
    from operon.workflow import log_run

    project = Project.init(tmp_path / "env-detail-project")
    db = Database(project.db_path)
    try:
        with db.transaction():
            environment_id = db.record_environment(
                {"system": {"os": "Linux"}, "capture_status": "partial"})
        run = log_run(db, project, {"step": "demo", "status": "completed",
                                    "environment_id": environment_id})
        stale = log_run(db, project, {"step": "demo", "status": "completed",
                                      "environment_id": "missing"})
    finally:
        db.close()

    detail = data.workflow_run_detail(project, run["run_id"])
    assert detail["environment_summary"] == "Linux; capture: partial"
    detail = data.workflow_run_detail(project, stale["run_id"])
    assert detail["environment_summary"] is None


def _seed_analysis_jobs(demo_project: Project, tmp_path: Path) -> tuple[Project, dict]:
    """A private copy of the demo project with two seeded analysis jobs.

    One completed job linked to a workflow run (so the scheduler job id is
    joinable) and one interrupted task with no run row at all — exactly the
    shape a cancelled job array leaves behind.
    """
    from operon.database import Database
    from operon.workflow import log_run

    target = copy_project_tree(demo_project.root, tmp_path / "analysis-jobs-project")
    project = Project.find(target)
    db = Database(project.db_path)
    try:
        file_id = db.query("SELECT file_id FROM files ORDER BY file_id LIMIT 1")[0]["file_id"]
        run = log_run(db, project, {
            "step": "analysis:seed_tool", "status": "completed",
            "entity_type": "assembly", "entity_id": "ASM_000001",
            "executor": "slurm", "scheduler_job_id": "7000_1",
        })
        base = {
            "analysis_name": "seed_tool",
            "entity_type": "assembly",
            "entity_id": "ASM_000001",
            "file_id": file_id,
            "tool": "seedtool",
            "tool_version": "1.0",
            "parameter_set": "seed_tool:1.0",
            "parameter_sha256": "0" * 64,
            "input_sha256": "1" * 64,
            "database_identity": "none",
            "status": "completed",
            "started_at": "2026-09-18T10:00:00+08:00",
            "finished_at": "2026-09-18T10:01:00+08:00",
            "workflow_run_id": run["run_id"],
        }
        db.insert_row("analysis_jobs", base)
        db.insert_row("analysis_jobs", {
            **base,
            "status": "interrupted",
            "started_at": "2026-09-18T10:05:00+08:00",
            "finished_at": None,
            "workflow_run_id": None,
            "error": "interrupted by SIGINT\nsecond line of the error",
        })
    finally:
        db.close()
    return project, {"run_id": run["run_id"], "file_id": file_id}


def test_list_analysis_jobs_includes_interrupted_tasks(demo_project: Project,
                                                       tmp_path: Path) -> None:
    project, seeded = _seed_analysis_jobs(demo_project, tmp_path)

    jobs = data.list_analysis_jobs(project)
    assert [job["status"] for job in jobs] == ["interrupted", "completed"]  # newest first
    interrupted, completed = jobs
    # The interrupted task has no run row: it stays visible, but the joined
    # scheduler columns are empty instead of hiding the row.
    assert interrupted["workflow_run_id"] is None
    assert interrupted["scheduler_job_id"] is None
    assert interrupted["executor"] is None
    assert "SIGINT" in interrupted["error"]
    assert completed["workflow_run_id"] == seeded["run_id"]
    assert completed["scheduler_job_id"] == "7000_1"
    assert completed["executor"] == "slurm"

    by_status = data.list_analysis_jobs(project, statuses=["interrupted"])
    assert [job["job_id"] for job in by_status] == [interrupted["job_id"]]
    assert data.list_analysis_jobs(project, analysis="seed") != []
    assert data.list_analysis_jobs(project, analysis="no_such_analysis") == []
    assert len(data.list_analysis_jobs(project, limit=1)) == 1
    assert data.list_analysis_jobs(project, limit=0)[0]["job_id"] == interrupted["job_id"]


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


def test_data_layer_never_writes(demo_project: Project, tmp_path: Path) -> None:
    """The data layer works on an OS-read-only database and never modifies it.

    The read-only pass runs on a private copy of the demo project.  SQLite
    hands the WAL side files of a read-only database that same read-only mode,
    and the shared module fixture must not carry it into every later test that
    copies it (ODR-0021).  The copy keeps the assertion meaningful: the
    database it starts from is byte-identical to the demo's.
    """
    project = Project.find(
        copy_project_tree(demo_project.root, tmp_path / "read-only-project"))
    db_path = project.db_path
    before = sha256_file(db_path)
    os.chmod(db_path, 0o444)
    try:
        data.project_summary(project)
        data.attention_items(project)
        data.entity_tree(project)
        data.entity_tree(project, include_retired=True)
        data.entity_detail(project, "assembly", "ASM_000001")
        data.entity_metrics(project, "assembly", "ASM_000001")
        data.list_files(project)
        data.file_statuses(project)
        data.file_detail(project, data.list_files(project)[0]["file_id"])
        runs = data.list_workflow_runs(project, limit=100)
        data.list_workflow_runs(project, step="qc", entity="RUN_000001")
        data.workflow_run_detail(project, runs[0]["run_id"])
        data.list_analysis_jobs(project)
        # Phase-3 read paths (pickers, publish, coverage).
        data.list_organisms_for_picker(project)
        data.list_samples_for_picker(project, "ORG_000001")
        data.list_assemblies_for_picker(project, "SMP_000001")
        data.list_annotations_for_picker(project, "ASM_000001")
        data.list_releases(project)
        data.release_preview(project, "assembly_production_v1")
        data.export_preview(project, entity_type="assembly")
        data.list_taxonomy_snapshots(project)
        data.list_reference_sets(project)
        data.list_coverage_reports(project)

        async def scenario() -> None:
            app = OperonApp(project)
            async with app.run_test(size=(140, 45)) as pilot:
                for key in "12345678":
                    await pilot.press(key)
                    await pilot.pause()
                    await _settled(app)

        _run(scenario())
    finally:
        os.chmod(db_path, 0o644)
    assert sha256_file(db_path) == before


@pytest.mark.bug("ODR-0021")
def test_copied_project_stays_writable_after_a_read_only_session(
    demo_project: Project, tmp_path: Path
) -> None:
    """A copy of a project that was read read-only still opens for writing.

    A read-only connection to a read-only database makes SQLite create the WAL
    side files with that read-only mode, and ``shutil.copytree`` preserves it:
    the copy's database could not be written, which is how the defect reached
    CI as an unrelated test failing with "attempt to write a readonly
    database".  ``copy_project_tree`` drops the shared-memory file and restores
    write permission.
    """
    from operon.database import Database
    from operon.workflow import log_run

    source = Project.find(
        copy_project_tree(demo_project.root, tmp_path / "read-only-source"))
    original_mode = source.db_path.stat().st_mode
    os.chmod(source.db_path, 0o444)
    try:
        reader = Database(source.db_path, read_only=True)
        try:
            reader.query("SELECT COUNT(*) AS n FROM files")
        finally:
            reader.close()
    finally:
        os.chmod(source.db_path, original_mode)

    shared_memory = Path(f"{source.db_path}-shm")
    assert shared_memory.exists(), (
        "the read-only session no longer leaves a -shm file behind; the "
        "regression test needs a different precondition"
    )
    assert not shared_memory.stat().st_mode & 0o200, (
        "the read-only session no longer leaves a read-only -shm file behind; "
        "the regression test needs a different precondition"
    )

    target = copy_project_tree(source.root, tmp_path / "copy")
    copied = Project.find(target)
    db = Database(copied.db_path)
    try:
        log_run(db, copied, {"step": "copy-probe", "status": "completed"})
    finally:
        db.close()


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
            assert _static_text(app.query_one("#nav-runs Label", Label)) == "4  Tasks"

            home = app.query_one(HomePanel)
            assert home.summary["entity_counts"]["assembly"] == 3
            body = _static_text(home.query_one("#home-body", Static))
            assert "PRJ_DEMO_001" in body
            assert "2026.08.demo" in body
            assert "Attention needed" in body
            assert "FAIL" in body

            runs_panel = app.query_one(RunsPanel)
            for key, expected in (("2", "entities"), ("3", "files"), ("4", "runs"), ("1", "home")):
                await pilot.press(key)
                await pilot.pause()
                await _settled(app)
                assert switcher.current == expected
                if expected == "runs":
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
            assert panel.include_retired is True
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
            assert "QC metrics" in detail_text
            assert "contig_n50 = 5000.0 bp" in detail_text
            assert "operon.builtin@" in detail_text
            assert "Analysis metrics" in detail_text

    _run(scenario())


def test_entities_retired_shown_dimmed_by_default(tmp_path: Path) -> None:
    """Retired entities are visible-but-dimmed by default; `t` hides them."""
    from operon.database import Database
    from operon.lifecycle import apply_lifecycle_event

    project = Project.init(tmp_path / "retire-ui-project")
    db = Database(project.db_path)
    try:
        db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Doomed"})
        db.insert_row("organisms", {"organism_id": "ORG_000002", "scientific_name": "Thriving"})
        apply_lifecycle_event(
            db, "organism", "ORG_000001",
            action="RETIRE", reason="test retirement", actor="tester", reason_code="duplicate",
        )
    finally:
        db.close()

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("entities")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(EntitiesPanel)
            assert panel.include_retired is True
            tree = panel.query_one("#entities-tree", Tree)
            node = _find_tree_node(tree, "organism", "ORG_000001")
            assert node is not None
            assert "(retired)" in node.label.plain
            assert any("dim" in str(span.style) for span in node.label.spans)

            panel.action_toggle_retired()
            await pilot.pause()
            await _settled(app)
            assert panel.include_retired is False
            assert _find_tree_node(tree, "organism", "ORG_000001") is None
            assert _find_tree_node(tree, "organism", "ORG_000002") is not None

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


@pytest.mark.parametrize("rank", ["subsp.", "ssp.", "var.", "subvar.", "f.", "subf."])
def test_scientific_name_rank_style(rank: str) -> None:
    from rich.console import Console

    from operon.tui.screens.common import styled_scientific_name

    name = f"Syntheticus alpha\t{rank}  beta"
    text = styled_scientific_name(name)
    assert text.plain == name
    console = Console()
    start = name.index(rank)
    for offset in range(len(name)):
        assert text.get_style_at_offset(console, offset).italic is (
            not start <= offset < start + len(rank)
        )


@pytest.mark.parametrize("name", [
    "Syntheticus alpha", "", "Syntheticus subvar.alpha", "Syntheticus xvar. beta",
    "Syntheticus [var.] beta",
])
def test_scientific_name_preserves_other_text(name: str) -> None:
    from rich.console import Console

    from operon.tui.screens.common import styled_scientific_name

    text = styled_scientific_name(name)
    assert text.plain == name
    assert all(text.get_style_at_offset(Console(), i).italic for i in range(len(name)))


def test_organism_names_render_italic(demo_project: Project) -> None:
    """Latin scientific names are italicized in the tree and the detail panel."""
    from rich.console import Console

    from operon.tui.screens.entities import _node_label

    name = "Syntheticus alpha subsp. beta var. gamma"
    organism = _node_label({"entity_type": "organism", "entity_id": "ORG_1",
                            "name": name})
    assert any("italic" in str(span.style) for span in organism.spans)
    sample = _node_label({"entity_type": "sample", "entity_id": "SMP_1", "name": "isolate A"})
    assert not any("italic" in str(span.style) for span in sample.spans)

    panel = EntitiesPanel(demo_project)
    detail = {
        "entity_type": "organism", "entity_id": "ORG_1",
        "fields": {"organism_id": "ORG_1", "scientific_name": name},
        "accessions": [], "state": None, "files": [], "metrics": {},
    }
    text = panel._detail_text(detail)
    name_start = text.plain.index("Syntheticus alpha")
    assert any(
        "italic" in str(span.style) and span.start <= name_start < span.end
        for span in text.spans
    )
    for rendered in (organism, text):
        for component in ("Syntheticus", "alpha", "beta", "gamma", "subsp.", "var."):
            start = rendered.plain.index(component)
            for offset in range(start, start + len(component)):
                assert rendered.get_style_at_offset(Console(), offset).italic is (
                    component not in ("subsp.", "var.")
                )


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

    record_with_env = dict(record, environment_id="env_x",
                           environment_summary="Ubuntu 22.04; 1024 kB")
    text = detail_screen._detail_text(record_with_env).plain
    assert "env_x" in text
    assert "Ubuntu 22.04; 1024 kB" in text

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
            detail_text = await _await_detail_text(app, str(run_id))
            assert run_id in detail_text
            assert "Workflow run" in detail_text
            assert "Execution details" in detail_text

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, RunDetailScreen)

    _run(scenario())


def test_analysis_jobs_modal_browses_interrupted_tasks(demo_project: Project,
                                                       tmp_path: Path) -> None:
    """The Tasks screen's jobs browser shows rows that have no workflow run."""
    project, _seeded = _seed_analysis_jobs(demo_project, tmp_path)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            app.action_switch_screen("runs")
            await pilot.pause()
            await _settled(app)
            await _click(pilot, "#runs-jobs")
            await pilot.pause()
            await _settled(app)
            modal = app.screen
            assert isinstance(modal, AnalysisJobsModal)
            table = modal.query_one("#jobs-table", DataTable)
            assert table.row_count == 2
            assert {job["status"] for job in modal.jobs} == {"interrupted", "completed"}

            # Selecting the interrupted task shows its full error text; it has
            # no run row, so the scheduler columns stay empty.
            table.focus()
            table.move_cursor(row=0, animate=False)
            await pilot.pause()
            detail = _static_text(modal.query_one("#jobs-detail", Static))
            assert "interrupted" in detail
            assert "second line of the error" in detail
            assert "scheduler_job_id" in detail

            # The completed task shows the joined scheduler job id and no error.
            table.move_cursor(row=1, animate=False)
            await pilot.pause()
            detail = _static_text(modal.query_one("#jobs-detail", Static))
            assert "7000_1" in detail
            assert "slurm" in detail
            assert "error" in detail and detail.rstrip().endswith("-")
            table.move_cursor(row=0, animate=False)
            await pilot.pause()

            # Highlight events from other tables and out-of-range cursor rows
            # are ignored instead of rewriting the detail pane.
            modal.on_data_table_row_highlighted(DataTable.RowHighlighted(
                DataTable(id="other-table"), 0, RowKey("other")))
            modal.on_data_table_row_highlighted(DataTable.RowHighlighted(
                table, 99, RowKey("out-of-range")))
            await pilot.pause()
            assert "second line of the error" in _static_text(
                modal.query_one("#jobs-detail", Static))

            # Filters narrow the listing down to the task without a run row.
            modal.query_one("#jobs-status", Select).value = "interrupted"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 1
            assert modal.jobs[0]["status"] == "interrupted"
            modal.query_one("#jobs-analysis", Input).value = "SEED"  # case-insensitive
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 1
            assert modal.jobs[0]["analysis_name"] == "seed_tool"
            modal.query_one("#jobs-analysis", Input).value = "no_such_analysis"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 0
            assert "no matching analysis jobs" in _static_text(
                modal.query_one("#jobs-detail", Static))

            # Resetting the filters restores the full listing; a non-positive
            # limit falls back to the default instead of hiding everything.
            modal.query_one("#jobs-analysis", Input).value = ""
            modal.query_one("#jobs-status", Select).value = ALL_STATUSES
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 2
            modal.query_one("#jobs-limit", Input).value = "0"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 2
            await _click(pilot, "#cancel")
            await pilot.pause()
            assert not isinstance(app.screen, AnalysisJobsModal)

    _run(scenario())


def test_analysis_jobs_modal_reports_load_failures(demo_project: Project,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("jobs query exploded")

    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            monkeypatch.setattr(data, "list_analysis_jobs", explode)
            modal = AnalysisJobsModal(demo_project)
            app.push_screen(modal)
            await pilot.pause()
            await _settled(app)
            assert "jobs query exploded" in _static_text(
                modal.query_one("#jobs-detail", Static))
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())


def test_help_modal(demo_project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.pause()
            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            body = _static_text(app.screen.query_one("#help-body", Static))
            assert "Tasks — workflow-run monitor" in body
            assert "show/hide retired entities" in body
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

            # A click on the ListItem lands on its child Label, so the strict
            # _click helper does not apply here.
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


def test_runs_table_view_survives_reload(tmp_path: Path) -> None:
    """Reloads keep the cursor and scroll offset."""
    from operon.database import Database
    from operon.workflow import log_run

    project = Project.init(tmp_path / "runs-scroll-project")
    db = Database(project.db_path)
    try:
        for index in range(80):
            log_run(db, project, {
                "step": "qc", "status": "completed",
                "entity_type": "run", "entity_id": f"RUN_{index:06d}",
            })
    finally:
        db.close()

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("runs")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(RunsPanel)
            table = panel.query_one("#runs-table", DataTable)
            assert table.row_count == 80

            table.move_cursor(row=5, animate=False)
            table.scroll_end(animate=False)
            await pilot.pause()
            cursor_row = table.cursor_row
            scroll_offset = table.scroll_offset
            assert cursor_row == 5
            assert scroll_offset.y > 0

            panel.reload()
            await _settled(app)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + SETTLE_TIMEOUT
            while loop.time() < deadline:
                await pilot.pause()
                if table.cursor_row == cursor_row and table.scroll_offset == scroll_offset:
                    break
                await asyncio.sleep(0.05)
            assert table.cursor_row == cursor_row
            assert table.scroll_offset == scroll_offset

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
    assert "reinstall OperonDBS" in capsys.readouterr().err


def test_tui_without_project_returns_2(tmp_path: Path, capsys) -> None:
    assert main(["--project", str(tmp_path / "nowhere"), "tui"]) == 2
    assert "no project.yaml" in capsys.readouterr().err


@pytest.mark.parametrize("release_at", [0.5, 1.5])
def test_splash_waits_for_first_paint_and_initial_reads(demo_project, monkeypatch, release_at):
    """Both fast and slow reads must pass the time and data readiness gates."""
    import threading

    import operon.tui.app as app_module
    from operon import __version__
    from operon.tui.splash import SplashScreen

    clock = [0.0]
    gate = threading.Event()
    original = HomePanel._fetch

    def fetch(panel):
        if not gate.wait(10):
            raise TimeoutError("Test did not release the initial read")
        return original(panel)

    monkeypatch.setattr(app_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(HomePanel, "_fetch", fetch)

    async def scenario():
        app = OperonApp(demo_project)
        try:
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause()
                assert isinstance(app.screen, SplashScreen)
                assert __version__ in _static_text(app.screen.query_one("#splash-version", Static))
                await pilot.press("2", "?", "r", "escape")
                assert isinstance(app.screen, SplashScreen)
                clock[0] = release_at
                await pilot.pause(0.1)
                assert isinstance(app.screen, SplashScreen)
                assert "Home" in _static_text(app.screen.query_one("#splash-status", Static))
                await pilot.resize_terminal(80, 24)
                gate.set()
                if release_at < 1:
                    for _ in range(100):
                        if app.query_one(HomePanel).initial_load_complete:
                            break
                        await asyncio.sleep(0.02)
                    await pilot.pause(0.1)
                    assert isinstance(app.screen, SplashScreen)
                    assert "Ready" in _static_text(app.screen.query_one("#splash-status", Static))
                clock[0] = 2
                await _settled(app)
                await pilot.pause()
                assert not isinstance(app.screen, SplashScreen)
                assert not app._starting
        finally:
            gate.set()

    _run(scenario())


def test_splash_quit_during_minimum_display(demo_project):
    from operon.tui.splash import SplashScreen

    async def scenario():
        app = OperonApp(demo_project)
        async with app.run_test() as pilot:
            assert isinstance(app.screen, SplashScreen)
            await pilot.press("ctrl+q")
        assert not app.is_running

    _run(scenario())


@pytest.mark.bug("ODR-0032")
def test_splash_leaves_when_a_panel_drops_its_first_render(demo_project, monkeypatch):
    """A first render the panel cannot show must not wedge startup behind the splash.

    ``Panel._apply`` drops a result it has nowhere to render (the widget tree is
    not there yet, or this one was never composed); the drop used to skip the
    initial-load latch as well, and the startup worker waits for every panel to
    latch — the app then stayed on the splash screen with every panel
    unreachable (ODR-0032).
    """
    import operon.tui.app as app_module
    from operon.tui.splash import SplashScreen

    clock = [0.0]
    monkeypatch.setattr(app_module, "monotonic", lambda: clock[0])

    def unrenderable(panel, payload):
        raise NoMatches("#home-summary")

    monkeypatch.setattr(HomePanel, "render_data", unrenderable)

    async def scenario():
        app = OperonApp(demo_project)
        async with app.run_test(size=(120, 40)) as pilot:
            home = app.query_one(HomePanel)
            await _wait_until(lambda: home.initial_load_complete, "the panel to report its load")
            assert home.initial_load_failed is True
            assert app._starting is True
            await _tick_until(lambda: not app._starting, clock,
                              description="the splash screen to leave")
            await pilot.pause()
            assert not isinstance(app.screen, SplashScreen)

    _run(scenario())


@pytest.mark.bug("ODR-0032")
def test_splash_leaves_after_the_startup_deadline(demo_project, monkeypatch):
    """A load that never reports one must not hold the splash screen forever.

    The readiness gate cannot cover a worker that never delivers at all (a
    hung read, a result posted after shutdown); without a deadline the app
    would sit on the splash screen with no key able to leave it (ODR-0032).
    """
    import operon.tui.app as app_module
    from operon.tui.splash import SplashScreen

    clock = [0.0]
    monkeypatch.setattr(app_module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(HomePanel, "_load", lambda panel: None)  # never delivers

    async def scenario():
        app = OperonApp(demo_project)
        async with app.run_test(size=(120, 40)) as pilot:
            home = app.query_one(HomePanel)
            assert home.initial_load_complete is False
            assert app._starting is True
            await _tick_until(lambda: not app._starting, clock,
                              description="the startup deadline to release the app")
            await pilot.pause()
            assert not isinstance(app.screen, SplashScreen)

    _run(scenario())


@pytest.mark.bug("ODR-0029")
def test_run_detail_read_waits_for_the_loaded_content(demo_project, monkeypatch):
    """The run-detail read must key on the content, not on the worker set.

    ``_settled`` returns while no worker is registered — and the detail screen
    starts its read from ``on_mount``, a message-loop turn after it is pushed,
    so the read could still see the ``loading…`` placeholder on a slow runner.
    The test holds the load back to make that window deterministic: the old
    shape reads the placeholder, the content wait cannot.
    """
    import threading

    monkeypatch.setattr(RunDetailScreen, "on_mount", lambda screen: None)  # load not scheduled

    async def scenario():
        app = OperonApp(demo_project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            app.action_switch_screen("runs")
            await pilot.pause()
            await _settled(app)
            table = app.query_one("#runs-table", DataTable)
            table.focus()
            table.move_cursor(row=0, animate=False)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, RunDetailScreen)

            # The window the flake fell into: no worker to wait for, so the old
            # gate passes while the pane still holds its placeholder.
            await _settled(app)
            assert not app.workers
            assert "loading" in _detail_text(app)

            released = threading.Event()
            original = data.workflow_run_detail

            def gated_detail(project_arg, run_id):
                if not released.wait(10):
                    raise AssertionError("test never released the run-detail read")
                return original(project_arg, run_id)

            monkeypatch.setattr(data, "workflow_run_detail", gated_detail)
            screen = app.screen
            screen._load()
            await pilot.pause()
            released.set()
            detail = await _await_detail_text(app, "Execution details")
            assert "Workflow run" in detail

    _run(scenario())


@pytest.mark.bug("ODR-0029")
def test_no_tui_test_reads_run_detail_straight_after_settling() -> None:
    """Every read of ``#run-detail`` in the TUI tests waits for its content.

    The window this guards is invisible to a single run: ``_settled`` reports
    the worker *set*, and the detail screen schedules its load a turn after
    ``on_mount``, so on a fast machine a raw read happens to see the loaded text
    and on a slow one it reads the ``loading…`` placeholder (ODR-0029).
    ``_await_detail_text`` is that wait; a raw
    ``= _static_text(app.screen.query_one('#run-detail' …))`` assignment is the
    shape the fix removed.
    """
    import re
    from pathlib import Path

    raw_read = re.compile(r'=\s*_static_text\(\s*app\.screen\.query_one\(\s*"#run-detail"')
    offenders = []
    for path in sorted(Path(__file__).parent.glob("test_tui*.py")):
        text = path.read_text(encoding="utf-8")
        for match in raw_read.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line}")
    assert not offenders, (
        "these reads take #run-detail from a settled app instead of waiting for "
        "its content: " + ", ".join(offenders)
    )


def test_plain_q_does_not_quit(demo_project):
    """Only ctrl+q quits; a stray q must never exit the app."""

    async def scenario():
        app = OperonApp(demo_project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            await pilot.press("q")
            await pilot.pause()
            assert app.is_running
            await pilot.press("ctrl+q")
            await pilot.pause()
        assert not app.is_running

    _run(scenario())


def test_splash_resources_and_small_terminal(monkeypatch):
    import struct
    from importlib.resources import files

    from operon.tui import splash

    png = files("operon.tui").joinpath("assets/splash.png").read_bytes()
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (1024, 768)
    # The pre-sampled companion is the 4x downsampled PNG, three bytes per pixel.
    pixels = splash.lake_pixels()
    assert len(pixels) == (width // 4) * (height // 4) * 3
    assert len(set(pixels)) > 1, "splash artwork must not be a flat colour"
    assert splash.lake_text(0, 0).plain == ""
    for width, height in [(1, 1), (20, 5), (80, 24), (140, 45)]:
        rendered = splash.lake_text(width, height)
        assert len(rendered.plain.splitlines()) <= height
        assert all(len(line) <= width for line in rendered.plain.splitlines())
    splash.lake_text.cache_clear()
    def missing():
        raise FileNotFoundError("missing artwork")
    monkeypatch.setattr(splash, "lake_pixels", missing)
    assert "OPERON" in splash.lake_text(80, 24).plain
    splash.lake_text.cache_clear()


# ---------------------------------------------------------------------------
# workflow list filters and run-detail log following (milestone M2b)
# ---------------------------------------------------------------------------


def _log_text(log) -> str:
    """The plain text a RichLog currently holds."""
    return "\n".join(strip.text for strip in log.lines)


def test_list_workflow_runs_advanced_filters(tmp_path: Path) -> None:
    """The delegated filters mirror the CLI's ``workflow list`` query."""
    from operon.database import Database
    from operon.workflow import log_run

    project = Project.init(tmp_path / "runs-filters-project")
    db = Database(project.db_path)
    try:
        first = log_run(db, project, {
            "step": "qc", "status": "completed", "tool": "fastp", "executor": "local",
            "entity_type": "run", "entity_id": "RUN_000001",
            "started_at": "2026-09-01T10:00:00+08:00",
        })
        second = log_run(db, project, {
            "step": "analysis:blastn_nt", "status": "failed", "tool": "blastn",
            "executor": "slurm", "entity_type": "assembly", "entity_id": "ASM_000001",
            "parent_run_id": first["run_id"],
            "started_at": "2026-09-10T10:00:00+08:00",
        })
    finally:
        db.close()

    def run_ids(**filters) -> list[str]:
        return [row["run_id"] for row in data.list_workflow_runs(project, **filters)]

    # Newest first by default; ``oldest_first`` reverses the order.
    assert run_ids() == [second["run_id"], first["run_id"]]
    assert run_ids(oldest_first=True) == [first["run_id"], second["run_id"]]

    # Half-open time bounds, in the delegated and the substring-filter path.
    assert run_ids(started_from="2026-09-05") == [second["run_id"]]
    assert run_ids(started_to="2026-09-05") == [first["run_id"]]
    assert run_ids(step="qc", started_from="2026-09-05") == []
    assert run_ids(step="qc", started_to="2026-09-05") == [first["run_id"]]

    # Exact-match filters and paging.
    assert run_ids(run_id=first["run_id"]) == [first["run_id"]]
    assert run_ids(parent_run_id=first["run_id"]) == [second["run_id"]]
    assert run_ids(tool="blastn") == [second["run_id"]]
    assert run_ids(executor="slurm") == [second["run_id"]]
    assert run_ids(statuses=["failed"]) == [second["run_id"]]
    assert run_ids(limit=1, offset=1) == [first["run_id"]]
    assert run_ids(limit=1, offset=1, oldest_first=True) == [second["run_id"]]

    with pytest.raises(ValidationError, match="ISO-8601"):
        data.list_workflow_runs(project, started_from="yesterday")
    with pytest.raises(ValidationError, match="--from must be earlier than --to"):
        data.list_workflow_runs(project, started_from="2026-09-10", started_to="2026-09-01")

    status = data.workflow_run_status(project, second["run_id"])
    assert status is not None
    assert set(status) == {"run_id", "status", "exit_code", "finished_at"}
    assert status["run_id"] == second["run_id"]
    assert status["status"] == "failed"
    assert status["exit_code"] is None
    assert data.workflow_run_status(project, "WF_missing") is None


@pytest.mark.parametrize("value", [
    "2026-09-18",
    "2026-09-18T10:00:00",
    "2026-09-18T10:00:00+08:00",
    "2026-09-18T10:00:00Z",
])
def test_normalize_workflow_time_matches_the_cli(value: str) -> None:
    """The TUI normalizes ISO bounds exactly like ``workflow list --from/--to``."""
    import argparse

    from operon.cli import _workflow_time

    assert data.normalize_workflow_time(value) == _workflow_time(value)
    with pytest.raises(ValidationError, match="ISO-8601"):
        data.normalize_workflow_time("not a time")
    with pytest.raises(argparse.ArgumentTypeError, match="ISO-8601"):
        _workflow_time("not a time")


def test_runs_panel_advanced_filters_behind_the_more_dialog(demo_project: Project) -> None:
    """The strip stays one row; the advanced filters live in the More… dialog."""
    from operon.tui.screens.runs import RunsFiltersModal

    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("runs")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(RunsPanel)
            table = panel.query_one("#runs-table", DataTable)
            total = table.row_count
            assert total > 0
            # One control strip: status/step/entity/limit + the More… button.
            strip = panel.query_one("#runs-filters")
            assert [child.id for child in strip.children] == [
                "runs-status", "runs-step", "runs-entity", "runs-limit", "runs-more",
            ]
            assert strip.region.height <= 4
            assert panel.query_one("#runs-more", Button).label.plain == "More…"

            # A bad ISO bound is refused inline and the dialog stays open.
            await _click(pilot, "#runs-more")
            await pilot.pause()
            await _settled(app)
            modal = app.screen
            assert isinstance(modal, RunsFiltersModal)
            modal.query_one("#runs-filter-from", Input).value = "not-a-time"
            modal.query_one("#runs-filter-apply", Button).press()
            await pilot.pause()
            assert "ISO-8601" in _static_text(modal.query_one("#runs-filter-error", Static))
            assert isinstance(app.screen, RunsFiltersModal)
            assert table.row_count == total

            # from < to is enforced with the CLI's own message.
            modal.query_one("#runs-filter-from", Input).value = "2026-12-31"
            modal.query_one("#runs-filter-to", Input).value = "2026-01-01"
            modal.query_one("#runs-filter-apply", Button).press()
            await pilot.pause()
            assert "earlier than --to" in _static_text(modal.query_one("#runs-filter-error", Static))

            # Applying a real filter reloads the table and the button counts it.
            modal.query_one("#runs-filter-from", Input).value = ""
            modal.query_one("#runs-filter-to", Input).value = ""
            modal.query_one("#runs-filter-tool", Input).value = "no_such_tool"
            modal.query_one("#runs-filter-apply", Button).press()
            await _wait_until(lambda: not isinstance(app.screen, RunsFiltersModal),
                              "the filters dialog to apply")
            await _settled(app)
            assert table.row_count == 0
            assert panel.query_one("#runs-more", Button).label.plain == "More… (1)"

            # oldest-first and paging still match the delegated query verbatim.
            await _click(pilot, "#runs-more")
            await pilot.pause()
            await _settled(app)
            modal = app.screen
            modal.query_one("#runs-filter-tool", Input).value = ""
            modal.query_one("#runs-filter-oldest-first", Checkbox).value = True
            modal.query_one("#runs-filter-offset", Input).value = "1"
            modal.query_one("#runs-filter-apply", Button).press()
            await _wait_until(lambda: not isinstance(app.screen, RunsFiltersModal),
                              "the advanced filters to apply")
            await _settled(app)
            expected = data.list_workflow_runs(demo_project, limit=100, offset=1,
                                               oldest_first=True)
            assert [row["run_id"] for row in panel.runs] == [
                row["run_id"] for row in expected]
            assert panel.query_one("#runs-more", Button).label.plain == "More… (2)"

            # Clear empties the advanced set again.
            await _click(pilot, "#runs-more")
            await pilot.pause()
            await _settled(app)
            modal = app.screen
            modal.query_one("#runs-filter-clear", Button).press()
            await _wait_until(lambda: not isinstance(app.screen, RunsFiltersModal),
                              "the advanced filters to clear")
            await _settled(app)
            assert panel.advanced == {}
            assert panel.query_one("#runs-more", Button).label.plain == "More…"
            assert table.row_count == total

    _run(scenario())



def _running_run(tmp_path: Path, name: str) -> Project:
    """A project with one ``running`` workflow run; returns the project."""
    from operon.database import Database
    from operon.workflow import log_run

    project = Project.init(tmp_path / name)
    db = Database(project.db_path)
    try:
        log_run(db, project, {
            "step": "qc", "status": "running",
            "entity_type": "run", "entity_id": "RUN_000001",
        })
    finally:
        db.close()
    return project


def _finish_run(project: Project, status: str = "completed") -> None:
    from operon.database import Database

    db = Database(project.db_path)
    try:
        with db.transaction():
            db.conn.execute(
                "UPDATE workflow_runs SET status=?, exit_code=? WHERE step='qc'",
                (status, 0 if status == "completed" else 1),
            )
    finally:
        db.close()


def test_run_detail_follow_streams_logs_until_finished(tmp_path: Path) -> None:
    project = _running_run(tmp_path, "follow-project")
    run_id = data.list_workflow_runs(project)[0]["run_id"]
    stdout_path = project.logs_root / f"{run_id}.stdout.log"
    stderr_path = project.logs_root / f"{run_id}.stderr.log"
    stdout_path.write_text("starting\n", encoding="utf-8")
    stderr_path.write_text("warning: heads up\n", encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            screen = RunDetailScreen(project, run_id)
            app.push_screen(screen)
            await pilot.pause()
            await _settled(app)
            follow = screen.query_one("#run-follow", Checkbox)
            # A running run enables the switch; entering follow drains the
            # existing tails once (stdout verbatim, stderr prefixed).
            assert not follow.disabled
            follow.value = True
            await pilot.pause()
            log = screen.query_one("#run-follow-log", RichLog)
            text = _log_text(log)
            assert "starting" in text
            assert "stderr: warning: heads up" in text
            assert screen._follow_timer is not None

            # New bytes are appended incrementally on the next tick.
            stdout_path.write_text(
                stdout_path.read_text(encoding="utf-8") + "step 2 running\n", encoding="utf-8")
            screen._follow_tick()
            await pilot.pause()
            text = _log_text(log)
            assert "step 2 running" in text
            assert text.count("starting") == 1  # offsets: nothing is re-read

            # The run finishing stops the follow and reports the final status.
            _finish_run(project)
            screen._follow_tick()
            await pilot.pause()
            text = _log_text(log)
            assert f"run {run_id} finished: status=completed exit_code=0" in text
            assert screen._follow_timer is None
            assert not screen._following
            assert follow.value is False and follow.disabled
            assert any("finished: completed" in notification.message
                       for notification in app._notifications)

    _run(scenario())


def test_run_detail_follow_timer_stops_on_unmount(tmp_path: Path) -> None:
    project = _running_run(tmp_path, "follow-unmount-project")
    run_id = data.list_workflow_runs(project)[0]["run_id"]

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            screen = RunDetailScreen(project, run_id)
            app.push_screen(screen)
            await pilot.pause()
            await _settled(app)
            screen.query_one("#run-follow", Checkbox).value = True
            await pilot.pause()
            assert screen._follow_timer is not None

            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not screen
            # Leaving the screen stops the timer and clears the follow state.
            assert screen._follow_timer is None
            assert not screen._following

    _run(scenario())


# ---------------------------------------------------------------------------
# captured environments (milestone M2b)
# ---------------------------------------------------------------------------


def _seed_environments(project: Project) -> dict[str, str]:
    """Record a fully captured and a partial environment plus a corrupt row."""
    from operon.database import Database

    db = Database(project.db_path)
    try:
        with db.transaction():
            complete = db.record_environment({
                "system": {"os": "Linux"},
                "conda": {
                    "status": "captured",
                    "explicit": "@EXPLICIT\nhttps://conda.anaconda.org/ch/noarch/test-1.0-0.conda\n",
                    "packages": [{
                        "name": "test", "version": "1.0", "build": "0",
                        "url": "https://conda.anaconda.org/ch/noarch/test-1.0-0.conda",
                    }],
                },
            })
            partial = db.record_environment(
                {"system": {"os": "Linux"}, "capture_status": "partial"})
        with db.transaction():
            db.conn.execute(
                "INSERT INTO execution_environments (environment_id, document, created_at) "
                "VALUES ('ENV_000000000000', 'not json', '2026-01-01T00:00:00+08:00')")
    finally:
        db.close()
    return {"complete": complete, "partial": partial}


def test_list_environments_documents_and_exports(tmp_path: Path) -> None:
    project = Project.init(tmp_path / "environments-project")
    seeded = _seed_environments(project)

    rows = data.list_environments(project)
    # Ordered by created_at, so the pinned corrupt row comes first (the two
    # recorded rows share a timestamp and tie-break on their content address).
    assert rows[0]["environment_id"] == "ENV_000000000000"
    assert {row["environment_id"] for row in rows} == {
        "ENV_000000000000", seeded["complete"], seeded["partial"],
    }
    by_id = {row["environment_id"]: row for row in rows}
    assert by_id["ENV_000000000000"]["summary"] == "-"  # unparseable document
    assert by_id[seeded["complete"]]["summary"] == "Linux; conda (1 packages)"
    assert by_id[seeded["partial"]]["summary"] == "Linux; capture: partial"
    assert "document" not in by_id[seeded["complete"]]

    document = data.environment_document(project, seeded["complete"])
    assert document["conda"]["status"] == "captured"
    with pytest.raises(ValidationError, match="unknown environment: ENV_missing"):
        data.environment_document(project, "ENV_missing")

    assert data.export_environment(
        project, seeded["complete"], "explicit").startswith("@EXPLICIT")
    yaml_text = data.export_environment(project, seeded["complete"], "yaml")
    assert "name: operon-restored" in yaml_text
    assert "test=1.0=0" in yaml_text
    with pytest.raises(ValidationError, match="no complete Conda explicit specification"):
        data.export_environment(project, seeded["partial"], "explicit")
    with pytest.raises(ValidationError, match="no complete Conda package inventory"):
        data.export_environment(project, seeded["partial"], "yaml")


def test_environments_modal_lists_and_renders(tmp_path: Path) -> None:
    """The Tasks screen's environment browser mirrors list/show/export."""
    from operon.tui.screens.environments import EnvironmentsModal

    project = Project.init(tmp_path / "environments-ui-project")
    seeded = _seed_environments(project)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("runs")
            await pilot.pause()
            await _settled(app)
            await _click(pilot, "#runs-environments")
            await pilot.pause()
            await _settled(app)
            modal = app.screen
            assert isinstance(modal, EnvironmentsModal)
            table = modal.query_one("#environments-table", DataTable)
            assert table.row_count == 3
            output = modal.query_one("#environments-output", RichLog)

            ids = [row["environment_id"] for row in modal.environments]
            table.focus()
            table.move_cursor(row=ids.index(seeded["complete"]), animate=False)
            await pilot.pause()
            await _click(pilot, "#environments-show")
            await pilot.pause()
            assert '"status": "captured"' in _log_text(output)

            await _click(pilot, "#environments-explicit")
            await pilot.pause()
            assert _log_text(output).startswith("@EXPLICIT")

            await _click(pilot, "#environments-yaml")
            await pilot.pause()
            assert "name: operon-restored" in _log_text(output)

            # A document without a conda inventory reports inline.
            table.move_cursor(row=ids.index(seeded["partial"]), animate=False)
            await pilot.pause()
            await _click(pilot, "#environments-explicit")
            await pilot.pause()
            assert "no complete Conda explicit specification" in _static_text(
                modal.query_one("#environments-error", Static))

            await _click(pilot, "#cancel")
            await pilot.pause()
            assert not isinstance(app.screen, EnvironmentsModal)

    _run(scenario())


def test_environments_modal_requires_a_selection(tmp_path: Path) -> None:
    """Buttons without a selected row stay inline errors."""
    from operon.tui.screens.environments import EnvironmentsModal

    project = Project.init(tmp_path / "environments-empty-project")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            modal = EnvironmentsModal(project)
            app.push_screen(modal)
            await pilot.pause()
            await _settled(app)
            assert modal.query_one("#environments-table", DataTable).row_count == 0
            modal.on_button_pressed(Button.Pressed(
                modal.query_one("#environments-show", Button)))
            assert "select an environment row first" in _static_text(
                modal.query_one("#environments-error", Static))
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, EnvironmentsModal)

    _run(scenario())


# ---------------------------------------------------------------------------
# analysis hits and sequence labels (milestone M3)
# ---------------------------------------------------------------------------


def _seed_hits(project: Project) -> None:
    """Insert two completed jobs and one failed job with alignments."""
    from operon.database import Database

    db = Database(project.db_path)
    try:
        db.conn.execute("PRAGMA foreign_keys=OFF")
        for suffix in ("1", "2", "3"):
            db.insert_row("organisms", {
                "organism_id": f"ORG_00000{suffix}", "scientific_name": f"Organism {suffix}",
            })
        with db.transaction():
            db.conn.execute(
                "INSERT INTO analysis_jobs(job_id, analysis_name, entity_type, entity_id, file_id,"
                " tool, tool_version, parameter_set, parameter_sha256, input_sha256,"
                " database_identity, status, started_at) VALUES "
                "(1,'blastn_nt','organism','ORG_000001','F1','blastn','2','p','sha','in','db',"
                " 'completed','t'),"
                "(2,'blastn_nt','organism','ORG_000002','F2','blastn','2','p','sha','in','db',"
                " 'completed','t'),"
                "(3,'blastn_nt','organism','ORG_000003','F3','blastn','2','p','sha','in','db',"
                " 'failed','t')"
            )
            db.conn.execute(
                "INSERT INTO analysis_alignments(job_id, analysis_name, entity_type, entity_id,"
                " query_id, subject_id, file_id, hit_rank, query_start, query_end, subject_start,"
                " subject_end, evalue, bitscore, percent_identity) VALUES "
                "(1,'blastn_nt','organism','ORG_000001','q1','s1','F1',1,1,10,1,10,1e-5,50.0,99.0),"
                "(1,'blastn_nt','organism','ORG_000001','q1','s2','F1',2,1,10,2,11,1e-3,20.0,80.0),"
                "(2,'blastn_nt','organism','ORG_000002','q2','s1','F2',1,5,15,5,15,1e-7,60.0,100.0),"
                "(3,'blastn_nt','organism','ORG_000003','q9','s9','F3',1,1,9,1,9,1e-9,42.0,97.0)"
            )
    finally:
        db.close()


def test_analysis_hits_filters_and_retired(tmp_path: Path) -> None:
    from operon.database import Database
    from operon.lifecycle import apply_lifecycle_event

    project = Project.init(tmp_path / "hits-project")
    _seed_hits(project)

    rows = data.analysis_hits(project, limit=100)
    # Ordered by entity_id, query_id, hit_rank; the failed job is excluded.
    assert [(row["entity_id"], row["query_id"], row["hit_rank"]) for row in rows] == [
        ("ORG_000001", "q1", 1), ("ORG_000001", "q1", 2), ("ORG_000002", "q2", 1),
    ]
    assert set(rows[0]) == set(data.ANALYSIS_HIT_COLUMNS)

    assert data.analysis_hits(project, entity_type="organism", limit=100) == rows
    assert data.analysis_hits(project, entity_type="assembly") == []
    assert len(data.analysis_hits(project, analysis="blastn_nt", limit=100)) == 3
    assert data.analysis_hits(project, analysis="other") == []
    assert len(data.analysis_hits(project, entity_id="ORG_000001", limit=100)) == 2
    assert len(data.analysis_hits(project, query_id="q1", limit=100)) == 2
    assert len(data.analysis_hits(project, subject_id="s1", limit=100)) == 2
    assert len(data.analysis_hits(project, evalue_max=1e-4, limit=100)) == 2
    assert len(data.analysis_hits(project, limit=1)) == 1

    db = Database(project.db_path)
    try:
        apply_lifecycle_event(db, "organism", "ORG_000001", action="RETIRE",
                              reason="test retirement", actor="tester", reason_code="duplicate")
    finally:
        db.close()
    assert [row["entity_id"] for row in data.analysis_hits(project)] == ["ORG_000002"]
    assert len(data.analysis_hits(project, include_retired=True)) == 3


def test_sequence_label_readers(tmp_path: Path) -> None:
    from operon.database import Database
    from operon.utils import sha256_file

    project = Project.init(tmp_path / "labels-project")
    source = project.root / "labels.faa"
    source.write_text(">s1\nMTEYK\n>s2\nMTEYR\n>s3\nMTEYD\n", encoding="utf-8")
    db = Database(project.db_path)
    try:
        db.insert_row("files", {
            "file_id": "FIL_000001", "entity_type": "annotation", "entity_id": "ANN_000001",
            "file_role": "protein_fasta", "format": "fasta", "compression": "none",
            "relative_path": "labels.faa", "size_bytes": source.stat().st_size,
            "sha256": sha256_file(source), "status": "CHECKSUM_VERIFIED",
        })
        db.insert_row("files", {
            "file_id": "FIL_000002", "entity_type": "annotation", "entity_id": "ANN_000002",
            "file_role": "protein_fasta", "format": "fasta", "compression": "none",
            "relative_path": "labels.faa", "size_bytes": source.stat().st_size,
            "sha256": sha256_file(source), "status": "CHECKSUM_VERIFIED",
        })
        with db.transaction():
            db.conn.execute(
                "INSERT INTO sequence_labels(file_id, seqid, label, profile_name,"
                " profile_sha256, decided_at) VALUES "
                "('FIL_000001','s1','A','bhlh','sha','t'),"
                "('FIL_000001','s2','A','bhlh','sha','t'),"
                "('FIL_000001','s3','U','bhlh','sha','t'),"
                "('FIL_000002','s1','A','bhlh','sha','t'),"
                "('FIL_000002','s9','B','other','sha','t')"
            )
    finally:
        db.close()

    summary = data.label_summary(project)
    assert [(row["label"], row["profile_name"], row["sequences"], row["files"])
            for row in summary] == [("A", "bhlh", 3, 2), ("B", "other", 1, 1), ("U", "bhlh", 1, 1)]
    assert [row["label"] for row in data.label_summary(project, profile_name="other")] == ["B"]

    labels = data.file_sequence_labels(project, "FIL_000001")
    assert [(row["label"], row["seqid"]) for row in labels] == [("A", "s1"), ("A", "s2"), ("U", "s3")]
    assert data.file_sequence_labels(project, "FIL_000009") == []

    detail = data.file_detail(project, "FIL_000001")
    assert detail is not None
    assert detail["labels"] == labels


def test_write_analysis_report_matches_cli_export(tmp_path: Path) -> None:
    """A TUI export is byte-identical to `report analysis --hits --out`."""
    import argparse

    from operon import cli
    from operon.database import Database

    project = Project.init(tmp_path / "hits-export-project")
    _seed_hits(project)
    for fmt in ("text", "tsv", "json"):
        cli_path = tmp_path / f"cli-{fmt}.txt"
        tui_path = tmp_path / f"tui-{fmt}.txt"
        args = argparse.Namespace(
            analysis=None, entity_type=None, entity_id=None, query_id=None,
            subject_id=None, evalue_max=None, limit=50, include_retired=False,
            hits=True, format=fmt, out=str(cli_path),
        )
        db = Database(project.db_path)
        try:
            assert cli._cmd_analysis_results(args, db) == 0
        finally:
            db.close()
        result = actions.write_analysis_report(project, out=str(tui_path), fmt=fmt, limit=50)
        assert result["rows"] == 3
        assert result["path"] == str(tui_path)
        assert tui_path.read_text(encoding="utf-8") == cli_path.read_text(encoding="utf-8")

    # The CLI's empty-result line is reused for an empty export.
    empty = tmp_path / "empty.txt"
    actions.write_analysis_report(project, out=str(empty), fmt="text", analysis="missing")
    assert empty.read_text(encoding="utf-8") == "(no analysis results)\n"

    with pytest.raises(ValidationError, match="output path is required"):
        actions.write_analysis_report(project, out="   ")
    with pytest.raises(ValidationError, match="unknown report format"):
        actions.write_analysis_report(project, out=str(tmp_path / "x"), fmt="yaml")


def test_analysis_hits_modal_browses_and_exports(tmp_path: Path) -> None:
    from operon.tui.screens.hits import AnalysisHitsModal

    project = Project.init(tmp_path / "hits-ui-project")
    _seed_hits(project)
    out_path = tmp_path / "hits.tsv"

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            app.action_switch_screen("runs")
            await pilot.pause()
            await _settled(app)
            await _click(pilot, "#runs-hits")
            await pilot.pause()
            await _settled(app)
            modal = app.screen
            assert isinstance(modal, AnalysisHitsModal)
            table = modal.query_one("#hits-table", DataTable)
            assert table.row_count == 3
            assert "3 hit row(s)" in _static_text(modal.query_one("#hits-status", Static))

            # Filters reload through the same read-only query.
            modal.query_one("#hits-entity-id", Input).value = "ORG_000002"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 1
            modal.query_one("#hits-query-id", Input).value = "nope"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 0
            assert "0 hit row(s)" in _static_text(modal.query_one("#hits-status", Static))
            modal.query_one("#hits-query-id", Input).value = ""
            modal.query_one("#hits-entity-id", Input).value = ""
            await pilot.pause()
            await _settled(app)

            modal.query_one("#hits-evalue-max", Input).value = "0.0001"
            await pilot.pause()
            await _settled(app)
            assert table.row_count == 2
            modal.query_one("#hits-evalue-max", Input).value = ""
            await pilot.pause()
            await _settled(app)

            # Export writes the same rows through the CLI's renderer.
            modal.query_one("#hits-format", Select).value = "tsv"
            modal.query_one("#hits-out", Input).value = str(out_path)
            await _click(pilot, "#hits-export-button")
            await _wait_until(
                lambda: "wrote 3 row(s)" in _static_text(
                    modal.query_one("#hits-status", Static)),
                "hits export to finish",
            )
            # A failed export reports inline and keeps the modal open.
            modal.query_one("#hits-out", Input).value = "   "
            await _click(pilot, "#hits-export-button")
            await _wait_until(
                lambda: "export failed" in _static_text(
                    modal.query_one("#hits-status", Static)),
                "hits export error",
            )
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, AnalysisHitsModal)

    _run(scenario())
    text = out_path.read_text(encoding="utf-8")
    assert text.splitlines()[0].split("\t") == list(data.ANALYSIS_HIT_COLUMNS)
    assert len(text.strip().splitlines()) == 4  # header + 3 hits


def test_sequence_labels_modal_and_file_detail(tmp_path: Path) -> None:
    from operon.database import Database
    from operon.tui.screens.labels import SequenceLabelsModal
    from operon.utils import sha256_file

    project = Project.init(tmp_path / "labels-ui-project")
    source = project.root / "labels.faa"
    source.write_text(">s1\nMTEYK\n>s2\nMTEYR\n", encoding="utf-8")
    db = Database(project.db_path)
    try:
        db.insert_row("files", {
            "file_id": "FIL_000001", "entity_type": "annotation", "entity_id": "ANN_000001",
            "file_role": "protein_fasta", "format": "fasta", "compression": "none",
            "relative_path": "labels.faa", "size_bytes": source.stat().st_size,
            "sha256": sha256_file(source), "status": "CHECKSUM_VERIFIED",
        })
        with db.transaction():
            db.conn.execute(
                "INSERT INTO sequence_labels(file_id, seqid, label, profile_name,"
                " profile_sha256, decided_at) VALUES "
                "('FIL_000001','s1','A','bhlh','sha','t'),"
                "('FIL_000001','s2','U','bhlh','sha','t')"
            )
    finally:
        db.close()

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            table = app.query_one("#files-table", DataTable)
            table.focus()
            table.move_cursor(row=0, animate=False)
            await pilot.pause()
            await _settled(app)
            detail = _static_text(app.query_one("#file-detail", Static))
            assert "Sequence labels" in detail
            assert "A" in detail and "bhlh" in detail

            await pilot.press("l")
            await pilot.pause()
            await _settled(app)
            modal = app.screen
            assert isinstance(modal, SequenceLabelsModal)
            assert modal.file_id is not None
            summary_table = modal.query_one("#labels-table", DataTable)
            assert summary_table.row_count == 2
            assert "2 label/profile group(s) in the project" in _static_text(
                modal.query_one("#labels-status", Static))
            file_table = modal.query_one("#labels-file-table", DataTable)
            assert modal.file_id == "FIL_000001"
            assert file_table.row_count == 2
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, SequenceLabelsModal)

    _run(scenario())


def _overflowing_controls(root: Any) -> list[str]:
    """Return identified row controls that overflow or are unusably narrow (ODR-0019)."""
    from textual.containers import Horizontal
    from textual.widgets import Button, Checkbox, Input, Select

    problems: list[str] = []
    for row in root.query(Horizontal):
        region = row.region
        right, bottom = region.x + region.width, region.y + region.height
        for child in row.children:
            if not isinstance(child, (Button, Checkbox, Input, Select)):
                continue
            if child.id is None or not child.display or child.region.height == 0:
                continue  # composite internals (a Select's own parts have no id)
            child_region = child.region
            if child_region.x + child_region.width > right or child_region.y >= bottom:
                problems.append(
                    f"{row.id}: {child.id} outside its row ({child_region} vs {region})"
                )
            elif child_region.width < 8:
                problems.append(f"{row.id}: {child.id} squeezed to {child_region.width} columns")
    return problems


@pytest.mark.bug("ODR-0019")
def test_filter_rows_keep_their_controls_inside_the_row(
    demo_project: Project,
    tmp_path: Path,
) -> None:
    """Every filter-row control fits inside its row (ODR-0019).

    An over-constrained ``Horizontal`` hands each child its preferred width, so a
    row without width rules pushes its trailing widgets past the right edge — the
    Analysis jobs modal lost its status and limit filters that way.  Mount the
    screens and modals that own filter rows and check each identified control.
    """
    from operon.tui.screens.hits import AnalysisHitsModal
    from operon.tui.screens.runs import AnalysisJobsModal

    hits_project = Project.init(tmp_path / "filter-row-project")
    _seed_hits(hits_project)

    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            for screen in ("files", "runs"):
                app.action_switch_screen(screen)
                await pilot.pause()
                await _settled(app)
                assert not _overflowing_controls(app.screen), f"{screen} screen"
            for modal in (AnalysisJobsModal(demo_project), AnalysisHitsModal(hits_project)):
                app.push_screen(modal)
                await pilot.pause()
                await _wait_until(
                    lambda target=modal: not target._loading, f"{type(modal).__name__} load",
                )
                assert not _overflowing_controls(modal), type(modal).__name__
                app.pop_screen()
                await pilot.pause()

    _run(scenario())


@pytest.mark.bug("ODR-0020")
def test_analysis_jobs_modal_layout_keeps_its_panes(demo_project: Project,
                                                    tmp_path: Path) -> None:
    """The jobs dialog's panes stay inside the box and never overlap (ODR-0020).

    The box used to hold no ``1fr`` child, so the surplus height went to the
    filter row: the table collapsed to one row, its rows rendered across the
    neighbouring controls, and the detail pane was invisible.
    """
    project, _seeded = _seed_analysis_jobs(demo_project, tmp_path)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("runs")
            await pilot.pause()
            await _settled(app)
            await _click(pilot, "#runs-jobs")
            await pilot.pause()
            await _settled(app)
            modal = app.screen
            assert isinstance(modal, AnalysisJobsModal)

            box = modal.query_one("#modal-box").region
            table = modal.query_one("#jobs-table", DataTable)
            detail_scroll = modal.query_one("#jobs-detail-scroll").region
            filters = modal.query_one("#jobs-filters").region
            buttons = modal.query_one("#modal-buttons").region

            # Every pane lives inside the box, and no two of the box's children
            # overlap: the filter row used to absorb the whole height and sit on
            # top of the table.
            panes = [w for w in modal.query_one("#modal-box").children if w.display]
            for pane in panes:
                region = pane.region
                assert region.y >= box.y, f"{pane.id} starts above the box"
                assert region.y + region.height <= box.y + box.height, \
                    f"{pane.id} ends below the box: {region} vs {box}"
            for index, first in enumerate(panes):
                for second in panes[index + 1:]:
                    a, b = first.region, second.region
                    assert not (a.x < b.x + b.width and b.x < a.x + a.width
                                and a.y < b.y + b.height and b.y < a.y + a.height), \
                        f"{first.id} overlaps {second.id}: {a} vs {b}"

            # The table gets a real viewport (not the single squeezed row) and
            # the detail keeps a usable column to its right.
            assert table.region.height >= 5, f"table got {table.region.height} rows"
            assert filters.y + filters.height <= table.region.y
            assert table.region.y + table.region.height <= buttons.y
            assert detail_scroll.width >= 20 and detail_scroll.height >= 5
            assert detail_scroll.x >= table.region.x + table.region.width
            assert table.region.y < detail_scroll.y + detail_scroll.height
            assert detail_scroll.y < table.region.y + table.region.height

    _run(scenario())


def test_fitting_select_expands_to_the_longest_option(demo_project: Project) -> None:
    """A long option renders on one line: the dropdown is as wide as its label."""
    from textual.widgets._select import SelectOverlay

    from operon.tui.screens.common import FittingSelect

    label = "sequence_classification (label sequences)"

    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            probe = FittingSelect([(label, "x"), ("short", "y")], value="x", id="fit-probe")
            probe.styles.width = 20
            short = FittingSelect([(label, "x"), ("short", "y")], value="y", id="fit-short")
            short.styles.width = 20
            await app.screen.mount(probe)
            await app.screen.mount(short)
            await pilot.pause()
            await _settled(app)

            # The collapsed control shows a long value without growing: a select
            # whose value is long is exactly as tall as one whose value is short.
            assert probe.region.height == short.region.height, (probe.region, short.region)

            probe.focus()
            probe.action_show_overlay()
            overlay = probe.query_one(SelectOverlay)
            await _wait_until(lambda: overlay.region.width > probe.region.width,
                              "the dropdown to widen to its content")
            assert overlay.region.width >= len(label) + 2, overlay.region
            assert overlay.region.width <= int(app.size.width * 0.8) + 1, overlay.region
            probe.expanded = False
            await pilot.pause()

    _run(scenario())


@pytest.mark.bug("ODR-0038")
def test_fitting_select_mount_without_an_overlay_does_not_crash_the_app(
    demo_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A select mounted before its overlay exists does not raise into the app.

    Textual dispatches ``_on_mount`` on every class in the MRO, so ``Select``'s own
    handler runs next to ``FittingSelect``'s guarded override — and only the override
    caught the failed overlay lookup.  Under load (the 3.11 and 3.12 legs of the
    local matrix) the traceback reached the app and ``run_test`` re-raised it at
    teardown.  The overlay is kept out of the tree here, so every retry misses too:
    the app has to survive, the value has to be adopted before the paint (ODR-0026),
    and readiness has to stay false rather than the app dying.
    """
    from textual.widgets import Select
    from textual.widgets._select import SelectOverlay

    from operon.tui.screens.common import FittingSelect

    original_compose = Select.compose

    def without_overlay(self: Select):
        for child in original_compose(self):
            if not isinstance(child, SelectOverlay):
                yield child

    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settled(app)
            monkeypatch.setattr(Select, "compose", without_overlay)
            probe = FittingSelect([("short", "x"), ("longer", "y")], value="y", id="odr-0038")
            await app.screen.mount(probe)
            monkeypatch.undo()

            # The retry budget runs out with the overlay still missing, and the app
            # is still there to be asked about it.
            for _ in range(60):
                await pilot.pause()
            assert probe.is_mounted
            assert not probe.options_ready
            assert probe.value == "y"

    _run(scenario())


@pytest.mark.bug("ODR-0039")
def test_fitting_select_reports_a_mount_that_ran_out_of_retries(
    demo_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A select that never gets its overlay says so, by name.

    ``options_ready`` stays False when the retry budget runs out, which reads exactly
    like "still coming" — so the form refuses every save with no reason to give.  The
    control has to report the difference: a reader can then tell the two apart and
    name what is stuck.
    """
    from textual.widgets import Select
    from textual.widgets._select import SelectOverlay

    from operon.tui.screens.common import FittingSelect

    original_compose = Select.compose

    def without_overlay(self: Select):
        for child in original_compose(self):
            if not isinstance(child, SelectOverlay):
                yield child

    warnings: list[str] = []

    class Recorder:
        """Stands in for the widget's Logger, which is a read-only property."""

        def warning(self, message: str, *args, **kwargs) -> None:
            warnings.append(message)

    async def scenario() -> None:
        app = OperonApp(demo_project)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settled(app)
            monkeypatch.setattr(FittingSelect, "log", property(lambda self: Recorder()))
            monkeypatch.setattr(Select, "compose", without_overlay)
            probe = FittingSelect([("short", "x")], value="x", id="odr-0039")
            await app.screen.mount(probe)

            # Out of retries, with the overlay still missing.  The patches stay for
            # the whole run: the recorder above has to be the widget's logger while
            # the retries give up, and the compose patch only matters at mount time.
            for _ in range(60):
                await pilot.pause()
            assert probe.is_mounted
            assert not probe.options_ready
            assert probe.options_gave_up
            assert warnings, "the exhausted retry path reported nothing"
            assert "odr-0039" in warnings[0], warnings

    _run(scenario())
