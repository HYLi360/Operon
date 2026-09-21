"""Classification-profile editor widgets.

``kind: sequence_classification`` profiles (grammar: ``operon/classify.py``,
documented in ``docs/en/guides/qc-profiles.md``) differ from qc profiles in
every structure the form has to model: ``applies_to`` is a mapping of
``entity_type``/``file_role``, ``sources`` map a name to an analysis with
``filter`` conditions and ``best_by`` ordering, and each rule either matches a
source with a ``when`` list, declares ``absent: true``, or acts as the
``default``.

Form boundary: flat conditions plus one level of ``any:`` groups and single
``not:`` negations can be edited.  A profile that nests deeper opens read-only
— the panel says so and offers no save — so the form never rewrites structure
it cannot represent.  Keys the form does not model are preserved verbatim at
every level (source, rule, condition and ``best_by`` entries), and the
composed document keeps the original key order.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button, Input, Select, Static

from operon.config import Project
from operon.tui import actions
from operon.tui.screens.common import (
    ComposedRows,
    FittingSelect,
    MountTracked,
    WriteModal,
    remount,
)

# Keep in sync with operon.classify._OPERATORS (asserted in
# tests/unit/test_tui_config.py); `like` is the case-insensitive SQL LIKE.
CLASSIFICATION_OPERATORS = (
    ">=", "<=", ">", "<", "==", "!=", "between", "in", "not_in", "exists", "like",
)
CONDITION_MODES = (("condition", "condition"), ("any of", "any"), ("not", "not"))
BEST_BY_DIRECTIONS = ("asc", "desc")
RULE_MODES = (("when", "when"), ("absent", "absent"), ("default", "default"))

CONDITION_MODELED_KEYS = frozenset({"field", "operator", "value", "values", "min", "max"})
BEST_BY_MODELED_KEYS = frozenset({"field", "direction", "rank", "default"})
SOURCE_MODELED_KEYS = frozenset({"analysis", "filter", "best_by"})
RULE_MODELED_KEYS = frozenset({"label", "source", "when", "absent", "default"})
# Document-level keys the form models; every other key is preserved verbatim
# and shown as a dim note (same round-trip rule as the qc editor).
CLASSIFICATION_MODELED_KEYS = frozenset(
    {"kind", "version", "description", "applies_to", "sources", "rules"}
)


def _extras(original: dict[str, Any], modeled: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in original.items() if key not in modeled}


def _extras_note(extras: dict[str, Any]) -> str:
    return "preserved as-is: " + ", ".join(str(key) for key in extras)


def _condition_value_text(condition: dict[str, Any]) -> str:
    """Render a condition's operand as the single text input shows it."""
    if "values" in condition and isinstance(condition["values"], list):
        return ", ".join(str(item) for item in condition["values"])
    if "min" in condition or "max" in condition:
        return f"{condition.get('min', '')}, {condition.get('max', '')}"
    value = condition.get("value")
    return "" if value is None else str(value)


def _condition_placeholder(operator: str) -> str:
    if operator == "between":
        return "min, max"
    if operator in {"in", "not_in"}:
        return "value, value, …"
    if operator == "like":
        return "pattern (case-insensitive LIKE, % wildcards)"
    if operator == "exists":
        return "(no value for 'exists')"
    return "value"


