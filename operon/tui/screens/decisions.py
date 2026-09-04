"""Decisions screen: current-decision table plus evaluate/curate modals."""

from __future__ import annotations

import json
import os
import shlex
from typing import Any, Iterable

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Select, Static

from operon.config import Project
from operon.tui import actions, data
from operon.tui.screens.common import (
    Panel,
    WriteModal,
    capture_table_view,
    restore_table_view,
    styled_decision,
)

ALL = "ALL"
DECISION_VALUES = ["PASS", "PASS_WITH_WARNINGS", "ACCEPT_WITH_WARNING", "REVIEW", "FAIL", "EXCLUDED"]
FILTER_DECISIONS = DECISION_VALUES + ["NOT_EVALUATED"]


class EvaluateModal(WriteModal):
    """Preview + confirm for `operon evaluate`."""

    def __init__(self, project: Project, selected: dict[str, Any] | None = None) -> None:
        super().__init__("Evaluate decisions")
        self.project = project
        self.selected = selected
        self.profiles = data.list_profiles(project)
        self.default_profile = str(project.config["qc"]["default_profile"])

    def compose_form(self) -> Iterable[Any]:
        scope_options = [("All entities (profile applies_to)", "all")]
        if self.selected:
            label = f"{self.selected['entity_type']}:{self.selected['entity_id']}"
            scope_options.append((f"Selected entity {label}", "selected"))
        yield Static("Scope", classes="modal-label")
        yield Select(scope_options, value="all", id="evaluate-scope", allow_blank=False)
        yield Static("Profile", classes="modal-label")
        default = self.default_profile if self.default_profile in self.profiles else (
            self.profiles[0] if self.profiles else self.default_profile
        )
        yield Select(
            [(name, name) for name in self.profiles] or [(default, default)],
            value=default, id="evaluate-profile", allow_blank=False,
        )

    def _profile(self) -> str:
        value = self.query_one("#evaluate-profile", Select).value
        return str(value) if value is not Select.NULL else self.default_profile

    def command_text(self) -> str:
        parts = ["operon", "evaluate", "--profile", self._profile()]
        if (self.selected
                and self.query_one("#evaluate-scope", Select).value == "selected"):
            parts += ["--entity-type", self.selected["entity_type"],
                      "--entity-id", self.selected["entity_id"]]
        return " ".join(parts)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in {"evaluate-scope", "evaluate-profile"}:
            self.refresh_command()

    def confirm(self) -> None:
        profile = self._profile()
        if self.selected and self.query_one("#evaluate-scope", Select).value == "selected":
            entity_type: str | None = self.selected["entity_type"]
            entity_id: str | None = self.selected["entity_id"]
        else:
            entity_type = entity_id = None
        self.run_action(
            lambda: actions.evaluate(
                self.project, entity_type=entity_type, entity_id=entity_id, profile=profile,
            )
        )

    def on_action_success(self, payload: Any) -> None:
        self.app.notify(f"{len(payload)} decisions evaluated")
        self.dismiss(payload)


class CurateModal(WriteModal):
    """Preview + confirm for `operon curate` on one decision row."""

    def __init__(self, project: Project, row: dict[str, Any]) -> None:
        super().__init__(f"Curate {row['entity_type']} {row['entity_id']}")
        self.project = project
        self.row = row

    def _current(self) -> str:
        return str(self.row.get("curated_decision") or self.row.get("decision") or "-")

    def compose_form(self) -> Iterable[Any]:
        current = self._current()
        yield Static(
            f"entity   {self.row['entity_type']} {self.row['entity_id']}\n"
            f"profile  {self.row['profile']}",
            classes="modal-info",
        )
        yield Static("New decision", classes="modal-label")
        initial = current if current in DECISION_VALUES else Select.NULL
        yield Select(
            [(value, value) for value in DECISION_VALUES],
            value=initial,
            id="curate-decision",
        )
        preview = f"{current} → {'?' if initial is Select.NULL else current}"
        yield Static(preview, id="curate-preview", classes="modal-info")
        yield Input(
            value=os.environ.get("USER", ""), placeholder="reviewer (required)",
            id="curate-reviewer",
        )
        yield Input(placeholder="reason (required)", id="curate-reason")
        yield Input(placeholder="evidence (optional)", id="curate-evidence")

    def _decision(self) -> str:
        value = self.query_one("#curate-decision", Select).value
        return "" if value is Select.NULL else str(value)

    def _preview_text(self) -> str:
        new = self._decision() or "?"
        return f"{self._current()} → {new}"

    def command_text(self) -> str:
        parts = [
            "operon", "curate",
            "--entity-type", self.row["entity_type"],
            "--entity-id", self.row["entity_id"],
            "--profile", self.row["profile"],
            "--decision", self._decision() or "…",
            "--reviewer", shlex.quote(self.query_one("#curate-reviewer", Input).value or "…"),
            "--reason", shlex.quote(self.query_one("#curate-reason", Input).value or "…"),
        ]
        evidence = self.query_one("#curate-evidence", Input).value.strip()
        if evidence:
            parts += ["--evidence", shlex.quote(evidence)]
        return " ".join(parts)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "curate-decision":
            self.query_one("#curate-preview", Static).update(self._preview_text())
            self.refresh_command()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"curate-reviewer", "curate-reason", "curate-evidence"}:
            self.refresh_command()

    def confirm(self) -> None:
        decision = self._decision()
        reviewer = self.query_one("#curate-reviewer", Input).value.strip()
        reason = self.query_one("#curate-reason", Input).value.strip()
        evidence = self.query_one("#curate-evidence", Input).value.strip() or None
        if not decision:
            self.show_error("choose a new decision")
            return
        if not reviewer:
            self.show_error("reviewer is required")
            return
        if not reason:
            self.show_error("reason is required for a curated decision")
            return
        row = self.row
        self.run_action(
            lambda: actions.curate(
                self.project, row["entity_type"], row["entity_id"], row["profile"],
                decision, reviewer, reason, evidence=evidence,
            )
        )

    def on_action_success(self, payload: Any) -> None:
        self.app.notify(
            f"recorded curated decision {self._decision()} for "
            f"{self.row['entity_type']} {self.row['entity_id']}"
        )
        self.dismiss(payload)


