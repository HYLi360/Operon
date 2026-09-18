"""Phase-2 TUI write actions: audited mutations through actions.py and modals."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.css.query import NoMatches
from textual.widgets import Button, DataTable, Input, Label, Select, Static, Tree

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ConflictError, ValidationError
from operon.tui import actions, data
from operon.tui.app import OperonApp
from operon.tui.screens.common import ErrorDialog
from operon.tui.screens.decisions import CurateModal, DecisionsPanel, EvaluateModal
from operon.tui.screens.entities import EntitiesPanel, LifecycleModal
from operon.tui.screens.files import FilesPanel
from operon.tui.screens.files_ops import IngestModal, QcModal, VerifyModal
from operon.tui.screens.run_external import RunExternalModal
from operon.tui.screens.runs import RunDetailScreen


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-writes-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    """Each write test gets its own copy of the demo project."""
    target = tmp_path / "project"
    shutil.copytree(demo_template.root, target)
    return Project.find(target)


def _query(project: Project, sql: str, params: tuple = ()) -> list[dict]:
    db = Database(project.db_path, read_only=True)
    try:
        return [dict(row) for row in db.query(sql, params)]
    finally:
        db.close()


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 15.0


def _run(coroutine) -> None:
    """Drive a Textual headless scenario without requiring pytest-asyncio."""
    asyncio.run(asyncio.wait_for(coroutine, timeout=SCENARIO_TIMEOUT))


async def _settled(app, timeout: float = SETTLE_TIMEOUT) -> None:
    """Wait until no workers are running, with a diagnostic timeout."""
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


async def _click(pilot, selector: str) -> None:
    """Click a widget, failing loudly when the click does not land on it.

    ``Pilot.click`` silently returns False when the target is clipped or
    obscured (e.g. a modal button pushed out of the box), which otherwise
    surfaces much later as a confusing timeout.
    """
    assert await pilot.click(selector), f"click did not land on {selector}"


def _find_tree_node(tree: Tree, entity_type: str, entity_id: str):
    stack = list(tree.root.children)
    while stack:
        node = stack.pop()
        if node.data == (entity_type, entity_id):
            return node
        stack.extend(node.children)
    return None


# ---------------------------------------------------------------------------
# Data layer additions (read-only)
# ---------------------------------------------------------------------------


def test_list_decisions_and_profiles(project: Project) -> None:
    rows = data.list_decisions(project)
    assert len(rows) == 6
    by_key = {(r["entity_type"], r["entity_id"]): r for r in rows}
    assert by_key[("assembly", "ASM_000002")]["decision"] == "FAIL"

    fails = data.list_decisions(project, decision="FAIL")
    assert {r["entity_id"] for r in fails} == {"ASM_000002", "ANN_000003"}

    by_profile = data.list_decisions(project, profile="reads_qc_v1")
    assert [r["entity_id"] for r in by_profile] == ["RUN_000001"]

    by_text = data.list_decisions(project, text="ASM_000001")
    assert [r["entity_id"] for r in by_text] == ["ASM_000001"]

    limited = data.list_decisions(project, limit=2)
    assert [r["entity_id"] for r in limited] == ["ANN_000001", "ANN_000003"]

    profiles = data.list_profiles(project)
    assert "assembly_production_v1" in profiles
    assert "reads_qc_v1" in profiles


# ---------------------------------------------------------------------------
# Actions layer
# ---------------------------------------------------------------------------


def test_evaluate_appends_decisions(project: Project) -> None:
    before = _query(project, "SELECT COUNT(*) AS n FROM decisions")[0]["n"]
    results = actions.evaluate(project, entity_type="assembly")
    assert len(results) == 3
    assert {r["decision"] for r in results} == {"PASS", "FAIL"}
    failing = next(r for r in results if r["entity_id"] == "ASM_000002")
    assert failing["reason_codes"] == ["LOW_CONTIGUITY"]
    after = _query(project, "SELECT COUNT(*) AS n FROM decisions")[0]["n"]
    assert after == before + 3


def test_evaluate_requires_entity_type_with_entity_id(project: Project) -> None:
    with pytest.raises(ValidationError):
        actions.evaluate(project, entity_id="ASM_000001")


def test_evaluate_single_entity(project: Project) -> None:
    results = actions.evaluate(project, entity_type="assembly", entity_id="ASM_000001")
    assert len(results) == 1
    assert results[0]["entity_id"] == "ASM_000001"
    assert results[0]["profile"] == "assembly_production_v1"


def test_lifecycle_actor_required(project: Project, monkeypatch) -> None:
    monkeypatch.delenv("USER", raising=False)
    with pytest.raises(ValidationError, match="--actor is required"):
        actions.lifecycle_apply(project, "ASM_000001", "RETIRE", reason="x", actor="")


def test_lifecycle_bad_action(project: Project) -> None:
    with pytest.raises(ValidationError, match="unsupported lifecycle action"):
        actions.lifecycle_apply(project, "ASM_000001", "DELETE", reason="x", actor="tester")


def test_curate_retired_entity_raises(project: Project) -> None:
    actions.lifecycle_apply(
        project, "ASM_000001", "RETIRE", reason="gone", actor="tester",
        reason_code="duplicate",
    )
    with pytest.raises(ValidationError, match="retired"):
        actions.curate(
            project, "assembly", "ASM_000001", "assembly_production_v1",
            "FAIL", reviewer="tester", reason="cannot curate retired",
        )


def test_run_qc_without_progress_callback(project: Project) -> None:
    first = _query(project, "SELECT file_id FROM files ORDER BY file_id LIMIT 1")[0]["file_id"]
    results = actions.run_qc(project, file_id=first)
    assert len(results) == 1 and results[0]["ok"] is True


def test_curate_updates_decision_audit_and_state(project: Project) -> None:
    cases = [
        ("assembly", "ASM_000002", "assembly_production_v1", "ACCEPT_WITH_WARNING", "ACCEPTED"),
        ("annotation", "ANN_000003", "annotation_release_v1", "FAIL", "REJECTED"),
        ("assembly", "ASM_000001", "assembly_production_v1", "REVIEW", "REVIEW"),
    ]
    for entity_type, entity_id, profile, decision, expected_state in cases:
        actions.curate(
            project, entity_type, entity_id, profile, decision,
            reviewer="tester", reason=f"manual {decision}", evidence="ticket-1",
        )
        row = _query(
            project,
            "SELECT curated_decision, curated_by FROM current_decisions "
            "WHERE entity_type=? AND entity_id=? AND profile=?",
            (entity_type, entity_id, profile),
        )[0]
        assert row["curated_decision"] == decision
        assert row["curated_by"] == "tester"
        change = _query(
            project,
            "SELECT field, new_value, reason, actor FROM changes "
            "WHERE object_type='decision' AND object_id=? ORDER BY change_id DESC LIMIT 1",
            (f"{entity_type}:{entity_id}:{profile}",),
        )[0]
        assert change["field"] == "curated_decision"
        assert change["new_value"] == decision
        state = _query(
            project,
            "SELECT state FROM entity_state WHERE entity_type=? AND entity_id=?",
            (entity_type, entity_id),
        )[0]
        assert state["state"] == expected_state


def test_curate_without_automatic_decision_raises(project: Project) -> None:
    with pytest.raises(ValidationError, match="no automatic decision"):
        actions.curate(
            project, "organism", "ORG_000001", "assembly_production_v1",
            "FAIL", reviewer="tester", reason="no decision exists",
        )


def test_lifecycle_retire_restore_roundtrip(project: Project) -> None:
    preview = actions.lifecycle_preview(project, "ASM_000001", "RETIRE")
    assert preview["will_change"] is True
    assert preview["target"] == {"entity_type": "assembly", "entity_id": "ASM_000001"}
    assert preview["entity_counts"]["assembly"] == 1
    assert preview["entity_counts"]["annotation"] == 1
    assert preview["reference_counts"]["files"] >= 2
    assert preview["physical_changes"]["artifact_bytes_deleted"] == 0

    result = actions.lifecycle_apply(
        project, "ASM_000001", "RETIRE", reason="superseded in demo test",
        actor="tester", reason_code="duplicate", evidence="ticket-9",
    )
    assert result["applied"] is True
    assert result["effectively_retired"] is True
    assert result["event"]["action"] == "RETIRE"
    assert result["event"]["reason_code"] == "duplicate"

    events = _query(
        project,
        "SELECT action, reason_code, actor FROM entity_lifecycle_events "
        "WHERE object_type='assembly' AND object_id='ASM_000001'",
    )
    assert [(e["action"], e["reason_code"], e["actor"]) for e in events] == [
        ("RETIRE", "duplicate", "tester")
    ]
    runs = _query(project, "SELECT step, status, command FROM workflow_runs WHERE step='lifecycle_retire'")
    assert len(runs) == 1
    assert runs[0]["status"] == "completed"
    assert runs[0]["command"] == "operon retire ASM_000001"
    current = _query(
        project,
        "SELECT action FROM current_entity_lifecycle "
        "WHERE object_type='assembly' AND object_id='ASM_000001'",
    )
    assert [row["action"] for row in current] == ["RETIRE"]
    assert data.entity_tree(project)[0]["entity_id"] == "ORG_000001"
    assert all(
        node["entity_id"] != "ASM_000001"
        for org in data.entity_tree(project)
        for node in _walk(org)
    )

    restore = actions.lifecycle_apply(
        project, "ASM_000001", "RESTORE", reason="mistake", actor="tester",
    )
    assert restore["applied"] is True
    assert restore["effectively_retired"] is False
    assert restore["event"]["action"] == "RESTORE"
    current = _query(
        project,
        "SELECT action FROM current_entity_lifecycle "
        "WHERE object_type='assembly' AND object_id='ASM_000001'",
    )
    assert [row["action"] for row in current] == ["RESTORE"]
    runs = _query(project, "SELECT step FROM workflow_runs WHERE step='lifecycle_restore'")
    assert len(runs) == 1


def _walk(node: dict):
    yield node
    for child in node["children"]:
        yield from _walk(child)


def test_lifecycle_noop_and_blocker(project: Project) -> None:
    actions.lifecycle_apply(
        project, "ASM_000001", "RETIRE", reason="first", actor="tester",
        reason_code="duplicate",
    )
    result = actions.lifecycle_apply(
        project, "ASM_000001", "RETIRE", reason="second", actor="tester",
        reason_code="duplicate",
    )
    assert result["applied"] is False
    events = _query(
        project,
        "SELECT COUNT(*) AS n FROM entity_lifecycle_events "
        "WHERE object_type='assembly' AND object_id='ASM_000001'",
    )
    assert events[0]["n"] == 1
    with pytest.raises(ValidationError, match="already active"):
        actions.lifecycle_apply(
            project, "ASM_000002", "RESTORE", reason="nothing to do", actor="tester",
        )


def test_lifecycle_invalid_reason_code_raises(project: Project) -> None:
    with pytest.raises(ValidationError, match="reason_code"):
        actions.lifecycle_apply(
            project, "ASM_000001", "RETIRE", reason="bad code",
            actor="tester", reason_code="not_a_code",
        )


def test_ingest_idempotent_and_conflict(project: Project) -> None:
    source = project.root / "incoming.fasta"
    source.write_text(">ctgX\nACGTACGTACGT\n", encoding="utf-8")
    row = actions.ingest(project, str(source), "assembly", "ASM_000001", "extra_fasta")
    assert row["file_role"] == "extra_fasta"
    assert row["entity_id"] == "ASM_000001"

    again = actions.ingest(project, str(source), "assembly", "ASM_000001", "extra_fasta")
    assert again["file_id"] == row["file_id"]

    count = _query(project, "SELECT COUNT(*) AS n FROM files")[0]["n"]
    source.write_text(">ctgX\nTTTTGGGGCCCC\n", encoding="utf-8")
    with pytest.raises(ConflictError) as excinfo:
        actions.ingest(project, str(source), "assembly", "ASM_000001", "extra_fasta")
    assert "sha256" in str(excinfo.value)
    assert _query(project, "SELECT COUNT(*) AS n FROM files")[0]["n"] == count


def test_ingest_missing_source_raises(project: Project) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        actions.ingest(project, str(project.root / "nope.fasta"), "assembly", "ASM_000001", "x")


def test_verify_marks_deleted_file_missing(project: Project) -> None:
    record = _query(project, "SELECT file_id, relative_path, status FROM files LIMIT 1")[0]
    (project.root / record["relative_path"]).unlink()
    results = actions.verify(project, [record["file_id"]])
    assert results[0]["file_id"] == record["file_id"]
    assert results[0]["status"] == "MISSING"
    row = _query(project, "SELECT status FROM files WHERE file_id=?", (record["file_id"],))[0]
    assert row["status"] == "MISSING"


def test_run_qc_writes_results_and_reports_progress(project: Project) -> None:
    first = _query(project, "SELECT file_id FROM files ORDER BY file_id LIMIT 1")[0]["file_id"]
    # qc_results upserts on input identity; the appended workflow run is the
    # reliable sign that QC actually ran again for the file.
    before = _query(project, "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='qc'")[0]["n"]
    events: list[tuple[int, int, str]] = []
    results = actions.run_qc(
        project, file_id=first,
        progress=lambda done, total, result: events.append((done, total, result["file_id"])),
    )
    assert len(results) == 1 and results[0]["ok"] is True
    assert events == [(1, 1, first)]
    after = _query(project, "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='qc'")[0]["n"]
    assert after == before + 1
    metrics = _query(project, "SELECT COUNT(*) AS n FROM qc_results WHERE file_id=?", (first,))
    assert metrics[0]["n"] > 0

    events.clear()
    results = actions.run_qc(
        project, entity_type="assembly",
        progress=lambda done, total, result: events.append((done, total, result["file_id"])),
    )
    assert len(results) == 3
    assert [done for done, _, _ in events] == [1, 2, 3]
    assert all(total == 3 for _, total, _ in events)


# ---------------------------------------------------------------------------
# Headless UI: decisions screen + curate/evaluate modals
# ---------------------------------------------------------------------------


def test_decisions_screen_and_curate_end_to_end(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("decisions")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(DecisionsPanel)
            table = panel.query_one("#decisions-table", DataTable)
            assert table.row_count == 6

            index = next(
                i for i, row in enumerate(panel.decisions) if row["entity_id"] == "ASM_000002"
            )
            table.focus()
            table.move_cursor(row=index, animate=False)
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, CurateModal)
            assert "operon curate" in _static_text(modal.query_one("#modal-command", Static))
            assert "FAIL →" in _static_text(modal.query_one("#curate-preview", Static))

            modal.query_one("#curate-decision", Select).value = "ACCEPT_WITH_WARNING"
            modal.query_one("#curate-reason", Input).value = "contiguity acceptable for demo"
            await pilot.pause()
            assert "ACCEPT_WITH_WARNING" in _static_text(modal.query_one("#curate-preview", Static))
            assert "contiguity acceptable" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, CurateModal)

    _run(scenario())

    refreshed = data.list_decisions(project, text="ASM_000002")
    assert refreshed[0]["curated_decision"] == "ACCEPT_WITH_WARNING"


def test_curate_empty_reason_stays_open_without_writing(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("decisions")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(DecisionsPanel)
            table = panel.query_one("#decisions-table", DataTable)
            index = next(
                i for i, row in enumerate(panel.decisions) if row["entity_id"] == "ASM_000002"
            )
            table.focus()
            table.move_cursor(row=index, animate=False)
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, CurateModal)

            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            assert isinstance(app.screen, CurateModal)
            assert "reason is required" in _static_text(modal.query_one("#modal-error", Static))
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, CurateModal)

    _run(scenario())
    changes = _query(project, "SELECT COUNT(*) AS n FROM changes WHERE object_type='decision'")
    assert changes[0]["n"] == 0


def test_evaluate_modal_end_to_end(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("decisions")
            await pilot.pause()
            await _settled(app)
            before = _query(project, "SELECT COUNT(*) AS n FROM decisions")[0]["n"]

            panel = app.query_one(DecisionsPanel)
            panel.query_one("#decisions-table", DataTable).focus()
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, EvaluateModal)
            assert "operon evaluate" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, EvaluateModal)
            assert _query(project, "SELECT COUNT(*) AS n FROM decisions")[0]["n"] == before + 3

    _run(scenario())


# ---------------------------------------------------------------------------
# Headless UI: lifecycle modal
# ---------------------------------------------------------------------------


def test_lifecycle_modal_retire_and_restore(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("entities")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(EntitiesPanel)
            tree = panel.query_one("#entities-tree", Tree)

            node = _find_tree_node(tree, "assembly", "ASM_000001")
            assert node is not None
            tree.select_node(node)
            await pilot.pause()
            await _settled(app)
            tree.focus()
            await pilot.press("x")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, LifecycleModal)
            await _settled(app)  # plan preview worker
            await pilot.pause()
            plan_text = _static_text(modal.query_one("#lifecycle-plan", Static))
            assert "RETIRE" in plan_text
            assert "ASM_000001" in plan_text
            assert not modal.query_one("#confirm", Button).disabled
            modal.query_one("#lifecycle-reason", Input).value = "retire from the TUI test"
            await pilot.pause()
            assert "operon retire ASM_000001" in _static_text(
                modal.query_one("#modal-command", Static)
            )
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, LifecycleModal)

            retired = _query(project, "SELECT entity_id FROM effective_retired_entities")
            assert "ASM_000001" in {row["entity_id"] for row in retired}

            # Retired entities stay visible (dimmed) by default; select it directly.
            node = _find_tree_node(tree, "assembly", "ASM_000001")
            assert node is not None
            assert "(retired)" in node.label.plain
            tree.select_node(node)
            await pilot.pause()
            await _settled(app)
            tree.focus()
            await pilot.press("x")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, LifecycleModal)
            await _settled(app)
            await pilot.pause()
            assert "RESTORE" in _static_text(modal.query_one("#lifecycle-plan", Static))
            modal.query_one("#lifecycle-reason", Input).value = "restore from the TUI test"
            await pilot.pause()
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, LifecycleModal)

            retired = _query(project, "SELECT entity_id FROM effective_retired_entities")
            assert "ASM_000001" not in {row["entity_id"] for row in retired}

    _run(scenario())


# ---------------------------------------------------------------------------
# Headless UI: files ingest / QC modals
# ---------------------------------------------------------------------------


def test_ingest_modal_end_to_end(project: Project) -> None:
    # A deep source path makes the equivalent-command line wrap, which used to
    # push the Confirm button outside the modal's max-height on CI runners
    # with long temp paths (macOS), where pilot.click silently missed it.
    source = project.root / (
        "nested_" + "d" * 60 + "/deeper_" + "d" * 60 + "/ui_ingest.fasta"
    )
    source.parent.mkdir(parents=True)
    source.write_text(">ui_ctg\nACGTACGT\n", encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(FilesPanel)
            table = panel.query_one("#files-table", DataTable)
            before = table.row_count

            table.focus()
            await pilot.press("i")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, IngestModal)
            modal.query_one("#ingest-source", Input).value = str(source)
            modal.query_one("#ingest-entity-type", Select).value = "assembly"
            modal.query_one("#ingest-entity-id", Input).value = "ASM_000001"
            modal.query_one("#ingest-role", Input).value = "ui_extra_fasta"
            await pilot.pause()
            assert "operon ingest" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _wait_until(
                lambda: not isinstance(app.screen, IngestModal),
                "dismiss the ingest modal",
            )
            await _settled(app)
            assert not isinstance(app.screen, IngestModal)
            assert table.row_count == before + 1

    _run(scenario())
    rows = _query(project, "SELECT file_id, file_role FROM files WHERE file_role='ui_extra_fasta'")
    assert len(rows) == 1


def test_ingest_conflict_stays_open_without_writing(project: Project) -> None:
    source = project.root / (
        "nested_" + "d" * 60 + "/deeper_" + "d" * 60 + "/conflict.fasta"
    )
    source.parent.mkdir(parents=True)
    source.write_text(">different\nTTTTCCCCAAAAGGGG\n", encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(FilesPanel)
            table = panel.query_one("#files-table", DataTable)
            before = table.row_count

            table.focus()
            await pilot.press("i")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, IngestModal)
            modal.query_one("#ingest-source", Input).value = str(source)
            modal.query_one("#ingest-entity-type", Select).value = "assembly"
            modal.query_one("#ingest-entity-id", Input).value = "ASM_000001"
            modal.query_one("#ingest-role", Input).value = "genome_fasta"
            await pilot.pause()
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _wait_until(
                lambda: "sha256" in _static_text(
                    modal.query_one("#modal-error", Static)
                ),
                "show the ingest conflict",
            )
            await _settled(app)
            assert isinstance(app.screen, IngestModal)
            error_text = _static_text(modal.query_one("#modal-error", Static))
            assert "sha256" in error_text
            assert table.row_count == before
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, IngestModal)

    _run(scenario())
    rows = _query(
        project,
        "SELECT file_id FROM files WHERE entity_id='ASM_000001' AND file_role='genome_fasta'",
    )
    assert len(rows) == 1


def test_qc_modal_single_file_with_progress(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(FilesPanel)
            table = panel.query_one("#files-table", DataTable)
            file_id = panel.files[0]["file_id"]
            before = _query(project, "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='qc'")[0]["n"]

            table.focus()
            table.move_cursor(row=0, animate=False)
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, QcModal)
            assert file_id in _static_text(modal.query_one("#qc-scope", Static))
            assert f"operon qc --file-id {file_id}" in _static_text(
                modal.query_one("#modal-command", Static)
            )
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, QcModal)
            after = _query(project, "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='qc'")[0]["n"]
            assert after == before + 1

    _run(scenario())


def test_qc_modal_cancel_before_confirm(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(FilesPanel)
            table = panel.query_one("#files-table", DataTable)
            table.focus()
            table.move_cursor(row=0, animate=False)
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, QcModal)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, QcModal)

    _run(scenario())


def test_qc_modal_double_dismiss_does_not_crash(project: Project) -> None:
    """Regression: a second close activation (double-clicked Cancel, or escape
    racing the worker callback) must not raise ScreenStackError."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            results: list[object] = []
            app.push_screen(QcModal(project, None, 0), results.append)
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, QcModal)
            modal.dismiss(None)
            modal.dismiss(None)
            await pilot.pause()
            assert not isinstance(app.screen, QcModal)
            assert results == [None]

    _run(scenario())