class ConditionRow(ComposedRows, Vertical):
    """One flat condition: field / operator / operand (+ verbatim extras)."""

    class RemoveRequested(Message):
        def __init__(self, row: ConditionRow) -> None:
            super().__init__()
            self.row = row

        @property
        def control(self) -> ConditionRow:
            return self.row

    def __init__(self, condition: dict[str, Any], *, removable: bool = True) -> None:
        super().__init__(classes="condition-row")
        self.original = dict(condition)
        self.extras = _extras(self.original, CONDITION_MODELED_KEYS)
        self.removable = removable

    def on_mount(self) -> None:
        self.mark_form_ready()

    def compose(self) -> ComposeResult:
        operator = str(self.original.get("operator", "=="))
        options = [(item, item) for item in CLASSIFICATION_OPERATORS]
        if operator not in CLASSIFICATION_OPERATORS:
            options.append((f"{operator} (unknown, preserved)", operator))
        with Horizontal(classes="condition-inputs"):
            yield Input(value=str(self.original.get("field", "")), placeholder="field",
                        classes="condition-field")
            yield FittingSelect(options, value=operator,
                                classes="condition-operator", allow_blank=False)
            yield Input(value=_condition_value_text(self.original),
                        placeholder=_condition_placeholder(operator), classes="condition-value")
            if self.removable:
                yield Button("✕", classes="condition-remove")
        if self.extras:
            yield Static(Text(_extras_note(self.extras), style="dim"), classes="condition-extras")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("condition-remove"):
            event.stop()
            self.post_message(self.RemoveRequested(self))

    def condition_document(self) -> dict[str, Any]:
        field = self.query_one(".condition-field", Input).value.strip()
        operator_value = self.query_one(".condition-operator", Select).value
        operator = "" if operator_value is Select.NULL else str(operator_value)
        text = self.query_one(".condition-value", Input).value.strip()
        operand: dict[str, Any] = {}
        if operator in {"in", "not_in"}:
            values = [actions.coerce_scalar(part) for part in text.split(",") if part.strip()]
            operand["values"] = values
        elif operator == "between":
            parts = [part.strip() for part in text.split(",")]
            lower = parts[0] if parts and parts[0] else ""
            upper = parts[1] if len(parts) > 1 else ""
            operand["min"] = actions.coerce_scalar(lower)
            operand["max"] = actions.coerce_scalar(upper)
        elif operator == "exists":
            pass
        else:
            operand["value"] = actions.coerce_scalar(text)
        document: dict[str, Any] = {}
        for key, original_value in self.original.items():
            if key in CONDITION_MODELED_KEYS:
                continue
            document[key] = original_value
        # Rebuild in the canonical shape: field, operator, operand, then extras
        # (dict insertion order follows the original keys where possible).
        ordered: dict[str, Any] = {}
        for key in self.original:
            if key in ("field", "operator"):
                ordered[key] = field if key == "field" else operator
        if "field" not in ordered:
            ordered["field"] = field
        if "operator" not in ordered:
            ordered["operator"] = operator
        ordered.update(operand)
        ordered.update(document)
        return ordered


