"""Config screen: structured profile/recipe editing, snapshots, tools-check."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("textual")

import yaml
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
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
from operon.tui.screens.common import ErrorDialog
from operon.tui.screens.config import (
    ConfigPanel,
    HistoryModal,
    NewProfileModal,
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
    # The splash screen blocks key bindings until the startup worker finishes;
    # that worker is scheduled via call_after_refresh, so the worker set can
    # be momentarily empty before it starts — gate on _starting as well.
    while app.workers or getattr(app, "_starting", False):
        if loop.time() > deadline:
            states = [worker.state.name for worker in app.workers]
            raise TimeoutError(f"workers did not finish within {timeout}s: {states}")
        await asyncio.sleep(0.05)


async def _wait_until(
        predicate, description: str, timeout: float = SETTLE_TIMEOUT,
) -> None:
    """Wait for a real UI condition instead of a fixed number of pause() cycles."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not reach {description} within {timeout}s")
        await asyncio.sleep(0.02)


def _profile_doc(project: Project, name: str = "assembly_production_v1") -> dict:
    return data.get_profile_document(project, name)


async def _click(pilot, selector: str) -> None:
    """Click a widget, failing loudly when the click does not land on it.

    ``Pilot.click`` silently returns False when the target is clipped or
    obscured (e.g. a modal button pushed out of the box), which otherwise
    surfaces much later as a confusing timeout.  Scroll the target into
    view first so buttons at the bottom of a scrollable form are clickable.
    """
    pilot.app.screen.query_one(selector).scroll_visible(animate=False)
    await pilot.pause()
    assert await pilot.click(selector), f"click did not land on {selector}"


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
    assert {tool["name"] for tool in tools} == {"blastn", "blastp", "hmmsearch", "busco", "rpsblast"}

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


