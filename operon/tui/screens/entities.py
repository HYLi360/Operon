"""Entities browser panel: hierarchy tree plus entity detail."""

from __future__ import annotations

import os
import shlex
from typing import Any, Iterable

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, Select, Static, Tree

from operon.config import Project
from operon.lifecycle import RETIRE_REASON_CODES
from operon.tui import actions, data
from operon.tui.screens.common import Panel, WriteModal, human_size, styled_file_status


def _node_label(node: dict[str, Any]) -> Text:
    label = Text(str(node["entity_id"]))
    name = node.get("name")
    if name:
        label.append(f"  {name}")
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
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._apply_plan, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

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


class EntitiesPanel(Panel):
    """Organisms → samples → runs/assemblies → annotations with details."""

    BINDINGS = [
        Binding("t", "toggle_retired", "Show/hide retired"),
        Binding("x", "lifecycle", "Retire/restore"),
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

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data is not None:
            self._load_detail(*event.node.data)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        if event.node.data is not None:
            self._load_detail(*event.node.data)

    @work(thread=True, exclusive=True, group="entity-detail")
    def _load_detail(self, entity_type: str, entity_id: str) -> None:
        try:
            payload: Any = data.entity_detail(self.project, entity_type, entity_id)
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._apply_detail, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

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
                text.append(f"  {field:<24} {value}\n")
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
