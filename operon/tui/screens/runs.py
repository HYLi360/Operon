"""Workflow-run monitor panel and run detail screen."""

from __future__ import annotations

import json
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Input, Select, Static

from operon.config import Project
from operon.tui import data
from operon.tui.screens.common import (
    Panel,
    entity_label,
    format_duration,
    styled_status,
)

ALL_STATUSES = "ALL"
RUN_STATUSES = ["running", "completed", "failed", "interrupted", "adopted", "planned"]


class RunsPanel(Panel):
    """Filterable, auto-refreshing workflow run listing."""

    def __init__(self, project: Project) -> None:
        super().__init__(id="runs")
        self.project = project
        self.runs: list[dict[str, Any]] = []
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical(id="runs-layout"):
            with Horizontal(id="runs-filters"):
                yield Select(
                    [(ALL_STATUSES, ALL_STATUSES)] + [(s, s) for s in RUN_STATUSES],
                    value=ALL_STATUSES, id="runs-status", allow_blank=False,
                )
                yield Input(placeholder="step contains", id="runs-step")
                yield Input(placeholder="entity contains", id="runs-entity")
                yield Input(value="100", placeholder="limit", id="runs-limit",
                            type="integer", restrict=r"\d*")
            yield DataTable(id="runs-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#runs-table", DataTable)
        table.add_columns("started", "status", "step", "entity", "duration", "run_id")
        super().on_mount()
        self.set_interval(2.0, self._auto_reload)

    def _auto_reload(self) -> None:
        # Polling while hidden would spawn workers indefinitely, which can
        # starve app.workers.wait_for_complete() and wastes queries.
        if self.display:
            self.reload()

    def reload(self) -> None:
        # The auto-refresh interval must never pile up overlapping loads.
        if self._loading:
            return
        self._loading = True
        super().reload()

    def _apply(self, payload: Any) -> None:
        self._loading = False
        super()._apply(payload)

    def _filters(self) -> tuple[list[str], str, str, int]:
        status_value = self.query_one("#runs-status", Select).value
        statuses = [] if status_value in (ALL_STATUSES, Select.NULL) else [str(status_value)]
        step = self.query_one("#runs-step", Input).value.strip()
        entity = self.query_one("#runs-entity", Input).value.strip()
        limit_text = self.query_one("#runs-limit", Input).value.strip()
        limit = int(limit_text) if limit_text.isdigit() else 100
        return statuses, step, entity, limit

    def _fetch(self) -> list[dict[str, Any]]:
        statuses, step, entity, limit = self._filters()
        return data.list_workflow_runs(
            self.project, statuses=statuses, step=step, entity=entity, limit=limit,
        )

    def render_data(self, payload: list[dict[str, Any]]) -> None:
        self.runs = payload
        table = self.query_one("#runs-table", DataTable)
        cursor_row = table.cursor_row
        table.clear()
        for record in payload:
            table.add_row(
                str(record.get("started_at") or "-"),
                styled_status(record.get("status")),
                str(record.get("step") or "-"),
                entity_label(record),
                format_duration(record),
                record["run_id"],
                key=record["run_id"],
            )
        if payload:
            table.move_cursor(row=min(max(cursor_row, 0), len(payload) - 1), animate=False)

    def show_error(self, exc: BaseException) -> None:
        self.app.notify(f"runs load failed: {exc}", severity="error")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"runs-step", "runs-entity", "runs-limit"}:
            self.reload()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "runs-status":
            self.reload()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "runs-table" and event.row_key is not None:
            self.app.push_screen(RunDetailScreen(self.project, str(event.row_key.value)))


class RunDetailScreen(Screen):
    """Full record of one workflow run, mirroring `operon workflow show`."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
    ]

    def __init__(self, project: Project, run_id: str) -> None:
        super().__init__()
        self.project = project
        self.run_id = run_id
        self.record: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="run-detail-scroll"):
            yield Static("loading…", id="run-detail", classes="body")

    def on_mount(self) -> None:
        self._load()

    def action_back(self) -> None:
        self.app.pop_screen()

    @work(thread=True)
    def _load(self) -> None:
        try:
            payload: Any = data.workflow_run_detail(self.project, self.run_id)
        except Exception as exc:  # noqa: BLE001 - surfaced in the screen
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._apply, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _apply(self, payload: Any) -> None:
        view = self.query_one("#run-detail", Static)
        if isinstance(payload, BaseException):
            view.update(Text(f"error: {payload}", style="red"))
            return
        self.record = payload
        view.update(self._detail_text(payload))

    def _detail_text(self, record: dict[str, Any] | None) -> Text:
        if record is None:
            return Text(f"workflow run does not exist: {self.run_id}", style="red")
        text = Text()

        def section(title: str, fields: list[tuple[str, Any]]) -> None:
            text.append(f"{title}\n", style="bold underline")
            for label, value in fields:
                rendered = "-" if value in (None, "") else str(value)
                text.append(f"  {label:<18} {rendered}\n")
            text.append("\n")

        section("Workflow run", [
            ("run_id", record["run_id"]),
            ("status", record["status"]),
            ("step", record["step"]),
            ("entity", entity_label(record)),
            ("parent_run_id", record.get("parent_run_id")),
            ("resumes_run_id", record.get("resumes_run_id")),
        ])
        section("Timing and resources", [
            ("started_at", record.get("started_at")),
            ("finished_at", record.get("finished_at")),
            ("duration", format_duration(record)),
            ("threads", record.get("threads")),
            ("max_rss_mb", record.get("max_rss_mb")),
            ("avg_rss_mb", record.get("avg_rss_mb")),
            ("cpu_seconds", record.get("cpu_seconds")),
        ])
        section("Execution", [
            ("command", record.get("command")),
            ("tool", record.get("tool")),
            ("tool_version", record.get("tool_version")),
            ("parameter_set", record.get("parameter_set")),
            ("executor", record.get("executor")),
            ("scheduler_job_id", record.get("scheduler_job_id")),
            ("exit_code", record.get("exit_code")),
            ("environment_id", record.get("environment_id")),
        ])
        section("Artifacts and logs", [
            ("input_sha256", record.get("input_sha256")),
            ("output_sha256", record.get("output_sha256")),
            ("log_file", record.get("log_file")),
            ("stdout_file", record.get("stdout_file")),
            ("stderr_file", record.get("stderr_file")),
        ])
        section("Outcome", [("error", record.get("error"))])
        text.append("Execution details\n", style="bold underline")
        details = record.get("execution_details")
        if isinstance(details, (dict, list)):
            text.append(json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        else:
            text.append("-" if details in (None, "") else str(details))
            text.append("\n")
        return text
