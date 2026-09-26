"""Config screen: structured, control-based editors for configuration files.

Two tabs:

* **QC Profiles** — edit ``kind: qc``, ``kind: sequence_classification`` and
  ``kind: taxonomy_coverage`` profiles under ``config/profiles/`` with
  structured forms (no free-text YAML).  Every save bumps the version and
  records a content-addressed snapshot, exactly like ``operon evaluate``.
* **Tools & Recipes** — inspect tools, run the equivalent of
  ``operon tools-check``, and edit one recipe inside ``config/tools.yaml``.
  Saving normalizes the file's formatting and drops hand-written comments;
  every version is preserved in ``recipe_snapshots``.

Round-trip fidelity rule: keys the forms do not model (``value_by``,
``source``, ``unknown``, ``database_mode``, ``output_name``, parameter spec
details, …) are preserved verbatim and shown as dim read-only notes, never
silently dropped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import yaml
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
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
from operon.tui.screens.common import (
    ENTITY_TYPE_OPTIONS,
    ComposedRows,
    DismissOnce,
    ErrorDialog,
    FittingSelect,
    MountTracked,
    Panel,
    WriteModal,
    remount,
)
from operon.tui.screens.config_classification import (
    CLASSIFICATION_MODELED_KEYS,
    ClassificationRuleRow,
    ClassificationSaveModal,
    SourceRow,
    classification_form_supported,
)
from operon.tui.screens.config_coverage import (
    COVERAGE_MODELED_KEYS,
    COVERAGE_RANKS,
    SECTION_MODELED_KEYS,
    THRESHOLD_MODELED_KEYS,
    CoverageSaveModal,
    coverage_form_supported,
)

ENTITY_TYPE_NAMES = list(actions.ENTITY_TYPE_NAMES)
OPERATOR_OPTIONS = [(operator, operator) for operator in actions.PROFILE_OPERATORS]
RESULT_PARSERS = (
    "none", "blast_tabular", "hmmer_tblout", "hmmer_domtblout", "rpsbproc_tabular", "busco_json",
)
ARTIFACT_KINDS = ("file", "directory")
# Keep in sync with operon.tools.ENVIRONMENT_POLICIES (asserted in
# tests/unit/test_tui_config.py); blank means the key stays absent and the
# core default ("warn") applies.
ENVIRONMENT_POLICIES = ("ignore", "warn", "strict")
HMMER_MODES = ("hmmsearch", "hmmscan")
DATABASE_MODES = ("reference", "mutable_cache")
#: The `slurm` override keys the per-recipe form models, in the order
#: `execution.slurm` documents them; anything else a recipe carries survives
#: the round-trip untouched (a future core key must not be dropped here).
RECIPE_SLURM_FIELDS = (
    ("partition", "#recipe-slurm-partition", "input"),
    ("time", "#recipe-slurm-time", "input"),
    ("mem_gb", "#recipe-slurm-mem-gb", "int"),
    ("poll_interval", "#recipe-slurm-poll-interval", "float"),
    ("array", "#recipe-slurm-array", "tri"),
    ("array_concurrency", "#recipe-slurm-array-concurrency", "int"),
    ("extra_sbatch", "#recipe-slurm-extra-sbatch", "lines"),
    ("setup_commands", "#recipe-slurm-setup-commands", "lines"),
)

PROFILE_MODELED_KEYS = frozenset({"kind", "version", "description", "applies_to", "required", "warnings"})
RULE_MODELED_KEYS = frozenset({"metric", "operator", "value", "code"})
RECIPE_MODELED_ORDER = (
    "description", "entity_type", "file_role", "file_role_prefix", "format",
    "input_kind", "output_kind", "database", "database_version",
    "database_mode", "database_checksum",
    "environment_policy", "output_subdir", "output_suffix", "output_name", "arguments",
    "commands", "parameters", "slurm", "result_parser", "result_glob", "hmmer_mode",
    "result_columns", "hit_metric_columns", "query_column", "subject_column",
    "numeric_columns", "qstart_column", "qend_column", "sstart_column",
    "send_column", "evalue_column", "bitscore_column", "pident_column",
    "max_hits_per_query",
)
RECIPE_MODELED_KEYS = frozenset(RECIPE_MODELED_ORDER) | {"version"}

_OMIT = object()


def _extras_note(extras: dict[str, Any]) -> str:
    return "preserved as-is: " + ", ".join(str(key) for key in extras)


class RuleRow(ComposedRows, Vertical):
    """One editable rule row: metric / operator / value / code + remove button.

    Rule keys the form does not model (``value_by``, ``source``, ``unknown``,
    ``unknown_code``, ``min``/``max``/``values``, …) are kept verbatim and
    rendered as a dim note below the inputs.

    The operator control is a :class:`FittingSelect` because this is one of the
    rows ``remount`` rebuilds: the guarded subclass retries Textual's own
    mount-phase lookups, adopts the value it was built with and advertises
    ``options_ready``, which ``ConfigPanel._form_mounting`` reads for every
    ``FittingSelect`` in the form (ODR-0023, ODR-0026).  The recipe editor's and
    the analyze modal's selects are composed once with their screen instead of
    being replaced, so they stay bare.
    """

    class RemoveRequested(Message):
        def __init__(self, row: RuleRow) -> None:
            super().__init__()
            self.row = row

        @property
        def control(self) -> RuleRow:
            return self.row

    def __init__(self, rule: dict[str, Any]) -> None:
        super().__init__(classes="rule-row")
        self.original = dict(rule)
        self.extras = {key: value for key, value in rule.items() if key not in RULE_MODELED_KEYS}

    def on_mount(self) -> None:
        self.mark_form_ready()

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
            yield FittingSelect(options, value=operator, classes="rule-operator",
                                allow_blank=False)
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


class CommandRow(ComposedRows, Vertical):
    """One step of a recipe ``commands`` chain.

    A block's whole vocabulary is ``arguments`` (required, non-empty),
    ``version_args`` and ``version_pattern``: the row starts from a copy of the
    block it was built from and only overwrites what it models, so any key a
    future core adds survives the round-trip untouched.  ``arguments`` is one
    argument per line, mirroring the recipe-level editor.

    The first row is the recipe's *logical owner*: the core requires its program
    to be the tool's executable unless the block declares its own
    ``version_args`` (the recorded tool version and the cache identity's version
    component describe that program).  The title says so.
    """

    class RemoveRequested(Message):
        def __init__(self, row: CommandRow) -> None:
            super().__init__()
            self.row = row

        @property
        def control(self) -> CommandRow:
            return self.row

    def __init__(self, block: dict[str, Any] | None = None, index: int = 1) -> None:
        super().__init__(classes="command-row")
        self.original = dict(block or {})
        self.index = index

    def on_mount(self) -> None:
        self.mark_form_ready()

    def compose(self) -> ComposeResult:
        version_args = self.original.get("version_args")
        with Horizontal(classes="command-inputs"):
            yield Static(self._title(), classes="command-title")
            yield Button("✕", classes="command-remove")
        yield Static("arguments (one per line; required)", classes="modal-label")
        yield TextArea(
            "\n".join(str(arg) for arg in self.original.get("arguments") or []),
            classes="command-arguments",
        )
        yield Input(
            value=(
                ", ".join(str(item) for item in version_args)
                if isinstance(version_args, list) else ""
            ),
            placeholder="version_args (comma separated; blank = inherit the tool's probe)",
            classes="command-version-args",
        )
        yield Input(
            value=str(self.original.get("version_pattern", "") or ""),
            placeholder="version_pattern (regex; requires version_args)",
            classes="command-version-pattern",
        )

    def _title(self) -> str:
        owner = "  (logical owner)" if self.index == 1 else ""
        return f"step {self.index}{owner}"

    def set_index(self, index: int) -> None:
        """Renumber after a removal, so the owner label follows the first row."""
        self.index = index
        if self.form_ready:  # a row still composing has no title to update yet
            self.query_one(".command-title", Static).update(self._title())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("command-remove"):
            event.stop()
            self.post_message(self.RemoveRequested(self))

    def command_document(self) -> dict[str, Any]:
        """Compose the block, preserving original key order and unknown keys."""
        arguments = [
            line.strip()
            for line in self.query_one(".command-arguments", TextArea).text.splitlines()
            if line.strip()
        ]
        version_text = self.query_one(".command-version-args", Input).value.strip()
        version_args = [part.strip() for part in version_text.split(",") if part.strip()]
        version_pattern = self.query_one(".command-version-pattern", Input).value.strip()
        document: dict[str, Any] = {}
        for key, original_value in self.original.items():
            if key == "arguments":
                document[key] = arguments
            elif key == "version_args":
                if version_args:
                    document[key] = version_args
            elif key == "version_pattern":
                if version_pattern:
                    document[key] = version_pattern
            else:
                document[key] = original_value
        if "arguments" not in document:
            document["arguments"] = arguments
        if version_args and "version_args" not in document:
            document["version_args"] = version_args
        if version_pattern and "version_pattern" not in document:
            document["version_pattern"] = version_pattern
        return document


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
                yield Static(yaml.safe_dump(self.document, sort_keys=False, allow_unicode=True))
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
            yield Input(placeholder="profile name, e.g. assembly_strict_v1", id="new-profile-name")
            yield Static("Kind", classes="modal-label")
            yield FittingSelect(
                [("qc (decision thresholds)", "qc"),
                 ("sequence_classification (label sequences)", actions.CLASSIFICATION_KIND),
                 ("taxonomy_coverage (coverage denominators)", actions.COVERAGE_KIND)],
                value="qc", id="new-profile-kind", allow_blank=False,
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
            lambda: actions.save_profile(self.project, self.profile_name, self.document,
                                         known_version=self.new_version - 1)
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
                known_version=self.new_version - 1,
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
    """Config screen: profile editors (qc + classification + coverage) + tools/recipes editor."""

    def __init__(self, project: Project) -> None:
        super().__init__(id="config")
        self.project = project
        self.profiles: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.recipes: list[dict[str, Any]] = []
        self.current_profile: str | None = None
        self.profile_doc: dict[str, Any] | None = None
        self.classification_profile: str | None = None
        self.classification_doc: dict[str, Any] | None = None
        self.coverage_profile: str | None = None
        self.coverage_doc: dict[str, Any] | None = None
        self._known_versions: dict[tuple[str, str], int] = {}
        self.current_recipe: str | None = None
        self.recipe_tool: str | None = None
        self.recipe_doc: dict[str, Any] | None = None
        # The chain the form currently holds.  Rows mount a turn late, so this
        # is the panel's own count and never read back from the DOM.
        self._command_row_count = 0
        # `slurm` keys the form does not model, carried verbatim into the save.
        self._slurm_extras: dict[str, Any] = {}
        self.checking_tools = False

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        # ``# pragma: no branch`` marks below: CPython 3.14's default
        # ``sys.monitoring`` coverage core never reports the with-statement
        # entry/exit arcs of these blocks, although every line inside them
        # executes in the headless UI tests (the same code reports 100% branch
        # coverage under COVERAGE_CORE=ctrace).  The pragma suppresses only
        # those unmeasurable arcs; no line is excluded from measurement.
        with TabbedContent(id="config-tabs"):
            with TabPane("QC Profiles", id="tab-profiles"):
                with Horizontal(id="profiles-layout"):
                    with Vertical(id="profiles-sidebar"):
                        yield ListView(id="profiles-list")
                        with Horizontal(classes="config-buttons"):
                            yield Button("New profile", id="profile-new")
                            yield Button("History", id="profile-history", disabled=True)
                            yield Button("Run classify", id="profile-run", disabled=True)
                    with VerticalScroll(id="profile-editor"):  # pragma: no branch
                        yield Static("select a profile", id="profile-heading")
                        yield Static("Description", classes="modal-label")
                        yield Input(id="profile-description")
                        yield Static("Applies to", classes="modal-label")
                        for entity_type in ENTITY_TYPE_NAMES:
                            yield Checkbox(entity_type, id=f"profile-applies-{entity_type}")
                        yield Static("", id="profile-version-note")
                        yield Static("", id="profile-extras-note")
                        yield Static("Required rules", classes="modal-label")
                        yield MountTracked(id="profile-required-rules")
                        yield Button("add rule", id="profile-add-required")
                        yield Static("Warning rules", classes="modal-label")
                        yield MountTracked(id="profile-warnings-rules")
                        yield Button("add rule", id="profile-add-warnings")
                        with Horizontal(classes="config-buttons"):  # pragma: no branch
                            yield Button("Save profile", id="profile-save",
                                         variant="primary", disabled=True)
                    with VerticalScroll(id="classification-editor"):  # pragma: no branch
                        yield Static("select a profile", id="classification-heading")
                        yield Static("", id="classification-readonly-note")
                        yield Static("Description", classes="modal-label")
                        yield Input(id="classification-description")
                        yield Static("Applies to (entity_type + file_role)", classes="modal-label")
                        yield Input(placeholder="entity_type", id="classification-entity-type")
                        yield Input(placeholder="file_role", id="classification-file-role")
                        yield Static("", id="classification-version-note")
                        yield Static("", id="classification-extras-note")
                        yield Static("Sources (name → analysis, filter, best_by)",
                                     classes="modal-label")
                        yield MountTracked(id="classification-sources")
                        yield Button("add source", id="classification-add-source")
                        yield Static("Rules (first match wins; label + source/when, absent, "
                                     "or default)", classes="modal-label")
                        yield MountTracked(id="classification-rules")
                        yield Button("add rule", id="classification-add-rule")
                        yield Static("", id="classification-save-error")
                        with Horizontal(classes="config-buttons"):  # pragma: no branch
                            yield Button("Save profile", id="classification-save",
                                         variant="primary", disabled=True)
                            yield Button("History", id="classification-history", disabled=True)
                    with VerticalScroll(id="coverage-editor"):  # pragma: no branch
                        yield Static("select a profile", id="coverage-heading")
                        yield Static("", id="coverage-readonly-note")
                        yield Static("Description", classes="modal-label")
                        yield Input(id="coverage-description")
                        yield Static("Taxonomy source", classes="modal-label")
                        yield FittingSelect(
                            [(source, source) for source in ("NCBI",)],
                            value="NCBI", id="coverage-taxonomy-source",
                            allow_blank=False,
                        )
                        yield Static("Root TaxIDs (comma separated)",
                                     classes="modal-label")
                        yield Input(placeholder="e.g. 33090", id="coverage-root-taxids")
                        yield Static("Target ranks", classes="modal-label")
                        yield Checkbox("family", id="coverage-rank-family")
                        yield Checkbox("genus", id="coverage-rank-genus")
                        yield Static("Filters", classes="modal-label")
                        yield Checkbox("exclude extinct taxa",
                                       id="coverage-exclude-extinct")
                        yield Static("Excluded subtrees (comma separated TaxIDs)",
                                     classes="modal-label")
                        yield Input(placeholder="blank = none",
                                    id="coverage-exclude-subtrees")
                        yield Static("Excluded name patterns (one regular expression "
                                     "per line)", classes="modal-label")
                        yield TextArea(id="coverage-exclude-patterns")
                        yield Static("Minimum coverage percent per checked target rank",
                                     classes="modal-label")
                        yield Input(placeholder="family %", id="coverage-threshold-family")
                        yield Input(placeholder="genus %", id="coverage-threshold-genus")
                        yield Static("", id="coverage-version-note")
                        yield Static("", id="coverage-extras-note")
                        yield Static("", id="coverage-save-error")
                        with Horizontal(classes="config-buttons"):  # pragma: no branch
                            yield Button("Save profile", id="coverage-save",
                                         variant="primary", disabled=True)
                            yield Button("History", id="coverage-history", disabled=True)
            with TabPane("Tools && Recipes", id="tab-tools"):
                with Vertical(id="tools-layout"):  # pragma: no branch
                    with Horizontal(classes="config-buttons"):
                        yield Button("Check tools", id="tools-check")
                    yield DataTable(id="tools-table", cursor_type="row")
                    yield Static("Recipes", classes="modal-label")
                    yield DataTable(id="recipes-table", cursor_type="row")
                    with Horizontal(classes="config-buttons"):
                        yield Button("Run analysis", id="recipe-run", disabled=True)
                    with VerticalScroll(id="recipe-editor"):  # pragma: no branch
                        yield Static("select a recipe", id="recipe-heading")
                        yield Static("Description", classes="modal-label")
                        yield Input(id="recipe-description")
                        yield Static("Entity type (blank = *)", classes="modal-label")
                        yield Select(ENTITY_TYPE_OPTIONS, id="recipe-entity-type", allow_blank=True)
                        yield Input(placeholder="file_role", id="recipe-file-role")
                        yield Input(placeholder="file_role_prefix (mutually exclusive with "
                                                "file_role)",
                                    id="recipe-file-role-prefix")
                        yield Input(placeholder="format", id="recipe-format")
                        yield Static("Input kind (blank = key absent)", classes="modal-label")
                        yield Select([(kind, kind) for kind in ARTIFACT_KINDS],
                                     id="recipe-input-kind", allow_blank=True)
                        yield Static("Output kind (blank = key absent)", classes="modal-label")
                        yield Select([(kind, kind) for kind in ARTIFACT_KINDS],
                                     id="recipe-output-kind", allow_blank=True)
                        yield Input(placeholder="database", id="recipe-database")
                        yield Input(placeholder="database_version", id="recipe-database-version")
                        yield Select([(mode, mode) for mode in DATABASE_MODES],
                                     id="recipe-database-mode", allow_blank=True)
                        yield Input(placeholder="database_checksum (sha256 hex; optional)",
                                    id="recipe-database-checksum")
                        yield Static("Environment policy (blank = key absent; core default "
                                     "'warn')", classes="modal-label")
                        yield Select([(policy, policy) for policy in ENVIRONMENT_POLICIES],
                                     id="recipe-environment-policy", allow_blank=True)
                        yield Static("Slurm overrides (optional; merges over "
                                     "execution.slurm — unknown keys preserved)",
                                     classes="modal-label")
                        yield Input(placeholder="slurm partition",
                                    id="recipe-slurm-partition")
                        yield Input(placeholder="slurm time (e.g. 24:00:00)",
                                    id="recipe-slurm-time")
                        yield Input(placeholder="slurm mem_gb", id="recipe-slurm-mem-gb")
                        yield Input(placeholder="slurm poll_interval (seconds)",
                                    id="recipe-slurm-poll-interval")
                        yield Select([("true", "true"), ("false", "false")],
                                     id="recipe-slurm-array", allow_blank=True)
                        yield Input(placeholder="slurm array_concurrency",
                                    id="recipe-slurm-array-concurrency")
                        yield Static("slurm extra_sbatch (one sbatch flag per line)",
                                     classes="modal-label")
                        yield TextArea(id="recipe-slurm-extra-sbatch")
                        yield Static("slurm setup_commands (one command per line)",
                                     classes="modal-label")
                        yield TextArea(id="recipe-slurm-setup-commands")
                        yield Static("", id="recipe-slurm-note")
                        yield Input(placeholder="output_subdir", id="recipe-output-subdir")
                        yield Input(placeholder="output_suffix", id="recipe-output-suffix")
                        yield Input(placeholder="output_name (template, e.g. "
                                               "${file_id}.out)", id="recipe-output-name")
                        yield Static("Arguments (one per line; ${placeholders} stay as-is)",
                                     classes="modal-label")
                        yield TextArea(id="recipe-arguments")
                        yield Static("Runtime parameters (name=default per line; other spec "
                                     "keys preserved)", classes="modal-label")
                        yield TextArea(id="recipe-parameters")
                        yield Static("", id="recipe-parameters-note")
                        yield Static("Commands chain (optional; mutually exclusive with "
                                     "arguments — every step runs in the parent tool's single "
                                     "run_method environment)", classes="modal-label")
                        yield MountTracked(id="recipe-command-list")
                        with Horizontal(classes="config-buttons"):
                            yield Button("Add step", id="recipe-add-command")
                        yield Static("", id="recipe-command-note")
                        yield Static("Result parser", classes="modal-label")
                        yield Select([(parser, parser) for parser in RESULT_PARSERS],
                                     value="none", id="recipe-result-parser", allow_blank=False)
                        yield Input(placeholder="result_glob", id="recipe-result-glob")
                        yield Static("HMMER mode (blank = key absent)", classes="modal-label")
                        yield Select([(mode, mode) for mode in HMMER_MODES],
                                     id="recipe-hmmer-mode", allow_blank=True)
                        yield Input(placeholder="result_columns (comma separated)",
                                    id="recipe-result-columns")
                        yield Input(placeholder="hit_metric_columns (comma separated)",
                                    id="recipe-hit-metric-columns")
                        yield Static("Column mapping (blank = parser defaults)",
                                     classes="modal-label")
                        yield Input(placeholder="query_column", id="recipe-query-column")
                        yield Input(placeholder="subject_column", id="recipe-subject-column")
                        yield Input(placeholder="numeric_columns (comma separated)",
                                    id="recipe-numeric-columns")
                        yield Input(placeholder="qstart_column", id="recipe-qstart-column")
                        yield Input(placeholder="qend_column", id="recipe-qend-column")
                        yield Input(placeholder="sstart_column", id="recipe-sstart-column")
                        yield Input(placeholder="send_column", id="recipe-send-column")
                        yield Input(placeholder="evalue_column", id="recipe-evalue-column")
                        yield Input(placeholder="bitscore_column", id="recipe-bitscore-column")
                        yield Input(placeholder="pident_column", id="recipe-pident-column")
                        yield Input(placeholder="max_hits_per_query (blank restores default: 5)",
                                    id="recipe-max-hits")
                        yield Static("", id="recipe-extras-note")
                        yield Static("", id="recipe-save-error")
                        with Horizontal(classes="config-buttons"):  # pragma: no branch
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
            "profiles": (
                data.list_qc_profiles(self.project)
                + data.list_classification_profiles(self.project)
                + data.list_coverage_profiles(self.project)
            ),
            "tools": data.list_tools(self.project),
            "recipes": data.list_recipes(self.project),
        }

    def render_data(self, payload: dict[str, Any]) -> None:
        self.profiles = payload["profiles"]
        self.tools = payload["tools"]
        self.recipes = payload["recipes"]

        list_view = self.query_one("#profiles-list", ListView)
        list_view.clear()
        tags = {
            actions.CLASSIFICATION_KIND: "classification",
            actions.COVERAGE_KIND: "coverage",
        }
        for profile in self.profiles:
            tag = tags.get(str(profile.get("kind") or "qc"), "")
            suffix = f"  · {tag}" if tag else ""
            list_view.append(ListItem(Label(f"{profile['name']}  v{profile['version']}{suffix}")))

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
        self._show_editor("qc")
        self.query_one("#profile-run", Button).disabled = True
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
            remount(container, *[
                RuleRow(rule) for rule in document.get(section, []) or []
                if isinstance(rule, dict)
            ])
        self.query_one("#profile-save", Button).disabled = False
        self.query_one("#profile-history", Button).disabled = False

    def _load_profile(self, name: str) -> None:
        kind = self._profile_kind(name)
        try:
            document = data.get_profile_document(self.project, name, kind=kind)
        except ValidationError as exc:
            self.app.notify(str(exc), severity="error")
            return
        self._remember_version("profile", name, document)
        self._render_profile_document(name, document)

    def _profile_kind(self, name: str) -> str:
        """The on-disk kind of ``name`` (from the last listing; qc when unknown)."""
        for profile in self.profiles:
            if profile.get("name") == name:
                return str(profile.get("kind") or "qc")
        return "qc"

    def _render_profile_document(self, name: str, document: dict[str, Any],
                                 note: str = "") -> None:
        """Route a document to the editor its kind needs."""
        document_kind = str(document.get("kind", "qc"))
        if document_kind == actions.CLASSIFICATION_KIND:
            self.classification_profile = name
            self.classification_doc = dict(document)
            self._render_classification_form(name, dict(document), note)
        elif document_kind == actions.COVERAGE_KIND:
            self.coverage_profile = name
            self.coverage_doc = dict(document)
            self._render_coverage_form(name, dict(document), note)
        else:
            self.current_profile = name
            self.profile_doc = dict(document)
            self._render_profile_form(name, document, note)

    def _show_editor(self, kind: str) -> None:
        self.query_one("#profile-editor").display = kind == "qc"
        self.query_one("#classification-editor").display = kind == actions.CLASSIFICATION_KIND
        self.query_one("#coverage-editor").display = kind == actions.COVERAGE_KIND

    def _remember_version(self, kind: str, name: str, document: dict[str, Any]) -> None:
        key = (kind, name)
        self._known_versions[key] = max(
            self._known_versions.get(key, 0), int(document.get("version", 1)),
        )

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

    def _profile_file_version(self, name: str, kind: str = "qc") -> int | None:
        try:
            version = int(data.get_profile_document(self.project, name, kind=kind).get("version", 1))
        except ValidationError:
            version = 0
        version = max(version, self._known_versions.get(("profile", name), 0))
        return data.config_version_floor(self.project, "profile", name, version) or None

    # -- classification-profile editor --------------------------------------

    def _source_names(self) -> list[str]:
        """Names of the source rows, skipping a row that has not composed yet."""
        names: list[str] = []
        for row in self.query(".source-row").results(SourceRow):
            try:
                name = row.query_one(".source-name", Input).value.strip()
            except NoMatches:
                continue
            if name:
                names.append(name)
        return names

    def _refresh_rule_sources(self) -> None:
        names = self._source_names()
        for row in self.query(".classrule-row").results(ClassificationRuleRow):
            row.set_sources(names)

    def _render_classification_form(self, name: str, document: dict[str, Any],
                                    note: str = "") -> None:
        self._show_editor(actions.CLASSIFICATION_KIND)
        supported, reason = classification_form_supported(document)
        heading = f"{name}" + (f"  —  {note}" if note else "")
        self.query_one("#classification-heading", Static).update(heading)
        self.query_one("#classification-readonly-note", Static).update(
            Text(
                f"structure exceeds the manual form: {reason} — edit the YAML file; saving "
                "from here is disabled and the file is never rewritten by the form",
                style="yellow",
            ) if not supported else ""
        )
        self.query_one("#classification-description", Input).value = str(
            document.get("description", ""))
        applies_to = document.get("applies_to")
        applies_to = applies_to if isinstance(applies_to, dict) else {}
        self.query_one("#classification-entity-type", Input).value = str(
            applies_to.get("entity_type", "") or "")
        self.query_one("#classification-file-role", Input).value = str(
            applies_to.get("file_role", "") or "")
        version = int(document.get("version", 1))
        self.query_one("#classification-version-note", Static).update(Text(
            f"version {version} — saving writes the next version and records a snapshot "
            "that a classify run consumes",
            style="dim",
        ))
        extras = {key: value for key, value in document.items() if key not in CLASSIFICATION_MODELED_KEYS}
        self.query_one("#classification-extras-note", Static).update(
            Text(_extras_note(extras), style="dim") if extras else ""
        )
        sources = document.get("sources")
        sources = sources if isinstance(sources, dict) else {}
        source_container = self.query_one("#classification-sources", Vertical)
        remount(source_container, *[
            SourceRow(str(source_name), source)
            for source_name, source in sources.items() if isinstance(source, dict)
        ])
        rules = document.get("rules")
        rules = [rule for rule in rules if isinstance(rule, dict)] if isinstance(rules, list) else []
        rules_container = self.query_one("#classification-rules", Vertical)
        names = [str(source_name) for source_name in sources]
        remount(rules_container, *[ClassificationRuleRow(rule, names) for rule in rules])
        self.query_one("#classification-save-error", Static).update("")
        self.query_one("#classification-save", Button).disabled = not supported
        self.query_one("#classification-history", Button).disabled = False
        # Running reads the profile from disk, so a not-yet-saved (or
        # read-only-nested) profile can still be run once its file exists.
        self.query_one("#profile-run", Button).disabled = not (
            self.project.profiles_dir / f"{name}.yaml"
        ).exists()

    def _compose_classification_document(self) -> dict[str, Any]:
        original = self.classification_doc or {}
        sources: dict[str, Any] = {}
        for row in self.query(".source-row").results(SourceRow):
            name, source = row.source_document()
            if name:
                sources[name] = source
        document: dict[str, Any] = {
            "kind": actions.CLASSIFICATION_KIND,
            "version": int(original.get("version", 1)),
            "description": self.query_one("#classification-description", Input).value.strip(),
            "applies_to": {
                "entity_type": self.query_one("#classification-entity-type", Input).value.strip(),
                "file_role": self.query_one("#classification-file-role", Input).value.strip(),
            },
            "sources": sources,
            "rules": [
                row.rule_document()
                for row in self.query(".classrule-row").results(ClassificationRuleRow)
            ],
        }
        for key, value in original.items():
            if key not in document:
                document[key] = value
        return document

    def _start_classification_save(self) -> None:
        if not self.classification_profile or self.classification_doc is None:
            return
        error = self.query_one("#classification-save-error", Static)
        names = self._source_names()
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            error.update(Text(
                f"duplicate source name(s): {', '.join(duplicates)} — source names are the "
                "mapping keys and must be unique",
                style="red",
            ))
            return
        error.update("")
        name = self.classification_profile
        document = self._form_document(self._compose_classification_document, error)
        if document is None:
            return
        file_version = self._profile_file_version(name, actions.CLASSIFICATION_KIND)
        new_version = 1 if file_version is None else file_version + 1
        self.app.push_screen(
            ClassificationSaveModal(self.project, name, document, new_version),
            self._on_classification_saved,
        )

    def _on_classification_saved(self, payload: Any) -> None:
        if not payload:
            return
        self.reload()
        name = self.classification_profile
        if name:
            self._load_profile(name)

    # -- coverage-profile editor ---------------------------------------------

    def _render_coverage_form(self, name: str, document: dict[str, Any],
                              note: str = "") -> None:
        self._show_editor(actions.COVERAGE_KIND)
        self.query_one("#profile-run", Button).disabled = True
        supported, reason = coverage_form_supported(document)
        self.query_one("#coverage-heading", Static).update(
            f"{name}" + (f"  —  {note}" if note else "")
        )
        self.query_one("#coverage-readonly-note", Static).update(
            Text(
                f"structure exceeds the manual form: {reason} — edit the YAML file; "
                "saving from here is disabled and the file is never rewritten by the form",
                style="yellow",
            ) if not supported else ""
        )
        self.query_one("#coverage-description", Input).value = str(
            document.get("description", ""))
        taxonomy = document.get("taxonomy")
        taxonomy = taxonomy if isinstance(taxonomy, dict) else {}
        source = str(taxonomy.get("source") or "NCBI")
        source_select = self.query_one("#coverage-taxonomy-source", Select)
        if source != "NCBI":
            source_select.set_options(
                [("NCBI", "NCBI"), (f"{source} (preserved)", source)]
            )
        source_select.value = source
        scope = document.get("scope")
        scope = scope if isinstance(scope, dict) else {}
        roots = scope.get("root_taxids")
        roots = [str(item) for item in roots] if isinstance(roots, list) else []
        self.query_one("#coverage-root-taxids", Input).value = ", ".join(roots)
        targets = document.get("targets")
        targets = targets if isinstance(targets, dict) else {}
        ranks = {str(rank).lower() for rank in targets.get("ranks") or []}
        for rank in COVERAGE_RANKS:
            checkbox = self.query_one(f"#coverage-rank-{rank}", Checkbox)
            checkbox.value = rank in ranks
            self.query_one(f"#coverage-threshold-{rank}", Input).disabled = (
                rank not in ranks)
        filters = document.get("filters")
        filters = filters if isinstance(filters, dict) else {}
        self.query_one("#coverage-exclude-extinct", Checkbox).value = bool(
            filters.get("exclude_extinct", False))
        subtrees = filters.get("exclude_subtrees")
        subtrees = [str(item) for item in subtrees] if isinstance(subtrees, list) else []
        self.query_one("#coverage-exclude-subtrees", Input).value = ", ".join(subtrees)
        patterns = filters.get("exclude_name_patterns")
        patterns = [str(item) for item in patterns] if isinstance(patterns, list) else []
        self.query_one("#coverage-exclude-patterns", TextArea).text = "\n".join(patterns)
        thresholds = document.get("thresholds")
        thresholds = thresholds if isinstance(thresholds, dict) else {}
        for rank in COVERAGE_RANKS:
            entry = thresholds.get(rank)
            entry = entry if isinstance(entry, dict) else {}
            value = entry.get("min_coverage_percent")
            self.query_one(f"#coverage-threshold-{rank}", Input).value = (
                "" if value is None else str(value))
        version = int(document.get("version", 1))
        self.query_one("#coverage-version-note", Static).update(Text(
            f"version {version} — saving writes the next version and records a "
            "snapshot that a taxonomy compile consumes",
            style="dim",
        ))
        extras = {key: value for key, value in document.items()
                  if key not in COVERAGE_MODELED_KEYS}
        self.query_one("#coverage-extras-note", Static).update(
            Text(_extras_note(extras), style="dim") if extras else ""
        )
        self.query_one("#coverage-save-error", Static).update("")
        self.query_one("#coverage-save", Button).disabled = not supported
        self.query_one("#coverage-history", Button).disabled = False

    def _sync_coverage_thresholds(self) -> None:
        for rank in COVERAGE_RANKS:
            self.query_one(f"#coverage-threshold-{rank}", Input).disabled = (
                not self.query_one(f"#coverage-rank-{rank}", Checkbox).value)

    def _compose_coverage_document(self) -> dict[str, Any]:
        original = self.coverage_doc or {}
        document: dict[str, Any] = {
            "kind": actions.COVERAGE_KIND,
            "version": int(original.get("version", 1)),
            "description": self.query_one("#coverage-description", Input).value.strip(),
        }

        def section(name: str) -> dict[str, Any]:
            value = original.get(name)
            if not isinstance(value, dict):
                return {}
            return {
                key: val for key, val in value.items()
                if key not in SECTION_MODELED_KEYS[name]
            }

        original_filters = original.get("filters")
        original_filters = original_filters if isinstance(original_filters, dict) else {}

        taxonomy = section("taxonomy")
        source_value = self.query_one("#coverage-taxonomy-source", Select).value
        taxonomy["source"] = "NCBI" if source_value is Select.NULL else str(source_value)
        document["taxonomy"] = taxonomy

        scope = section("scope")
        roots = [
            actions.coerce_scalar(part)
            for part in self.query_one("#coverage-root-taxids", Input).value.split(",")
            if part.strip()
        ]
        original_scope = original.get("scope")
        if roots or (isinstance(original_scope, dict) and "root_taxids" in original_scope):
            scope["root_taxids"] = roots
        document["scope"] = scope

        ranks = [
            rank for rank in COVERAGE_RANKS
            if self.query_one(f"#coverage-rank-{rank}", Checkbox).value
        ]
        targets = section("targets")
        original_targets = original.get("targets")
        if ranks or (isinstance(original_targets, dict)
                     and "ranks" in original_targets):
            targets["ranks"] = ranks
        document["targets"] = targets

        filters = section("filters")
        exclude_extinct = self.query_one("#coverage-exclude-extinct", Checkbox).value
        if exclude_extinct or "exclude_extinct" in original_filters:
            filters["exclude_extinct"] = exclude_extinct
        subtrees = [
            actions.coerce_scalar(part)
            for part in self.query_one("#coverage-exclude-subtrees", Input).value.split(",")
            if part.strip()
        ]
        if subtrees or "exclude_subtrees" in original_filters:
            filters["exclude_subtrees"] = subtrees
        patterns = [
            line.strip()
            for line in self.query_one("#coverage-exclude-patterns", TextArea).text.splitlines()
            if line.strip()
        ]
        if patterns or "exclude_name_patterns" in original_filters:
            filters["exclude_name_patterns"] = patterns
        document["filters"] = filters

        original_thresholds = original.get("thresholds")
        original_thresholds = (
            original_thresholds if isinstance(original_thresholds, dict) else {})
        thresholds: dict[str, Any] = {}
        for rank in COVERAGE_RANKS:
            if rank not in ranks:
                continue
            entry: dict[str, Any] = {}
            original_entry = original_thresholds.get(rank)
            if isinstance(original_entry, dict):
                entry.update({
                    key: value for key, value in original_entry.items()
                    if key not in THRESHOLD_MODELED_KEYS
                })
            text = self.query_one(f"#coverage-threshold-{rank}", Input).value.strip()
            if text:
                entry["min_coverage_percent"] = actions.coerce_scalar(text)
            thresholds[rank] = entry
        document["thresholds"] = thresholds

        for key, value in original.items():
            if key not in document:
                document[key] = value
        return document

    def _start_coverage_save(self) -> None:
        if not self.coverage_profile or self.coverage_doc is None:
            return
        error = self.query_one("#coverage-save-error", Static)
        ranks = [
            rank for rank in COVERAGE_RANKS
            if self.query_one(f"#coverage-rank-{rank}", Checkbox).value
        ]
        if not ranks:
            error.update(Text("at least one target rank is required", style="red"))
            return
        missing = [
            rank for rank in ranks
            if not self.query_one(f"#coverage-threshold-{rank}", Input).value.strip()
        ]
        if missing:
            error.update(Text(
                f"missing minimum coverage for target rank(s): {', '.join(missing)}",
                style="red",
            ))
            return
        for part in self.query_one("#coverage-root-taxids", Input).value.split(","):
            part = part.strip()
            if part and not part.isdigit():
                error.update(Text(
                    f"root TaxIDs must be positive integers; got {part!r}", style="red"))
                return
        for part in self.query_one("#coverage-exclude-subtrees", Input).value.split(","):
            part = part.strip()
            if part and not part.isdigit():
                error.update(Text(
                    f"excluded subtrees must be positive integers; got {part!r}",
                    style="red",
                ))
                return
        error.update("")
        name = self.coverage_profile
        document = self._form_document(self._compose_coverage_document, error)
        if document is None:
            return
        file_version = self._profile_file_version(name, actions.COVERAGE_KIND)
        new_version = 1 if file_version is None else file_version + 1
        self.app.push_screen(
            CoverageSaveModal(self.project, name, document, new_version),
            self._on_coverage_saved,
        )

    def _on_coverage_saved(self, payload: Any) -> None:
        if not payload:
            return
        self.reload()
        name = self.coverage_profile
        if name:
            self._load_profile(name)

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
        self.query_one("#recipe-file-role-prefix", Input).value = str(
            document.get("file_role_prefix", "") or "")
        self.query_one("#recipe-format", Input).value = str(document.get("format", "") or "")
        for key, widget_id, options in (
                ("input_kind", "#recipe-input-kind", ARTIFACT_KINDS),
                ("output_kind", "#recipe-output-kind", ARTIFACT_KINDS),
                ("environment_policy", "#recipe-environment-policy", ENVIRONMENT_POLICIES),
                ("hmmer_mode", "#recipe-hmmer-mode", HMMER_MODES),
                ("database_mode", "#recipe-database-mode", DATABASE_MODES),
        ):
            value = str(document.get(key, "") or "")
            select = self.query_one(widget_id, Select)
            if value and value not in options:
                select.set_options(
                    [(option, option) for option in options]
                    + [(f"{value} (preserved)", value)]
                )
            select.value = value if value else Select.NULL
        self.query_one("#recipe-database", Input).value = str(document.get("database", "") or "")
        self.query_one("#recipe-database-version", Input).value = str(
            document.get("database_version", "") or "")
        self.query_one("#recipe-database-checksum", Input).value = str(
            document.get("database_checksum", "") or "")
        self.query_one("#recipe-output-subdir", Input).value = str(
            document.get("output_subdir", "") or "")
        self.query_one("#recipe-output-suffix", Input).value = str(
            document.get("output_suffix", "") or "")
        self.query_one("#recipe-output-name", Input).value = str(
            document.get("output_name", "") or "")
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
        self._render_command_rows(document.get("commands"))
        self._render_slurm_fields(document.get("slurm"))
        parser = str(document.get("result_parser", "none") or "none")
        parser_select = self.query_one("#recipe-result-parser", Select)
        if parser not in RESULT_PARSERS:
            parser_select.set_options(
                [(p, p) for p in RESULT_PARSERS] + [(f"{parser} (preserved)", parser)]
            )
        parser_select.value = parser
        for key, widget_id in (("result_glob", "#recipe-result-glob"),
                               ("query_column", "#recipe-query-column"),
                               ("subject_column", "#recipe-subject-column"),
                               ("qstart_column", "#recipe-qstart-column"),
                               ("qend_column", "#recipe-qend-column"),
                               ("sstart_column", "#recipe-sstart-column"),
                               ("send_column", "#recipe-send-column"),
                               ("evalue_column", "#recipe-evalue-column"),
                               ("bitscore_column", "#recipe-bitscore-column"),
                               ("pident_column", "#recipe-pident-column")):
            self.query_one(widget_id, Input).value = str(document.get(key, "") or "")
        for key, widget_id in (("result_columns", "#recipe-result-columns"),
                               ("hit_metric_columns", "#recipe-hit-metric-columns"),
                               ("numeric_columns", "#recipe-numeric-columns")):
            columns = document.get(key, []) or []
            self.query_one(widget_id, Input).value = ", ".join(str(c) for c in columns)
        max_hits = document.get("max_hits_per_query")
        self.query_one("#recipe-max-hits", Input).value = "" if max_hits is None else str(max_hits)
        self.query_one("#recipe-save-error", Static).update("")
        extras = {key: value for key, value in document.items() if key not in RECIPE_MODELED_KEYS}
        self.query_one("#recipe-extras-note", Static).update(
            Text(_extras_note(extras), style="dim") if extras else ""
        )
        self.query_one("#recipe-save", Button).disabled = False
        self.query_one("#recipe-history", Button).disabled = False
        self.query_one("#recipe-run", Button).disabled = False

    def _render_slurm_fields(self, slurm: Any) -> None:
        """Fill the slurm overrides, keeping every key the form does not model."""
        data = slurm if isinstance(slurm, dict) else {}
        modeled = {name for name, _, _ in RECIPE_SLURM_FIELDS}
        self._slurm_extras = {key: value for key, value in data.items()
                             if key not in modeled}
        for key, widget_id, kind in RECIPE_SLURM_FIELDS:
            value = data.get(key)
            if kind == "lines":
                self.query_one(widget_id, TextArea).text = (
                    "\n".join(str(item) for item in value)
                    if isinstance(value, list) else "")
            elif kind == "tri":
                self.query_one(widget_id, Select).value = (
                    Select.NULL if value is None else ("true" if value else "false"))
            else:
                self.query_one(widget_id, Input).value = (
                    "" if value is None else str(value))
        note = self.query_one("#recipe-slurm-note", Static)
        note.update(
            Text("preserved slurm keys — " + ", ".join(sorted(self._slurm_extras)),
                 style="dim")
            if self._slurm_extras else ""
        )

    def _compose_slurm_document(self) -> dict[str, Any]:
        """The `slurm` block: modeled keys when set, unmodeled keys verbatim.

        A number the widget cannot parse is handed over as text on purpose —
        `_start_recipe_save` refuses it inline with the core's own wording
        rather than letting `load_slurm_config` trip over it at run time.
        """
        document: dict[str, Any] = dict(self._slurm_extras)
        for key, widget_id, kind in RECIPE_SLURM_FIELDS:
            if kind == "lines":
                lines = [line.strip()
                         for line in self.query_one(widget_id, TextArea).text.splitlines()
                         if line.strip()]
                if lines:
                    document[key] = lines
                continue
            if kind == "tri":
                value = self.query_one(widget_id, Select).value
                if value is not Select.NULL:
                    document[key] = str(value) == "true"
                continue
            text = self.query_one(widget_id, Input).value.strip()
            if not text:
                continue
            for kind_type, convert in (("int", int), ("float", float)):
                if kind == kind_type:
                    try:
                        document[key] = convert(text)
                    except ValueError:
                        document[key] = text
                    break
            else:
                document[key] = text
        return document

    def _render_command_rows(self, blocks: Any) -> None:
        """Rebuild the commands rows from a document (a new recipe, a snapshot)."""
        rows = [
            CommandRow(block, index)
            for index, block in enumerate(blocks if isinstance(blocks, list) else [], start=1)
            if isinstance(block, dict)
        ]
        remount(self.query_one("#recipe-command-list", MountTracked), *rows)
        # The panel counts the chain itself: the replacement lands a turn later,
        # so neither the note nor a reader may trust the DOM for it.
        self._command_row_count = len(rows)
        self._refresh_command_note()

    def _renumber_command_rows(self) -> None:
        for index, row in enumerate(self.query(CommandRow).results(CommandRow), start=1):
            row.set_index(index)

    def _refresh_command_note(self) -> None:
        """Say what the chain means, and shout when ``arguments`` is also set."""
        row_count = self._command_row_count
        arguments = self.query_one("#recipe-arguments", TextArea).text.strip()
        note_view = self.query_one("#recipe-command-note", Static)
        if not row_count:
            note_view.update("")
            return
        if arguments:
            note_view.update(Text(
                f"'commands' and 'arguments' are mutually exclusive — clear one "
                f"before saving ({row_count} step(s) defined)",
                style="yellow",
            ))
            return
        note_view.update(Text(
            f"{row_count} step(s); every step runs in the parent tool's single "
            "run_method environment",
            style="dim",
        ))

    def _load_recipe(self, name: str) -> None:
        try:
            info = data.get_recipe_document(self.project, name)
        except ValidationError as exc:
            self.app.notify(str(exc), severity="error")
            return
        self.current_recipe = name
        self.recipe_tool = info["tool"]
        self.recipe_doc = info["document"]
        self._remember_version("recipe", name, info["document"])
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
        numeric_columns = [
            column.strip()
            for column in self.query_one("#recipe-numeric-columns", Input).value.split(",")
            if column.strip()
        ]
        max_hits_text = self.query_one("#recipe-max-hits", Input).value.strip()
        max_hits: Any = _OMIT
        if max_hits_text:
            try:
                max_hits = int(max_hits_text)
            except ValueError:
                max_hits = max_hits_text  # save_recipe round-trip rejects with a clear error

        new_values: dict[str, Any] = {}
        for key, widget_id in (("description", "#recipe-description"),
                               ("file_role", "#recipe-file-role"),
                               ("file_role_prefix", "#recipe-file-role-prefix"),
                               ("format", "#recipe-format"),
                               ("database", "#recipe-database"),
                               ("database_version", "#recipe-database-version"),
                               ("database_checksum", "#recipe-database-checksum"),
                               ("output_subdir", "#recipe-output-subdir"),
                               ("output_suffix", "#recipe-output-suffix"),
                               ("output_name", "#recipe-output-name"),
                               ("result_glob", "#recipe-result-glob"),
                               ("query_column", "#recipe-query-column"),
                               ("subject_column", "#recipe-subject-column"),
                               ("qstart_column", "#recipe-qstart-column"),
                               ("qend_column", "#recipe-qend-column"),
                               ("sstart_column", "#recipe-sstart-column"),
                               ("send_column", "#recipe-send-column"),
                               ("evalue_column", "#recipe-evalue-column"),
                               ("bitscore_column", "#recipe-bitscore-column"),
                               ("pident_column", "#recipe-pident-column")):
            value = self.query_one(widget_id, Input).value.strip()
            new_values[key] = value if value or key in original else _OMIT
        for key, widget_id in (("input_kind", "#recipe-input-kind"),
                               ("output_kind", "#recipe-output-kind"),
                               ("environment_policy", "#recipe-environment-policy"),
                               ("hmmer_mode", "#recipe-hmmer-mode"),
                               ("database_mode", "#recipe-database-mode")):
            select_value = self.query_one(widget_id, Select).value
            text = "" if select_value is Select.NULL else str(select_value)
            new_values[key] = text if text or key in original else _OMIT
        new_values["entity_type"] = (
            entity_type if entity_type or "entity_type" in original else _OMIT
        )
        new_values["arguments"] = arguments if arguments or "arguments" in original else _OMIT
        commands = [row.command_document() for row in self.query(CommandRow).results(CommandRow)]
        new_values["commands"] = commands if commands or "commands" in original else _OMIT
        slurm_values = self._compose_slurm_document()
        new_values["slurm"] = (
            slurm_values if slurm_values or "slurm" in original else _OMIT
        )
        new_values["parameters"] = parameters if parameters or "parameters" in original else _OMIT
        new_values["result_parser"] = parser
        new_values["result_columns"] = (
            columns if columns or "result_columns" in original else _OMIT
        )
        new_values["hit_metric_columns"] = (
            hit_columns if hit_columns or "hit_metric_columns" in original else _OMIT
        )
        new_values["numeric_columns"] = (
            numeric_columns if numeric_columns or "numeric_columns" in original else _OMIT
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
            version = int(data.get_recipe_document(self.project, name)["document"].get("version", 1))
        except ValidationError:
            version = 0
        version = max(version, self._known_versions.get(("recipe", name), 0))
        return data.config_version_floor(self.project, "recipe", name, version) or None

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
            self.post_to_ui(self._apply_tool_result, entry)

        try:
            payload: Any = actions.check_tools(self.project, on_result=on_result)
        except Exception as exc:  # noqa: BLE001 - routed to _tools_check_done  # pylint: disable=broad-exception-caught
            payload = exc
        self.post_to_ui(self._tools_check_done, payload)

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

    async def on_command_row_remove_requested(self, event: CommandRow.RemoveRequested) -> None:
        event.stop()
        # Removal is deferred, so renumber and re-count only once it has landed.
        await event.row.remove()
        self._command_row_count = max(0, self._command_row_count - 1)
        self._renumber_command_rows()
        self._refresh_command_note()

    def on_source_row_remove_requested(self, event: SourceRow.RemoveRequested) -> None:
        event.stop()
        event.row.remove()
        self._refresh_rule_sources()

    def on_classification_rule_row_remove_requested(
            self, event: ClassificationRuleRow.RemoveRequested) -> None:
        event.stop()
        event.row.remove()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "recipe-arguments":
            self._refresh_command_note()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.has_class("source-name"):
            # Rule rows pick their source from the declared names.
            self._refresh_rule_sources()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id in {"coverage-rank-family", "coverage-rank-genus"}:
            self._sync_coverage_thresholds()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "profile-add-required":
            self.query_one("#profile-required-rules", MountTracked).mount_later(
                RuleRow({"metric": "", "operator": ">=", "value": "", "code": ""}),
                when_present=".rule-row",
            )
        elif button_id == "profile-add-warnings":
            self.query_one("#profile-warnings-rules", MountTracked).mount_later(
                RuleRow({"metric": "", "operator": ">", "value": "", "code": ""}),
                when_present=".rule-row",
            )
        elif button_id == "recipe-add-command":
            container = self.query_one("#recipe-command-list", MountTracked)
            index = self._command_row_count + 1
            container.mount_later(CommandRow({"arguments": []}, index),
                                  when_present=".command-row")
            self._command_row_count = index
            self._refresh_command_note()
        elif button_id == "profile-save":
            self._start_profile_save()
        elif button_id == "profile-history":
            self._open_profile_history()
        elif button_id == "profile-new":
            self.app.push_screen(NewProfileModal(), self._on_new_profile)
        elif button_id == "classification-add-source":
            self.query_one("#classification-sources", MountTracked).mount_later(
                SourceRow("", {"analysis": "", "filter": []}), when_present=".source-row",
            )
        elif button_id == "classification-add-rule":
            self.query_one("#classification-rules", MountTracked).mount_later(
                ClassificationRuleRow({"label": "", "source": "", "when": []},
                                      self._source_names()),
                when_present=".classrule-row",
            )
        elif button_id == "classification-save":
            self._start_classification_save()
        elif button_id == "classification-history":
            self._open_profile_history()
        elif button_id == "coverage-save":
            self._start_coverage_save()
        elif button_id == "coverage-history":
            self._open_profile_history()
        elif button_id == "profile-run":
            self._open_classify()
        elif button_id == "recipe-save":
            self._start_recipe_save()
        elif button_id == "recipe-history":
            self._open_recipe_history()
        elif button_id == "recipe-run":
            self._open_run_analysis()
        elif button_id == "tools-check":
            self._start_tools_check()

    # -- flow starters ------------------------------------------------------

    #: Shown when a save arrives before a deferred form rebuild has composed.
    FORM_MOUNTING_MESSAGE = "the form is still loading — save again in a moment"
    #: Answered when a control ran out of mount retries: waiting cannot help it, so
    #: the message names the controls and sends the reader to a reload (ODR-0039).
    FORM_STALLED_MESSAGE = (
        "a dropdown in this form never finished loading — reload the profile (r) "
        "and try again"
    )

    #: Rows whose own composed subtree must be in the tree before a read.
    EDITOR_ROW_SELECTORS = (
        ".rule-row, .source-row, .classrule-row, .bestby-row, .condition-row, "
        ".condition-editor, .command-row"
    )

    def _form_mounting(self) -> bool:
        """True while a deferred editor rebuild has not composed its rows yet.

        The panel replaces a whole editor with ``remount``: the replacement rows
        mount a message-loop turn later, each row mounts its own nested rows — a
        source's filter editors, a rule's when-conditions, a condition editor's
        body — a turn after that, and a ``Select`` inside those rows is queried
        by Textual while it mounts.  Reading the form inside that window walks
        half-built rows; the composition then either raises ``NoMatches`` or,
        worse, returns a document with the missing rows silently dropped, which
        is why a save refuses instead (ODR-0023).  Two signals cover it: every
        container that fills itself later says so through :class:`MountTracked`,
        and every row latches :attr:`ComposedRows.form_ready` in its own
        ``on_mount`` — which Textual runs only once the row's whole subtree
        (inputs and their children) is in the tree.  A third signal covers the
        one control that can still be half-alive inside a composed row: a
        ``Select`` whose own mount lookup failed reports
        :attr:`FittingSelect.options_ready` only once it has adopted its value
        (ODR-0026).
        """
        for container in self.query(".mount-tracked").results(MountTracked):
            if not container.mounts_settled:
                return True
        for row in self.query(self.EDITOR_ROW_SELECTORS).results(ComposedRows):
            if not row.form_ready:
                return True
        for select in self.query(FittingSelect):
            if not select.options_ready:
                return True
        return False

    def _form_document(
        self,
        compose: Callable[[], dict[str, Any]],
        error_view: Static | None = None,
    ) -> dict[str, Any] | None:
        """Compose an editor document, or report a form that is still mounting.

        A save is answered with :attr:`FORM_MOUNTING_MESSAGE` while
        :meth:`_form_mounting` holds, so the composition never runs on a tree
        that is missing rows — and a genuine selector problem stays loud
        instead of hiding behind the message (the end-to-end save tests would
        catch it).
        """
        if self._form_mounting():
            message = self._blocked_save_message()
            if error_view is None:
                self.app.notify(message, severity="warning")
            else:
                error_view.update(Text(message, style="yellow"))
            return None
        return compose()

    def _blocked_save_message(self) -> str:
        """What a refused save says: waiting, or a control that will never come.

        A select whose mount retries ran out blocks the form exactly like one that is
        still mounting, and no amount of waiting helps it — so name those controls and
        send the reader to a reload instead of telling them to try again (ODR-0039).
        """
        stalled = [
            str(select.id or type(select).__name__)
            for select in self.query(FittingSelect)
            if select.options_gave_up
        ]
        if not stalled:
            return self.FORM_MOUNTING_MESSAGE
        return f"{self.FORM_STALLED_MESSAGE}: {', '.join(stalled)}"

    def _on_new_profile(self, payload: Any) -> None:
        if not payload:
            return
        name = str(payload["name"])
        kind = str(payload.get("kind") or "qc")
        if (self.project.profiles_dir / f"{name}.yaml").exists():
            self.app.notify(
                f"profile {name!r} already exists — opening it instead", severity="warning",
            )
            self._load_profile(name)
            return
        if kind == actions.CLASSIFICATION_KIND:
            self.classification_profile = name
            self.classification_doc = {
                "kind": actions.CLASSIFICATION_KIND, "version": 1, "description": "",
                "applies_to": {"entity_type": "annotation", "file_role": "protein_fasta"},
                "sources": {}, "rules": [],
            }
            self._render_classification_form(
                name, dict(self.classification_doc), note="new profile (not saved yet)")
            return
        if kind == actions.COVERAGE_KIND:
            self.coverage_profile = name
            self.coverage_doc = {
                "kind": actions.COVERAGE_KIND, "version": 1, "description": "",
                "taxonomy": {"source": "NCBI"},
                "scope": {"root_taxids": [1]},
                "targets": {"ranks": ["family", "genus"]},
                "filters": {"exclude_extinct": False},
                "thresholds": {
                    "family": {"min_coverage_percent": 80},
                    "genus": {"min_coverage_percent": 80},
                },
            }
            self._render_coverage_form(
                name, dict(self.coverage_doc), note="new profile (not saved yet)")
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
        document = self._form_document(self._compose_profile_document)
        if document is None:
            return
        file_version = self._profile_file_version(name)
        new_version = 1 if file_version is None else file_version + 1
        self.app.push_screen(
            ProfileSaveModal(self.project, name, document, new_version),
            self._on_profile_saved,
        )

    def _active_profile_name(self) -> str | None:
        """The profile loaded in the editor currently on display.

        ``current_profile`` is maintained only by the qc editor path; the
        classification editor keeps its own name, so a History request must
        resolve the name from the visible editor instead (ODR-0041).
        """
        if self.query_one("#classification-editor").display:
            return self.classification_profile
        if self.query_one("#coverage-editor").display:
            return self.coverage_profile
        return self.current_profile

    def _open_profile_history(self) -> None:
        name = self._active_profile_name()
        if not name:
            return
        rows = data.profile_history(self.project, name)

        def restore(document: dict[str, Any]) -> None:
            self.profile_doc = dict(document)
            self._render_profile_document(
                name, dict(document),
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

    def _open_classify(self) -> None:
        """Run the selected classification profile (reads it from disk)."""
        from operon.tui.screens.classify import ClassifyModal

        name = self.classification_profile
        if not name or not (self.project.profiles_dir / f"{name}.yaml").exists():
            self.app.notify("save the classification profile before running it",
                            severity="warning")
            return
        self.app.push_screen(
            ClassifyModal(self.project, name, self.classification_doc),
            self._after_classify,
        )

    def _after_classify(self, payload: Any) -> None:
        if payload:
            self.reload()

    def _start_recipe_save(self) -> None:
        if not self.current_recipe or not self.recipe_tool or self.recipe_doc is None:
            return
        error = self.query_one("#recipe-save-error", Static)
        document = self._form_document(self._compose_recipe_document, error)
        if document is None:
            return
        # Mirror the mutual-exclusion checks of tools.get_recipe inline, before
        # the confirmation modal opens; the save_recipe round-trip still
        # re-validates everything else against the core loader.
        commands = document.get("commands") or []
        if commands and document.get("arguments"):
            error.update(Text(
                f"analysis {self.current_recipe!r}: 'commands' and 'arguments' "
                "are mutually exclusive",
                style="red",
            ))
            return
        for index, block in enumerate(commands, start=1):
            if not block.get("arguments"):
                error.update(Text(
                    f"analysis {self.current_recipe!r}: commands block {index} "
                    "requires a non-empty 'arguments' list",
                    style="red",
                ))
                return
            if block.get("version_pattern") and not block.get("version_args"):
                error.update(Text(
                    f"analysis {self.current_recipe!r}: commands block {index} "
                    "version_pattern requires version_args",
                    style="red",
                ))
                return
        slurm = document.get("slurm") or {}
        for key, kind, message in (
                ("mem_gb", int, "slurm mem_gb must be an integer >= 0"),
                ("poll_interval", float, "slurm poll_interval must be a number > 0"),
                ("array_concurrency", int,
                 "slurm array_concurrency must be a positive integer")):
            value = slurm.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, kind):
                error.update(Text(f"analysis {self.current_recipe!r}: {message}", style="red"))
                return
            if (key == "mem_gb" and value < 0) or (key == "array_concurrency" and value < 1) \
                    or (key == "poll_interval" and value <= 0):
                error.update(Text(f"analysis {self.current_recipe!r}: {message}", style="red"))
                return
        if (str(document.get("database_mode", "")).strip() == "mutable_cache"
                and not str(document.get("database_version", "")).strip()):
            error.update(Text(
                f"analysis {self.current_recipe!r}: mutable_cache requires an "
                "explicit database_version",
                style="red",
            ))
            return
        if (str(document.get("file_role", "")).strip()
                and str(document.get("file_role_prefix", "")).strip()):
            error.update(Text(
                f"analysis {self.current_recipe!r}: 'file_role' and 'file_role_prefix' "
                "are mutually exclusive",
                style="red",
            ))
            return
        error.update("")
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

    # -- run analysis ---------------------------------------------------------

    def _open_run_analysis(self) -> None:
        if not self.current_recipe:
            return
        from operon.tui.screens.analyze import AnalyzeModal, analysis_finished

        self.app.push_screen(
            AnalyzeModal(self.project, recipe_name=self.current_recipe),
            lambda payload: analysis_finished(self.app, payload),
        )
