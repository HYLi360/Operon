#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
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
from operon.errors import ConflictError
from operon.files import ingest_file
from operon.tui import actions, data
from operon.utils import format_table
from tests.tui_helpers import click as _click

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


async def _await_notification(app: Any, needle: str = "", severity: str = "") -> None:
    """Wait for a raised notification matching ``needle`` and/or ``severity``.

    ``App.notify`` queues a message, so a callback that has already updated its
    widgets raises the notification a message-loop turn later: reading
    ``app._notifications`` straight after the widgets turns a correct flow into
    an intermittent failure on a slow interpreter (ODR-51).
    """

    def raised() -> bool:
        return any(
            (not severity or notification.severity == severity)
            and (not needle or needle in notification.message)
            for notification in app._notifications
        )

    await _wait_until(raised, f"the notification {needle or severity!r}")


def parse_command_text(text: str) -> Any:
    """Parse a modal's equivalent-command preview with the real CLI parser."""
    from operon.cli import _parser

    argv = shlex.split(text)
    assert argv and argv[0] == "operon", f"unexpected command preview: {text!r}"
    return _parser().parse_args(argv[1:])


def spy_action(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    result: Any,
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
    assert (
        cli_main(
            [
                "--project",
                str(root),
                "init",
                str(root),
                "--project-id",
                "PRJ_TUI_REMOTES",
            ]
        )
        == 0
    )
    project = Project.find(root)
    config = yaml.safe_load(project.config_path.read_text(encoding="utf-8"))
    config["remotes"] = remotes
    project.config_path.write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    project.config["remotes"] = remotes
    return project


def _ingest(project: Project, name: str, index: int = 1) -> dict[str, Any]:
    """Register one small file through the same core path the CLI uses."""
    db = Database(project.db_path)
    try:
        if not db.query("SELECT 1 FROM organisms WHERE organism_id='ORG_000001'"):
            db.insert_row(
                "organisms",
                {
                    "organism_id": "ORG_000001",
                    "scientific_name": "Testus",
                    "taxonomy_source": "NCBI",
                },
            )
            db.insert_row(
                "samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"}
            )
        assembly_id = f"ASM_{index:06d}"
        if not db.query("SELECT 1 FROM assemblies WHERE assembly_id=?", (assembly_id,)):
            db.insert_row(
                "assemblies",
                {
                    "assembly_id": assembly_id,
                    "sample_id": "SMP_000001",
                    "assembly_level": "contig",
                    "assembly_version": 1,
                },
            )
        source = project.root / name
        source.write_text(f">{name}\n" + "ACGT" * 50 + "\n", encoding="utf-8")
        return ingest_file(db, project, source, "assembly", assembly_id, "genome_fasta")
    finally:
        db.close()


def _add_location(
    project: Project,
    file_row: dict[str, Any],
    name: str,
    status: str = "VERIFIED",
    verified_at: str = "2026-01-01T00:00:00Z",
) -> None:
    db = Database(project.db_path)
    try:
        db.insert_row(
            "file_locations",
            {
                "file_id": file_row["file_id"],
                "location_name": name,
                "location_type": "sftp",
                "uri": f"sftp://fake{file_row['relative_path']}",
                "relative_path": file_row["relative_path"],
                "sha256": file_row["sha256"],
                "size_bytes": file_row["size_bytes"],
                "status": status,
                "verified_at": verified_at,
            },
        )
    finally:
        db.close()


# -- data / action layer -------------------------------------------------------


def test_list_remotes_reports_endpoints_and_flags_invalid_entries(
    tmp_path: Path,
) -> None:
    project = _project_with_remotes(
        tmp_path,
        {
            "mirror": {
                "type": "sftp",
                "host": "hpc.example.org",
                "user": "alice",
                "port": 2222,
                "root": "/data/operon",
            },
            "broken": {"type": "sftp", "host": "hpc.example.org"},
            "notmap": "oops",
        },
    )

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