def test_verify_modal_end_to_end(project: Project) -> None:
    """Verifying a corrupted artifact reports CHECKSUM_FAILED instead of passing."""
    verified: dict[str, str] = {}

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(FilesPanel)
            table = panel.query_one("#files-table", DataTable)
            file_id = panel.files[0]["file_id"]
            verified["file_id"] = file_id

            # Corrupt the archived bytes in place (same size) so the recomputed
            # digest can no longer match the manifest entry.
            artifact = project.root / panel.files[0]["relative_path"]
            original = artifact.read_bytes()
            artifact.write_bytes(b"X" + original[1:])

            table.focus()
            table.move_cursor(row=0, animate=False)
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, VerifyModal)
            assert file_id in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")

            # The failure is reported in the error dialog, not as a success
            # toast. The dialog becomes the current screen before its children
            # are mounted, so wait for the body itself rather than for the
            # screen type alone (the two are far apart on a loaded runner).
            def dialog_texts() -> tuple[str, str] | None:
                screen = app.screen
                if not isinstance(screen, ErrorDialog):
                    return None
                try:
                    return (
                        _static_text(screen.query_one("#modal-title", Label)),
                        _static_text(screen.query_one("#error-dialog-body", Static)),
                    )
                except NoMatches:
                    return None

            def dialog_ready() -> bool:
                texts = dialog_texts()
                return texts is not None and f"{file_id}: CHECKSUM_FAILED" in texts[1]

            await _wait_until(dialog_ready, "verification failure dialog")
            title, body = dialog_texts() or ("", "")
            assert "1 of 1 file(s) failed verification" in title
            assert f"{file_id}: CHECKSUM_FAILED" in body
            await pilot.press("escape")
            await _wait_until(
                lambda: not isinstance(app.screen, VerifyModal), "verify modal closed")

    _run(scenario())
    row = _query(
        project, "SELECT status FROM files WHERE file_id=?", (verified["file_id"],))[0]
    assert row["status"] == "CHECKSUM_FAILED"