class DecisionsPanel(Panel):
    """Current decisions with filters and evaluate/curate write actions."""

    BINDINGS = [
        Binding("e", "evaluate", "Evaluate"),
        Binding("c", "curate", "Curate"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__(id="decisions")
        self.project = project
        self.decisions: list[dict[str, Any]] = []
        self.profiles: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="decisions-layout"):
            with Horizontal(id="decisions-filters"):
                yield Select([(ALL, ALL)], value=ALL, id="decisions-profile", allow_blank=False)
                yield Select(
                    [(ALL, ALL)] + [(value, value) for value in FILTER_DECISIONS],
                    value=ALL, id="decisions-decision", allow_blank=False,
                )
                yield Input(placeholder="filter by entity", id="decisions-filter")
            yield DataTable(id="decisions-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#decisions-table", DataTable)
        table.add_columns("entity", "profile", "decision", "reason_codes", "evaluated_at")
        super().on_mount()

    def _filters(self) -> tuple[str | None, str | None, str]:
        profile_value = self.query_one("#decisions-profile", Select).value
        decision_value = self.query_one("#decisions-decision", Select).value
        profile = None if profile_value in (ALL, Select.NULL) else str(profile_value)
        decision = None if decision_value in (ALL, Select.NULL) else str(decision_value)
        text = self.query_one("#decisions-filter", Input).value.strip()
        return profile, decision, text

    def _fetch(self) -> dict[str, Any]:
        profile, decision, text = self._filters()
        return {
            "decisions": data.list_decisions(
                self.project, profile=profile, decision=decision, text=text,
            ),
            "profiles": data.list_profiles(self.project),
        }

    def render_data(self, payload: dict[str, Any]) -> None:
        self.decisions = payload["decisions"]
        new_profiles = payload["profiles"]
        if new_profiles != self.profiles:
            self.profiles = new_profiles
            select = self.query_one("#decisions-profile", Select)
            current = select.value
            select.set_options([(ALL, ALL)] + [(name, name) for name in new_profiles])
            if current not in (ALL, Select.NULL) and current in new_profiles:
                select.value = current
        table = self.query_one("#decisions-table", DataTable)
        view = capture_table_view(table)
        table.clear()
        for record in self.decisions:
            effective = str(record.get("curated_decision") or record.get("decision") or "-")
            cell = styled_decision(effective)
            if record.get("curated_decision") and record["curated_decision"] != record.get("decision"):
                cell.append(" ✎curated", style="cyan")
            try:
                reasons = ", ".join(json.loads(record.get("reason_codes") or "[]")) or "-"
            except json.JSONDecodeError:
                reasons = str(record.get("reason_codes") or "-")
            table.add_row(
                f"{record['entity_type']}:{record['entity_id']}",
                record["profile"],
                cell,
                reasons,
                str(record.get("evaluated_at") or "-"),
                key=f"{record['entity_type']}:{record['entity_id']}:{record['profile']}",
            )
        restore_table_view(table, view, len(self.decisions))

    def show_error(self, exc: BaseException) -> None:
        self.app.notify(f"decisions load failed: {exc}", severity="error")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "decisions-filter":
            self.reload()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in {"decisions-profile", "decisions-decision"}:
            self.reload()

    def _selected_row(self) -> dict[str, Any] | None:
        table = self.query_one("#decisions-table", DataTable)
        if not self.decisions or table.cursor_row is None:
            return None
        if 0 <= table.cursor_row < len(self.decisions):
            return self.decisions[table.cursor_row]
        return None

    def _after_write(self, result: Any) -> None:
        if result:
            self.app.reload_after_write()

    def action_evaluate(self) -> None:
        self.app.push_screen(
            EvaluateModal(self.project, self._selected_row()), self._after_write,
        )

    def action_curate(self) -> None:
        row = self._selected_row()
        if row is None:
            self.app.notify("select a decision row first", severity="warning")
            return
        self.app.push_screen(CurateModal(self.project, row), self._after_write)
