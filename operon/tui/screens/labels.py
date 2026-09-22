"""Sequence-label browser for the Files screen.

``sequence_labels`` rows have no CLI reader of their own: ``classify-sequences``
writes them (per ``(file_id, seqid, profile_name)``) and ``report analysis
--hits`` shows the alignments behind them.  This read-only view mirrors the same
data — the project-wide label × count summary backed by
``idx_sequence_labels_label``, plus the selected file's label rows — so the
result of a classify run can be inspected without leaving the TUI.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, Static

from operon.config import Project
from operon.tui import data
from operon.tui.screens.common import DismissOnce, WorkerResults

FILE_LABEL_COLUMNS = ("seqid", "label", "profile_name", "decided_at")
SUMMARY_COLUMNS = ("label", "profile_name", "sequences", "files")


class SequenceLabelsModal(DismissOnce, WorkerResults, ModalScreen):
    """Read-only ``sequence_labels`` browser (per file + project summary)."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, project: Project, file_id: str | None = None) -> None:
        super().__init__()
        self.project = project
        self.file_id = file_id
        self.summary: list[dict[str, Any]] = []
        self.file_labels: list[dict[str, Any]] = []
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box", classes="wide"):
            yield Label("Sequence labels", id="modal-title")
            if self.file_id is not None:
                yield Static(f"file {self.file_id}", classes="modal-label")
                yield DataTable(id="labels-file-table", cursor_type="row")
            yield Static("project-wide label summary", classes="modal-label")
            yield DataTable(id="labels-table", cursor_type="row")
            yield Static("", id="labels-status")
            with Horizontal(id="modal-buttons"):
                yield Button("Close", id="cancel", variant="primary")

    def on_mount(self) -> None:
        summary_table = self.query_one("#labels-table", DataTable)
        summary_table.add_columns(*SUMMARY_COLUMNS)
        if self.file_id is not None:
            file_table = self.query_one("#labels-file-table", DataTable)
            file_table.add_columns(*FILE_LABEL_COLUMNS)
        self._load()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)

    @work(thread=True)
    def _load(self) -> None:
        try:
            payload: Any = {
                "summary": data.label_summary(self.project),
                "file": (data.file_sequence_labels(self.project, self.file_id)
                         if self.file_id is not None else []),
            }
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal  # pylint: disable=broad-exception-caught
            payload = exc
        self.post_to_ui(self._apply, payload)

    def _apply(self, payload: Any) -> None:
        status = self.query_one("#labels-status", Static)
        if isinstance(payload, BaseException):
            status.update(Text(f"error: {payload}", style="red"))
            return
        self.summary = payload["summary"]
        self.file_labels = payload["file"]
        summary_table = self.query_one("#labels-table", DataTable)
        summary_table.clear()
        for row in self.summary:
            summary_table.add_row(*[str(row[column]) for column in SUMMARY_COLUMNS])
        if self.file_id is not None:
            file_table = self.query_one("#labels-file-table", DataTable)
            file_table.clear()
            for row in self.file_labels:
                file_table.add_row(*[str(row[column]) for column in FILE_LABEL_COLUMNS])
            status.update(
                f"{len(self.file_labels)} label row(s) for this file, "
                f"{len(self.summary)} label/profile group(s) in the project"
            )
        else:
            status.update(f"{len(self.summary)} label/profile group(s) in the project")
