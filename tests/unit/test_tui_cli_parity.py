"""TUI/CLI parity tests (Layers 1–3).

Layer 1 — command coverage: every CLI leaf command appears exactly once in
``operon.tui.parity.REGISTRY``, every registry entry names a real CLI
command, and every entry is internally consistent (notes, resolvable
actions, importable modals).

Layer 2 — parameter mapping: for each ``implemented`` entry, every optional
CLI flag of the command is either mapped to a TUI widget/context in
``params`` or excused with a reason in ``waived``; stale keys that do not
correspond to a real CLI flag are errors.

Layer 3 — preview/call parity (pilot-driven): for exemplar modals, the
displayed equivalent command parses cleanly with the real CLI parser, and
the kwargs the modal passes to its ``actions.*`` function match the parsed
command exactly.

Layer 4 — audit-trail equivalence: the same operation runs once through the
CLI and once through the matching ``operon.tui.actions`` function on twin
demo projects; the ``changes`` and ``workflow_runs`` tables (plus
per-exemplar semantic side tables) must match after dropping volatile
fields.

Layers 1 and 2 are pure argparse introspection and run without Textual.
Layer 3 and the modal-resolution check need the ``tui`` extra and skip
without it.  Layer 4 is Textual-free.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import shutil
import sys
import textwrap
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from rich.text import Text

from operon.cli import main as cli_main
from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.tui import actions, parity
from operon.tui.parity import (
    REGISTRY,
    STATUS_CLI_ONLY,
    STATUS_IMPLEMENTED,
    STATUS_PLANNED,
    STATUSES,
)


def _cli_leaves() -> dict[tuple[str, ...], object]:
    return dict(parity.iter_cli_commands())


def _registry_commands() -> list[tuple[str, ...]]:
    return [entry.command for entry in REGISTRY]


# -- Layer 1: command coverage -----------------------------------------------


def test_every_cli_leaf_command_is_registered_exactly_once() -> None:
    leaves = _cli_leaves()
    registered = _registry_commands()
    duplicates = sorted({cmd for cmd in registered if registered.count(cmd) > 1})
    assert not duplicates, "duplicate TUI parity entries for: " + ", ".join(
        " ".join(cmd) for cmd in duplicates
    )
    missing = sorted(set(leaves) - set(registered))
    assert not missing, (
        "CLI command(s) without a TUI parity entry: "
        + ", ".join(" ".join(cmd) for cmd in missing)
        + " — implement the command in the TUI or register it as "
        "cli-only/planned with a reason in operon/tui/parity.py"
    )


def test_every_registry_entry_exists_in_the_cli_tree() -> None:
    leaves = _cli_leaves()
    stale = sorted(set(_registry_commands()) - set(leaves))
    assert not stale, (
        "parity registry entries with no matching CLI command (TUI ahead of "
        "CLI or stale entry): " + ", ".join(" ".join(cmd) for cmd in stale)
    )


def test_registry_covers_the_same_command_set_as_the_cli() -> None:
    assert set(_registry_commands()) == set(_cli_leaves())


def test_entry_statuses_are_valid() -> None:
    for entry in REGISTRY:
        assert entry.status in STATUSES, (
            f"{entry.command_text}: unknown status {entry.status!r}"
        )


def test_cli_only_and_planned_entries_have_a_reason() -> None:
    for entry in REGISTRY:
        if entry.status in (STATUS_CLI_ONLY, STATUS_PLANNED):
            assert entry.note.strip(), (
                f"{entry.command_text}: {entry.status} entries must record "
                "a reason / milestone in `note`"
            )


def test_implemented_entries_resolve_their_action() -> None:
    for entry in REGISTRY:
        if entry.status != STATUS_IMPLEMENTED:
            continue
        assert entry.actions, (
            f"{entry.command_text}: implemented entries must name an "
            "actions.* or data.* function"
        )
        assert entry.actions.split(".")[0] in ("actions", "data"), (
            f"{entry.command_text}: actions must reference the actions or "
            f"data module, got {entry.actions!r}"
        )
        assert callable(parity.resolve_action(entry)), (
            f"{entry.command_text}: {entry.actions} is not callable"
        )


def test_implemented_entries_resolve_their_modal() -> None:
    pytest.importorskip("textual")
    for entry in REGISTRY:
        if entry.status != STATUS_IMPLEMENTED or not entry.modal:
            continue
        modal = parity.resolve_modal(entry)
        assert isinstance(modal, type), (
            f"{entry.command_text}: {entry.modal} did not resolve to a class"
        )


def test_strict_mode_flags_planned_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    planned = [
        entry.command_text for entry in REGISTRY if entry.status == STATUS_PLANNED
    ]
    assert planned, "registry sanity check: expected at least one planned entry"

    monkeypatch.delenv("OPERON_PARITY_STRICT", raising=False)
    assert parity.strict_violations() == []

    monkeypatch.setenv("OPERON_PARITY_STRICT", "1")
    assert sorted(parity.strict_violations()) == sorted(planned)

    monkeypatch.setenv("OPERON_PARITY_STRICT", "0")
    assert parity.strict_violations() == []


# -- Layer 2: parameter mapping ------------------------------------------------
#
# Mapping convention (see operon/tui/parity.py): ``params``/``waived`` are
# keyed by argparse dest (``entity_type`` for ``--entity-type``).  Positional
# arguments are excluded — they arrive from screen context (selected row,
# wizard flow) rather than a flag — and so is ``-h/--help``, which is
# argparse plumbing with no TUI meaning.


def _implemented_entries() -> list[parity.ParityEntry]:
    return [entry for entry in REGISTRY if entry.status == STATUS_IMPLEMENTED]


def test_implemented_entries_cover_every_cli_flag() -> None:
    problems: list[str] = []
    for entry in _implemented_entries():
        options = parity.cli_options(entry.command)
        uncovered = sorted(set(options) - set(entry.params) - set(entry.waived))
        if uncovered:
            problems.append(
                f"{entry.command_text}: CLI flag(s) {uncovered} are neither "
                "mapped in params nor excused in waived"
            )
    assert not problems, "\n".join(problems)


def test_implemented_entries_have_no_stale_mapping_keys() -> None:
    problems: list[str] = []
    for entry in _implemented_entries():
        options = parity.cli_options(entry.command)
        stale_params = sorted(set(entry.params) - set(options))
        stale_waived = sorted(set(entry.waived) - set(options))
        if stale_params:
            problems.append(
                f"{entry.command_text}: params key(s) {stale_params} do not "
                "match any CLI flag dest"
            )
        if stale_waived:
            problems.append(
                f"{entry.command_text}: waived key(s) {stale_waived} do not "
                "match any CLI flag dest"
            )
    assert not problems, "\n".join(problems)


def test_waived_flags_have_reasons_and_params_have_targets() -> None:
    for entry in _implemented_entries():
        for dest, reason in entry.waived.items():
            assert reason.strip(), (
                f"{entry.command_text}: waived flag {dest!r} needs a reason"
            )
        for dest, target in entry.params.items():
            assert target.strip(), (
                f"{entry.command_text}: params flag {dest!r} needs a widget "
                "id or a 'context: …' target"
            )


# ---------------------------------------------------------------------------
# Layer 3: command_text() <-> actions call parity (pilot-driven)
# ---------------------------------------------------------------------------
#
# For each exemplar modal: fill the form, then assert (a) the displayed
# equivalent command parses cleanly with the real CLI parser and carries the
# form values (repeated flags item by item, omitted flags at CLI defaults),
# and (b) confirming passes exactly those parameters to the actions function
# (spied via monkeypatch, so nothing actually runs).  These tests need
# Textual; each skips without the ``tui`` extra.


def spy_action(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    result: object,
) -> list[tuple[tuple, dict]]:
    """Replace ``operon.tui.actions.<name>`` with a recording stub.

    Returns the list of captured ``(args, kwargs)`` calls.  ``result`` is
    handed back to the modal unchanged, so pass a payload shaped like the
    real action's return value.
    """
    calls: list[tuple[tuple, dict]] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(actions, name, stub)
    return calls


async def _push(pilot, modal, selector: str | None = None) -> None:
    """Push a screen and wait until its form **and** buttons have composed.

    ``push_screen`` mounts a modal in stages: querying a nested widget right
    after ``pilot.pause()`` can raise ``NoMatches``, and under the full suite's
    parallel workers a mount can occasionally take seconds of event-loop time.
    Wait for the Confirm button plus a form widget (or an explicit selector),
    and re-push once if the modal never mounted (guarded so a screen that is
    already on the stack is never pushed twice).
    """
    for attempt in range(2):
        if pilot.app.screen is not modal:
            pilot.app.push_screen(modal)

        def ready() -> bool:
            if len(modal.query("#confirm")) == 0:
                return False
            if selector is None:
                # Any composed form child means compose_form ran (ClassifyModal's
                # form holds Statics only, so looking for inputs is not enough).
                return bool(modal.query("#modal-form > *"))
            return len(modal.query(selector)) > 0

        try:
            await _wait_until(ready, f"{type(modal).__name__} to compose", timeout=10.0)
        except TimeoutError:
            if attempt:
                raise
            await pilot.pause()
            await asyncio.sleep(0.5)
            continue
        await pilot.pause()
        return



async def _q(modal, selector: str, *types):
    """Query a widget inside a modal, waiting until compose has created it.

    ``push_screen`` mounts a modal in stages; querying a nested widget right
    after ``pilot.pause()`` can raise ``NoMatches`` on a loaded machine.  This
    waits for the selector to resolve, then returns ``query_one``.
    """
    await _wait_until(lambda: len(modal.query(selector)) > 0, f"{selector} to exist")
    return modal.query_one(selector, *types)


def _static_text(widget) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


def parse_command_text(text: str):
    """Parse a modal's equivalent-command preview with the real CLI parser.

    The preview starts with the program name (``operon ...``), which is
    stripped before ``parse_args``.  A parse error raises ``SystemExit``
    and fails the test — the preview must always be a valid CLI invocation.
    """
    from operon.cli import _parser

    argv = shlex.split(text)
    assert argv and argv[0] == "operon", f"unexpected command preview: {text!r}"
    return _parser().parse_args(argv[1:])


# Headless scaffolding, copied from test_tui_analyze.py / test_tui_writes.py
# (per-file copies are the established convention for the TUI test suite).

SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 30.0


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
    predicate: Callable[[], bool],
    description: str,
    timeout: float = SETTLE_TIMEOUT,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not reach {description} within {timeout}s")
        await asyncio.sleep(0.05)


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-parity-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    """Each pilot test gets its own copy of the demo project."""
    target = tmp_path / "project"
    shutil.copytree(demo_template.root, target)
    return Project.find(target)


def _write_fake_tool(project: Project, tmp_path: Path) -> None:
    """Install a fake recipe with one choices parameter and one required one."""
    script = tmp_path / "fakeblast.py"
    script.write_text(
        textwrap.dedent("""
        import sys
        args = sys.argv[1:]
        if '-version' in args:
            print('fakeblast: 9.8.7')
            raise SystemExit(0)
        out = args[args.index('-out') + 1]
        with open(out, 'w') as handle:
            handle.write('q1\\ts1\\t99.0\\t100\\t1e-10\\t500\\n')
        """).strip(),
        encoding="utf-8",
    )
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
                            "-query",
                            "${input}",
                            "-out",
                            "${output}",
                            "-num_threads",
                            "${threads}",
                        ],
                        "parameters": {
                            "mode": {
                                "choices": ["fast", "sensitive"],
                                "default": "fast",
                            },
                            "marker": {"required": True},
                        },
                        "result_parser": "none",
                    },
                },
            },
        },
    }
    project.tools_config_path.write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )


def test_analyze_modal_command_text_matches_action_kwargs(
    project: Project,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Checkbox, Input, Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.analyze import AnalyzeModal

    _write_fake_tool(project, tmp_path)
    payload = {
        "analysis": "fake_nt",
        "dry_run": False,
        "results": [],
        "messages": "",
        "total": 0,
        "succeeded": 0,
        "errors": 0,
    }
    calls = spy_action(monkeypatch, "run_analysis", payload)
    # The backend preflight constructs a real executor — out of scope for a
    # preview/kwargs check; record the calls instead.
    preflights: list[tuple] = []
    monkeypatch.setattr(
        actions,
        "preflight_backend",
        lambda _project, backend=None, *, recipe_name=None: (
            preflights.append((backend, recipe_name))
            or {"backend": backend or "local", "description": backend or "local"}
        ),
    )
    dismissed: list = []

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
            modal.query_one("#analyze-param-mode", Select).value = "sensitive"
            modal.query_one("#analyze-param-marker", Input).value = "TT"
            modal.query_one("#analyze-entity-id", Input).value = "ASM_000001"
            modal.query_one("#analyze-limit", Input).value = "2"
            modal.query_one("#analyze-threads", Input).value = "4"
            modal.query_one("#analyze-force", Checkbox).value = True
            modal.query_one("#analyze-keep-partial", Checkbox).value = True
            await pilot.pause()

            # (a) the preview parses with the real CLI parser and carries the
            # form values, repeated --param entries item by item; the default
            # backend selection omits --backend exactly like the CLI default.
            assert modal.query_one("#analyze-backend", Select).value == ""
            ns = parse_command_text(modal.command_text())
            assert ns.analysis == "fake_nt"
            assert ns.param == ["mode=sensitive", "marker=TT"]
            assert ns.entity_type == "assembly"  # prefilled from the recipe
            assert ns.entity_id == "ASM_000001"
            assert ns.limit == 2
            assert ns.threads == 4
            assert ns.dry_run is False
            assert ns.force is True
            assert ns.keep_partial is True
            assert ns.backend is None  # project default

            # (b) confirming passes exactly the parsed parameters through.
            modal.confirm()
            await _wait_until(lambda: dismissed, "analysis modal dismissed")

            # (c) an explicit scheduler backend appears in the preview and
            # reaches the action unchanged.
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
            ns = parse_command_text(modal.command_text())
            assert ns.backend == "slurm"
            modal.confirm()
            await _wait_until(lambda: len(dismissed) == 2, "second analysis modal dismissed")

    _run(scenario())
    assert dismissed == [payload, payload]
    assert len(calls) == 2
    args, kwargs = calls[0]
    assert args[1] == "fake_nt"
    assert callable(kwargs.pop("progress"))
    assert isinstance(kwargs.pop("cancel_event"), threading.Event)
    assert kwargs == {
        "entity_type": "assembly",
        "entity_id": "ASM_000001",
        "limit": 2,
        "threads": 4,
        "backend": None,
        "dry_run": False,
        "force": True,
        "keep_partial": True,
        "parameters": {"mode": "sensitive", "marker": "TT"},
    }
    args, kwargs = calls[1]
    assert args[1] == "fake_nt"
    assert callable(kwargs.pop("progress"))
    assert isinstance(kwargs.pop("cancel_event"), threading.Event)
    assert kwargs["backend"] == "slurm"
    # The modal preflights the resolved backend before any worker starts.
    assert preflights == [(None, "fake_nt"), ("slurm", "fake_nt")]


def test_qc_modal_command_text_matches_action_kwargs(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Checkbox, Input, Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.files_ops import QcModal

    calls = spy_action(monkeypatch, "run_qc", [{"file_id": "FIL_000001", "ok": True}])
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(100, 30)) as pilot:
            await _settled(app)

            # Defaults: the preview shows no options, so the CLI parser's
            # defaults and the kwargs the worker receives must agree.
            modal = QcModal(project, "FIL_000001", 1)
            app.push_screen(modal, dismissed.append)
            await pilot.pause()
            ns = parse_command_text(modal.command_text())
            assert ns.file_id == "FIL_000001"
            assert (ns.sample_size, ns.phred_offset, ns.rehash) == (
                1000000,
                "33",
                False,
            )
            modal.confirm()
            await _wait_until(lambda: dismissed, "first qc modal dismissed")

            # Non-default options: every widget appears in the preview and in
            # the action call.
            modal = QcModal(project, "FIL_000001", 1)
            app.push_screen(modal, dismissed.append)
            await pilot.pause()
            modal.query_one("#qc-sample-size", Input).value = "500"
            modal.query_one("#qc-phred-offset", Select).value = "64"
            modal.query_one("#qc-rehash", Checkbox).value = True
            await pilot.pause()
            ns = parse_command_text(modal.command_text())
            assert (ns.sample_size, ns.phred_offset, ns.rehash) == (500, "64", True)
            modal.confirm()
            await _wait_until(lambda: len(dismissed) == 2, "second qc modal dismissed")

    _run(scenario())
    expected = [
        {
            "file_id": "FIL_000001",
            "sample_size": 1000000,
            "phred_offset": "33",
            "rehash": False,
        },
        {
            "file_id": "FIL_000001",
            "sample_size": 500,
            "phred_offset": "64",
            "rehash": True,
        },
    ]
    assert len(calls) == 2
    for (args, kwargs), want in zip(calls, expected, strict=True):
        assert callable(kwargs.pop("progress"))
        assert kwargs == want


def test_run_external_modal_command_text_matches_action_kwargs(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Checkbox, Input, Select  # noqa: F401

    from operon.tui.app import OperonApp
    from operon.tui.screens.run_external import RunExternalModal

    payload = {
        "run_id": "WF_0001",
        "step": "busco",
        "status": "completed",
        "exit_code": 0,
        "finished_at": "2026-09-18T12:00:00+08:00",
        "error": None,
        "stdout_file": "/tmp/stdout.log",
        "stderr_file": "/tmp/stderr.log",
        "messages": "",
    }
    calls = spy_action(monkeypatch, "run_external", payload)
    preflights: list = []
    monkeypatch.setattr(
        actions,
        "preflight_backend",
        lambda _project, backend=None, *, recipe_name=None: (
            preflights.append(backend)
            or {"backend": backend or "local", "description": backend or "local"}
        ),
    )
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = RunExternalModal(project)
            app.push_screen(modal, dismissed.append)
            await pilot.pause()
            # Empty form: the preview keeps the CLI shape with placeholders.
            assert modal.command_text() == "operon run-external --step '…' --command '…'"

            modal.query_one("#external-step", Input).value = "busco"
            modal.query_one("#external-command", Input).value = "busco -i in.fa -o out -m genome"
            modal.query_one("#external-entity-type", Select).value = "assembly"
            modal.query_one("#external-entity-id", Input).value = "ASM_000001"
            modal.query_one("#external-tool", Input).value = "busco"
            modal.query_one("#external-parameter-set", Input).value = "busco_v1"
            modal.query_one("#external-inputs", Input).value = "a.fa, b.fa"
            modal.query_one("#external-expected-outputs", Input).value = "out.tsv"
            modal.query_one("#external-threads", Input).value = "4"
            modal.query_one("#external-cwd", Input).value = "/tmp"
            modal.query_one("#external-timeout", Input).value = "30"
            modal.query_one("#external-backend", Select).value = "slurm"
            await pilot.pause()

            ns = parse_command_text(modal.command_text())
            assert ns.step == "busco"
            assert ns.command_line == "busco -i in.fa -o out -m genome"
            assert ns.entity_type == "assembly"
            assert ns.entity_id == "ASM_000001"
            assert ns.tool == "busco"
            assert ns.parameter_set == "busco_v1"
            assert ns.inputs == ["a.fa", "b.fa"]
            assert ns.expected_output == ["out.tsv"]
            assert ns.threads == 4
            assert ns.cwd == "/tmp"
            assert ns.timeout == 30.0
            assert ns.backend == "slurm"

            modal.confirm()
            await _wait_until(lambda: bool(dismissed), "run-external modal dismissed")

    _run(scenario())
    assert dismissed == [payload]
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1] == "busco"
    assert args[2] == "busco -i in.fa -o out -m genome"
    assert kwargs == {
        "entity_type": "assembly",
        "entity_id": "ASM_000001",
        "parameter_set": "busco_v1",
        "tool": "busco",
        "inputs": ["a.fa", "b.fa"],
        "expected_outputs": ["out.tsv"],
        "threads": 4,
        "cwd": "/tmp",
        "timeout": 30.0,
        "backend": "slurm",
    }
    # The modal preflights the resolved backend before any worker starts.
    assert preflights == ["slurm"]


# ---------------------------------------------------------------------------
# Layer 4: audit-trail equivalence (CLI vs TUI actions)
# ---------------------------------------------------------------------------
#
# Twin demo projects: the same operation runs once through the CLI
# (``operon.cli.main``) and once through the matching ``operon.tui.actions``
# function, then the audit trail — ``changes`` and ``workflow_runs``, plus
# per-exemplar semantic side tables — is compared as sorted row sets.
# Textual is not involved; these tests run anywhere.
#
# Volatile columns dropped before comparison, and why:
#   workflow_runs: run_id/parent_run_id/resumes_run_id (random time-derived
#     ids), started_at/finished_at (wall clock), duration_seconds/
#     cpu_seconds/max_rss_mb/avg_rss_mb (timings), execution_details
#     (embeds qc stage timings; the qc exemplar compares it separately with
#     the volatile subkeys stripped).
#   changes: change_id/reverts_change_id (surrogate keys), changed_at
#     (wall clock), workflow_run_id (FK to a random run id).
#   files: downloaded_at (wall clock).
#   decisions: decision_id/profile_snapshot_id (surrogate keys),
#     evaluated_at/curated_at (wall clock).
#   qc_profiles: profile_snapshot_id (surrogate key), recorded_at.
#   entity_state: updated_at (wall clock).
#   qc_results: qc_result_id (surrogate key), evaluated_at (wall clock).

AUDIT_TABLES = ("changes", "workflow_runs")

_AUDIT_DROP: dict[str, frozenset[str]] = {
    "workflow_runs": frozenset(
        {
            "run_id",
            "parent_run_id",
            "resumes_run_id",
            "started_at",
            "finished_at",
            "duration_seconds",
            "cpu_seconds",
            "max_rss_mb",
            "avg_rss_mb",
            "execution_details",
        }
    ),
    "changes": frozenset(
        {
            "change_id",
            "reverts_change_id",
            "changed_at",
            "workflow_run_id",
        }
    ),
    "files": frozenset({"downloaded_at"}),
    "decisions": frozenset(
        {
            "decision_id",
            "profile_snapshot_id",
            "evaluated_at",
            "curated_at",
        }
    ),
    "qc_profiles": frozenset({"profile_snapshot_id", "recorded_at"}),
    "entity_state": frozenset({"updated_at"}),
    "qc_results": frozenset({"qc_result_id", "evaluated_at"}),
}


def _twin_projects(tmp_path: Path, demo_template: Project) -> tuple[Project, Project]:
    """Two identical copies of the demo project: one for the CLI, one for the TUI."""
    cli_root = tmp_path / "cli-project"
    tui_root = tmp_path / "tui-project"
    shutil.copytree(demo_template.root, cli_root)
    shutil.copytree(demo_template.root, tui_root)
    return Project.find(cli_root), Project.find(tui_root)


def _table_rows(project: Project, table: str) -> list[tuple]:
    """Rows of one audit/side table as sorted tuples, volatile columns dropped."""
    drop = _AUDIT_DROP.get(table, frozenset())
    db = Database(project.db_path, read_only=True)
    try:
        rows = [dict(row) for row in db.query(f"SELECT * FROM {table}")]
    finally:
        db.close()
    columns = [column for column in rows[0] if column not in drop] if rows else []
    return sorted(tuple(row[column] for column in columns) for row in rows)


def _assert_audit_equal(
    cli_project: Project,
    tui_project: Project,
    *side_tables: str,
) -> None:
    for table in (*AUDIT_TABLES, *side_tables):
        cli_rows = _table_rows(cli_project, table)
        tui_rows = _table_rows(tui_project, table)
        assert cli_rows == tui_rows, (
            f"audit divergence in {table}: "
            f"CLI-only {sorted(set(cli_rows) - set(tui_rows))}, "
            f"TUI-only {sorted(set(tui_rows) - set(cli_rows))}"
        )


def _normalized_execution_details(project: Project) -> list[str]:
    """workflow_runs.execution_details JSON, with embedded timings stripped.

    Dropped subkeys: ``stages_seconds`` (per-stage wall-clock timings) and
    ``integrity.verification_cached_at`` (wall-clock cache timestamp).
    Everything else — parser backend, input descriptor, related inputs,
    integrity flags — is semantic and compared.
    """
    db = Database(project.db_path, read_only=True)
    try:
        rows = db.query(
            "SELECT execution_details FROM workflow_runs "
            "WHERE execution_details IS NOT NULL"
        )
        documents = [json.loads(row["execution_details"]) for row in rows]
    finally:
        db.close()
    for document in documents:
        document.pop("stages_seconds", None)
        integrity = document.get("integrity")
        if isinstance(integrity, dict):
            integrity.pop("verification_cached_at", None)
    return sorted(json.dumps(document, sort_keys=True) for document in documents)


def test_audit_parity_ingest(
    tmp_path: Path,
    demo_template: Project,
    capsys: pytest.CaptureFixture,
) -> None:
    cli_project, tui_project = _twin_projects(tmp_path, demo_template)
    # One shared source path: workflow_runs.command embeds it verbatim.
    source = tmp_path / "incoming.fasta"
    source.write_text(">ctgX\nACGTACGTACGT\n", encoding="utf-8")

    rc = cli_main(
        [
            "--project",
            str(cli_project.root),
            "ingest",
            "--source",
            str(source),
            "--entity-type",
            "assembly",
            "--entity-id",
            "ASM_000001",
            "--role",
            "parity_fasta",
        ]
    )
    capsys.readouterr()
    assert rc == 0
    row = actions.ingest(
        tui_project,
        str(source),
        "assembly",
        "ASM_000001",
        "parity_fasta",
    )
    assert row["file_id"]  # same content-addressed id expected on both sides

    _assert_audit_equal(cli_project, tui_project, "files")


def test_audit_parity_evaluate(
    tmp_path: Path,
    demo_template: Project,
    capsys: pytest.CaptureFixture,
) -> None:
    cli_project, tui_project = _twin_projects(tmp_path, demo_template)
    # The CLI asks before re-evaluating curated entities; --yes is the
    # non-interactive form of the TUI modal's Confirm step.
    rc = cli_main(
        [
            "--project",
            str(cli_project.root),
            "evaluate",
            "--entity-type",
            "assembly",
            "--yes",
        ]
    )
    capsys.readouterr()
    assert rc == 0
    results = actions.evaluate(tui_project, entity_type="assembly")
    assert len(results) == 3

    _assert_audit_equal(
        cli_project, tui_project, "decisions", "qc_profiles", "entity_state"
    )


def test_audit_parity_curate(
    tmp_path: Path,
    demo_template: Project,
    capsys: pytest.CaptureFixture,
) -> None:
    cli_project, tui_project = _twin_projects(tmp_path, demo_template)
    rc = cli_main(
        [
            "--project",
            str(cli_project.root),
            "curate",
            "--entity-type",
            "assembly",
            "--entity-id",
            "ASM_000002",
            "--profile",
            "assembly_production_v1",
            "--decision",
            "REVIEW",
            "--reviewer",
            "tester",
            "--reason",
            "audit parity",
            "--evidence",
            "ticket-1",
        ]
    )
    capsys.readouterr()
    assert rc == 0
    actions.curate(
        tui_project,
        "assembly",
        "ASM_000002",
        "assembly_production_v1",
        "REVIEW",
        "tester",
        "audit parity",
        evidence="ticket-1",
    )

    _assert_audit_equal(cli_project, tui_project, "decisions", "entity_state")


def test_audit_parity_qc_single_file(
    tmp_path: Path,
    demo_template: Project,
    capsys: pytest.CaptureFixture,
) -> None:
    cli_project, tui_project = _twin_projects(tmp_path, demo_template)
    rc = cli_main(
        [
            "--project",
            str(cli_project.root),
            "qc",
            "--file-id",
            "FIL_000001",
        ]
    )
    capsys.readouterr()
    assert rc == 0
    results = actions.run_qc(tui_project, file_id="FIL_000001")
    assert len(results) == 1 and results[0]["ok"] is True

    _assert_audit_equal(cli_project, tui_project, "qc_results", "entity_state")
    assert _normalized_execution_details(cli_project) == (
        _normalized_execution_details(tui_project)
    )


def test_classify_modal_command_text_matches_action_kwargs(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The classify dialog preview is the CLI call, and Confirm passes the profile."""
    pytest.importorskip("textual")
    from textual.widgets import Button, Static

    from operon.tui.app import OperonApp
    from operon.tui.screens.classify import ClassifyModal

    payload = {
        "profile": "bhlh_v1", "profile_sha256": "sha", "files": 1,
        "files_without_sequences": 0, "ignored_completed_jobs": 0, "sequences": 5,
        "label_counts": {"A": 3, "U": 2}, "unlabeled": 0, "labels_written": 5,
        "labels_removed": 0, "run_id": "WF_0001",
    }
    calls = spy_action(monkeypatch, "run_classify", payload)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = ClassifyModal(project, "bhlh_v1", {"applies_to": {}, "sources": {},
                                                      "rules": []})
            await _push(pilot, modal, "#classify-summary")
            ns = parse_command_text(modal.command_text())
            assert ns.profile == "bhlh_v1"

            modal.confirm()
            await _wait_until(lambda: bool(calls), "classify run")
            await pilot.pause()
            # The modal stays open with the CLI-shaped summary and a re-run button.
            summary = _static_text(await _q(modal, "#classify-summary", Static))
            assert "labels written: 5, removed: 0 (run WF_0001)" in summary
            assert "A" in summary and "3" in summary
            assert (await _q(modal, "#confirm", Button)).label == "Run again"

    _run(scenario())
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1] == "bhlh_v1"
    assert kwargs == {}


