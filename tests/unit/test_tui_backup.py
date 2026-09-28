"""Backup TUI: actions.create_backup/verify_backup and the Home-screen modals.

The actions mirror ``operon backup create`` / ``operon backup verify``: create
runs on a read-only session exactly like the CLI and produces the same
checksum manifest; verify authenticates an existing backup directory without
touching project state.  The create modal refuses to close while running; the
verify modal renders its result in place (nothing is written).
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("textual")

import shlex

from rich.text import Text
from textual.widgets import Button, Input, Select, Static

from operon.cli import main as cli_main
from operon.config import Project
from operon.errors import ConflictError, ValidationError
from operon.tui import actions
from operon.tui.app import OperonApp
from operon.tui.screens.backup import (
    BACKUP_SCOPES,
    BackupModal,
    VerifyBackupModal,
    backup_verify_text,
)
from operon.tui.screens.home import HomePanel
from tests import helpers
from tests.tui_helpers import click as _click

SCENARIO_TIMEOUT = 180.0
SETTLE_TIMEOUT = 30.0
#: Budget for a worker result crossing back from its thread to the UI, and for
#: the screen teardown that follows it (ODR-0046).  Those steps have no upper
#: bound a loaded machine cannot exceed: a busy runner once left the dismissal
#: of a cancelled run past the 30 s SETTLE_TIMEOUT and reddened the suite with
#: no product fault behind it.  The scenario cap above is three times this
#: budget so a wait may legitimately use all of it, and a real hang still fails
#: here instead of at a red suite on a busy CI runner.
HANDOFF_TIMEOUT = 120.0


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """Each test gets its own freshly initialized project."""
    return Project.init(tmp_path / "project")


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


async def _wait_until(predicate: Callable[[], bool], description: str,
                      timeout: float = SETTLE_TIMEOUT) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not {description} within {timeout}s")
        await asyncio.sleep(0.05)


async def _push(pilot, modal, selector: str | None = None) -> None:
    """Push a screen and wait until its form **and** buttons have composed."""
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
        except TimeoutError:
            if attempt:
                raise
            await pilot.pause()
            continue
        await pilot.pause()
        return


async def _q(modal, selector: str, *types):
    await _wait_until(lambda: len(modal.query(selector)) > 0, f"{selector} to exist")
    return modal.query_one(selector, *types)


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


def _notifications(app) -> list[tuple[str, str]]:
    return [(n.severity, str(n.message)) for n in app._notifications]


def parse_command_text(text: str):
    """Parse a modal's equivalent-command preview with the real CLI parser."""
    from operon.cli import _parser

    argv = shlex.split(text)
    assert argv and argv[0] == "operon", f"unexpected command preview: {text!r}"
    return _parser().parse_args(argv[1:])


def _manifest(path: Path) -> dict:
    return json.loads((path / "backup-manifest.json").read_text(encoding="utf-8"))


def _comparable(manifest: dict) -> dict:
    """A manifest with volatile parts dropped (timestamps; sqlite page bytes)."""
    comparable = {key: value for key, value in manifest.items() if key != "created_at"}
    comparable["files"] = [
        item for item in comparable["files"] if item["relative_path"] != "operon.sqlite"
    ]
    return comparable


# ---------------------------------------------------------------------------
# actions.create_backup / actions.verify_backup
# ---------------------------------------------------------------------------


def test_backup_create_matches_the_cli(tmp_path: Path, capsys) -> None:
    template = Project.init(tmp_path / "template")
    cli_project = Project.find(helpers.copy_project_tree(template.root, tmp_path / "cli"))
    tui_project = Project.find(helpers.copy_project_tree(template.root, tmp_path / "tui"))
    cli_out = tmp_path / "cli-backup"
    tui_out = tmp_path / "tui-backup"

    rc = cli_main(
        ["--project", str(cli_project.root), "backup", "create", "--output", str(cli_out)]
    )
    capsys.readouterr()
    assert rc == 0

    result = actions.create_backup(tui_project, str(tui_out))
    assert result == {"path": str(tui_out.resolve()), "scope": "control",
                      "file_count": result["file_count"]}
    assert result["file_count"] > 0

    assert _comparable(_manifest(cli_out)) == _comparable(_manifest(tui_out))
    # Both outputs verify, and each verifier accepts the other's directory.
    assert actions.verify_backup(str(cli_out))["ok"] is True
    assert actions.verify_backup(str(tui_out))["ok"] is True


def test_backup_create_validation_scopes_and_collisions(project: Project,
                                                        tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="output path is required"):
        actions.create_backup(project, "   ")

    with pytest.raises(ValidationError, match="scope must be one of"):
        actions.create_backup(project, str(tmp_path / "b"), scope="everything")

    # The destination is never inside the project root and never overwritten.
    with pytest.raises(ValidationError, match="outside the project root"):
        actions.create_backup(project, str(project.root / "inside-backup"))

    (project.raw_root / "kept.bin").write_bytes(b"raw bytes\n")
    full_out = tmp_path / "full-backup"
    result = actions.create_backup(project, str(full_out), scope="full")
    assert result["scope"] == "full"
    manifest = _manifest(full_out)
    assert manifest["scope"] == "full"
    assert "raw/kept.bin" in {item["relative_path"] for item in manifest["files"]}
    assert len(BACKUP_SCOPES) == 3

    with pytest.raises(ConflictError, match="already exists"):
        actions.create_backup(project, str(full_out))