class ConditionEditor(ComposedRows, Vertical):
    """A top-level condition: flat, one ``any:`` group, or one ``not:`` wrap."""

    class RemoveRequested(Message):
        def __init__(self, editor: ConditionEditor) -> None:
            super().__init__()
            self.editor = editor

        @property
        def control(self) -> ConditionEditor:
            return self.editor

    def __init__(self, condition: dict[str, Any]) -> None:
        super().__init__(classes="condition-editor")
        self.original = dict(condition)
        self.extras = _extras(self.original, CONDITION_MODELED_KEYS | {"any", "not"})
        self._mode = self._original_mode()
        #: The document the body was last seeded from.  A reader that lands while
        #: a mode change is replacing the body composes this (ODR-0035).
        self._seeded_document: dict[str, Any] = dict(condition)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="condition-inputs"):
            yield FittingSelect(list(CONDITION_MODES), value=self._mode,
                                 classes="condition-mode",
                         allow_blank=False)
            yield Button("✕", classes="condition-remove")
        yield MountTracked(classes="condition-body")
        if self.extras:
            yield Static(Text(_extras_note(self.extras), style="dim"), classes="condition-extras")

    def on_mount(self) -> None:
        self._rebuild(self._mode, self.original)
        self.mark_form_ready()

    def _original_mode(self) -> str:
        if "any" in self.original:
            return "any"
        if "not" in self.original:
            return "not"
        return "condition"

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.has_class("condition-mode"):
            event.stop()
            mode = "condition" if event.value is Select.NULL else str(event.value)
            if mode == self._mode:
                return
            self._mode = mode
            self._rebuild(mode, self.editor_document())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("condition-remove"):
            event.stop()
            self.post_message(self.RemoveRequested(self))
        elif event.button.has_class("condition-add"):
            event.stop()
            self.query_one(".condition-group", MountTracked).mount_later(
                ConditionRow({"field": ""}), when_present=".condition-row",
            )

    def on_condition_row_remove_requested(self, event: ConditionRow.RemoveRequested) -> None:
        event.stop()
        event.row.remove()

    def _rebuild(self, mode: str, document: dict[str, Any]) -> None:
        """Swap the body to the widgets the mode needs, seeding from ``document``."""
        body = self.query_one(".condition-body", Vertical)
        self._seeded_document = dict(document)
        if mode == "any":
            group = document.get("any") if isinstance(document.get("any"), list) else []
            rows = [item for item in group if isinstance(item, dict)] or [{"field": ""}]
            remount(
                body,
                MountTracked(*[ConditionRow(row) for row in rows], classes="condition-group"),
                Button("add condition", classes="condition-add"),
            )
        else:
            leaf = document
            if mode == "not" and isinstance(document.get("not"), dict):
                leaf = document["not"]
            # A leaf body is what both group modes fall back to.  ``editor_document``
            # reports the mode that was just selected, so a mode change hands in a
            # document that already carries the new group key and this branch is
            # the editor's own last line of defence: the panel's
            # ``classification_form_supported`` gate opens anything else read-only.
            if not isinstance(leaf, dict) or "any" in leaf or "not" in leaf:
                leaf = {"field": ""}
            remount(body, ConditionRow(dict(leaf), removable=False))

    def _composable_rows(self, selector: str) -> list[ConditionRow]:
        """The rows under *selector* that a composition may read.

        ``Widget.remove()`` calls ``App._prune``, which marks the node and its whole
        subtree with ``_pruning`` before the children go, and the row leaves the
        tree only after Textual has pruned it.  A composition that lands in between
        would read a row whose inputs are gone — or worse, a control that still
        answers with a blank value (ODR-0026) — and ``form_ready`` latches one-way
        on purpose (ODR-0023), so the panel's readiness gate cannot see the window.
        Such a row is on its way out: leaving it out of the document is what the
        removal asks for.
        """
        return [row for row in self.query(selector).results(ConditionRow)
                if not row._pruning and row.query(".condition-field")]

    def editor_document(self) -> dict[str, Any]:
        """Compose the condition this editor edits.

        A mode change replaces the body through ``MountTracked.replace_children``,
        which retires the old rows and mounts the next generation a turn later:
        while that is in flight the body holds nothing to compose.  The container
        answers "are my rows there yet" through ``mounts_settled`` (ODR-0023), so a
        reader that lands in the window — a save pressed in the same turn as the
        mode change — gets the document the replacement was seeded from, instead of
        a ``NoMatches`` in leaf/not mode or an empty ``any:`` group that would drop
        the condition (ODR-0035).  A row that is being removed right now is skipped
        rather than read (ODR-0036).
        """
        mode_value = self.query_one(".condition-mode", Select).value
        mode = "condition" if mode_value is Select.NULL else str(mode_value)
        body = self.query_one(".condition-body", MountTracked)
        if not body.mounts_settled:
            document: dict[str, Any] = dict(self._seeded_document)
        elif mode == "any":
            rows = self._composable_rows(".condition-group .condition-row")
            document = {"any": [row.condition_document() for row in rows]}
        else:
            rows = self._composable_rows(".condition-body .condition-row")
            if not rows:
                document = dict(self._seeded_document)
            else:
                leaf = rows[0].condition_document()
                document = {"not": leaf} if mode == "not" else leaf
        for key, value in self.extras.items():
            document.setdefault(key, value)
        return document