def test_list_locations_shares_the_cli_query(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project_with_remotes(tmp_path, {})
    first = _ingest(project, "one.fa")
    second = _ingest(project, "two.fa", 2)
    _add_location(project, first, "mirror")
    _add_location(project, second, "mirror", status="MISSING", verified_at="")

    rows = data.list_locations(project)
    assert [row["file_id"] for row in rows] == sorted(
        [first["file_id"], second["file_id"]]
    )
    assert set(rows[0]) == {
        "file_id",
        "relative_path",
        "local_status",
        "remote",
        "remote_status",
        "verified_at",
    }
    assert rows[0]["remote"] == "mirror" and rows[0]["remote_status"] == "VERIFIED"

    # The CLI prints exactly the table these rows describe.
    capsys.readouterr()  # discard the ``init`` banner
    assert cli_main(["--project", str(project.root), "locations"]) == 0
    printed = capsys.readouterr().out
    expected = format_table(
        [
            "file_id",
            "relative_path",
            "local_status",
            "remote",
            "remote_status",
            "verified_at",
        ],
        ([row[column] for column in row] for row in rows),
    )
    assert printed == expected + "\n"

    # The repeated --file-id filter selects the same rows on both paths.
    filtered = data.list_locations(project, file_ids=[second["file_id"]])
    assert [row["file_id"] for row in filtered] == [second["file_id"]]
    assert (
        cli_main(
            [
                "--project",
                str(project.root),
                "locations",
                "--file-id",
                second["file_id"],
            ]
        )
        == 0
    )
    # Columns are padded per invocation, so compare against the filtered table.
    assert (
        capsys.readouterr().out
        == format_table(
            [
                "file_id",
                "relative_path",
                "local_status",
                "remote",
                "remote_status",
                "verified_at",
            ],
            ([row[column] for column in row] for row in filtered),
        )
        + "\n"
    )


def test_check_remotes_probes_each_remote_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project_with_remotes(
        tmp_path,
        {
            "down": {"type": "sftp", "host": "down.example.org", "root": "/data"},
            "mirror": {"type": "sftp", "host": "up.example.org", "root": "/data"},
            "broken": {"type": "sftp", "host": "up.example.org"},
        },
    )
    probed: list[str] = []

    def fake_check(project_arg: Project, name: str) -> dict[str, Any]:
        probed.append(name)
        if name == "down":
            raise OSError("connection refused")
        return {
            "name": name,
            "type": "sftp",
            "address": "up.example.org:22",
            "root": "/data",
            "files": 3,
            "status": "ok",
            "error": "",
        }

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

    project = _project_with_remotes(
        tmp_path,
        {
            "mirror": {
                "type": "sftp",
                "host": "hpc.example.org",
                "root": "/data/operon",
            },
        },
    )
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
                await _q(app.screen, "#remotes-command", Static)
            )
            assert "operon locations" in _static_text(
                await _q(app.screen, "#locations-command", Static)
            )

    _run(scenario())


@pytest.mark.bug("ODR-51")
def test_remotes_check_button_probes_and_reports_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable

    from operon.tui.app import OperonApp

    project = _project_with_remotes(
        tmp_path,
        {
            "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
        },
    )
    rows = [
        {
            "name": "mirror",
            "type": "sftp",
            "address": "hpc.example.org:22",
            "root": "/data",
            "files": "",
            "status": "error",
            "error": "OSError: connection refused",
        }
    ]
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
                lambda: table.row_count == 1 and "error" in str(table.get_row_at(0)[5]),
                "checked row",
            )
            await _wait_until(lambda: not button.disabled, "button re-enabled")
            assert calls == [project]
            await _await_notification(app, severity="error")

    _run(scenario())


