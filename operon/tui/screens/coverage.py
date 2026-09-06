"""Coverage screen: taxonomy snapshots, reference sets, and coverage reports.

Lists the frozen NCBI Taxonomy snapshots and compiled reference sets, runs
``operon report coverage`` through a confirm dialog (a report below its
thresholds is a warning result, not a crash), and browses existing
``reports/coverage/COV_*`` directories — provenance headline plus the
summary/targets/missing/observations/excluded TSVs as tables.
"""

from __future__ import annotations

import shlex
from typing import Any, Iterable

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from operon.config import Project
from operon.tui import actions, data
from operon.tui.screens.common import Panel, WriteModal, styled_decision

SCOPE_OPTIONS = [("project metadata", "metadata"), ("frozen release", "release")]

REPORT_TABS = (
    ("coverage_summary", "Summary"),
    ("coverage_targets", "Targets"),
    ("coverage_missing", "Missing"),
    ("coverage_observations", "Observations"),
    ("coverage_excluded_observations", "Excluded"),
)


class CoverageModal(WriteModal):
    """Confirm + generate for ``operon report coverage``."""

    def __init__(self, project: Project, reference_set_id: str,
                 release_version: str | None) -> None:
        super().__init__(f"Coverage report: {reference_set_id}")
        self.project = project
        self.reference_set_id = reference_set_id
        self.release_version = release_version

    def compose_form(self) -> Iterable[Any]:
        if self.release_version:
            scope = f"frozen release {self.release_version}"
        else:
            scope = "current project metadata"
        yield Static(
            f"Measure family/genus coverage of {scope} against reference set "
            f"{self.reference_set_id}?\n"
            "Identical inputs reuse the cached immutable report; a result below "
            "the profile thresholds is reported as FAIL (the CLI exits 1).",
            classes="modal-info",
        )

    def command_text(self) -> str:
        parts = ["operon", "report", "coverage", "--reference-set",
                 shlex.quote(self.reference_set_id)]
        if self.release_version:
            parts += ["--release", shlex.quote(self.release_version)]
        return " ".join(parts)

    def confirm(self) -> None:
        self.run_action(lambda: actions.run_coverage(
            self.project, self.reference_set_id, release_version=self.release_version,
        ))

    def on_action_success(self, payload: Any) -> None:
        metrics = "; ".join(
            f"{m['rank']} {float(m['coverage_percent']):.2f}% "
            f"(min {float(m['threshold_percent']):.2f}%)"
            for m in payload.get("metrics", [])
        )
        reused = " (reused cached report)" if payload.get("reused") else ""
        if payload.get("exit_code"):
            self.app.notify(
                f"coverage {payload['decision']}: {metrics}{reused} — report {payload['path']}",
                severity="warning",
            )
        else:
            self.app.notify(
                f"coverage {payload['decision']}: {metrics}{reused}"
            )
        self.dismiss(payload)


