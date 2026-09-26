"""Remotes screen: read-only mirror listing, connectivity probe, residency.

The screen never reimplements anything: the mirror list comes from the same
core ``list_remotes``/``get_remote`` parsing the CLI uses, the connectivity
probe is the CLI's ``check_remote`` per remote, and the residency listing runs
the shared core ``list_locations`` query behind ``operon locations`` — this
module pins both layers (pure data/action layer plus headless UI).
"""

from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from operon.cli import main as cli_main
from operon.config import Project
from operon.database import Database
from operon.files import ingest_file
from operon.tui import actions, data
from operon.utils import format_table

#: Wall-clock budget for the waits below (the tree-wide settle budget).
SETTLE_TIMEOUT = 30.0
#: Cap for one scenario, above the settle budget.
SCENARIO_TIMEOUT = 180.0


def _run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=SCENARIO_TIMEOUT))


async def _settled(app: Any) -> None:
    """Wait for the splash to finish and for every worker to drain."""
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while time.monotonic() < deadline:
        if getattr(app, "_starting", False) is False and not app.workers:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("app never settled")


async def _wait_until(predicate: Any, what: str, timeout: float | None = None) -> None:
    deadline = time.monotonic() + (SETTLE_TIMEOUT if timeout is None else timeout)
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def parse_command_text(text: str) -> Any:
    """Parse a modal's equivalent-command preview with the real CLI parser."""
    from operon.cli import _parser

    argv = shlex.split(text)
    assert argv and argv[0] == "operon", f"unexpected command preview: {text!r}"
    return _parser().parse_args(argv[1:])


