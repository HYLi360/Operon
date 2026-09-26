"""Entities browser panel: hierarchy tree plus entity detail."""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select, Static, Tree

from operon.config import Project, resolve_actor
from operon.lifecycle import RETIRE_REASON_CODES
from operon.schema import ENTITY_PREFIXES, ENTITY_TABLES
from operon.tui import actions, data
from operon.tui.screens.common import (
    ComposedRows,
    DismissOnce,
    MountTracked,
    Panel,
    WorkerResults,
    WriteModal,
    capture_table_view,
    human_size,
    restore_table_view,
    styled_file_status,
    styled_scientific_name,
)
from operon.workflow import TRANSITIONS, VALID_STATES

NEXT_ID_TYPES = list(ENTITY_PREFIXES)


def _node_label(node: dict[str, Any]) -> Text:
    label = Text(str(node["entity_id"]))
    name = node.get("name")
    if name:
        label.append("  ")
        if node.get("entity_type") == "organism":
            label.append_text(styled_scientific_name(name))
        else:
            label.append(str(name))
    state = node.get("state")
    if state:
        label.append(f"  [{state}]", style="dim")
    if node.get("retired"):
        label.stylize("dim strike")
        label.append("  (retired)", style="dim")
    return label


METRIC_ROW_LIMIT = 200


def _metrics_section(
        text: Text,
        title: str,
        rows: list[dict[str, Any]],
        group_key: str,
        *,
        show_tool: bool,
) -> None:
    text.append(f"\n{title}\n", style="bold")
    if not rows:
        text.append("  (none)\n", style="dim")
        return
    group: Any = None
    for row in rows[:METRIC_ROW_LIMIT]:
        if row[group_key] != group:
            group = row[group_key]
            text.append(f"  {group}\n", style="dim")
        text.append(f"    {row['metric_name']} = {row['metric_value']}")
        if row.get("metric_unit"):
            text.append(f" {row['metric_unit']}")
        if show_tool:
            text.append(f"  {row['tool']}@{row['tool_version']}", style="dim")
        text.append("\n")
    if len(rows) > METRIC_ROW_LIMIT:
        text.append(f"  … and {len(rows) - METRIC_ROW_LIMIT} more\n", style="dim")