def test_verify_backup_reports_corruption_missing_and_unexpected(
        project: Project, tmp_path: Path) -> None:
    out = tmp_path / "backup"
    actions.create_backup(project, str(out))
    assert actions.verify_backup(str(out)) == {
        "path": str(out.resolve()), "scope": "control",
        "checked": len(_manifest(out)["files"]), "unexpected": 0,
        "ok": True, "failures": [],
    }

    # Same bytes length, different content: the checksum, not the size, fails.
    project_yaml = out / "project.yaml"
    original = project_yaml.read_text(encoding="utf-8")
    project_yaml.write_text(original.replace("project", "proiect", 1), encoding="utf-8")
    assert project_yaml.stat().st_size == len(original.encode("utf-8"))

    # A missing entry: pick a config file the control scope copied.
    config_files = sorted((out / "config").rglob("*.yaml"))
    assert config_files, "the control scope copies config/"
    victim = config_files[-1]
    victim_rel = victim.relative_to(out).as_posix()
    victim.unlink()

    (out / "stray.bin").write_bytes(b"not in the manifest\n")

    result = actions.verify_backup(str(out))
    assert result["ok"] is False
    failures = {(item["relative_path"], item["error"]) for item in result["failures"]}
    assert ("project.yaml", "checksum mismatch") in failures
    assert (victim_rel, "missing") in failures
    assert result["unexpected"] == 1  # stray.bin; project.yaml still exists
    assert any(item["error"] == "unexpected file" for item in result["failures"])

    with pytest.raises(ValidationError, match="manifest is missing"):
        actions.verify_backup(str(tmp_path / "no-backup"))
    with pytest.raises(ValidationError, match="backup path is required"):
        actions.verify_backup("  ")


def test_backup_verify_text_renders_ok_and_failures() -> None:
    ok_text = backup_verify_text({
        "path": "/tmp/b", "scope": "control", "checked": 4, "unexpected": 0,
        "ok": True, "failures": [],
    }).plain
    assert "OK" in ok_text and "checked    4" in ok_text

    failed_text = backup_verify_text({
        "path": "/tmp/b", "scope": "full", "checked": 4, "unexpected": 1,
        "ok": False,
        "failures": [{"relative_path": "a.tsv", "error": "size mismatch"}],
    }).plain
    assert "FAILED" in failed_text
    assert "a.tsv: size mismatch" in failed_text


# ---------------------------------------------------------------------------
# Home-screen entry points and the modals
# ---------------------------------------------------------------------------


def test_home_screen_opens_backup_modals(project: Project) -> None:
    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            assert isinstance(app.screen.query_one("#home", HomePanel), HomePanel)
            await _click(pilot, "#home-backup-create")
            await _wait_until(lambda: isinstance(app.screen, BackupModal),
                              "backup modal to open")
            await pilot.press("escape")
            await _wait_until(lambda: not isinstance(app.screen, BackupModal),
                              "backup modal to close")
            await _click(pilot, "#home-backup-verify")
            await _wait_until(lambda: isinstance(app.screen, VerifyBackupModal),
                              "verify modal to open")

    _run(scenario())


def test_backup_modal_command_text_matches_action_kwargs(
        project: Project, tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[tuple, dict]] = []
    output = tmp_path / "modal-backup"

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return {"path": str(output), "scope": kwargs.get("scope", "control"),
                "file_count": 7}

    monkeypatch.setattr(actions, "create_backup", stub)
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = BackupModal(project)
            app.push_screen(modal, dismissed.append)
            await _push(pilot, modal, "#backup-output")
            # Empty form: the preview keeps the required flag with a placeholder.
            assert modal.command_text() == "operon backup create --output '…'"
            # Defaults: the preview omits --scope, so the CLI default and the
            # kwargs must agree.
            namespace = parse_command_text(modal.command_text())
            assert namespace.scope == "control"
            (await _q(modal, "#backup-output", Input)).value = str(output)
            await pilot.pause()
            namespace = parse_command_text(modal.command_text())
            assert namespace.output == str(output) and namespace.scope == "control"
            (await _q(modal, "#backup-scope", Select)).value = "full"
            await pilot.pause()
            namespace = parse_command_text(modal.command_text())
            assert namespace.scope == "full"
            await _click(pilot, "#confirm")
            await _wait_until(lambda: bool(dismissed), "backup modal dismissed")

    _run(scenario())
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (project, str(output))
    assert kwargs == {"scope": "full"}


