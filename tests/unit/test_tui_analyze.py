"""Analyze modal and actions.run_analysis: recipe execution through the TUI."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("textual")

import yaml
from rich.text import Text
from textual.widgets import (
    Button,
    Checkbox,
    ContentSwitcher,
    Input,
    Select,
    Static,
    TabbedContent,
)
from textual.pilot import OutOfBounds

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ValidationError
from operon.tui import actions
from operon.tui.app import OperonApp
from operon.tui.screens import analyze as analyze_module
from operon.tui.screens.analyze import AnalyzeModal
from operon.tui.screens.common import ErrorDialog
from operon.tui.screens.config import ConfigPanel


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-analyze-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    """Each test gets its own copy of the demo project."""
    target = tmp_path / "project"
    shutil.copytree(demo_template.root, target)
    return Project.find(target)


SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 30.0


def _run(coroutine) -> None:
    """Drive a Textual headless scenario without requiring pytest-asyncio."""
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


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


def _screen_text(screen, selector: str) -> str:
    """Text of the first match, or "" while the widget is still mounting."""
    matches = screen.query(selector)
    return _static_text(matches.first()) if matches else ""


def _notifications(app) -> list[tuple[str, str]]:
    return [(notification.severity, notification.message) for notification in app._notifications]


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



def _query(project: Project, sql: str, params: tuple = ()) -> list[dict]:
    db = Database(project.db_path, read_only=True)
    try:
        return [dict(row) for row in db.query(sql, params)]
    finally:
        db.close()


def _write_fake_tool(project: Project, tmp_path: Path,
                     *, slurm: dict | None = None) -> None:
    """Install a runnable fake BLAST-style tool plus a broken one.

    ``fake_nt`` targets the demo's three assembly genome_fasta files and
    declares one choices parameter (``mode``) and one required parameter
    (``marker``); ``fake_broken`` points at a missing executable so every
    file fails.  ``slurm`` adds a recipe-level ``slurm:`` block (e.g.
    ``{"array": True}``) exactly like a hand-written recipe.
    """
    script = tmp_path / "fakeblast.py"
    script.write_text(textwrap.dedent("""
        import sys
        args = sys.argv[1:]
        if '-version' in args:
            print('fakeblast: 9.8.7')
            raise SystemExit(0)
        out = args[args.index('-out') + 1]
        with open(out, 'w') as handle:
            handle.write('q1\\ts1\\t99.0\\t100\\t1e-10\\t500\\n')
    """).strip(), encoding="utf-8")
    config = {
        "version": 1,
        "tools": {
            "fakeblast": {
                "executable": str(script),
                "run_method": sys.executable,
                "version_args": ["-version"],
                "version_pattern": r"fakeblast:\s*([^\s]+)",
                "recipes": {
                    "fake_nt": {
                        "description": "fake recipe",
                        "entity_type": "assembly",
                        "file_role": "genome_fasta",
                        "format": "fasta",
                        "output_subdir": "fake_nt",
                        "output_suffix": ".out.tsv",
                        "arguments": [
                            "-query", "${input}", "-out", "${output}",
                            "-num_threads", "${threads}",
                        ],
                        "parameters": {
                            "mode": {"choices": ["fast", "sensitive"], "default": "fast"},
                            "marker": {"required": True},
                        },
                        "result_parser": "none",
                        **({"slurm": dict(slurm)} if slurm else {}),
                    },
                },
            },
            "brokentool": {
                "executable": str(tmp_path / "definitely-not-there"),
                "run_method": "",
                "version_args": ["--version"],
                "version_pattern": r"([0-9.]+)",
                "recipes": {
                    "fake_broken": {
                        "description": "broken recipe",
                        "entity_type": "assembly",
                        "file_role": "genome_fasta",
                        "format": "fasta",
                        "output_subdir": "fake_broken",
                        "output_suffix": ".out.tsv",
                        "arguments": ["-query", "${input}", "-out", "${output}"],
                        "result_parser": "none",
                    },
                },
            },
        },
    }
    project.tools_config_path.write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# actions.run_analysis
# ---------------------------------------------------------------------------


def test_run_analysis_executes_records_and_caches(project: Project, tmp_path: Path) -> None:
    _write_fake_tool(project, tmp_path)
    result = actions.run_analysis(project, "fake_nt", parameters={"marker": "x"})
    assert result["analysis"] == "fake_nt"
    assert result["dry_run"] is False
    assert result["total"] == 3  # the demo's three assembly genome_fasta files
    assert result["succeeded"] == 3
    assert result["errors"] == 0
    assert {row["status"] for row in result["results"]} == {"completed"}

    jobs = _query(project, "SELECT * FROM analysis_jobs WHERE analysis_name='fake_nt'")
    assert len(jobs) == 3
    assert {job["status"] for job in jobs} == {"completed"}
    assert {job["tool_version"] for job in jobs} == {"9.8.7"}
    runs = _query(project, "SELECT * FROM workflow_runs WHERE step='analysis:fake_nt'")
    assert len(runs) == 3
    snapshots = _query(project, "SELECT * FROM recipe_snapshots WHERE recipe_name='fake_nt'")
    assert len(snapshots) == 1  # content-addressed: three runs share one snapshot

    # An identical second run reuses the cache instead of executing again.
    cached = actions.run_analysis(project, "fake_nt", parameters={"marker": "x"})
    assert {row["status"] for row in cached["results"]} == {"cached"}
    assert cached["succeeded"] == 3
    assert _query(
        project, "SELECT COUNT(*) AS n FROM analysis_jobs WHERE analysis_name='fake_nt'"
    )[0]["n"] == 3

    # --dry-run plans without executing or writing.
    planned = actions.run_analysis(
        project, "fake_nt", parameters={"marker": "x"}, dry_run=True, force=True,
    )
    assert planned["dry_run"] is True
    assert {row["status"] for row in planned["results"]} == {"planned"}
    assert _query(
        project, "SELECT COUNT(*) AS n FROM analysis_jobs WHERE analysis_name='fake_nt'"
    )[0]["n"] == 3


def test_run_analysis_parameter_validation_writes_nothing(project: Project, tmp_path: Path) -> None:
    _write_fake_tool(project, tmp_path)
    with pytest.raises(ValidationError, match="must be one of"):
        actions.run_analysis(project, "fake_nt", parameters={"mode": "zz", "marker": "x"})
    with pytest.raises(ValidationError, match="missing required runtime parameter 'marker'"):
        actions.run_analysis(project, "fake_nt", parameters={"mode": "fast"})
    with pytest.raises(ValidationError, match="undeclared runtime parameter"):
        actions.run_analysis(project, "fake_nt", parameters={"marker": "x", "nope": "1"})
    with pytest.raises(ValidationError, match="limit must be a positive integer"):
        actions.run_analysis(project, "fake_nt", parameters={"marker": "x"}, limit=0)
    with pytest.raises(ValidationError, match="threads must be a positive integer"):
        actions.run_analysis(project, "fake_nt", parameters={"marker": "x"}, threads=-2)
    assert _query(project, "SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"] == 0


def test_run_analysis_captures_stdout(project: Project, tmp_path: Path, capsys) -> None:
    _write_fake_tool(project, tmp_path)
    result = actions.run_analysis(
        project, "fake_nt", entity_type="annotation", parameters={"marker": "x"},
    )
    assert result["total"] == 0
    assert result["results"] == []
    assert "no candidate files" in result["messages"]
    assert capsys.readouterr().out == ""  # core prints must not reach the terminal


def test_run_analysis_progress_and_error_counts(project: Project, tmp_path: Path) -> None:
    _write_fake_tool(project, tmp_path)
    seen: list[tuple[int, int, str, str]] = []
    result = actions.run_analysis(
        project, "fake_broken",
        progress=lambda index, total, file_id, phase: seen.append((index, total, file_id, phase)),
    )
    assert result["total"] == 3
    assert result["errors"] == 3
    assert result["succeeded"] == 0
    assert {row["status"] for row in result["results"]} == {"error"}
    assert (1, 3, result["results"][0]["file_id"], "start") in seen
    assert any(phase == "error" for _i, _t, _f, phase in seen)


def test_run_analysis_forwards_backend_and_cancel_event(project: Project, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    _write_fake_tool(project, tmp_path)
    received: dict[str, Any] = {}

    def fake_core(project_arg, db, analysis, **kwargs):
        received.update(kwargs)
        return []

    monkeypatch.setattr("operon.tools.run_analysis", fake_core)
    cancel_event = threading.Event()
    result = actions.run_analysis(
        project, "fake_nt", parameters={"marker": "x"}, backend="slurm",
        cancel_event=cancel_event,
    )
    assert received["cancel_event"] is cancel_event
    assert received["progress_callback"] is None
    assert received["backend"] == "slurm"
    assert result["total"] == 0

    # The CLI's --backend choices are enforced before the core is called.
    with pytest.raises(ValidationError, match="unknown execution backend 'kubernetes'"):
        actions.run_analysis(project, "fake_nt", backend="kubernetes")


# ---------------------------------------------------------------------------
# AnalyzeModal
# ---------------------------------------------------------------------------


def test_analyze_modal_command_text_tracks_controls(project: Project, tmp_path: Path) -> None:
    _write_fake_tool(project, tmp_path)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = AnalyzeModal(project)
            app.push_screen(modal)
            await pilot.pause()
            assert modal.command_text() == "operon analyze --analysis '…'"

            modal.query_one("#analyze-recipe", Select).value = "fake_nt"
            await _wait_until(
                lambda: bool(modal.query("#analyze-param-marker"))
                and modal.query_one("#analyze-param-mode", Select).value == "fast",
                "parameter controls mounted",
            )
            # The recipe declares entity_type assembly; it prefills the filter.
            assert modal.query_one("#analyze-entity-type", Select).value == "assembly"
            # choices -> Select prefilled with the default; required without
            # default -> empty Input, marked with a star.
            labels = [_static_text(w) for w in modal.query("#analyze-parameters Static")]
            assert "marker *" in labels

            modal.query_one("#analyze-param-marker", Input).value = "TT"
            modal.query_one("#analyze-entity-id", Input).value = "ASM_000001"
            modal.query_one("#analyze-limit", Input).value = "2"
            modal.query_one("#analyze-threads", Input).value = "4"
            modal.query_one("#analyze-dry-run", Checkbox).value = True
            await pilot.pause()
            command = modal.command_text()
            assert command.startswith("operon analyze --analysis fake_nt ")
            assert "--param mode=fast --param marker=TT" in command
            assert "--entity-type assembly --entity-id ASM_000001" in command
            assert "--limit 2 --threads 4 --dry-run" in command
            assert "--force" not in command

            modal.query_one("#analyze-dry-run", Checkbox).value = False
            modal.query_one("#analyze-force", Checkbox).value = True
            modal.query_one("#analyze-keep-partial", Checkbox).value = True
            await pilot.pause()
            command = modal.command_text()
            assert "--dry-run" not in command
            assert command.endswith("--force --keep-partial")

            # Unrelated widgets never rewrite the preview.
            modal.on_input_changed(Input.Changed(input=Input(id="other"), value="zzz"))
            modal.on_select_changed(Select.Changed(Select([("A", "a")], id="other"), "a"))
            modal.on_checkbox_changed(Checkbox.Changed(Checkbox("other", id="other-check"), True))
            # Clearing the recipe picker keeps the current recipe.
            modal.on_select_changed(Select.Changed(
                modal.query_one("#analyze-recipe", Select), Select.NULL))
            assert modal.command_text() == command

            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())


def test_analyze_modal_fixed_recipe_and_inline_validation(project: Project, tmp_path: Path) -> None:
    _write_fake_tool(project, tmp_path)
    dismissed: list[Any] = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = AnalyzeModal(project, recipe_name="fake_nt")
            app.push_screen(modal, dismissed.append)
            await pilot.pause()
            # Fixed recipe: no picker, and parameter controls render immediately.
            assert not modal.query("#analyze-recipe")
            await _wait_until(
                lambda: bool(modal.query("#analyze-param-marker")),
                "parameter controls mounted",
            )
            assert modal.command_text().startswith(
                "operon analyze --analysis fake_nt --param mode=fast"
            )

            modal.query_one("#analyze-limit", Input).value = "abc"
            modal.confirm()
            assert "limit must be a positive integer" in _static_text(
                modal.query_one("#modal-error", Static))
            modal.query_one("#analyze-limit", Input).value = ""
            modal.query_one("#analyze-threads", Input).value = "0"
            modal.confirm()
            assert "threads must be a positive integer" in _static_text(
                modal.query_one("#modal-error", Static))
            modal.query_one("#analyze-threads", Input).value = ""
            modal.confirm()
            assert "missing required runtime parameter 'marker'" in _static_text(
                modal.query_one("#modal-error", Static))
            assert not modal.running
            assert not modal.query_one("#confirm", Button).disabled
            assert dismissed == []
            await pilot.press("escape")
            await pilot.pause()

            # A fixed recipe that fails to load reports inline.
            broken = AnalyzeModal(project, recipe_name="no_such_recipe")
            app.push_screen(broken)
            await pilot.pause()
            assert "unknown analysis" in _static_text(broken.query_one("#modal-error", Static))
            await pilot.press("escape")
            await pilot.pause()

            # Without a recipe selection, confirm stays inline.
            picker = AnalyzeModal(project)
            app.push_screen(picker)
            await pilot.pause()
            picker.confirm()
            assert "select a recipe" in _static_text(picker.query_one("#modal-error", Static))
            # Parameter values tolerate controls that have not mounted yet.
            picker._param_names = ["ghost"]
            assert picker._parameter_values() == {}
            await _click(pilot, "#cancel")  # cancel button while idle dismisses
            await pilot.pause()
            assert app.screen is not picker

    _run(scenario())
    assert dismissed == [None]  # escape dismisses with no result


def test_analyze_modal_dry_run_plan_then_real_run(project: Project, tmp_path: Path) -> None:
    _write_fake_tool(project, tmp_path)
    dismissed: list[Any] = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = AnalyzeModal(project, recipe_name="fake_nt")
            app.push_screen(modal, dismissed.append)
            await pilot.pause()
            await _wait_until(
                lambda: bool(modal.query("#analyze-param-marker")),
                "parameter controls mounted",
            )
            modal.query_one("#analyze-param-marker", Input).value = "TT"
            modal.query_one("#analyze-dry-run", Checkbox).value = True
            modal.confirm()
            await _wait_until(
                lambda: "Uncheck dry-run" in _static_text(modal.query_one("#analyze-plan", Static)),
                "dry-run plan rendering",
            )
            # The plan lists every candidate file and the modal stays open.
            plan_text = _static_text(modal.query_one("#analyze-plan", Static))
            assert "dry-run plan" in plan_text
            assert plan_text.count("planned") == 3
            assert "ASM_000001" in plan_text
            assert app.screen is modal
            assert not modal.running
            assert not modal.query_one("#confirm", Button).disabled
            assert dismissed == []

            modal.query_one("#analyze-dry-run", Checkbox).value = False
            await pilot.pause()
            assert "--dry-run" not in modal.command_text()
            modal.confirm()
            await _wait_until(lambda: dismissed, "real-run dismissal")
            payload = dismissed[0]
            assert payload["total"] == 3
            assert payload["succeeded"] == 3
            assert payload["errors"] == 0
            assert any("analysis fake_nt: 3/3 succeeded" in message
                       for _, message in _notifications(app))

    _run(scenario())
    jobs = _query(project, "SELECT * FROM analysis_jobs WHERE analysis_name='fake_nt'")
    assert len(jobs) == 3


def test_analyze_modal_dry_run_no_candidates(project: Project, tmp_path: Path) -> None:
    _write_fake_tool(project, tmp_path)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = AnalyzeModal(project, recipe_name="fake_nt")
            app.push_screen(modal)
            await pilot.pause()
            await _wait_until(
                lambda: bool(modal.query("#analyze-param-marker")),
                "parameter controls mounted",
            )
            modal.query_one("#analyze-param-marker", Input).value = "TT"
            modal.query_one("#analyze-entity-type", Select).value = "annotation"
            modal.query_one("#analyze-dry-run", Checkbox).value = True
            modal.confirm()
            await _wait_until(
                lambda: "(no candidate files)" in _static_text(
                    modal.query_one("#analyze-plan", Static)),
                "empty dry-run plan",
            )
            plan_text = _static_text(modal.query_one("#analyze-plan", Static))
            assert "no candidate files for fake_nt" in plan_text  # captured stdout
            assert app.screen is modal
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())


def test_analyze_modal_cancel_and_failure_paths(project: Project, monkeypatch) -> None:
    """Cancelling a running batch reports partial progress; crashes stay inline."""
    released = threading.Event()
    dismissed: list[Any] = []

    def blocking_run(project_arg, analysis, *, progress=None, **kwargs):
        if not released.wait(10):
            raise AssertionError("test never released the analysis stub")
        progress(1, 2, "FIL_000001", "start")
        return {"results": [], "messages": "", "total": 0, "succeeded": 0,
                "errors": 0, "dry_run": False, "analysis": analysis}

    def failing_run(project_arg, analysis, *, progress=None, **kwargs):
        raise RuntimeError("analysis exploded")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            monkeypatch.setattr(analyze_module.actions, "run_analysis", blocking_run)
            modal = AnalyzeModal(project, recipe_name="blastn_nt")
            app.push_screen(modal, dismissed.append)
            await pilot.pause()
            modal.confirm()
            await pilot.pause()
            assert modal.running
            assert modal.query_one("#analyze-progress").display is True
            modal.confirm()  # a second confirm while running is a no-op
            assert modal.running

            modal.on_button_pressed(Button.Pressed(modal.query_one("#cancel", Button)))
            assert modal._worker.is_cancelled
            modal.action_cancel()  # a queued escape cancels again without dismissing
            assert app.screen is modal
            released.set()
            await _wait_until(lambda: dismissed, "cancelled analysis dismissal")
            assert dismissed == [{"cancelled": True, "done": 0, "total": 0}]
            assert any("analysis cancelled after 0/0 file(s)" in message
                       for _severity, message in _notifications(app))

            # The shared callback ignores empty/cancelled payloads.
            analyze_module.analysis_finished(app, None)
            analyze_module.analysis_finished(app, {"cancelled": True})
            await pilot.pause()
            assert not isinstance(app.screen, ErrorDialog)

            # Without a live worker, cancelling leaves the modal open.
            modal = AnalyzeModal(project, recipe_name="blastn_nt")
            app.push_screen(modal)
            await pilot.pause()
            modal.running = True
            modal._worker = None
            modal.on_button_pressed(Button.Pressed(modal.query_one("#cancel", Button)))
            modal.action_cancel()
            assert app.screen is modal
            modal.running = False
            await pilot.press("escape")
            await pilot.pause()

            monkeypatch.setattr(analyze_module.actions, "run_analysis", failing_run)
            failing = AnalyzeModal(project, recipe_name="blastn_nt")
            app.push_screen(failing)
            await pilot.pause()
            failing.confirm()
            await _wait_until(lambda: not failing.running, "analysis failure handling")
            assert "analysis exploded" in _static_text(failing.query_one("#modal-error", Static))
            assert not failing.query_one("#confirm", Button).disabled
            assert app.screen is failing

            # A worker completing after the app stopped is dropped silently.
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(type(app), "is_running", property(lambda self: False))
                patch.setattr("textual.worker.get_current_worker",
                              lambda: SimpleNamespace(is_cancelled=False))
                app.clear_notifications()
                AnalyzeModal._run_analysis.__wrapped__(failing)
            assert app.screen is failing
            assert _notifications(app) == []
            await pilot.press("escape")

    try:
        _run(scenario())
    finally:
        released.set()


# ---------------------------------------------------------------------------
# Entry points and result callback
# ---------------------------------------------------------------------------


def test_config_screen_run_analysis_entry(project: Project, tmp_path: Path) -> None:
    _write_fake_tool(project, tmp_path)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("config")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(ConfigPanel)
            panel.query_one("#config-tabs", TabbedContent).active = "tab-tools"
            await pilot.pause()
            # No recipe selected yet: the entry point stays disabled.
            assert panel.query_one("#recipe-run", Button).disabled

            panel._load_recipe("fake_nt")
            await pilot.pause()
            assert not panel.query_one("#recipe-run", Button).disabled
            await _click(pilot, "#recipe-run")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, AnalyzeModal)
            assert modal.fixed_recipe == "fake_nt"
            await _wait_until(
                lambda: bool(modal.query("#analyze-param-marker")),
                "parameter controls mounted",
            )
            modal.query_one("#analyze-param-marker", Input).value = "TT"
            modal.query_one("#analyze-threads", Input).value = "2"
            modal.confirm()
            await _wait_until(
                lambda: not isinstance(app.screen, AnalyzeModal), "analysis modal dismissed",
            )
            await _settled(app)
            # Success callback: no failures, all panels reloaded, Runs screen shown.
            assert not isinstance(app.screen, ErrorDialog)
            assert app.query_one("#main", ContentSwitcher).current == "runs"

    _run(scenario())
    assert _query(
        project, "SELECT COUNT(*) AS n FROM analysis_jobs WHERE analysis_name='fake_nt'"
    )[0]["n"] == 3


def test_analysis_failure_callback_shows_error_dialog(project: Project, tmp_path: Path) -> None:
    _write_fake_tool(project, tmp_path)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("runs")
            await pilot.pause()
            await _click(pilot, "#runs-new-analysis")
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, AnalyzeModal)
            assert modal.fixed_recipe is None
            modal.query_one("#analyze-recipe", Select).value = "fake_broken"
            await pilot.pause()
            modal.confirm()
            await _wait_until(
                lambda: "cannot launch" in _screen_text(app.screen, "#error-dialog-body"),
                "analysis error dialog",
            )
            assert isinstance(app.screen, ErrorDialog)
            assert "3 of 3 file(s) failed analysis" in _screen_text(
                app.screen, "#modal-title")
            await pilot.press("escape")
            await pilot.pause()
            assert app.query_one("#main", ContentSwitcher).current == "runs"
            assert any(severity == "warning" and "0/3 succeeded" in message
                       for severity, message in _notifications(app))

    _run(scenario())


def test_backend_select_helpers_before_mount(project: Project, tmp_path: Path) -> None:
    """Reading backend controls before the form mounts stays safe."""
    from operon.tui.screens.common import (
        backend_select_options,
        project_default_backend,
        selected_backend,
    )

    _write_fake_tool(project, tmp_path)
    modal = AnalyzeModal(project, recipe_name="fake_nt")
    assert selected_backend(modal, "analyze-backend") == ""
    assert selected_backend(modal, "external-backend") == ""
    assert modal._resolved_backend() == "local"  # the demo project's default
    modal._update_cancel_note()  # no-op: the note does not exist yet
    # The default option carries the project default in its label.
    assert project_default_backend(project) == "local"
    assert backend_select_options(project)[0] == ("project default (local)", "")
    assert [value for _label, value in backend_select_options(project)] == [
        "", "local", "slurm", "ssh",
    ]


# ---------------------------------------------------------------------------
# M2b: execution backend selection and scheduler-aware cancellation
# ---------------------------------------------------------------------------

# Captured at import time: individual tests narrow PATH, and the fake
# scheduler scripts still need the system directories (bash, grep, ...).
_SYSTEM_PATH = os.environ.get("PATH", "")

FAKE_SBATCH = """\
#!/usr/bin/env bash
# Fake sbatch: single jobs run synchronously (honoring --output/--error like
# Slurm); a job array is queued and only leaves the queue via the fake scancel.
script="${!#}"
out="$(grep -m1 '^#SBATCH --output=' "$script" | cut -d= -f2-)"
err="$(grep -m1 '^#SBATCH --error=' "$script" | cut -d= -f2-)"
if grep -q '^#SBATCH --array=' "$script"; then
  touch "${OPERON_FAKE_SLURM_STATE}/array-submitted"
  echo 4242
  exit 0