class LifecycleModal(WriteModal):
    """Plan preview + confirm for `operon retire|restore --apply`."""

    def __init__(self, project: Project, entity_type: str, entity_id: str, retired: bool) -> None:
        self.action = "RESTORE" if retired else "RETIRE"
        super().__init__(f"{self.action.title()} {entity_type} {entity_id}")
        self.project = project
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.plan: dict[str, Any] | None = None

    def compose_form(self) -> Iterable[Any]:
        yield Static("loading plan…", id="lifecycle-plan", classes="modal-info")
        if self.action == "RETIRE":
            yield Static("Reason code", classes="modal-label")
            yield Select(
                [(code, code) for code in sorted(RETIRE_REASON_CODES)],
                value="other", id="lifecycle-reason-code", allow_blank=False,
            )
        yield Input(placeholder="reason (required)", id="lifecycle-reason")
        yield Input(
            value=resolve_actor() or "", placeholder="actor (required)",
            id="lifecycle-actor",
        )
        yield Input(placeholder="evidence (optional)", id="lifecycle-evidence")

    def on_mount(self) -> None:
        super().on_mount()
        self.set_confirm_enabled(False)
        self._load_plan()

    @work(thread=True)
    def _load_plan(self) -> None:
        try:
            payload: Any = actions.lifecycle_preview(
                self.project, self.entity_id, self.action,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal  # pylint: disable=broad-exception-caught
            payload = exc
        self.post_to_ui(self._apply_plan, payload)

    def _apply_plan(self, payload: Any) -> None:
        if isinstance(payload, BaseException):
            self.show_error(payload)
            return
        self.plan = payload
        view = self.query_one("#lifecycle-plan", Static)
        text = Text()
        target = payload["target"]
        text.append(f"action   {payload['action']}\n")
        text.append(f"target   {target['entity_type']} {target['entity_id']}\n")
        counts = payload["entity_counts"]
        affected = {kind: n for kind, n in counts.items() if n}
        text.append("entities " + ("  ".join(f"{kind}:{n}" for kind, n in affected.items()) or "-") + "\n")
        text.append(f"files    {payload['reference_counts']['files']} affected\n")
        references = payload["reference_counts"]
        text.append("refs     " + "  ".join(
            f"{key}:{references[key]}"
            for key in ("accessions", "qc_results", "decisions", "workflow_runs", "release_members")
        ) + "\n")
        physical = payload["physical_changes"]
        text.append("physical ")
        first = True
        for key, value in physical.items():
            if not first:
                text.append("  ")
            chunk = Text(f"{key}={value}")
            if value:
                chunk.stylize("red bold")
            text.append_text(chunk)
            first = False
        text.append("\n")
        if not payload["will_change"]:
            text.append(f"\n{payload['blocker'] or 'no change'}", style="red")
        view.update(text)
        self.set_confirm_enabled(bool(payload["will_change"]))
        self.refresh_command()

    def _reason_code(self) -> str | None:
        if self.action != "RETIRE":
            return None
        value = self.query_one("#lifecycle-reason-code", Select).value
        return None if value is Select.NULL else str(value)

    def command_text(self) -> str:
        parts = [
            "operon", self.action.lower(), self.entity_id,
            "--reason", shlex.quote(self.query_one("#lifecycle-reason", Input).value or "…"),
        ]
        reason_code = self._reason_code()
        if reason_code:
            parts += ["--reason-code", reason_code]
        actor = self.query_one("#lifecycle-actor", Input).value.strip()
        if actor:
            parts += ["--actor", shlex.quote(actor)]
        evidence = self.query_one("#lifecycle-evidence", Input).value.strip()
        if evidence:
            parts += ["--evidence", shlex.quote(evidence)]
        parts += ["--apply", "--yes"]
        return " ".join(parts)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"lifecycle-reason", "lifecycle-actor", "lifecycle-evidence"}:
            self.refresh_command()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "lifecycle-reason-code":
            self.refresh_command()

    def confirm(self) -> None:
        if self.plan is None or not self.plan.get("will_change"):
            return
        reason = self.query_one("#lifecycle-reason", Input).value.strip()
        actor = self.query_one("#lifecycle-actor", Input).value.strip()
        evidence = self.query_one("#lifecycle-evidence", Input).value.strip() or None
        if not reason:
            self.show_error("reason is required")
            return
        if not actor:
            self.show_error("actor is required")
            return
        self.run_action(
            lambda: actions.lifecycle_apply(
                self.project, self.entity_id, self.action, reason, actor,
                reason_code=self._reason_code(), evidence=evidence,
            )
        )

    def on_action_success(self, payload: Any) -> None:
        if payload.get("applied"):
            self.app.notify(f"{self.action} applied to {self.entity_type} {self.entity_id}")
        else:
            self.app.notify(f"no change: {self.entity_type} {self.entity_id}")
        self.dismiss(payload)


