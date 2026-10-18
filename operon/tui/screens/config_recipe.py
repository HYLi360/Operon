"""Recipe-editor block of the config screen.

:class:`RecipeEditor` is the recipe editor of
:class:`operon.tui.screens.config.ConfigPanel`, mixed in as a base class: the
state it uses (``current_recipe``, ``recipe_tool``, ``recipe_doc``,
``_command_row_count``, ``_slurm_extras``, ``_parameter_problems``) stays on
the panel, initialized by ``ConfigPanel.__init__``.  The split is pure code
motion — the render/compose/save flow, the inline validation that mirrors
``operon.tools.get_recipe``, the slurm overrides and the command-chain rows
behave exactly as before.

The module also owns the recipe vocabulary constants (``RECIPE_MODELED_ORDER``
and friends): keys the form does not model survive the round-trip verbatim,
and the document the form writes keeps the original key order.
"""

from __future__ import annotations

import re
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Input, Select, Static, TextArea

from operon.errors import ValidationError
from operon.tui import data
from operon.tui.screens.common import (
    ENTITY_TYPE_OPTIONS,
    FittingSelect,
    MountTracked,
    remount,
)
from operon.tui.screens.config_modals import HistoryModal, RecipeSaveModal
from operon.tui.screens.config_rows import (
    _PARAM_NAME_RE,
    CommandRow,
    _extras_note,
    _format_parameter_line,
    _parse_parameter_line,
)