def test_ingest_modal_inline_validation(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(FilesPanel)
            panel.query_one("#files-table", DataTable).focus()
            await pilot.press("i")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, IngestModal)
            # Clear any prefill from the selected row for a deterministic form.
            modal.query_one("#ingest-source", Input).value = ""
            modal.query_one("#ingest-entity-id", Input).value = ""
            modal.query_one("#ingest-role", Input).value = ""
            await pilot.pause()
            await _click(pilot, "#confirm")
            await pilot.pause()
            assert isinstance(app.screen, IngestModal)
            assert "source is required" in _static_text(modal.query_one("#modal-error", Static))

            modal.query_one("#ingest-source", Input).value = "/tmp/whatever.fasta"
            await pilot.pause()
            await asyncio.sleep(0.3)  # outlast the Button -active debounce window
            await _click(pilot, "#confirm")
            await pilot.pause()
            assert isinstance(app.screen, IngestModal)
            assert "entity id is required" in _static_text(
                modal.query_one("#modal-error", Static)
            )

            modal.query_one("#ingest-entity-id", Input).value = "ASM_000001"
            modal.query_one("#ingest-role", Input).value = "whatever"
            await pilot.pause()
            await asyncio.sleep(0.3)  # outlast the Button -active debounce window
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            assert isinstance(app.screen, IngestModal)
            assert "does not exist" in _static_text(modal.query_one("#modal-error", Static))
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, IngestModal)

    _run(scenario())