def test_remotes_locations_filter_maps_repeated_file_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Input, Static

    from operon.tui.app import OperonApp

    project = _project_with_remotes(tmp_path, {})
    first = _ingest(project, "one.fa")
    second = _ingest(project, "two.fa", 2)
    seen: list[list[str] | None] = []
    real = data.list_locations

    def spy(
        project_arg: Project, *, file_ids: Any = None, limit: int = 0
    ) -> list[dict[str, Any]]:
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

            (
                await _q(app.screen, "#locations-filter", Input)
            ).value = f"{second['file_id']}, {first['file_id']} {second['file_id']}"
            await _click(pilot, "#locations-apply")
            await _wait_until(
                lambda: table.row_count == 2 and seen[-1] is not None, "filtered rows"
            )
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
            assert row.region.contains_region(button.region), (
                row.region,
                button.region,
            )
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both paths hand the core the same remote name and file ids."""
    project = _project_with_remotes(
        tmp_path,
        {
            "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
        },
    )
    file_row = _ingest(project, "one.fa")
    capsys.readouterr()  # discard the ``init`` banner

    push_calls: list[tuple[tuple, dict]] = []
    pull_calls: list[tuple[tuple, dict]] = []

    def spy_push(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        push_calls.append((args, kwargs))
        return [
            {
                "file_id": "FIL_000001",
                "relative_path": "raw/x.fa",
                "status": "uploaded",
                "error": None,
            }
        ]

    def spy_pull(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        pull_calls.append((args, kwargs))
        return []

    monkeypatch.setattr("operon.remotes.push", spy_push)
    monkeypatch.setattr("operon.remotes.pull", spy_pull)

    actions.push(project, "mirror", [file_row["file_id"]])
    tui_push = _sync_args(push_calls)[-1]
    actions.pull(project, "mirror")
    tui_pull = _sync_args(pull_calls)[-1]

    assert (
        cli_main(
            [
                "--project",
                str(project.root),
                "push",
                "--remote",
                "mirror",
                "--file-id",
                file_row["file_id"],
            ]
        )
        == 0
    )
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Input, Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PushModal

    project = _project_with_remotes(
        tmp_path,
        {
            "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
        },
    )
    file_row = _ingest(project, "one.fa")
    results = [
        {
            "file_id": file_row["file_id"],
            "relative_path": "raw/one.fa",
            "status": "uploaded",
            "error": None,
        }
    ]
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PullModal

    project = _project_with_remotes(
        tmp_path,
        {
            "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
        },
    )
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


@pytest.mark.bug("ODR-51")
def test_push_modal_shows_plan_results_and_stays_open_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Static

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PushModal

    project = _project_with_remotes(
        tmp_path,
        {
            "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
        },
    )
    good = _ingest(project, "one.fa")
    bad = _ingest(project, "two.fa", 2)
    results = [
        {
            "file_id": good["file_id"],
            "relative_path": good["relative_path"],
            "status": "uploaded",
            "error": None,
        },
        {
            "file_id": bad["file_id"],
            "relative_path": bad["relative_path"],
            "status": "error",
            "error": "RemoteError: connection reset",
        },
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
            await _wait_until(lambda: table.row_count == 2, "result rows")
            assert app.screen is modal, "a failed file must keep the dialog open"
            status = _static_text(await _q(modal, "#sync-status", Static))
            assert "uploaded: 1" in status and "error: 1" in status
            await _await_notification(app, severity="error")
            # Closing hands the results back so the screen can reload.
            modal.action_cancel()
            await _wait_until(lambda: bool(dismissed), "modal dismissed")

    _run(scenario())
    assert calls and calls[0][0][1] == "mirror"
    assert dismissed == [results]


@pytest.mark.bug("ODR-51")
def test_sync_modal_refuses_to_close_while_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    import threading

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PushModal

    project = _project_with_remotes(
        tmp_path,
        {
            "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
        },
    )
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
            await _await_notification(app, needle="cannot be interrupted")
            release.set()
            await _settled(app)

    _run(scenario())


@pytest.mark.bug("ODR-68")
def test_push_and_pull_buttons_open_the_dialog_prefilled_with_the_mirror(
    tmp_path: Path,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PullModal, PushModal

    project = _project_with_remotes(
        tmp_path,
        {
            "archive": {"type": "sftp", "host": "archive.example.org", "root": "/data"},
            "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
        },
    )

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
            # _q waits for the Select to exist, not for its mount-time value to
            # settle: predicate on the value, as the evict branch does (ODR-68).
            remote = await _q(modal, "#sync-remote", Select)
            await _wait_until(lambda: remote.value == "mirror", "the prefilled remote")
            assert remote.value == "mirror"
            assert modal.command_text() == "operon push --remote mirror"
            modal.action_cancel()
            await _wait_until(
                lambda: not isinstance(app.screen, PushModal), "push closed"
            )

            await _click(pilot, "#remotes-pull")
            await _wait_until(lambda: isinstance(app.screen, PullModal), "pull dialog")
            modal = app.screen
            remote = await _q(modal, "#sync-remote", Select)
            await _wait_until(lambda: remote.value == "mirror", "the prefilled remote")
            assert remote.value == "mirror"
            assert modal.command_text() == "operon pull --remote mirror"
            modal.action_cancel()

    _run(scenario())


# -- evict: the verification gate ----------------------------------------------


def test_evict_plan_reports_eligible_and_blocked_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-flight is read-only and explains every refusal."""
    project = _project_with_remotes(
        tmp_path,
        {
            "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
        },
    )
    good = _ingest(project, "one.fa")
    bad = _ingest(project, "two.fa", 2)
    calls: list[str] = []

    class _Store:
        def __init__(self, spec: Any) -> None:
            self.spec = spec

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read_manifest(self) -> dict[str, Any]:
            return {"files": {}}

    def fake_verify(
        project_arg: Project,
        name: str,
        record: dict[str, Any],
        db: Any = None,
        **kwargs: Any,
    ) -> str:
        assert db is None, "the pre-flight must not touch file_locations"
        calls.append(record["file_id"])
        if record["file_id"] == bad["file_id"]:
            raise ConflictError(
                "remote 'mirror' artifact diverges from its manifest: x.fa"
            )
        return "/data/x.fa"

    monkeypatch.setattr("operon.remotes.SFTPStore", _Store)
    monkeypatch.setattr("operon.remotes.verify_remote_record", fake_verify)

    rows = actions.evict_plan(project, "mirror")
    assert [row["file_id"] for row in rows] == sorted([good["file_id"], bad["file_id"]])
    assert calls == [row["file_id"] for row in rows]
    by_id = {row["file_id"]: row for row in rows}
    assert by_id[good["file_id"]]["eligible"] is True
    assert by_id[good["file_id"]]["reason"] == ""
    assert by_id[bad["file_id"]]["eligible"] is False
    assert "diverges" in by_id[bad["file_id"]]["reason"]

    # Nothing was recorded: the probe leaves the residency cache alone.
    assert _query_locations(project) == []