class BestByRow(ComposedRows, Vertical):
    """One ``best_by`` entry: field / direction / optional rank map / default."""

    class RemoveRequested(Message):
        def __init__(self, row: BestByRow) -> None:
            super().__init__()
            self.row = row

        @property
        def control(self) -> BestByRow:
            return self.row

    def __init__(self, entry: dict[str, Any]) -> None:
        super().__init__(classes="bestby-row")
        self.original = dict(entry)
        self.extras = _extras(self.original, BEST_BY_MODELED_KEYS)

    def on_mount(self) -> None:
        self.mark_form_ready()

    def compose(self) -> ComposeResult:
        direction = str(self.original.get("direction", "asc") or "asc")
        options = [(item, item) for item in BEST_BY_DIRECTIONS]
        if direction not in BEST_BY_DIRECTIONS:
            options.append((f"{direction} (unknown, preserved)", direction))
        rank = self.original.get("rank")
        rank_text = ", ".join(f"{key}={value}" for key, value in rank.items()) if isinstance(rank, dict) else ""
        default = self.original.get("default")
        with Horizontal(classes="bestby-inputs"):
            yield Input(value=str(self.original.get("field", "")), placeholder="field",
                        classes="bestby-field")
            yield FittingSelect(options, value=direction,
                                classes="bestby-direction", allow_blank=False)
            yield Input(value=rank_text, placeholder="rank map: Value=rank, … (blank = none)",
                        classes="bestby-rank")
            yield Input(value="" if default is None else str(default),
                        placeholder="default (blank = none)", classes="bestby-default")
            yield Button("✕", classes="bestby-remove")
        if self.extras:
            yield Static(Text(_extras_note(self.extras), style="dim"), classes="bestby-extras")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("bestby-remove"):
            event.stop()
            self.post_message(self.RemoveRequested(self))

    def best_by_document(self) -> dict[str, Any]:
        field = self.query_one(".bestby-field", Input).value.strip()
        direction_value = self.query_one(".bestby-direction", Select).value
        direction = "asc" if direction_value is Select.NULL else str(direction_value)
        rank_text = self.query_one(".bestby-rank", Input).value.strip()
        default_text = self.query_one(".bestby-default", Input).value.strip()
        document: dict[str, Any] = {}
        for key, original_value in self.original.items():
            if key in BEST_BY_MODELED_KEYS:
                continue
            document[key] = original_value
        ordered: dict[str, Any] = {"field": field}
        # The core defaults a missing direction to 'asc'; emit it only when the
        # file had one or the editor diverges from that default, so a profile
        # that relied on the default round-trips unchanged.
        if "direction" in self.original or direction != "asc":
            ordered["direction"] = direction
        if rank_text:
            rank: dict[str, Any] = {}
            for part in rank_text.split(","):
                if not part.strip():
                    continue
                key, _, value = part.partition("=")
                # ``float`` rewrote whole ranks as ``0.0``/``1.0``, so a form
                # round trip did not reproduce the on-disk document (ODR-0026).
                rank[key.strip()] = actions.coerce_scalar(value.strip()) if value.strip() else 0
            ordered["rank"] = rank
        if default_text:
            ordered["default"] = actions.coerce_scalar(default_text)
        ordered.update(document)
        return ordered


class SourceRow(ComposedRows, Vertical):
    """One ``sources`` entry: name + analysis + filter conditions + best_by."""

    class RemoveRequested(Message):
        def __init__(self, row: SourceRow) -> None:
            super().__init__()
            self.row = row

        @property
        def control(self) -> SourceRow:
            return self.row

    def __init__(self, name: str, source: dict[str, Any]) -> None:
        super().__init__(classes="source-row")
        self.original_name = name
        self.original = dict(source)
        self.extras = _extras(self.original, SOURCE_MODELED_KEYS)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="source-inputs"):
            yield Input(value=self.original_name, placeholder="source name",
                        classes="source-name")
            yield Input(value=str(self.original.get("analysis", "")), placeholder="analysis",
                        classes="source-analysis")
            yield Button("✕", classes="source-remove")
        yield Static("filter conditions (AND-ed; empty = all rows)", classes="modal-label")
        yield MountTracked(classes="source-filter")
        yield Button("add filter condition", classes="source-add-filter")
        yield Static("best_by (empty = hit_rank ascending)", classes="modal-label")
        yield MountTracked(classes="source-bestby")
        yield Button("add best_by entry", classes="source-add-bestby")
        if self.extras:
            yield Static(Text(_extras_note(self.extras), style="dim"), classes="source-extras")

    def on_mount(self) -> None:
        filters = [item for item in self.original.get("filter") or [] if isinstance(item, dict)]
        if filters:
            self.query_one(".source-filter", MountTracked).mount_later(
                *[ConditionEditor(item) for item in filters],
                when_present=".condition-editor",
            )
        entries = [item for item in self.original.get("best_by") or [] if isinstance(item, dict)]
        if entries:
            self.query_one(".source-bestby", MountTracked).mount_later(
                *[BestByRow(item) for item in entries], when_present=".bestby-row",
            )
        self.mark_form_ready()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("source-remove"):
            event.stop()
            self.post_message(self.RemoveRequested(self))
        elif event.button.has_class("source-add-filter"):
            event.stop()
            self.query_one(".source-filter", MountTracked).mount_later(
                ConditionEditor({"field": ""}), when_present=".condition-editor",
            )
        elif event.button.has_class("source-add-bestby"):
            event.stop()
            self.query_one(".source-bestby", MountTracked).mount_later(
                BestByRow({"field": "hit_rank", "direction": "asc"}),
                when_present=".bestby-row",
            )

    def on_condition_editor_remove_requested(self, event: ConditionEditor.RemoveRequested) -> None:
        event.stop()
        event.editor.remove()

    def on_best_by_row_remove_requested(self, event: BestByRow.RemoveRequested) -> None:
        event.stop()
        event.row.remove()

    def source_document(self) -> tuple[str, dict[str, Any]]:
        name = self.query_one(".source-name", Input).value.strip()
        best_by = [
            row.best_by_document()
            for row in self.query(".source-bestby .bestby-row").results(BestByRow)
        ]
        document: dict[str, Any] = {
            "analysis": self.query_one(".source-analysis", Input).value.strip(),
            "filter": [
                editor.editor_document()
                for editor in self.query(".source-filter .condition-editor").results(ConditionEditor)
            ],
        }
        # ``best_by`` must be omitted when empty: the core defaults it to
        # hit_rank ascending and rejects an empty list.
        if best_by:
            document["best_by"] = best_by
        for key, value in self.extras.items():
            document[key] = value
        return name, document


