"""Phase-2 TUI write actions: audited mutations through actions.py and modals."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import Button, DataTable, Input, Select, Static, Tree

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ConflictError, ValidationError
from operon.tui import actions, data
from operon.tui.app import OperonApp
from operon.tui.screens.decisions import CurateModal, DecisionsPanel, EvaluateModal
from operon.tui.screens.entities import EntitiesPanel, LifecycleModal
from operon.tui.screens.files import FilesPanel
from operon.tui.screens.files_ops import IngestModal, QcModal, VerifyModal


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
    while app.workers:
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
            await pilot.click("#confirm")
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

            await pilot.click("#confirm")
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
            await pilot.click("#confirm")
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
            await pilot.click("#confirm")
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
            await pilot.click("#confirm")
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
    source = project.root / "ui_ingest.fasta"
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
            await pilot.click("#confirm")
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
    source = project.root / "conflict.fasta"
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
            await pilot.click("#confirm")
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
            await pilot.click("#confirm")
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


def test_verify_modal_end_to_end(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(FilesPanel)
            table = panel.query_one("#files-table", DataTable)
            file_id = panel.files[0]["file_id"]

            table.focus()
            table.move_cursor(row=0, animate=False)
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, VerifyModal)
            assert file_id in _static_text(modal.query_one("#modal-command", Static))
            await pilot.click("#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, VerifyModal)

    _run(scenario())
    row = _query(project, "SELECT status FROM files ORDER BY file_id LIMIT 1")[0]
    # Demo files were standardized after their initial verification; both are healthy.
    assert row["status"] in {"CHECKSUM_VERIFIED", "STANDARDIZED"}


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
            await pilot.click("#confirm")
            await pilot.pause()
            assert isinstance(app.screen, IngestModal)
            assert "source is required" in _static_text(modal.query_one("#modal-error", Static))

            modal.query_one("#ingest-source", Input).value = "/tmp/whatever.fasta"
            await pilot.pause()
            await asyncio.sleep(0.3)  # outlast the Button -active debounce window
            await pilot.click("#confirm")
            await pilot.pause()
            assert isinstance(app.screen, IngestModal)
            assert "entity id is required" in _static_text(
                modal.query_one("#modal-error", Static)
            )

            modal.query_one("#ingest-entity-id", Input).value = "ASM_000001"
            modal.query_one("#ingest-role", Input).value = "whatever"
            await pilot.pause()
            await asyncio.sleep(0.3)  # outlast the Button -active debounce window
            await pilot.click("#confirm")
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
            await pilot.click("#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, EvaluateModal)
            assert _query(project, "SELECT COUNT(*) AS n FROM decisions")[0]["n"] == before + 1

    _run(scenario())
