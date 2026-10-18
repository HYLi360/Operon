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

import re
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
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
    ErrorDialog,
    FittingSelect,
    MountTracked,
    Panel,
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
from operon.tui.screens.config_modals import (
    CondaDefaultsSaveModal,
    HistoryModal,
    NewProfileModal,
    ProfileSaveModal,
    RecipeSaveModal,  # noqa: F401 - re-exported for the recipe editor's tests
    SnapshotViewModal,  # noqa: F401 - re-exported for the HistoryModal view flow and its tests
    ToolSaveModal,
)
from operon.tui.screens.config_recipe import (
    ARTIFACT_KINDS,
    DATABASE_MODES,
    ENVIRONMENT_POLICIES,
    HMMER_MODES,
    RESULT_PARSERS,
    RecipeEditor,
)
from operon.tui.screens.config_rows import (
    CommandRow,
    RuleRow,
    _extras_note,
    _format_parameter_line,  # noqa: F401 - re-exported for tests/unit/test_tui_config.py
    _parse_parameter_line,  # noqa: F401 - re-exported for tests/unit/test_tui_config.py
)

ENTITY_TYPE_NAMES = list(actions.ENTITY_TYPE_NAMES)

PROFILE_MODELED_KEYS = frozenset(
    {"kind", "version", "description", "applies_to", "required", "warnings"}
)

#: Run-method modes the tool editor offers.  A blank selection means the
#: ``run_method`` key is absent, so the executable runs directly.
TOOL_RUN_METHOD_OPTIONS = [
    ("command line (string)", "string"),
    ("conda", "conda"),
    ("prefix", "prefix"),
    ("path (no launcher)", "path"),
]