class ClassificationRuleRow(ComposedRows, Vertical):
    """One ``rules`` entry: label + (source & when) | absent | default."""

    class RemoveRequested(Message):
        def __init__(self, row: ClassificationRuleRow) -> None:
            super().__init__()
            self.row = row

        @property
        def control(self) -> ClassificationRuleRow:
            return self.row

    def __init__(self, rule: dict[str, Any], source_names: Iterable[str]) -> None:
        super().__init__(classes="classrule-row")
        self.original = dict(rule)
        self.extras = _extras(self.original, RULE_MODELED_KEYS)
        self.source_names = list(source_names)

    def compose(self) -> ComposeResult:
        mode = self._original_mode()
        with Horizontal(classes="classrule-inputs"):
            yield Input(value=str(self.original.get("label", "")), placeholder="label",
                        classes="classrule-label")
            yield FittingSelect(list(RULE_MODES), value=mode,
                                classes="classrule-mode", allow_blank=False)
            yield FittingSelect(self._source_options(), classes="classrule-source",
                                allow_blank=True)
            yield Button("✕", classes="classrule-remove")
        yield Static("when (all conditions must hold)", classes="modal-label classrule-when-label")
        yield MountTracked(classes="classrule-when")
        yield Button("add when condition", classes="classrule-add-when")
        if self.extras:
            yield Static(Text(_extras_note(self.extras), style="dim"), classes="classrule-extras")

    def _original_mode(self) -> str:
        if self.original.get("default"):
            return "default"
        if self.original.get("absent"):
            return "absent"
        return "when"

    def _source_options(self) -> list[tuple[str, str]]:
        options = [(name, name) for name in self.source_names]
        source = self.original.get("source")
        if source is not None and str(source) not in self.source_names:
            options.append((f"{source} (unknown, preserved)", str(source)))
        return options

    def set_sources(self, source_names: Iterable[str]) -> None:
        """Refresh the source choices after the panel's sources changed.

        A row that has not composed yet keeps the list for :meth:`on_mount`,
        which reads ``source_names`` when it builds the Select.
        """
        self.source_names = list(source_names)
        try:
            select = self.query_one(".classrule-source", Select)
        except NoMatches:
            return
        current = select.value
        select.set_options(self._source_options())
        if current is not Select.NULL and str(current) in self.source_names:
            select.value = current

    def on_mount(self) -> None:
        source = self.original.get("source")
        if source is not None:
            self.query_one(".classrule-source", Select).value = str(source)
        conditions = [item for item in self.original.get("when") or [] if isinstance(item, dict)]
        if conditions:
            self.query_one(".classrule-when", MountTracked).mount_later(
                *[ConditionEditor(item) for item in conditions],
                when_present=".condition-editor",
            )
        self._sync_mode()
        self.mark_form_ready()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.has_class("classrule-mode"):
            event.stop()
            self._sync_mode()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("classrule-remove"):
            event.stop()
            self.post_message(self.RemoveRequested(self))
        elif event.button.has_class("classrule-add-when"):
            event.stop()
            self.query_one(".classrule-when", MountTracked).mount_later(
                ConditionEditor({"field": ""}), when_present=".condition-editor",
            )

    def on_condition_editor_remove_requested(self, event: ConditionEditor.RemoveRequested) -> None:
        event.stop()
        event.editor.remove()

    def _mode(self) -> str:
        value = self.query_one(".classrule-mode", Select).value
        return "when" if value is Select.NULL else str(value)

    def _sync_mode(self) -> None:
        """A default rule takes no source/when; absent takes no when list."""
        mode = self._mode()
        self.query_one(".classrule-source", Select).disabled = mode == "default"
        for widget in self.query(".classrule-when, .classrule-add-when, .classrule-when-label"):
            widget.display = mode == "when"

    def rule_document(self) -> dict[str, Any]:
        label = self.query_one(".classrule-label", Input).value.strip()
        mode = self._mode()
        document: dict[str, Any] = {"label": label}
        if mode == "default":
            document["default"] = True
        else:
            source_value = self.query_one(".classrule-source", Select).value
            document["source"] = "" if source_value is Select.NULL else str(source_value)
            if mode == "absent":
                document["absent"] = True
            else:
                document["when"] = [
                    editor.editor_document()
                    for editor in self.query(".classrule-when .condition-editor").results(ConditionEditor)
                ]
        for key, value in self.extras.items():
            document.setdefault(key, value)
        return document


