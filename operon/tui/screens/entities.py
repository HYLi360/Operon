"""Entities browser panel: hierarchy tree plus entity detail."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static, Tree

from operon.config import Project
from operon.tui import data
from operon.tui.screens.common import Panel, human_size, styled_file_status


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


class EntitiesPanel(Panel):
    """Organisms → samples → runs/assemblies → annotations with details."""

    BINDINGS = [
        Binding("t", "toggle_retired", "Toggle retired"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__(id="entities")
        self.project = project
        self.include_retired = False
        self.tree_data: list[dict[str, Any]] = []
        self.detail: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="entities-layout"):
            yield Tree("entities", id="entities-tree")
            with VerticalScroll(id="entity-detail-scroll"):
                yield Static("select an entity", id="entity-detail", classes="body")

    def _fetch(self) -> list[dict[str, Any]]:
        return data.entity_tree(self.project, include_retired=self.include_retired)

    def render_data(self, payload: list[dict[str, Any]]) -> None:
        self.tree_data = payload
        tree = self.query_one("#entities-tree", Tree)
        tree.clear()

        def populate(parent: Any, nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
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
        return text