class ConfigPanel(RecipeEditor, Panel):
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
        self.current_tool: str | None = None
        self.tool_doc: dict[str, Any] | None = None
        # The chain the form currently holds.  Rows mount a turn late, so this
        # is the panel's own count and never read back from the DOM.
        self._command_row_count = 0
        # `slurm` keys the form does not model, carried verbatim into the save.
        self._slurm_extras: dict[str, Any] = {}
        # Parameter lines the grammar could not read, reported on save.
        self._parameter_problems: list[str] = []
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
                            yield Button(
                                "Run classify", id="profile-run", disabled=True
                            )
                    with VerticalScroll(id="profile-editor"):  # pragma: no branch
                        yield Static("select a profile", id="profile-heading")
                        yield Static("Description", classes="modal-label")
                        yield Input(id="profile-description")
                        yield Static("Applies to", classes="modal-label")
                        for entity_type in ENTITY_TYPE_NAMES:
                            yield Checkbox(
                                entity_type, id=f"profile-applies-{entity_type}"
                            )
                        yield Static("", id="profile-version-note")
                        yield Static("", id="profile-extras-note")
                        yield Static("Required rules", classes="modal-label")
                        yield MountTracked(id="profile-required-rules")
                        yield Button("add rule", id="profile-add-required")
                        yield Static("Warning rules", classes="modal-label")
                        yield MountTracked(id="profile-warnings-rules")
                        yield Button("add rule", id="profile-add-warnings")
                        with Horizontal(classes="config-buttons"):  # pragma: no branch
                            yield Button(
                                "Save profile",
                                id="profile-save",
                                variant="primary",
                                disabled=True,
                            )
                    with VerticalScroll(
                        id="classification-editor"
                    ):  # pragma: no branch
                        yield Static("select a profile", id="classification-heading")
                        yield Static("", id="classification-readonly-note")
                        yield Static("Description", classes="modal-label")
                        yield Input(id="classification-description")
                        yield Static(
                            "Applies to (entity_type + file_role)",
                            classes="modal-label",
                        )
                        yield Input(
                            placeholder="entity_type", id="classification-entity-type"
                        )
                        yield Input(
                            placeholder="file_role", id="classification-file-role"
                        )
                        yield Static("", id="classification-version-note")
                        yield Static("", id="classification-extras-note")
                        yield Static(
                            "Sources (name → analysis, filter, best_by)",
                            classes="modal-label",
                        )
                        yield MountTracked(id="classification-sources")
                        yield Button("add source", id="classification-add-source")
                        yield Static(
                            "Rules (first match wins; label + source/when, absent, "
                            "or default)",
                            classes="modal-label",
                        )
                        yield MountTracked(id="classification-rules")
                        yield Button("add rule", id="classification-add-rule")
                        yield Static("", id="classification-save-error")
                        with Horizontal(classes="config-buttons"):  # pragma: no branch
                            yield Button(
                                "Save profile",
                                id="classification-save",
                                variant="primary",
                                disabled=True,
                            )
                            yield Button(
                                "History", id="classification-history", disabled=True
                            )
                    with VerticalScroll(id="coverage-editor"):  # pragma: no branch
                        yield Static("select a profile", id="coverage-heading")
                        yield Static("", id="coverage-readonly-note")
                        yield Static("Description", classes="modal-label")
                        yield Input(id="coverage-description")
                        yield Static("Taxonomy source", classes="modal-label")
                        yield FittingSelect(
                            [(source, source) for source in ("NCBI",)],
                            value="NCBI",
                            id="coverage-taxonomy-source",
                            allow_blank=False,
                        )
                        yield Static(
                            "Root TaxIDs (comma separated)", classes="modal-label"
                        )
                        yield Input(placeholder="e.g. 33090", id="coverage-root-taxids")
                        yield Static("Target ranks", classes="modal-label")
                        yield Checkbox("family", id="coverage-rank-family")
                        yield Checkbox("genus", id="coverage-rank-genus")
                        yield Static("Filters", classes="modal-label")
                        yield Checkbox(
                            "exclude extinct taxa", id="coverage-exclude-extinct"
                        )
                        yield Static(
                            "Excluded subtrees (comma separated TaxIDs)",
                            classes="modal-label",
                        )
                        yield Input(
                            placeholder="blank = none", id="coverage-exclude-subtrees"
                        )
                        yield Static(
                            "Excluded name patterns (one regular expression per line)",
                            classes="modal-label",
                        )
                        yield TextArea(id="coverage-exclude-patterns")
                        yield Static(
                            "Minimum coverage percent per checked target rank",
                            classes="modal-label",
                        )
                        yield Input(
                            placeholder="family %", id="coverage-threshold-family"
                        )
                        yield Input(
                            placeholder="genus %", id="coverage-threshold-genus"
                        )
                        yield Static("", id="coverage-version-note")
                        yield Static("", id="coverage-extras-note")
                        yield Static("", id="coverage-save-error")
                        with Horizontal(classes="config-buttons"):  # pragma: no branch
                            yield Button(
                                "Save profile",
                                id="coverage-save",
                                variant="primary",
                                disabled=True,
                            )
                            yield Button(
                                "History", id="coverage-history", disabled=True
                            )
            with TabPane("Tools && Recipes", id="tab-tools"):
                with Vertical(id="tools-layout"):  # pragma: no branch
                    yield Static(
                        "Conda defaults (every tool that inherits them)",
                        classes="modal-label",
                    )
                    yield Input(
                        placeholder="conda binary (blank = conda)", id="conda-bin"
                    )
                    yield Static(
                        "conda run args (one token per line; blank = "
                        "run --no-capture-output)",
                        classes="modal-label",
                    )
                    yield TextArea(id="conda-run-args")
                    yield Static("", id="conda-note")
                    yield Static("", id="conda-save-error")
                    with Horizontal(classes="config-buttons"):
                        yield Button("Save defaults", id="conda-save", disabled=True)
                        yield Button("Check tools", id="tools-check")
                    yield DataTable(id="tools-table", cursor_type="row")
                    with VerticalScroll(id="tool-editor"):  # pragma: no branch
                        yield Static("select a tool", id="tool-heading")
                        yield Static("Description", classes="modal-label")
                        yield Input(id="tool-description")
                        yield Static(
                            "Executable (blank = tool name)", classes="modal-label"
                        )
                        yield Input(id="tool-executable")
                        yield Static(
                            "Version args (one token per line)", classes="modal-label"
                        )
                        yield TextArea(id="tool-version-args")
                        yield Static(
                            "Version pattern (regular expression)",
                            classes="modal-label",
                        )
                        yield Input(id="tool-version-pattern")
                        yield Static("Run method", classes="modal-label")
                        yield FittingSelect(
                            TOOL_RUN_METHOD_OPTIONS,
                            id="tool-run-method",
                            prompt="no run_method (run directly)",
                            allow_blank=True,
                        )
                        with Vertical(id="tool-run-string-row"):
                            yield Static(
                                "Launcher command (run verbatim)", classes="modal-label"
                            )
                            yield Input(id="tool-run-string")
                        with Vertical(id="tool-run-conda-row"):
                            yield Static(
                                "conda environment (required)", classes="modal-label"
                            )
                            yield Input(id="tool-run-conda-env")
                            yield Static(
                                "conda binary (blank = top-level conda.bin)",
                                classes="modal-label",
                            )
                            yield Input(id="tool-run-conda-bin")
                            yield Static(
                                "conda run args (one token per line; blank = "
                                "top-level conda.run_args)",
                                classes="modal-label",
                            )
                            yield TextArea(id="tool-run-conda-args")
                        with Vertical(id="tool-run-prefix-row"):
                            yield Static(
                                "Launcher prefix (one token per line)",
                                classes="modal-label",
                            )
                            yield TextArea(id="tool-run-prefix")
                        yield Static("", id="tool-extras-note")
                        yield Static("", id="tool-save-error")
                        with Horizontal(classes="config-buttons"):  # pragma: no branch
                            yield Button(
                                "Save tool",
                                id="tool-save",
                                variant="primary",
                                disabled=True,
                            )
                    yield Static("Recipes", classes="modal-label")
                    yield DataTable(id="recipes-table", cursor_type="row")
                    with Horizontal(classes="config-buttons"):
                        yield Button("Run analysis", id="recipe-run", disabled=True)
                    with VerticalScroll(id="recipe-editor"):  # pragma: no branch
                        yield Static("select a recipe", id="recipe-heading")
                        yield Static("Description", classes="modal-label")
                        yield Input(id="recipe-description")
                        yield Static("Entity type (blank = *)", classes="modal-label")
                        yield FittingSelect(
                            ENTITY_TYPE_OPTIONS,
                            id="recipe-entity-type",
                            allow_blank=True,
                        )
                        yield Input(placeholder="file_role", id="recipe-file-role")
                        yield Input(
                            placeholder="file_role_prefix (mutually exclusive with "
                            "file_role)",
                            id="recipe-file-role-prefix",
                        )
                        yield Input(placeholder="format", id="recipe-format")
                        yield Static(
                            "Input kind (blank = key absent)", classes="modal-label"
                        )
                        yield FittingSelect(
                            [(kind, kind) for kind in ARTIFACT_KINDS],
                            id="recipe-input-kind",
                            allow_blank=True,
                        )
                        yield Static(
                            "Output kind (blank = key absent)", classes="modal-label"
                        )
                        yield FittingSelect(
                            [(kind, kind) for kind in ARTIFACT_KINDS],
                            id="recipe-output-kind",
                            allow_blank=True,
                        )
                        yield Input(placeholder="database", id="recipe-database")
                        yield Input(
                            placeholder="database_version", id="recipe-database-version"
                        )
                        yield FittingSelect(
                            [(mode, mode) for mode in DATABASE_MODES],
                            id="recipe-database-mode",
                            allow_blank=True,
                        )
                        yield Input(
                            placeholder="database_checksum (sha256 hex; optional)",
                            id="recipe-database-checksum",
                        )
                        yield Static(
                            "Environment policy (blank = key absent; core default "
                            "'warn')",
                            classes="modal-label",
                        )
                        yield FittingSelect(
                            [(policy, policy) for policy in ENVIRONMENT_POLICIES],
                            id="recipe-environment-policy",
                            allow_blank=True,
                        )
                        yield Static(
                            "Slurm overrides (optional; merges over "
                            "execution.slurm — unknown keys preserved)",
                            classes="modal-label",
                        )
                        yield Input(
                            placeholder="slurm partition", id="recipe-slurm-partition"
                        )
                        yield Input(
                            placeholder="slurm time (e.g. 24:00:00)",
                            id="recipe-slurm-time",
                        )
                        yield Input(
                            placeholder="slurm mem_gb", id="recipe-slurm-mem-gb"
                        )
                        yield Input(
                            placeholder="slurm poll_interval (seconds)",
                            id="recipe-slurm-poll-interval",
                        )
                        yield FittingSelect(
                            [("true", "true"), ("false", "false")],
                            id="recipe-slurm-array",
                            allow_blank=True,
                        )
                        yield Input(
                            placeholder="slurm array_concurrency",
                            id="recipe-slurm-array-concurrency",
                        )
                        yield Static(
                            "slurm extra_sbatch (one sbatch flag per line)",
                            classes="modal-label",
                        )
                        yield TextArea(id="recipe-slurm-extra-sbatch")
                        yield Static(
                            "slurm setup_commands (one command per line)",
                            classes="modal-label",
                        )
                        yield TextArea(id="recipe-slurm-setup-commands")
                        yield Static("", id="recipe-slurm-note")
                        yield Input(
                            placeholder="output_subdir", id="recipe-output-subdir"
                        )
                        yield Input(
                            placeholder="output_suffix", id="recipe-output-suffix"
                        )
                        yield Input(
                            placeholder="output_name (template, e.g. ${file_id}.out)",
                            id="recipe-output-name",
                        )
                        yield Static(
                            "Arguments (one per line; ${placeholders} stay as-is)",
                            classes="modal-label",
                        )
                        yield TextArea(id="recipe-arguments")
                        yield Static(
                            "Runtime parameters (name=default per line, then "
                            "optional `; required`, `; choices=a,b` and "
                            "`; pattern=<regex>` — pattern takes the rest of the "
                            "line; other spec keys preserved)",
                            classes="modal-label",
                        )
                        yield TextArea(id="recipe-parameters")
                        yield Static("", id="recipe-parameters-note")
                        yield Static(
                            "Commands chain (optional; mutually exclusive with "
                            "arguments — every step runs in the parent tool's single "
                            "run_method environment)",
                            classes="modal-label",
                        )
                        yield MountTracked(id="recipe-command-list")
                        with Horizontal(classes="config-buttons"):
                            yield Button("Add step", id="recipe-add-command")
                        yield Static("", id="recipe-command-note")
                        yield Static("Result parser", classes="modal-label")
                        yield FittingSelect(
                            [(parser, parser) for parser in RESULT_PARSERS],
                            value="none",
                            id="recipe-result-parser",
                            allow_blank=False,
                        )
                        yield Input(placeholder="result_glob", id="recipe-result-glob")
                        yield Static(
                            "HMMER mode (blank = key absent)", classes="modal-label"
                        )
                        yield FittingSelect(
                            [(mode, mode) for mode in HMMER_MODES],
                            id="recipe-hmmer-mode",
                            allow_blank=True,
                        )
                        yield Input(
                            placeholder="result_columns (comma separated)",
                            id="recipe-result-columns",
                        )
                        yield Input(
                            placeholder="hit_metric_columns (comma separated)",
                            id="recipe-hit-metric-columns",
                        )
                        yield Static(
                            "Column mapping (blank = parser defaults)",
                            classes="modal-label",
                        )
                        yield Input(
                            placeholder="query_column", id="recipe-query-column"
                        )
                        yield Input(
                            placeholder="subject_column", id="recipe-subject-column"
                        )
                        yield Input(
                            placeholder="numeric_columns (comma separated)",
                            id="recipe-numeric-columns",
                        )
                        yield Input(
                            placeholder="qstart_column", id="recipe-qstart-column"
                        )
                        yield Input(placeholder="qend_column", id="recipe-qend-column")
                        yield Input(
                            placeholder="sstart_column", id="recipe-sstart-column"
                        )
                        yield Input(placeholder="send_column", id="recipe-send-column")
                        yield Input(
                            placeholder="evalue_column", id="recipe-evalue-column"
                        )
                        yield Input(
                            placeholder="bitscore_column", id="recipe-bitscore-column"
                        )
                        yield Input(
                            placeholder="pident_column", id="recipe-pident-column"
                        )
                        yield Input(
                            placeholder="max_hits_per_query (blank restores default: 5)",
                            id="recipe-max-hits",
                        )
                        yield Static("", id="recipe-extras-note")
                        yield Static("", id="recipe-save-error")
                        with Horizontal(classes="config-buttons"):  # pragma: no branch
                            yield Button(
                                "Save recipe",
                                id="recipe-save",
                                variant="primary",
                                disabled=True,
                            )
                            yield Button("History", id="recipe-history", disabled=True)

    def on_mount(self) -> None:
        tools_table = self.query_one("#tools-table", DataTable)
        tools_table.add_column("tool", key="name")
        tools_table.add_column("executable", key="executable")
        tools_table.add_column("run_method", key="run_method")
        tools_table.add_column("detected version", key="version")
        recipes_table = self.query_one("#recipes-table", DataTable)
        recipes_table.add_columns(
            "name", "version", "tool", "entity_type", "file_role", "format"
        )
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
            list_view.append(
                ListItem(Label(f"{profile['name']}  v{profile['version']}{suffix}"))
            )

        self._render_conda_defaults()
        tools_table = self.query_one("#tools-table", DataTable)
        tools_table.clear()
        for tool in self.tools:
            tools_table.add_row(
                tool["name"],
                tool["executable"],
                tool["run_method"] or "(direct)",
                Text("not checked", style="dim"),
                key=tool["name"],
            )

        recipes_table = self.query_one("#recipes-table", DataTable)
        recipes_table.clear()
        for recipe in self.recipes:
            recipes_table.add_row(
                recipe["name"],
                str(recipe["version"]),
                recipe["tool"],
                recipe["entity_type"],
                recipe["file_role"],
                recipe["format"],
                key=recipe["name"],
            )

    def show_error(self, exc: BaseException) -> None:
        self.app.notify(f"config load failed: {exc}", severity="error")

    # -- profile editor ----------------------------------------------------

    def _rule_rows(self, section: str) -> list[RuleRow]:
        return list(
            self.query_one(f"#profile-{section}-rules", Vertical).query(RuleRow)
        )

    def _render_profile_form(
        self, name: str, document: dict[str, Any], note: str = ""
    ) -> None:
        self._show_editor("qc")
        self.query_one("#profile-run", Button).disabled = True
        self.query_one("#profile-heading", Static).update(
            f"{name}" + (f"  —  {note}" if note else "")
        )
        self.query_one("#profile-description", Input).value = str(
            document.get("description", "")
        )
        applies_to = {str(item) for item in document.get("applies_to", []) or []}
        for entity_type in ENTITY_TYPE_NAMES:
            self.query_one(f"#profile-applies-{entity_type}", Checkbox).value = (
                entity_type in applies_to
            )
        version = int(document.get("version", 1))
        self.query_one("#profile-version-note", Static).update(
            Text(
                f"version {version} — saving writes the next version and records a snapshot",
                style="dim",
            )
        )
        extras = {
            key: value
            for key, value in document.items()
            if key not in PROFILE_MODELED_KEYS
        }
        self.query_one("#profile-extras-note", Static).update(
            Text(_extras_note(extras), style="dim") if extras else ""
        )
        for section in ("required", "warnings"):
            container = self.query_one(f"#profile-{section}-rules", Vertical)
            remount(
                container,
                *[
                    RuleRow(rule)
                    for rule in document.get(section, []) or []
                    if isinstance(rule, dict)
                ],
            )
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

    def _render_profile_document(
        self, name: str, document: dict[str, Any], note: str = ""
    ) -> None:
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
        self.query_one("#classification-editor").display = (
            kind == actions.CLASSIFICATION_KIND
        )
        self.query_one("#coverage-editor").display = kind == actions.COVERAGE_KIND

    def _remember_version(self, kind: str, name: str, document: dict[str, Any]) -> None:
        key = (kind, name)
        self._known_versions[key] = max(
            self._known_versions.get(key, 0),
            int(document.get("version", 1)),
        )

    def _compose_profile_document(self) -> dict[str, Any]:
        original = self.profile_doc or {}
        document: dict[str, Any] = {
            "kind": "qc",
            "version": int(original.get("version", 1)),
            "description": self.query_one("#profile-description", Input).value.strip(),
            "applies_to": [
                entity_type
                for entity_type in ENTITY_TYPE_NAMES
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
            version = int(
                data.get_profile_document(self.project, name, kind=kind).get(
                    "version", 1
                )
            )
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

    def _render_classification_form(
        self, name: str, document: dict[str, Any], note: str = ""
    ) -> None:
        self._show_editor(actions.CLASSIFICATION_KIND)
        supported, reason = classification_form_supported(document)
        heading = f"{name}" + (f"  —  {note}" if note else "")
        self.query_one("#classification-heading", Static).update(heading)
        self.query_one("#classification-readonly-note", Static).update(
            Text(
                f"structure exceeds the manual form: {reason} — edit the YAML file; saving "
                "from here is disabled and the file is never rewritten by the form",
                style="yellow",
            )
            if not supported
            else ""
        )
        self.query_one("#classification-description", Input).value = str(
            document.get("description", "")
        )
        applies_to = document.get("applies_to")
        applies_to = applies_to if isinstance(applies_to, dict) else {}
        self.query_one("#classification-entity-type", Input).value = str(
            applies_to.get("entity_type", "") or ""
        )
        self.query_one("#classification-file-role", Input).value = str(
            applies_to.get("file_role", "") or ""
        )
        version = int(document.get("version", 1))
        self.query_one("#classification-version-note", Static).update(
            Text(
                f"version {version} — saving writes the next version and records a snapshot "
                "that a classify run consumes",
                style="dim",
            )
        )
        extras = {
            key: value
            for key, value in document.items()
            if key not in CLASSIFICATION_MODELED_KEYS
        }
        self.query_one("#classification-extras-note", Static).update(
            Text(_extras_note(extras), style="dim") if extras else ""
        )
        sources = document.get("sources")
        sources = sources if isinstance(sources, dict) else {}
        source_container = self.query_one("#classification-sources", Vertical)
        remount(
            source_container,
            *[
                SourceRow(str(source_name), source)
                for source_name, source in sources.items()
                if isinstance(source, dict)
            ],
        )
        rules = document.get("rules")
        rules = (
            [rule for rule in rules if isinstance(rule, dict)]
            if isinstance(rules, list)
            else []
        )
        rules_container = self.query_one("#classification-rules", Vertical)
        names = [str(source_name) for source_name in sources]
        remount(
            rules_container, *[ClassificationRuleRow(rule, names) for rule in rules]
        )
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
            "description": self.query_one(
                "#classification-description", Input
            ).value.strip(),
            "applies_to": {
                "entity_type": self.query_one(
                    "#classification-entity-type", Input
                ).value.strip(),
                "file_role": self.query_one(
                    "#classification-file-role", Input
                ).value.strip(),
            },
            "sources": sources,
            "rules": [
                row.rule_document()
                for row in self.query(".classrule-row").results(ClassificationRuleRow)
                if not row.removal_pending
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
            error.update(
                Text(
                    f"duplicate source name(s): {', '.join(duplicates)} — source names are the "
                    "mapping keys and must be unique",
                    style="red",
                )
            )
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

    def _render_coverage_form(
        self, name: str, document: dict[str, Any], note: str = ""
    ) -> None:
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
            )
            if not supported
            else ""
        )
        self.query_one("#coverage-description", Input).value = str(
            document.get("description", "")
        )
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
                rank not in ranks
            )
        filters = document.get("filters")
        filters = filters if isinstance(filters, dict) else {}
        self.query_one("#coverage-exclude-extinct", Checkbox).value = bool(
            filters.get("exclude_extinct", False)
        )
        subtrees = filters.get("exclude_subtrees")
        subtrees = (
            [str(item) for item in subtrees] if isinstance(subtrees, list) else []
        )
        self.query_one("#coverage-exclude-subtrees", Input).value = ", ".join(subtrees)
        patterns = filters.get("exclude_name_patterns")
        patterns = (
            [str(item) for item in patterns] if isinstance(patterns, list) else []
        )
        self.query_one("#coverage-exclude-patterns", TextArea).text = "\n".join(
            patterns
        )
        thresholds = document.get("thresholds")
        thresholds = thresholds if isinstance(thresholds, dict) else {}
        for rank in COVERAGE_RANKS:
            entry = thresholds.get(rank)
            entry = entry if isinstance(entry, dict) else {}
            value = entry.get("min_coverage_percent")
            self.query_one(f"#coverage-threshold-{rank}", Input).value = (
                "" if value is None else str(value)
            )
        version = int(document.get("version", 1))
        self.query_one("#coverage-version-note", Static).update(
            Text(
                f"version {version} — saving writes the next version and records a "
                "snapshot that a taxonomy compile consumes",
                style="dim",
            )
        )
        extras = {
            key: value
            for key, value in document.items()
            if key not in COVERAGE_MODELED_KEYS
        }
        self.query_one("#coverage-extras-note", Static).update(
            Text(_extras_note(extras), style="dim") if extras else ""
        )
        self.query_one("#coverage-save-error", Static).update("")
        self.query_one("#coverage-save", Button).disabled = not supported
        self.query_one("#coverage-history", Button).disabled = False

    def _sync_coverage_thresholds(self) -> None:
        for rank in COVERAGE_RANKS:
            self.query_one(
                f"#coverage-threshold-{rank}", Input
            ).disabled = not self.query_one(f"#coverage-rank-{rank}", Checkbox).value

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
                key: val
                for key, val in value.items()
                if key not in SECTION_MODELED_KEYS[name]
            }

        original_filters = original.get("filters")
        original_filters = (
            original_filters if isinstance(original_filters, dict) else {}
        )

        taxonomy = section("taxonomy")
        source_value = self.query_one("#coverage-taxonomy-source", Select).value
        taxonomy["source"] = (
            "NCBI" if source_value is Select.NULL else str(source_value)
        )
        document["taxonomy"] = taxonomy

        scope = section("scope")
        roots = [
            actions.coerce_scalar(part)
            for part in self.query_one("#coverage-root-taxids", Input).value.split(",")
            if part.strip()
        ]
        original_scope = original.get("scope")
        if roots or (
            isinstance(original_scope, dict) and "root_taxids" in original_scope
        ):
            scope["root_taxids"] = roots
        document["scope"] = scope

        ranks = [
            rank
            for rank in COVERAGE_RANKS
            if self.query_one(f"#coverage-rank-{rank}", Checkbox).value
        ]
        targets = section("targets")
        original_targets = original.get("targets")
        if ranks or (
            isinstance(original_targets, dict) and "ranks" in original_targets
        ):
            targets["ranks"] = ranks
        document["targets"] = targets

        filters = section("filters")
        exclude_extinct = self.query_one("#coverage-exclude-extinct", Checkbox).value
        if exclude_extinct or "exclude_extinct" in original_filters:
            filters["exclude_extinct"] = exclude_extinct
        subtrees = [
            actions.coerce_scalar(part)
            for part in self.query_one("#coverage-exclude-subtrees", Input).value.split(
                ","
            )
            if part.strip()
        ]
        if subtrees or "exclude_subtrees" in original_filters:
            filters["exclude_subtrees"] = subtrees
        patterns = [
            line.strip()
            for line in self.query_one(
                "#coverage-exclude-patterns", TextArea
            ).text.splitlines()
            if line.strip()
        ]
        if patterns or "exclude_name_patterns" in original_filters:
            filters["exclude_name_patterns"] = patterns
        document["filters"] = filters

        original_thresholds = original.get("thresholds")
        original_thresholds = (
            original_thresholds if isinstance(original_thresholds, dict) else {}
        )
        thresholds: dict[str, Any] = {}
        for rank in COVERAGE_RANKS:
            if rank not in ranks:
                continue
            entry: dict[str, Any] = {}
            original_entry = original_thresholds.get(rank)
            if isinstance(original_entry, dict):
                entry.update(
                    {
                        key: value
                        for key, value in original_entry.items()
                        if key not in THRESHOLD_MODELED_KEYS
                    }
                )
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
            rank
            for rank in COVERAGE_RANKS
            if self.query_one(f"#coverage-rank-{rank}", Checkbox).value
        ]
        if not ranks:
            error.update(Text("at least one target rank is required", style="red"))
            return
        missing = [
            rank
            for rank in ranks
            if not self.query_one(f"#coverage-threshold-{rank}", Input).value.strip()
        ]
        if missing:
            error.update(
                Text(
                    f"missing minimum coverage for target rank(s): {', '.join(missing)}",
                    style="red",
                )
            )
            return
        for part in self.query_one("#coverage-root-taxids", Input).value.split(","):
            part = part.strip()
            if part and not part.isdigit():
                error.update(
                    Text(
                        f"root TaxIDs must be positive integers; got {part!r}",
                        style="red",
                    )
                )
                return
        for part in self.query_one("#coverage-exclude-subtrees", Input).value.split(
            ","
        ):
            part = part.strip()
            if part and not part.isdigit():
                error.update(
                    Text(
                        f"excluded subtrees must be positive integers; got {part!r}",
                        style="red",
                    )
                )
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

    # -- tool editor --------------------------------------------------------

    #: Tool-level keys the editor owns; everything else in the tool mapping is
    #: either shown read-only (recipes) or preserved verbatim (extras note).
    TOOL_MODELED_KEYS = (
        "description",
        "executable",
        "run_method",
        "version_args",
        "version_pattern",
    )

    #: Sub-keys the run-method form owns inside a ``run_method`` mapping.  Any
    #: other sub-key of the parsed mapping survives a conda-mode edit.
    RUN_METHOD_SUBKEYS = ("mode", "env", "bin", "args", "prefix")

    def _load_tool(self, name: str) -> None:
        try:
            info = data.get_tool_document(self.project, name)
        except ValidationError as exc:
            self.app.notify(str(exc), severity="error")
            return
        self.current_tool = name
        self.tool_doc = info["document"]
        self._render_tool_form(name, info["document"])

    def _render_tool_form(self, name: str, document: dict[str, Any]) -> None:
        extras = {
            key: value
            for key, value in document.items()
            if key not in self.TOOL_MODELED_KEYS and key != "recipes"
        }
        self.query_one("#tool-heading", Static).update(name)
        for key, widget_id in (
            ("description", "#tool-description"),
            ("executable", "#tool-executable"),
            ("version_pattern", "#tool-version-pattern"),
        ):
            self.query_one(widget_id, Input).value = str(document.get(key, "") or "")
        version_args = document.get("version_args") or []
        args = version_args if isinstance(version_args, list) else [version_args]
        self.query_one("#tool-version-args", TextArea).text = "\n".join(
            str(arg) for arg in args
        )
        self._render_run_method(document.get("run_method"))
        note = self.query_one("#tool-extras-note", Static)
        messages = [_extras_note(extras)] if extras else []
        if self._run_method_extras:
            messages.append("run_method keeps " + ", ".join(self._run_method_extras))
        note.update(Text("; ".join(messages), style="dim") if messages else "")
        self.query_one("#tool-save-error", Static).update("")
        self.query_one("#tool-save", Button).disabled = False

    def _render_run_method(self, run_method: Any) -> None:
        """Fill the run-method widgets; the mode Select drives which rows show."""
        method = run_method if isinstance(run_method, dict) else {}
        self._run_method_extras = sorted(set(method) - set(self.RUN_METHOD_SUBKEYS))
        if isinstance(run_method, str) and run_method:
            mode = "string"
        elif method:
            mode = str(method.get("mode", "conda") or "conda")
        else:
            mode = ""  # no run_method key: the executable runs directly
        self.query_one("#tool-run-string", Input).value = (
            run_method if isinstance(run_method, str) else ""
        )
        self.query_one("#tool-run-conda-env", Input).value = str(
            method.get("env", "") or ""
        )
        self.query_one("#tool-run-conda-bin", Input).value = str(
            method.get("bin", "") or ""
        )
        self.query_one("#tool-run-conda-args", TextArea).text = "\n".join(
            self._as_token_list(method.get("args"))
        )
        self.query_one("#tool-run-prefix", TextArea).text = "\n".join(
            self._as_token_list(method.get("prefix"))
        )
        select = self.query_one("#tool-run-method", Select)
        select.value = mode if mode else Select.NULL
        self._sync_tool_run_method()

    @staticmethod
    def _as_token_list(value: Any) -> list[str]:
        if not value:
            return []
        tokens = value if isinstance(value, list) else [value]
        return [str(token) for token in tokens]

    def _sync_tool_run_method(self) -> None:
        """Show only the rows of the selected run-method mode."""
        value = self.query_one("#tool-run-method", Select).value
        mode = "" if value is Select.NULL else str(value)
        self.query_one("#tool-run-string-row", Vertical).display = mode == "string"
        self.query_one("#tool-run-conda-row", Vertical).display = mode == "conda"
        self.query_one("#tool-run-prefix-row", Vertical).display = mode == "prefix"

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "tool-run-method":
            self._sync_tool_run_method()

    def _compose_tool_document(self) -> dict[str, Any]:
        # The document is the complete state of the modeled keys: a blanked
        # field is omitted, which save_tool writes as a removed key (so a blank
        # executable falls back to the tool name).
        document: dict[str, Any] = {}
        for key, widget_id in (
            ("description", "#tool-description"),
            ("executable", "#tool-executable"),
            ("version_pattern", "#tool-version-pattern"),
        ):
            value = self.query_one(widget_id, Input).value.strip()
            if value:
                document[key] = value
        args = [
            line.strip()
            for line in self.query_one("#tool-version-args", TextArea).text.splitlines()
            if line.strip()
        ]
        if args:
            document["version_args"] = args
        run_method = self._compose_run_method()
        if run_method is not None:
            document["run_method"] = run_method
        return document

    def _compose_run_method(self) -> Any:
        """The run_method the form holds, or None when the key is dropped."""
        value = self.query_one("#tool-run-method", Select).value
        mode = "" if value is Select.NULL else str(value)
        if not mode:  # blank: no run_method key, the executable runs directly
            return None
        if mode == "string":
            return self.query_one("#tool-run-string", Input).value.strip()
        if mode == "prefix":
            return {"mode": "prefix", "prefix": self._text_lines("#tool-run-prefix")}
        if mode == "path":
            return {"mode": "path"}
        original = (self.tool_doc or {}).get("run_method")
        method: dict[str, Any] = {}
        if isinstance(original, dict) and str(original.get("mode", "")) == "conda":
            method.update(original)  # unmodeled sub-keys survive a conda edit
        method["mode"] = "conda"
        method["env"] = self.query_one("#tool-run-conda-env", Input).value.strip()
        conda_bin = self.query_one("#tool-run-conda-bin", Input).value.strip()
        if conda_bin:
            method["bin"] = conda_bin
        else:
            method.pop("bin", None)  # blank inherits the top-level conda.bin
        conda_args = self._text_lines("#tool-run-conda-args")
        if conda_args:
            method["args"] = conda_args
        else:
            method.pop("args", None)  # blank inherits the top-level conda.run_args
        return method

    def _text_lines(self, widget_id: str) -> list[str]:
        """A TextArea field as one token per non-empty line."""
        return [
            line.strip()
            for line in self.query_one(widget_id, TextArea).text.splitlines()
            if line.strip()
        ]

    def _start_tool_save(self) -> None:
        if not self.current_tool or self.tool_doc is None:
            return
        error = self.query_one("#tool-save-error", Static)
        document = self._form_document(self._compose_tool_document, error)
        if document is None:
            return
        pattern = str(document.get("version_pattern", "") or "")
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                error.update(
                    Text(
                        f"tool {self.current_tool!r}: version_pattern is not a valid "
                        f"regular expression ({exc})",
                        style="red",
                    )
                )
                return
        run_method = document.get("run_method")
        if run_method == "":
            error.update(
                Text(
                    f"tool {self.current_tool!r}: the command-line run method needs "
                    "a command",
                    style="red",
                )
            )
            return
        if isinstance(run_method, dict):
            if run_method.get("mode") == "conda" and not run_method.get("env"):
                error.update(
                    Text(
                        f"tool {self.current_tool!r}: the conda run method requires "
                        "an environment (or blank for no run_method)",
                        style="red",
                    )
                )
                return
            if run_method.get("mode") == "prefix" and not run_method.get("prefix"):
                error.update(
                    Text(
                        f"tool {self.current_tool!r}: the prefix run method requires "
                        "at least one token (or blank for no run_method)",
                        style="red",
                    )
                )
                return
        self.app.push_screen(
            ToolSaveModal(self.project, self.current_tool, document),
            self._on_tool_saved,
        )

    def _on_tool_saved(self, payload: Any) -> None:
        if not payload:
            return
        self.reload()
        name = self.current_tool
        if name:
            self._load_tool(name)

    # -- conda defaults editor ----------------------------------------------

    def _render_conda_defaults(self) -> None:
        try:
            document = data.get_tools_defaults(self.project)["document"]
        except ValidationError as exc:
            self.query_one("#conda-note", Static).update(Text(str(exc), style="red"))
            self.query_one("#conda-save", Button).disabled = True
            return
        extras = {
            key: value
            for key, value in document.items()
            if key not in actions.MODELED_CONDA_KEYS
        }
        self.query_one("#conda-bin", Input).value = str(document.get("bin", "") or "")
        self.query_one("#conda-run-args", TextArea).text = "\n".join(
            self._as_token_list(document.get("run_args"))
        )
        note = self.query_one("#conda-note", Static)
        note.update(Text(_extras_note(extras), style="dim") if extras else "")
        self.query_one("#conda-save-error", Static).update("")
        self.query_one("#conda-save", Button).disabled = False

    def _compose_conda_document(self) -> dict[str, Any]:
        # The document is the complete state of the modeled keys: a blanked
        # field is omitted, which save_tools_defaults writes as a removed key —
        # the core then falls back to its built-in default.
        document: dict[str, Any] = {}
        binary = self.query_one("#conda-bin", Input).value.strip()
        if binary:
            document["bin"] = binary
        args = self._text_lines("#conda-run-args")
        if args:
            document["run_args"] = args
        return document

    def _start_conda_save(self) -> None:
        error = self.query_one("#conda-save-error", Static)
        document = self._form_document(self._compose_conda_document, error)
        if document is None:
            return
        self.app.push_screen(
            CondaDefaultsSaveModal(self.project, document),
            self._on_conda_saved,
        )

    def _on_conda_saved(self, payload: Any) -> None:
        if not payload:
            return
        self.reload()

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
        elif event.data_table.id == "tools-table" and event.row_key.value is not None:
            self._load_tool(str(event.row_key.value))

    def on_rule_row_remove_requested(self, event: RuleRow.RemoveRequested) -> None:
        event.row.remove()

    def on_source_row_remove_requested(self, event: SourceRow.RemoveRequested) -> None:
        event.stop()
        event.row.remove()
        self._refresh_rule_sources()

    async def on_classification_rule_row_remove_requested(
        self, event: ClassificationRuleRow.RemoveRequested
    ) -> None:
        event.stop()
        # Removal is deferred; until it lands, the row no longer answers for
        # the form (ODR-69).
        event.row.removal_pending = True
        await event.row.remove()

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
            container.mount_later(
                CommandRow({"arguments": []}, index), when_present=".command-row"
            )
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
                SourceRow("", {"analysis": "", "filter": []}),
                when_present=".source-row",
            )
        elif button_id == "classification-add-rule":
            self.query_one("#classification-rules", MountTracked).mount_later(
                ClassificationRuleRow(
                    {"label": "", "source": "", "when": []}, self._source_names()
                ),
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
        elif button_id == "conda-save":
            self._start_conda_save()
        elif button_id == "tool-save":
            self._start_tool_save()
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
    #: the message names the controls and sends the reader to a reload (ODR-39).
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
        is why a save refuses instead (ODR-23).  Two signals cover it: every
        container that fills itself later says so through :class:`MountTracked`,
        and every row latches :attr:`ComposedRows.form_ready` in its own
        ``on_mount`` — which Textual runs only once the row's whole subtree
        (inputs and their children) is in the tree.  A third signal covers the
        one control that can still be half-alive inside a composed row: a
        ``Select`` whose own mount lookup failed reports
        :attr:`FittingSelect.options_ready` only once it has adopted its value
        (ODR-26).
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
        send the reader to a reload instead of telling them to try again (ODR-39).
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
                f"profile {name!r} already exists — opening it instead",
                severity="warning",
            )
            self._load_profile(name)
            return
        if kind == actions.CLASSIFICATION_KIND:
            self.classification_profile = name
            self.classification_doc = {
                "kind": actions.CLASSIFICATION_KIND,
                "version": 1,
                "description": "",
                "applies_to": {
                    "entity_type": "annotation",
                    "file_role": "protein_fasta",
                },
                "sources": {},
                "rules": [],
            }
            self._render_classification_form(
                name, dict(self.classification_doc), note="new profile (not saved yet)"
            )
            return
        if kind == actions.COVERAGE_KIND:
            self.coverage_profile = name
            self.coverage_doc = {
                "kind": actions.COVERAGE_KIND,
                "version": 1,
                "description": "",
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
                name, dict(self.coverage_doc), note="new profile (not saved yet)"
            )
            return
        self.current_profile = name
        self.profile_doc = {
            "kind": "qc",
            "version": 1,
            "description": "",
            "applies_to": ["assembly"],
            "required": [],
            "warnings": [],
        }
        self._render_profile_form(
            name, self.profile_doc, note="new profile (not saved yet)"
        )

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
        resolve the name from the visible editor instead (ODR-41).
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
                name,
                dict(document),
                note="restored from snapshot — saving creates the next version",
            )

        self.app.push_screen(
            HistoryModal(
                self.project,
                "profile",
                name,
                rows,
                fetch_snapshot=lambda sid: data.get_profile_snapshot(
                    self.project, name, sid
                ),
                to_editor=lambda document: document,
            ),
            lambda document: restore(document) if document else None,
        )

    def _open_classify(self) -> None:
        """Run the selected classification profile (reads it from disk)."""
        from operon.tui.screens.classify import ClassifyModal

        name = self.classification_profile
        if not name or not (self.project.profiles_dir / f"{name}.yaml").exists():
            self.app.notify(
                "save the classification profile before running it", severity="warning"
            )
            return
        self.app.push_screen(
            ClassifyModal(self.project, name, self.classification_doc),
            self._after_classify,
        )

    def _after_classify(self, payload: Any) -> None:
        if payload:
            self.reload()