def _leaf_condition(condition: Any) -> bool:
    return (isinstance(condition, dict) and "any" not in condition and "not" not in condition)


def _condition_within_depth(condition: Any) -> bool:
    """Flat conditions plus one level of ``any:``/``not:`` — deeper is YAML-only.

    Either the condition is a leaf, or a single ``any:``/``not:`` that holds
    leaves.  A ``not:`` of an ``any:`` (or any group inside a group) is deeper
    than the form represents, so it opens read-only rather than losing structure.
    """
    if not isinstance(condition, dict):
        return False
    if "any" in condition:
        group = condition["any"]
        return isinstance(group, list) and all(_leaf_condition(item) for item in group)
    if "not" in condition:
        return _leaf_condition(condition["not"])
    return _leaf_condition(condition)


def classification_form_supported(document: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(supported, reason)``: can the manual form represent this profile?

    The qc-free structures (``sources``/``rules`` collections, rule modes and
    one level of ``any:``/``not:``) are editable; anything deeper — nested
    groups, non-mapping conditions — opens read-only so the form never rewrites
    structure it cannot represent.
    """
    sources = document.get("sources")
    if sources is None:
        sources = {}
    if not isinstance(sources, dict):
        return False, "sources is not a mapping"
    for name, source in sources.items():
        if not isinstance(source, dict):
            return False, f"source {name!r} is not a mapping"
        for condition in source.get("filter") or []:
            if not _condition_within_depth(condition):
                return False, f"source {name!r} nests conditions deeper than one any:/not: level"
        for entry in source.get("best_by") or []:
            if not isinstance(entry, dict):
                return False, f"source {name!r} has a best_by entry that is not a mapping"
    rules = document.get("rules")
    if rules is None:
        rules = []
    if not isinstance(rules, list):
        return False, "rules is not a list"
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            return False, f"rule {index} is not a mapping"
        for condition in rule.get("when") or []:
            if not _condition_within_depth(condition):
                return False, f"rule {index} nests conditions deeper than one any:/not: level"
    return True, ""


class ClassificationSaveModal(WriteModal):
    """Confirm a classification-profile save: file path + version + snapshot."""

    def __init__(self, project: Project, name: str, document: dict[str, Any],
                 new_version: int) -> None:
        super().__init__(f"Save classification profile {name}")
        self.project = project
        self.profile_name = name
        self.document = document
        self.new_version = new_version

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            f"writes config/profiles/{self.profile_name}.yaml as kind "
            f"sequence_classification version {self.new_version} + records a "
            "content-addressed snapshot.  A classify run consumes the exact snapshot "
            "you save here.",
            classes="modal-info",
        )

    def command_text(self) -> str:
        return (f"config/profiles/{self.profile_name}.yaml → kind sequence_classification, "
                f"version {self.new_version} + qc_profiles snapshot")

    def confirm(self) -> None:
        self.run_action(
            lambda: actions.save_classification_profile(
                self.project, self.profile_name, self.document,
                known_version=self.new_version - 1,
            )
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
