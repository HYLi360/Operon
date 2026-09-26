"""Config screen: structured profile/recipe editing, snapshots, tools-check."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

import yaml
from rich.text import Text
from textual.containers import VerticalScroll
from textual.pilot import OutOfBounds
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
from operon.tui.screens.common import ErrorDialog, FittingSelect, MountTracked
from operon.tui.screens.config import (
    ENVIRONMENT_POLICIES,
    CommandRow,
    ConfigPanel,
    HistoryModal,
    NewProfileModal,
    ProfileSaveModal,
    RecipeSaveModal,
    RuleRow,
    SnapshotViewModal,
)
from operon.tui.screens.config_classification import (
    BestByRow,
    ClassificationSaveModal,
    classification_form_supported,
)
from operon.tui.screens.config_coverage import (
    CoverageSaveModal,
    coverage_form_supported,
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
SETTLE_TIMEOUT = 30.0


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
    before = _query(project, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]  # nosec B608 # fixed fixture table names
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
    assert _query(project, f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] == before  # nosec B608 # fixed fixture table names


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
            assert len(list_view.children) == 6

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


@pytest.mark.bug("ODR-0027")
def test_profile_editor_scrolls_to_all_rules(project: Project) -> None:
    """The rules editor must scroll: rule containers use height 1fr by default
    (plain Vertical), which clipped the rules to a fixed non-scrolling window."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "assembly_production_v1")
            await _await_form_ready(pilot, panel)
            editor = panel.query_one("#profile-editor", VerticalScroll)
            # The rows are in the tree before the layout gives the editor its
            # content height, so wait for the growth the test is about instead
            # of measuring in the mounting turn (ODR-0027).
            await _wait_until(
                lambda: editor.virtual_size.height > editor.scrollable_content_region.height,
                "the rules editor to grow past its viewport",
            )
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


async def _await_notification(pilot, app, needle: str) -> None:
    """Wait until some raised notification's message contains ``needle``.

    A save runs on a worker and raises its notification a message-loop turn
    after the modal closes, so reading ``_notifications(app)`` right after a
    single ``pause()`` sees the list before the text is in it — on a slow
    runner that turns a correct save into a failure (ODR-0027).
    """
    await _wait_until(
        lambda: any(needle in message for _, message in _notifications(app)),
        f"the notification {needle!r}",
    )


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
            added_rows = await _await_rows(pilot, panel.query_one("#profile-required-rules"),
                                           ".rule-row", 6, ".rule-metric")
            assert len(panel._rule_rows("required")) == 6
            added = added_rows[-1]
            assert added.query_one(".rule-operator", Select).value == ">="
            added.query_one(".rule-metric", Input).value = "gene_count"
            added.query_one(".rule-value", Input).value = "7"
            added.query_one(".rule-code", Input).value = "TOO_FEW_GENES"

            await _click(pilot, "#profile-add-warnings")
            warn_rows = await _await_rows(pilot, panel.query_one("#profile-warnings-rules"),
                                          ".rule-row", 3, ".rule-metric")
            assert len(panel._rule_rows("warnings")) == 3
            warn = warn_rows[-1]
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
            await _await_notification(pilot, app, "saved probe_extras_v1 version 2")

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
            await _await_notification(
                pilot, app, "assembly_production_v1: unchanged — version 1 kept")

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
            row = (await _await_rows(pilot, panel.query_one("#profile-required-rules"),
                                     ".rule-row", 1, ".rule-metric"))[0]
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
            await _await_notification(pilot, app, "saved assembly_strict_v1 version 1")

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
            await _await_notification(
                pilot, app, "profile 'reads_qc_v1' already exists — opening it instead")

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

            # The default rpsbproc parser is directly supported by the form.
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

            # Clearing max_hits removes the optional limit and records a new version.
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
            await _await_notification(pilot, app, "saved blastn_nt version 2")

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
    assert get_recipe(project, "blastn_nt").version == 2
    assert get_recipe(project, "blastn_nt").max_hits_per_query == 5  # core default
    assert "max_hits_per_query" not in get_recipe(project, "blastn_nt").raw
    assert [row["version"] for row in data.recipe_history(project, "blastn_nt")] == [2]
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


@pytest.mark.parametrize("kind", ["profile", "recipe"])
@pytest.mark.parametrize("replacement", ["missing", "older"])
def test_save_config_uses_snapshot_version_floor(project: Project, kind: str, replacement: str) -> None:
    """File deletion or an external rollback must not reuse historical versions."""
    if kind == "profile":
        name = "assembly_production_v1"
        original = data.get_profile_document(project, name)
        path = project.profiles_dir / f"{name}.yaml"
        save = lambda doc: actions.save_profile(project, name, doc)
    else:
        name = "blastn_nt"
        loaded = data.get_recipe_document(project, name)
        original = loaded["document"]
        path = project.tools_config_path
        save = lambda doc: actions.save_recipe(project, loaded["tool"], name, doc)
    edited = dict(original, description="first edit")
    assert save(edited)["version"] == 2
    edited["description"] = "second edit"
    assert save(edited)["version"] == 3
    if kind == "profile":
        if replacement == "missing":
            path.unlink()
        else:
            path.write_text(yaml.safe_dump(original), encoding="utf-8")
    else:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        recipes = config["tools"][loaded["tool"]]["recipes"]
        if replacement == "missing":
            del recipes[name]
        else:
            recipes[name] = original
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert save(edited)["version"] == 4
    assert save(edited)["unchanged"] is True
    history = data.profile_history if kind == "profile" else data.recipe_history
    assert [row["version"] for row in history(project, name)][-3:] == [2, 3, 4]


@pytest.mark.parametrize("parser", ["hmmer_domtblout", "rpsbproc_tabular"])
def test_config_screen_additional_parsers_save_and_reload(project: Project, parser: str) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            panel._load_recipe("blastn_nt")
            await pilot.pause()
            panel.query_one("#recipe-result-parser", Select).value = parser
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, RecipeSaveModal)
            panel._load_recipe("blastn_nt")
            assert panel.query_one("#recipe-result-parser", Select).value == parser

    _run(scenario())
    recipe = get_recipe(project, "blastn_nt")
    assert recipe.raw["result_parser"] == parser
    assert recipe.version == 2


# --- recipe editor: execution and parser-mapping fields ---------------------


def test_recipe_editor_environment_policy_options_match_core() -> None:
    from operon import tools

    assert ENVIRONMENT_POLICIES == tuple(tools.ENVIRONMENT_POLICIES)


NEW_RECIPE_INPUT_IDS = (
    "#recipe-file-role-prefix", "#recipe-result-glob",
    "#recipe-query-column", "#recipe-subject-column", "#recipe-numeric-columns",
    "#recipe-qstart-column", "#recipe-qend-column", "#recipe-sstart-column",
    "#recipe-send-column", "#recipe-evalue-column", "#recipe-bitscore-column",
    "#recipe-pident-column",
)
NEW_RECIPE_SELECT_IDS = (
    "#recipe-input-kind", "#recipe-output-kind",
    "#recipe-environment-policy", "#recipe-hmmer-mode",
)
NEW_RECIPE_KEYS = (
    "file_role_prefix", "input_kind", "output_kind", "environment_policy",
    "result_glob", "hmmer_mode", "query_column", "subject_column",
    "numeric_columns", "qstart_column", "qend_column", "sstart_column",
    "send_column", "evalue_column", "bitscore_column", "pident_column",
)


def test_config_screen_recipe_new_fields_roundtrip(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            panel._load_recipe("blastn_nt")
            await pilot.pause()
            # blastn_nt declares none of the newly modeled keys.
            for widget_id in NEW_RECIPE_SELECT_IDS:
                assert panel.query_one(widget_id, Select).value is Select.NULL
            for widget_id in NEW_RECIPE_INPUT_IDS:
                assert panel.query_one(widget_id, Input).value == ""

            # file_role_prefix is mutually exclusive with file_role: clear the role first.
            panel.query_one("#recipe-file-role", Input).value = ""
            panel.query_one("#recipe-file-role-prefix", Input).value = "genome"
            panel.query_one("#recipe-input-kind", Select).value = "file"
            panel.query_one("#recipe-output-kind", Select).value = "directory"
            panel.query_one("#recipe-environment-policy", Select).value = "strict"
            panel.query_one("#recipe-result-glob", Input).value = "summary*.json"
            panel.query_one("#recipe-hmmer-mode", Select).value = "hmmsearch"
            panel.query_one("#recipe-numeric-columns", Input).value = "pident, evalue"
            for widget_id, value in (
                    ("#recipe-query-column", "qseqid"),
                    ("#recipe-subject-column", "sseqid"),
                    ("#recipe-qstart-column", "qstart"),
                    ("#recipe-qend-column", "qend"),
                    ("#recipe-sstart-column", "sstart"),
                    ("#recipe-send-column", "send"),
                    ("#recipe-evalue-column", "evalue"),
                    ("#recipe-bitscore-column", "bitscore"),
                    ("#recipe-pident-column", "pident"),
            ):
                panel.query_one(widget_id, Input).value = value
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

            # The editor reloaded from disk; every control shows the saved value.
            assert panel.query_one("#recipe-file-role", Input).value == ""
            assert panel.query_one("#recipe-file-role-prefix", Input).value == "genome"
            assert panel.query_one("#recipe-input-kind", Select).value == "file"
            assert panel.query_one("#recipe-output-kind", Select).value == "directory"
            assert panel.query_one("#recipe-environment-policy", Select).value == "strict"
            assert panel.query_one("#recipe-result-glob", Input).value == "summary*.json"
            assert panel.query_one("#recipe-hmmer-mode", Select).value == "hmmsearch"
            assert panel.query_one("#recipe-numeric-columns", Input).value == "pident, evalue"
            assert panel.query_one("#recipe-query-column", Input).value == "qseqid"
            assert panel.query_one("#recipe-pident-column", Input).value == "pident"

    _run(scenario())
    raw = get_recipe(project, "blastn_nt").raw
    assert raw["version"] == 2
    assert raw["file_role"] == ""  # a cleared modeled key stays as an empty string
    assert raw["file_role_prefix"] == "genome"
    assert raw["input_kind"] == "file"
    assert raw["output_kind"] == "directory"
    assert raw["environment_policy"] == "strict"
    assert raw["result_glob"] == "summary*.json"
    assert raw["hmmer_mode"] == "hmmsearch"
    assert raw["numeric_columns"] == ["pident", "evalue"]
    assert raw["query_column"] == "qseqid"
    assert raw["subject_column"] == "sseqid"
    assert raw["qstart_column"] == "qstart"
    assert raw["qend_column"] == "qend"
    assert raw["sstart_column"] == "sstart"
    assert raw["send_column"] == "send"
    assert raw["evalue_column"] == "evalue"
    assert raw["bitscore_column"] == "bitscore"
    assert raw["pident_column"] == "pident"
    history = data.recipe_history(project, "blastn_nt")
    assert [row["version"] for row in history] == [2]
    snapshot = data.get_recipe_snapshot(project, "blastn_nt", history[0]["snapshot_id"])
    assert snapshot["recipe"]["environment_policy"] == "strict"
    assert snapshot["recipe"]["numeric_columns"] == ["pident", "evalue"]
    assert snapshot["recipe"]["file_role_prefix"] == "genome"


def test_config_screen_recipe_blank_new_fields_stay_absent(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            panel._load_recipe("hmmsearch_pfam")
            await pilot.pause()
            # hmmer_mode is declared by this recipe and renders selected.
            assert panel.query_one("#recipe-hmmer-mode", Select).value == "hmmsearch"
            panel.query_one("#recipe-description", Input).value = "touched"
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RecipeSaveModal)
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, RecipeSaveModal)

    _run(scenario())
    raw = get_recipe(project, "hmmsearch_pfam").raw
    assert raw["version"] == 2
    assert raw["description"] == "touched"
    assert raw["hmmer_mode"] == "hmmsearch"
    # Blank controls mean "key absent", never an empty string.
    for key in NEW_RECIPE_KEYS:
        if key != "hmmer_mode":
            assert key not in raw
    history = data.recipe_history(project, "hmmsearch_pfam")
    assert [row["version"] for row in history] == [2]
    snapshot = data.get_recipe_snapshot(project, "hmmsearch_pfam", history[0]["snapshot_id"])
    assert "environment_policy" not in snapshot["recipe"]