def test_evaluate_modal_selected_entity_scope(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("decisions")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(DecisionsPanel)
            table = panel.query_one("#decisions-table", DataTable)
            index = next(
                i for i, row in enumerate(panel.decisions) if row["entity_id"] == "ASM_000001"
            )
            table.focus()
            table.move_cursor(row=index, animate=False)
            await pilot.pause()
            before = _query(project, "SELECT COUNT(*) AS n FROM decisions")[0]["n"]

            await pilot.press("e")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, EvaluateModal)
            modal.query_one("#evaluate-scope", Select).value = "selected"
            await pilot.pause()
            command = _static_text(modal.query_one("#modal-command", Static))
            assert "--entity-type assembly --entity-id ASM_000001" in command
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, EvaluateModal)
            assert _query(project, "SELECT COUNT(*) AS n FROM decisions")[0]["n"] == before + 1

    _run(scenario())


@pytest.mark.parametrize("phred", ["33", "64", "auto"])
def test_qc_options_match_cli_metrics_and_provenance(project: Project, tmp_path: Path, phred: str) -> None:
    import json

    from operon.cli import main

    source = tmp_path / "reads.fastq"
    source.write_text("@r1\nACGT\n+\nIIII\n@r2\nAAAA\n+\nHHHH\n@r3\nGGGG\n+\nIIII\n")
    record = actions.ingest(project, str(source), "run", "RUN_000001", "option_test", fmt="fastq")
    cli_root = tmp_path / "cli-project"
    shutil.copytree(project.root, cli_root)
    cli_project = Project.find(cli_root)
    result = actions.run_qc(project, file_id=record["file_id"], sample_size=2,
                            phred_offset=phred, rehash=True)
    assert result[0]["ok"]
    assert main(["--project", str(cli_root), "qc", "--file-id", record["file_id"],
                 "--sample-size", "2", "--phred-offset", phred, "--rehash"]) == 0
    sql = ("SELECT metric_name, metric_value, metric_numeric, parameter_set FROM qc_results "
           "WHERE file_id=? ORDER BY metric_name")
    metrics = _query(project, sql, (record["file_id"],))
    assert metrics == _query(cli_project, sql, (record["file_id"],))
    assert any(f":sample_2:phred_{phred}" in row["parameter_set"] for row in metrics)
    run = _query(project, "SELECT execution_details FROM workflow_runs WHERE step='qc' "
                 "ORDER BY rowid DESC LIMIT 1")[0]
    details = json.loads(run["execution_details"])
    assert details["integrity"]["rehash_requested"] is True