def test_backup_modal_requires_output_and_shows_core_error_inline(
        project: Project, tmp_path: Path) -> None:
    existing = tmp_path / "taken"
    actions.create_backup(project, str(existing))

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = BackupModal(project)
            app.push_screen(modal)
            await _push(pilot, modal, "#backup-output")

            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "--output is required" in _static_text(
                    app.screen.query_one("#modal-error", Static)),
                "missing --output error")
            assert app.screen is modal
            assert app.screen.query_one("#confirm", Button).disabled is False

            (await _q(modal, "#backup-output", Input)).value = str(existing)
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "already exists" in _static_text(
                    app.screen.query_one("#modal-error", Static)),
                "conflict error inline")
            assert app.screen is modal
            assert not any(severity == "information" for severity, _ in _notifications(app))

    _run(scenario())


def test_backup_modal_refuses_cancel_while_running(project: Project,
                                                   tmp_path: Path, monkeypatch) -> None:
    released = threading.Event()
    started = threading.Event()

    def blocking_create(*args, **kwargs):
        started.set()
        released.wait(HANDOFF_TIMEOUT)
        return {"path": "/tmp/slow-backup", "scope": "control", "file_count": 1}

    monkeypatch.setattr(actions, "create_backup", blocking_create)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = BackupModal(project)
            app.push_screen(modal)
            await _push(pilot, modal, "#backup-output")
            (await _q(modal, "#backup-output", Input)).value = str(tmp_path / "slow")
            await _click(pilot, "#confirm")
            await _wait_until(started.is_set, "create to have reached the core",
                              timeout=HANDOFF_TIMEOUT)
            await _click(pilot, "#cancel")
            await _wait_until(
                lambda: any(severity == "warning" and "cannot be interrupted" in message
                            for severity, message in _notifications(app)),
                "refusal notification")
            assert app.screen is modal
            released.set()
            await _wait_until(lambda: app.screen is not modal, "modal to close",
                              timeout=HANDOFF_TIMEOUT)

    try:
        _run(scenario())
    finally:
        released.set()


def test_verify_modal_renders_result_and_stays_open(project: Project,
                                                    tmp_path: Path) -> None:
    good = tmp_path / "good-backup"
    actions.create_backup(project, str(good))
    bad = tmp_path / "bad-backup"
    actions.create_backup(project, str(bad))
    (bad / "project.yaml").write_text("broken: true\n", encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 60)) as pilot:
            await _settled(app)
            modal = VerifyBackupModal()
            app.push_screen(modal)
            await _push(pilot, modal, "#backup-verify-input")
            # Empty input: the preview keeps a placeholder and Confirm refuses.
            assert modal.command_text() == "operon backup verify --input '…'"
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "--input is required" in _static_text(
                    app.screen.query_one("#modal-error", Static)),
                "missing --input error")

            (await _q(modal, "#backup-verify-input", Input)).value = str(good)
            await pilot.pause()
            assert modal.command_text() == f"operon backup verify --input {good}"
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "result:" in _static_text(
                    app.screen.query_one("#backup-verify-results", Static)),
                "verify result to render")
            assert app.screen is modal  # the result stays on screen
            text = _static_text(app.screen.query_one("#backup-verify-results", Static))
            assert "OK" in text
            assert any(severity == "information" and "OK" in message
                       for severity, message in _notifications(app))

            # The dialog can verify the next directory without reopening.
            (await _q(modal, "#backup-verify-input", Input)).value = str(bad)
            await _click(pilot, "#confirm")
            await _wait_until(
                lambda: "FAILED" in _static_text(
                    app.screen.query_one("#backup-verify-results", Static)),
                "failure result to render")
            text = _static_text(app.screen.query_one("#backup-verify-results", Static))
            assert "project.yaml" in text
            assert any(severity == "error" and "problem(s)" in message
                       for severity, message in _notifications(app))
            assert app.screen is modal

    _run(scenario())


def test_verify_modal_drops_result_after_teardown(project: Project,
                                                  tmp_path: Path, monkeypatch) -> None:
    """A result that lands after the dialog was closed must not raise."""
    released = threading.Event()
    started = threading.Event()

    def blocking_verify(path):
        started.set()
        released.wait(HANDOFF_TIMEOUT)
        return {"path": str(path), "scope": "control", "checked": 1,
                "unexpected": 0, "ok": True, "failures": []}

    monkeypatch.setattr(actions, "verify_backup", blocking_verify)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = VerifyBackupModal()
            app.push_screen(modal)
            await _push(pilot, modal, "#backup-verify-input")
            (await _q(modal, "#backup-verify-input", Input)).value = str(tmp_path / "b")
            await _click(pilot, "#confirm")
            await _wait_until(started.is_set, "verify to have reached the core",
                              timeout=HANDOFF_TIMEOUT)
            # Cancel while running is refused; unlock, then dismiss and let the
            # worker deliver into a torn-down tree.
            await _click(pilot, "#cancel")
            assert app.screen is modal
            modal.dismiss(None)
            await _wait_until(lambda: app.screen is not modal, "modal to close")
            released.set()
            await _settled(app)

    try:
        _run(scenario())
    finally:
        released.set()
