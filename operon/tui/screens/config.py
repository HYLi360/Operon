"""Config screen: structured, control-based editors for configuration files.

Two tabs:

* **QC Profiles** — edit ``kind: qc`` profiles under ``config/profiles/`` with
  structured forms (no free-text YAML).  Every save bumps the version and
  records a content-addressed snapshot, exactly like ``operon evaluate``.
* **Tools & Recipes** — inspect tools, run the equivalent of
  ``operon tools-check``, and edit one recipe inside ``config/tools.yaml``.
  Saving normalizes the file's formatting and drops hand-written comments;
  every version is preserved in ``recipe_snapshots``.

Round-trip fidelity rule: keys the forms do not model (``value_by``,
``source``, ``unknown``, ``result_glob``, parameter spec details, …) are
preserved verbatim and shown as dim read-only notes, never silently dropped.
"""

from __future__ import annotations

from typing import Any, Iterable

import yaml
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from operon.config import Project
from operon.errors import ValidationError
from operon.tui import actions, data
from operon.tui.screens.common import ENTITY_TYPE_OPTIONS, ErrorDialog, Panel, WriteModal

ENTITY_TYPE_NAMES = list(actions.ENTITY_TYPE_NAMES)
OPERATOR_OPTIONS = [(operator, operator) for operator in actions.PROFILE_OPERATORS]
RESULT_PARSERS = ("none", "blast_tabular", "hmmer_tblout", "busco_json")

PROFILE_MODELED_KEYS = frozenset({"kind", "version", "description", "applies_to", "required", "warnings"})
RULE_MODELED_KEYS = frozenset({"metric", "operator", "value", "code"})
RECIPE_MODELED_ORDER = (
    "description", "entity_type", "file_role", "format", "database", "database_version",
    "output_subdir", "output_suffix", "arguments", "parameters", "result_parser",
    "result_columns", "hit_metric_columns", "max_hits_per_query",
)
RECIPE_MODELED_KEYS = frozenset(RECIPE_MODELED_ORDER) | {"version"}

_OMIT = object()


def _extras_note(extras: dict[str, Any]) -> str:
    return "preserved as-is: " + ", ".join(str(key) for key in extras)


class RuleRow(Vertical):
    """One editable rule row: metric / operator / value / code + remove button.

    Rule keys the form does not model (``value_by``, ``source``, ``unknown``,
    ``unknown_code``, ``min``/``max``/``values``, …) are kept verbatim and
    rendered as a dim note below the inputs.
    """

    class RemoveRequested(Message):
        def __init__(self, row: "RuleRow") -> None:
            super().__init__()
            self.row = row

        @property
        def control(self) -> "RuleRow":
            return self.row

    def __init__(self, rule: dict[str, Any]) -> None:
        super().__init__(classes="rule-row")
        self.original = dict(rule)
        self.extras = {key: value for key, value in rule.items() if key not in RULE_MODELED_KEYS}

    def compose(self) -> ComposeResult:
        operator = str(self.original.get("operator", ">="))
        options = list(OPERATOR_OPTIONS)
        if operator not in actions.PROFILE_OPERATORS:
            options.append((f"{operator} (unknown, preserved)", operator))
        value = self.original.get("value")
        with Horizontal(classes="rule-inputs"):
            yield Input(
                value=str(self.original.get("metric", "")),
                placeholder="metric", classes="rule-metric",
            )
            yield Select(options, value=operator, classes="rule-operator", allow_blank=False)
            yield Input(
                value="" if value is None else str(value),
                placeholder="value", classes="rule-value",
            )
            yield Input(
                value=str(self.original.get("code", "")),
                placeholder="code", classes="rule-code",
            )
            yield Button("✕", classes="rule-remove")
        if self.extras:
            yield Static(Text(_extras_note(self.extras), style="dim"), classes="rule-extras")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("rule-remove"):
            event.stop()
            self.post_message(self.RemoveRequested(self))

    def rule_document(self) -> dict[str, Any]:
        """Compose the rule, preserving original key order and unknown keys."""
        metric = self.query_one(".rule-metric", Input).value.strip()
        operator_value = self.query_one(".rule-operator", Select).value
        operator = "" if operator_value is Select.NULL else str(operator_value)
        value_text = self.query_one(".rule-value", Input).value.strip()
        code = self.query_one(".rule-code", Input).value.strip()
        document: dict[str, Any] = {}
        for key, original_value in self.original.items():
            if key == "metric":
                document[key] = metric
            elif key == "operator":
                document[key] = operator
            elif key == "value":
                if value_text:
                    document[key] = actions.coerce_scalar(value_text)
            elif key == "code":
                document[key] = code
            else:
                document[key] = original_value
        if "metric" not in document:
            document["metric"] = metric
        if "operator" not in document:
            document["operator"] = operator
        if value_text and "value" not in document:
            document["value"] = actions.coerce_scalar(value_text)
        if "code" not in document:
            document["code"] = code
        return document