def test_config_screen_recipe_unknown_select_value_is_preserved(project: Project) -> None:
    """An out-of-vocabulary value of a modeled Select stays selected and
    preserved (get_recipe does not validate hmmer_mode at load time)."""
    path = project.tools_config_path
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["tools"]["hmmsearch"]["recipes"]["hmmsearch_pfam"]["hmmer_mode"] = "hmmscan2"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            panel._load_recipe("hmmsearch_pfam")
            await pilot.pause()
            assert panel.query_one("#recipe-hmmer-mode", Select).value == "hmmscan2"

    _run(scenario())


@pytest.mark.bug("ODR-0024")
def test_config_screen_recipe_file_role_prefix_conflict_blocks_save(project: Project) -> None:
    original_bytes = project.tools_config_path.read_bytes()

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            panel._load_recipe("blastn_nt")
            await pilot.pause()
            # blastn_nt sets file_role; adding a prefix must be rejected inline,
            # before the confirmation modal opens.
            panel.query_one("#recipe-file-role-prefix", Input).value = "genome"
            await _click(pilot, "#recipe-save")
            error_view = panel.query_one("#recipe-save-error", Static)
            # The press is a queued message: wait for the outcome it produces
            # instead of assuming one pause() cycle handled it (ODR-0024).
            await _wait_until(
                lambda: "mutually exclusive" in _static_text(error_view),
                "the inline conflict error",
            )
            assert not isinstance(app.screen, RecipeSaveModal)
            error = _static_text(error_view)
            assert "'file_role' and 'file_role_prefix' are mutually exclusive" in error
            assert "blastn_nt" in error

            # Clearing the conflict re-arms the save flow.
            panel.query_one("#recipe-file-role-prefix", Input).value = ""
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            await _wait_until(lambda: isinstance(app.screen, RecipeSaveModal),
                              "the recipe save modal")
            assert isinstance(app.screen, RecipeSaveModal)
            assert _static_text(panel.query_one("#recipe-save-error", Static)) == ""
            await _click(pilot, "#cancel")
            await pilot.pause()
            assert not isinstance(app.screen, RecipeSaveModal)

    _run(scenario())
    assert project.tools_config_path.read_bytes() == original_bytes
    assert data.recipe_history(project, "blastn_nt") == []


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

            # Saving recreates it above the recorded version, even with unchanged content.
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

    _run(scenario())
    assert load_profile(
        project.profiles_dir, "assembly_production_v1", expected_kind="qc"
    )["version"] == 2


@pytest.mark.parametrize("record_history", [False, True])
def test_config_screen_recipe_deleted_under_the_ui(project: Project, record_history: bool) -> None:
    from operon.tools import get_tool

    recipe = get_recipe(project, "busco_autolineage")
    if record_history:
        db = Database(project.db_path)
        try:
            with db.transaction():
                db.record_recipe(recipe.name, recipe.version, {
                    "recipe": recipe.raw, "tool": get_tool(project, recipe.tool_name).raw,
                })
        finally:
            db.close()

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
            assert "version 2" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, RecipeSaveModal)

    _run(scenario())
    recipe = get_recipe(project, "busco_autolineage")
    assert recipe.version == 2
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


# ---------------------------------------------------------------------------
# classification profiles (kind: sequence_classification, milestone M3 C2)
# ---------------------------------------------------------------------------

BHLH_PROFILE = {
    "kind": "sequence_classification",
    "version": 1,
    "description": "bHLH tiers",
    "applies_to": {"entity_type": "annotation", "file_role": "protein_fasta"},
    "sources": {
        "core": {
            "analysis": "rpsbproc_cdd",
            "filter": [
                {"field": "hit_type", "operator": "in", "values": ["Specific", "Motif"]},
            ],
            "best_by": [
                {"field": "hit_type", "rank": {"Specific": 0, "Motif": 1}},
                {"field": "evalue", "direction": "asc"},
            ],
        },
    },
    "rules": [
        {"label": "A", "source": "core",
         "when": [{"field": "short_name", "operator": "like", "value": "bHLH%"}]},
        {"label": "U", "source": "core", "absent": True},
        {"label": "C", "default": True},
    ],
}


async def _await_rows(pilot, root, selector: str, count: int, child: str) -> list:
    """Wait until ``root`` holds ``count`` ``selector`` rows whose ``child`` exists.

    Mounting a row subtree takes more than one message-loop turn, so a single
    ``pilot.pause()`` can observe a row that has not composed yet.  The budget is
    this file's wall-clock settle timeout rather than a fixed number of cycles: a
    loaded CI runner needs seconds to deliver the click and mount the row it
    produces, and a cycle count that is generous on a fast machine runs out there
    (ODR-0027).
    """
    def composed() -> list:
        return [row for row in root.query(selector) if len(list(row.query(child))) > 0]

    try:
        await _wait_until(lambda: len(composed()) >= count,
                          f"{count} {selector} rows with {child}")
    except TimeoutError as error:
        raise AssertionError(
            f"{selector} rows with {child}: saw {len(composed())}, wanted {count}"
        ) from error
    return composed()


async def _await_form_ready(pilot, panel) -> None:
    """Wait until the editor's deferred row mounts have landed (ODR-0023).

    Reads the same signal the save path uses: ``remount`` replaces an editor's
    rows a message-loop turn later, and the rows mount their own nested rows a
    turn after that, so a single ``pilot.pause()`` can still observe a form
    that is missing rows.  The budget is wall-clock, not a number of cycles: 120
    cycles of ``pause() + sleep`` is about a second on an idle machine and runs
    out on a loaded one (ODR-0027).
    """
    await _wait_until(
        lambda: not panel._form_mounting(),
        "finish mounting the editor form",
    )


def _write_classification_profile(project: Project, name: str, document: dict) -> Path:
    path = project.profiles_dir / f"{name}.yaml"
    path.write_text("# hand-written comment\n" + yaml.safe_dump(document, sort_keys=False),
                    encoding="utf-8")
    return path


def test_classification_operators_match_the_core() -> None:
    """The form's operator list is the core grammar's (module comment)."""
    from operon.classify import _OPERATORS
    from operon.tui.screens.config_classification import CLASSIFICATION_OPERATORS

    assert set(CLASSIFICATION_OPERATORS) == set(_OPERATORS)
    assert "like" in CLASSIFICATION_OPERATORS


def test_classification_profile_data_layer(project: Project) -> None:
    _write_classification_profile(project, "bhlh_tiers", BHLH_PROFILE)

    assert "bhlh_tiers" not in [row["name"] for row in data.list_qc_profiles(project)]
    listed = data.list_classification_profiles(project)
    assert [row["name"] for row in listed] == ["bhlh_tiers"]
    assert listed[0]["kind"] == "sequence_classification"
    document = data.get_profile_document(project, "bhlh_tiers", kind="sequence_classification")
    assert document["rules"][0]["label"] == "A"
    with pytest.raises(ValidationError):
        data.get_profile_document(project, "bhlh_tiers")  # qc is the default kind
    with pytest.raises(ValidationError):
        data.get_profile_document(project, "assembly_production_v1",
                                  kind="sequence_classification")