def _query_locations(project: Project) -> list[dict[str, Any]]:
    db = Database(project.db_path, read_only=True)
    try:
        return [dict(row) for row in db.query("SELECT * FROM file_locations")]
    finally:
        db.close()


def test_evict_modal_gates_confirm_on_the_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Button, DataTable, Static

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import EvictModal

    project = _project_with_remotes(
        tmp_path,
        {
            "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
        },
    )
    good = _ingest(project, "one.fa")
    bad = _ingest(project, "two.fa", 2)
    rows = [
        {
            "file_id": good["file_id"],
            "relative_path": good["relative_path"],
            "size_bytes": 123,
            "eligible": True,
            "reason": "",
        },
        {
            "file_id": bad["file_id"],
            "relative_path": bad["relative_path"],
            "size_bytes": 456,
            "eligible": False,
            "reason": "RemoteError: remote 'mirror' has no manifest entry",
        },
    ]
    plan_calls = spy_action(monkeypatch, "evict_plan", rows)
    evict_calls = spy_action(
        monkeypatch,
        "evict",
        [
            {
                "file_id": good["file_id"],
                "relative_path": good["relative_path"],
                "status": "evicted",
                "error": None,
            }
        ],
    )

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = EvictModal(project, remote="mirror")
            app.push_screen(modal)
            await _pilot_push(pilot, modal, "#evict-check")
            confirm = await _q(modal, "#confirm", Button)
            # Nothing verified yet: Confirm is refused, also on the direct path.
            assert confirm.disabled
            modal.confirm()
            await pilot.pause()
            assert evict_calls == []
            assert "verify the remote copies first" in _static_text(
                await _q(modal, "#modal-error", Static)
            )

            await _click(pilot, "#evict-check")
            table = await _q(modal, "#evict-plan", DataTable)
            await _wait_until(lambda: table.row_count == 2, "plan rows")
            assert plan_calls and plan_calls[0][0][1] == "mirror"
            gate = _static_text(await _q(modal, "#evict-gate", Static))
            assert "1 of 2 file(s) cannot be evicted" in gate
            assert confirm.disabled, "a blocked file must keep Confirm disabled"

            # A blocked file cannot be evicted even by calling confirm().
            modal.confirm()
            await pilot.pause()
            assert evict_calls == []
            assert "not verified on the mirror" in _static_text(
                await _q(modal, "#modal-error", Static)
            )

            # Fix the verdict: every file eligible → Confirm opens and runs.
            plan_calls[0][1].update(rows[1])  # the stub returns the same list
            rows[1].update(eligible=True, reason="")
            await _click(pilot, "#evict-check")
            await _wait_until(lambda: not confirm.disabled, "Confirm to open")
            assert "all 2 file(s) verified" in _static_text(
                await _q(modal, "#evict-gate", Static)
            )

            await _click(pilot, "#confirm")
            await _wait_until(lambda: len(evict_calls) == 1, "evict call")
            await _wait_until(lambda: confirm.disabled, "gate re-armed after the run")

    _run(scenario())
    args, kwargs = evict_calls[0]
    assert args == (project, "mirror", None)
    assert kwargs == {}


