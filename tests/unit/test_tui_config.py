"""Config screen: structured profile/recipe editing, snapshots, tools-check."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("textual")

import yaml
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Input,
    ListView,
    Select,
    Static,
    TabbedContent,
    TextArea,
)

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ValidationError
from operon.profiles import load_profile
from operon.tools import get_recipe
from operon.tui import actions, data
from operon.tui.app import OperonApp
from operon.tui.screens.config import (
    ConfigPanel,
    HistoryModal,
    ProfileSaveModal,
    RecipeSaveModal,
    SnapshotViewModal,
)


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-config-demo"))


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


def _profile_doc(project: Project, name: str = "assembly_production_v1") -> dict:
    return data.get_profile_document(project, name)


# ---------------------------------------------------------------------------
# Data layer additions (read-only)
# ---------------------------------------------------------------------------


def test_profile_history_and_snapshots(project: Project) -> None:
    rows = data.profile_history(project, "assembly_production_v1")
    assert len(rows) == 1  # recorded by the demo's evaluate_all
    assert rows[0]["version"] == 1
    assert rows[0]["uses"] == 3  # three assembly decisions point at it

    document = data.get_profile_snapshot(
        project, "assembly_production_v1", rows[0]["snapshot_id"]
    )
    assert document["kind"] == "qc"
    assert document["version"] == 1
    assert any(rule["metric"] == "total_length" for rule in document["required"])

    with pytest.raises(ValidationError, match="no snapshot"):
        data.get_profile_snapshot(project, "assembly_production_v1", 99999)
    assert data.profile_history(project, "reads_qc_v1")


def test_recipe_history_and_snapshot(project: Project) -> None:
    assert data.recipe_history(project, "blastn_nt") == []
    recipe_doc = data.get_recipe_document(project, "blastn_nt")
    recipe_doc["document"]["description"] = "edited"
    result = actions.save_recipe(project, recipe_doc["tool"], "blastn_nt", recipe_doc["document"])

    rows = data.recipe_history(project, "blastn_nt")
    assert [row["version"] for row in rows] == [2]
    assert rows[0]["snapshot_id"] == result["snapshot_id"]
    document = data.get_recipe_snapshot(project, "blastn_nt", result["snapshot_id"])
    assert document["recipe"]["description"] == "edited"
    assert document["tool"]["executable"] == "blastn"  # snapshot carries the tool spec
    with pytest.raises(ValidationError, match="no snapshot"):
        data.get_recipe_snapshot(project, "blastn_nt", 99999)


def test_list_helpers(project: Project) -> None:
    profiles = data.list_qc_profiles(project)
    names = [profile["name"] for profile in profiles]
    assert "assembly_production_v1" in names
    # taxonomy_coverage profiles are not qc profiles and must not appear.
    assert "coverage_viridiplantae_v1" not in names

    tools = data.list_tools(project)
    assert {tool["name"] for tool in tools} == {"blastn", "blastp", "hmmsearch", "busco"}

    recipes = data.list_recipes(project)
    by_name = {recipe["name"]: recipe for recipe in recipes}
    assert by_name["blastn_nt"]["tool"] == "blastn"
    assert by_name["blastn_nt"]["entity_type"] == "assembly"


# ---------------------------------------------------------------------------
# actions.save_profile
# ---------------------------------------------------------------------------


def test_save_profile_bumps_and_matches_evaluate_snapshot(project: Project) -> None:
    document = _profile_doc(project)
    rule = next(r for r in document["required"] if r["metric"] == "total_length")
    rule["value"] = 2000
    result = actions.save_profile(project, "assembly_production_v1", document)
    assert result["unchanged"] is False
    assert result["version"] == 2

    loaded = load_profile(project.profiles_dir, "assembly_production_v1", expected_kind="qc")
    assert loaded["version"] == 2
    assert next(r for r in loaded["required"] if r["metric"] == "total_length")["value"] == 2000

    # A CLI evaluate of the same file must map to the very same snapshot row.
    actions.evaluate(
        project, entity_type="assembly", entity_id="ASM_000001",
        profile="assembly_production_v1",
    )
    decision = _query(
        project,
        "SELECT profile_snapshot_id, profile_sha256 FROM decisions "
        "WHERE profile='assembly_production_v1' ORDER BY decision_id DESC LIMIT 1",
    )[0]
    assert decision["profile_snapshot_id"] == result["snapshot_id"]
    assert decision["profile_sha256"] == result["sha256"]
    canonical = json.dumps(loaded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == result["sha256"]


def test_save_profile_preserves_unknown_rule_keys(project: Project) -> None:
    name = "annotation_busco_viridiplantae_odb12_v1"
    document = _profile_doc(project, name)
    document["description"] = "edited in the TUI"
    result = actions.save_profile(project, name, document)
    assert result["version"] == 2

    loaded = load_profile(project.profiles_dir, name, expected_kind="qc")
    rule = loaded["required"][0]
    assert rule["value_by"]["metric"] == "busco_lineage_dataset"
    assert rule["value_by"]["unknown"] == "warning"
    assert rule["source"] == {"qc_stage": "analysis:busco_autolineage"}
    assert rule["unknown_code"] == "BUSCO_LINEAGE_UNCONFIGURED"
    assert loaded["description"] == "edited in the TUI"


def test_save_profile_rejects_invalid_documents(project: Project) -> None:
    path = project.profiles_dir / "assembly_production_v1.yaml"
    original_bytes = path.read_bytes()

    document = _profile_doc(project)
    document["required"][2]["operator"] = "~~"
    with pytest.raises(ValidationError, match="unknown operator"):
        actions.save_profile(project, "assembly_production_v1", document)

    document = _profile_doc(project)
    document["required"][2]["metric"] = "  "
    with pytest.raises(ValidationError, match="metric is required"):
        actions.save_profile(project, "assembly_production_v1", document)

    document = _profile_doc(project)
    document["required"][2]["code"] = ""
    with pytest.raises(ValidationError, match="code is required"):
        actions.save_profile(project, "assembly_production_v1", document)

    document = _profile_doc(project)
    document["applies_to"] = ["assembly", "contig"]
    with pytest.raises(ValidationError, match="unknown entity types"):
        actions.save_profile(project, "assembly_production_v1", document)

    document = _profile_doc(project)
    document["kind"] = "taxonomy_coverage"
    with pytest.raises(ValidationError, match="kind 'qc'"):
        actions.save_profile(project, "assembly_production_v1", document)

    for bad_name in ("../escape", "a/b", "", ".", ".."):
        with pytest.raises(ValidationError, match="invalid profile name"):
            actions.save_profile(project, bad_name, _profile_doc(project))

    assert path.read_bytes() == original_bytes


def test_save_profile_noop_does_not_bump(project: Project) -> None:
    before = _query(project, "SELECT COUNT(*) AS n FROM qc_profiles")[0]["n"]
    result = actions.save_profile(
        project, "assembly_production_v1", _profile_doc(project),
    )
    assert result == {
        "name": "assembly_production_v1", "version": 1, "sha256": None,
        "snapshot_id": None, "unchanged": True,
    }
    assert load_profile(project.profiles_dir, "assembly_production_v1",
                        expected_kind="qc")["version"] == 1
    after = _query(project, "SELECT COUNT(*) AS n FROM qc_profiles")[0]["n"]
    assert after == before


def test_save_profile_new_profile_gets_version_1(project: Project) -> None:
    document = {
        "kind": "qc", "description": "strict assembly gate",
        "applies_to": ["assembly"],
        "required": [
            {"metric": "total_length", "operator": ">=", "value": "5000", "code": "TOO_SHORT"},
        ],
        "warnings": [],
    }
    result = actions.save_profile(project, "assembly_strict_v1", document)
    assert result["version"] == 1
    loaded = load_profile(project.profiles_dir, "assembly_strict_v1", expected_kind="qc")
    # numeric-looking string values are stored as numbers
    assert loaded["required"][0]["value"] == 5000
    assert isinstance(loaded["required"][0]["value"], int)


def test_profile_history_restore_then_save_creates_next_version(project: Project) -> None:
    document = _profile_doc(project)
    next(r for r in document["required"] if r["metric"] == "total_length")["value"] = 2000
    actions.save_profile(project, "assembly_production_v1", document)

    history = data.profile_history(project, "assembly_production_v1")
    assert [row["version"] for row in history] == [1, 2]
    restored = data.get_profile_snapshot(
        project, "assembly_production_v1", history[0]["snapshot_id"]
    )
    result = actions.save_profile(project, "assembly_production_v1", restored)
    assert result["version"] == 3  # restore never overwrites in place
    loaded = load_profile(project.profiles_dir, "assembly_production_v1", expected_kind="qc")
    assert next(r for r in loaded["required"] if r["metric"] == "total_length")["value"] == 1000


# ---------------------------------------------------------------------------
# actions.save_recipe
# ---------------------------------------------------------------------------


def test_save_recipe_roundtrip(project: Project) -> None:
    info = data.get_recipe_document(project, "blastn_nt")
    document = dict(info["document"])
    document["description"] = "edited recipe"
    document["max_hits_per_query"] = 9
    result = actions.save_recipe(project, info["tool"], "blastn_nt", document)
    assert result["unchanged"] is False
    assert result["version"] == 2

    recipe = get_recipe(project, "blastn_nt")
    assert recipe.version == 2
    assert recipe.description == "edited recipe"
    assert recipe.max_hits_per_query == 9
    assert recipe.arguments[1] == "${database}"  # placeholders survive verbatim

    rows = data.recipe_history(project, "blastn_nt")
    assert [row["version"] for row in rows] == [2]
    snapshot = data.get_recipe_snapshot(project, "blastn_nt", rows[0]["snapshot_id"])
    assert snapshot["recipe"]["max_hits_per_query"] == 9


def test_save_recipe_preserves_unknown_keys(project: Project) -> None:
    info = data.get_recipe_document(project, "busco_autolineage")
    document = dict(info["document"])
    document["description"] = "touched"
    actions.save_recipe(project, info["tool"], "busco_autolineage", document)

    raw = get_recipe(project, "busco_autolineage").raw
    assert raw["database_mode"] == "mutable_cache"
    assert raw["result_glob"] == "short_summary.specific.*.json"
    assert raw["output_kind"] == "directory"
    assert raw["version"] == 2


def test_save_recipe_failure_restores_file_bytes(project: Project) -> None:
    path = project.tools_config_path
    original_bytes = path.read_bytes()

    info = data.get_recipe_document(project, "blastn_nt")
    document = dict(info["document"])
    document["parameters"] = {"broken": "not-a-mapping"}
    with pytest.raises(ValidationError, match="rolled back"):
        actions.save_recipe(project, info["tool"], "blastn_nt", document)
    assert path.read_bytes() == original_bytes

    document = dict(info["document"])
    document["input_kind"] = "bogus"
    document["description"] = "changed"
    with pytest.raises(ValidationError, match="rolled back"):
        actions.save_recipe(project, info["tool"], "blastn_nt", document)
    assert path.read_bytes() == original_bytes

    with pytest.raises(ValidationError, match="unknown tool"):
        actions.save_recipe(project, "no_such_tool", "x", {})
    with pytest.raises(ValidationError, match="invalid recipe name"):
        actions.save_recipe(project, "blastn", "../x", {})
    assert path.read_bytes() == original_bytes


def test_save_recipe_noop_does_not_bump(project: Project) -> None:
    info = data.get_recipe_document(project, "blastn_nt")
    result = actions.save_recipe(project, info["tool"], "blastn_nt", dict(info["document"]))
    assert result["unchanged"] is True
    assert result["version"] == 1
    assert data.recipe_history(project, "blastn_nt") == []


def test_save_recipe_normalizes_comments(project: Project) -> None:
    path = project.tools_config_path
    path.write_text(path.read_text(encoding="utf-8") + "\n# hand-written note\n", encoding="utf-8")
    info = data.get_recipe_document(project, "blastn_nt")
    document = dict(info["document"])
    document["description"] = "edited"
    actions.save_recipe(project, info["tool"], "blastn_nt", document)
    assert "hand-written note" not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# actions.check_tools
# ---------------------------------------------------------------------------


def _write_fake_tools_config(project: Project, tmp_path: Path) -> Path:
    fake = tmp_path / "faketool"
    fake.write_text("#!/bin/sh\necho 'faketool 1.2.3'\n", encoding="utf-8")
    fake.chmod(0o755)
    config = {
        "tools": {
            "faketool": {
                "executable": str(fake),
                "run_method": "",
                "version_args": ["--version"],
                "version_pattern": r"faketool\s+([^\s]+)",
                "recipes": {},
            },
            "missingtool": {
                "executable": str(tmp_path / "definitely-not-there"),
                "run_method": "",
                "version_args": ["--version"],
                "version_pattern": r"([0-9.]+)",
                "recipes": {},
            },
        }
    }
    project.tools_config_path.write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    return fake


def test_check_tools_ok_and_missing(project: Project, tmp_path: Path) -> None:
    _write_fake_tools_config(project, tmp_path)
    results = actions.check_tools(project, timeout=30)
    by_name = {entry["name"]: entry for entry in results}
    assert by_name["faketool"]["ok"] is True
    assert by_name["faketool"]["version"] == "1.2.3"
    missing = by_name["missingtool"]
    assert missing["ok"] is False
    assert missing["version"] is None
    assert "cannot launch" in missing["error"]


def test_check_tools_reports_each_row_via_callback(project: Project, tmp_path: Path) -> None:
    _write_fake_tools_config(project, tmp_path)
    seen: list[str] = []
    results = actions.check_tools(project, timeout=30, on_result=lambda e: seen.append(e["name"]))
    assert seen == [entry["name"] for entry in results]


# ---------------------------------------------------------------------------
# Headless UI: profile editing, history, tools-check
# ---------------------------------------------------------------------------


async def _open_config(app, pilot) -> ConfigPanel:
    app.action_switch_screen("config")
    await pilot.pause()
    await _settled(app)
    return app.query_one(ConfigPanel)


def _select_profile(panel: ConfigPanel, name: str) -> None:
    panel._load_profile(name)


def test_config_screen_profile_save_end_to_end(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            list_view = panel.query_one("#profiles-list", ListView)
            assert len(list_view.children) == 5

            _select_profile(panel, "assembly_production_v1")
            await pilot.pause()
            assert panel.current_profile == "assembly_production_v1"
            assert len(panel._rule_rows("required")) == 6
            assert len(panel._rule_rows("warnings")) == 2
            assert not panel.query_one("#profile-save", Button).disabled

            row = next(
                r for r in panel._rule_rows("required")
                if r.query_one(".rule-metric", Input).value == "contig_n50"
            )
            row.query_one(".rule-value", Input).value = "2500"
            await pilot.click("#profile-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ProfileSaveModal)
            assert "version 2" in _static_text(modal.query_one("#modal-command", Static))
            await pilot.click("#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, ProfileSaveModal)
            # Editor reloaded from disk after the save; no duplicated rule rows.
            assert len(panel._rule_rows("required")) == 6

    _run(scenario())
    loaded = load_profile(project.profiles_dir, "assembly_production_v1", expected_kind="qc")
    assert loaded["version"] == 2
    assert next(r for r in loaded["required"] if r["metric"] == "contig_n50")["value"] == 2500
    rows = _query(
        project, "SELECT profile_version FROM qc_profiles WHERE profile_name='assembly_production_v1'"
    )
    assert sorted(row["profile_version"] for row in rows) == [1, 2]


def test_config_screen_save_error_stays_inline(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "reads_qc_v1")
            await pilot.pause()
            panel._rule_rows("required")[1].query_one(".rule-metric", Input).value = ""
            await pilot.click("#profile-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ProfileSaveModal)
            await pilot.click("#confirm")
            await pilot.pause()
            await _settled(app)
            assert isinstance(app.screen, ProfileSaveModal)
            assert "metric is required" in _static_text(modal.query_one("#modal-error", Static))
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())
    assert load_profile(project.profiles_dir, "reads_qc_v1", expected_kind="qc")["version"] == 1


def test_config_screen_history_view_and_restore(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "assembly_production_v1")
            await pilot.pause()
            await pilot.click("#profile-history")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, HistoryModal)
            table = modal.query_one("#history-table", DataTable)
            assert table.row_count == 1

            await pilot.click("#view")
            await pilot.pause()
            view = app.screen
            assert isinstance(view, SnapshotViewModal)
            assert "total_length" in _static_text(view.query_one("#snapshot-view-scroll Static"))
            await pilot.click("#cancel")
            await pilot.pause()
            assert isinstance(app.screen, HistoryModal)

            await pilot.click("#restore")
            await pilot.pause()
            assert not isinstance(app.screen, HistoryModal)
            heading = _static_text(panel.query_one("#profile-heading", Static))
            assert "restored from snapshot" in heading

    _run(scenario())
    # Restore only loads the editor; nothing is written until Save.
    assert load_profile(project.profiles_dir, "assembly_production_v1",
                        expected_kind="qc")["version"] == 1


def test_config_screen_recipe_save_end_to_end(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            panel.query_one("#config-tabs", TabbedContent).active = "tab-tools"
            await pilot.pause()
            recipes_table = panel.query_one("#recipes-table", DataTable)
            assert recipes_table.row_count == 5

            panel._load_recipe("blastn_nt")
            await pilot.pause()
            assert panel.current_recipe == "blastn_nt"
            assert panel.recipe_tool == "blastn"
            assert "${database}" in panel.query_one("#recipe-arguments", TextArea).text
            parser = panel.query_one("#recipe-result-parser", Select)
            assert parser.value == "blast_tabular"
            assert panel.query_one("#recipe-result-columns", Input).value.startswith("qseqid")

            panel.query_one("#recipe-description", Input).value = "edited via TUI"
            panel.query_one("#recipe-max-hits", Input).value = "7"
            panel.query_one("#recipe-editor", VerticalScroll).scroll_end(animate=False)
            await pilot.pause()
            await pilot.pause()
            await pilot.click("#recipe-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RecipeSaveModal)
            assert "version 2" in _static_text(modal.query_one("#modal-command", Static))
            await pilot.click("#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, RecipeSaveModal)

    _run(scenario())
    recipe = get_recipe(project, "blastn_nt")
    assert recipe.version == 2
    assert recipe.description == "edited via TUI"
    assert recipe.max_hits_per_query == 7
    rows = data.recipe_history(project, "blastn_nt")
    assert [row["version"] for row in rows] == [2]


def test_config_screen_tools_check(project: Project, tmp_path: Path) -> None:
    _write_fake_tools_config(project, tmp_path)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            panel.query_one("#config-tabs", TabbedContent).active = "tab-tools"
            await pilot.pause()
            tools_table = panel.query_one("#tools-table", DataTable)
            assert tools_table.row_count == 2

            await pilot.click("#tools-check")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert tools_table.get_cell("faketool", "version").plain == "1.2.3"
            assert tools_table.get_cell("missingtool", "version").plain == "MISSING"
            assert not panel.query_one("#tools-check", Button).disabled
            # Failures opened the error details dialog.
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())