class CoveragePanel(Panel):
    """Taxonomy snapshots, reference sets, coverage report runner + browser."""

    def __init__(self, project: Project) -> None:
        super().__init__(id="coverage")
        self.project = project
        self.snapshots: list[dict[str, Any]] = []
        self.reference_sets: list[dict[str, Any]] = []
        self.releases: list[dict[str, Any]] = []
        self.reports: list[dict[str, Any]] = []
        self.report: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="coverage-layout"):
            yield Static("Taxonomy snapshots", classes="modal-label")
            yield DataTable(id="taxonomy-snapshots-table", cursor_type="row")
            yield Static("Reference sets", classes="modal-label")
            yield DataTable(id="reference-sets-table", cursor_type="row")
            yield Static("Generate report", classes="modal-label")
            with Horizontal(id="coverage-form"):
                yield Select([], id="coverage-reference-set", allow_blank=True)
                yield Select(SCOPE_OPTIONS, value="metadata", id="coverage-scope",
                             allow_blank=False)
                yield Select([], id="coverage-release", allow_blank=True)
                yield Button("Generate", id="coverage-generate", variant="primary")
            yield Static("", id="coverage-error")
            yield Static("Reports", classes="modal-label")
            yield DataTable(id="coverage-reports-table", cursor_type="row")
            yield Static("select a report", id="coverage-report-headline", classes="modal-info")
            with TabbedContent(id="coverage-report-tabs"):
                for table_name, title in REPORT_TABS:
                    with TabPane(title, id=f"tab-{table_name}"):
                        yield DataTable(id=f"coverage-table-{table_name}")

    def on_mount(self) -> None:
        self.query_one("#taxonomy-snapshots-table", DataTable).add_columns(
            "snapshot", "version", "nodes", "status", "imported_at")
        self.query_one("#reference-sets-table", DataTable).add_columns(
            "reference_set_id", "profile", "taxonomy", "family", "genus", "compiled_at")
        self.query_one("#coverage-reports-table", DataTable).add_columns(
            "report", "reference set", "scope", "decision", "created_at")
        self.query_one("#coverage-release", Select).display = False
        super().on_mount()

    # -- data loading ---------------------------------------------------------

    def _fetch(self) -> dict[str, Any]:
        return {
            "snapshots": data.list_taxonomy_snapshots(self.project),
            "reference_sets": data.list_reference_sets(self.project),
            "releases": data.list_releases(self.project),
            "reports": data.list_coverage_reports(self.project),
        }

    def render_data(self, payload: dict[str, Any]) -> None:
        self.snapshots = payload["snapshots"]
        self.reference_sets = payload["reference_sets"]
        self.releases = payload["releases"]
        self.reports = payload["reports"]

        table = self.query_one("#taxonomy-snapshots-table", DataTable)
        table.clear()
        for row in self.snapshots:
            table.add_row(
                row["taxonomy_snapshot_id"], row["taxonomy_version"],
                str(row["node_count"]), row["status"], str(row["imported_at"]),
                key=str(row["taxonomy_snapshot_id"]),
            )
        table = self.query_one("#reference-sets-table", DataTable)
        table.clear()
        for row in self.reference_sets:
            table.add_row(
                row["reference_set_id"],
                f"{row['profile_name']} v{row['profile_version']}",
                row["taxonomy_version"],
                str(row["family_count"]), str(row["genus_count"]), str(row["compiled_at"]),
                key=str(row["reference_set_id"]),
            )
        select = self.query_one("#coverage-reference-set", Select)
        select.set_options([
            (row["reference_set_id"], row["reference_set_id"]) for row in self.reference_sets
        ])
        select = self.query_one("#coverage-release", Select)
        select.set_options([
            (row["version"], row["version"]) for row in self.releases
        ])
        generate = self.query_one("#coverage-generate", Button)
        generate.disabled = not self.reference_sets

        table = self.query_one("#coverage-reports-table", DataTable)
        table.clear()
        for row in self.reports:
            scope = str(row["scope_kind"])
            if row.get("scope_value"):
                scope += f":{row['scope_value']}"
            table.add_row(
                row["report_id"], str(row["reference_set_id"]), scope,
                styled_decision(row.get("decision")), str(row["created_at"]),
                key=str(row["report_id"]),
            )
        if not self.reference_sets:
            self.query_one("#coverage-error", Static).update(
                Text("no reference sets compiled yet — use `operon taxonomy compile`",
                     style="dim"))

    def show_error(self, exc: BaseException) -> None:
        self.query_one("#coverage-error", Static).update(Text(f"error: {exc}", style="red"))

    # -- report generation ----------------------------------------------------

    def _generate(self) -> None:
        reference_set_id = self.query_one("#coverage-reference-set", Select).value
        if reference_set_id is Select.NULL:
            self.show_error("select a reference set first")
            return
        scope = self.query_one("#coverage-scope", Select).value
        release_version = None
        if scope == "release":
            selected = self.query_one("#coverage-release", Select).value
            if selected is Select.NULL:
                self.show_error("select a release for release-scope coverage")
                return
            release_version = str(selected)
        self.query_one("#coverage-error", Static).update("")
        self.app.push_screen(
            CoverageModal(self.project, str(reference_set_id), release_version),
            self._after_generate,
        )

    def _after_generate(self, result: Any) -> None:
        if result:
            self.app.reload_after_write()

    # -- report browsing --------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "coverage-reports-table" and event.row_key is not None:
            self._load_report(str(event.row_key.value))

    @work(thread=True, exclusive=True, group="coverage-report")
    def _load_report(self, report_id: str) -> None:
        try:
            payload: Any = data.read_coverage_report(self.project, report_id)
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._apply_report, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _apply_report(self, payload: Any) -> None:
        headline = self.query_one("#coverage-report-headline", Static)
        if isinstance(payload, BaseException):
            headline.update(Text(f"error: {payload}", style="red"))
            return
        self.report = payload
        provenance = payload["provenance"]
        scope = str(provenance.get("scope_kind", "?"))
        if provenance.get("scope_value"):
            scope += f":{provenance['scope_value']}"
        headline.update(
            f"{payload['report_id']}  reference set "
            f"{provenance.get('reference_set_id', '?')}  scope {scope}  "
            f"decision {provenance.get('decision', '?')}  "
            f"created {provenance.get('created_at', '?')}"
        )
        for table_name, _title in REPORT_TABS:
            table = self.query_one(f"#coverage-table-{table_name}", DataTable)
            table.clear(columns=True)
            content = payload["tables"].get(table_name)
            if not content:
                table.add_column("(no data)")
                continue
            table.add_columns(*[str(column) for column in content["columns"]])
            for row in content["rows"]:
                table.add_row(*[str(cell) for cell in row])
            if content["truncated"]:
                table.add_row(
                    f"… {content['total'] - data.COVERAGE_REPORT_LIMIT} more rows",
                    *[""] * (len(content["columns"]) - 1),
                )

    # -- events -----------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "coverage-generate":
            self._generate()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "coverage-scope":
            self.query_one("#coverage-release", Select).display = (
                event.value == "release"
            )