class SetStateModal(WriteModal):
    """Form + confirm for `operon set-state` (manual, audited transition).

    Two deliberate departures from the CLI flags, both on the side of the
    audit trail: the message is required here (the CLI defaults it to
    "forced transition"), and ``--force`` is an explicit, default-off
    checkbox — the dialog shows the standard transitions from the current
    state and lets the core's own ``ConflictError`` explain a non-standard
    one instead of pre-empting it.
    """

    def __init__(self, project: Project, entity_type: str, entity_id: str,
                 current_state: str) -> None:
        super().__init__(f"Set state: {entity_type} {entity_id}")
        self.project = project
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.current_state = (current_state or "").upper()

    def compose_form(self) -> Iterable[Any]:
        allowed = sorted(TRANSITIONS.get(self.current_state, set()))
        current = self.current_state or "(none recorded)"
        info = Text()
        info.append(f"current state: {current}\n")
        if self.current_state:
            info.append("standard transitions: " + (", ".join(allowed) if allowed else "(none)") + "\n")
        info.append(
            "a manual change is recorded in `changes` with your message as its "
            "reason and the actor as its author"
        )
        yield Static(info, id="set-state-info", classes="modal-info")
        yield Select(
            [(state, state) for state in sorted(VALID_STATES)],
            prompt="target state", id="set-state-state", allow_blank=True,
        )
        yield Input(placeholder="message (required: the audit reason)", id="set-state-message")
        yield Checkbox(
            "force a non-standard transition (--force; recorded as forced)",
            id="set-state-force",
        )
        yield Static("", id="set-state-hint", classes="modal-info")

    def _selected_state(self) -> str:
        value = self.query_one("#set-state-state", Select).value
        return "" if value is Select.NULL else str(value)

    def _forced(self) -> bool:
        return bool(self.query_one("#set-state-force", Checkbox).value)

    def refresh_hint(self) -> None:
        """Explain the current choice: legal transition, forced, or refused."""
        state = self._selected_state()
        hint = self.query_one("#set-state-hint", Static)
        if not state or not self.current_state:
            hint.update("")
            return
        allowed = TRANSITIONS.get(self.current_state, set())
        if state == self.current_state:
            hint.update(f"{state} is already the current state — nothing to record")
        elif state in allowed:
            hint.update(f"{self.current_state} → {state} is a standard transition")
        elif self._forced():
            hint.update(
                f"{self.current_state} → {state} is NOT a standard transition; "
                "the forced change is recorded in the audit trail"
            )
        else:
            hint.update(
                f"{self.current_state} → {state} is not a standard transition — "
                "tick the force box to make it anyway (audited as forced)"
            )

    def command_text(self) -> str:
        parts = [
            "operon", "set-state",
            "--entity-type", self.entity_type,
            "--entity-id", self.entity_id,
            "--state", self._selected_state() or "…",
        ]
        message = self.query_one("#set-state-message", Input).value.strip()
        if message:
            parts += ["--message", shlex.quote(message)]
        if self._forced():
            parts.append("--force")
        return " ".join(parts)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "set-state-state":
            self.refresh_hint()
            self.refresh_command()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "set-state-message":
            self.refresh_command()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "set-state-force":
            self.refresh_hint()
            self.refresh_command()

    def confirm(self) -> None:
        state = self._selected_state()
        message = self.query_one("#set-state-message", Input).value.strip()
        if not state:
            self.show_error("select the target state")
            return
        if not message:
            self.show_error("message is required: a manual state change is audited with its reason")
            return
        self.run_action(
            lambda: actions.set_state(
                self.project, self.entity_type, self.entity_id, state, message,
                force=self._forced(),
            )
        )

    def on_action_success(self, payload: Any) -> None:
        previous = payload.get("previous_state") or "(none)"
        suffix = " (forced)" if payload.get("forced") else ""
        self.app.notify(
            f"{payload['entity_type']} {payload['entity_id']}: "
            f"{previous} → {payload['state']}{suffix}"
        )
        self.dismiss(payload)


class ExportMetadataModal(WriteModal):
    """Form + confirm for `operon report metadata` (read-only export).

    The export runs through the core exporter with the CLI's own flags, on a
    read-only session: it writes files, never provenance rows.
    """

    def __init__(self, project: Project) -> None:
        super().__init__("Export metadata report")
        self.project = project

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            "write one TSV per table plus manifest.json into the output "
            "directory; the files are byte-identical to a CLI export of the "
            "same project. Read-only: no `changes` or `workflow_runs` rows are "
            "recorded.",
            id="metadata-info", classes="modal-info",
        )
        yield Input(
            placeholder="output directory (blank = reports/metadata)",
            id="metadata-output",
        )
        yield Checkbox(
            "include retired entities (--include-retired)",
            id="metadata-include-retired",
        )

    def command_text(self) -> str:
        parts = ["operon", "report", "metadata"]
        output = self.query_one("#metadata-output", Input).value.strip()
        if output:
            parts += ["--output", shlex.quote(output)]
        if self.query_one("#metadata-include-retired", Checkbox).value:
            parts.append("--include-retired")
        return " ".join(parts)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "metadata-output":
            self.refresh_command()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "metadata-include-retired":
            self.refresh_command()

    def confirm(self) -> None:
        self.run_action(
            lambda: actions.export_metadata_report(
                self.project,
                output=self.query_one("#metadata-output", Input).value.strip() or None,
                include_retired=self.query_one("#metadata-include-retired", Checkbox).value,
            )
        )

    def on_action_success(self, payload: dict[str, Any]) -> None:
        self.app.notify(
            f"metadata report written to {payload['path']} "
            f"({payload['tables']} table(s), {payload['rows']} row(s)"
            + (", retired included" if payload["include_retired"] else "") + ")"
        )
        self.dismiss(payload)


RETIREMENT_COLUMNS = [
    "entity_type", "entity_id", "retired_by_type", "retired_by_id",
    "reason_code", "reason", "actor", "retired_at",
]


