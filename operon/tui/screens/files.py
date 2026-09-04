"""Files browser panel: filterable manifest table plus file detail."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Select, Static

from operon.config import Project
from operon.tui import data
from operon.tui.screens.common import Panel, human_size, styled_file_status

ALL_STATUSES = "ALL"

KNOWN_FILE_STATUSES = [
    "CHECKSUM_VERIFIED", "STANDARDIZED", "REMOTE_ONLY",
    "REMOTE_UNVERIFIED", "MISSING", "CHECKSUM_FAILED",
]


class FilesPanel(Panel):
    """Manifest files with substring/status filters and residency details."""

    def __init__(self, project: Project) -> None:
        super().__init__(id="files")
        self.project = project
        self.files: list[dict[str, Any]] = []
        self.statuses: list[str] = []
        self.detail: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="files-layout"):
            with Horizontal(id="files-filters"):
                yield Input(placeholder="filter by id / entity / path", id="files-filter")
                yield Select([(ALL_STATUSES, ALL_STATUSES)], value=ALL_STATUSES,
                             id="files-status", allow_blank=False)
            yield DataTable(id="files-table", cursor_type="row")
            with VerticalScroll(id="file-detail-scroll"):
                yield Static("select a file", id="file-detail", classes="body")

    def on_mount(self) -> None:
        table = self.query_one("#files-table", DataTable)
        table.add_columns("file_id", "entity", "role", "format", "size", "sha256", "status")
        super().on_mount()

    def _filters(self) -> tuple[str | None, str, str]:
        status_value = self.query_one("#files-status", Select).value
        status = None if status_value in (ALL_STATUSES, Select.NULL) else str(status_value)
        text = self.query_one("#files-filter", Input).value.strip()
        return status, text, ""

    def _fetch(self) -> dict[str, Any]:
        status, text, entity = self._filters()
        return {
            "files": data.list_files(self.project, status=status, text=text, entity=entity),
            "statuses": data.file_statuses(self.project),
        }

    def render_data(self, payload: dict[str, Any]) -> None:
        self.files = payload["files"]
        new_statuses = sorted(set(KNOWN_FILE_STATUSES) | set(payload["statuses"]))
        if new_statuses != self.statuses:
            self.statuses = new_statuses
            select = self.query_one("#files-status", Select)
            current = select.value
            select.set_options([(ALL_STATUSES, ALL_STATUSES)] + [(s, s) for s in new_statuses])
            if current not in (ALL_STATUSES, Select.NULL) and current in new_statuses:
                select.value = current
        table = self.query_one("#files-table", DataTable)
        table.clear()
        for record in self.files:
            entity = f"{record['entity_type']}:{record['entity_id']}"
            table.add_row(
                record["file_id"],
                entity,
                record["file_role"],
                record["format"],
                human_size(record.get("size_bytes")),
                str(record.get("sha256") or "")[:12],
                styled_file_status(record.get("status")),
                key=record["file_id"],
            )

    def show_error(self, exc: BaseException) -> None:
        self.query_one("#file-detail", Static).update(Text(f"error: {exc}", style="red"))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "files-filter":
            self.reload()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "files-status":
            self.reload()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "files-table" and event.row_key is not None:
            self._load_detail(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "files-table" and event.row_key is not None:
            self._load_detail(str(event.row_key.value))

    @work(thread=True, exclusive=True, group="file-detail")
    def _load_detail(self, file_id: str) -> None:
        try:
            payload: Any = data.file_detail(self.project, file_id)
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._apply_detail, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _apply_detail(self, payload: Any) -> None:
        detail_view = self.query_one("#file-detail", Static)
        if isinstance(payload, BaseException):
            detail_view.update(Text(f"error: {payload}", style="red"))
            return
        self.detail = payload
        detail_view.update(self._detail_text(payload))

    def _detail_text(self, detail: dict[str, Any] | None) -> Text:
        if detail is None:
            return Text("file not found", style="red")
        record = detail["file"]
        text = Text()
        text.append(f"file {record['file_id']}\n", style="bold underline")
        for field in ("entity_type", "entity_id", "file_role", "format", "compression",
                      "relative_path", "source_url", "downloaded_at"):
            value = record.get(field)
            if value not in (None, ""):
                text.append(f"  {field:<16} {value}\n")
        text.append(f"  {'size_bytes':<16} {record.get('size_bytes')} "
                    f"({human_size(record.get('size_bytes'))})\n")
        text.append(f"  {'sha256':<16} {record.get('sha256')}\n")
        text.append("  status          ")
        text.append(styled_file_status(record.get("status")))
        text.append("\n")
        text.append("\nLocations\n", style="bold")
        if detail["locations"]:
            for location in detail["locations"]:
                text.append("  ")
                text.append(styled_file_status(location.get("status")))
                text.append(f"  {location['location_name']} ({location['location_type']})\n")
                text.append(f"      {location['uri']}\n")
                if location.get("verified_at"):
                    text.append(f"      verified {location['verified_at']}\n")
        else:
            text.append("  (no residency records)\n", style="dim")
        return text