def test_evict_modal_rearms_the_gate_when_the_selection_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("textual")
    from textual.widgets import Button, Input, Select, Static

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import EvictModal

    project = _project_with_remotes(
        tmp_path,
        {
            "mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"},
            "other": {"type": "sftp", "host": "other.example.org", "root": "/data"},
        },
    )
    file_row = _ingest(project, "one.fa")
    rows = [
        {
            "file_id": file_row["file_id"],
            "relative_path": file_row["relative_path"],
            "size_bytes": 1,
            "eligible": True,
            "reason": "",
        }
    ]
    spy_action(monkeypatch, "evict_plan", rows)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = EvictModal(project, remote="mirror")
            app.push_screen(modal)
            await _pilot_push(pilot, modal, "#evict-check")
            confirm = await _q(modal, "#confirm", Button)
            await _click(pilot, "#evict-check")
            await _wait_until(lambda: not confirm.disabled, "Confirm to open")

            # Changing the mirror invalidates the earlier verdict.
            (await _q(modal, "#sync-remote", Select)).value = "other"
            await pilot.pause()
            assert confirm.disabled
            assert "selection changed" in _static_text(
                await _q(modal, "#evict-gate", Static)
            )

            # So does editing the file-id filter.
            await _click(pilot, "#evict-check")
            await _wait_until(lambda: not confirm.disabled, "Confirm to open again")
            (await _q(modal, "#sync-file-ids", Input)).value = file_row["file_id"]
            await pilot.pause()
            assert confirm.disabled

    _run(scenario())


# -- probe/status edge cases and sync-modal guards -----------------------------


def test_remotes_check_with_no_remotes_keeps_the_empty_state(tmp_path: Path) -> None:
    """An empty probe result stops at the empty-state render, not the summary."""
    pytest.importorskip("textual")
    from textual.widgets import Button, Static

    from operon.tui.app import OperonApp

    project = _project_with_remotes(tmp_path, {})

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            button = await _q(app.screen, "#remotes-check", Button)
            await _click(pilot, "#remotes-check")
            await _wait_until(lambda: not button.disabled, "the check to finish")
            status = _static_text(await _q(app.screen, "#remotes-status", Static))
            assert "no remotes configured" in status
            # Without the empty-payload guard this would read "checked 0 remote(s)".
            assert "checked" not in status

    _run(scenario())


def test_remotes_check_reports_a_clean_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every remote answering ok lands in the green summary and a notification."""
    pytest.importorskip("textual")
    from textual.widgets import Button, Static

    from operon.tui.app import OperonApp

    project = _project_with_remotes(
        tmp_path,
        {"mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"}},
    )
    rows = [
        {
            "name": "mirror",
            "type": "sftp",
            "address": "hpc.example.org:22",
            "root": "/data",
            "files": 2,
            "status": "ok",
            "error": "",
        }
    ]
    monkeypatch.setattr(
        actions, "check_remotes", lambda project: [dict(row) for row in rows]
    )

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            button = await _q(app.screen, "#remotes-check", Button)
            await _click(pilot, "#remotes-check")
            status = await _q(app.screen, "#remotes-status", Static)
            await _wait_until(
                lambda: "1 ok, 0 failed" in _static_text(status), "the green summary"
            )
            await _wait_until(lambda: not button.disabled, "the check to finish")
            await _await_notification(app, needle="checked 1 remote(s)")

    _run(scenario())


def test_remotes_check_surfaces_a_probe_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashing action is rendered inline and the status line is cleared."""
    pytest.importorskip("textual")
    from textual.widgets import Button, Static

    from operon.tui.app import OperonApp

    project = _project_with_remotes(
        tmp_path,
        {"mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"}},
    )

    def explode(_project: Project) -> list[dict[str, Any]]:
        raise OSError("sftp stack exploded")

    monkeypatch.setattr(actions, "check_remotes", explode)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            button = await _q(app.screen, "#remotes-check", Button)
            await _click(pilot, "#remotes-check")
            error = await _q(app.screen, "#remotes-error", Static)
            await _wait_until(
                lambda: "sftp stack exploded" in _static_text(error), "the error line"
            )
            await _wait_until(lambda: not button.disabled, "the check to finish")
            status = _static_text(await _q(app.screen, "#remotes-status", Static))
            assert "checking" not in status

    _run(scenario())


