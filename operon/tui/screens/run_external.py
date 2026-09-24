"""Run-external modal: launch one external command from the Tasks screen.

Mirrors ``operon run-external`` field by field.  The command line is split with
the same ``shlex`` rules (shell quoting, no pipes or redirections), inputs and
expected outputs are comma-separated form values rendered as repeated CLI
flags, and the backend select preflights its choice exactly like the analyze
modal.  A fresh run id is allocated at submission — the same note the CLI
prints — and a command that ran but failed is a *recorded* outcome: the run row
exists, so the modal reports the failure and hands the run id back to the
Tasks screen, which opens the full record.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import Any

from textual.css.query import NoMatches
from textual.widgets import Button, Input, Select, Static

from operon.config import Project
from operon.errors import ValidationError
from operon.tui import actions
from operon.tui.screens.common import (
    ENTITY_TYPE_OPTIONS,
    WriteModal,
    backend_select_options,
)


def split_list(text: str) -> list[str]:
    """Comma-separated form input -> the repeated CLI flag values."""
    return [item.strip() for item in text.split(",") if item.strip()]


class RunExternalModal(WriteModal):
    """Form + preview for ``operon run-external``."""

    def __init__(self, project: Project) -> None:
        super().__init__("Run external command")
        self.project = project
        self.running = False
        self._options: dict[str, Any] = {}
        self._step = ""
        self._command_line = ""

    def compose_form(self) -> Iterable[Any]:
        yield Static("Step (required; recorded as the workflow step)", classes="modal-label")
        yield Input(placeholder="step, e.g. busco / quast / fastp", id="external-step")
        yield Static("Command (required; split with shell-like quoting — no pipes or redirects)",
                     classes="modal-label")
        yield Input(placeholder="e.g. busco -i in.fa -o out -m genome", id="external-command")
        yield Static("Entity (optional)", classes="modal-label")
        yield Select(ENTITY_TYPE_OPTIONS, id="external-entity-type", allow_blank=True)
        yield Input(placeholder="entity id", id="external-entity-id")
        yield Static("Tool (optional; its version is detected from config/tools.yaml)",
                     classes="modal-label")
        yield Input(placeholder="tool name", id="external-tool")
        yield Input(placeholder="parameter set label", id="external-parameter-set")
        yield Input(placeholder="inputs (comma-separated; hashed for provenance)",
                    id="external-inputs")
        yield Input(placeholder="expected outputs (comma-separated; must exist and be non-empty)",
                    id="external-expected-outputs")
        yield Input(placeholder="threads (blank = project default)", id="external-threads")
        yield Input(placeholder="working directory (blank = project root)", id="external-cwd")
        yield Input(placeholder="timeout seconds (blank = none)", id="external-timeout")
        yield Static("Execution backend", classes="modal-label")
        yield Select(backend_select_options(self.project), value="", id="external-backend")
        yield Static(
            "A fresh run id (WF_…) is allocated at submission; logs land in the project's "
            "logs/ directory (`operon workflow show <id> --follow` streams them on the CLI, "
            "which can also interrupt a running command).",
            id="external-run-note", classes="modal-info",
        )
        yield Static("", id="external-status", classes="modal-info")

    # -- form values and preview ---------------------------------------------

    def _form_values(self) -> dict[str, Any]:
        entity_value = self.query_one("#external-entity-type", Select).value
        backend_value = self.query_one("#external-backend", Select).value
        return {
            "step": self.query_one("#external-step", Input).value.strip(),
            "command_line": self.query_one("#external-command", Input).value.strip(),
            "entity_type": "" if entity_value is Select.NULL else str(entity_value),
            "entity_id": self.query_one("#external-entity-id", Input).value.strip(),
            "tool": self.query_one("#external-tool", Input).value.strip(),
            "parameter_set": self.query_one("#external-parameter-set", Input).value.strip(),
            "inputs": split_list(self.query_one("#external-inputs", Input).value),
            "expected_outputs": split_list(
                self.query_one("#external-expected-outputs", Input).value),
            "threads": self.query_one("#external-threads", Input).value.strip(),
            "cwd": self.query_one("#external-cwd", Input).value.strip(),
            "timeout": self.query_one("#external-timeout", Input).value.strip(),
            "backend": "" if backend_value is Select.NULL else str(backend_value),
        }

    def command_text(self) -> str:
        values = self._form_values()
        parts = [
            "operon", "run-external",
            "--step", shlex.quote(values["step"] or "…"),
            "--command", shlex.quote(values["command_line"] or "…"),
        ]
        for field, flag in (("entity_type", "--entity-type"), ("entity_id", "--entity-id"),
                            ("parameter_set", "--parameter-set"), ("tool", "--tool")):
            if values[field]:
                parts += [flag, shlex.quote(values[field])]
        for value in values["inputs"]:
            parts += ["--input", shlex.quote(value)]
        for value in values["expected_outputs"]:
            parts += ["--expected-output", shlex.quote(value)]
        for field, flag in (("threads", "--threads"), ("cwd", "--cwd"), ("timeout", "--timeout")):
            if values[field]:
                parts += [flag, shlex.quote(values[field])]
        if values["backend"]:
            parts += ["--backend", shlex.quote(values["backend"])]
        return " ".join(parts)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("external-"):
            self.refresh_command()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id and event.select.id.startswith("external-"):
            self.refresh_command()

    # -- execution ------------------------------------------------------------

    @staticmethod
    def _positive_int(text: str, label: str) -> int | None:
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            raise ValidationError(f"{label} must be a positive integer") from None
        if value <= 0:
            raise ValidationError(f"{label} must be a positive integer")
        return value

    @staticmethod
    def _positive_float(text: str, label: str) -> float | None:
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            raise ValidationError(f"{label} must be a positive number") from None
        if value <= 0:
            raise ValidationError(f"{label} must be a positive number")
        return value

    def confirm(self) -> None:
        if self.running:
            return
        values = self._form_values()
        if not values["step"]:
            self.show_error("step is required")
            return
        if not values["command_line"]:
            self.show_error("command is required")
            return
        try:
            threads = self._positive_int(values["threads"], "threads")
            timeout = self._positive_float(values["timeout"], "timeout")
            # Same validation the CLI applies, before any worker starts.
            if not shlex.split(values["command_line"]):
                raise ValidationError("--command must not be empty")
            actions.preflight_backend(self.project, values["backend"] or None)
        except ValidationError as exc:
            self.show_error(exc)
            return
        self._step = values["step"]
        self._command_line = values["command_line"]
        self._options = {
            "entity_type": values["entity_type"] or None,
            "entity_id": values["entity_id"] or None,
            "parameter_set": values["parameter_set"] or None,
            "tool": values["tool"] or None,
            "inputs": values["inputs"],
            "expected_outputs": values["expected_outputs"],
            "threads": threads,
            "cwd": values["cwd"] or None,
            "timeout": timeout,
            "backend": values["backend"] or None,
        }
        self.running = True
        self._set_controls_disabled(True)
        self.clear_error()
        self.query_one("#external-status", Static).update("running…")
        self.run_action(lambda: actions.run_external(
            self.project, self._step, self._command_line, **self._options))

    def _set_controls_disabled(self, disabled: bool) -> None:
        for widget in self.query("Input, Select"):
            widget.disabled = disabled

    def _action_done(self, payload: Any) -> None:
        self.running = False
        self._set_controls_disabled(False)
        try:
            self.query_one("#external-status", Static).update("")
        except NoMatches:  # pragma: no cover - modal teardown race
            pass
        super()._action_done(payload)

    def on_action_success(self, payload: dict[str, Any]) -> None:
        if payload.get("status") == "completed":
            self.app.notify(f"run {payload['run_id']}: {payload['step']} completed")
        else:
            self.app.notify(
                f"run {payload['run_id']}: {payload['step']} failed — "
                f"{payload.get('error') or 'unknown error'}",
                severity="error",
            )
        self.dismiss(payload)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel" and self.running:
            # ODR-0043: stop the MRO walk so WriteModal cannot dismiss mid-run.
            event.prevent_default()
            self.action_cancel()
            return
        super().on_button_pressed(event)

    def action_cancel(self) -> None:
        if self.running:
            # The core has no cooperative cancel for a single external command:
            # its interrupt path is signal-driven (the CLI's Ctrl+C).
            self.app.notify(
                "a running command cannot be interrupted from the TUI; "
                "the CLI can (Ctrl+C)",
                severity="warning",
            )
            return
        self.dismiss(None)