class SnapshotViewModal(ModalScreen):
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
                yield Static(yaml.safe_dump(self.document, sort_keys=False, allow_unicode=True))
            with Horizontal(id="modal-buttons"):
                yield Button("Close", id="cancel", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)


class HistoryModal(ModalScreen):
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
            yield Label(f"Snapshot history — {self.kind_label} {self.target_name}", id="modal-title")
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
        except Exception as exc:  # noqa: BLE001 - shown inline
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


class NewProfileModal(ModalScreen):
    """Prompt for the name of a new qc profile."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label("New qc profile", id="modal-title")
            yield Input(placeholder="profile name, e.g. assembly_strict_v1", id="new-profile-name")
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
        self.dismiss(name)


class ProfileSaveModal(WriteModal):
    """Confirm a profile save: file path + new version + snapshot recording."""

    def __init__(self, project: Project, name: str, document: dict[str, Any], new_version: int) -> None:
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
        return (f"config/profiles/{self.profile_name}.yaml → version {self.new_version} "
                "+ qc_profiles snapshot")

    def confirm(self) -> None:
        self.run_action(
            lambda: actions.save_profile(self.project, self.profile_name, self.document)
        )

    def on_action_success(self, payload: Any) -> None:
        if payload.get("unchanged"):
            self.app.notify(f"{self.profile_name}: unchanged — version {payload['version']} kept")
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
        return (f"config/tools.yaml → {self.tool_name}.{self.recipe_name} version "
                f"{self.new_version} + recipe_snapshots row")

    def confirm(self) -> None:
        self.run_action(
            lambda: actions.save_recipe(
                self.project, self.tool_name, self.recipe_name, self.document,
            )
        )

    def on_action_success(self, payload: Any) -> None:
        if payload.get("unchanged"):
            self.app.notify(f"{self.recipe_name}: unchanged — version {payload['version']} kept")
        else:
            self.app.notify(
                f"saved {self.recipe_name} version {payload['version']} "
                f"(snapshot #{payload['snapshot_id']})"
            )
        self.dismiss(payload)


class ConfigPanel(Panel):
    """Config screen: QC profile editor + tools/recipes editor."""

    def __init__(self, project: Project) -> None:
        super().__init__(id="config")
        self.project = project
        self.profiles: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.recipes: list[dict[str, Any]] = []
        self.current_profile: str | None = None
        self.profile_doc: dict[str, Any] | None = None
        self.current_recipe: str | None = None
        self.recipe_tool: str | None = None
        self.recipe_doc: dict[str, Any] | None = None
        self.checking_tools = False

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with TabbedContent(id="config-tabs"):
            with TabPane("QC Profiles", id="tab-profiles"):
                with Horizontal(id="profiles-layout"):
                    with Vertical(id="profiles-sidebar"):
                        yield ListView(id="profiles-list")
                        with Horizontal(classes="config-buttons"):
                            yield Button("New profile", id="profile-new")
                            yield Button("History", id="profile-history", disabled=True)
                    with VerticalScroll(id="profile-editor"):
                        yield Static("select a profile", id="profile-heading")
                        yield Static("Description", classes="modal-label")
                        yield Input(id="profile-description")
                        yield Static("Applies to", classes="modal-label")
                        for entity_type in ENTITY_TYPE_NAMES:
                            yield Checkbox(entity_type, id=f"profile-applies-{entity_type}")
                        yield Static("", id="profile-version-note")
                        yield Static("", id="profile-extras-note")
                        yield Static("Required rules", classes="modal-label")
                        yield Vertical(id="profile-required-rules")
                        yield Button("add rule", id="profile-add-required")
                        yield Static("Warning rules", classes="modal-label")
                        yield Vertical(id="profile-warnings-rules")
                        yield Button("add rule", id="profile-add-warnings")
                        with Horizontal(classes="config-buttons"):
                            yield Button("Save profile", id="profile-save",
                                         variant="primary", disabled=True)
            with TabPane("Tools && Recipes", id="tab-tools"):
                with Vertical(id="tools-layout"):
                    with Horizontal(classes="config-buttons"):
                        yield Button("Check tools", id="tools-check")
                    yield DataTable(id="tools-table", cursor_type="row")
                    yield Static("Recipes", classes="modal-label")
                    yield DataTable(id="recipes-table", cursor_type="row")
                    with VerticalScroll(id="recipe-editor"):
                        yield Static("select a recipe", id="recipe-heading")
                        yield Static("Description", classes="modal-label")
                        yield Input(id="recipe-description")
                        yield Static("Entity type (blank = *)", classes="modal-label")
                        yield Select(ENTITY_TYPE_OPTIONS, id="recipe-entity-type", allow_blank=True)
                        yield Input(placeholder="file_role", id="recipe-file-role")
                        yield Input(placeholder="format", id="recipe-format")
                        yield Input(placeholder="database", id="recipe-database")
                        yield Input(placeholder="database_version", id="recipe-database-version")
                        yield Input(placeholder="output_subdir", id="recipe-output-subdir")
                        yield Input(placeholder="output_suffix", id="recipe-output-suffix")
                        yield Static("Arguments (one per line; ${placeholders} stay as-is)",
                                     classes="modal-label")
                        yield TextArea(id="recipe-arguments")
                        yield Static("Runtime parameters (name=default per line; other spec "
                                     "keys preserved)", classes="modal-label")
                        yield TextArea(id="recipe-parameters")
                        yield Static("", id="recipe-parameters-note")
                        yield Static("Result parser", classes="modal-label")
                        yield Select([(parser, parser) for parser in RESULT_PARSERS],
                                     value="none", id="recipe-result-parser", allow_blank=False)
                        yield Input(placeholder="result_columns (comma separated)",
                                    id="recipe-result-columns")
                        yield Input(placeholder="hit_metric_columns (comma separated)",
                                    id="recipe-hit-metric-columns")
                        yield Input(placeholder="max_hits_per_query", id="recipe-max-hits")
                        yield Static("", id="recipe-extras-note")
                        with Horizontal(classes="config-buttons"):
                            yield Button("Save recipe", id="recipe-save",
                                         variant="primary", disabled=True)
                            yield Button("History", id="recipe-history", disabled=True)

    def on_mount(self) -> None:
        tools_table = self.query_one("#tools-table", DataTable)
        tools_table.add_column("tool", key="name")
        tools_table.add_column("executable", key="executable")
        tools_table.add_column("run_method", key="run_method")
        tools_table.add_column("detected version", key="version")
        recipes_table = self.query_one("#recipes-table", DataTable)
        recipes_table.add_columns("name", "version", "tool", "entity_type", "file_role", "format")
        super().on_mount()

    # -- data loading -----------------------------------------------------

    def _fetch(self) -> dict[str, Any]:
        return {
            "profiles": data.list_qc_profiles(self.project),
            "tools": data.list_tools(self.project),
            "recipes": data.list_recipes(self.project),
        }

    def render_data(self, payload: dict[str, Any]) -> None:
        self.profiles = payload["profiles"]
        self.tools = payload["tools"]
        self.recipes = payload["recipes"]

        list_view = self.query_one("#profiles-list", ListView)
        list_view.clear()
        for profile in self.profiles:
            list_view.append(ListItem(Label(f"{profile['name']}  v{profile['version']}")))

        tools_table = self.query_one("#tools-table", DataTable)
        tools_table.clear()
        for tool in self.tools:
            tools_table.add_row(
                tool["name"], tool["executable"], tool["run_method"] or "(direct)",
                Text("not checked", style="dim"), key=tool["name"],
            )

        recipes_table = self.query_one("#recipes-table", DataTable)
        recipes_table.clear()
        for recipe in self.recipes:
            recipes_table.add_row(
                recipe["name"], str(recipe["version"]), recipe["tool"],
                recipe["entity_type"], recipe["file_role"], recipe["format"],
                key=recipe["name"],
            )

    def show_error(self, exc: BaseException) -> None:
        self.app.notify(f"config load failed: {exc}", severity="error")

    # -- profile editor ----------------------------------------------------

    def _rule_rows(self, section: str) -> list[RuleRow]:
        return list(self.query_one(f"#profile-{section}-rules", Vertical).query(RuleRow))

    def _render_profile_form(self, name: str, document: dict[str, Any], note: str = "") -> None:
        self.query_one("#profile-heading", Static).update(
            f"{name}" + (f"  —  {note}" if note else "")
        )
        self.query_one("#profile-description", Input).value = str(document.get("description", ""))
        applies_to = {str(item) for item in document.get("applies_to", []) or []}
        for entity_type in ENTITY_TYPE_NAMES:
            self.query_one(f"#profile-applies-{entity_type}", Checkbox).value = (
                entity_type in applies_to
            )
        version = int(document.get("version", 1))
        self.query_one("#profile-version-note", Static).update(Text(
            f"version {version} — saving writes the next version and records a snapshot",
            style="dim",
        ))
        extras = {key: value for key, value in document.items() if key not in PROFILE_MODELED_KEYS}
        self.query_one("#profile-extras-note", Static).update(
            Text(_extras_note(extras), style="dim") if extras else ""
        )
        for section in ("required", "warnings"):
            container = self.query_one(f"#profile-{section}-rules", Vertical)
            container.remove_children()
            for rule in document.get(section, []) or []:
                if isinstance(rule, dict):
                    container.mount(RuleRow(rule))
        self.query_one("#profile-save", Button).disabled = False
        self.query_one("#profile-history", Button).disabled = False

    def _load_profile(self, name: str) -> None:
        try:
            document = data.get_profile_document(self.project, name)
        except ValidationError as exc:
            self.app.notify(str(exc), severity="error")
            return
        self.current_profile = name
        self.profile_doc = document
        self._render_profile_form(name, document)

    def _compose_profile_document(self) -> dict[str, Any]:
        original = self.profile_doc or {}
        document: dict[str, Any] = {
            "kind": "qc",
            "version": int(original.get("version", 1)),
            "description": self.query_one("#profile-description", Input).value.strip(),
            "applies_to": [
                entity_type for entity_type in ENTITY_TYPE_NAMES
                if self.query_one(f"#profile-applies-{entity_type}", Checkbox).value
            ],
            "required": [row.rule_document() for row in self._rule_rows("required")],
            "warnings": [row.rule_document() for row in self._rule_rows("warnings")],
        }
        for key, value in original.items():
            if key not in document:
                document[key] = value
        return document

    def _on_profile_saved(self, payload: Any) -> None:
        if not payload:
            return
        self.reload()
        name = self.current_profile
        if name:
            self._load_profile(name)

    def _profile_file_version(self, name: str) -> int | None:
        try:
            return int(data.get_profile_document(self.project, name).get("version", 1))
        except ValidationError:
            return None

    # -- recipe editor ------------------------------------------------------

    def _render_recipe_form(self, name: str, document: dict[str, Any], note: str = "") -> None:
        self.query_one("#recipe-heading", Static).update(
            f"{self.recipe_tool}.{name}" + (f"  —  {note}" if note else "")
        )
        self.query_one("#recipe-description", Input).value = str(document.get("description", ""))
        entity_type = str(document.get("entity_type", "") or "")
        entity_select = self.query_one("#recipe-entity-type", Select)
        entity_options = list(ENTITY_TYPE_OPTIONS)
        if entity_type and entity_type not in dict(ENTITY_TYPE_OPTIONS):
            entity_options.append((f"{entity_type} (preserved)", entity_type))
            entity_select.set_options(entity_options)
        entity_select.value = entity_type if entity_type else Select.NULL
        self.query_one("#recipe-file-role", Input).value = str(document.get("file_role", "") or "")
        self.query_one("#recipe-format", Input).value = str(document.get("format", "") or "")
        self.query_one("#recipe-database", Input).value = str(document.get("database", "") or "")
        self.query_one("#recipe-database-version", Input).value = str(
            document.get("database_version", "") or "")
        self.query_one("#recipe-output-subdir", Input).value = str(
            document.get("output_subdir", "") or "")
        self.query_one("#recipe-output-suffix", Input).value = str(
            document.get("output_suffix", "") or "")
        arguments = document.get("arguments", []) or []
        self.query_one("#recipe-arguments", TextArea).text = "\n".join(str(a) for a in arguments)
        parameters = document.get("parameters", {}) or {}
        lines = []
        preserved_specs = []
        for param_name, spec in parameters.items():
            spec = spec if isinstance(spec, dict) else {}
            default = spec.get("default")
            lines.append(f"{param_name}={'' if default is None else default}")
            extra_keys = sorted(set(spec) - {"default"})
            if extra_keys:
                preserved_specs.append(f"{param_name}: {', '.join(extra_keys)}")
        self.query_one("#recipe-parameters", TextArea).text = "\n".join(lines)
        self.query_one("#recipe-parameters-note", Static).update(
            Text("preserved spec keys — " + "; ".join(preserved_specs), style="dim")
            if preserved_specs else ""
        )
        parser = str(document.get("result_parser", "none") or "none")
        parser_select = self.query_one("#recipe-result-parser", Select)
        if parser not in RESULT_PARSERS:
            parser_select.set_options(
                [(p, p) for p in RESULT_PARSERS] + [(f"{parser} (preserved)", parser)]
            )
        parser_select.value = parser
        for key, widget_id in (("result_columns", "#recipe-result-columns"),
                               ("hit_metric_columns", "#recipe-hit-metric-columns")):
            columns = document.get(key, []) or []
            self.query_one(widget_id, Input).value = ", ".join(str(c) for c in columns)
        max_hits = document.get("max_hits_per_query")
        self.query_one("#recipe-max-hits", Input).value = "" if max_hits is None else str(max_hits)
        extras = {key: value for key, value in document.items() if key not in RECIPE_MODELED_KEYS}
        self.query_one("#recipe-extras-note", Static).update(
            Text(_extras_note(extras), style="dim") if extras else ""
        )
        self.query_one("#recipe-save", Button).disabled = False
        self.query_one("#recipe-history", Button).disabled = False

    def _load_recipe(self, name: str) -> None:
        try:
            info = data.get_recipe_document(self.project, name)
        except ValidationError as exc:
            self.app.notify(str(exc), severity="error")
            return
        self.current_recipe = name
        self.recipe_tool = info["tool"]
        self.recipe_doc = info["document"]
        self._render_recipe_form(name, info["document"])

    def _compose_recipe_document(self) -> dict[str, Any]:
        original = self.recipe_doc or {}
        entity_value = self.query_one("#recipe-entity-type", Select).value
        entity_type = "" if entity_value is Select.NULL else str(entity_value)
        parser_value = self.query_one("#recipe-result-parser", Select).value
        parser = "none" if parser_value is Select.NULL else str(parser_value)
        arguments = [
            line.strip()
            for line in self.query_one("#recipe-arguments", TextArea).text.splitlines()
            if line.strip()
        ]
        original_parameters = original.get("parameters", {}) or {}
        parameters: dict[str, Any] = {}
        for line in self.query_one("#recipe-parameters", TextArea).text.splitlines():
            line = line.strip()
            if not line:
                continue
            param_name, separator, default_text = line.partition("=")
            param_name = param_name.strip()
            if not param_name:
                continue
            spec = dict(original_parameters.get(param_name) or {})
            if separator and default_text.strip():
                spec["default"] = actions.coerce_scalar(default_text)
            else:
                spec.pop("default", None)
            parameters[param_name] = spec
        columns = [
            column.strip()
            for column in self.query_one("#recipe-result-columns", Input).value.split(",")
            if column.strip()
        ]
        hit_columns = [
            column.strip()
            for column in self.query_one("#recipe-hit-metric-columns", Input).value.split(",")
            if column.strip()
        ]
        max_hits_text = self.query_one("#recipe-max-hits", Input).value.strip()
        max_hits: Any = _OMIT
        if max_hits_text:
            try:
                max_hits = int(max_hits_text)
            except ValueError:
                max_hits = max_hits_text  # save_recipe round-trip rejects with a clear error
        elif "max_hits_per_query" in original:
            max_hits = original["max_hits_per_query"]

        new_values: dict[str, Any] = {}
        for key, widget_id in (("description", "#recipe-description"),
                               ("file_role", "#recipe-file-role"),
                               ("format", "#recipe-format"),
                               ("database", "#recipe-database"),
                               ("database_version", "#recipe-database-version"),
                               ("output_subdir", "#recipe-output-subdir"),
                               ("output_suffix", "#recipe-output-suffix")):
            value = self.query_one(widget_id, Input).value.strip()
            new_values[key] = value if value or key in original else _OMIT
        new_values["entity_type"] = (
            entity_type if entity_type or "entity_type" in original else _OMIT
        )
        new_values["arguments"] = arguments if arguments or "arguments" in original else _OMIT
        new_values["parameters"] = parameters if parameters or "parameters" in original else _OMIT
        new_values["result_parser"] = parser
        new_values["result_columns"] = (
            columns if columns or "result_columns" in original else _OMIT
        )
        new_values["hit_metric_columns"] = (
            hit_columns if hit_columns or "hit_metric_columns" in original else _OMIT
        )
        new_values["max_hits_per_query"] = max_hits

        document: dict[str, Any] = {}
        for key, value in original.items():
            if key in new_values:
                if new_values[key] is not _OMIT:
                    document[key] = new_values[key]
            else:
                document[key] = value
        for key in RECIPE_MODELED_ORDER:
            if key not in document and new_values.get(key, _OMIT) is not _OMIT:
                document[key] = new_values[key]
        return document

    def _recipe_file_version(self, name: str) -> int | None:
        try:
            return int(data.get_recipe_document(self.project, name)["document"].get("version", 1))
        except ValidationError:
            return None

    def _on_recipe_saved(self, payload: Any) -> None:
        if not payload:
            return
        self.reload()
        name = self.current_recipe
        if name:
            self._load_recipe(name)

    # -- tools check --------------------------------------------------------

    def _start_tools_check(self) -> None:
        if self.checking_tools:
            return
        self.checking_tools = True
        self.query_one("#tools-check", Button).disabled = True
        table = self.query_one("#tools-table", DataTable)
        for tool in self.tools:
            table.update_cell(tool["name"], "version", Text("checking…", style="dim"))
        self._tools_check_worker()

    @work(thread=True)
    def _tools_check_worker(self) -> None:
        def on_result(entry: dict[str, Any]) -> None:
            try:
                self.app.call_from_thread(self._apply_tool_result, entry)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

        try:
            payload: Any = actions.check_tools(self.project, on_result=on_result)
        except Exception as exc:  # noqa: BLE001 - routed to _tools_check_done
            payload = exc
        if not self.app.is_running:  # pragma: no cover - shutdown race guard
            return
        try:
            self.app.call_from_thread(self._tools_check_done, payload)
        except RuntimeError:  # pragma: no cover - app is shutting down
            pass

    def _apply_tool_result(self, entry: dict[str, Any]) -> None:
        table = self.query_one("#tools-table", DataTable)
        try:
            if entry["ok"]:
                cell = Text(str(entry["version"]), style="green")
            else:
                cell = Text("MISSING", style="red")
            table.update_cell(entry["name"], "version", cell)
        except KeyError:  # pragma: no cover - table rebuilt mid-check
            pass

    def _tools_check_done(self, payload: Any) -> None:
        self.checking_tools = False
        self.query_one("#tools-check", Button).disabled = False
        if isinstance(payload, BaseException):
            self.app.notify(f"tools-check failed: {payload}", severity="error")
            return
        ok = sum(1 for entry in payload if entry["ok"])
        failed = [entry for entry in payload if not entry["ok"]]
        self.app.notify(
            f"tools-check: {ok}/{len(payload)} tool(s) detected",
            severity="information" if not failed else "warning",
        )
        if failed:
            detail = "\n".join(f"{entry['name']}: {entry['error']}" for entry in failed)
            self.app.push_screen(ErrorDialog("tools-check failures", detail))

    # -- event handlers -----------------------------------------------------

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "profiles-list":
            return
        index = event.list_view.index
        if index is not None and 0 <= index < len(self.profiles):
            self._load_profile(self.profiles[index]["name"])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "recipes-table" and event.row_key.value is not None:
            self._load_recipe(str(event.row_key.value))

    def on_rule_row_remove_requested(self, event: RuleRow.RemoveRequested) -> None:
        event.row.remove()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "profile-add-required":
            self.query_one("#profile-required-rules", Vertical).mount(
                RuleRow({"metric": "", "operator": ">=", "value": "", "code": ""})
            )
        elif button_id == "profile-add-warnings":
            self.query_one("#profile-warnings-rules", Vertical).mount(
                RuleRow({"metric": "", "operator": ">", "value": "", "code": ""})
            )
        elif button_id == "profile-save":
            self._start_profile_save()
        elif button_id == "profile-history":
            self._open_profile_history()
        elif button_id == "profile-new":
            self.app.push_screen(NewProfileModal(), self._on_new_profile)
        elif button_id == "recipe-save":
            self._start_recipe_save()
        elif button_id == "recipe-history":
            self._open_recipe_history()
        elif button_id == "tools-check":
            self._start_tools_check()

    # -- flow starters ------------------------------------------------------

    def _on_new_profile(self, name: Any) -> None:
        if not name:
            return
        name = str(name)
        if (self.project.profiles_dir / f"{name}.yaml").exists():
            self.app.notify(
                f"profile {name!r} already exists — opening it instead", severity="warning",
            )
            self._load_profile(name)
            return
        self.current_profile = name
        self.profile_doc = {
            "kind": "qc", "version": 1, "description": "",
            "applies_to": ["assembly"], "required": [], "warnings": [],
        }
        self._render_profile_form(name, self.profile_doc, note="new profile (not saved yet)")

    def _start_profile_save(self) -> None:
        if not self.current_profile or self.profile_doc is None:
            return
        name = self.current_profile
        document = self._compose_profile_document()
        file_version = self._profile_file_version(name)
        new_version = 1 if file_version is None else file_version + 1
        self.app.push_screen(
            ProfileSaveModal(self.project, name, document, new_version),
            self._on_profile_saved,
        )

    def _open_profile_history(self) -> None:
        if not self.current_profile:
            return
        name = self.current_profile
        rows = data.profile_history(self.project, name)

        def restore(document: dict[str, Any]) -> None:
            self.profile_doc = dict(document)
            self._render_profile_form(
                name, self.profile_doc,
                note="restored from snapshot — saving creates the next version",
            )

        self.app.push_screen(
            HistoryModal(
                self.project, "profile", name, rows,
                fetch_snapshot=lambda sid: data.get_profile_snapshot(self.project, name, sid),
                to_editor=lambda document: document,
            ),
            lambda document: restore(document) if document else None,
        )

    def _start_recipe_save(self) -> None:
        if not self.current_recipe or not self.recipe_tool or self.recipe_doc is None:
            return
        document = self._compose_recipe_document()
        file_version = self._recipe_file_version(self.current_recipe)
        new_version = 1 if file_version is None else file_version + 1
        self.app.push_screen(
            RecipeSaveModal(
                self.project, self.recipe_tool, self.current_recipe, document, new_version,
            ),
            self._on_recipe_saved,
        )

    def _open_recipe_history(self) -> None:
        if not self.current_recipe:
            return
        name = self.current_recipe
        rows = data.recipe_history(self.project, name)

        def restore(document: dict[str, Any]) -> None:
            self.recipe_doc = dict(document)
            self._render_recipe_form(
                name, self.recipe_doc,
                note="restored from snapshot — saving creates the next version",
            )

        self.app.push_screen(
            HistoryModal(
                self.project, "recipe", name, rows,
                fetch_snapshot=lambda sid: data.get_recipe_snapshot(self.project, name, sid),
                to_editor=lambda document: dict(document.get("recipe", {})),
            ),
            lambda document: restore(document) if document else None,
        )