def test_remotes_check_guard_and_status_while_a_probe_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second check is refused, and a mid-probe reload keeps the status line."""
    pytest.importorskip("textual")
    import threading

    from textual.widgets import Button, DataTable, Input, Static

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import RemotesPanel

    project = _project_with_remotes(
        tmp_path,
        {"mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"}},
    )
    first = _ingest(project, "one.fa")
    second = _ingest(project, "two.fa", 2)
    _add_location(project, first, "mirror")
    _add_location(project, second, "mirror")
    release = threading.Event()
    started = threading.Event()
    rows = [
        {
            "name": "mirror",
            "type": "sftp",
            "address": "hpc.example.org:22",
            "root": "/data",
            "files": 2,
            "status": "ok",
            "error": "",
        }
    ]

    def blocking(_project: Project) -> list[dict[str, Any]]:
        started.set()
        assert release.wait(SETTLE_TIMEOUT)
        return [dict(row) for row in rows]

    monkeypatch.setattr(actions, "check_remotes", blocking)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            table = await _q(app.screen, "#locations-table", DataTable)
            await _wait_until(lambda: table.row_count == 2, "residency rows")
            button = await _q(app.screen, "#remotes-check", Button)
            await _click(pilot, "#remotes-check")
            await _wait_until(started.is_set, "the probe to start")

            # A second check while one is in flight is refused, not queued.
            app.query_one(RemotesPanel)._check()
            await _await_notification(app, needle="already running", severity="warning")

            # A residency reload while the probe runs keeps the status line.
            (await _q(app.screen, "#locations-filter", Input)).value = first["file_id"]
            await _click(pilot, "#locations-apply")
            await _wait_until(lambda: table.row_count == 1, "the filtered reload")
            status = await _q(app.screen, "#remotes-status", Static)
            assert "checking connectivity" in _static_text(status)

            release.set()
            await _wait_until(lambda: not button.disabled, "the probe to finish")
            assert "1 ok, 0 failed" in _static_text(status)
            await _settled(app)

    _run(scenario())


def test_remotes_write_buttons_warn_when_nothing_is_configured(
    tmp_path: Path,
) -> None:
    """Push/Evict without a remotes section warn instead of opening a dialog."""
    pytest.importorskip("textual")
    from textual.widgets import Button

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import EvictModal, PushModal

    project = _project_with_remotes(tmp_path, {})

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            await _q(app.screen, "#remotes-push", Button)
            await _click(pilot, "#remotes-push")
            await _click(pilot, "#remotes-evict")

            def warned_twice() -> bool:
                return (
                    sum(
                        1
                        for notification in app._notifications
                        if notification.severity == "warning"
                        and "no remotes configured" in str(notification.message)
                    )
                    >= 2
                )

            await _wait_until(warned_twice, "both warnings")
            assert not isinstance(app.screen, (EvictModal, PushModal))

    _run(scenario())


@pytest.mark.bug("ODR-68")
def test_remotes_evict_and_push_buttons_open_prefilled_dialogs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sync buttons prefill the mirror and hand their result to the screen."""
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Select

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import EvictModal, PushModal

    project = _project_with_remotes(
        tmp_path,
        {"mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"}},
    )
    results = [
        {
            "file_id": "FIL_000001",
            "relative_path": "raw/x.fa",
            "status": "uploaded",
            "error": None,
        }
    ]
    calls = spy_action(monkeypatch, "push", results)
    reloads: list[list[str] | None] = []
    real_locations = data.list_locations

    def spy(project_arg: Project, *, file_ids: Any = None, limit: int = 0) -> Any:
        reloads.append(None if file_ids is None else list(file_ids))
        return real_locations(project_arg, file_ids=file_ids, limit=limit)

    monkeypatch.setattr(data, "list_locations", spy)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            table = await _q(app.screen, "#remotes-table", DataTable)
            await _wait_until(lambda: table.row_count == 1, "remote row")

            await _click(pilot, "#remotes-evict")
            await _wait_until(
                lambda: isinstance(app.screen, EvictModal), "evict dialog"
            )
            evict = app.screen
            remote_select = await _q(evict, "#sync-remote", Select)
            await _wait_until(
                lambda: remote_select.value == "mirror", "the prefilled remote"
            )
            evict.action_cancel()
            await _wait_until(
                lambda: not isinstance(app.screen, EvictModal), "evict dialog to close"
            )

            baseline = len(reloads)
            await _click(pilot, "#remotes-push")
            await _wait_until(lambda: isinstance(app.screen, PushModal), "push dialog")
            push = app.screen
            # Same mount-time window as the evict branch above (ODR-68).
            push_select = await _q(push, "#sync-remote", Select)
            await _wait_until(
                lambda: push_select.value == "mirror", "the prefilled remote"
            )
            assert push_select.value == "mirror"
            push.confirm()
            result_table = await _q(push, "#sync-results", DataTable)
            await _wait_until(lambda: result_table.row_count == 1, "result rows")
            push.action_cancel()
            await _wait_until(
                lambda: not isinstance(app.screen, PushModal), "push dialog to close"
            )
            # A truthy result reloads every panel (the residency listing reruns).
            await _wait_until(lambda: len(reloads) > baseline, "panels reloaded")

    _run(scenario())
    assert calls and calls[0][0][1] == "mirror"


