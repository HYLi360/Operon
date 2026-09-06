"""Home dashboard panel."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Static

from operon.config import Project
from operon.tui import data
from operon.tui.screens.common import (
    Panel,
    format_duration,
    human_size,
    styled_decision,
    styled_status,
)


class HomePanel(Panel):
    """Project overview: counts, decisions, latest release, attention items."""

    def __init__(self, project: Project) -> None:
        super().__init__(id="home")
        self.project = project
        self.summary: dict[str, Any] | None = None
        self.attention: dict[str, Any] | None = None
        self.recent_runs: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Static("loading…", id="home-body", classes="body")

    def _fetch(self) -> dict[str, Any]:
        return {
            "summary": data.project_summary(self.project),
            "attention": data.attention_items(self.project),
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
        text.append(f"  {summary.get('file_count', 0)} files, "
                    f"{human_size(summary.get('file_bytes', 0))} total\n\n")

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
        attention = self.attention or {}
        items = 0
        for record in attention.get("runs") or []:
            items += 1
            text.append("  ")
            text.append(styled_status(record.get("status")))
            text.append(f"  run {record['run_id']}  {record.get('step', '-')}\n")
        if (attention.get("failed_run_count") or 0) > len(attention.get("runs") or []):
            text.append(f"  … and {attention['failed_run_count'] - len(attention.get('runs', []))} "
                        "more failed/interrupted runs\n", style="dim")
        for row in attention.get("decisions") or []:
            items += 1
            effective = row.get("curated_decision") or row.get("decision")
            text.append("  ")
            text.append(styled_decision(effective))
            text.append(f"  {row['entity_type']} {row['entity_id']}  ({row.get('profile', '-')})\n")
        for row in attention.get("files") or []:
            items += 1
            text.append(f"  ", style=None)
            text.append(Text(str(row["status"]), style="red"))
            text.append(f"  file {row['file_id']}  {row.get('relative_path', '-')}\n")
        if not items:
            text.append("  nothing needs attention\n", style="dim green")
        return text
