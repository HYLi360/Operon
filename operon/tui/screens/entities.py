"""Entities browser panel: hierarchy tree plus entity detail."""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterable
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button, Checkbox, Input, Select, Static, Tree

from operon.config import Project
from operon.lifecycle import RETIRE_REASON_CODES
from operon.schema import ENTITY_PREFIXES
from operon.tui import actions, data
from operon.tui.screens.common import (
    ComposedRows,
    MountTracked,
    Panel,
    WriteModal,
    human_size,
    styled_file_status,
    styled_scientific_name,
)

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
            value=os.environ.get("USER", ""), placeholder="actor (required)",
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


class FieldRow(ComposedRows, Horizontal):
    """One ``--field KEY=VALUE`` row in the add-record dialog."""

    class RemoveRequested(Message):
        def __init__(self, row: "FieldRow") -> None:
            super().__init__()
            self.row = row

        @property
        def control(self) -> "FieldRow":
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


class EntitiesPanel(Panel):
    """Organisms → samples → runs/assemblies → annotations with details."""

    BINDINGS = [
        Binding("t", "toggle_retired", "Show/hide retired"),
        Binding("x", "lifecycle", "Retire/restore"),
        Binding("a", "add_record", "Add record"),
        Binding("A", "add_accession", "Add accession"),
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