def test_remotes_locations_note_when_the_row_cap_is_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaching the residency cap points at the --file-id filter."""
    pytest.importorskip("textual")
    from textual.widgets import Static

    from operon.tui.app import OperonApp

    project = _project_with_remotes(tmp_path, {})
    _add_location(project, _ingest(project, "one.fa"), "mirror")
    # The row cap is a module constant; one row is enough to be at or above it.
    monkeypatch.setattr(data, "LOCATIONS_LIMIT", 1)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            note = await _q(app.screen, "#locations-note", Static)
            await _wait_until(
                lambda: "showing the first 1 residency rows" in _static_text(note),
                "the cap note",
            )

    _run(scenario())


def test_remotes_selected_remote_falls_back_without_a_row_or_a_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty table and a failing cursor lookup both degrade to a safe name."""
    pytest.importorskip("textual")
    from textual.widgets import DataTable

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import RemotesPanel

    project = _project_with_remotes(
        tmp_path,
        {"mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"}},
    )

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("remotes")
            await pilot.pause()
            panel = app.query_one(RemotesPanel)
            table = await _q(app.screen, "#remotes-table", DataTable)
            await _wait_until(lambda: table.row_count == 1, "remote row")

            def broken(_coordinate: Any) -> Any:
                raise RuntimeError("no cell for this coordinate")

            monkeypatch.setattr(table, "coordinate_to_cell_key", broken)
            assert panel._selected_remote() == "mirror"
            panel.remotes = []
            assert panel._selected_remote() == ""
            table.clear()
            assert panel._selected_remote() == ""

            # A button the panel does not own is ignored, not misrouted.
            from types import SimpleNamespace

            panel.on_button_pressed(
                SimpleNamespace(button=SimpleNamespace(id="foreign-button"))
            )

    _run(scenario())


def test_push_modal_renders_a_plan_error_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing plan preview is rendered as a hint, not raised out of the form."""
    pytest.importorskip("textual")
    from textual.widgets import Static

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PushModal

    project = _project_with_remotes(
        tmp_path,
        {"mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"}},
    )

    def explode(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise OSError("preview exploded")

    monkeypatch.setattr(data, "sync_preview", explode)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = PushModal(project, remote="mirror")
            await _pilot_push(pilot, modal, "#sync-plan")
            plan = await _q(modal, "#sync-plan", Static)
            await _wait_until(
                lambda: "preview exploded" in _static_text(plan), "the plan error"
            )

    _run(scenario())


def test_sync_modal_confirm_guards_and_cancel_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirm refuses an unselected/vanished remote and a running transfer."""
    pytest.importorskip("textual")
    import threading
    from types import SimpleNamespace

    from textual.widgets import Static

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import PushModal

    project = _project_with_remotes(
        tmp_path,
        {"mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"}},
    )
    release = threading.Event()
    started = threading.Event()

    def blocking(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        started.set()
        assert release.wait(SETTLE_TIMEOUT)
        return []

    monkeypatch.setattr(actions, "push", blocking)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)

            # No remote selected: Confirm refuses before any worker starts.
            untouched = PushModal(project)
            await _pilot_push(pilot, untouched, "#sync-remote")
            untouched.confirm()
            assert "select a remote first" in _static_text(
                await _q(untouched, "#modal-error", Static)
            )

            # A remote that vanished from the config between open and confirm.
            vanished = PushModal(project, remote="mirror")
            await _pilot_push(pilot, vanished, "#sync-remote")
            vanished.remotes = ["ghost"]
            vanished.confirm()
            assert "unknown remote 'mirror'" in _static_text(
                await _q(vanished, "#modal-error", Static)
            )

            # Events from widgets the dialog does not own are ignored.
            untouched.on_input_changed(
                SimpleNamespace(input=SimpleNamespace(id="foreign-input"))
            )
            untouched.on_select_changed(
                SimpleNamespace(select=SimpleNamespace(id="foreign-select"))
            )

            running = PushModal(project, remote="mirror")
            await _pilot_push(pilot, running, "#sync-remote")
            running.confirm()
            await _wait_until(started.is_set, "the transfer to start")
            running.confirm()  # refused: a run is already in flight
            await _click(pilot, "#cancel")
            await _await_notification(app, needle="cannot be interrupted")
            assert app.screen is running, "a running transfer must not close"

            release.set()
            await _settled(app)
            running.action_cancel()
            await _wait_until(
                lambda: app.screen is not running, "the running dialog to close"
            )
            vanished.action_cancel()
            await _wait_until(
                lambda: app.screen is not vanished, "the second dialog to close"
            )
            untouched.action_cancel()
            await _wait_until(
                lambda: app.screen is not untouched, "the first dialog to close"
            )

    _run(scenario())


