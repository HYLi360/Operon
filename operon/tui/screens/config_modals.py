"""Config screen modals: snapshot viewer, history browser, save confirmations.

These dialogs are shared by the qc-profile, classification, coverage, tool and
recipe editors of :class:`operon.tui.screens.config.ConfigPanel`; they carry no
state of their own beyond the constructor arguments and never touch the
panel's form widgets.

Round-trip fidelity rule (same as the panel): keys the forms do not model are
preserved verbatim, and saving normalizes the file's formatting and drops
hand-written comments — every version is preserved as a content-addressed
snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import yaml
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from operon.config import Project
from operon.errors import ValidationError
from operon.tui import actions
from operon.tui.screens.common import DismissOnce, FittingSelect, WriteModal


class SnapshotViewModal(DismissOnce, ModalScreen):
    """Read-only rendering of one recorded snapshot document."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, title: str, document: dict[str, Any]) -> None:
        super().__init__()
        self.view_title = title
        self.document = document

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self.view_title, id="modal-title")
            with VerticalScroll(id="snapshot-view-scroll"):
                yield Static(
                    yaml.safe_dump(self.document, sort_keys=False, allow_unicode=True)
                )
            with Horizontal(id="modal-buttons"):
                yield Button("Close", id="cancel", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)


class HistoryModal(DismissOnce, ModalScreen):
    """Snapshot history table with View (read-only) and Restore-into-editor.

    Restoring never overwrites a file: the snapshot document is loaded into
    the editor, and saving it creates the *next* version of the config file.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(
        self,
        project: Project,
        kind_label: str,
        name: str,
        rows: list[dict[str, Any]],
        fetch_snapshot: Any,
        to_editor: Any,
    ) -> None:
        super().__init__()
        self.project = project
        self.kind_label = kind_label
        self.target_name = name
        self.rows = rows
        self.fetch_snapshot = fetch_snapshot
        self.to_editor = to_editor

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(
                f"Snapshot history — {self.kind_label} {self.target_name}",
                id="modal-title",
            )
            yield DataTable(id="history-table", cursor_type="row")
            yield Static("", id="history-error")
            with Horizontal(id="modal-buttons"):
                yield Button("View", id="view")
                yield Button("Restore into editor", id="restore", variant="primary")
                yield Button("Close", id="cancel")

    def on_mount(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.add_columns("snapshot_id", "version", "sha256", "recorded_at", "uses")
        for row in self.rows:
            table.add_row(
                str(row["snapshot_id"]),
                str(row["version"]),
                str(row["sha256"])[:12],
                str(row["recorded_at"]),
                str(row["uses"]),
            )

    def _selected(self) -> dict[str, Any] | None:
        table = self.query_one("#history-table", DataTable)
        if not self.rows or table.cursor_row is None:
            return None
        if 0 <= table.cursor_row < len(self.rows):
            return self.rows[table.cursor_row]
        return None

    def _show_error(self, exc: BaseException) -> None:
        self.query_one("#history-error", Static).update(Text(str(exc), style="red"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        row = self._selected()
        if row is None:
            self._show_error(ValidationError("select a snapshot row first"))
            return
        try:
            document = self.fetch_snapshot(int(row["snapshot_id"]))
        except Exception as exc:  # noqa: BLE001 - shown inline  # pylint: disable=broad-exception-caught
            self._show_error(exc)
            return
        if event.button.id == "view":
            self.app.push_screen(
                SnapshotViewModal(
                    f"{self.kind_label} {self.target_name} — snapshot {row['snapshot_id']} "
                    f"(version {row['version']})",
                    document,
                )
            )
        elif event.button.id == "restore":
            self.dismiss(self.to_editor(document))


class NewProfileModal(DismissOnce, ModalScreen):
    """Prompt for the name and kind of a new profile."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("New profile", id="modal-title")
            yield Input(
                placeholder="profile name, e.g. assembly_strict_v1",
                id="new-profile-name",
            )
            yield Static("Kind", classes="modal-label")
            yield FittingSelect(
                [
                    ("qc (decision thresholds)", "qc"),
                    (
                        "sequence_classification (label sequences)",
                        actions.CLASSIFICATION_KIND,
                    ),
                    (
                        "taxonomy_coverage (coverage denominators)",
                        actions.COVERAGE_KIND,
                    ),
                ],
                value="qc",
                id="new-profile-kind",
                allow_blank=False,
            )
            yield Static("", id="history-error")
            with Horizontal(id="modal-buttons"):
                yield Button("Create", id="confirm", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        name = self.query_one("#new-profile-name", Input).value.strip()
        try:
            actions._validate_config_name("profile", name)
        except ValidationError as exc:
            self.query_one("#history-error", Static).update(Text(str(exc), style="red"))
            return
        kind_value = self.query_one("#new-profile-kind", Select).value
        kind = "qc" if kind_value is Select.NULL else str(kind_value)
        self.dismiss({"name": name, "kind": kind})


class ProfileSaveModal(WriteModal):
    """Confirm a profile save: file path + new version + snapshot recording."""

    def __init__(
        self, project: Project, name: str, document: dict[str, Any], new_version: int
    ) -> None:
        super().__init__(f"Save profile {name}")
        self.project = project
        self.profile_name = name
        self.document = document
        self.new_version = new_version

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            f"writes config/profiles/{self.profile_name}.yaml as version {self.new_version} "
            "+ records a content-addressed snapshot",
            classes="modal-info",
        )

    def command_text(self) -> str:
        return (
            f"config/profiles/{self.profile_name}.yaml → version {self.new_version} "
            "+ qc_profiles snapshot"
        )

    def confirm(self) -> None:
        self.run_action(
            lambda: actions.save_profile(
                self.project,
                self.profile_name,
                self.document,
                known_version=self.new_version - 1,
            )
        )

    def on_action_success(self, payload: Any) -> None:
        if payload.get("unchanged"):
            self.app.notify(
                f"{self.profile_name}: unchanged — version {payload['version']} kept"
            )
        else:
            self.app.notify(
                f"saved {self.profile_name} version {payload['version']} "
                f"(snapshot #{payload['snapshot_id']})"
            )
        self.dismiss(payload)