fi
bash "$script" > "$out" 2> "$err"
echo 4241
"""

FAKE_SQUEUE_EMPTY = """\
#!/usr/bin/env bash
# Fake squeue: the queue is always empty (every job finished).
exit 0
"""

FAKE_SQUEUE_HOLDING = """\
#!/usr/bin/env bash
# Fake squeue: only the array job (4242) waits in the queue, until the fake
# scancel records the cancellation.  The poll budget bounds a regression to a
# failing assertion instead of a hanging test.
for arg in "$@"; do
  if [ "$arg" = "4242" ]; then
    if [ -f "${OPERON_FAKE_SLURM_STATE}/cancelled" ]; then
      exit 0
    fi
    polls=$(( $(cat "${OPERON_FAKE_SLURM_STATE}/polls" 2>/dev/null || echo 0) + 1 ))
    echo "$polls" > "${OPERON_FAKE_SLURM_STATE}/polls"
    if [ "$polls" -gt 200 ]; then
      exit 0
    fi
    echo "4242 operon running"
    exit 0
  fi
done
exit 0
"""

FAKE_SCANCEL = """\
#!/usr/bin/env bash
# Fake scancel: record the cancelled job ids; squeue then reports it gone.
printf '%s\\n' "$@" >> "${OPERON_FAKE_SLURM_STATE}/scancel.log"
touch "${OPERON_FAKE_SLURM_STATE}/cancelled"
"""


def _install_fake_slurm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        *, holding: bool) -> Path:
    """Put fake sbatch/squeue/scancel binaries on PATH; returns the state dir.

    ``SlurmExecutor`` shells out to these names, so a PATH shim is what makes
    the real executor testable without a cluster.  With ``holding`` a
    submitted array stays queued (squeue reports it present) until scancel
    records the cancellation — the state a mid-array cancel has to deal with.
    """
    bin_dir = tmp_path / "fakebin"
    state = tmp_path / "slurm-state"
    bin_dir.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    scripts = {
        "sbatch": FAKE_SBATCH,
        "squeue": FAKE_SQUEUE_HOLDING if holding else FAKE_SQUEUE_EMPTY,
    }
    if holding:
        scripts["scancel"] = FAKE_SCANCEL
    for name, content in scripts.items():
        script = bin_dir / name
        script.write_text(content, encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("OPERON_FAKE_SLURM_STATE", str(state))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{_SYSTEM_PATH}")
    return state


def _empty_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PATH without any scheduler binary, for the preflight failure cases."""
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))