@pytest.mark.parametrize("options", [{"sample_size": 0}, {"sample_size": -1}, {"phred_offset": "42"}])
def test_qc_invalid_options_do_not_write(project: Project, options: dict) -> None:
    before = _query(project, "SELECT COUNT(*) AS n FROM workflow_runs")
    with pytest.raises(ValidationError):
        actions.run_qc(project, **options)
    assert _query(project, "SELECT COUNT(*) AS n FROM workflow_runs") == before


def test_qc_modal_options_validation_and_worker_values(project: Project, monkeypatch) -> None:
    from textual.widgets import Checkbox
    captured = []

    def run(project_, *, file_id, progress, **options):
        captured.append((file_id, options))
        progress(1, 1, {"file_id": file_id, "ok": True})
        return [{"file_id": file_id, "ok": True}]

    monkeypatch.setattr(actions, "run_qc", run)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(100, 30)) as pilot:
            await _settled(app)
            modal = QcModal(project, "FIL_000001", 1)
            app.push_screen(modal)
            await pilot.pause()
            assert modal.command_text() == "operon qc --file-id FIL_000001"
            for invalid in ("", "abc", "0", "-1", "1.5"):
                modal.query_one("#qc-sample-size", Input).value = invalid
                modal.confirm()
                assert "positive integer" in _static_text(modal.query_one("#modal-error", Static))
                assert not modal.running
            assert captured == []
            modal.query_one("#qc-sample-size", Input).value = "2"
            modal.query_one("#qc-phred-offset", Select).value = "64"
            modal.query_one("#qc-rehash", Checkbox).value = True
            await pilot.pause()
            assert "--sample-size 2 --phred-offset 64 --rehash" in _static_text(
                modal.query_one("#modal-command", Static))
            # The fixed footer remains reachable in a small terminal.
            assert await pilot.click("#confirm")
            await pilot.pause()
            await _settled(app)
            assert not isinstance(app.screen, QcModal)

    _run(scenario())
    assert captured == [("FIL_000001", {"sample_size": 2, "phred_offset": "64", "rehash": True})]