RESULT_PARSERS = (
    "none",
    "blast_tabular",
    "hmmer_tblout",
    "hmmer_domtblout",
    "rpsbproc_tabular",
    "busco_json",
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

RECIPE_MODELED_ORDER = (
    "description",
    "entity_type",
    "file_role",
    "file_role_prefix",
    "format",
    "input_kind",
    "output_kind",
    "database",
    "database_version",
    "database_mode",
    "database_checksum",
    "environment_policy",
    "output_subdir",
    "output_suffix",
    "output_name",
    "arguments",
    "commands",
    "parameters",
    "slurm",
    "result_parser",
    "result_glob",
    "hmmer_mode",
    "result_columns",
    "hit_metric_columns",
    "query_column",
    "subject_column",
    "numeric_columns",
    "qstart_column",
    "qend_column",
    "sstart_column",
    "send_column",
    "evalue_column",
    "bitscore_column",
    "pident_column",
    "max_hits_per_query",
)
RECIPE_MODELED_KEYS = frozenset(RECIPE_MODELED_ORDER) | {"version"}
#: The spec keys a single parameter line models; every other key a spec carries
#: is preserved verbatim (and listed under the editor).
PARAMETER_MODELED_KEYS = frozenset({"default", "required", "choices", "pattern"})

_OMIT = object()


class RecipeEditor:
    """The recipe editor of ``ConfigPanel``, mixed in as a base class.

    The state these methods use (``current_recipe``, ``recipe_tool``,
    ``recipe_doc``, ``_command_row_count``, ``_slurm_extras``,
    ``_parameter_problems``) stays on the panel: ``ConfigPanel.__init__``
    initializes it.  Nothing here overrides ``Panel``; the split is pure
    code motion, so the handlers and helpers read exactly as before.
    """

    def _compose_recipe_editor(self) -> ComposeResult:
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
                placeholder="file_role_prefix (mutually exclusive with file_role)",
                id="recipe-file-role-prefix",
            )
            yield Input(placeholder="format", id="recipe-format")
            yield Static("Input kind (blank = key absent)", classes="modal-label")
            yield FittingSelect(
                [(kind, kind) for kind in ARTIFACT_KINDS],
                id="recipe-input-kind",
                allow_blank=True,
            )
            yield Static("Output kind (blank = key absent)", classes="modal-label")
            yield FittingSelect(
                [(kind, kind) for kind in ARTIFACT_KINDS],
                id="recipe-output-kind",
                allow_blank=True,
            )
            yield Input(placeholder="database", id="recipe-database")
            yield Input(placeholder="database_version", id="recipe-database-version")
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
                "Environment policy (blank = key absent; core default 'warn')",
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
            yield Input(placeholder="slurm partition", id="recipe-slurm-partition")
            yield Input(
                placeholder="slurm time (e.g. 24:00:00)",
                id="recipe-slurm-time",
            )
            yield Input(placeholder="slurm mem_gb", id="recipe-slurm-mem-gb")
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
            yield Input(placeholder="output_subdir", id="recipe-output-subdir")
            yield Input(placeholder="output_suffix", id="recipe-output-suffix")
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
            yield Static("HMMER mode (blank = key absent)", classes="modal-label")
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
            yield Input(placeholder="query_column", id="recipe-query-column")
            yield Input(placeholder="subject_column", id="recipe-subject-column")
            yield Input(
                placeholder="numeric_columns (comma separated)",
                id="recipe-numeric-columns",
            )
            yield Input(placeholder="qstart_column", id="recipe-qstart-column")
            yield Input(placeholder="qend_column", id="recipe-qend-column")
            yield Input(placeholder="sstart_column", id="recipe-sstart-column")
            yield Input(placeholder="send_column", id="recipe-send-column")
            yield Input(placeholder="evalue_column", id="recipe-evalue-column")
            yield Input(placeholder="bitscore_column", id="recipe-bitscore-column")
            yield Input(placeholder="pident_column", id="recipe-pident-column")
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

    def _render_recipe_form(
        self, name: str, document: dict[str, Any], note: str = ""
    ) -> None:
        self.query_one("#recipe-heading", Static).update(
            f"{self.recipe_tool}.{name}" + (f"  —  {note}" if note else "")
        )
        self.query_one("#recipe-description", Input).value = str(
            document.get("description", "")
        )
        entity_type = str(document.get("entity_type", "") or "")
        entity_select = self.query_one("#recipe-entity-type", Select)
        entity_options = list(ENTITY_TYPE_OPTIONS)
        if entity_type and entity_type not in dict(ENTITY_TYPE_OPTIONS):
            entity_options.append((f"{entity_type} (preserved)", entity_type))
            entity_select.set_options(entity_options)
        entity_select.value = entity_type if entity_type else Select.NULL
        self.query_one("#recipe-file-role", Input).value = str(
            document.get("file_role", "") or ""
        )
        self.query_one("#recipe-file-role-prefix", Input).value = str(
            document.get("file_role_prefix", "") or ""
        )
        self.query_one("#recipe-format", Input).value = str(
            document.get("format", "") or ""
        )
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
        self.query_one("#recipe-database", Input).value = str(
            document.get("database", "") or ""
        )
        self.query_one("#recipe-database-version", Input).value = str(
            document.get("database_version", "") or ""
        )
        self.query_one("#recipe-database-checksum", Input).value = str(
            document.get("database_checksum", "") or ""
        )
        self.query_one("#recipe-output-subdir", Input).value = str(
            document.get("output_subdir", "") or ""
        )
        self.query_one("#recipe-output-suffix", Input).value = str(
            document.get("output_suffix", "") or ""
        )
        self.query_one("#recipe-output-name", Input).value = str(
            document.get("output_name", "") or ""
        )
        arguments = document.get("arguments", []) or []
        self.query_one("#recipe-arguments", TextArea).text = "\n".join(
            str(a) for a in arguments
        )
        parameters = document.get("parameters", {}) or {}
        lines = []
        preserved_specs = []
        for param_name, spec in parameters.items():
            spec = spec if isinstance(spec, dict) else {}
            lines.append(_format_parameter_line(param_name, spec))
            extra_keys = sorted(set(spec) - PARAMETER_MODELED_KEYS)
            if extra_keys:
                preserved_specs.append(f"{param_name}: {', '.join(extra_keys)}")
        self.query_one("#recipe-parameters", TextArea).text = "\n".join(lines)
        self.query_one("#recipe-parameters-note", Static).update(
            Text("preserved spec keys — " + "; ".join(preserved_specs), style="dim")
            if preserved_specs
            else ""
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
        for key, widget_id in (
            ("result_glob", "#recipe-result-glob"),
            ("query_column", "#recipe-query-column"),
            ("subject_column", "#recipe-subject-column"),
            ("qstart_column", "#recipe-qstart-column"),
            ("qend_column", "#recipe-qend-column"),
            ("sstart_column", "#recipe-sstart-column"),
            ("send_column", "#recipe-send-column"),
            ("evalue_column", "#recipe-evalue-column"),
            ("bitscore_column", "#recipe-bitscore-column"),
            ("pident_column", "#recipe-pident-column"),
        ):
            self.query_one(widget_id, Input).value = str(document.get(key, "") or "")
        for key, widget_id in (
            ("result_columns", "#recipe-result-columns"),
            ("hit_metric_columns", "#recipe-hit-metric-columns"),
            ("numeric_columns", "#recipe-numeric-columns"),
        ):
            columns = document.get(key, []) or []
            self.query_one(widget_id, Input).value = ", ".join(str(c) for c in columns)
        max_hits = document.get("max_hits_per_query")
        self.query_one("#recipe-max-hits", Input).value = (
            "" if max_hits is None else str(max_hits)
        )
        self.query_one("#recipe-save-error", Static).update("")
        extras = {
            key: value
            for key, value in document.items()
            if key not in RECIPE_MODELED_KEYS
        }
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
        self._slurm_extras = {
            key: value for key, value in data.items() if key not in modeled
        }
        for key, widget_id, kind in RECIPE_SLURM_FIELDS:
            value = data.get(key)
            if kind == "lines":
                self.query_one(widget_id, TextArea).text = (
                    "\n".join(str(item) for item in value)
                    if isinstance(value, list)
                    else ""
                )
            elif kind == "tri":
                self.query_one(widget_id, Select).value = (
                    Select.NULL if value is None else ("true" if value else "false")
                )
            else:
                self.query_one(widget_id, Input).value = (
                    "" if value is None else str(value)
                )
        note = self.query_one("#recipe-slurm-note", Static)
        note.update(
            Text(
                "preserved slurm keys — " + ", ".join(sorted(self._slurm_extras)),
                style="dim",
            )
            if self._slurm_extras
            else ""
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
                lines = [
                    line.strip()
                    for line in self.query_one(widget_id, TextArea).text.splitlines()
                    if line.strip()
                ]
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
            for index, block in enumerate(
                blocks if isinstance(blocks, list) else [], start=1
            )
            if isinstance(block, dict)
        ]
        remount(self.query_one("#recipe-command-list", MountTracked), *rows)
        # The panel counts the chain itself: the replacement lands a turn later,
        # so neither the note nor a reader may trust the DOM for it.
        self._command_row_count = len(rows)
        self._refresh_command_note()

    def _renumber_command_rows(self, removed: CommandRow | None = None) -> None:
        """Renumber the chain, skipping the row that is on its way out.

        ``remove()`` prunes asynchronously, so on a busy event loop the row it
        removes can still be in the tree when the chain is renumbered — and
        numbering every row found then hands the *survivor* the removed row's
        number, which is how a two-step chain that lost its head ended up
        labelled ``step 2`` (ODR-56).  The row being removed is therefore
        excluded explicitly.
        """

        for index, row in enumerate(
            (
                row
                for row in self.query(CommandRow).results(CommandRow)
                if row is not removed
            ),
            start=1,
        ):
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
            note_view.update(
                Text(
                    f"'commands' and 'arguments' are mutually exclusive — clear one "
                    f"before saving ({row_count} step(s) defined)",
                    style="yellow",
                )
            )
            return
        note_view.update(
            Text(
                f"{row_count} step(s); every step runs in the parent tool's single "
                "run_method environment",
                style="dim",
            )
        )

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
        self._parameter_problems = []
        for index, line in enumerate(
            self.query_one("#recipe-parameters", TextArea).text.splitlines(), start=1
        ):
            parsed = _parse_parameter_line(line, original_parameters)
            if parsed is None:
                continue
            parameters[parsed[0]] = parsed[1]
            self._parameter_problems.extend(
                f"line {index}: {problem}" for problem in parsed[2]
            )
        columns = [
            column.strip()
            for column in self.query_one("#recipe-result-columns", Input).value.split(
                ","
            )
            if column.strip()
        ]
        hit_columns = [
            column.strip()
            for column in self.query_one(
                "#recipe-hit-metric-columns", Input
            ).value.split(",")
            if column.strip()
        ]
        numeric_columns = [
            column.strip()
            for column in self.query_one("#recipe-numeric-columns", Input).value.split(
                ","
            )
            if column.strip()
        ]
        max_hits_text = self.query_one("#recipe-max-hits", Input).value.strip()
        max_hits: Any = _OMIT
        if max_hits_text:
            try:
                max_hits = int(max_hits_text)
            except ValueError:
                max_hits = (
                    max_hits_text  # save_recipe round-trip rejects with a clear error
                )

        new_values: dict[str, Any] = {}
        for key, widget_id in (
            ("description", "#recipe-description"),
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
            ("pident_column", "#recipe-pident-column"),
        ):
            value = self.query_one(widget_id, Input).value.strip()
            new_values[key] = value if value or key in original else _OMIT
        for key, widget_id in (
            ("input_kind", "#recipe-input-kind"),
            ("output_kind", "#recipe-output-kind"),
            ("environment_policy", "#recipe-environment-policy"),
            ("hmmer_mode", "#recipe-hmmer-mode"),
            ("database_mode", "#recipe-database-mode"),
        ):
            select_value = self.query_one(widget_id, Select).value
            text = "" if select_value is Select.NULL else str(select_value)
            new_values[key] = text if text or key in original else _OMIT
        new_values["entity_type"] = (
            entity_type if entity_type or "entity_type" in original else _OMIT
        )
        new_values["arguments"] = (
            arguments if arguments or "arguments" in original else _OMIT
        )
        commands = [
            row.command_document() for row in self.query(CommandRow).results(CommandRow)
        ]
        new_values["commands"] = (
            commands if commands or "commands" in original else _OMIT
        )
        slurm_values = self._compose_slurm_document()
        new_values["slurm"] = (
            slurm_values if slurm_values or "slurm" in original else _OMIT
        )
        new_values["parameters"] = (
            parameters if parameters or "parameters" in original else _OMIT
        )
        new_values["result_parser"] = parser
        new_values["result_columns"] = (
            columns if columns or "result_columns" in original else _OMIT
        )
        new_values["hit_metric_columns"] = (
            hit_columns if hit_columns or "hit_metric_columns" in original else _OMIT
        )
        new_values["numeric_columns"] = (
            numeric_columns
            if numeric_columns or "numeric_columns" in original
            else _OMIT
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
            version = int(
                data.get_recipe_document(self.project, name)["document"].get(
                    "version", 1
                )
            )
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

    async def on_command_row_remove_requested(
        self, event: CommandRow.RemoveRequested
    ) -> None:
        event.stop()
        # Removal is deferred, so renumber and re-count only once it has landed.
        await event.row.remove()
        self._command_row_count = max(0, self._command_row_count - 1)
        self._renumber_command_rows(event.row)
        self._refresh_command_note()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "recipe-arguments":
            self._refresh_command_note()

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
            error.update(
                Text(
                    f"analysis {self.current_recipe!r}: 'commands' and 'arguments' "
                    "are mutually exclusive",
                    style="red",
                )
            )
            return
        for index, block in enumerate(commands, start=1):
            if not block.get("arguments"):
                error.update(
                    Text(
                        f"analysis {self.current_recipe!r}: commands block {index} "
                        "requires a non-empty 'arguments' list",
                        style="red",
                    )
                )
                return
            if block.get("version_pattern") and not block.get("version_args"):
                error.update(
                    Text(
                        f"analysis {self.current_recipe!r}: commands block {index} "
                        "version_pattern requires version_args",
                        style="red",
                    )
                )
                return
        if self._parameter_problems:
            error.update(
                Text(
                    f"analysis {self.current_recipe!r}: "
                    + "; ".join(self._parameter_problems),
                    style="red",
                )
            )
            return
        for name, spec in (document.get("parameters") or {}).items():
            if _PARAM_NAME_RE.fullmatch(name) is None:
                error.update(
                    Text(
                        f"analysis {self.current_recipe!r}: invalid parameter name {name!r}",
                        style="red",
                    )
                )
                return
            pattern = str(spec.get("pattern", "") or "")
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    error.update(
                        Text(
                            f"analysis {self.current_recipe!r}: parameter {name!r} pattern "
                            f"is not a valid regular expression ({exc})",
                            style="red",
                        )
                    )
                    return
        slurm = document.get("slurm") or {}
        for key, kind, message in (
            ("mem_gb", int, "slurm mem_gb must be an integer >= 0"),
            ("poll_interval", float, "slurm poll_interval must be a number > 0"),
            (
                "array_concurrency",
                int,
                "slurm array_concurrency must be a positive integer",
            ),
        ):
            value = slurm.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, kind):
                error.update(
                    Text(f"analysis {self.current_recipe!r}: {message}", style="red")
                )
                return
            if (
                (key == "mem_gb" and value < 0)
                or (key == "array_concurrency" and value < 1)
                or (key == "poll_interval" and value <= 0)
            ):
                error.update(
                    Text(f"analysis {self.current_recipe!r}: {message}", style="red")
                )
                return
        if (
            str(document.get("database_mode", "")).strip() == "mutable_cache"
            and not str(document.get("database_version", "")).strip()
        ):
            error.update(
                Text(
                    f"analysis {self.current_recipe!r}: mutable_cache requires an "
                    "explicit database_version",
                    style="red",
                )
            )
            return
        if (
            str(document.get("file_role", "")).strip()
            and str(document.get("file_role_prefix", "")).strip()
        ):
            error.update(
                Text(
                    f"analysis {self.current_recipe!r}: 'file_role' and 'file_role_prefix' "
                    "are mutually exclusive",
                    style="red",
                )
            )
            return
        error.update("")
        file_version = self._recipe_file_version(self.current_recipe)
        new_version = 1 if file_version is None else file_version + 1
        self.app.push_screen(
            RecipeSaveModal(
                self.project,
                self.recipe_tool,
                self.current_recipe,
                document,
                new_version,
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
                name,
                self.recipe_doc,
                note="restored from snapshot — saving creates the next version",
            )

        self.app.push_screen(
            HistoryModal(
                self.project,
                "recipe",
                name,
                rows,
                fetch_snapshot=lambda sid: data.get_recipe_snapshot(
                    self.project, name, sid
                ),
                to_editor=lambda document: dict(document.get("recipe", {})),
            ),
            lambda document: restore(document) if document else None,
        )

    def _open_run_analysis(self) -> None:
        if not self.current_recipe:
            return
        from operon.tui.screens.analyze import AnalyzeModal, analysis_finished

        self.app.push_screen(
            AnalyzeModal(self.project, recipe_name=self.current_recipe),
            lambda payload: analysis_finished(self.app, payload),
        )