def test_save_classification_profile_versions_and_validation(project: Project) -> None:
    def fresh() -> dict:
        return json.loads(json.dumps(BHLH_PROFILE))

    result = actions.save_classification_profile(project, "bhlh_saved", fresh())
    assert result["version"] == 1 and result["snapshot_id"] is not None
    text = (project.profiles_dir / "bhlh_saved.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Operon sequence_classification profile bhlh_saved")
    load_profile(project.profiles_dir, "bhlh_saved", expected_kind="sequence_classification")
    snapshot = _query(
        project,
        "SELECT profile_document FROM qc_profiles WHERE profile_name='bhlh_saved'",
    )
    assert json.loads(snapshot[0]["profile_document"])["kind"] == "sequence_classification"

    unchanged = actions.save_classification_profile(project, "bhlh_saved", fresh())
    assert unchanged["unchanged"] is True and unchanged["version"] == 1
    changed = fresh()
    changed["description"] = "changed"
    assert actions.save_classification_profile(project, "bhlh_saved", changed)["version"] == 2

    broken = fresh()
    broken["applies_to"] = {"entity_type": "annotation"}
    with pytest.raises(ValidationError, match="applies_to.file_role"):
        actions.save_classification_profile(project, "broken", broken)
    with pytest.raises(ValidationError, match="on-disk kind"):
        actions.save_classification_profile(project, "assembly_production_v1", fresh())
    with pytest.raises(ValidationError, match="only kind 'qc'"):
        actions.save_profile(project, "bhlh_saved", fresh())

    coerced = fresh()
    coerced["rules"][0]["when"] = [{"field": "evalue", "operator": "<=", "value": "1e-5"}]
    actions.save_classification_profile(project, "bhlh_coerced", coerced)
    loaded = load_profile(project.profiles_dir, "bhlh_coerced",
                          expected_kind="sequence_classification")
    assert loaded["rules"][0]["when"][0]["value"] == 1e-05


