"""Captured-environment browser for the Tasks screen.

Read-only mirror of the ``operon environments`` group: the same table as
``environments list``, the verbatim JSON document of ``environments show``,
and the Conda reconstruction specifications of ``environments export``.  The
CLI prints these to stdout, so the TUI's counterpart is a read-only text area
— writing the spec to a file stays a CLI redirection (``operon environments
export <id> > spec.yaml``).
"""

from __future__ import annotations

import json
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, RichLog, Static

from operon.config import Project
from operon.tui import data
from operon.tui.screens.common import DismissOnce, WorkerResults


class EnvironmentsModal(DismissOnce, WorkerResults, ModalScreen):
    """Read-only table of captured environments with show/export views."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self.environments: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("Captured environments", id="modal-title")
            yield DataTable(id="environments-table", cursor_type="row")
            yield RichLog(id="environments-output", max_lines=1000, wrap=True, markup=False)
            yield Static("", id="environments-error")
            with Horizontal(id="modal-buttons"):
                yield Button("View JSON", id="environments-show")
                yield Button("Export explicit", id="environments-explicit")
                yield Button("Export yaml", id="environments-yaml")
                yield Button("Close", id="cancel")

    def on_mount(self) -> None:
        table = self.query_one("#environments-table", DataTable)
        table.add_columns("environment_id", "created_at", "summary")
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        try:
            payload: Any = data.list_environments(self.project)
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal
            payload = exc
        self.post_to_ui(self._apply, payload)

    def _apply(self, payload: Any) -> None:
        if isinstance(payload, BaseException):
            self._show_error(payload)
            return
        self.environments = payload
        table = self.query_one("#environments-table", DataTable)
        table.clear()
        for row in payload:
            table.add_row(
                str(row["environment_id"]),
                str(row.get("created_at") or "-"),
                str(row.get("summary") or "-"),
                key=str(row["environment_id"]),
            )

    def _selected(self) -> dict[str, Any] | None:
        table = self.query_one("#environments-table", DataTable)
        if not self.environments or table.cursor_row is None:
            return None
        if 0 <= table.cursor_row < len(self.environments):
            return self.environments[table.cursor_row]
        return None

    def _show_error(self, exc: BaseException | str) -> None:
        self.query_one("#environments-error", Static).update(Text(str(exc), style="red"))

    def _clear_error(self) -> None:
        self.query_one("#environments-error", Static).update("")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        row = self._selected()
        if row is None:
            self._show_error("select an environment row first")
            return
        environment_id = str(row["environment_id"])
        output = self.query_one("#environments-output", RichLog)
        try:
            if event.button.id == "environments-show":
                document = data.environment_document(self.project, environment_id)
                text = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)
            else:
                fmt = "explicit" if event.button.id == "environments-explicit" else "yaml"
                text = data.export_environment(self.project, environment_id, fmt).rstrip("\n")
        except Exception as exc:  # noqa: BLE001 - shown inline
            self._show_error(exc)
            return
        self._clear_error()
        output.clear()
        output.write(text)
