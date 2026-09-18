"""Workflow-run monitor panel and run detail screen."""

from __future__ import annotations

import json
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    RichLog,
    Select,
    Static,
)

from operon.config import Project
from operon.tui import data
from operon.tui.screens.common import (
    DismissOnce,
    Panel,
    capture_table_view,
    entity_label,
    format_duration,
    restore_table_view,
    styled_status,
)

ALL_STATUSES = "ALL"
RUN_STATUSES = ["running", "completed", "failed", "interrupted", "adopted", "planned"]
ANALYSIS_JOB_CHOICES = [ALL_STATUSES, *data.ANALYSIS_JOB_STATUSES]


class RunsPanel(Panel):
    """Filterable workflow run listing; refreshes on demand (``r``)."""

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
            with Horizontal(id="runs-filters-advanced"):
                yield Input(placeholder="from (ISO-8601)", id="runs-from")
                yield Input(placeholder="to (ISO-8601)", id="runs-to")
                yield Input(placeholder="run id", id="runs-run-id")
                yield Input(placeholder="parent run id", id="runs-parent-run-id")
                yield Input(placeholder="tool", id="runs-tool")
                yield Input(placeholder="executor", id="runs-executor")
                yield Input(value="0", placeholder="offset", id="runs-offset",
                            type="integer", restrict=r"\d*")
                yield Checkbox("oldest first", id="runs-oldest-first")
            with Horizontal(classes="config-buttons"):
                yield Button("Analysis jobs", id="runs-jobs")
                yield Button("Run external", id="runs-external")
                yield Button("Environments", id="runs-environments")
                yield Button("New analysis", id="runs-new-analysis")
            yield DataTable(id="runs-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#runs-table", DataTable)
        table.add_columns("started", "status", "step", "entity", "duration", "run_id")
        super().on_mount()

    def reload(self) -> None:
        # Filter edits and manual refreshes must never pile up overlapping loads.
        if self._loading:
            return
        self._loading = True
        super().reload()

    def _apply(self, payload: Any) -> None:
        self._loading = False
        super()._apply(payload)

    def _filters(self) -> dict[str, Any]:
        status_value = self.query_one("#runs-status", Select).value
        statuses = [] if status_value in (ALL_STATUSES, Select.NULL) else [str(status_value)]
        limit_text = self.query_one("#runs-limit", Input).value.strip()
        offset_text = self.query_one("#runs-offset", Input).value.strip()

        def text(widget_id: str) -> str | None:
            return self.query_one(f"#{widget_id}", Input).value.strip() or None

        return {
            "statuses": statuses,
            "step": self.query_one("#runs-step", Input).value.strip(),
            "entity": self.query_one("#runs-entity", Input).value.strip(),
            "limit": int(limit_text) if limit_text.isdigit() else 100,
            "offset": int(offset_text) if offset_text.isdigit() else 0,
            "started_from": text("runs-from"),
            "started_to": text("runs-to"),
            "run_id": text("runs-run-id"),
            "parent_run_id": text("runs-parent-run-id"),
            "tool": text("runs-tool"),
            "executor": text("runs-executor"),
            "oldest_first": self.query_one("#runs-oldest-first", Checkbox).value,
        }

    def _fetch(self) -> list[dict[str, Any]]:
        # ``list_workflow_runs`` validates the ISO bounds; a bad value lands in
        # show_error() as an inline notification and leaves the table as is.
        return data.list_workflow_runs(self.project, **self._filters())

    def render_data(self, payload: list[dict[str, Any]]) -> None:
        self.runs = payload
        table = self.query_one("#runs-table", DataTable)
        view = capture_table_view(table)
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
        restore_table_view(table, view, len(payload))

    def show_error(self, exc: BaseException) -> None:
        self.app.notify(f"runs load failed: {exc}", severity="error")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {
            "runs-step", "runs-entity", "runs-limit", "runs-from", "runs-to",
            "runs-run-id", "runs-parent-run-id", "runs-tool", "runs-executor",
            "runs-offset",
        }:
            self.reload()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "runs-status":
            self.reload()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "runs-oldest-first":
            self.reload()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "runs-table" and event.row_key is not None:
            self.app.push_screen(RunDetailScreen(self.project, str(event.row_key.value)))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "runs-new-analysis":
            from operon.tui.screens.analyze import AnalyzeModal, analysis_finished

            self.app.push_screen(
                AnalyzeModal(self.project),
                lambda payload: analysis_finished(self.app, payload),
            )
        elif event.button.id == "runs-jobs":
            self.app.push_screen(AnalysisJobsModal(self.project))
        elif event.button.id == "runs-external":
            from operon.tui.screens.run_external import RunExternalModal

            self.app.push_screen(RunExternalModal(self.project), self._external_finished)
        elif event.button.id == "runs-environments":
            from operon.tui.screens.environments import EnvironmentsModal

            self.app.push_screen(EnvironmentsModal(self.project))

    def _external_finished(self, payload: Any) -> None:
        """Dismiss callback: reload the panel and open the finished run's record."""
        if not payload:
            return
        self.app.reload_after_write()
        self.app.push_screen(RunDetailScreen(self.project, payload["run_id"]))