@pytest.mark.bug("ODR-0027")
def test_config_classification_editor_end_to_end(project: Project) -> None:
    _write_classification_profile(project, "bhlh_tiers", BHLH_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)
            list_view = panel.query_one("#profiles-list", ListView)
            labels = [item.query_one(Label).render().plain for item in list_view.children]
            assert any("bhlh_tiers" in label and "classification" in label for label in labels)

            panel._load_profile("bhlh_tiers")
            await pilot.pause()
            assert panel.query_one("#classification-editor").display
            assert not panel.query_one("#profile-editor").display
            assert panel.classification_profile == "bhlh_tiers"
            assert panel.query_one("#classification-entity-type", Input).value == "annotation"
            assert panel.query_one("#classification-file-role", Input).value == "protein_fasta"
            assert len(list(panel.query(".source-row"))) == 1
            assert len(list(panel.query(".classrule-row"))) == 3
            assert len(list(panel.query(".bestby-row"))) == 2
            # Rows mount a message-loop turn after their container is emptied and
            # compose their inputs a turn after that: wait for the inputs before
            # reading the form, or the composition sees half-built rows (ODR-0023).
            await _await_rows(pilot, panel, ".source-row", 1, ".source-name")
            await _await_rows(pilot, panel, ".classrule-row", 3, ".classrule-label")
            await _await_rows(pilot, panel, ".classrule-when .condition-row", 1,
                              ".condition-field")
            await _await_rows(pilot, panel, ".bestby-row", 2, ".bestby-field")
            # … and for the Selects they hold, which take their value a turn
            # after the row composes (ODR-0026).
            await _await_form_ready(pilot, panel)
            # The form reproduces the on-disk document exactly.
            assert panel._compose_classification_document() == BHLH_PROFILE
            assert not panel.query_one("#classification-save", Button).disabled

            # Edit a condition, add a source and a rule through the buttons.
            rule_a = list(panel.query(".classrule-row"))[0]
            rule_a.query_one(".condition-value", Input).value = "bHLH%, HLH%"
            await _click(pilot, "#classification-add-source")
            rows = await _await_rows(pilot, panel, ".source-row", 2, ".source-name")
            source_row = rows[1]
            source_row.query_one(".source-name", Input).value = "extra"
            source_row.query_one(".source-analysis", Input).value = "hmmscan_pfam"
            await pilot.pause()
            await _click(pilot, "#classification-add-rule")
            rules = await _await_rows(pilot, panel, ".classrule-row", 4, ".classrule-label")
            new_rule = rules[3]
            new_rule.query_one(".classrule-label", Input).value = "B"
            source_select = new_rule.query_one(".classrule-source", Select)
            assert "extra" in [value for _, value in source_select._options]  # refreshed
            source_select.value = "extra"
            # The editor scrolls: press the button directly (Pilot.click needs
            # the target inside the visible area, and the selector matches rows).
            new_rule.query_one(".classrule-add-when", Button).press()
            when_rows = await _await_rows(pilot, new_rule, ".condition-row", 1, ".condition-field")
            when_row = when_rows[0]
            when_row.query_one(".condition-field", Input).value = "evalue"
            when_row.query_one(".condition-value", Input).value = "1e-5"
            # A default rule hides its source/when area.
            default_rule = list(panel.query(".classrule-row"))[2]
            assert default_rule.query_one(".classrule-when").display is False
            assert default_rule.query_one(".classrule-source", Select).disabled is True

            await _await_form_ready(pilot, panel)
            composed = panel._compose_classification_document()
            assert composed["rules"][0]["when"][0]["value"] == "bHLH%, HLH%"
            assert composed["rules"][3]["source"] == "extra"
            assert composed["sources"]["extra"]["analysis"] == "hmmscan_pfam"

            await _click(pilot, "#classification-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ClassificationSaveModal)
            assert "kind sequence_classification" in _static_text(
                modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, ClassificationSaveModal)
            await _await_notification(pilot, app, "saved bhlh_tiers version 2")
            # The editor reloaded from disk: the freshly saved structure renders.
            assert len(list(panel.query(".source-row"))) == 2
            assert len(list(panel.query(".classrule-row"))) == 4

            # Removing rows is a form-only edit (nothing is written until save).
            rows_after = await _await_rows(pilot, panel, ".source-row", 2, ".source-name")
            rows_after[0].query_one(".source-remove", Button).press()
            await pilot.pause()
            assert len(list(panel.query(".source-row"))) == 1

    _run(scenario())
    saved = load_profile(project.profiles_dir, "bhlh_tiers",
                         expected_kind="sequence_classification")
    assert saved["version"] == 2
    assert saved["rules"][0]["when"][0]["value"] == "bHLH%, HLH%"
    assert saved["sources"]["extra"]["analysis"] == "hmmscan_pfam"
    assert saved["rules"][3] == {"label": "B", "source": "extra",
                                 "when": [{"field": "evalue", "operator": "==", "value": 1e-05}]}
    assert _query(project, "SELECT profile_version FROM qc_profiles "
                           "WHERE profile_name='bhlh_tiers'")[-1]["profile_version"] == 2


@pytest.mark.bug("ODR-0027")
def test_config_classification_editor_guards_and_readonly(project: Project) -> None:
    nested = json.loads(json.dumps(BHLH_PROFILE))
    nested["rules"][0]["when"] = [{"any": [{"not": {"field": "x", "operator": "exists"}}]}]
    _write_classification_profile(project, "nested_tiers", nested)
    _write_classification_profile(project, "bhlh_tiers", BHLH_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)

            # Deeper nesting opens read-only with the reason spelled out.
            panel._load_profile("nested_tiers")
            await pilot.pause()
            assert panel.query_one("#classification-save", Button).disabled
            note = _static_text(panel.query_one("#classification-readonly-note", Static))
            assert "nests conditions deeper" in note and "edit the YAML file" in note

            # Duplicate source names are refused inline before the modal opens.
            panel._load_profile("bhlh_tiers")
            await pilot.pause()
            # A row is in the tree a turn before its inputs are: wait for the
            # composed row instead of reading it in the mounting turn (ODR-0023).
            rows = await _await_rows(pilot, panel, ".source-row", 1, ".source-name")
            rows[0].query_one(".source-name", Input).value = "dup"
            await _click(pilot, "#classification-add-source")
            rows = await _await_rows(pilot, panel, ".source-row", 2, ".source-name")
            second = rows[1]
            second.query_one(".source-name", Input).value = "dup"
            await _click(pilot, "#classification-save")
            await pilot.pause()
            assert not isinstance(app.screen, ClassificationSaveModal)
            error = _static_text(panel.query_one("#classification-save-error", Static))
            assert "duplicate source name" in error

            # Unmodeled document keys survive a save untouched.
            second.query_one(".source-name", Input).value = "second"
            second.query_one(".source-analysis", Input).value = "second_analysis"
            await pilot.pause()
            panel.classification_doc = dict(panel.classification_doc or {}, custom_key="kept")

    _run(scenario())


def test_config_screen_new_classification_profile(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)
            await _click(pilot, "#profile-new")
            await pilot.pause()
            app.screen.query_one("#new-profile-name", Input).value = "new_tiers"
            app.screen.query_one("#new-profile-kind", Select).value = "sequence_classification"
            await _click(pilot, "#confirm")
            await pilot.pause()
            heading = _static_text(panel.query_one("#classification-heading", Static))
            assert "new_tiers" in heading and "new profile (not saved yet)" in heading
            assert panel.query_one("#classification-entity-type", Input).value == "annotation"
            assert len(list(panel.query(".source-row"))) == 0
            # An empty skeleton is still editable: adding rows is the point.
            assert not panel.query_one("#classification-save", Button).disabled

            await _click(pilot, "#classification-add-source")
            source = (await _await_rows(pilot, panel, ".source-row", 1, ".source-name"))[0]
            source.query_one(".source-name", Input).value = "core"
            source.query_one(".source-analysis", Input).value = "rpsbproc_cdd"
            await _click(pilot, "#classification-add-rule")
            rule = (await _await_rows(pilot, panel, ".classrule-row", 1, ".classrule-label"))[0]
            rule.query_one(".classrule-label", Input).value = "C"
            rule.query_one(".classrule-mode", Select).value = "default"
            await pilot.pause()
            assert rule.query_one(".classrule-when").display is False

            panel.query_one("#classification-description", Input).value = "created in the TUI"
            await _click(pilot, "#classification-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ClassificationSaveModal)
            assert "version 1" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()

    _run(scenario())
    created = load_profile(project.profiles_dir, "new_tiers",
                           expected_kind="sequence_classification")
    assert created["version"] == 1
    assert created["description"] == "created in the TUI"
    assert created["sources"] == {"core": {"analysis": "rpsbproc_cdd", "filter": []}}
    assert created["rules"] == [{"label": "C", "default": True}]


@pytest.mark.bug("ODR-0023")
def test_classification_save_refuses_a_form_that_is_still_mounting(project: Project) -> None:
    """Saving inside a deferred rebuild is refused with a message, not a crash.

    ``_render_classification_form`` replaces the source and rule rows through
    ``remount``, and those rows mount their own filter/when rows a turn later,
    so the form is not readable in the turn the render returns in.  Reading it
    there used to raise ``NoMatches`` out of a button handler — or, when the
    containers were still empty, compose a document with the rows silently
    dropped (ODR-0023).  The save reports the wait, and opens the modal once
    the form has mounted.
    """
    _write_classification_profile(project, "bhlh_tiers", BHLH_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("bhlh_tiers")
            # No await in between: this is the state a click handler sees in the
            # turn after a rebuild, with the rows still on their way.
            panel._start_classification_save()
            error = panel.query_one("#classification-save-error", Static)
            assert panel.FORM_MOUNTING_MESSAGE in _static_text(error)
            assert not isinstance(app.screen, ClassificationSaveModal)

            await _await_form_ready(pilot, panel)
            panel._start_classification_save()
            await pilot.pause()
            assert isinstance(app.screen, ClassificationSaveModal)
            await pilot.press("escape")

    _run(scenario())


@pytest.mark.bug("ODR-0039")
def test_blocked_save_names_a_control_that_never_finished_loading(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused save tells "still loading" and "never loaded" apart.

    A select whose mount retries ran out blocks the form exactly like one that is
    still mounting, and waiting never helps it — so the message has to say that and
    name the control.  The stalled pair of signals is set on the class here; that the
    real exhaustion path sets them is covered by
    test_fitting_select_reports_a_mount_that_ran_out_of_retries.
    """
    from operon.tui.screens.common import FittingSelect

    _write_classification_profile(project, "bhlh_tiers", BHLH_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("bhlh_tiers")
            await _await_form_ready(pilot, panel)

            monkeypatch.setattr(FittingSelect, "options_ready", property(lambda self: False))
            monkeypatch.setattr(FittingSelect, "options_gave_up", property(lambda self: True))
            panel._start_classification_save()
            error = panel.query_one("#classification-save-error", Static)
            await pilot.pause()
            text = _static_text(error)
            assert panel.FORM_STALLED_MESSAGE in text
            assert panel.FORM_MOUNTING_MESSAGE not in text

    _run(scenario())


@pytest.mark.bug("ODR-0023")
def test_row_reports_ready_only_once_its_subtree_is_in_the_tree(project: Project) -> None:
    """A mounted row is not readable until its own subtree composed.

    The panel refuses a save while the form is still mounting, and the signal it
    reads is the row's own latch: mounted-but-uncomposed rows are what made a
    composition walk half-built widgets and raise ``NoMatches`` out of a button
    handler (ODR-0023).  This pins both halves of that contract.
    """

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            container = panel.query_one("#profile-required-rules", MountTracked)
            row = RuleRow({"metric": "assembly.length", "operator": ">=", "value": "1",
                           "code": "C_LEN"})
            container.mount_later(row, when_present=".rule-row")
            # The mount lands a message-loop turn later: in this turn the row is
            # not in the tree, so both the row and the form report "not yet".
            assert not row.form_ready
            assert panel._form_mounting()

            await _wait_until(lambda: row.form_ready, "the row's subtree to compose")
            assert not panel._form_mounting()

    _run(scenario())


@pytest.mark.bug("ODR-0023")
def test_click_helper_reaches_a_button_whose_centre_is_off_screen(
    project: Project, monkeypatch
) -> None:
    """``_click`` activates an enabled button even when ``Pilot.click`` cannot.

    A rebuilt layout can leave the target's centre outside the screen region,
    where ``Pilot.click`` raises ``OutOfBounds`` instead of returning False —
    the failure that reached CI in an unrelated test (ODR-0023).  Pressing the
    button is the same activation a landed click produces.
    """

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            _select_profile(panel, "assembly_production_v1")
            await pilot.pause()

            async def click_away(*_args, **_kwargs):
                raise OutOfBounds(
                    "Target offset is outside of currently-visible screen region."
                )

            monkeypatch.setattr(pilot, "click", click_away)
            await _click(pilot, "#profile-save")
            await pilot.pause()
            assert isinstance(app.screen, ProfileSaveModal)
            await pilot.press("escape")

    _run(scenario())


@pytest.mark.bug("ODR-0026")
def test_select_whose_mount_lookup_failed_adopts_its_value() -> None:
    """A Select that could not paint its label must still report its value.

    Textual's ``_init_selected_option`` reaches for the overlay *before* it
    stores the value it was constructed with, so the deferred mount guarded for
    ODR-0023 left ``value`` at ``NULL`` — and a condition whose operator was
    ``like`` composed as ``''`` (ODR-0026).
    """
    select = FittingSelect([("like", "like"), ("==", "==")], value="like", allow_blank=False)
    select._value = Select.NULL            # what the interrupted mount left behind
    select._options_ready = False
    select._adopt_value_before_overlay()
    assert select.value == "like"
    assert not select.options_ready


@pytest.mark.bug("ODR-0026")
def test_classification_save_refuses_a_form_with_an_uninitialised_select(project: Project) -> None:
    """A Select that has not adopted its value yet is a half-built control.

    Reading it yields a blank operator, so the save refuses — like any other
    not-yet-mounted part of the form (ODR-0023) — and proceeds once the Select
    reports itself ready.
    """
    _write_classification_profile(project, "bhlh_tiers", BHLH_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("bhlh_tiers")
            await _await_form_ready(pilot, panel)
            select = panel.query_one(".condition-operator", FittingSelect)
            assert select.options_ready

            select._options_ready = False
            panel._start_classification_save()
            error = panel.query_one("#classification-save-error", Static)
            assert panel.FORM_MOUNTING_MESSAGE in _static_text(error)
            assert not isinstance(app.screen, ClassificationSaveModal)

            select._options_ready = True
            panel._start_classification_save()
            await pilot.pause()
            assert isinstance(app.screen, ClassificationSaveModal)
            await pilot.press("escape")

    _run(scenario())


@pytest.mark.bug("ODR-0026")
def test_best_by_rank_map_keeps_whole_ranks_whole(project: Project) -> None:
    """A rank map of whole numbers must not be rewritten as floats.

    ``float`` turned ``{Specific: 0, Motif: 1}`` into ``0.0``/``1.0``, so saving
    an untouched form rewrote the file it was read from (ODR-0026).
    """

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            hook = MountTracked()
            app.screen.mount(hook)
            await pilot.pause()
            row = BestByRow({"field": "hit_type", "direction": "asc",
                             "rank": {"Specific": 0, "Motif": 1}})
            hook.mount(row)
            await _wait_until(lambda: row.form_ready, "the best-by row to compose")
            document = row.best_by_document()
            assert document["rank"] == {"Specific": 0, "Motif": 1}
            assert [type(value) for value in document["rank"].values()] == [int, int]

    _run(scenario())


# --------------------------------------------------------------------------- #
# The QC rule operator control (ODR-0023 / ODR-0026)
# --------------------------------------------------------------------------- #

@pytest.mark.bug("ODR-0023")
@pytest.mark.bug("ODR-0026")
def test_rule_operator_controls_are_guarded_and_keep_their_value(project: Project) -> None:
    """The QC rule operator is the guarded Select: ready, and on its value.

    ``RuleRow`` is one of the rows ``remount`` rebuilds, so its operator control
    must be a ``FittingSelect``.  A bare ``Select`` there was invisible to
    ``_form_mounting``'s readiness query and carried both exposures the guarded
    subclass exists for — Textual's own mount-phase lookups (ODR-0023) and the
    value the control was built with being lost when that setup is deferred
    (ODR-0026).  This drives the real path: the editor is rendered twice through
    ``_load_profile``, so the second pass replaces an already populated form and
    its rows arrive while the previous generation is still retiring, and each
    pass reads the controls and the document they compose.
    """

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            await _settled(app)
            app.action_switch_screen("config")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(ConfigPanel)
            await _await_form_ready(pilot, panel)

            document = _profile_doc(project, "assembly_production_v1")
            operators = [str(rule.get("operator", "")) for rule in document["required"]]
            assert operators, "the demo profile needs required rules"

            for render in range(2):
                panel._load_profile("assembly_production_v1")
                await _await_form_ready(pilot, panel)
                rows = panel._rule_rows("required")
                assert len(rows) == len(operators)
                for row, operator in zip(rows, operators):
                    control = row.query_one(".rule-operator")
                    assert isinstance(control, FittingSelect), (
                        f"render {render}: the operator control is a bare "
                        f"{type(control).__name__}, so _form_mounting cannot see it"
                    )
                    assert control.options_ready
                    assert control.value == operator
                    assert row.rule_document()["operator"] == operator

    _run(scenario())


# --------------------------------------------------------------------------- #
# Two editor rebuilds in one turn (ODR-0030)
# --------------------------------------------------------------------------- #

@pytest.mark.bug("ODR-0030")
def test_two_editor_rebuilds_in_one_turn_leave_one_generation(project: Project) -> None:
    """Two rebuilds in one turn must not leave both generations in the form.

    Both selections are handled before either replacement's deferred mount
    lands.  The first replacement used to mount next to the second one's rows,
    so the form held two generations, the readiness check read the *old* rows as
    if the new form were ready, and a save composed a document with every rule
    twice (ODR-0030).  The two profiles below hold 1 + 3 and 6 + 3 rules, so a
    doubled generation is unmistakable.
    """
    first = "annotation_busco_viridiplantae_odb12_v1"
    second = "annotation_release_v1"

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            await _settled(app)
            app.action_switch_screen("config")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(ConfigPanel)

            # Two rebuilds inside one turn: the second replaces the first
            # before its deferred mount could land.
            panel._load_profile(first)
            panel._load_profile(second)
            await _wait_until(lambda: not panel._form_mounting(),
                              "the editor to finish rebuilding")

            document = _profile_doc(project, second)
            container = panel.query_one("#profile-required-rules", MountTracked)
            rows = panel._rule_rows("required")
            assert len(rows) == len(document["required"])
            assert len(container.children) == len(document["required"])
            assert [row.query_one(".rule-metric", Input).value for row in rows] == [
                rule["metric"] for rule in document["required"]
            ]
            assert panel.current_profile == second

            # A save right after the rebuild composes the file's rules once.
            await _click(pilot, "#profile-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ProfileSaveModal)
            assert modal.document["required"] == document["required"]
            assert modal.document["warnings"] == document["warnings"]

    _run(scenario())


# --------------------------------------------------------------------------- #
# Classification editor: row buttons and unmodelled values
# --------------------------------------------------------------------------- #

def test_classification_editor_row_buttons_add_and_remove(project: Project) -> None:
    """Every add/remove button in the classification editor edits its own row.

    The row-scoped buttons sit in a scrolling editor, so the press is sent to the
    button itself (``Pilot.click`` needs the target inside the visible region).
    Each pair adds a row and removes it again, and the document composed at the
    end is the one on disk with only the emptied ``when`` list left over — a
    removal must not leave a row, an input or a message handler behind.
    """
    _write_classification_profile(project, "bhlh_tiers", BHLH_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("bhlh_tiers")
            await pilot.pause()
            await _await_form_ready(pilot, panel)
            source = (await _await_rows(pilot, panel, ".source-row", 1, ".source-name"))[0]

            # The source's filter editors: add one, remove it again.
            await _await_rows(pilot, source, ".condition-editor", 1, ".condition-field")
            source.query_one(".source-add-filter", Button).press()
            editors = await _await_rows(pilot, source, ".condition-editor", 2, ".condition-field")
            editors[1].query_one(".condition-remove", Button).press()
            await _await_rows(pilot, source, ".condition-editor", 1, ".condition-field")

            # The editor's own nested add/remove is covered by the mode test below.
            # best_by entries.
            source.query_one(".source-add-bestby", Button).press()
            entries = await _await_rows(pilot, source, ".bestby-row", 3, ".bestby-field")
            entries[2].query_one(".bestby-remove", Button).press()
            await _await_rows(pilot, source, ".bestby-row", 2, ".bestby-field")

            # A whole source and a whole rule.
            await _click(pilot, "#classification-add-source")
            sources = await _await_rows(pilot, panel, ".source-row", 2, ".source-name")
            sources[1].query_one(".source-remove", Button).press()
            await _await_rows(pilot, panel, ".source-row", 1, ".source-name")
            await _click(pilot, "#classification-add-rule")
            rules = await _await_rows(pilot, panel, ".classrule-row", 4, ".classrule-label")
            rules[3].query_one(".classrule-remove", Button).press()
            await _await_rows(pilot, panel, ".classrule-row", 3, ".classrule-label")

            # A rule's when condition (a filter editor, removed through its own ✕).
            rule_a = (await _await_rows(pilot, panel, ".classrule-row", 3,
                                        ".classrule-label"))[0]
            whens = await _await_rows(pilot, rule_a, ".condition-editor", 1,
                                      ".condition-field")
            whens[0].query_one(".condition-remove", Button).press()
            await _wait_until(lambda: not rule_a.query(".condition-editor"),
                              "the when condition to go")

            await _await_form_ready(pilot, panel)
            expected = json.loads(json.dumps(BHLH_PROFILE))
            expected["rules"][0]["when"] = []
            assert panel._compose_classification_document() == expected

    _run(scenario())


def test_classification_condition_editor_mode_and_nested_rows(project: Project) -> None:
    """A filter editor rebuilds its body between a leaf and an ``any`` group.

    The ``any`` body is the one that carries the add/remove pair for its nested
    rows, and it seeds one empty row when the document holds none; removing the
    row again leaves the group the editor composes.  Every step waits for the
    rows the rebuild produced instead of reading the body in its mounting turn.
    """
    _write_classification_profile(project, "bhlh_tiers", BHLH_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("bhlh_tiers")
            await pilot.pause()
            await _await_form_ready(pilot, panel)
            source = (await _await_rows(pilot, panel, ".source-row", 1, ".source-name"))[0]
            editor = (await _await_rows(pilot, source, ".condition-editor", 1,
                                        ".condition-field"))[0]

            editor.query_one(".condition-mode", FittingSelect).value = "any"
            # The add button is what makes the body an any-group: wait for it
            # rather than for a row, which both bodies carry.
            await _wait_until(lambda: bool(editor.query(".condition-add")),
                              "the any-mode body to compose")
            rows = await _await_rows(pilot, editor, ".condition-row", 1, ".condition-field")
            assert rows[0].query_one(".condition-field", Input).value == ""

            editor.query_one(".condition-add", Button).press()
            rows = await _await_rows(pilot, editor, ".condition-row", 2, ".condition-field")
            rows[1].query_one(".condition-field", Input).value = "evalue"
            rows[1].query_one(".condition-value", Input).value = "1e-5"
            rows[1].query_one(".condition-remove", Button).press()
            await _wait_until(
                lambda: len(list(editor.query(".condition-row"))) == 1,
                "the nested condition to go",
            )

            await _await_form_ready(pilot, panel)
            assert editor.editor_document() == {
                "any": [{"field": "", "operator": "==", "value": ""}]
            }

            # Not-mode: an any-group body cannot seed a ``not:``, so the inner
            # condition starts empty.  Every wait reads through the editor's own
            # composition (safe inside a replace window, ODR-0035/ODR-0036) and on
            # the group the replacement retired — never on a raw row, which the
            # next turn may prune out from under the test.
            editor.query_one(".condition-mode", FittingSelect).value = "not"
            body = editor.query_one(".condition-body", MountTracked)
            await _wait_until(lambda: not body.query(".condition-group"),
                              "the retired group to go")
            # The replacement's row is in the tree a turn before its operator
            # Select has adopted its value (ODR-0026), so this value assertion
            # waits on the same signal the save path waits on.
            await _await_form_ready(pilot, panel)
            assert editor.editor_document() == {
                "not": {"field": "", "operator": "==", "value": ""}
            }
            # The not body keeps one row, and it is not removable from here.
            assert len(list(body.query(".condition-remove"))) == 0

            # Back to a leaf: the body loses its rows and its remove button.  The
            # not-group and the leaf have the same widgets, so the wait reads the
            # composition as well as the retired group.
            editor.query_one(".condition-mode", FittingSelect).value = "condition"
            await _wait_until(
                lambda: not body.query(".condition-group")
                and list(editor.editor_document()) == ["field", "operator", "value"],
                "the leaf body to compose",
            )
            # Same readiness as the save path reads before this assertion.
            await _await_form_ready(pilot, panel)
            assert list(editor.editor_document()) == ["field", "operator", "value"]
            assert len(list(body.query(".condition-remove"))) == 0

    _run(scenario())


def test_classification_editor_keeps_operands_and_keys_it_does_not_model(
    project: Project,
) -> None:
    """A profile using operands and keys the form does not model round-trips.

    ``between`` operands, ``exists``, an operator and a direction with no option
    in the pickers, and extra keys at the condition, best_by, source and rule
    level: what the editor composes is what it read, and the operand inputs show
    the text the loader derived for each operator.  The one shape it does not
    keep verbatim is a quoted numeric operand — that text is composed as the
    number it looks like, the coercion the form pins for every operand input
    (``test_tui_config.py``'s ``value == 7`` assertion next to the QC editor) —
    and a condition that omits what its operator needs comes back canonically
    (``{"field": x}`` gains ``"operator": "=="`` and ``"value": ""``), which is
    the shape ``classify``'s own validator asks for.
    """
    document = json.loads(json.dumps(BHLH_PROFILE))
    source = document["sources"]["core"]
    source["note"] = "kept verbatim"
    source["filter"] = [
        {"field": "hit_type", "operator": "in", "values": ["Specific", "Motif"]},
        {"field": "length", "operator": "between", "min": 100, "max": 200},
        {"field": "description", "operator": "exists"},
        {"field": "score", "operator": "custom_op", "value": "n/a", "note": "unknown"},
        {"not": {"field": "description", "operator": "==", "value": ""}},
    ]
    source["best_by"] = [
        {"field": "hit_type", "rank": {"Specific": 0, "Motif": 1}},
        {"field": "evalue", "direction": "asc", "default": 1e-5, "note": "kept too"},
        {"field": "score", "direction": "sideways"},
    ]
    document["rules"][0]["note"] = "extra at rule level"
    _write_classification_profile(project, "bhlh_rich", document)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("bhlh_rich")
            await pilot.pause()
            await _await_form_ready(pilot, panel)
            # Five source filters plus the first rule's when condition.
            await _await_rows(pilot, panel, ".condition-editor", 6, ".condition-field")

            def editor_for(field: str, operator: str):
                """The filter editor for ``field`` using ``operator``.

                The same field name can appear in two entries (``description`` is
                both an ``exists`` leaf and the inner condition of a ``not:``), so
                the operator is what identifies the row.
                """
                for candidate in panel.query(".condition-editor"):
                    if candidate.query_one(".condition-field", Input).value != field:
                        continue
                    control = candidate.query_one(".condition-operator", Select)
                    if str(control.value) == operator:
                        return candidate
                raise AssertionError(f"no {operator!r} editor for {field!r}")

            between = editor_for("length", "between")
            assert between.query_one(".condition-value", Input).value == "100, 200"
            assert between.query_one(".condition-value", Input).placeholder == "min, max"

            exists = editor_for("description", "exists")
            assert exists.query_one(".condition-value", Input).value == ""
            assert exists.query_one(".condition-value", Input).placeholder == (
                "(no value for 'exists')"
            )

            unknown = editor_for("score", "custom_op")
            assert unknown.query_one(".condition-operator", FittingSelect).value == "custom_op"
            assert unknown.query_one(".condition-value", Input).value == "n/a"

            direction = panel.query(".bestby-direction").results(FittingSelect)
            assert [str(item.value) for item in direction] == ["asc", "asc", "sideways"]

            composed = panel._compose_classification_document()
            core = document["sources"]["core"]
            assert composed["sources"]["core"]["filter"] == core["filter"]
            assert composed["sources"]["core"]["best_by"] == core["best_by"]
            assert composed["sources"]["core"] == core
            assert composed["rules"] == document["rules"]
            assert composed == document

            # A stray comma in a rank map is skipped rather than becoming a rank
            # with an empty name.
            ranked = list(panel.query(".bestby-row").results(BestByRow))[1]
            ranked.query_one(".bestby-rank", Input).value = "Specific=0, , Motif=1"
            await _await_form_ready(pilot, panel)
            assert panel._compose_classification_document()["sources"]["core"][
                "best_by"
            ][1]["rank"] == {"Specific": 0, "Motif": 1}

    _run(scenario())


# --------------------------------------------------------------------------- #
# Which documents the classification form can represent
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ({"sources": {"core": {"filter": [{"field": "hit_type"}]}},
          "rules": [{"label": "A", "when": [{"field": "x"}]}]}, (True, "")),
        ({"sources": {"core": {"filter": [
            {"any": [{"field": "a"}, {"field": "b"}]}]}}, "rules": []}, (True, "")),
        ({"sources": {"core": {"filter": [{"not": {"field": "a"}}]}},
          "rules": []}, (True, "")),
        ({}, (True, "")),  # missing collections are editable, not read-only
        ({"version": 3, "description": "x"}, (True, "")),  # keys the form ignores
    ],
)
def test_classification_form_accepts_flat_conditions(
    document: dict, expected: tuple[bool, str]
) -> None:
    assert classification_form_supported(document) == expected


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ({"sources": "core", "rules": []}, "sources is not a mapping"),
        ({"sources": {"core": "rpsbproc_cdd"}, "rules": []},
         "source 'core' is not a mapping"),
        ({"sources": {"core": {"best_by": ["evalue"]}}, "rules": []},
         "source 'core' has a best_by entry that is not a mapping"),
        ({"sources": {"core": {"filter": [{"any": [{"any": [{"field": "a"}]}]}]}},
          "rules": []},
         "source 'core' nests conditions deeper than one any:/not: level"),
        ({"sources": {"core": {"filter": [{"not": {"any": [{"field": "a"}]}}]}},
          "rules": []},
         "source 'core' nests conditions deeper than one any:/not: level"),
        ({"sources": {"core": {"filter": ["hit_type"]}}, "rules": []},
         "source 'core' nests conditions deeper than one any:/not: level"),
        ({"sources": {"core": {"filter": [{"any": {"field": "a"}}]}}, "rules": []},
         "source 'core' nests conditions deeper than one any:/not: level"),
        ({"sources": {}, "rules": {"label": "A"}}, "rules is not a list"),
        ({"sources": {}, "rules": ["label A"]}, "rule 0 is not a mapping"),
        ({"sources": {}, "rules": [{"label": "A",
                                    "when": [{"any": [{"field": "a"},
                                                      {"not": {"field": "b"}}]}]}]},
         "rule 0 nests conditions deeper than one any:/not: level"),
    ],
)
@pytest.mark.bug("ODR-0034")
def test_classification_form_refuses_what_it_cannot_represent(
    document: dict, reason: str
) -> None:
    """Every shape the form would rewrite rather than preserve says so.

    The reason reaches the read-only note, so it names the offending source or
    rule index instead of a bare "unsupported".
    """
    supported, message = classification_form_supported(document)
    assert supported is False
    assert message == reason


def test_classification_save_of_an_unchanged_document_keeps_the_version(
    project: Project,
) -> None:
    """Saving a form that composes the file it came from keeps the version.

    ``save_classification_profile`` compares the composed document with the one
    on disk and reports ``unchanged``; the modal has to say so and leave the
    version alone instead of writing a new one for a no-op.
    """
    _write_classification_profile(project, "bhlh_unchanged", BHLH_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("bhlh_unchanged")
            await pilot.pause()
            await _await_form_ready(pilot, panel)

            await _click(pilot, "#classification-save")
            await pilot.pause()
            assert isinstance(app.screen, ClassificationSaveModal)
            await _click(pilot, "#confirm")
            await _await_notification(
                pilot, app, "bhlh_unchanged: unchanged — version 1 kept"
            )
            assert data.get_profile_document(
                project, "bhlh_unchanged", kind="sequence_classification"
            )["version"] == 1

    _run(scenario())


@pytest.mark.bug("ODR-0035")
def test_classification_editor_composes_while_the_body_is_between_generations(
    project: Project, monkeypatch
) -> None:
    """A read inside a mode change's replace window composes the seeded document.

    ``replace_children`` retires the old rows and mounts the next generation a turn
    later, so for that moment the body holds nothing a reader can compose: before
    the fix this raised ``NoMatches`` in leaf/not mode (and reported an empty group
    in any mode), which is what a save pressed in the same turn would get.  The test
    hooks the body's own ``mount`` — the product calls it after the retirement has
    landed — so the read happens exactly inside the window instead of whenever a
    polling loop happens to land there, which is how the pre-fix failure surfaced as
    a load-dependent flake (ODR-0035).
    """
    _write_classification_profile(project, "bhlh_tiers", BHLH_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("bhlh_tiers")
            await pilot.pause()
            await _await_form_ready(pilot, panel)
            source = (await _await_rows(pilot, panel, ".source-row", 1, ".source-name"))[0]
            editor = (await _await_rows(pilot, source, ".condition-editor", 1,
                                        ".condition-field"))[0]
            body = editor.query_one(".condition-body", MountTracked)

            reads: list[Any] = []
            original_mount = body.mount

            async def watched_mount(*args, **kwargs):
                try:
                    reads.append(editor.editor_document())
                except Exception as exc:  # noqa: BLE001 - the defect is the raise
                    reads.append(exc)
                return await original_mount(*args, **kwargs)

            monkeypatch.setattr(body, "mount", watched_mount)
            editor.query_one(".condition-mode", FittingSelect).value = "not"
            await _wait_until(lambda: bool(reads), "the read inside the replace window")
            await _await_form_ready(pilot, panel)

            assert reads and not isinstance(reads[0], Exception), reads
            # The seed is the condition the editor was holding, wrapped for the mode
            # it is switching to; the live composition agrees once the body lands.
            assert reads[0] == {
                "not": {"field": "hit_type", "operator": "in",
                        "values": ["Specific", "Motif"]},
            }
            assert editor.editor_document() == reads[0]

    _run(scenario())


@pytest.mark.bug("ODR-0036")
def test_classification_editor_composes_while_a_nested_row_is_being_removed(
    project: Project,
) -> None:
    """A read across a nested row's removal skips the row that is going away.

    Textual prunes a removed row's children a turn before the row leaves the tree,
    and the row's ``form_ready`` latch stays set (one-way by design, ODR-0023), so a
    composition that lands in that turn read a row with no inputs and raised
    ``NoMatches`` — the shape the loaded-suite failure of the mode test showed.  The
    read is taken every turn from the removal until the row is gone, so the window is
    polled rather than hoped for.
    """
    _write_classification_profile(project, "bhlh_tiers", BHLH_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(170, 55)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("bhlh_tiers")
            await pilot.pause()
            await _await_form_ready(pilot, panel)
            source = (await _await_rows(pilot, panel, ".source-row", 1, ".source-name"))[0]
            editor = (await _await_rows(pilot, source, ".condition-editor", 1,
                                        ".condition-field"))[0]
            editor.query_one(".condition-mode", FittingSelect).value = "any"
            await _wait_until(lambda: bool(editor.query(".condition-add")),
                              "the any-mode body to compose")
            await _await_rows(pilot, editor, ".condition-row", 1, ".condition-field")
            editor.query_one(".condition-add", Button).press()
            rows = await _await_rows(pilot, editor, ".condition-row", 2, ".condition-field")
            victim = rows[1]

            reads: list[Any] = []
            victim.query_one(".condition-remove", Button).press()
            for _ in range(40):
                try:
                    reads.append(editor.editor_document())
                except Exception as exc:  # noqa: BLE001 - the defect is the raise
                    reads.append(exc)
                if len(list(editor.query(".condition-row"))) == 1:
                    break
                await pilot.pause()

            assert len(list(editor.query(".condition-row"))) == 1, "the row is still in the tree"
            assert not [read for read in reads if isinstance(read, Exception)], reads
            # The row that left carries nothing into the document, and the row that
            # stayed is composed as it stands.  (A *blank* control belongs to
            # ODR-0026's window — a select that has not adopted its value yet — which
            # the readiness gate covers, not this one.)
            assert reads[-1] == {"any": [{"field": "", "operator": "==", "value": ""}]}

    _run(scenario())


@pytest.mark.bug("ODR-0041")
def test_config_screen_classification_history_opens_the_selected_profile(
        project: Project) -> None:
    _write_classification_profile(project, "bhlh_tiers", BHLH_PROFILE)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            # Fresh screen: no qc profile was ever selected, so the qc-only
            # gate used to swallow the click (ODR-0041).
            panel._load_profile("bhlh_tiers")
            await pilot.pause()
            await _click(pilot, "#classification-history")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, HistoryModal)
            assert "bhlh_tiers" in _static_text(modal.query_one("#modal-title", Static))
            await _click(pilot, "#cancel")
            await pilot.pause()

            # A stale qc selection must not leak into the classification
            # profile's history either.
            panel._load_profile("assembly_production_v1")
            await pilot.pause()
            panel._load_profile("bhlh_tiers")
            await pilot.pause()
            await _click(pilot, "#classification-history")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, HistoryModal)
            title = _static_text(modal.query_one("#modal-title", Static))
            assert "bhlh_tiers" in title
            assert "assembly_production_v1" not in title

    _run(scenario())


# --- coverage profile editor (kind: taxonomy_coverage) -----------------------


def _write_coverage_profile(project: Project, name: str, document: dict) -> Path:
    path = project.profiles_dir / f"{name}.yaml"
    path.write_text("# hand-written comment\n" + yaml.safe_dump(document, sort_keys=False),
                    encoding="utf-8")
    return path


VIRIDIPLANTAE_COVERAGE = {
    "kind": "taxonomy_coverage",
    "version": 1,
    "description": "editor probe",
    "taxonomy": {"source": "NCBI"},
    "scope": {"root_taxids": [33090]},
    "targets": {"ranks": ["family", "genus"]},
    "filters": {
        "exclude_extinct": True,
        "exclude_subtrees": [9903],
        "exclude_name_patterns": [r"(?i)^unclassified(?:\s|$)"],
    },
    "thresholds": {
        "family": {"min_coverage_percent": 80},
        "genus": {"min_coverage_percent": 70},
    },
}


def test_coverage_ranks_match_the_core() -> None:
    """The form's rank list is the core grammar's (module comment)."""
    from operon.taxonomy import TARGET_RANK_ORDER
    from operon.tui.screens.config_coverage import COVERAGE_RANKS

    assert set(COVERAGE_RANKS) == set(TARGET_RANK_ORDER)


def test_coverage_form_supported_accepts_the_flat_grammar() -> None:
    document = json.loads(json.dumps(VIRIDIPLANTAE_COVERAGE))
    assert coverage_form_supported(document)[0]

    document["taxonomy"]["extra_key"] = {"kept": True}  # unknown keys preserved
    assert coverage_form_supported(document)[0]
    del document["filters"]                             # optional section absent
    assert coverage_form_supported(document)[0]
    document["targets"]["ranks"] = ["genus"]            # a single rank
    del document["thresholds"]["family"]
    assert coverage_form_supported(document)[0]


@pytest.mark.parametrize("mutate,fragment", [
    (lambda d: d.__setitem__("taxonomy", "NCBI"), "taxonomy is not a mapping"),
    (lambda d: d.__setitem__("scope", []), "scope is not a mapping"),
    (lambda d: d["scope"].__setitem__("root_taxids", "33090"),
     "root_taxids is not a list"),
    (lambda d: d.__setitem__("targets", []), "targets is not a mapping"),
    (lambda d: d["targets"].__setitem__("ranks", ["species"]), "family/genus"),
    (lambda d: d.__setitem__("filters", []), "filters is not a mapping"),
    (lambda d: d["filters"].__setitem__("exclude_subtrees", 9903),
     "exclude_subtrees is not a list"),
    (lambda d: d["thresholds"].__setitem__("species", {"min_coverage_percent": 50}),
     "not a configured target rank"),
    (lambda d: d.__setitem__("thresholds", []), "thresholds is not a mapping"),
    (lambda d: d["thresholds"].__setitem__("family", 80), "not a mapping"),
    (lambda d: d["filters"].__setitem__("exclude_name_patterns", "[x]"),
     "list of strings"),
    (lambda d: d["filters"].__setitem__("exclude_name_patterns", ["x", 1]),
     "list of strings"),
])
def test_coverage_form_supported_refuses_what_it_cannot_represent(
        mutate, fragment: str) -> None:
    document = json.loads(json.dumps(VIRIDIPLANTAE_COVERAGE))
    mutate(document)
    supported, reason = coverage_form_supported(document)
    assert not supported
    assert fragment in reason


def test_coverage_profile_data_layer(project: Project) -> None:
    _write_coverage_profile(project, "cov_editor_probe", VIRIDIPLANTAE_COVERAGE)

    assert "cov_editor_probe" not in [
        row["name"] for row in data.list_qc_profiles(project)]
    listed = {row["name"]: row for row in data.list_coverage_profiles(project)}
    # The demo project already ships the coverage_viridiplantae_v1 example.
    assert listed["cov_editor_probe"]["kind"] == "taxonomy_coverage"
    assert listed["cov_editor_probe"]["version"] == 1
    document = data.get_profile_document(project, "cov_editor_probe",
                                         kind="taxonomy_coverage")
    assert document["scope"]["root_taxids"] == [33090]
    with pytest.raises(ValidationError):
        data.get_profile_document(project, "cov_editor_probe")  # qc is the default


def test_save_coverage_profile_versions_and_validation(project: Project) -> None:
    def fresh() -> dict:
        return json.loads(json.dumps(VIRIDIPLANTAE_COVERAGE))

    result = actions.save_coverage_profile(project, "cov_saved", fresh())
    assert result["version"] == 1 and result["snapshot_id"] is not None
    text = (project.profiles_dir / "cov_saved.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Operon taxonomy_coverage profile cov_saved")
    load_profile(project.profiles_dir, "cov_saved", expected_kind="taxonomy_coverage")
    snapshot = _query(
        project,
        "SELECT profile_document FROM qc_profiles WHERE profile_name='cov_saved'",
    )
    assert json.loads(snapshot[0]["profile_document"])["kind"] == "taxonomy_coverage"

    unchanged = actions.save_coverage_profile(project, "cov_saved", fresh())
    assert unchanged["unchanged"] is True and unchanged["version"] == 1
    changed = fresh()
    changed["description"] = "changed"
    assert actions.save_coverage_profile(project, "cov_saved", changed)["version"] == 2

    broken = fresh()
    broken["scope"]["root_taxids"] = []
    with pytest.raises(ValidationError, match="root_taxids must be a non-empty list"):
        actions.save_coverage_profile(project, "broken", broken)
    broken = fresh()
    broken["thresholds"]["family"]["min_coverage_percent"] = 101
    with pytest.raises(ValidationError, match="between 0 and 100"):
        actions.save_coverage_profile(project, "broken", broken)
    broken = fresh()
    broken["targets"]["ranks"] = ["species"]
    with pytest.raises(ValidationError, match="family, genus"):
        actions.save_coverage_profile(project, "broken", broken)
    broken = fresh()
    broken["name"] = "someone_else"
    with pytest.raises(ValidationError, match="does not match filename"):
        actions.save_coverage_profile(project, "broken", broken)
    broken = fresh()
    broken["filters"]["exclude_name_patterns"] = ["(["]
    with pytest.raises(ValidationError, match="invalid coverage exclusion"):
        actions.save_coverage_profile(project, "broken", broken)

    with pytest.raises(ValidationError, match="only kind 'qc'"):
        actions.save_profile(project, "cov_saved", fresh())
    with pytest.raises(ValidationError, match="on-disk kind"):
        actions.save_coverage_profile(project, "assembly_production_v1", fresh())

    # The M4 refusal is gone: the kind is accepted and validated by the core.
    accepted = actions.save_profile(
        project, "cov_via_generic", fresh(), kind="taxonomy_coverage")
    assert accepted["version"] == 1


def test_config_coverage_editor_end_to_end(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            list_view = panel.query_one("#profiles-list", ListView)
            labels = [item.query_one(Label).render().plain for item in list_view.children]
            assert any("coverage_viridiplantae_v1" in label and "coverage" in label
                       for label in labels)

            panel._load_profile("coverage_viridiplantae_v1")
            await pilot.pause()
            await _await_form_ready(pilot, panel)
            assert panel.query_one("#coverage-editor").display
            assert not panel.query_one("#profile-editor").display
            assert not panel.query_one("#classification-editor").display
            assert panel.coverage_profile == "coverage_viridiplantae_v1"
            assert _static_text(panel.query_one("#coverage-readonly-note", Static)) == ""
            assert panel.query_one("#coverage-root-taxids", Input).value == "33090"
            assert panel.query_one("#coverage-rank-family", Checkbox).value is True
            assert panel.query_one("#coverage-rank-genus", Checkbox).value is True
            assert panel.query_one("#coverage-exclude-extinct", Checkbox).value is True
            assert panel.query_one("#coverage-threshold-family", Input).value == "80"
            assert panel.query_one("#coverage-taxonomy-source", Select).value == "NCBI"

            panel.query_one("#coverage-threshold-genus", Input).value = "75"
            await _click(pilot, "#coverage-save")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, CoverageSaveModal)
            assert "version 2" in _static_text(modal.query_one("#modal-command", Static))
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, CoverageSaveModal)
            await _await_notification(pilot, app, "saved coverage_viridiplantae_v1 version 2")

    _run(scenario())
    loaded = load_profile(project.profiles_dir, "coverage_viridiplantae_v1",
                          expected_kind="taxonomy_coverage")
    assert loaded["version"] == 2
    assert loaded["thresholds"]["genus"]["min_coverage_percent"] == 75
    rows = _query(
        project,
        "SELECT profile_version FROM qc_profiles "
        "WHERE profile_name='coverage_viridiplantae_v1'",
    )
    assert [row["profile_version"] for row in rows] == [2]


def test_config_coverage_save_of_an_unchanged_document_keeps_the_version(
    project: Project,
) -> None:
    """Saving a form that composes the file it came from keeps the version."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("coverage_viridiplantae_v1")
            await pilot.pause()
            await _await_form_ready(pilot, panel)

            await _click(pilot, "#coverage-save")
            await pilot.pause()
            assert isinstance(app.screen, CoverageSaveModal)
            await _click(pilot, "#confirm")
            await _await_notification(
                pilot, app, "coverage_viridiplantae_v1: unchanged — version 1 kept"
            )
            assert data.get_profile_document(
                project, "coverage_viridiplantae_v1", kind="taxonomy_coverage"
            )["version"] == 1

    _run(scenario())


def test_config_coverage_editor_guards_and_readonly(project: Project) -> None:
    document = json.loads(json.dumps(VIRIDIPLANTAE_COVERAGE))
    document["thresholds"]["species"] = {"min_coverage_percent": 50}
    path = _write_coverage_profile(project, "cov_unsupported", document)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("cov_unsupported")
            await pilot.pause()
            await _await_form_ready(pilot, panel)
            note = _static_text(panel.query_one("#coverage-readonly-note", Static))
            assert "species" in note and "edit the YAML file" in note
            assert panel.query_one("#coverage-save", Button).disabled

    _run(scenario())
    # The visit must not rewrite a profile the form cannot represent.
    assert "hand-written comment" in path.read_text(encoding="utf-8")


def test_config_coverage_editor_inline_value_guards(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("coverage_viridiplantae_v1")
            await pilot.pause()
            await _await_form_ready(pilot, panel)

            panel.query_one("#coverage-threshold-family", Input).value = ""
            await _click(pilot, "#coverage-save")
            await pilot.pause()
            assert "missing minimum coverage" in _static_text(
                panel.query_one("#coverage-save-error", Static))
            assert not isinstance(app.screen, CoverageSaveModal)

            panel.query_one("#coverage-threshold-family", Input).value = "80"
            panel.query_one("#coverage-root-taxids", Input).value = "33090, abc"
            await _click(pilot, "#coverage-save")
            await pilot.pause()
            assert "must be positive integers" in _static_text(
                panel.query_one("#coverage-save-error", Static))

    _run(scenario())


def test_config_screen_new_coverage_profile(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _click(pilot, "#profile-new")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, NewProfileModal)
            modal.query_one("#new-profile-name", Input).value = "cov_new_v1"
            modal.query_one("#new-profile-kind", Select).value = actions.COVERAGE_KIND
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _await_form_ready(pilot, panel)
            assert panel.query_one("#coverage-editor").display
            heading = _static_text(panel.query_one("#coverage-heading", Static))
            assert "cov_new_v1" in heading and "new profile (not saved yet)" in heading
            assert panel.query_one("#coverage-root-taxids", Input).value == "1"
            assert panel.query_one("#coverage-threshold-family", Input).value == "80"

            panel.query_one("#coverage-root-taxids", Input).value = "33090"
            await _click(pilot, "#coverage-save")
            await pilot.pause()
            assert isinstance(app.screen, CoverageSaveModal)
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            await _await_notification(pilot, app, "saved cov_new_v1 version 1")

    _run(scenario())
    loaded = load_profile(project.profiles_dir, "cov_new_v1",
                          expected_kind="taxonomy_coverage")
    assert loaded["version"] == 1
    assert loaded["scope"]["root_taxids"] == [33090]


def test_config_coverage_history_restore_into_editor(project: Project) -> None:
    document = json.loads(json.dumps(VIRIDIPLANTAE_COVERAGE))
    actions.save_coverage_profile(project, "cov_hist", document)
    document["description"] = "second version"
    actions.save_coverage_profile(project, "cov_hist", document)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            panel._load_profile("cov_hist")
            await pilot.pause()
            await _click(pilot, "#coverage-history")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, HistoryModal)
            assert "cov_hist" in _static_text(modal.query_one("#modal-title", Static))
            table = modal.query_one("#history-table", DataTable)
            assert table.row_count == 2
            table.move_cursor(row=0, animate=False)
            await pilot.pause()
            await _click(pilot, "#restore")
            await pilot.pause()
            await _await_form_ready(pilot, panel)
            assert panel.query_one("#coverage-description", Input).value == "editor probe"
            heading = _static_text(panel.query_one("#coverage-heading", Static))
            assert "restored from snapshot" in heading

    _run(scenario())
    # Restore only loads the editor; nothing is written until Save.
    assert load_profile(project.profiles_dir, "cov_hist",
                        expected_kind="taxonomy_coverage")["description"] == \
        "second version"


# ---------------------------------------------------------------------------
# Recipe `commands` chains (P9a): structured steps, mutual exclusion
# ---------------------------------------------------------------------------


def _add_commands_recipe_probe(project: Project) -> None:
    """Install a hand-written recipe whose steps are a commands chain."""
    path = project.tools_config_path
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["tools"]["blastn"]["recipes"]["chain_probe"] = {
        "description": "two-step chain",
        "file_role": "genome_fasta",
        "format": "fasta",
        "commands": [
            {
                "arguments": ["blastn", "-query", "${input}"],
                "version_args": ["-version"],
                "version_pattern": r"blastn\s+([^\s]+)",
            },
            {"arguments": ["makeblastdb", "-in", "${output}"]},
        ],
    }
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _command_rows(panel: ConfigPanel) -> list[CommandRow]:
    return list(panel.query(CommandRow).results(CommandRow))


def test_config_screen_commands_chain_loads_and_saves(project: Project) -> None:
    _add_commands_recipe_probe(project)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            panel._load_recipe("chain_probe")
            await _wait_until(lambda: len(_command_rows(panel)) == 2, "the two steps")

            first, second = _command_rows(panel)
            assert "step 1  (logical owner)" == _static_text(
                first.query_one(".command-title", Static))
            assert "step 2" == _static_text(second.query_one(".command-title", Static))
            assert first.query_one(".command-arguments", TextArea).text.splitlines() == [
                "blastn", "-query", "${input}",
            ]
            assert first.query_one(".command-version-args", Input).value == "-version"
            assert first.query_one(".command-version-pattern", Input).value == r"blastn\s+([^\s]+)"
            assert second.query_one(".command-version-args", Input).value == ""
            note = _static_text(panel.query_one("#recipe-command-note", Static))
            assert "2 step(s)" in note and "mutually exclusive" not in note

            second.query_one(".command-arguments", TextArea).text = (
                "makeblastdb\n-in\n${output}\n-out\n${output}.db"
            )
            await pilot.pause()
            panel.query_one("#recipe-editor", VerticalScroll).scroll_end(animate=False)
            await pilot.pause()
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            assert isinstance(app.screen, RecipeSaveModal)
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()
            assert not isinstance(app.screen, RecipeSaveModal)

    _run(scenario())
    recipe = get_recipe(project, "chain_probe")
    assert recipe.version == 2
    assert [list(step.arguments) for step in recipe.commands] == [
        ["blastn", "-query", "${input}"],
        ["makeblastdb", "-in", "${output}", "-out", "${output}.db"],
    ]
    assert recipe.commands[0].version_args == ["-version"]
    assert recipe.commands[1].version_args is None
    assert recipe.arguments == []


def test_config_screen_commands_chain_validation_and_step_rows(project: Project) -> None:
    _add_commands_recipe_probe(project)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            panel._load_recipe("chain_probe")
            await _wait_until(lambda: len(_command_rows(panel)) == 2, "the two steps")

            # A new step starts empty: the save refuses with the core's wording.
            await _click(pilot, "#recipe-add-command")
            await _wait_until(lambda: len(_command_rows(panel)) == 3, "the third step")
            panel.query_one("#recipe-editor", VerticalScroll).scroll_end(animate=False)
            await pilot.pause()
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            assert "commands block 3 requires a non-empty 'arguments' list" in _static_text(
                panel.query_one("#recipe-save-error", Static))
            assert not isinstance(app.screen, RecipeSaveModal)

            # Fill it in, then combine the chain with `arguments`: refused, and
            # the note says so before the save is even attempted.
            _command_rows(panel)[2].query_one(".command-arguments", TextArea).text = "samtools\nview"
            panel.query_one("#recipe-arguments", TextArea).text = "-out\nx"
            await pilot.pause()
            assert "mutually exclusive" in _static_text(
                panel.query_one("#recipe-command-note", Static))
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            assert "'commands' and 'arguments' are mutually exclusive" in _static_text(
                panel.query_one("#recipe-save-error", Static))
            assert not isinstance(app.screen, RecipeSaveModal)

            # A version_pattern without version_args is refused too.
            panel.query_one("#recipe-arguments", TextArea).text = ""
            _command_rows(panel)[2].query_one(".command-version-pattern", Input).value = "v([0-9.]+)"
            await pilot.pause()
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            assert "commands block 3 version_pattern requires version_args" in _static_text(
                panel.query_one("#recipe-save-error", Static))
            assert not isinstance(app.screen, RecipeSaveModal)
            _command_rows(panel)[2].query_one(".command-version-pattern", Input).value = ""

            # Loading a chainless recipe clears the previous chain's rows.
            panel._load_recipe("blastn_nt")
            await _wait_until(lambda: len(_command_rows(panel)) == 0, "the chain to clear")
            assert _static_text(panel.query_one("#recipe-command-note", Static)) == ""
            panel._load_recipe("chain_probe")
            await _wait_until(lambda: len(_command_rows(panel)) == 2, "the chain back")

            # Drop the first step: the survivor is renumbered and inherits the
            # owner label, and the note follows the chain the form now holds.
            await pilot.pause()
            _command_rows(panel)[0].query_one(".command-remove", Button).press()
            await _wait_until(lambda: len(_command_rows(panel)) == 1, "the step to go")
            rows = _command_rows(panel)
            assert "step 1  (logical owner)" == _static_text(
                rows[0].query_one(".command-title", Static))
            assert "makeblastdb" in rows[0].query_one(".command-arguments", TextArea).text
            assert "1 step(s)" in _static_text(panel.query_one("#recipe-command-note", Static))

    _run(scenario())
    # Nothing was written: every refusal left the file at version 1.
    assert get_recipe(project, "chain_probe").version == 1


def test_config_screen_recipe_output_name_roundtrip(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            panel = await _open_config(app, pilot)
            await _open_tools_tab(panel, pilot)
            panel._load_recipe("blastn_nt")
            await pilot.pause()
            assert panel.query_one("#recipe-output-name", Input).value == ""

            panel.query_one("#recipe-output-name", Input).value = "${file_id}.blast.tsv"
            await pilot.pause()
            panel.query_one("#recipe-editor", VerticalScroll).scroll_end(animate=False)
            await pilot.pause()
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            assert isinstance(app.screen, RecipeSaveModal)
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()

            # Clearing it keeps the key with an empty value: a key the recipe
            # declared is never silently dropped (unlike `max_hits_per_query`,
            # which is documented to restore the core default).
            panel._load_recipe("blastn_nt")
            await pilot.pause()
            assert panel.query_one("#recipe-output-name", Input).value == "${file_id}.blast.tsv"
            panel.query_one("#recipe-output-name", Input).value = ""
            await pilot.pause()
            await _click(pilot, "#recipe-save")
            await pilot.pause()
            assert isinstance(app.screen, RecipeSaveModal)
            await _click(pilot, "#confirm")
            await pilot.pause()
            await _settled(app)
            await pilot.pause()

    _run(scenario())
    recipe = get_recipe(project, "blastn_nt")
    assert recipe.version == 3
    assert recipe.output_name_template == ""
    raw = yaml.safe_load(project.tools_config_path.read_text(encoding="utf-8"))
    assert raw["tools"]["blastn"]["recipes"]["blastn_nt"]["output_name"] == ""
    assert [row["version"] for row in data.recipe_history(project, "blastn_nt")] == [2, 3]
