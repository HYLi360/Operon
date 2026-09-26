"""Remotes screen: configured SFTP mirrors and file residency.

Lists the remotes from ``project.yaml`` (``operon remotes`` rows) without any
network access on load, tests connectivity on demand through the same core
``check_remote`` the CLI uses, and shows the project-wide local/remote
residency listing behind ``operon locations`` (shared core query, same columns
and ordering).
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Input, ProgressBar, Select, Static

from operon.config import Project
from operon.tui import actions, data
from operon.tui.screens.common import Panel, WriteModal


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
                yield Button("Push…", id="remotes-push")
                yield Button("Pull…", id="remotes-pull")
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
        elif event.button.id == "remotes-push":
            self._open_sync(PushModal)
        elif event.button.id == "remotes-pull":
            self._open_sync(PullModal)

    # -- push / pull (write entries) ------------------------------------------

    def _selected_remote(self) -> str:
        """The mirror under the cursor, so a dialog opens pre-filled."""
        table = self.query_one("#remotes-table", DataTable)
        if table.row_count == 0:
            return ""
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:  # noqa: BLE001 - no cursor/row yet: fall back to the first
            return str(self.remotes[0]["name"]) if self.remotes else ""
        return "" if key is None else str(key.value)

    def _open_sync(self, modal_type: Any) -> None:
        if not self.remotes:
            self.app.notify(
                "no remotes configured; add a 'remotes:' section to project.yaml",
                severity="warning",
            )
            return
        self.app.push_screen(
            modal_type(self.project, remote=self._selected_remote()),
            self._after_sync,
        )

    def _after_sync(self, result: Any) -> None:
        if result:
            # file_locations / file status and the sync log all changed.
            self.app.reload_after_write()


class SyncModal(WriteModal):
    """Confirm + results for ``operon push`` / ``operon pull``.

    Both commands transfer file by file and report per-file outcomes (one
    failed file does not stop the batch), so the dialog shows the selection
    before Confirm, an activity indicator while the transfer runs, and the
    CLI's result table afterwards — a run that ends with errors stays open
    with the failures listed.  The core has no cooperative cancel (per-file
    transfers are atomic and the remote manifest is published last), so a
    running transfer refuses to close instead of pretending.
    """

    #: ``push`` or ``pull``; set by the subclasses.
    verb = "push"

    def __init__(self, project: Project, remote: str = "",
                 file_ids: Iterable[str] = ()) -> None:
        super().__init__(f"{self.verb.title()} {'to' if self.verb == 'push' else 'from'} a remote")
        self.project = project
        self.initial_remote = remote
        self.initial_file_ids = list(file_ids)
        self.remotes = [row["name"] for row in data.list_remotes(project)]
        self.results: list[dict[str, Any]] | None = None
        self.running = False

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            "upload the selected manifest files to a configured SFTP mirror "
            "(checksum-verified, idempotent: a remote copy that already matches "
            "is reported ``skipped``, a diverging copy is never overwritten). "
            "The remote manifest is published last, so an interrupted transfer "
            "claims nothing."
            if self.verb == "push" else
            "restore the selected files from a configured SFTP mirror "
            "(checksum-verified; a local copy that already matches is reported "
            "``skipped``, a local file with different bytes is never "
            "overwritten). With no file ids every entry in the remote manifest "
            "is restored.",
            classes="modal-info",
        )
        with Horizontal(id="sync-form"):
            yield Select(
                [(name, name) for name in self.remotes],
                prompt="select a remote" if self.remotes else "no remotes configured",
                value=self.initial_remote or Select.NULL,
                id="sync-remote",
                allow_blank=True,
            )
            yield Input(
                value=", ".join(self.initial_file_ids),
                placeholder="file ids (comma-separated; empty = "
                            + ("all manifest files" if self.verb == "push"
                               else "the whole remote manifest") + ")",
                id="sync-file-ids",
            )
        yield Static("", id="sync-plan", classes="modal-info")
        yield ProgressBar(total=None, id="sync-progress")
        yield DataTable(id="sync-results")
        yield Static("", id="sync-status", classes="modal-info")

    def on_mount(self) -> None:
        self.query_one("#sync-progress", ProgressBar).display = False
        self.query_one("#sync-results", DataTable).display = False
        super().on_mount()
        self._refresh_plan()

    # -- form state -----------------------------------------------------------

    def _values(self) -> dict[str, Any]:
        remote = self.query_one("#sync-remote", Select).value
        return {
            "remote": "" if remote is Select.NULL else str(remote),
            "file_ids": parse_file_ids(self.query_one("#sync-file-ids", Input).value),
        }

    def command_text(self) -> str:
        values = self._values()
        parts = ["operon", self.verb]
        parts += ["--remote", shlex.quote(values["remote"]) if values["remote"] else "'…'"]
        for file_id in values["file_ids"]:
            parts += ["--file-id", shlex.quote(file_id)]
        return " ".join(parts)

    def _refresh_plan(self) -> None:
        """Show the local side of the selection the transfer will start from."""
        values = self._values()
        plan = self.query_one("#sync-plan", Static)
        if not values["remote"]:
            plan.update(Text("select a remote first", style="dim"))
            return
        if self.verb == "pull" and not values["file_ids"]:
            plan.update(Text(
                "plan: every entry in the remote manifest (the list is read "
                "from the mirror when the transfer starts); nothing is "
                "downloaded twice and existing local bytes are never "
                "overwritten", style="dim"))
            return
        try:
            preview = data.sync_preview(self.project, file_ids=values["file_ids"] or None)
        except Exception as exc:  # noqa: BLE001 - rendered as a hint, the confirm repeats it
            plan.update(Text(f"{exc}", style="red"))
            return
        if not preview["count"]:
            plan.update(Text("plan: no manifest files selected" if values["file_ids"]
                             else "plan: no manifest files yet", style="dim"))
            return
        size = preview["bytes"]
        human = f"{size / 1048576:.1f} MiB" if size >= 1048576 else f"{size} B"
        detail = ("every manifest file" if not values["file_ids"] else
                  f"{preview['count']} selected file(s)")
        plan.update(
            f"plan: {detail}, {human} on disk; files whose remote copy already "
            "matches are reported ``skipped``"
            + ("" if self.verb == "push" else
               " — only ids present in the remote manifest are restored"))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "sync-file-ids":
            self.refresh_command()
            self._refresh_plan()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sync-remote":
            self.refresh_command()
            self._refresh_plan()

    # -- run ------------------------------------------------------------------

    def confirm(self) -> None:
        if self.running:
            return
        values = self._values()
        if not values["remote"]:
            self.show_error("select a remote first (--remote is required)")
            return
        if values["remote"] not in self.remotes:
            self.show_error(f"unknown remote {values['remote']!r}; configure it in project.yaml first")
            return
        self.running = True
        self.clear_error()
        self._set_controls_disabled(True)
        self.query_one("#sync-status", Static).update(
            f"{self.verb}ing… (a running {self.verb} cannot be interrupted from the TUI)"
        )
        self.query_one("#sync-progress", ProgressBar).display = True
        self.run_action(lambda: (actions.push if self.verb == "push" else actions.pull)(
            self.project, values["remote"], values["file_ids"] or None,
        ))

    def _set_controls_disabled(self, disabled: bool) -> None:
        for widget in self.query("Select, Input"):
            widget.disabled = disabled

    def _action_done(self, payload: Any) -> None:
        self.running = False
        self._set_controls_disabled(False)
        self.query_one("#sync-status", Static).update("")
        self.query_one("#sync-progress", ProgressBar).display = False
        super()._action_done(payload)

    def on_action_success(self, payload: list[dict[str, Any]]) -> None:
        """Render the CLI's result table; the dialog stays open."""
        self.results = payload
        table = self.query_one("#sync-results", DataTable)
        table.clear(columns=True)
        table.display = True
        table.add_columns("file_id", "relative_path", "status", "error")
        for row in payload:
            table.add_row(
                str(row.get("file_id", "")), str(row["relative_path"]),
                str(row["status"]), str(row.get("error") or ""),
            )
        counts: dict[str, int] = {}
        for row in payload:
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
        summary = ", ".join(f"{status}: {count}" for status, count in sorted(counts.items()))
        remote = self._values()["remote"]
        failures = [row for row in payload if row["status"] == "error"]
        self.query_one("#sync-status", Static).update(
            Text(f"{self.verb} {remote}: {summary}",
                 style="red" if failures else "green"))
        self.app.notify(
            f"{self.verb} {remote}: {summary}" + (
                " — " + "; ".join(f"{row['relative_path']}: {row['error']}" for row in failures[:3])
                if failures else ""),
            severity="error" if failures else "information",
        )

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
                f"a running {self.verb} cannot be interrupted from the TUI",
                severity="warning",
            )
            return
        self.dismiss(self.results)


class PushModal(SyncModal):
    """``operon push``."""

    verb = "push"


class PullModal(SyncModal):
    """``operon pull``."""

    verb = "pull"