def test_evict_modal_check_errors_stay_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing remote and an unreachable mirror both fail the gate, not the dialog."""
    pytest.importorskip("textual")
    from textual.widgets import Button, Static

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import EvictModal

    project = _project_with_remotes(
        tmp_path,
        {"mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"}},
    )

    def explode(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise OSError("mirror unreachable")

    monkeypatch.setattr(actions, "evict_plan", explode)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)

            # No mirror selected: the pre-flight refuses before probing.
            bare = EvictModal(project)
            await _pilot_push(pilot, bare, "#evict-check")
            (await _q(bare, "#evict-check", Button)).press()
            error = await _q(bare, "#modal-error", Static)
            await _wait_until(
                lambda: "select a remote first" in _static_text(error),
                "the missing-remote error",
            )
            assert (await _q(bare, "#evict-check", Button)).disabled is False
            bare.action_cancel()
            await _wait_until(
                lambda: not isinstance(app.screen, EvictModal), "the dialog to close"
            )

            # A mirror that cannot be reached fails the gate, not the dialog.
            modal = EvictModal(project, remote="mirror")
            await _pilot_push(pilot, modal, "#evict-check")
            await _click(pilot, "#evict-check")
            gate = await _q(modal, "#evict-gate", Static)
            await _wait_until(
                lambda: "verification failed" in _static_text(gate),
                "the gate to re-arm",
            )
            assert "mirror unreachable" in _static_text(
                await _q(modal, "#modal-error", Static)
            )
            assert (await _q(modal, "#confirm", Button)).disabled

    _run(scenario())


def test_evict_modal_guards_while_the_check_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second check and a confirm() during the pre-flight are both refused."""
    pytest.importorskip("textual")
    import threading

    from textual.widgets import Button, Static

    from operon.tui.app import OperonApp
    from operon.tui.screens.remotes import EvictModal

    project = _project_with_remotes(
        tmp_path,
        {"mirror": {"type": "sftp", "host": "hpc.example.org", "root": "/data"}},
    )
    release = threading.Event()
    started = threading.Event()

    def blocking(
        _project: Project, _remote: str, _file_ids: Any = None
    ) -> list[dict[str, Any]]:
        started.set()
        assert release.wait(SETTLE_TIMEOUT)
        return []

    monkeypatch.setattr(actions, "evict_plan", blocking)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = EvictModal(project, remote="mirror")
            await _pilot_push(pilot, modal, "#evict-check")
            await _click(pilot, "#evict-check")
            await _wait_until(started.is_set, "the pre-flight to start")

            # A second pre-flight while one runs is refused.
            modal._check()
            assert modal.checking is True

            # confirm() during the check is refused: no gate error is written.
            modal.confirm()
            assert "verify the remote copies first" not in _static_text(
                await _q(modal, "#modal-error", Static)
            )
            assert app.screen is modal

            release.set()
            gate = await _q(modal, "#evict-gate", Static)
            await _wait_until(
                lambda: "nothing selected" in _static_text(gate),
                "the empty-plan gate",
            )
            assert (await _q(modal, "#confirm", Button)).disabled
            modal.action_cancel()
            await _wait_until(
                lambda: not isinstance(app.screen, EvictModal), "the dialog to close"
            )
            await _settled(app)

    _run(scenario())