class AnalysisJobsModal(DismissOnce, ModalScreen):
    """Read-only ``analysis_jobs`` browser launched from the Tasks screen.

    The Tasks list shows ``workflow_runs``, which only exist for tasks whose
    bookkeeping finished; a task interrupted inside a job array keeps an
    ``analysis_jobs`` row and no run row.  This view reads the jobs table
    directly, joins the scheduler job id from the run when there is one, and
    shows the selected row's full error and artifact paths underneath.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self.jobs: list[dict[str, Any]] = []
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("Analysis jobs", id="modal-title")
            with Horizontal(id="jobs-filters"):
                yield Input(placeholder="analysis contains", id="jobs-analysis")
                yield Select(
                    [(name, name) for name in ANALYSIS_JOB_CHOICES],
                    value=ALL_STATUSES, id="jobs-status",
                )
                yield Input(value="200", placeholder="limit", id="jobs-limit",
                            type="integer", restrict=r"\d*")
            yield DataTable(id="jobs-table", cursor_type="row")
            yield Static("", id="jobs-detail")
            with Horizontal(id="modal-buttons"):
                yield Button("Close", id="cancel", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#jobs-table", DataTable)
        table.add_columns("job_id", "status", "analysis", "entity", "file_id",
                          "scheduler_job_id", "finished_at")
        self.reload()

    def reload(self) -> None:
        # Filter edits and manual refreshes must never pile up overlapping loads.
        if self._loading:
            return
        self._loading = True
        self._load()

    def _filters(self) -> tuple[str, list[str], int]:
        analysis = self.query_one("#jobs-analysis", Input).value.strip()
        status_value = self.query_one("#jobs-status", Select).value
        statuses = ([] if status_value in (ALL_STATUSES, Select.NULL)
                    else [str(status_value)])
        limit_text = self.query_one("#jobs-limit", Input).value.strip()
        limit = int(limit_text) if limit_text.isdigit() and int(limit_text) > 0 else 200
        return analysis, statuses, limit

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"jobs-analysis", "jobs-limit"}:
            self.reload()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "jobs-status":
            self.reload()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)

    @work(thread=True)
    def _load(self) -> None:
        analysis, statuses, limit = self._filters()
        try:
            payload: Any = data.list_analysis_jobs(
                self.project, analysis=analysis, statuses=statuses, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._apply, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _apply(self, payload: Any) -> None:
        self._loading = False
        detail = self.query_one("#jobs-detail", Static)
        if isinstance(payload, BaseException):
            detail.update(Text(f"error: {payload}", style="red"))
            return
        self.jobs = payload
        table = self.query_one("#jobs-table", DataTable)
        view = capture_table_view(table)
        table.clear()
        for job in payload:
            table.add_row(
                str(job["job_id"]),
                styled_status(job.get("status")),
                str(job.get("analysis_name") or "-"),
                entity_label(job),
                str(job.get("file_id") or "-"),
                str(job.get("scheduler_job_id") or "-"),
                str(job.get("finished_at") or "-"),
                key=str(job["job_id"]),
            )
        restore_table_view(table, view, len(payload))
        detail.update(self._detail_text(payload[0] if payload else None))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "jobs-table":
            return
        index = event.cursor_row
        if 0 <= index < len(self.jobs):
            self.query_one("#jobs-detail", Static).update(self._detail_text(self.jobs[index]))

    def _detail_text(self, job: dict[str, Any] | None) -> Text:
        text = Text()
        if job is None:
            text.append("no matching analysis jobs", style="dim")
            return text
        text.append(
            f"job {job['job_id']} · {job.get('analysis_name')} · {job.get('status')}\n",
            style="bold",
        )
        for label, value in (
            ("entity", f"{job.get('entity_type')}:{job.get('entity_id')}"),
            ("file_id", job.get("file_id")),
            ("tool", f"{job.get('tool')} {job.get('tool_version')}"),
            ("scheduler_job_id", job.get("scheduler_job_id")),
            ("executor", job.get("executor")),
            ("workflow_run_id", job.get("workflow_run_id")),
            ("started_at", job.get("started_at")),
            ("finished_at", job.get("finished_at")),
            ("output", job.get("output_relative_path")),
            ("stdout", job.get("stdout_file")),
            ("stderr", job.get("stderr_file")),
        ):
            rendered = "-" if value in (None, "") else str(value)
            text.append(f"  {label:<18} {rendered}\n")
        error = str(job.get("error") or "").strip()
        text.append(f"  {'error':<18} ")
        if error:
            text.append(f"{error}\n", style="red")
        else:
            text.append("-\n")
        return text


class RunDetailScreen(Screen):
    """Full record of one workflow run, mirroring `operon workflow show`.

    While the run is ``running``, the *Follow logs* switch streams the local
    ``logs/<run_id>.stdout.log``/``.stderr.log`` tails into the screen with a
    one-second timer — the same files and incremental reads the CLI's
    ``workflow show --follow`` uses.  Following only observes: it never
    cancels or alters the run, it stops by itself once the run leaves
    ``running``, and the timer is stopped on unmount.  With the SSH backend
    the logs are pulled back only when the run ends, so nothing appears until
    then.
    """

    BINDINGS = [
        Binding("escape", "back", "Back"),
    ]

    FOLLOW_INTERVAL = 1.0

    def __init__(self, project: Project, run_id: str) -> None:
        super().__init__()
        self.project = project
        self.run_id = run_id
        self.record: dict[str, Any] | None = None
        self._follow_timer: Any = None
        self._following = False
        self._stdout_offset = 0
        self._stderr_offset = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="run-detail-layout"):
            with VerticalScroll(id="run-detail-scroll"):
                yield Static("loading…", id="run-detail", classes="body")
            yield Checkbox("Follow logs (while the run is running)", id="run-follow",
                           disabled=True)
            yield Static("", id="run-follow-note", classes="modal-info")
            yield RichLog(id="run-follow-log", max_lines=500, wrap=True, markup=False)

    def on_mount(self) -> None:
        self._load()

    def on_unmount(self) -> None:
        # The follow timer must never outlive the screen.
        self._stop_following()

    def action_back(self) -> None:
        # A queued second escape must not pop the screen underneath.
        if self.app.screen is self:
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
        running = bool(payload and payload.get("status") == "running")
        self.query_one("#run-follow", Checkbox).disabled = not running
        note = self.query_one("#run-follow-note", Static)
        if running:
            note.update(
                f"streams {self.project.logs_root / (self.run_id + '.stdout.log')} and "
                f".stderr.log as they grow; with the SSH backend logs are pulled back "
                "only when the run ends"
            )
        else:
            note.update("log following is available while the run is running")
        if not running and self._following:
            self._finish_following()

    # -- log following --------------------------------------------------------

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id != "run-follow":
            return
        if event.value:
            self._start_following()
        else:
            self._stop_following()

    def _start_following(self) -> None:
        self._stdout_offset = 0
        self._stderr_offset = 0
        self._following = True
        self.query_one("#run-follow-log", RichLog).clear()
        self._follow_tick()
        if self._following and self._follow_timer is None:
            self._follow_timer = self.set_interval(self.FOLLOW_INTERVAL, self._follow_tick)

    def _stop_following(self) -> None:
        self._following = False
        if self._follow_timer is not None:
            self._follow_timer.stop()
            self._follow_timer = None

    def _finish_following(self) -> None:
        """Stop at a final state, reset the switch and refresh the record."""
        self._stop_following()
        try:
            follow = self.query_one("#run-follow", Checkbox)
        except NoMatches:  # pragma: no cover - screen teardown race
            return
        follow.disabled = True
        follow.value = False
        self._load()

    def _follow_tick(self) -> None:
        """Append new log bytes and stop once the run no longer runs."""
        from operon.workflow import read_log_tail

        log = self.query_one("#run-follow-log", RichLog)
        stdout_path = self.project.logs_root / f"{self.run_id}.stdout.log"
        stderr_path = self.project.logs_root / f"{self.run_id}.stderr.log"
        text, self._stdout_offset = read_log_tail(stdout_path, self._stdout_offset)
        if text:
            log.write(text.rstrip("\n"))
        err_text, self._stderr_offset = read_log_tail(stderr_path, self._stderr_offset)
        for line in err_text.splitlines():
            log.write(f"stderr: {line}")
        status = data.workflow_run_status(self.project, self.run_id)
        if status is None:
            log.write(f"workflow run disappeared while following: {self.run_id}")
            self._finish_following()
            return
        if status["status"] != "running":
            exit_code = status.get("exit_code")
            log.write(
                f"run {self.run_id} finished: status={status['status']} "
                f"exit_code={exit_code if exit_code is not None else '-'}"
            )
            self.app.notify(f"run {self.run_id} finished: {status['status']}")
            self._finish_following()

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
            ("environment", record.get("environment_summary")),
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