def spy_action(
    monkeypatch: pytest.MonkeyPatch, name: str, result: Any,
) -> list[tuple[tuple, dict]]:
    """Replace ``operon.tui.actions.<name>`` with a recording stub."""
    calls: list[tuple[tuple, dict]] = []

    def stub(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(actions, name, stub)
    return calls


async def _pilot_push(pilot: Any, modal: Any, selector: str | None = None) -> None:
    """Push a modal and wait until its form and buttons have composed."""
    for attempt in range(2):
        if pilot.app.screen is not modal:
            pilot.app.push_screen(modal)

        def ready() -> bool:
            if len(modal.query("#confirm")) == 0:
                return False
            if selector is None:
                return bool(modal.query("#modal-form > *"))
            return len(modal.query(selector)) > 0

        try:
            await _wait_until(ready, f"{type(modal).__name__} to compose", timeout=10.0)
        except AssertionError:
            if attempt:
                raise
            await pilot.pause()
            continue
        await pilot.pause()
        return
    raise AssertionError(f"{type(modal).__name__} never composed")


async def _click(pilot: Any, selector: str) -> None:
    """Click a widget, working around the press-effect and OutOfBounds traps."""
    from textual.widgets import Button

    widget = pilot.app.screen.query_one(selector)
    for _ in range(20):
        if not getattr(widget, "has_class", lambda _c: False)("-active"):
            break
        await pilot.pause()
    try:
        result = await pilot.click(selector)
    except Exception:  # noqa: BLE001 - the layout moved under the click
        result = False
    if not result and isinstance(widget, Button):
        widget.press()


async def _q(screen: Any, selector: str, *types: Any) -> Any:
    await _wait_until(lambda: len(screen.query(selector)) > 0, f"{selector} to exist")
    return screen.query_one(selector, *types)


def _static_text(widget: Any) -> str:
    from rich.text import Text

    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


def _project_with_remotes(tmp_path: Path, remotes: dict[str, Any]) -> Project:
    """A fresh project whose project.yaml carries the given remotes block."""
    root = tmp_path / "project"
    assert cli_main(["--project", str(root), "init", str(root),
                     "--project-id", "PRJ_TUI_REMOTES"]) == 0
    project = Project.find(root)
    config = yaml.safe_load(project.config_path.read_text(encoding="utf-8"))
    config["remotes"] = remotes
    project.config_path.write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    project.config["remotes"] = remotes
    return project


def _ingest(project: Project, name: str, index: int = 1) -> dict[str, Any]:
    """Register one small file through the same core path the CLI uses."""
    db = Database(project.db_path)
    try:
        if not db.query("SELECT 1 FROM organisms WHERE organism_id='ORG_000001'"):
            db.insert_row("organisms", {
                "organism_id": "ORG_000001", "scientific_name": "Testus",
                "taxonomy_source": "NCBI"})
            db.insert_row("samples", {
                "sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        assembly_id = f"ASM_{index:06d}"
        if not db.query("SELECT 1 FROM assemblies WHERE assembly_id=?", (assembly_id,)):
            db.insert_row("assemblies", {
                "assembly_id": assembly_id, "sample_id": "SMP_000001",
                "assembly_level": "contig", "assembly_version": 1})
        source = project.root / name
        source.write_text(f">{name}\n" + "ACGT" * 50 + "\n", encoding="utf-8")
        return ingest_file(db, project, source, "assembly", assembly_id,
                           "genome_fasta")
    finally:
        db.close()


def _add_location(project: Project, file_row: dict[str, Any], name: str,
                  status: str = "VERIFIED", verified_at: str = "2026-01-01T00:00:00Z") -> None:
    db = Database(project.db_path)
    try:
        db.insert_row("file_locations", {
            "file_id": file_row["file_id"], "location_name": name,
            "location_type": "sftp", "uri": f"sftp://fake{file_row['relative_path']}",
            "relative_path": file_row["relative_path"],
            "sha256": file_row["sha256"], "size_bytes": file_row["size_bytes"],
            "status": status, "verified_at": verified_at,
        })
    finally:
        db.close()


# -- data / action layer -------------------------------------------------------


def test_list_remotes_reports_endpoints_and_flags_invalid_entries(tmp_path: Path) -> None:
    project = _project_with_remotes(tmp_path, {
        "mirror": {"type": "sftp", "host": "hpc.example.org", "user": "alice",
                   "port": 2222, "root": "/data/operon"},
        "broken": {"type": "sftp", "host": "hpc.example.org"},
        "notmap": "oops",
    })

    rows = data.list_remotes(project)
    assert [row["name"] for row in rows] == ["broken", "mirror", "notmap"]
    by_name = {row["name"]: row for row in rows}

    mirror = by_name["mirror"]
    assert mirror["address"] == "alice@hpc.example.org:2222"
    assert mirror["root"] == "/data/operon"
    # Nothing is contacted on load: the CLI's connectivity columns start blank.
    assert mirror["status"] == "not checked" and mirror["error"] == ""
    assert mirror["files"] == ""

    assert by_name["broken"]["status"] == "invalid"
    assert "'root'" in by_name["broken"]["error"]
    assert by_name["notmap"]["status"] == "invalid"


def test_list_locations_shares_the_cli_query(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _project_with_remotes(tmp_path, {})
    first = _ingest(project, "one.fa")
    second = _ingest(project, "two.fa", 2)
    _add_location(project, first, "mirror")
    _add_location(project, second, "mirror", status="MISSING", verified_at="")

    rows = data.list_locations(project)
    assert [row["file_id"] for row in rows] == sorted(
        [first["file_id"], second["file_id"]])
    assert set(rows[0]) == {
        "file_id", "relative_path", "local_status", "remote", "remote_status",
        "verified_at"}
    assert rows[0]["remote"] == "mirror" and rows[0]["remote_status"] == "VERIFIED"

    # The CLI prints exactly the table these rows describe.
    capsys.readouterr()  # discard the ``init`` banner
    assert cli_main(["--project", str(project.root), "locations"]) == 0
    printed = capsys.readouterr().out
    expected = format_table(
        ["file_id", "relative_path", "local_status", "remote", "remote_status",
         "verified_at"],
        ([row[column] for column in row] for row in rows),
    )
    assert printed == expected + "\n"

    # The repeated --file-id filter selects the same rows on both paths.
    filtered = data.list_locations(project, file_ids=[second["file_id"]])
    assert [row["file_id"] for row in filtered] == [second["file_id"]]
    assert cli_main(["--project", str(project.root), "locations",
                     "--file-id", second["file_id"]]) == 0
    # Columns are padded per invocation, so compare against the filtered table.
    assert capsys.readouterr().out == format_table(
        ["file_id", "relative_path", "local_status", "remote", "remote_status",
         "verified_at"],
        ([row[column] for column in row] for row in filtered),
    ) + "\n"


def test_check_remotes_probes_each_remote_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project_with_remotes(tmp_path, {
        "down": {"type": "sftp", "host": "down.example.org", "root": "/data"},
        "mirror": {"type": "sftp", "host": "up.example.org", "root": "/data"},
        "broken": {"type": "sftp", "host": "up.example.org"},
    })
    probed: list[str] = []

    def fake_check(project_arg: Project, name: str) -> dict[str, Any]:
        probed.append(name)
        if name == "down":
            raise OSError("connection refused")
        return {"name": name, "type": "sftp", "address": "up.example.org:22",
                "root": "/data", "files": 3, "status": "ok", "error": ""}

    monkeypatch.setattr("operon.remotes.check_remote", fake_check)

    rows = actions.check_remotes(project)
    # An invalid entry is never contacted; the rest are probed in name order.
    assert probed == ["down", "mirror"]
    by_name = {row["name"]: row for row in rows}
    assert by_name["mirror"]["status"] == "ok" and by_name["mirror"]["files"] == 3
    assert by_name["down"]["status"] == "error"
    assert "connection refused" in by_name["down"]["error"]
    assert by_name["broken"]["status"] == "invalid"


# -- UI layer ------------------------------------------------------------------


def test_remotes_screen_lists_mirrors_and_residency(tmp_path: Path) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Static

    from operon.tui.app import OperonApp

    project = _project_with_remotes(tmp_path, {
        "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data/operon"},
    })
    file_row = _ingest(project, "one.fa")
    _add_location(project, file_row, "mirror")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            remotes = await _q(app.screen, "#remotes-table", DataTable)
            await _wait_until(lambda: remotes.row_count == 1, "remote row")
            assert remotes.get_row_at(0)[0] == "mirror"
            assert remotes.get_row_at(0)[3] == "/data/operon"

            locations = await _q(app.screen, "#locations-table", DataTable)
            await _wait_until(lambda: locations.row_count == 1, "location row")
            assert str(locations.get_row_at(0)[0]) == file_row["file_id"]
            assert str(locations.get_row_at(0)[3]) == "mirror"

            # The equivalent commands are echoed next to both sections.
            assert "operon remotes" in _static_text(
                await _q(app.screen, "#remotes-command", Static))
            assert "operon locations" in _static_text(
                await _q(app.screen, "#locations-command", Static))

    _run(scenario())


def test_remotes_check_button_probes_and_reports_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable

    from operon.tui.app import OperonApp

    project = _project_with_remotes(tmp_path, {
        "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
    })
    rows = [{"name": "mirror", "type": "sftp", "address": "hpc.example.org:22",
             "root": "/data", "files": "", "status": "error",
             "error": "OSError: connection refused"}]
    calls: list[Project] = []

    def fake_probe(project_arg: Project) -> list[dict[str, Any]]:
        calls.append(project_arg)
        return rows

    monkeypatch.setattr(actions, "check_remotes", fake_probe)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            button = await _q(app.screen, "#remotes-check", Button)
            assert not button.disabled
            await _click(pilot, "#remotes-check")
            table = await _q(app.screen, "#remotes-table", DataTable)
            await _wait_until(
                lambda: table.row_count == 1
                and "error" in str(table.get_row_at(0)[5]), "checked row")
            await _wait_until(lambda: not button.disabled, "button re-enabled")
            assert calls == [project]
            severities = [n.severity for n in app._notifications]
            assert "error" in severities, severities

    _run(scenario())


def test_remotes_locations_filter_maps_repeated_file_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Input, Static

    from operon.tui.app import OperonApp

    project = _project_with_remotes(tmp_path, {})
    first = _ingest(project, "one.fa")
    second = _ingest(project, "two.fa", 2)
    seen: list[list[str] | None] = []
    real = data.list_locations

    def spy(project_arg: Project, *, file_ids: Any = None, limit: int = 0) -> list[dict[str, Any]]:
        seen.append(None if file_ids is None else list(file_ids))
        return real(project_arg, file_ids=file_ids, limit=limit)

    monkeypatch.setattr(data, "list_locations", spy)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            table = await _q(app.screen, "#locations-table", DataTable)
            await _wait_until(lambda: table.row_count == 2, "unfiltered rows")

            (await _q(app.screen, "#locations-filter", Input)).value = (
                f"{second['file_id']}, {first['file_id']} {second['file_id']}")
            await _click(pilot, "#locations-apply")
            await _wait_until(lambda: table.row_count == 2 and seen[-1] is not None,
                              "filtered rows")
            # Duplicates collapse and the order the user typed is kept.
            assert seen[-1] == [second["file_id"], first["file_id"]]
            command = _static_text(await _q(app.screen, "#locations-command", Static))
            assert command.count("--file-id") == 2

    _run(scenario())


def test_remotes_filter_row_stays_inside_its_parent(tmp_path: Path) -> None:
    """At the 80x24 minimum the filter row must not clip its own controls."""
    pytest.importorskip("textual")
    from textual.containers import Horizontal
    from textual.widgets import Button, Input

    from operon.tui.app import OperonApp

    project = _project_with_remotes(tmp_path, {})
    _ingest(project, "one.fa")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(80, 24)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            row = await _q(app.screen, "#locations-form", Horizontal)
            field = await _q(app.screen, "#locations-filter", Input)
            button = await _q(app.screen, "#locations-apply", Button)
            assert row.region.contains_region(field.region), (row.region, field.region)
            assert row.region.contains_region(button.region), (row.region, button.region)
            assert field.region.width >= 10
            assert button.region.width >= 8

    _run(scenario())


# -- push / pull: the action layer calls the core exactly like the CLI ---------


def _sync_args(calls: list[tuple[tuple, dict]]) -> list[tuple[str, Any, bool]]:
    """Reduce recorded core calls to (remote name, file_ids, writable session)."""
    reduced: list[tuple[str, Any, bool]] = []
    for args, _kwargs in calls:
        db, _project, name = args[0], args[1], args[2]
        reduced.append((name, kwargs_file_ids(_kwargs, args), not db.read_only))
    return reduced


def kwargs_file_ids(kwargs: dict[str, Any], args: tuple = ()) -> Any:
    if "file_ids" in kwargs:
        return kwargs["file_ids"]
    return args[3] if len(args) > 3 else None


def test_actions_push_and_pull_pass_the_cli_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Both paths hand the core the same remote name and file ids."""
    project = _project_with_remotes(tmp_path, {
        "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
    })
    file_row = _ingest(project, "one.fa")
    capsys.readouterr()  # discard the ``init`` banner

    push_calls: list[tuple[tuple, dict]] = []
    pull_calls: list[tuple[tuple, dict]] = []

    def spy_push(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        push_calls.append((args, kwargs))
        return [{"file_id": "FIL_000001", "relative_path": "raw/x.fa",
                 "status": "uploaded", "error": None}]

    def spy_pull(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        pull_calls.append((args, kwargs))
        return []

    monkeypatch.setattr("operon.remotes.push", spy_push)
    monkeypatch.setattr("operon.remotes.pull", spy_pull)

    actions.push(project, "mirror", [file_row["file_id"]])
    tui_push = _sync_args(push_calls)[-1]
    actions.pull(project, "mirror")
    tui_pull = _sync_args(pull_calls)[-1]

    assert cli_main(["--project", str(project.root), "push", "--remote", "mirror",
                     "--file-id", file_row["file_id"]]) == 0
    cli_push = _sync_args(push_calls)[-1]
    assert cli_main(["--project", str(project.root), "pull", "--remote", "mirror"]) == 0
    cli_pull = _sync_args(pull_calls)[-1]
    capsys.readouterr()

    assert tui_push == cli_push, "push: the TUI and the CLI must call the core alike"
    assert tui_pull == cli_pull, "pull: the TUI and the CLI must call the core alike"
    # …and both open a writable session, which push/pull require.
    assert all(entry[2] for entry in (tui_push, tui_pull, cli_push, cli_pull))


def test_actions_push_requires_a_remote(tmp_path: Path) -> None:
    from operon.errors import ValidationError

    project = _project_with_remotes(tmp_path, {})
    for action in (actions.push, actions.pull):
        with pytest.raises(ValidationError):
            action(project, "  ")


# -- push / pull: the modals ---------------------------------------------------


def test_push_modal_command_text_matches_action_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Input, Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PushModal

    project = _project_with_remotes(tmp_path, {
        "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
    })
    file_row = _ingest(project, "one.fa")
    results = [{"file_id": file_row["file_id"], "relative_path": "raw/one.fa",
                "status": "uploaded", "error": None}]
    calls = spy_action(monkeypatch, "push", results)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = PushModal(project)
            app.push_screen(modal)
            await _pilot_push(pilot, modal, "#sync-remote")
            # Defaults: no remote selected yet, so the preview keeps --remote
            # with a placeholder (the parser needs the required flag).
            assert modal.command_text() == "operon push --remote '…'"

            (await _q(modal, "#sync-remote", Select)).value = "mirror"
            (await _q(modal, "#sync-file-ids", Input)).value = file_row["file_id"]
            await pilot.pause()
            namespace = parse_command_text(modal.command_text())
            assert namespace.remote == "mirror"
            assert namespace.file_id == [file_row["file_id"]]

            modal.confirm()
            await _wait_until(lambda: len(calls) == 1, "push call")

    _run(scenario())
    args, kwargs = calls[0]
    assert args == (project, "mirror", [file_row["file_id"]])
    assert kwargs == {}


def test_pull_modal_command_text_matches_action_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PullModal

    project = _project_with_remotes(tmp_path, {
        "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
    })
    calls = spy_action(monkeypatch, "pull", [])

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = PullModal(project, remote="mirror")
            app.push_screen(modal)
            await _pilot_push(pilot, modal, "#sync-remote")
            # The CLI parser has no default for --remote: an unselected state
            # keeps the placeholder, and the prefilled remote is what runs.
            assert modal.command_text() == "operon pull --remote mirror"
            namespace = parse_command_text(modal.command_text())
            assert namespace.remote == "mirror" and namespace.file_id == []

            (await _q(modal, "#sync-remote", Select)).value = "mirror"
            modal.confirm()
            await _wait_until(lambda: len(calls) == 1, "pull call")

    _run(scenario())
    args, kwargs = calls[0]
    assert args == (project, "mirror", None)
    assert kwargs == {}


def test_push_modal_shows_plan_results_and_stays_open_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Select, Static

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PushModal

    project = _project_with_remotes(tmp_path, {
        "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
    })
    good = _ingest(project, "one.fa")
    bad = _ingest(project, "two.fa", 2)
    results = [
        {"file_id": good["file_id"], "relative_path": good["relative_path"],
         "status": "uploaded", "error": None},
        {"file_id": bad["file_id"], "relative_path": bad["relative_path"],
         "status": "error", "error": "RemoteError: connection reset"},
    ]
    calls = spy_action(monkeypatch, "push", results)
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = PushModal(project, remote="mirror")
            app.push_screen(modal, dismissed.append)
            await _pilot_push(pilot, modal, "#sync-plan")
            plan = _static_text(await _q(modal, "#sync-plan", Static))
            # The preview names the local selection (count, bytes) — decided
            # file by file only during the transfer.
            assert "2 selected file(s)" in plan or "every manifest file" in plan
            assert "skipped" in plan

            modal.confirm()
            table = await _q(modal, "#sync-results", DataTable)
            await _wait_until(
                lambda: table.row_count == 2, "result rows")
            assert app.screen is modal, "a failed file must keep the dialog open"
            status = _static_text(await _q(modal, "#sync-status", Static))
            assert "uploaded: 1" in status and "error: 1" in status
            severities = [n.severity for n in app._notifications]
            assert "error" in severities, severities
            # Closing hands the results back so the screen can reload.
            modal.action_cancel()
            await _wait_until(lambda: bool(dismissed), "modal dismissed")

    _run(scenario())
    assert calls and calls[0][0][1] == "mirror"
    assert dismissed == [results]


def test_sync_modal_refuses_to_close_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    import threading

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PushModal

    project = _project_with_remotes(tmp_path, {
        "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
    })
    release = threading.Event()
    started = threading.Event()

    def blocking(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        started.set()
        assert release.wait(SETTLE_TIMEOUT)
        return []

    monkeypatch.setattr(actions, "push", blocking)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = PushModal(project, remote="mirror")
            app.push_screen(modal)
            await _pilot_push(pilot, modal, "#sync-remote")
            modal.confirm()
            await _wait_until(started.is_set, "the transfer to start")
            modal.action_cancel()
            await pilot.pause()
            assert app.screen is modal, "a running push must not close"
            assert "cannot be interrupted" in " ".join(
                n.message for n in app._notifications)
            release.set()
            await _settled(app)

    _run(scenario())


def test_push_and_pull_buttons_open_the_dialog_prefilled_with_the_mirror(
    tmp_path: Path,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PullModal, PushModal

    project = _project_with_remotes(tmp_path, {
        "archive": {"type": "sftp", "host": "archive.example.org", "root": "/data"},
        "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
    })

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            table = await _q(app.screen, "#remotes-table", DataTable)
            await _wait_until(lambda: table.row_count == 2, "remote rows")
            table.move_cursor(row=1)  # the second mirror, "mirror"
            await pilot.pause()

            await _click(pilot, "#remotes-push")
            await _wait_until(lambda: isinstance(app.screen, PushModal), "push dialog")
            modal = app.screen
            assert (await _q(modal, "#sync-remote", Select)).value == "mirror"
            assert modal.command_text() == "operon push --remote mirror"
            modal.action_cancel()
            await _wait_until(lambda: not isinstance(app.screen, PushModal), "push closed")

            await _click(pilot, "#remotes-pull")
            await _wait_until(lambda: isinstance(app.screen, PullModal), "pull dialog")
            modal = app.screen
            assert (await _q(modal, "#sync-remote", Select)).value == "mirror"
            assert modal.command_text() == "operon pull --remote mirror"
            modal.action_cancel()

    _run(scenario())