class RecipeSaveModal(WriteModal):
    """Confirm a recipe save inside tools.yaml (formatting is normalized)."""

    def __init__(
        self,
        project: Project,
        tool_name: str,
        recipe_name: str,
        document: dict[str, Any],
        new_version: int,
    ) -> None:
        super().__init__(f"Save recipe {recipe_name}")
        self.project = project
        self.tool_name = tool_name
        self.recipe_name = recipe_name
        self.document = document
        self.new_version = new_version

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            f"writes config/tools.yaml recipe {self.tool_name}.{self.recipe_name} as version "
            f"{self.new_version} + records a snapshot.  NOTE: saving normalizes the file's "
            "formatting and drops hand-written comments; every version is preserved in "
            "recipe_snapshots.",
            classes="modal-info",
        )

    def command_text(self) -> str:
        return (
            f"config/tools.yaml → {self.tool_name}.{self.recipe_name} version "
            f"{self.new_version} + recipe_snapshots row"
        )

    def confirm(self) -> None:
        self.run_action(
            lambda: actions.save_recipe(
                self.project,
                self.tool_name,
                self.recipe_name,
                self.document,
                known_version=self.new_version - 1,
            )
        )

    def on_action_success(self, payload: Any) -> None:
        if payload.get("unchanged"):
            self.app.notify(
                f"{self.recipe_name}: unchanged — version {payload['version']} kept"
            )
        else:
            self.app.notify(
                f"saved {self.recipe_name} version {payload['version']} "
                f"(snapshot #{payload['snapshot_id']})"
            )
        self.dismiss(payload)


class CondaDefaultsSaveModal(WriteModal):
    """Confirm a save of the top-level ``conda:`` section (formatting is normalized)."""

    def __init__(self, project: Project, document: dict[str, Any]) -> None:
        super().__init__("Save conda defaults")
        self.project = project
        self.document = document

    def compose_form(self) -> Iterable[Any]:
        keys = ", ".join(sorted(self.document)) or "(no conda keys)"
        yield Static(
            f"writes the top-level conda: section of config/tools.yaml ({keys}; a "
            "blanked field falls back to the built-in default) and records one "
            "snapshot per recipe whose resolved launcher changes.  NOTE: saving "
            "normalizes the file's formatting and drops hand-written comments; "
            "every version is preserved in recipe_snapshots.",
            classes="modal-info",
        )

    def command_text(self) -> str:
        return "config/tools.yaml → conda: defaults + recipe_snapshots rows"

    def confirm(self) -> None:
        self.run_action(
            lambda: actions.save_tools_defaults(self.project, self.document)
        )

    def on_action_success(self, payload: Any) -> None:
        if payload.get("unchanged"):
            self.app.notify("conda defaults: unchanged — nothing written")
        else:
            names = ", ".join(payload.get("recipes") or [])
            self.app.notify(
                "saved conda defaults"
                + (f" (snapshots: {names})" if names else " (no launcher changed)")
            )
        self.dismiss(payload)


class ToolSaveModal(WriteModal):
    """Confirm a tool-level save inside tools.yaml (formatting is normalized)."""

    def __init__(
        self,
        project: Project,
        tool_name: str,
        document: dict[str, Any],
    ) -> None:
        super().__init__(f"Save tool {tool_name}")
        self.project = project
        self.tool_name = tool_name
        self.document = document

    def compose_form(self) -> Iterable[Any]:
        keys = ", ".join(sorted(self.document)) or "(no tool-level keys)"
        yield Static(
            f"writes config/tools.yaml tool {self.tool_name} ({keys}; blanked "
            "fields are removed) and records one snapshot per recipe, so "
            "`operon recipes history` shows the change.  NOTE: saving normalizes "
            "the file's formatting and drops hand-written comments; every version "
            "is preserved in recipe_snapshots.",
            classes="modal-info",
        )

    def command_text(self) -> str:
        return f"config/tools.yaml → tool {self.tool_name} + recipe_snapshots rows"

    def confirm(self) -> None:
        self.run_action(
            lambda: actions.save_tool(self.project, self.tool_name, self.document)
        )

    def on_action_success(self, payload: Any) -> None:
        if payload.get("unchanged"):
            self.app.notify(f"{self.tool_name}: unchanged — nothing written")
        else:
            names = ", ".join(payload.get("recipes") or [])
            self.app.notify(
                f"saved tool {self.tool_name}"
                + (f" (snapshots: {names})" if names else "")
            )
        self.dismiss(payload)