def test_extract_modal_command_text_matches_action_kwargs(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Input, Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.derived_ops import ExtractModal

    payload = {"extracted": 2, "excluded": 1, "output": "/tmp/out.faa", "manifest": None}
    calls = spy_action(monkeypatch, "extract_domains", payload)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = ExtractModal(project, "FIL_000001")
            await _push(pilot, modal, "#extract-manifest")
            (await _q(modal, "#extract-analysis", Input)).value = "cdd"
            (await _q(modal, "#extract-flank", Input)).value = "7"
            (await _q(modal, "#extract-min-length", Input)).value = "40"
            (await _q(modal, "#extract-region-mode", Select)).value = "all"
            (await _q(modal, "#extract-subject-like", Input)).value = "bHLH%"
            (await _q(modal, "#extract-evalue-max", Input)).value = "1e-5"
            (await _q(modal, "#extract-out", Input)).value = "/tmp/domains.faa"
            (await _q(modal, "#extract-manifest", Input)).value = "/tmp/domains.tsv"
            await pilot.pause()

            ns = parse_command_text(modal.command_text())
            assert ns.file_id == "FIL_000001"
            assert ns.analysis == "cdd" and ns.regions_tsv is None
            assert ns.flank == 7 and ns.min_length == 40
            assert ns.all_regions is True and ns.best_only is False
            assert ns.subject_like == "bHLH%" and ns.evalue_max == 1e-5
            assert ns.out == "/tmp/domains.faa" and ns.manifest == "/tmp/domains.tsv"

            modal.confirm()
            await _wait_until(lambda: bool(calls), "extract run")

    _run(scenario())
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (project,)
    assert kwargs == {
        "file_id": "FIL_000001", "out": "/tmp/domains.faa", "analysis": "cdd",
        "regions_tsv": None, "flank": 7, "min_length": 40, "best_only": False,
        "subject_like": "bHLH%", "evalue_max": 1e-5, "manifest": "/tmp/domains.tsv",
    }


def test_select_modal_command_text_matches_action_kwargs(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Input, Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.derived_ops import SelectSequencesModal

    payload = {"total": 5, "selected": 2, "excluded": 3, "output": "/tmp/sel.faa",
               "manifest": None}
    calls = spy_action(monkeypatch, "select_sequences", payload)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = SelectSequencesModal(project, "FIL_000001")
            await _push(pilot, modal, "#select-manifest")
            (await _q(modal, "#select-analysis-1", Input)).value = "cdd"
            (await _q(modal, "#select-analysis-2", Input)).value = "pfam"
            (await _q(modal, "#select-subject-like", Input)).value = "bHLH%"
            (await _q(modal, "#select-min-span", Input)).value = "20"
            (await _q(modal, "#select-requirement", Select)).value = "no-hit"
            (await _q(modal, "#select-entity-type", Select)).value = "annotation"
            (await _q(modal, "#select-entity-id", Input)).value = "ANN_000001"
            (await _q(modal, "#select-out", Input)).value = "/tmp/sel.faa"
            await pilot.pause()

            ns = parse_command_text(modal.command_text())
            assert ns.file_id == "FIL_000001"
            assert ns.analysis == ["cdd", "pfam"]
            assert ns.subject_like == "bHLH%" and ns.min_span == 20
            assert ns.require_no_hit is True and ns.require_hit is False
            assert ns.entity_type == "annotation" and ns.entity_id == "ANN_000001"
            assert ns.out == "/tmp/sel.faa"

            modal.confirm()
            await _wait_until(lambda: bool(calls), "select run")

    _run(scenario())
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (project,)
    assert kwargs == {
        "file_id": "FIL_000001", "out": "/tmp/sel.faa", "analyses": ["cdd", "pfam"],
        "subject_like": "bHLH%", "evalue_max": None, "min_span": 20, "hit_type": None,
        "require_hit": False, "entity_type": "annotation", "entity_id": "ANN_000001",
        "manifest": None,
    }


def test_adopt_modal_command_text_matches_action_kwargs(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Input, Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.derived_ops import AdoptModal

    payload = {"registered": 1, "reused": 0, "file_ids": ["FIL_000009"], "items": []}
    calls = spy_action(monkeypatch, "adopt", payload)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = AdoptModal(project, path="/tmp/derived.faa",
                               derived_from=["FIL_000001"])
            await _push(pilot, modal, "#adopt-preview-button")
            (await _q(modal, "#adopt-entity-type", Select)).value = "annotation"
            (await _q(modal, "#adopt-entity-id", Input)).value = "ANN_000001"
            (await _q(modal, "#adopt-role", Input)).value = "selected_proteins"
            (await _q(modal, "#adopt-format", Input)).value = "fasta"
            (await _q(modal, "#adopt-compression", Input)).value = "none"
            (await _q(modal, "#adopt-workflow-run-id", Input)).value = "WF_0001"
            (await _q(modal, "#adopt-actor", Input)).value = "tester"
            await pilot.pause()

            ns = parse_command_text(modal.command_text())
            assert ns.file == "/tmp/derived.faa" and ns.manifest is None
            assert ns.entity_type == "annotation" and ns.entity_id == "ANN_000001"
            assert ns.role == "selected_proteins" and ns.fmt == "fasta"
            assert ns.compression == "none" and ns.derived_from == ["FIL_000001"]
            assert ns.workflow_run_id == "WF_0001" and ns.actor == "tester"

            modal.confirm()
            await _wait_until(lambda: bool(calls), "adopt run")

    _run(scenario())
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (project,)
    assert kwargs["actor"] == "tester"
    assert kwargs["items"] == [{
        "path": "/tmp/derived.faa", "entity_type": "annotation", "entity_id": "ANN_000001",
        "role": "selected_proteins", "format": "fasta", "compression": "none",
        "derived_from": ["FIL_000001"], "workflow_run_id": "WF_0001",
    }]


def test_fanout_modal_command_text_matches_action_kwargs(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Input, Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.derived_ops import FanoutModal

    preview = {"dry_run": True, "units": [
        {"unit": "unitA", "sequences": 2, "role": "units:unitA", "status": "would_create"},
    ], "duplicate_rows": 0}
    calls: list[tuple[tuple, dict]] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return preview if kwargs.get("dry_run") else {
            "created": 1, "reused": 0, "run_id": "WF_0002", "units": preview["units"],
        }

    monkeypatch.setattr(actions, "fanout", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = FanoutModal(project, "FIL_000001")
            await _push(pilot, modal, "#fanout-preview-table")
            (await _q(modal, "#fanout-assignments", Input)).value = "FIL_000002"
            (await _q(modal, "#fanout-sources", Input)).value = "FIL_000001, FIL_000004"
            (await _q(modal, "#fanout-entity-type", Select)).value = "annotation"
            (await _q(modal, "#fanout-entity-id", Input)).value = "ANN_000001"
            (await _q(modal, "#fanout-role-prefix", Input)).value = "units"
            (await _q(modal, "#fanout-unit-column", Input)).value = "family"
            (await _q(modal, "#fanout-parent-run", Input)).value = "WF_0001"
            (await _q(modal, "#fanout-actor", Input)).value = "tester"
            await pilot.pause()

            ns = parse_command_text(modal.command_text())
            assert ns.assignments_file == "FIL_000002"
            assert ns.source_file == ["FIL_000001", "FIL_000004"]
            assert ns.entity_type == "annotation" and ns.entity_id == "ANN_000001"
            assert ns.role_prefix == "units" and ns.unit_column == "family"
            assert ns.seqid_column == "seqid" and ns.parent_run_id == "WF_0001"
            assert ns.dry_run is False  # the preview command is the real run

            modal.run_dry_run()
            await _wait_until(lambda: len(calls) == 1, "fanout dry run")
            await pilot.pause()
            modal.confirm()
            await _wait_until(lambda: len(calls) == 2, "fanout run")

    _run(scenario())
    preview_args, preview_kwargs = calls[0]
    run_args, run_kwargs = calls[1]
    assert preview_args == (project,) and run_args == (project,)
    assert preview_kwargs["dry_run"] is True and run_kwargs["dry_run"] is False
    expected = {
        "assignments_file_id": "FIL_000002",
        "source_file_ids": ["FIL_000001", "FIL_000004"],
        "entity_type": "annotation", "entity_id": "ANN_000001",
        "role_prefix": "units", "unit_column": "family", "seqid_column": "seqid",
        "parent_run_id": "WF_0001", "actor": "tester", "dry_run": False,
    }
    assert run_kwargs == expected
    assert {key: value for key, value in preview_kwargs.items() if key != "dry_run"} == \
        {key: value for key, value in expected.items() if key != "dry_run"}