# ---------------------------------------------------------------------------
# run-external: action, preview, failure reporting and cancellation limits
# ---------------------------------------------------------------------------


def _write_marker_script(tmp_path: Path) -> Path:
    """A tiny external command: writes a marker file, then exits with argv[1].

    ``marker_step.sh <exit code> <output path>`` lets one script cover the
    success and recorded-failure paths of ``run_external``.
    """
    script = tmp_path / "marker_step.sh"
    script.write_text(
        '#!/usr/bin/env bash\necho ok > "$2"\nexit "$1"\n', encoding="utf-8")
    script.chmod(0o755)
    return script


def test_run_external_action_success_failure_and_validation(project: Project,
                                                            tmp_path: Path) -> None:
    import yaml

    script = _write_marker_script(tmp_path)
    out = tmp_path / "step_out.txt"

    result = actions.run_external(
        project, "marker_step", shlex.join([str(script), "0", str(out)]),
        expected_outputs=[str(out)], threads=2, timeout=60,
    )
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert result["run_id"].startswith("WF_")
    # The CLI's pre-run note is captured, not printed over the screen.
    assert f"watch: operon workflow show {result['run_id']} --follow" in result["messages"]
    row = _query(
        project, "SELECT * FROM workflow_runs WHERE run_id=?", (result["run_id"],))[0]
    assert row["step"] == "marker_step"
    assert row["executor"] == "local"
    assert row["threads"] == 2

    # A command that ran but failed is a *recorded* outcome, not an error.
    failed_out = tmp_path / "failed_out.txt"
    failed = actions.run_external(
        project, "marker_step", shlex.join([str(script), "3", str(failed_out)]),
        expected_outputs=[str(failed_out)],
    )
    assert failed["status"] == "failed"
    assert "exit code 3" in failed["error"]
    failed_row = _query(
        project, "SELECT * FROM workflow_runs WHERE run_id=?", (failed["run_id"],))[0]
    assert failed_row["status"] == "failed"
    assert failed_row["exit_code"] == 3

    # Tool provenance mirrors the CLI: an unconfigured name records no version,
    # and a failed detection only warns (into the captured messages).
    unconfigured = actions.run_external(
        project, "marker_step", shlex.join([str(script), "0", str(out)]),
        tool="no_such_tool",
    )
    assert _query(project, "SELECT tool FROM workflow_runs WHERE run_id=?",
                  (unconfigured["run_id"],))[0]["tool"] == "no_such_tool"
    project.tools_config_path.write_text(yaml.safe_dump({
        "version": 1,
        "tools": {"broken": {
            "executable": str(tmp_path / "definitely-missing"),
            "version_args": ["--version"],
            "version_pattern": r"([0-9.]+)",
            "recipes": {},
        }},
    }, sort_keys=False), encoding="utf-8")
    warned = actions.run_external(
        project, "marker_step", shlex.join([str(script), "0", str(out)]),
        tool="broken",
    )
    assert warned["status"] == "completed"
    assert "warning: version detection for 'broken' failed" in warned["messages"]

    # Form-level problems validate like the CLI, before anything is written.
    before = len(_query(project, "SELECT * FROM workflow_runs"))
    with pytest.raises(ValidationError, match="--command must not be empty"):
        actions.run_external(project, "marker_step", "   ")
    with pytest.raises(ValidationError, match="unknown execution backend"):
        actions.run_external(project, "marker_step", "true", backend="kubernetes")
    with pytest.raises(ValidationError, match="declared input does not exist"):
        actions.run_external(project, "marker_step", "true",
                             inputs=[str(tmp_path / "nope.fa")])
    with pytest.raises(ValidationError, match="timeout must be a positive number"):
        actions.run_external(project, "marker_step", "true", timeout=0)
    with pytest.raises(ValidationError, match="threads must be a positive integer"):
        actions.run_external(project, "marker_step", "true", threads=-1)
    assert len(_query(project, "SELECT * FROM workflow_runs")) == before