@pytest.mark.parametrize("kind", ["profile", "new_profile", "recipe"])
@pytest.mark.parametrize("failure", ["before_insert", "after_insert", "interrupt"])
def test_config_snapshot_failure_restores_bytes_and_database(project, monkeypatch, kind, failure):
    if kind == "recipe":
        path = project.tools_config_path
        info = data.get_recipe_document(project, "blastn_nt")
        document = info["document"]
        save = lambda: actions.save_recipe(project, info["tool"], "blastn_nt", document)
        method, table = "record_recipe", "recipe_snapshots"
    else:
        name = "new_profile" if kind == "new_profile" else "assembly_production_v1"
        path = project.profiles_dir / f"{name}.yaml"
        document = _profile_doc(project)
        save = lambda: actions.save_profile(project, name, document)
        method, table = "record_profile", "qc_profiles"
    document["description"] = "must roll back"
    if path.exists():
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    original = path.read_bytes() if path.exists() else None
    before = _query(project, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
    record = getattr(Database, method)

    def fail(db, *args, **kwargs):
        if failure != "before_insert":
            record(db, *args, **kwargs)
        if failure == "interrupt":
            raise KeyboardInterrupt()
        raise sqlite3.OperationalError("snapshot storage failed")

    monkeypatch.setattr(Database, method, fail)
    expected = KeyboardInterrupt if failure == "interrupt" else ValidationError
    with pytest.raises(expected):
        save()
    assert (path.read_bytes() if path.exists() else None) == original
    assert _query(project, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == before


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
    await _settled(app)
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
            await _click(pilot, "#profile-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ProfileSaveModal)
            assert "version 2" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
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


def test_profile_editor_scrolls_to_all_rules(project: Project) -> None:
    """The rules editor must scroll: rule containers use height 1fr by default
    (plain Vertical), which clipped the rules to a fixed non-scrolling window."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "assembly_production_v1")
            await pilot.pause()
            editor = panel.query_one("#profile-editor", VerticalScroll)
            assert editor.virtual_size.height > editor.scrollable_content_region.height
            assert editor.max_scroll_y > 0
            editor.scroll_end(animate=False)
            await pilot.pause()
            last_rule = panel._rule_rows("warnings")[-1]
            viewport = editor.scrollable_content_region
            assert (
                viewport.y <= last_rule.region.y
                and last_rule.region.bottom <= viewport.bottom + editor.scroll_offset.y
            )

    _run(scenario())


def test_config_screen_save_error_stays_inline(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "reads_qc_v1")
            await pilot.pause()
            panel._rule_rows("required")[1].query_one(".rule-metric", Input).value = ""
            await _click(pilot, "#profile-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ProfileSaveModal)
            await _click(pilot, "#confirm")
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
            await _click(pilot, "#profile-history")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, HistoryModal)
            table = modal.query_one("#history-table", DataTable)
            assert table.row_count == 1

            await _click(pilot, "#view")
            await pilot.pause()
            view = app.screen
            assert isinstance(view, SnapshotViewModal)
            assert "total_length" in _static_text(view.query_one("#snapshot-view-scroll Static"))
            await _click(pilot, "#cancel")
            await pilot.pause()
            assert isinstance(app.screen, HistoryModal)

            await _click(pilot, "#restore")
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
            assert recipes_table.row_count == 6

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
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RecipeSaveModal)
            assert "version 2" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
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


# ---------------------------------------------------------------------------
# Headless UI: remaining editor, history, and tools-check paths
# ---------------------------------------------------------------------------


def _notifications(app) -> list[tuple[str, str]]:
    """(severity, message) of every notification raised on the app so far.

    Notification text is the user-visible contract these tests assert; Textual
    keeps the raised notifications on the app instance.
    """
    return [(notification.severity, notification.message) for notification in app._notifications]


async def _open_tools_tab(panel: ConfigPanel, pilot) -> None:
    panel.query_one("#config-tabs", TabbedContent).active = "tab-tools"
    await pilot.pause()


def _write_profile_probe(project: Project, name: str, text: str) -> Path:
    path = project.profiles_dir / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _add_recipe_probe(project: Project) -> None:
    """Install a hand-written recipe with unmodeled keys and parameter specs."""
    path = project.tools_config_path
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["tools"]["blastn"]["recipes"]["custom_probe"] = {
        "description": "hand-written probe recipe",
        "entity_type": "project",  # not one of the form's entity-type options
        "file_role": "genome_fasta",
        "format": "fasta",
        "parameters": {
            "alpha": {"description": "first", "required": True},
            "beta": {"default": 3},
            "gamma": None,
        },
        "arguments": ["-query", "${input}"],
    }
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _write_only_working_tool(project: Project, tmp_path: Path) -> None:
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
            }
        }
    }
    project.tools_config_path.write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )


# --- profile editor: rules, extras, and validation --------------------------


def test_config_screen_add_and_remove_rule_rows(project: Project, monkeypatch) -> None:
    """✕ removes a rule row; both "add rule" buttons append modelled defaults."""
    removed_controls: list[object] = []
    original_handler = ConfigPanel.on_rule_row_remove_requested

    def recording_handler(self, event) -> None:
        removed_controls.append(event.control)
        original_handler(self, event)

    monkeypatch.setattr(ConfigPanel, "on_rule_row_remove_requested", recording_handler)
    removed: dict[str, str] = {}

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "assembly_production_v1")
            await pilot.pause()
            first_rule = panel._rule_rows("required")[0]
            removed["metric"] = first_rule.query_one(".rule-metric", Input).value
            await _click(pilot, ".rule-remove")
            await pilot.pause()
            assert removed_controls == [first_rule]  # message.control is the row
            assert len(panel._rule_rows("required")) == 5
            assert all(
                row.query_one(".rule-metric", Input).value != removed["metric"]
                for row in panel._rule_rows("required")
            )

            await _click(pilot, "#profile-add-required")
            await pilot.pause()
            assert len(panel._rule_rows("required")) == 6
            added = panel._rule_rows("required")[-1]
            assert added.query_one(".rule-operator", Select).value == ">="
            added.query_one(".rule-metric", Input).value = "gene_count"
            added.query_one(".rule-value", Input).value = "7"
            added.query_one(".rule-code", Input).value = "TOO_FEW_GENES"

            await _click(pilot, "#profile-add-warnings")
            await pilot.pause()
            assert len(panel._rule_rows("warnings")) == 3
            warn = panel._rule_rows("warnings")[-1]
            assert warn.query_one(".rule-operator", Select).value == ">"
            warn.query_one(".rule-metric", Input).value = "n_percent"
            warn.query_one(".rule-value", Input).value = "2"
            warn.query_one(".rule-code", Input).value = "HIGH_N"

            await _click(pilot, "#profile-save")
            await pilot.pause()
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, ProfileSaveModal)

    _run(scenario())
    loaded = load_profile(project.profiles_dir, "assembly_production_v1", expected_kind="qc")
    assert loaded["version"] == 2
    metrics = [rule["metric"] for rule in loaded["required"]]
    assert removed["metric"] not in metrics
    assert metrics[-1] == "gene_count"
    assert loaded["required"][-1]["value"] == 7  # numeric-looking text stored as a number
    assert loaded["warnings"][-1] == {
        "metric": "n_percent", "operator": ">", "value": 2, "code": "HIGH_N",
    }


PROBE_EXTRAS_PROFILE = """\
kind: qc
version: 1
description: probe profile keeping unmodeled keys
applies_to:
- assembly
review_note: keep-me-verbatim
required:
- metric: total_length
  operator: '>='
  value: 500
  code: TOO_SHORT
  value_by:
    metric: assembly_scale
    unknown: warning
  source:
    qc_stage: qc:assembly
  unknown_code: LENGTH_UNKNOWN
- metric: parseable
  operator: exists
  code: FORMAT_INVALID
warnings: []
"""


def test_config_screen_profile_unmodeled_keys_survive_save(project: Project) -> None:
    path = _write_profile_probe(project, "probe_extras_v1", PROBE_EXTRAS_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "probe_extras_v1")
            await pilot.pause()
            assert panel.current_profile == "probe_extras_v1"
            assert len(panel._rule_rows("required")) == 2
            assert "preserved as-is: review_note" in _static_text(
                panel.query_one("#profile-extras-note", Static)
            )
            extras_note = _static_text(
                panel._rule_rows("required")[0].query_one(".rule-extras", Static)
            )
            assert "preserved as-is: value_by, source, unknown_code" in extras_note
            # An `exists` rule carries no value; the form must not invent one.
            assert panel._rule_rows("required")[1].query_one(".rule-value", Input).value == ""

            panel.query_one("#profile-description", Input).value = "edited with extras"
            await _click(pilot, "#profile-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ProfileSaveModal)
            assert "version 2" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, ProfileSaveModal)
            assert any("saved probe_extras_v1 version 2" in message
                       for _, message in _notifications(app))

    _run(scenario())
    loaded = load_profile(project.profiles_dir, "probe_extras_v1", expected_kind="qc")
    assert loaded["version"] == 2
    assert loaded["description"] == "edited with extras"
    assert loaded["review_note"] == "keep-me-verbatim"
    first, second = loaded["required"]
    assert first["value_by"] == {"metric": "assembly_scale", "unknown": "warning"}
    assert first["source"] == {"qc_stage": "qc:assembly"}
    assert first["unknown_code"] == "LENGTH_UNKNOWN"
    assert "value" not in second
    assert second["operator"] == "exists"
    assert "review_note: keep-me-verbatim" in path.read_text(encoding="utf-8")
    rows = _query(
        project,
        "SELECT profile_version FROM qc_profiles WHERE profile_name='probe_extras_v1'",
    )
    assert [row["profile_version"] for row in rows] == [2]


PROBE_INCOMPLETE_PROFILE = """\
kind: qc
version: 1
description: probe profile with rule keys the form cannot model
applies_to:
- assembly
review_note: keep-me-verbatim
required:
- metric: total_length
  operator: '@@'
  value: 500
  code: TOO_SHORT
  source:
    qc_stage: qc:assembly
- operator: '>='
  value: 10
  code: NO_METRIC
- metric: contig_n50
  value: 200
  code: NO_OPERATOR
- metric: gc_percent
  operator: '>='
  code: NO_VALUE
- metric: n_percent
  operator: '>='
  value: 5
- not-a-mapping-rule
warnings: []
"""


def test_config_screen_profile_rule_fallbacks_and_unknown_operator(project: Project) -> None:
    """Rules missing modelled keys keep their shape; unknown operators and
    unparseable rules surface as a validation error without touching the file."""
    path = _write_profile_probe(project, "probe_incomplete_v1", PROBE_INCOMPLETE_PROFILE)
    original_bytes = path.read_bytes()

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "probe_incomplete_v1")
            await pilot.pause()
            rows = panel._rule_rows("required")
            assert len(rows) == 5  # the non-mapping rule is skipped
            # An operator the form does not offer stays selected and preserved.
            assert rows[0].query_one(".rule-operator", Select).value == "@@"
            assert "preserved as-is: source" in _static_text(
                rows[0].query_one(".rule-extras", Static)
            )
            assert "preserved as-is: review_note" in _static_text(
                panel.query_one("#profile-extras-note", Static)
            )
            # Give the value-less rule a value the form did not model, and clear
            # the value of a rule that has one (it must not be re-invented).
            rows[0].query_one(".rule-value", Input).value = ""
            rows[3].query_one(".rule-value", Input).value = "42"

            await _click(pilot, "#profile-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ProfileSaveModal)
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            assert isinstance(app.screen, ProfileSaveModal)
            assert "unknown operator '@@'" in _static_text(
                modal.query_one("#modal-error", Static)
            )
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())
    assert path.read_bytes() == original_bytes
    assert _query(
        project, "SELECT COUNT(*) AS n FROM qc_profiles WHERE profile_name='probe_incomplete_v1'"
    )[0]["n"] == 0


def test_config_screen_unchanged_profile_save_notifies(project: Project) -> None:
    before = _query(project, "SELECT COUNT(*) AS n FROM qc_profiles")[0]["n"]

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "assembly_production_v1")
            await pilot.pause()
            await _click(pilot, "#profile-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ProfileSaveModal)
            assert "version 2" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, ProfileSaveModal)
            assert any(
                "assembly_production_v1: unchanged — version 1 kept" in message
                for _, message in _notifications(app)
            )

    _run(scenario())
    assert load_profile(
        project.profiles_dir, "assembly_production_v1", expected_kind="qc"
    )["version"] == 1
    assert _query(project, "SELECT COUNT(*) AS n FROM qc_profiles")[0]["n"] == before


# --- new profile modal ------------------------------------------------------


def test_config_screen_new_profile_cancel_and_invalid_name(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _click(pilot, "#profile-new")
            await pilot.pause()
            assert isinstance(app.screen, NewProfileModal)
            await _click(pilot, "#cancel")
            await pilot.pause()
            assert not isinstance(app.screen, NewProfileModal)
            assert panel.current_profile is None
            assert panel.query_one("#profile-save", Button).disabled

            await _click(pilot, "#profile-new")
            await pilot.pause()
            modal = app.screen
            modal.query_one("#new-profile-name", Input).value = "../escape"
            await _click(pilot, "#confirm")
            await pilot.pause()
            assert isinstance(app.screen, NewProfileModal)
            assert "invalid profile name" in _static_text(
                modal.query_one("#history-error", Static)
            )
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, NewProfileModal)

    _run(scenario())
    assert sorted(path.name for path in project.profiles_dir.glob("*.yaml")) == [
        "annotation_busco_viridiplantae_odb12_v1.yaml",
        "annotation_release_v1.yaml",
        "assembly_production_v1.yaml",
        "coverage_viridiplantae_v1.yaml",
        "file_integrity_v1.yaml",
        "reads_qc_v1.yaml",
    ]


def test_config_screen_new_profile_is_created_as_version_1(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _click(pilot, "#profile-new")
            await pilot.pause()
            app.screen.query_one("#new-profile-name", Input).value = "assembly_strict_v1"
            await _click(pilot, "#confirm")
            await pilot.pause()
            heading = _static_text(panel.query_one("#profile-heading", Static))
            assert "assembly_strict_v1" in heading and "new profile (not saved yet)" in heading
            assert "version 1" in _static_text(panel.query_one("#profile-version-note", Static))
            # new profiles start as an assembly gate with empty rule sections
            assert panel.query_one("#profile-applies-assembly", Checkbox).value is True
            assert panel._rule_rows("required") == []
            assert panel._rule_rows("warnings") == []

            panel.query_one("#profile-description", Input).value = "strict assembly gate"
            await _click(pilot, "#profile-add-required")
            await pilot.pause()
            row = panel._rule_rows("required")[0]
            row.query_one(".rule-metric", Input).value = "total_length"
            row.query_one(".rule-value", Input).value = "5000"
            row.query_one(".rule-code", Input).value = "TOO_SHORT"

            await _click(pilot, "#profile-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ProfileSaveModal)
            assert "version 1" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, ProfileSaveModal)
            assert any("saved assembly_strict_v1 version 1" in message
                       for _, message in _notifications(app))

    _run(scenario())
    loaded = load_profile(project.profiles_dir, "assembly_strict_v1", expected_kind="qc")
    assert loaded["version"] == 1
    assert loaded["applies_to"] == ["assembly"]
    assert loaded["required"] == [
        {"metric": "total_length", "operator": ">=", "value": 5000, "code": "TOO_SHORT"}
    ]
    assert loaded["warnings"] == []
    rows = _query(
        project,
        "SELECT profile_version FROM qc_profiles WHERE profile_name='assembly_strict_v1'",
    )
    assert [row["profile_version"] for row in rows] == [1]


def test_config_screen_new_profile_with_existing_name_opens_it(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _click(pilot, "#profile-new")
            await pilot.pause()
            app.screen.query_one("#new-profile-name", Input).value = "reads_qc_v1"
            await _click(pilot, "#confirm")
            await pilot.pause()
            assert not isinstance(app.screen, NewProfileModal)
            assert panel.current_profile == "reads_qc_v1"
            assert "reads_qc_v1" in _static_text(panel.query_one("#profile-heading", Static))
            assert panel.query_one("#profile-description", Input).value.startswith("Raw read QC")
            assert len(panel._rule_rows("required")) == 4
            assert any(
                "profile 'reads_qc_v1' already exists — opening it instead" in message
                for _, message in _notifications(app)
            )

    _run(scenario())


# --- history modal ----------------------------------------------------------


def test_config_screen_history_without_snapshots_and_cancel(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _click(pilot, "#profile-new")
            await pilot.pause()
            app.screen.query_one("#new-profile-name", Input).value = "unsaved_probe_v1"
            await _click(pilot, "#confirm")
            await pilot.pause()

            await _click(pilot, "#profile-history")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, HistoryModal)
            assert modal.query_one("#history-table", DataTable).row_count == 0
            await _click(pilot, "#restore")
            await pilot.pause()
            assert isinstance(app.screen, HistoryModal)
            assert "select a snapshot row first" in _static_text(
                modal.query_one("#history-error", Static)
            )
            await _click(pilot, "#cancel")
            await pilot.pause()
            assert not isinstance(app.screen, HistoryModal)

    _run(scenario())


def test_config_screen_history_snapshot_read_error_is_inline(project: Project, monkeypatch) -> None:
    def broken_snapshot(project_: Project, name: str, snapshot_id: int) -> dict:
        raise ValidationError(f"snapshot {snapshot_id} is unreadable")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "assembly_production_v1")
            await pilot.pause()
            await _click(pilot, "#profile-history")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, HistoryModal)
            assert modal.query_one("#history-table", DataTable).row_count == 1

            monkeypatch.setattr(data, "get_profile_snapshot", broken_snapshot)
            await _click(pilot, "#view")
            await pilot.pause()
            assert isinstance(app.screen, HistoryModal)
            assert "is unreadable" in _static_text(modal.query_one("#history-error", Static))
            await _click(pilot, "#restore")
            await pilot.pause()
            assert isinstance(app.screen, HistoryModal)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HistoryModal)

    _run(scenario())


# --- recipe editor ----------------------------------------------------------


def test_config_screen_recipe_unmodeled_keys_and_parameters_roundtrip(project: Project) -> None:
    _add_recipe_probe(project)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            assert panel.query_one("#recipes-table", DataTable).row_count == 7

            # A result parser the form does not offer stays selected.
            panel._load_recipe("rpsblast_cdd")
            await pilot.pause()
            assert panel.query_one("#recipe-result-parser", Select).value == "rpsbproc_tabular"

            panel._load_recipe("custom_probe")
            await pilot.pause()
            assert panel.current_recipe == "custom_probe"
            assert panel.query_one("#recipe-entity-type", Select).value == "project"
            note = _static_text(panel.query_one("#recipe-parameters-note", Static))
            assert "alpha: description, required" in note
            assert "beta" not in note
            assert panel.query_one("#recipe-parameters", TextArea).text == "alpha=\nbeta=3\ngamma="
            assert panel.query_one("#recipe-result-parser", Select).value == "none"

            panel.query_one("#recipe-arguments", TextArea).text = "-query\n${input}\n-outfmt\n6"
            panel.query_one("#recipe-parameters", TextArea).text = (
                "alpha=fabales\n"
                "\n"
                "beta=9\n"
                "gamma=\n"
                "new_param=5\n"
                "=5\n"
            )
            panel.query_one("#recipe-editor", VerticalScroll).scroll_end(animate=False)
            await pilot.pause()
            await pilot.pause()
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RecipeSaveModal)
            assert "version 2" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, RecipeSaveModal)

    _run(scenario())
    recipe = get_recipe(project, "custom_probe")
    assert recipe.version == 2
    assert recipe.entity_type == "project"  # preserved, not coerced to a known entity
    assert recipe.arguments == ["-query", "${input}", "-outfmt", "6"]
    assert recipe.raw["result_parser"] == "none"  # added by the editor
    parameters = recipe.raw["parameters"]
    assert parameters["alpha"] == {
        "description": "first", "required": True, "default": "fabales",
    }
    assert parameters["beta"] == {"default": 9}
    assert parameters["gamma"] == {}
    assert parameters["new_param"] == {"default": 5}
    assert "=5" not in parameters
    # sibling recipes and unmodeled recipe keys are untouched
    assert get_recipe(project, "blastn_nt").arguments[1] == "${database}"
    history = data.recipe_history(project, "custom_probe")
    assert [row["version"] for row in history] == [2]
    snapshot = data.get_recipe_snapshot(project, "custom_probe", history[0]["snapshot_id"])
    assert snapshot["recipe"]["entity_type"] == "project"
    assert snapshot["tool"]["executable"] == "blastn"


def test_config_screen_recipe_unchanged_max_hits_and_cancel(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)

            # Cancelling the confirmation writes nothing.
            panel._load_recipe("blastn_nt")
            await pilot.pause()
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            assert isinstance(app.screen, RecipeSaveModal)
            await _click(pilot, "#cancel")
            await pilot.pause()
            assert not isinstance(app.screen, RecipeSaveModal)
            assert data.recipe_history(project, "blastn_nt") == []

            # Clearing max_hits keeps the file's value: the save is a no-op.
            panel.query_one("#recipe-max-hits", Input).value = ""
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RecipeSaveModal)
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, RecipeSaveModal)
            assert any("blastn_nt: unchanged — version 1 kept" in message
                       for _, message in _notifications(app))

            # A recipe without max_hits_per_query also saves as a no-op.
            panel._load_recipe("busco_autolineage")
            await pilot.pause()
            assert panel.query_one("#recipe-max-hits", Input).value == ""
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, RecipeSaveModal)
            assert any("busco_autolineage: unchanged — version 1 kept" in message
                       for _, message in _notifications(app))

    _run(scenario())
    assert get_recipe(project, "blastn_nt").version == 1
    assert get_recipe(project, "blastn_nt").max_hits_per_query == 5
    assert data.recipe_history(project, "blastn_nt") == []
    assert data.recipe_history(project, "busco_autolineage") == []


def test_config_screen_recipe_invalid_max_hits_stays_inline(project: Project) -> None:
    original_bytes = project.tools_config_path.read_bytes()

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            panel._load_recipe("blastn_nt")
            await pilot.pause()
            panel.query_one("#recipe-max-hits", Input).value = "many"
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RecipeSaveModal)
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            assert isinstance(app.screen, RecipeSaveModal)
            assert "rolled back" in _static_text(modal.query_one("#modal-error", Static))
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())
    assert project.tools_config_path.read_bytes() == original_bytes
    assert data.recipe_history(project, "blastn_nt") == []


def test_config_screen_recipe_history_view_and_restore(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)

            panel._load_recipe("blastn_nt")
            await pilot.pause()
            panel.query_one("#recipe-description", Input).value = "saved via TUI"
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()

            panel._load_recipe("blastn_nt")
            await pilot.pause()
            panel.query_one("#recipe-description", Input).value = "second edit"
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()

            panel._load_recipe("blastn_nt")
            await pilot.pause()
            panel.query_one("#recipe-description", Input).value = "unsaved scratch edit"
            await _click(pilot, "#recipe-history")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, HistoryModal)
            assert modal.query_one("#history-table", DataTable).row_count == 2

            await _click(pilot, "#view")
            await pilot.pause()
            view = app.screen
            assert isinstance(view, SnapshotViewModal)
            assert "saved via TUI" in _static_text(view.query_one("#snapshot-view-scroll Static"))
            await _click(pilot, "#cancel")
            await pilot.pause()
            assert isinstance(app.screen, HistoryModal)

            await _click(pilot, "#restore")
            await pilot.pause()
            assert not isinstance(app.screen, HistoryModal)
            # Restoring loads the first snapshot into the editor as the next version.
            assert panel.query_one("#recipe-description", Input).value == "saved via TUI"
            heading = _static_text(panel.query_one("#recipe-heading", Static))
            assert "restored from snapshot" in heading

            await _click(pilot, "#recipe-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RecipeSaveModal)
            assert "version 4" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()

    _run(scenario())
    recipe = get_recipe(project, "blastn_nt")
    assert recipe.version == 4
    assert recipe.description == "saved via TUI"
    assert [row["version"] for row in data.recipe_history(project, "blastn_nt")] == [2, 3, 4]


# --- vanished files and load failures ---------------------------------------


def test_config_screen_profile_deleted_under_the_ui(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "assembly_production_v1")
            await pilot.pause()
            (project.profiles_dir / "assembly_production_v1.yaml").unlink()

            # Selecting the vanished profile in the list reports it instead of crashing.
            list_view = panel.query_one("#profiles-list", ListView)
            index = next(
                i for i, profile in enumerate(panel.profiles)
                if profile["name"] == "assembly_production_v1"
            )
            label = list_view.children[index].query_one(Label)
            assert await pilot.click(label)
            await pilot.pause()
            assert panel.current_profile == "assembly_production_v1"  # unchanged: load failed
            assert any("not found" in message for _, message in _notifications(app))

            # Saving recreates it from the editor content as version 1 again.
            await _click(pilot, "#profile-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ProfileSaveModal)
            assert "version 1" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, ProfileSaveModal)

    _run(scenario())
    assert load_profile(
        project.profiles_dir, "assembly_production_v1", expected_kind="qc"
    )["version"] == 1


def test_config_screen_recipe_deleted_under_the_ui(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            panel._load_recipe("busco_autolineage")
            await pilot.pause()

            config = yaml.safe_load(project.tools_config_path.read_text(encoding="utf-8"))
            del config["tools"]["busco"]["recipes"]["busco_autolineage"]
            project.tools_config_path.write_text(
                yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
            )

            table = panel.query_one("#recipes-table", DataTable)
            table.focus()
            table.move_cursor(row=table.get_row_index("busco_autolineage"), animate=False)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert panel.current_recipe == "busco_autolineage"  # unchanged: load failed
            assert any("unknown analysis" in message for _, message in _notifications(app))

            await _click(pilot, "#recipe-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RecipeSaveModal)
            assert "version 1" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, RecipeSaveModal)

    _run(scenario())
    recipe = get_recipe(project, "busco_autolineage")
    assert recipe.version == 1
    assert recipe.raw["database_mode"] == "mutable_cache"


def test_config_screen_list_and_table_selection_loads_editors(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)

            list_view = panel.query_one("#profiles-list", ListView)
            index = next(
                i for i, profile in enumerate(panel.profiles) if profile["name"] == "reads_qc_v1"
            )
            assert await pilot.click(list_view.children[index].query_one(Label))
            await pilot.pause()
            assert panel.current_profile == "reads_qc_v1"
            assert len(panel._rule_rows("required")) == 4
            assert not panel.query_one("#profile-save", Button).disabled

            await _open_tools_tab(panel, pilot)
            tools_table = panel.query_one("#tools-table", DataTable)
            tools_table.focus()
            tools_table.move_cursor(row=0, animate=False)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            # Selecting a tool row must not load a recipe.
            assert _static_text(panel.query_one("#recipe-heading", Static)) == "select a recipe"
            assert panel.current_recipe is None

            recipes_table = panel.query_one("#recipes-table", DataTable)
            recipes_table.focus()
            recipes_table.move_cursor(row=recipes_table.get_row_index("blastp_nr"), animate=False)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert panel.current_recipe == "blastp_nr"
            assert panel.recipe_tool == "blastp"
            heading = _static_text(panel.query_one("#recipe-heading", Static))
            assert heading == "blastp.blastp_nr"

    _run(scenario())


def test_config_screen_load_failure_notifies(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _open_config(app, pilot)
            project.tools_config_path.write_text("tools: [unclosed\n", encoding="utf-8")
            app.action_refresh()
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert any("config load failed" in message for _, message in _notifications(app))

    _run(scenario())


# --- tools check ------------------------------------------------------------


def _tool_version_cell(table: DataTable, tool: str) -> str | None:
    """The version cell text, or None while the worker is rebuilding the table."""
    try:
        return table.get_cell(tool, "version").plain
    except Exception:  # the row is briefly absent while the table reloads
        return None


@pytest.mark.parametrize("all_ok", [True, False], ids=["all-ok", "missing-tool"])
def test_config_screen_tools_check(project: Project, tmp_path: Path, all_ok: bool) -> None:
    if all_ok:
        _write_only_working_tool(project, tmp_path)
    else:
        _write_fake_tools_config(project, tmp_path)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            tools_table = panel.query_one("#tools-table", DataTable)
            assert tools_table.row_count == (1 if all_ok else 2)

            await _click(pilot, "#tools-check")
            # Wait for the actual per-row outcome instead of a fixed number of
            # pause() cycles: the worker updates the table row by row.
            await _wait_until(
                lambda: _tool_version_cell(tools_table, "faketool") == "1.2.3"
                and _tool_version_cell(tools_table, "missingtool")
                == (None if all_ok else "MISSING"),
                "tools-check row updates",
            )
            await _settled(app)
            await pilot.pause()
            assert not panel.query_one("#tools-check", Button).disabled
            if all_ok:
                assert ("information", "tools-check: 1/1 tool(s) detected") in _notifications(app)
                # No failures: the details dialog stays closed.
                assert not isinstance(app.screen, ErrorDialog)
            else:
                assert _tool_version_cell(tools_table, "missingtool") == "MISSING"
                # Failures open the error details dialog.
                await _wait_until(
                    lambda: isinstance(app.screen, ErrorDialog), "tools-check error dialog")
                await pilot.press("escape")
                await _wait_until(
                    lambda: not isinstance(app.screen, ErrorDialog), "error dialog closed")

    _run(scenario())


def test_config_screen_tools_check_worker_error(project: Project, monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise RuntimeError("probe exploded")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            monkeypatch.setattr(actions, "check_tools", explode)

            await _click(pilot, "#tools-check")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert any(
                severity == "error" and "tools-check failed: probe exploded" in message
                for severity, message in _notifications(app)
            )
            assert not panel.query_one("#tools-check", Button).disabled
            assert not isinstance(app.screen, ErrorDialog)

    _run(scenario())
