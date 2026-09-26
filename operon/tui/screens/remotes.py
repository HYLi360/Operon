"""Remotes screen: configured SFTP mirrors and file residency.

Lists the remotes from ``project.yaml`` (``operon remotes`` rows) without any
network access on load, tests connectivity on demand through the same core
``check_remote`` the CLI uses, and shows the project-wide local/remote
residency listing behind ``operon locations`` (shared core query, same columns
and ordering).
"""

from __future__ import annotations

import shlex
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Input, Static

from operon.config import Project
from operon.tui import actions, data
from operon.tui.screens.common import Panel


def parse_file_ids(value: str) -> list[str]:
    """Split a comma/whitespace separated file-id list (CLI ``--file-id``).

    Mirrors the CLI's handling of a repeated ``--file-id``: blank entries are
    dropped and duplicates keep their first position.
    """
    seen: list[str] = []
    for chunk in value.replace(",", " ").split():
        if chunk not in seen:
            seen.append(chunk)
    return seen


class RemotesPanel(Panel):
    """Remote mirrors, on-demand connectivity, and file residency."""

    def __init__(self, project: Project) -> None:
        super().__init__(id="remotes")
        self.project = project
        self.remotes: list[dict[str, Any]] = []
        self.locations: list[dict[str, Any]] = []
        self.file_ids: list[str] = []
        self.checking = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="remotes-layout"):
            yield Static("Configured remotes", classes="modal-label")
            yield Static("", id="remotes-command", classes="modal-info")
            yield DataTable(id="remotes-table", cursor_type="row")
            with Horizontal(classes="config-buttons"):
                yield Button("Check connectivity", id="remotes-check")
            yield Static("", id="remotes-status", classes="modal-info")
            yield Static("File locations", classes="modal-label")
            yield Static("", id="locations-command", classes="modal-info")
            with Horizontal(id="locations-form"):
                yield Input(
                    placeholder="file ids (comma-separated; empty = all files)",
                    id="locations-filter",
                )
                yield Button("Filter", id="locations-apply", variant="primary")
            yield DataTable(id="locations-table", cursor_type="row")
            yield Static("", id="locations-note", classes="modal-info")
            yield Static("", id="remotes-error")

    def on_mount(self) -> None:
        self.query_one("#remotes-table", DataTable).add_columns(
            "name", "type", "address", "root", "files", "status", "error")
        self.query_one("#locations-table", DataTable).add_columns(
            "file_id", "relative_path", "local_status", "remote",
            "remote_status", "verified_at")
        self.query_one("#remotes-command", Static).update(
            "equivalent command: operon remotes — the screen lists the mirrors "
            "without connecting; connectivity is probed only when you ask "
            "(SFTP connects can block)")
        self._refresh_locations_command()
        super().on_mount()

    # -- data loading ---------------------------------------------------------

    def _fetch(self) -> dict[str, Any]:
        return {
            "remotes": data.list_remotes(self.project),
            "locations": data.list_locations(
                self.project, file_ids=self.file_ids or None,
            ),
        }

    def render_data(self, payload: dict[str, Any]) -> None:
        self.remotes = payload["remotes"]
        self.locations = payload["locations"]
        self._render_remotes()
        self._render_locations()

    def _render_remotes(self) -> None:
        table = self.query_one("#remotes-table", DataTable)
        table.clear()
        for row in self.remotes:
            status = str(row["status"])
            cell = Text(status, style="green") if status == "ok" else Text(
                status, style="" if status == "not checked" else "red")
            table.add_row(
                str(row["name"]), str(row["type"]), str(row["address"]),
                str(row["root"]), str(row["files"]), cell, str(row["error"]),
                key=str(row["name"]),
            )
        status_line = self.query_one("#remotes-status", Static)
        if not self.remotes:
            status_line.update(Text(
                "no remotes configured; add a 'remotes:' section to project.yaml",
                style="dim"))
        elif not self.checking:
            unchecked = sum(1 for row in self.remotes if row["status"] == "not checked")
            if unchecked and unchecked == len(self.remotes):
                status_line.update(Text(
                    "connectivity not checked yet — press *Check connectivity*",
                    style="dim"))

    def _render_locations(self) -> None:
        table = self.query_one("#locations-table", DataTable)
        table.clear()
        for row in self.locations:
            table.add_row(
                str(row["file_id"]), str(row["relative_path"]),
                str(row["local_status"]), str(row["remote"]),
                str(row["remote_status"]), str(row["verified_at"]),
            )
        note = self.query_one("#locations-note", Static)
        if len(self.locations) >= data.LOCATIONS_LIMIT:
            note.update(Text(
                f"showing the first {data.LOCATIONS_LIMIT} residency rows of a "
                "larger project — narrow the list by file id (the CLI prints "
                "every row)", style="yellow"))
        elif not self.locations:
            note.update(Text(
                "no manifest files match" if self.file_ids else "no manifest files yet",
                style="dim"))

    def show_error(self, exc: BaseException) -> None:
        self.query_one("#remotes-error", Static).update(Text(f"error: {exc}", style="red"))

    # -- connectivity ---------------------------------------------------------

    def _check(self) -> None:
        if self.checking:
            self.app.notify("a connectivity check is already running", severity="warning")
            return
        self.checking = True
        self.query_one("#remotes-check", Button).disabled = True
        self.query_one("#remotes-status", Static).update(
            "checking connectivity… (SFTP connects can block; the screen stays usable)")
        self.query_one("#remotes-error", Static).update("")
        self._probe()

    @work(thread=True, exclusive=True, group="remotes-check")
    def _probe(self) -> None:
        try:
            payload: Any = actions.check_remotes(self.project)
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel
            payload = exc
        self.post_to_ui(self._apply_check, payload)

    def _apply_check(self, payload: Any) -> None:
        self.checking = False
        self.query_one("#remotes-check", Button).disabled = False
        status_line = self.query_one("#remotes-status", Static)
        if isinstance(payload, BaseException):
            status_line.update("")
            self.show_error(payload)
            return
        self.remotes = payload
        self._render_remotes()
        if not payload:
            return
        failed = [row for row in payload if row["status"] != "ok"]
        summary = (f"checked {len(payload)} remote(s): "
                   f"{len(payload) - len(failed)} ok, {len(failed)} failed")
        if failed:
            status_line.update(Text(summary, style="red"))
            self.app.notify(
                summary + " — " + ", ".join(
                    f"{row['name']}: {row['error'] or row['status']}" for row in failed),
                severity="error",
            )
        else:
            status_line.update(Text(summary, style="green"))
            self.app.notify(summary)

    # -- residency filter -----------------------------------------------------

    def _refresh_locations_command(self) -> None:
        parts = ["operon", "locations"]
        for file_id in self.file_ids:
            parts += ["--file-id", shlex.quote(file_id)]
        self.query_one("#locations-command", Static).update(
            "equivalent command: " + " ".join(parts))

    def _apply_filter(self) -> None:
        self.file_ids = parse_file_ids(
            self.query_one("#locations-filter", Input).value)
        self.query_one("#remotes-error", Static).update("")
        self._refresh_locations_command()
        self.reload()

    # -- events ---------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "remotes-check":
            self._check()
        elif event.button.id == "locations-apply":
            self._apply_filter()
