"""Row widgets and the parameter-line grammar of the config screen editors.

``RuleRow`` renders one qc-profile rule (metric / operator / value / code) and
``CommandRow`` one step of a recipe ``commands`` chain; both keep every key
the form does not model verbatim, shown as a dim note.  The ``_format`` /
``_parse`` pair is the line grammar of the recipe editor's ``parameters``
field: ``name=default; required; choices=a,b; pattern=<regex>``.
"""

from __future__ import annotations

import re
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Input, Select, Static, TextArea

from operon.tui import actions
from operon.tui.screens.common import ComposedRows, FittingSelect

OPERATOR_OPTIONS = [(operator, operator) for operator in actions.PROFILE_OPERATORS]
RULE_MODELED_KEYS = frozenset({"metric", "operator", "value", "code"})


def _extras_note(extras: dict[str, Any]) -> str:
    return "preserved as-is: " + ", ".join(str(key) for key in extras)


#: A trailing `;`-segment of a parameter line that the form understands.  A
#: line whose trailing segments do not all match is read as a plain
#: `name=default` (so a default value may contain `;`), and `pattern=` swallows
#: the rest of the line, because a regex may contain any separator.
_PARAM_FLAG_RE = re.compile(
    r"^(required(\s*=\s*(true|false|yes|no))?|choices\s*=|pattern\s*=)",
    re.IGNORECASE,
)
_PARAM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _format_parameter_line(name: str, spec: dict[str, Any]) -> str:
    """One parameter as the editor's line: `name=default; required; …`."""
    default = spec.get("default")
    line = f"{name}={'' if default is None else default}"
    if bool(spec.get("required", False)):
        line += "; required"
    choices = spec.get("choices")
    if isinstance(choices, list) and choices:
        line += "; choices=" + ",".join(str(choice) for choice in choices)
    pattern = str(spec.get("pattern", "") or "")
    if pattern:
        line += "; pattern=" + pattern
    return line


def _parse_parameter_line(
    line: str, originals: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any], list[str]] | None:
    """Read one editor line back into ``(name, spec, problems)``.

    ``None`` means a blank line.  *originals* is the recipe's whole parameters
    mapping: every spec key the line does not model is carried over from that
    parameter's own spec, while the modeled ones (`default`, `required`,
    `choices`, `pattern`) are authoritative — the line shows exactly what will
    be written, so deleting a flag deletes the key.

    The flag section is everything after the first ``;``, and it must open with a
    flag.  A later segment the grammar does not know is reported in *problems*
    (the form refuses to save rather than write it) so a typo can never turn into
    part of a default value; a ``;`` in a default value stays a default value as
    long as the segment after it is not flag-shaped.  ``pattern=`` owns the rest
    of the line, because a regex may contain ``;``.
    """
    line = line.strip()
    if not line:
        return None
    segments = [segment.strip() for segment in line.split(";")]
    head = segments[0]
    flags: list[str] = []
    problems: list[str] = []
    if len(segments) > 1:
        if _PARAM_FLAG_RE.match(segments[1]):
            for segment in segments[1:]:
                if not _PARAM_FLAG_RE.match(segment):
                    problems.append(f"unrecognized parameter flag {segment!r}")
                    break
                flags.append(segment)
                if segment.lower().startswith("pattern"):
                    break  # the regex owns the rest of the line
        else:
            head = line  # the `;` belongs to the default value
    param_name, separator, default_text = head.partition("=")
    param_name = param_name.strip()
    if not param_name:
        return None
    spec = dict((originals or {}).get(param_name) or {})
    if separator and default_text.strip():
        spec["default"] = actions.coerce_scalar(default_text)
    else:
        spec.pop("default", None)
    if problems:
        return param_name, spec, problems
    spec.pop("required", None)
    spec.pop("choices", None)
    spec.pop("pattern", None)
    for segment in flags:
        lowered = segment.lower()
        if lowered.startswith("required"):
            _, _, flag_value = segment.partition("=")
            spec["required"] = (
                flag_value.strip().lower() not in {"false", "no"}
                if flag_value.strip()
                else True
            )
        elif lowered.startswith("choices"):
            _, _, listed = segment.partition("=")
            choices = [choice.strip() for choice in listed.split(",") if choice.strip()]
            if choices:
                spec["choices"] = choices
    if flags:
        marker = re.search(r"(?:^|;)\s*pattern\s*=", line[len(head) :], re.IGNORECASE)
        if marker is not None:
            pattern = line[len(head) :][marker.end() :].strip()
            if pattern:
                spec["pattern"] = pattern
    return param_name, spec, problems


class RuleRow(ComposedRows, Vertical):
    """One editable rule row: metric / operator / value / code + remove button.

    Rule keys the form does not model (``value_by``, ``source``, ``unknown``,
    ``unknown_code``, ``min``/``max``/``values``, …) are kept verbatim and
    rendered as a dim note below the inputs.

    The operator control is a :class:`FittingSelect` because this is one of the
    rows ``remount`` rebuilds: the guarded subclass retries Textual's own
    mount-phase lookups, adopts the value it was built with and advertises
    ``options_ready``, which ``ConfigPanel._form_mounting`` reads for every
    ``FittingSelect`` in the form (ODR-23, ODR-26).  The recipe editor's and
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
        self.extras = {
            key: value for key, value in rule.items() if key not in RULE_MODELED_KEYS
        }

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
                placeholder="metric",
                classes="rule-metric",
            )
            yield FittingSelect(
                options, value=operator, classes="rule-operator", allow_blank=False
            )
            yield Input(
                value="" if value is None else str(value),
                placeholder="value",
                classes="rule-value",
            )
            yield Input(
                value=str(self.original.get("code", "")),
                placeholder="code",
                classes="rule-code",
            )
            yield Button("✕", classes="rule-remove")
        if self.extras:
            yield Static(
                Text(_extras_note(self.extras), style="dim"), classes="rule-extras"
            )

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
                if isinstance(version_args, list)
                else ""
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
        version_args = [
            part.strip() for part in version_text.split(",") if part.strip()
        ]
        version_pattern = self.query_one(
            ".command-version-pattern", Input
        ).value.strip()
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