def test_run_external_modal_preview_run_and_detail(project: Project, tmp_path: Path) -> None:
    script = _write_marker_script(tmp_path)
    out = tmp_path / "ui_out.txt"

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            app.action_switch_screen("runs")
            await pilot.pause()
            await _settled(app)
            await _click(pilot, "#runs-external")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RunExternalModal)
            assert "allocated at submission" in _static_text(
                modal.query_one("#external-run-note", Static))

            # Inline validation: nothing runs until the form is valid.
            modal.confirm()
            assert "step is required" in _static_text(modal.query_one("#modal-error", Static))
            modal.query_one("#external-step", Input).value = "marker_step"
            modal.confirm()
            assert "command is required" in _static_text(modal.query_one("#modal-error", Static))
            modal.query_one("#external-command", Input).value = shlex.join(
                [str(script), "0", str(out)])
            modal.query_one("#external-expected-outputs", Input).value = str(out)
            modal.query_one("#external-threads", Input).value = "-1"
            modal.confirm()
            assert "threads must be a positive integer" in _static_text(
                modal.query_one("#modal-error", Static))
            modal.query_one("#external-threads", Input).value = ""
            modal.query_one("#external-timeout", Input).value = "abc"
            modal.confirm()
            assert "timeout must be a positive number" in _static_text(
                modal.query_one("#modal-error", Static))
            modal.query_one("#external-timeout", Input).value = ""
            await pilot.pause()
            assert modal.command_text().startswith(
                "operon run-external --step marker_step --command ")
            assert f"--expected-output {out}" in modal.command_text()
            assert not modal.running

            await _click(pilot, "#confirm")
            await _wait_until(lambda: not isinstance(app.screen, RunExternalModal),
                              "external modal dismissal")
            await _settled(app)
            # The success callback opens the finished run's record.
            assert isinstance(app.screen, RunDetailScreen)
            detail = _static_text(app.screen.query_one("#run-detail", Static))
            assert "marker_step" in detail
            assert "Execution details" in detail

    _run(scenario())
    row = _query(project, "SELECT * FROM workflow_runs WHERE step='marker_step'")[0]
    assert row["status"] == "completed"
    assert out.read_text(encoding="utf-8").strip() == "ok"