class RetiredModal(DismissOnce, WorkerResults, ModalScreen):
    """Read-only ``operon retired`` browser.

    The CLI prints the same rows; the checkbox is the ``--direct-only`` flag.
    Nothing is written: the modal only reads the lifecycle views.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self.rows: list[dict[str, Any]] = []
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box", classes="wide"):
            yield Label("Retired entities", id="modal-title")
            yield Static("", id="retired-command", classes="modal-info")
            yield Checkbox(
                "direct retirements only (--direct-only)",
                id="retired-direct-only",
            )
            yield DataTable(id="retired-table", cursor_type="row")
            yield Static("", id="retired-status")
            with Horizontal(id="modal-buttons"):
                yield Button("Close", id="cancel", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#retired-table", DataTable)
        table.add_columns(*RETIREMENT_COLUMNS)
        self._refresh_command()
        self._load()

    def _refresh_command(self) -> None:
        parts = ["operon", "retired"]
        if self.query_one("#retired-direct-only", Checkbox).value:
            parts.append("--direct-only")
        self.query_one("#retired-command", Static).update(" ".join(parts))

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "retired-direct-only":
            self._refresh_command()
            self._load()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)

    @work(thread=True)
    def _load(self) -> None:
        try:
            payload: Any = data.list_retired(
                self.project,
                direct_only=self.query_one("#retired-direct-only", Checkbox).value,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal  # pylint: disable=broad-exception-caught
            payload = exc
        self.post_to_ui(self._apply, payload)

    def _apply(self, payload: Any) -> None:
        status = self.query_one("#retired-status", Static)
        if isinstance(payload, BaseException):
            status.update(Text(f"error: {payload}", style="red"))
            return
        self.rows = payload
        table = self.query_one("#retired-table", DataTable)
        view = capture_table_view(table)
        table.clear()
        for row in self.rows:
            table.add_row(*[
                "" if row.get(column) is None else str(row.get(column, ""))
                for column in RETIREMENT_COLUMNS
            ])
        restore_table_view(table, view, len(self.rows))
        status.update(
            f"{len(self.rows)} retirement(s)" if self.rows else "no retired entities"
        )


class ExportQcModal(WriteModal):
    """Form + confirm for `operon report qc --export` (read-only export).

    The entity-type filter mirrors the CLI flag; the export itself is the
    core's own, so the TSVs are byte-identical to a CLI run with the same
    filter.
    """

    def __init__(self, project: Project, entity_type: str = "") -> None:
        super().__init__("Export QC results")
        self.project = project
        self.initial_entity_type = entity_type or ""

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            "write qc/aggregate/qc_results.tsv (long form) and "
            "qc_results.wide.tsv (one row per entity/file) — the files are "
            "byte-identical to a CLI export with the same filter. Read-only: "
            "no `changes` or `workflow_runs` rows are recorded.",
            id="qc-export-info", classes="modal-info",
        )
        options = [("all entity types", "")] + [
            (kind, kind) for kind in sorted(ENTITY_TABLES)
        ]
        yield Select(
            options, value=self.initial_entity_type, id="qc-export-type",
            allow_blank=False,
        )
        yield Checkbox(
            "include retired entities (--include-retired)",
            id="qc-export-include-retired",
        )

    def _entity_type(self) -> str:
        value = self.query_one("#qc-export-type", Select).value
        return "" if value is Select.NULL else str(value)

    def command_text(self) -> str:
        parts = ["operon", "report", "qc"]
        entity_type = self._entity_type()
        if entity_type:
            parts += ["--entity-type", entity_type]
        parts.append("--export")
        if self.query_one("#qc-export-include-retired", Checkbox).value:
            parts.append("--include-retired")
        return " ".join(parts)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "qc-export-type":
            self.refresh_command()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "qc-export-include-retired":
            self.refresh_command()

    def confirm(self) -> None:
        self.run_action(
            lambda: actions.export_qc_report(
                self.project,
                entity_type=self._entity_type() or None,
                include_retired=self.query_one("#qc-export-include-retired", Checkbox).value,
            )
        )

    def on_action_success(self, payload: dict[str, Any]) -> None:
        self.app.notify(
            f"QC report written to {payload['directory']} "
            f"({payload['rows']} row(s)"
            + (f", entity type {payload['entity_type']}" if payload["entity_type"] else "")
            + (", retired included" if payload["include_retired"] else "") + ")"
        )
        self.dismiss(payload)


class FieldRow(ComposedRows, Horizontal):
    """One ``--field KEY=VALUE`` row in the add-record dialog."""

    class RemoveRequested(Message):
        def __init__(self, row: FieldRow) -> None:
            super().__init__()
            self.row = row

        @property
        def control(self) -> FieldRow:
            return self.row

    def __init__(self, field: str = "", value: str = "") -> None:
        super().__init__(classes="field-row")
        self._initial = (field, value)

    def compose(self) -> ComposeResult:
        field, value = self._initial
        yield Input(value=field, placeholder="field", classes="field-key")
        yield Input(value=value, placeholder="value", classes="field-value")
        yield Button("✕", classes="field-remove")

    def on_mount(self) -> None:
        self.mark_form_ready()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("field-remove"):
            event.stop()
            self.post_message(self.RemoveRequested(self))

    def pair(self) -> tuple[str, str]:
        """The row's ``(key, value)`` with the CLI's ``parse_key_values`` key rules."""
        key = self.query_one(".field-key", Input).value
        value = self.query_one(".field-value", Input).value
        return key.strip().strip("-"), value


class AddRecordModal(WriteModal):
    """Add one metadata record (``operon add``): type, optional ID, KEY=VALUE rows."""

    def __init__(self, project: Project) -> None:
        super().__init__("Add metadata record")
        self.project = project

    def compose_form(self) -> Iterable[Any]:
        yield Static("entity type", classes="modal-label")
        yield Select(
            [(name, name) for name in data.ENTITY_TYPES],
            value=data.ENTITY_TYPES[0], id="add-entity-type", allow_blank=False,
        )
        yield Static("internal ID (blank = allocate the next ID)", classes="modal-label")
        yield Input(placeholder="auto-allocate", id="add-record-id")
        yield Static("fields (repeatable, like --field KEY=VALUE)", classes="modal-label")
        yield MountTracked(id="add-fields")
        with Horizontal(classes="config-buttons"):
            yield Button("Add field", id="add-field-row")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#add-fields", MountTracked).mount_later(
            FieldRow(), when_present=".field-row",
        )

    def _field_container(self) -> MountTracked:
        return self.query_one("#add-fields", MountTracked)

    def _all_field_rows(self) -> list[FieldRow]:
        # A row on its way out answers NoMatches or blank (ODR-0036): skip it.
        return [row for row in self._field_container().query(FieldRow).results(FieldRow)
                if not row._pruning]

    def _field_rows(self) -> list[FieldRow]:
        """Rows a reader may compose: a half-mounted row cannot be read (ODR-0023).

        ``WriteModal.on_mount`` runs a second time through Textual's MRO message
        dispatch right after ``mount_later`` registered the seeded row, while
        that row is in the tree without its composed inputs.
        """
        return [row for row in self._all_field_rows() if row.form_ready]

    def _fields_ready(self) -> bool:
        try:
            container = self._field_container()
        except NoMatches:
            return False
        if not container.mounts_settled:
            return False
        return all(row.form_ready for row in self._all_field_rows())

    def _entity_type(self) -> str:
        return str(self.query_one("#add-entity-type", Select).value)

    def _field_pairs(self) -> list[tuple[str, str]]:
        return [row.pair() for row in self._field_rows()]

    def command_text(self) -> str:
        parts = ["operon", "add", self._entity_type()]
        record_id = self.query_one("#add-record-id", Input).value.strip()
        if record_id:
            parts += ["--id", shlex.quote(record_id)]
        for key, value in self._field_pairs():
            if key:
                parts += ["--field", f"{key}={shlex.quote(value)}"]
        return " ".join(parts)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "add-record-id" or event.input.has_class("field-key") \
                or event.input.has_class("field-value"):
            self.refresh_command()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "add-entity-type":
            self.refresh_command()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-field-row":
            event.stop()
            self._field_container().mount_later(FieldRow(), when_present=".field-row")
        else:
            # ODR-0047: the MRO dispatch would run WriteModal's handler a second
            # time, running the confirmed action twice per click.
            event.prevent_default()
            super().on_button_pressed(event)

    def on_field_row_remove_requested(self, event: FieldRow.RemoveRequested) -> None:
        event.stop()
        event.row.remove()
        self.refresh_command()

    def confirm(self) -> None:
        if not self._fields_ready():
            self.show_error("the form is still loading — confirm again in a moment")
            return
        entity_type = self._entity_type()
        record_id = self.query_one("#add-record-id", Input).value.strip() or None
        fields: dict[str, str] = {}
        for key, value in self._field_pairs():
            if not key and value:
                self.show_error(f"field name is required for value {value!r}")
                return
            if not key:
                continue
            if key in fields:
                self.show_error(f"duplicate field {key!r}")
                return
            fields[key] = value
        self.run_action(
            lambda: actions.add_record(
                self.project, entity_type, fields, record_id=record_id,
            )
        )

    def on_action_success(self, payload: Any) -> None:
        self.app.notify(f"added {payload['entity_type']} {payload['entity_id']}")
        for warning in payload.get("warnings") or []:
            self.app.notify(warning, severity="warning")
        self.dismiss(payload)


class AddAccessionModal(WriteModal):
    """Map an external accession to an internal stable ID (``operon add-accession``)."""

    def __init__(self, project: Project, entity_type: str | None = None,
                 entity_id: str | None = None) -> None:
        super().__init__("Add accession mapping")
        self.project = project
        self._prefill_type = entity_type
        self._prefill_id = entity_id

    def compose_form(self) -> Iterable[Any]:
        yield Static("internal entity (must be active)", classes="modal-label")
        yield Select(
            [(name, name) for name in data.ENTITY_TYPES],
            value=self._prefill_type if self._prefill_type in data.ENTITY_TYPES
            else data.ENTITY_TYPES[0],
            id="acc-internal-type", allow_blank=False,
        )
        yield Input(
            value=self._prefill_id or "",
            placeholder="internal id (e.g. ASM_000001)", id="acc-internal-id",
        )
        yield Input(placeholder="namespace (e.g. NCBI_Assembly)", id="acc-namespace")
        yield Input(placeholder="accession", id="acc-accession")
        yield Input(placeholder="version (optional)", id="acc-version")
        yield Checkbox("primary mapping", id="acc-primary")

    def command_text(self) -> str:
        def quoted(value: str) -> str:
            return shlex.quote(value) if value.strip() else shlex.quote("…")

        parts = [
            "operon", "add-accession",
            "--internal-type", str(self.query_one("#acc-internal-type", Select).value),
            "--internal-id", quoted(self.query_one("#acc-internal-id", Input).value),
            "--namespace", quoted(self.query_one("#acc-namespace", Input).value),
            "--accession", quoted(self.query_one("#acc-accession", Input).value),
        ]
        version = self.query_one("#acc-version", Input).value
        if version.strip():
            parts += ["--version", shlex.quote(version.strip())]
        if self.query_one("#acc-primary", Checkbox).value:
            parts.append("--primary")
        return " ".join(parts)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("acc-"):
            self.refresh_command()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "acc-internal-type":
            self.refresh_command()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "acc-primary":
            self.refresh_command()

    def confirm(self) -> None:
        internal_type = str(self.query_one("#acc-internal-type", Select).value)
        internal_id = self.query_one("#acc-internal-id", Input).value.strip()
        namespace = self.query_one("#acc-namespace", Input).value.strip()
        accession = self.query_one("#acc-accession", Input).value.strip()
        version = self.query_one("#acc-version", Input).value.strip() or None
        primary = self.query_one("#acc-primary", Checkbox).value
        missing = [
            label for label, value in (
                ("--internal-id", internal_id), ("--namespace", namespace),
                ("--accession", accession))
            if not value
        ]
        if missing:
            self.show_error(f"required: {', '.join(missing)}")
            return
        self.run_action(lambda: actions.add_accession(
            self.project, internal_type=internal_type, internal_id=internal_id,
            namespace=namespace, accession=accession, version=version, primary=primary,
        ))

    def on_action_success(self, payload: Any) -> None:
        self.app.notify(
            f"mapped {payload['namespace']}:{payload['accession']} -> "
            f"{payload['internal_type']} {payload['internal_id']}"
        )
        self.dismiss(payload)


class NextIdModal(WriteModal):
    """Reserve the next stable internal ID (``operon next-id``).

    The reservation consumes the ID immediately, so the modal stays open after
    a successful reservation to show the ID, and Confirm is disabled — a second
    reservation would burn another ID.  Close with *Close*/``esc`` (the
    reservation is not undoable, exactly like the CLI).
    """

    def __init__(self, project: Project) -> None:
        super().__init__("Reserve next internal ID")
        self.project = project
        self._reserved = False

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            "Reserving an ID consumes it; an unused reservation becomes a gap, "
            "exactly like the CLI.",
            classes="modal-info",
        )
        yield Static("entity type", classes="modal-label")
        yield Select(
            [(name, name) for name in NEXT_ID_TYPES],
            value=NEXT_ID_TYPES[0], id="nextid-entity-type", allow_blank=False,
        )
        yield Static("", id="nextid-result")

    def command_text(self) -> str:
        return f"operon next-id {self.query_one('#nextid-entity-type', Select).value}"

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "nextid-entity-type" and not self._reserved:
            self.refresh_command()

    def confirm(self) -> None:
        if self._reserved:
            return
        self.run_action(lambda: actions.reserve_next_id(
            self.project, str(self.query_one("#nextid-entity-type", Select).value)))

    def on_action_success(self, payload: Any) -> None:
        self._reserved = True
        self.query_one("#nextid-result", Static).update(Text(
            f"reserved {payload['entity_type']} ID: {payload['entity_id']}",
            style="bold",
        ))
        self.set_confirm_enabled(False)
        self.query_one("#cancel", Button).label = "Close"
        self.app.notify(
            f"reserved {payload['entity_type']} {payload['entity_id']} "
            "(unused reservations become gaps)"
        )


class EntitiesPanel(Panel):
    """Organisms → samples → runs/assemblies → annotations with details."""

    BINDINGS = [
        Binding("t", "toggle_retired", "Show/hide retired"),
        Binding("x", "lifecycle", "Retire/restore"),
        Binding("s", "set_state", "Set state"),
        Binding("a", "add_record", "Add record"),
        Binding("A", "add_accession", "Add accession"),
        Binding("n", "next_id", "Next ID"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__(id="entities")
        self.project = project
        self.include_retired = True
        self.tree_data: list[dict[str, Any]] = []
        self.detail: dict[str, Any] | None = None
        self._retired_index: dict[tuple[str, str], bool] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="entities-layout"):
            yield Tree("entities", id="entities-tree")
            with VerticalScroll(id="entity-detail-scroll"):
                yield Static("select an entity", id="entity-detail", classes="body")
        with Horizontal(id="entities-actions"):
            yield Button("Set state", id="entities-set-state")
            yield Button("Export metadata…", id="entities-export-metadata")
            yield Button("Export QC…", id="entities-export-qc")
            yield Button("Retired…", id="entities-retired")

    def _fetch(self) -> list[dict[str, Any]]:
        return data.entity_tree(self.project, include_retired=self.include_retired)

    def render_data(self, payload: list[dict[str, Any]]) -> None:
        self.tree_data = payload
        self._retired_index = {}
        tree = self.query_one("#entities-tree", Tree)
        tree.clear()

        def populate(parent: Any, nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
                self._retired_index[(node["entity_type"], node["entity_id"])] = node["retired"]
                child = parent.add(
                    _node_label(node),
                    data=(node["entity_type"], node["entity_id"]),
                )
                populate(child, node["children"])

        populate(tree.root, payload)
        tree.root.expand()

    def show_error(self, exc: BaseException) -> None:
        self.query_one("#entity-detail", Static).update(Text(f"error: {exc}", style="red"))

    def action_toggle_retired(self) -> None:
        self.include_retired = not self.include_retired
        self.reload()

    def action_lifecycle(self) -> None:
        if not self.detail:
            self.app.notify("select an entity first", severity="warning")
            return
        entity_type = self.detail["entity_type"]
        entity_id = self.detail["entity_id"]
        retired = self._retired_index.get((entity_type, entity_id), False)
        self.app.push_screen(
            LifecycleModal(self.project, entity_type, entity_id, retired),
            self._after_lifecycle,
        )

    def _after_lifecycle(self, result: Any) -> None:
        if result:
            self.app.reload_after_write()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "entities-set-state":
            self.action_set_state()
        elif event.button.id == "entities-export-metadata":
            self.app.push_screen(ExportMetadataModal(self.project))
        elif event.button.id == "entities-export-qc":
            entity_type = self.detail["entity_type"] if self.detail else ""
            self.app.push_screen(ExportQcModal(self.project, entity_type))
        elif event.button.id == "entities-retired":
            self.app.push_screen(RetiredModal(self.project))

    def action_set_state(self) -> None:
        if not self.detail:
            self.app.notify("select an entity first", severity="warning")
            return
        entity_type = self.detail["entity_type"]
        entity_id = self.detail["entity_id"]
        state = self.detail.get("state") or {}
        self.app.push_screen(
            SetStateModal(self.project, entity_type, entity_id, state.get("state", "")),
            self._after_set_state,
        )

    def _after_set_state(self, result: Any) -> None:
        if result:
            self.app.reload_after_write()

    def action_add_record(self) -> None:
        self.app.push_screen(AddRecordModal(self.project), self._after_add)

    def action_add_accession(self) -> None:
        entity_type = entity_id = None
        if self.detail:
            entity_type = self.detail["entity_type"]
            entity_id = self.detail["entity_id"]
        self.app.push_screen(
            AddAccessionModal(self.project, entity_type, entity_id),
            self._after_add,
        )

    def action_next_id(self) -> None:
        self.app.push_screen(NextIdModal(self.project))

    def _after_add(self, result: Any) -> None:
        if result:
            self.app.reload_after_write()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data is not None:
            self._show_detail(*event.node.data)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data is not None:
            self._show_detail(*event.node.data)

    def _show_detail(self, entity_type: str, entity_id: str) -> None:
        """Read one entity's detail, stamped with the node it answers.

        A read already inside its thread still posts its payload when
        ``exclusive=True`` cancelled its worker (ODR-0031): the stamp lets the
        panel drop the superseded entity instead of overwriting the pane.
        """
        self.begin_request((entity_type, entity_id))
        self._load_detail(entity_type, entity_id)

    @work(thread=True, exclusive=True, group="entity-detail")
    def _load_detail(self, entity_type: str, entity_id: str) -> None:
        try:
            payload: Any = data.entity_detail(self.project, entity_type, entity_id)
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel  # pylint: disable=broad-exception-caught
            payload = exc
        self.post_to_ui(self._apply_detail, payload, key=(entity_type, entity_id))

    def _apply_detail(self, payload: Any) -> None:
        detail_view = self.query_one("#entity-detail", Static)
        if isinstance(payload, BaseException):
            detail_view.update(Text(f"error: {payload}", style="red"))
            return
        self.detail = payload
        detail_view.update(self._detail_text(payload))

    def _detail_text(self, detail: dict[str, Any] | None) -> Text:
        if detail is None:
            return Text("entity not found", style="red")
        text = Text()
        text.append(f"{detail['entity_type']} {detail['entity_id']}\n", style="bold underline")
        for field, value in detail["fields"].items():
            if value not in (None, ""):
                text.append(f"  {field:<24} ")
                if field == "scientific_name":
                    text.append_text(styled_scientific_name(value))
                    text.append("\n")
                else:
                    text.append(f"{value}\n")
        supersessions = detail.get("supersessions") or []
        if supersessions:
            # The browser lists the links instead of hiding the entities the
            # CLI's default graph view drops; each row names both ends so the
            # chain reads in either direction.
            text.append("\nSupersessions\n", style="bold")
            for row in supersessions:
                if (row["object_type"], row["object_id"]) == (
                    detail["entity_type"], detail["entity_id"],
                ):
                    line = (f"  superseded by {row['superseded_by_type']} "
                            f"{row['superseded_by_id']}")
                else:
                    line = (f"  supersedes {row['object_type']} "
                            f"{row['object_id']}")
                if row.get("reason"):
                    line += f"  — {row['reason']}"
                text.append(line + f"  ({row['superseded_at']})\n")
        state = detail.get("state")
        text.append("\nState\n", style="bold")
        if state:
            text.append(f"  {state['state']}"
                        + (f"  — {state['message']}" if state.get("message") else "")
                        + f"  ({state['updated_at']})\n")
        else:
            text.append("  (no state recorded)\n", style="dim")
        text.append("\nAccessions\n", style="bold")
        if detail["accessions"]:
            for accession in detail["accessions"]:
                primary = " (primary)" if accession.get("is_primary") else ""
                version = f".{accession['version']}" if accession.get("version") else ""
                text.append(f"  {accession['namespace']}:{accession['accession']}{version}{primary}\n")
        else:
            text.append("  (none)\n", style="dim")
        text.append("\nFiles\n", style="bold")
        if detail["files"]:
            for record in detail["files"]:
                text.append("  ")
                text.append(styled_file_status(record.get("status")))
                text.append(f"  {record['file_id']}  {record['file_role']:<18} "
                            f"{human_size(record.get('size_bytes')):>10}  {record['relative_path']}\n")
        else:
            text.append("  (none)\n", style="dim")
        metrics = detail.get("metrics") or {}
        _metrics_section(
            text, "QC metrics", metrics.get("qc") or [], "qc_stage", show_tool=True,
        )
        _metrics_section(
            text, "Analysis metrics", metrics.get("analysis") or [], "analysis_name",
            show_tool=False,
        )
        return text
