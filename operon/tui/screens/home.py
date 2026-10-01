"""Home dashboard panel."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Static

from operon import health
from operon.config import Project
from operon.tui import data
from operon.tui.screens.common import (
    Panel,
    format_duration,
    human_size,
    styled_decision,
    styled_status,
)

ATTENTION_PAGE = 10
"""Items shown per source list in the dashboard's attention page (the rest is counted)."""


class HomePanel(Panel):
    """Project overview: counts, decisions, latest release, attention items."""

    def __init__(self, project: Project) -> None:
        super().__init__(id="home")
        self.project = project
        self.summary: dict[str, Any] | None = None
        self.attention: health.AttentionReport | None = None
        self.recent_runs: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="home-actions"):
            yield Button("NCBI Datasets import", id="home-ncbi-datasets")
            yield Button("Import dataset", id="home-import-wizard")
            yield Button("Import table", id="home-import-table")
            yield Button("Create backup", id="home-backup-create")
            yield Button("Verify backup", id="home-backup-verify")
        yield Static("loading…", id="home-body", classes="body")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "home-ncbi-datasets":
            from operon.tui.screens.ncbi_datasets import NcbiDatasetsModal

            self.app.push_screen(NcbiDatasetsModal(self.project), self._after_write)
        elif event.button.id == "home-import-wizard":
            from operon.tui.screens.import_wizard import ImportWizardScreen

            self.app.push_screen(ImportWizardScreen(self.project), self._after_write)
        elif event.button.id == "home-import-table":
            from operon.tui.screens.table_import import ImportTableModal

            self.app.push_screen(ImportTableModal(self.project), self._after_write)
        elif event.button.id == "home-backup-create":
            from operon.tui.screens.backup import BackupModal

            # A backup writes outside the project; no panel data changes.
            self.app.push_screen(BackupModal(self.project))
        elif event.button.id == "home-backup-verify":
            from operon.tui.screens.backup import VerifyBackupModal

            self.app.push_screen(VerifyBackupModal())

    def _after_write(self, payload: Any) -> None:
        if payload:
            self.app.reload_after_write()

    def _fetch(self) -> dict[str, Any]:
        return {
            "summary": data.project_summary(self.project),
            "attention": data.attention_report(self.project, limit=ATTENTION_PAGE),
            "recent_runs": data.list_workflow_runs(self.project, limit=10),
        }

    def render_data(self, payload: dict[str, Any]) -> None:
        self.summary = payload["summary"]
        self.attention = payload["attention"]
        self.recent_runs = payload["recent_runs"]
        self.query_one("#home-body", Static).update(self._build_text())

    def show_error(self, exc: BaseException) -> None:
        self.query_one("#home-body", Static).update(Text(f"error: {exc}", style="red"))

    def _build_text(self) -> Text:
        project = self.project
        summary = self.summary or {}
        text = Text()
        text.append("Project\n", style="bold underline")
        text.append(f"  id:   {project.config['project'].get('id', '-')}\n")
        text.append(f"  name: {project.config['project'].get('name', '-')}\n")
        text.append(f"  root: {project.root}\n")
        text.append(f"  db:   {project.db_path}\n\n")

        text.append("Entities\n", style="bold underline")
        for entity_type, count in (summary.get("entity_counts") or {}).items():
            text.append(f"  {entity_type + 's':<12} {count}\n")
        text.append("\n")

        text.append("Files\n", style="bold underline")
        text.append(
            f"  {summary.get('file_count', 0)} files, "
            f"{human_size(summary.get('file_bytes', 0))} total\n\n"
        )

        text.append("Current decisions\n", style="bold underline")
        decision_counts = summary.get("decision_counts") or {}
        if decision_counts:
            for decision, count in sorted(decision_counts.items()):
                text.append("  ")
                text.append(styled_decision(decision))
                text.append(f"  {count}\n")
        else:
            text.append("  (none)\n", style="dim")
        text.append("\n")

        release = summary.get("latest_release")
        text.append("Latest release\n", style="bold underline")
        if release:
            text.append(f"  {release['version']}  ({release['created_at']})\n\n")
        else:
            text.append("  (none)\n\n", style="dim")

        text.append("Recent workflow runs\n", style="bold underline")
        if self.recent_runs:
            for record in self.recent_runs:
                text.append("  ")
                text.append(styled_status(record.get("status")))
                text.append(
                    f"  {record.get('step', '-'):<16} {record.get('started_at', '-')}"
                    f"  {format_duration(record)}\n"
                )
        else:
            text.append("  (none)\n", style="dim")
        text.append("\n")

        text.append("Attention needed\n", style="bold underline")
        attention = self.attention or health.AttentionReport(items=(), totals={})
        run_items = [
            item for item in attention.items if item.kind == health.KIND_FAILED_RUN
        ]
        decision_items = [
            item
            for item in attention.items
            if item.kind in (health.KIND_DECISION_REVIEW, health.KIND_DECISION_FAIL)
        ]
        file_items = [
            item for item in attention.items if item.kind == health.KIND_FILE_UNHEALTHY
        ]
        for item in run_items:
            text.append("  ")
            text.append(styled_status(item.details.get("status")))
            text.append(f"  run {item.object_id}  {item.details.get('step', '-')}\n")
        failed_total = attention.totals.get(health.KIND_FAILED_RUN, 0)
        if failed_total > len(run_items):
            text.append(
                f"  … and {failed_total - len(run_items)} "
                "more failed/interrupted runs\n",
                style="dim",
            )
        for item in decision_items:
            text.append("  ")
            text.append(styled_decision(item.details.get("decision")))
            text.append(
                f"  {item.object_type} {item.object_id}  "
                f"({item.details.get('profile', '-')})\n"
            )
        decision_total = attention.totals.get(
            health.KIND_DECISION_REVIEW, 0
        ) + attention.totals.get(health.KIND_DECISION_FAIL, 0)
        if decision_total > len(decision_items):
            text.append(
                f"  … and {decision_total - len(decision_items)} more decisions "
                "need review\n",
                style="dim",
            )
        for item in file_items:
            text.append("  ", style=None)
            text.append(Text(str(item.details["status"]), style="red"))
            text.append(
                f"  file {item.object_id}  {item.details.get('relative_path', '-')}\n"
            )
        file_total = attention.totals.get(health.KIND_FILE_UNHEALTHY, 0)
        if file_total > len(file_items):
            text.append(
                f"  … and {file_total - len(file_items)} more unhealthy files\n",
                style="dim",
            )
        if not attention.total:
            text.append("  nothing needs attention\n", style="dim green")
        return text
