"""Alignment-hit browser for the Tasks screen.

Read-only mirror of ``operon report analysis --hits``: the same filters, the
same read-only query (:func:`operon.tui.data.analysis_hits`) and the same
column order.  The export row writes the current query through the CLI's own
renderer (:func:`operon.tui.actions.write_analysis_report`), so the file is
byte-identical to ``report analysis --hits --format … --out …``.  The job
summary view (``report analysis`` without ``--hits``) stays CLI-only.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select, Static

from operon.config import Project
from operon.tui import actions, data
from operon.tui.screens.common import (
    DismissOnce,
    WorkerResults,
    capture_table_view,
    restore_table_view,
)

LOOKUP_WIDGETS = (
    "hits-analysis", "hits-entity-type", "hits-entity-id",
    "hits-query-id", "hits-subject-id", "hits-evalue-max", "hits-limit",
)
RELOAD_WIDGETS = (*LOOKUP_WIDGETS, "hits-include-retired")


class AnalysisHitsModal(DismissOnce, WorkerResults, ModalScreen):
    """Filter + table browser over ``analysis_alignments``."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self.hits: list[dict[str, Any]] = []
        self._loading = False
        self._exporting = False

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box", classes="wide"):
            yield Label("Analysis hits", id="modal-title")
            with Horizontal(id="hits-filters"):
                yield Input(placeholder="analysis name", id="hits-analysis")
                yield Input(placeholder="entity type", id="hits-entity-type")
                yield Input(placeholder="entity id", id="hits-entity-id")
                yield Input(value="20", placeholder="limit", id="hits-limit",
                            type="integer", restrict=r"\d*")
            with Horizontal(id="hits-filters-2"):
                yield Input(placeholder="query id", id="hits-query-id")
                yield Input(placeholder="subject id", id="hits-subject-id")
                yield Input(placeholder="evalue max", id="hits-evalue-max",
                            restrict=r"[0-9eE.+-]*")
                yield Checkbox("include retired", id="hits-include-retired")
            yield DataTable(id="hits-table", cursor_type="row")
            with Horizontal(id="hits-export"):
                yield Select(
                    [("text", "text"), ("tsv", "tsv"), ("json", "json")],
                    value="text", id="hits-format",
                )
                yield Input(placeholder="export path (same rows as the current query)",
                            id="hits-out")
                yield Button("Export", id="hits-export-button")
            yield Static("", id="hits-status")
            with Horizontal(id="modal-buttons"):
                yield Button("Close", id="cancel", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#hits-table", DataTable)
        table.add_columns(*data.ANALYSIS_HIT_COLUMNS)
        self.reload()

    def reload(self) -> None:
        # Filter edits must never pile up overlapping loads.
        if self._loading:
            return
        self._loading = True
        self._load()

    def _filters(self) -> dict[str, Any]:
        def text(widget_id: str) -> str | None:
            return self.query_one(f"#{widget_id}", Input).value.strip() or None

        limit_text = self.query_one("#hits-limit", Input).value.strip()
        evalue_text = self.query_one("#hits-evalue-max", Input).value.strip()
        return {
            "analysis": text("hits-analysis"),
            "entity_type": text("hits-entity-type"),
            "entity_id": text("hits-entity-id"),
            "query_id": text("hits-query-id"),
            "subject_id": text("hits-subject-id"),
            "evalue_max": float(evalue_text) if evalue_text else None,
            "limit": int(limit_text) if limit_text.isdigit() and int(limit_text) > 0 else 20,
            "include_retired": self.query_one("#hits-include-retired", Checkbox).value,
        }

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in LOOKUP_WIDGETS:
            self.reload()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "hits-include-retired":
            self.reload()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "hits-export-button":
            self._start_export()

    @work(thread=True)
    def _load(self) -> None:
        try:
            payload: Any = data.analysis_hits(self.project, **self._filters())
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal
            payload = exc
        self.post_to_ui(self._apply, payload)

    def _apply(self, payload: Any) -> None:
        self._loading = False
        status = self.query_one("#hits-status", Static)
        if isinstance(payload, BaseException):
            status.update(Text(f"error: {payload}", style="red"))
            return
        self.hits = payload
        table = self.query_one("#hits-table", DataTable)
        view = capture_table_view(table)
        table.clear()
        for row in self.hits:
            table.add_row(*[
                "" if row[column] is None else str(row[column])
                for column in data.ANALYSIS_HIT_COLUMNS
            ])
        restore_table_view(table, view, len(self.hits))
        status.update(f"{len(self.hits)} hit row(s)")

    def _set_status(self, message: str, *, error: bool = False) -> None:
        status = self.query_one("#hits-status", Static)
        status.update(Text(message, style="red") if error else message)

    def _start_export(self) -> None:
        """Render the current query through the CLI's exporter (no DB writes)."""
        if self._exporting:
            return
        fmt_value = self.query_one("#hits-format", Select).value
        out_path = self.query_one("#hits-out", Input).value.strip()
        fmt = "text" if fmt_value is Select.NULL else str(fmt_value)
        filters = self._filters()
        self._exporting = True
        self._export(out_path, fmt, filters)

    @work(thread=True)
    def _export(self, out_path: str, fmt: str, filters: dict[str, Any]) -> None:
        try:
            payload: Any = actions.write_analysis_report(
                self.project, out=out_path, fmt=fmt, **filters)
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal
            payload = exc
        self.post_to_ui(self._apply_export, payload)

    def _apply_export(self, payload: Any) -> None:
        self._exporting = False
        if isinstance(payload, BaseException):
            self._set_status(f"export failed: {payload}", error=True)
            return
        self._set_status(f"wrote {payload['rows']} row(s) to {payload['path']}")
        self.app.notify(f"analysis hits exported: {payload['path']}")
