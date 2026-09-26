"""Backup modals: create and verify checksum-manifested project backups.

Mirrors ``operon backup create`` and ``operon backup verify``: the runs go
through the same core functions, so the produced directory, its
``backup-manifest.json`` and the verification semantics are identical to the
CLI (a backup created here verifies with the CLI and vice versa).  Neither
core call has a cooperative cancel, so a running form refuses to close (the
``TaxonomyImportModal`` pattern) instead of pretending.  Verification is
read-only: its result — checked/unexpected counts and per-file failures —
stays on screen instead of dismissing the dialog.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import Any

from rich.text import Text
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Select, Static

from operon.config import Project
from operon.tui import actions
from operon.tui.screens.common import WriteModal

#: The CLI's ``--scope`` choices, in parser order (the TUI never invents one).
BACKUP_SCOPES = ("control", "results", "full")


class BackupModal(WriteModal):
    """Form + confirm for ``operon backup create``."""

    def __init__(self, project: Project) -> None:
        super().__init__("Create backup")
        self.project = project
        self.running = False

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            "create a consistent SQLite-centered backup: the selected scope's "
            "project paths are copied into a staging directory next to the "
            "destination, the database is snapshotted through SQLite's backup "
            "API, and every file is recorded with its sha256 in "
            "backup-manifest.json.  The destination must not exist and must be "
            "outside the project root; verify it afterwards with the CLI or "
            "the Verify backup dialog.",
            classes="modal-info",
        )
        yield Select(
            [(scope, scope) for scope in BACKUP_SCOPES],
            value="control",
            allow_blank=False,
            id="backup-scope",
        )
        yield Input(placeholder="output directory (--output)", id="backup-output")
        yield Static("", id="backup-status", classes="modal-info")

    def _values(self) -> dict[str, str]:
        scope = self.query_one("#backup-scope", Select).value
        return {
            "output": self.query_one("#backup-output", Input).value.strip(),
            "scope": "" if scope is Select.NULL else str(scope),
        }

    def command_text(self) -> str:
        values = self._values()
        parts = ["operon", "backup", "create"]
        # --output is required by the parser, so an empty form keeps the flag
        # with a placeholder instead of dropping it (the RunExternalModal shape).
        parts += ["--output",
                  shlex.quote(values["output"]) if values["output"] else "'…'"]
        if values["scope"] and values["scope"] != "control":
            parts += ["--scope", values["scope"]]
        return " ".join(parts)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "backup-scope":
            self.refresh_command()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "backup-output":
            self.refresh_command()

    def confirm(self) -> None:
        if self.running:
            return
        values = self._values()
        if not values["output"]:
            self.show_error("--output is required")
            return
        self.running = True
        self._set_controls_disabled(True)
        self.clear_error()
        self.query_one("#backup-status", Static).update(
            "creating backup… (a running backup cannot be interrupted)"
        )
        self.run_action(lambda: actions.create_backup(
            self.project, values["output"], scope=values["scope"]))

    def _set_controls_disabled(self, disabled: bool) -> None:
        for widget in self.query("Input, Select"):
            widget.disabled = disabled

    def _action_done(self, payload: Any) -> None:
        self.running = False
        self._set_controls_disabled(False)
        try:
            self.query_one("#backup-status", Static).update("")
        except NoMatches:  # pragma: no cover - modal teardown race
            pass
        super()._action_done(payload)

    def on_action_success(self, payload: dict[str, Any]) -> None:
        self.app.notify(
            f"backup written to {payload['path']} "
            f"({payload['file_count']} file(s), scope {payload['scope']})"
        )
        self.dismiss(payload)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel" and self.running:
            # Textual dispatches a message to every MRO class defining the
            # handler (ODR-0043); prevent_default keeps WriteModal's own
            # on_button_pressed from dismissing the modal mid-run.
            event.prevent_default()
            self.action_cancel()
            return
        # ODR-0047: the MRO dispatch would run WriteModal's handler a second time.
        event.prevent_default()
        super().on_button_pressed(event)

    def action_cancel(self) -> None:
        if self.running:
            self.app.notify(
                "a running backup cannot be interrupted from the TUI",
                severity="warning",
            )
            return
        self.dismiss(None)


class VerifyBackupModal(WriteModal):
    """Form + result view for ``operon backup verify``.

    The result is the point, so a completed verification renders its summary
    and failure list in the dialog and leaves it open; ``esc``/Cancel closes
    it.  Nothing is written either way.
    """

    def __init__(self) -> None:
        super().__init__("Verify backup")
        self.running = False

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            "verify every file recorded in a backup's manifest: each entry's "
            "size and sha256 is re-checked, symlink targets are compared, and "
            "files that are not in the manifest are reported as unexpected.  "
            "Read-only — the backup is never modified.",
            classes="modal-info",
        )
        yield Input(placeholder="backup directory (--input)", id="backup-verify-input")
        yield Static("", id="backup-verify-status", classes="modal-info")
        yield Static("", id="backup-verify-results")

    def _input_path(self) -> str:
        return self.query_one("#backup-verify-input", Input).value.strip()

    def command_text(self) -> str:
        path = self._input_path()
        if not path:
            return "operon backup verify --input '…'"
        return f"operon backup verify --input {shlex.quote(path)}"

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "backup-verify-input":
            self.refresh_command()

    def confirm(self) -> None:
        if self.running:
            return
        path = self._input_path()
        if not path:
            self.show_error("--input is required")
            return
        self.running = True
        self.query_one("#backup-verify-input", Input).disabled = True
        self.clear_error()
        self.query_one("#backup-verify-status", Static).update(
            "verifying… (every file is re-hashed; this cannot be interrupted)"
        )
        self.run_action(lambda: actions.verify_backup(path))

    def _action_done(self, payload: Any) -> None:
        self.running = False
        try:
            self.query_one("#backup-verify-input", Input).disabled = False
            self.query_one("#backup-verify-status", Static).update("")
        except NoMatches:  # pragma: no cover - modal teardown race
            pass
        super()._action_done(payload)

    def on_action_success(self, payload: dict[str, Any]) -> None:
        """Render the result and keep the dialog open (nothing was written)."""
        self.query_one("#backup-verify-results", Static).update(
            backup_verify_text(payload))
        if payload["ok"]:
            self.app.notify(
                f"backup {payload['path']}: OK "
                f"({payload['checked']} file(s) checked)"
            )
        else:
            self.app.notify(
                f"backup {payload['path']}: {len(payload['failures'])} problem(s)",
                severity="error",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel" and self.running:
            event.prevent_default()
            self.action_cancel()
            return
        # ODR-0047: the MRO dispatch would run WriteModal's handler a second time.
        event.prevent_default()
        super().on_button_pressed(event)

    def action_cancel(self) -> None:
        if self.running:
            self.app.notify(
                "a running backup verification cannot be interrupted from the TUI",
                severity="warning",
            )
            return
        self.dismiss(None)


def backup_verify_text(payload: dict[str, Any]) -> Text:
    """Render a ``verify_backup`` payload the way the modal shows it."""
    text = Text()
    style = "green" if payload.get("ok") else "red"
    text.append("result: ", style="bold")
    text.append("OK\n" if payload.get("ok") else "FAILED\n", style=style)
    text.append(f"  path       {payload.get('path', '-')}\n")
    text.append(f"  scope      {payload.get('scope') or '-'}\n")
    text.append(f"  checked    {payload.get('checked', 0)}\n")
    text.append(f"  unexpected {payload.get('unexpected', 0)}\n")
    failures = payload.get("failures") or []
    if failures:
        text.append("failures\n", style="bold underline")
        for failure in failures:
            text.append(
                f"  {failure.get('relative_path', '-')}: "
                f"{failure.get('error', '-')}\n",
                style="red",
            )
    return text