def test_preflight_backend_resolves_and_validates(project: Project, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    _empty_path(tmp_path, monkeypatch)

    assert actions.preflight_backend(project) == {"backend": "local", "description": "local"}
    assert actions.preflight_backend(project, "local")["backend"] == "local"
    with pytest.raises(ValidationError, match="unknown execution backend"):
        actions.preflight_backend(project, "kubernetes")
    with pytest.raises(ValidationError, match="requires execution.ssh.host"):
        actions.preflight_backend(project, "ssh")
    with pytest.raises(ValidationError, match="slurm backend requires 'sbatch' in PATH"):
        actions.preflight_backend(project, "slurm")

    _install_fake_slurm(tmp_path, monkeypatch, holding=False)
    assert actions.preflight_backend(project, "slurm") == {
        "backend": "slurm", "description": "slurm",
    }

    # Recipe-level slurm overrides are validated by the same code path the
    # core uses before a run starts.
    _write_fake_tool(project, tmp_path, slurm={"poll_interval": -1})
    with pytest.raises(ValidationError, match="poll_interval must be > 0"):
        actions.preflight_backend(project, "slurm", recipe_name="fake_nt")


def test_run_analysis_cancel_mid_array_scancels_and_interrupts(
        project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cooperative cancel while the job array is queued: one scancel, and
    the unfinished tasks land as ``interrupted`` (no workflow_runs rows)."""
    _write_fake_tool(project, tmp_path, slurm={"array": True})
    project.config["execution"]["slurm"]["poll_interval"] = 0.01
    state = _install_fake_slurm(tmp_path, monkeypatch, holding=True)

    cancel_event = threading.Event()

    def cancel_once_submitted() -> None:
        marker = state / "array-submitted"
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel_event.set()

    watcher = threading.Thread(target=cancel_once_submitted, daemon=True)
    watcher.start()
    try:
        with pytest.raises(actions.AnalysisCancelled):
            actions.run_analysis(
                project, "fake_nt", parameters={"marker": "TT"}, backend="slurm",
                cancel_event=cancel_event,
            )
    finally:
        watcher.join(timeout=5)

    # The array was cancelled with one scancel carrying its job id.
    assert (state / "scancel.log").read_text(encoding="utf-8").split() == ["4242"]
    jobs = _query(project, "SELECT * FROM analysis_jobs ORDER BY job_id")
    assert [job["status"] for job in jobs] == ["interrupted"] * 3
    assert _query(project, "SELECT * FROM workflow_runs WHERE step='analysis:fake_nt'") == []


def test_analyze_modal_backend_preflight_then_slurm_run(
        project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend select preflights inline, then runs on the real executor."""
    _write_fake_tool(project, tmp_path)
    _empty_path(tmp_path, monkeypatch)
    dismissed: list[Any] = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = AnalyzeModal(project, recipe_name="fake_nt")
            app.push_screen(modal, dismissed.append)
            await pilot.pause()
            await _wait_until(
                lambda: bool(modal.query("#analyze-param-marker")),
                "parameter controls mounted",
            )
            modal.query_one("#analyze-param-marker", Input).value = "TT"
            modal.query_one("#analyze-backend", Select).value = "slurm"
            await pilot.pause()
            assert "--backend slurm" in modal.command_text()
            assert "scancel" in _static_text(
                modal.query_one("#analyze-cancel-note", Static))

            # No sbatch in PATH: the preflight reports inline, no worker starts.
            modal.confirm()
            assert "slurm backend requires 'sbatch' in PATH" in _static_text(
                modal.query_one("#modal-error", Static))
            assert not modal.running
            assert not modal.query_one("#confirm", Button).disabled
            assert dismissed == []

            # With the scheduler commands available the same form runs on Slurm.
            _install_fake_slurm(tmp_path, monkeypatch, holding=False)
            modal.confirm()
            await _wait_until(lambda: dismissed, "slurm analysis dismissal")
            await _settled(app)

    _run(scenario())
    payload = dismissed[0]
    assert payload["total"] == 3 and payload["succeeded"] == 3
    runs = _query(project, "SELECT * FROM workflow_runs WHERE step='analysis:fake_nt'")
    assert {run["executor"] for run in runs} == {"slurm"}
    assert {run["scheduler_job_id"] for run in runs} == {"4241"}
    jobs = _query(project, "SELECT * FROM analysis_jobs ORDER BY job_id")
    assert {job["status"] for job in jobs} == {"completed"}


def test_analyze_modal_cancel_mid_array_scancels_and_reports(
        project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel while the array is queued: scancel runs, the modal reports the
    cancelled batch, and tasks land as interrupted."""
    _write_fake_tool(project, tmp_path, slurm={"array": True})
    project.config["execution"]["slurm"]["poll_interval"] = 0.01
    state = _install_fake_slurm(tmp_path, monkeypatch, holding=True)
    dismissed: list[Any] = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = AnalyzeModal(project, recipe_name="fake_nt")
            app.push_screen(modal, dismissed.append)
            await pilot.pause()
            await _wait_until(
                lambda: bool(modal.query("#analyze-param-marker")),
                "parameter controls mounted",
            )
            modal.query_one("#analyze-param-marker", Input).value = "TT"
            modal.query_one("#analyze-backend", Select).value = "slurm"
            await pilot.pause()
            modal.confirm()
            await _wait_until(
                lambda: (state / "array-submitted").exists(),
                "job array submission", timeout=30,
            )
            assert modal.running

            modal.on_button_pressed(Button.Pressed(modal.query_one("#cancel", Button)))
            assert "cancelling…" in _static_text(
                modal.query_one("#analyze-status", Static))
            await _wait_until(lambda: dismissed, "cancelled analysis dismissal", timeout=30)
            assert any("analysis cancelled after" in message
                       for _severity, message in _notifications(app))
            await _settled(app)

    _run(scenario())
    assert (state / "scancel.log").read_text(encoding="utf-8").split() == ["4242"]
    assert dismissed == [{"cancelled": True, "done": 3, "total": 3}]
    jobs = _query(project, "SELECT * FROM analysis_jobs ORDER BY job_id")
    assert [job["status"] for job in jobs] == ["interrupted"] * 3
    assert _query(project, "SELECT * FROM workflow_runs WHERE step='analysis:fake_nt'") == []