def test_run_external_modal_failed_command_opens_record(project: Project, tmp_path: Path) -> None:
    script = _write_marker_script(tmp_path)
    out = tmp_path / "failed_ui_out.txt"

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            app.action_switch_screen("runs")
            await pilot.pause()
            await _settled(app)
            await _click(pilot, "#runs-external")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RunExternalModal)
            modal.query_one("#external-step", Input).value = "marker_step"
            modal.query_one("#external-command", Input).value = shlex.join(
                [str(script), "3", str(out)])
            await pilot.pause()
            await _click(pilot, "#confirm")
            await _wait_until(lambda: not isinstance(app.screen, RunExternalModal),
                              "failed external dismissal")
            await _settled(app)
            assert isinstance(app.screen, RunDetailScreen)
            assert any(severity == "error" and "failed" in message
                       for severity, message in
                       [(n.severity, n.message) for n in app._notifications])
            detail = _static_text(app.screen.query_one("#run-detail", Static))
            assert "exit code 3" in detail

    _run(scenario())
    row = _query(project, "SELECT * FROM workflow_runs WHERE step='marker_step'")[0]
    assert row["status"] == "failed"


def test_run_external_modal_cannot_be_cancelled_while_running(project: Project,
                                                              monkeypatch) -> None:
    """A running external command has no cooperative cancel: the modal stays."""
    released = threading.Event()
    dismissed: list = []

    def blocking_run(project_arg, step, command_line, **kwargs):
        if not released.wait(10):
            raise AssertionError("test never released the run stub")
        return {"run_id": "WF_STUB", "step": step, "status": "completed",
                "exit_code": 0, "finished_at": None, "error": None,
                "stdout_file": "", "stderr_file": "", "messages": ""}

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            monkeypatch.setattr("operon.tui.screens.run_external.actions.run_external",
                                blocking_run)
            modal = RunExternalModal(project)
            app.push_screen(modal, dismissed.append)
            await pilot.pause()
            modal.query_one("#external-step", Input).value = "marker_step"
            modal.query_one("#external-command", Input).value = "true"
            await pilot.pause()
            modal.confirm()
            await _wait_until(lambda: modal.running, "running external command")
            assert any("running…" in _static_text(
                modal.query_one("#external-status", Static)) for _ in [0])

            # Cancel and escape must not dismiss the modal mid-run.
            modal.on_button_pressed(Button.Pressed(modal.query_one("#cancel", Button)))
            modal.action_cancel()
            await pilot.pause()
            assert app.screen is modal
            assert any(severity == "warning" and "cannot be interrupted" in message
                       for severity, message in
                       [(n.severity, n.message) for n in app._notifications])
            assert dismissed == []
            assert all(widget.disabled for widget in modal.query("Input, Select"))

            released.set()
            await _wait_until(lambda: dismissed, "external modal dismissal")
            await _settled(app)

    try:
        _run(scenario())
    finally:
        released.set()
    assert dismissed[0]["run_id"] == "WF_STUB"
