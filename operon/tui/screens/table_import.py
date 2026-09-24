"""Import table dialog: controlled CSV/XLSX metadata templates and imports.

Mirrors ``operon import table`` (Home screen): generate an empty CSV/XLSX
template for one of the six importable tables, or preview and import a file.
The preview is the mandatory preflight before Confirm (the FanoutModal
pattern): it writes nothing, fills the key/action/changed-fields table, and
any edit to the table, mode or file path locks Confirm again; a stale answer
delivered after a form change is discarded (the NcbiDatasetsModal pattern).
Confirm applies the same plan the CLI would — through
:func:`operon.tui.actions.import_table`, which re-runs the preview inside the
writable session — so audit rows and state transitions are identical to
``--yes``.  The apply is a single short transaction with no cooperative
cancel, so a running form refuses to close (the RunExternalModal pattern;
Cancel is additionally guarded against Textual's MRO double dispatch,
ODR-0043).
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import Any

from rich.text import Text
from textual import work
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Button, DataTable, Input, Select, Static

from operon.config import Project
from operon.table_import import IMPORTABLE_TABLES
from operon.tui import actions
from operon.tui.screens.common import WriteModal

ON_CONFLICT_OPTIONS = [
    ("error (abort when rows would change)", "error"),
    ("skip (keep existing rows)", "skip"),
    ("update (audited overwrite)", "update"),
]


class ImportTableModal(WriteModal):
    """Form + mandatory preview + confirm for ``operon import table``."""

    def __init__(self, project: Project) -> None:
        super().__init__("Import table")
        self.project = project
        self.preview: dict[str, Any] | None = None
        self.preview_values: dict[str, Any] = {}
        self._preview_snapshot: dict[str, Any] = {}
        self.preview_running = False
        self.running = False
        self._run_kind = ""

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            "controlled metadata tables: generate an empty CSV/XLSX template, or "
            "preview and import a file.  The preview is the mandatory preflight "
            "for an import — it writes nothing; Confirm applies the same plan "
            "the CLI would, with the same changes audit rows.",
            classes="modal-info",
        )
        yield Select(
            [(table, table) for table in IMPORTABLE_TABLES],
            value="organisms",
            allow_blank=False,
            id="table-table",
        )
        yield Select(
            [("Import file (preview, then apply)", "import"),
             ("Generate template", "template")],
            value="import",
            allow_blank=False,
            id="table-mode",
        )
        yield Input(placeholder="input .csv/.xlsx path (--file)", id="table-file")
        yield Input(placeholder="template output .csv/.xlsx path (--template)",
                    id="table-template-out")
        yield Select(
            ON_CONFLICT_OPTIONS,
            prompt="on-conflict (blank = the CLI default)",
            allow_blank=True,
            id="table-on-conflict",
        )
        with Horizontal(classes="config-buttons"):
            yield Button("Preview import", id="table-preview-button")
        yield Static("", id="table-status", classes="modal-info")
        yield DataTable(id="table-preview-table", cursor_type="row")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#table-preview-table", DataTable).add_columns(
            "key", "action", "changed_fields")
        self.set_confirm_enabled(False)

    # -- form values ---------------------------------------------------------

    def _values(self) -> dict[str, Any]:
        mode = self.query_one("#table-mode", Select).value
        on_conflict = self.query_one("#table-on-conflict", Select).value
        return {
            "mode": "import" if mode is Select.NULL else str(mode),
            "table": str(self.query_one("#table-table", Select).value),
            "file": self.query_one("#table-file", Input).value.strip(),
            "template_out": self.query_one("#table-template-out", Input).value.strip(),
            "on_conflict": "" if on_conflict is Select.NULL else str(on_conflict),
        }

    @staticmethod
    def _preview_relevant(values: dict[str, Any]) -> dict[str, Any]:
        """The subset of the form the preview content depends on."""
        return {key: values[key] for key in ("mode", "table", "file")}

    def command_text(self) -> str:
        values = self._values()
        parts = ["operon", "import", "table", "--table", values["table"]]
        if values["mode"] == "template":
            parts += ["--template", shlex.quote(values["template_out"] or "…")]
        else:
            parts += ["--file", shlex.quote(values["file"] or "…")]
            if values["on_conflict"]:
                parts += ["--on-conflict", values["on_conflict"]]
        return " ".join(parts)

    # -- change tracking -----------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("table-"):
            self._invalidate_preview()
            self.refresh_command()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id and event.select.id.startswith("table-"):
            if event.select.id == "table-mode":
                self._apply_mode()
            self._invalidate_preview()
            self.refresh_command()

    def _apply_mode(self) -> None:
        if self.running or self.preview_running:
            return
        self.query_one("#table-preview-button", Button).disabled = \
            self._values()["mode"] == "template"
        self.set_confirm_enabled(self._confirm_allowed())

    def _invalidate_preview(self) -> None:
        # Only a real edit invalidates the preview: a queued/repeated change
        # event carrying the same values must not discard a fresh preview.
        # on-conflict is deliberately excluded — the CLI picks it after the
        # preview, so choosing it here must not lock Confirm again.
        if self.preview is None:
            return
        if self._preview_relevant(self._values()) == self._preview_relevant(self.preview_values):
            self.refresh_command()
            return
        self.preview = None
        self.set_confirm_enabled(self._confirm_allowed())
        self.query_one("#table-preview-table", DataTable).clear()
        self.query_one("#table-status", Static).update(
            Text("form changed — run the preview again", style="yellow"))

    def _confirm_allowed(self) -> bool:
        if self.running or self.preview_running:
            return False
        values = self._values()
        return values["mode"] == "template" or self.preview is not None

    # -- preview (mandatory preflight) ----------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "table-preview-button":
            event.stop()
            event.prevent_default()
            self.run_preview()
            return
        if event.button.id == "cancel" and self.running:
            # Textual dispatches a message to every MRO class defining the
            # handler (ODR-0043); prevent_default keeps WriteModal's own
            # on_button_pressed from dismissing the modal mid-run.
            event.prevent_default()
            self.action_cancel()
            return
        # ODR-0047: the MRO dispatch would run WriteModal's handler a second time.
        event.prevent_default()
        super().on_button_pressed(event)

    def run_preview(self) -> None:
        values = self._values()
        if not values["file"]:
            self.show_error("a table input path is required (--file)")
            return
        self.clear_error()
        self.preview_running = True
        self._preview_snapshot = values
        self.query_one("#table-preview-button", Button).disabled = True
        self.query_one("#table-status", Static).update("preview running…")
        self._preview(values)

    @work(thread=True)
    def _preview(self, values: dict[str, Any]) -> None:
        try:
            payload: Any = actions.table_import_preview(
                self.project, values["table"], values["file"])
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal  # pylint: disable=broad-exception-caught
            payload = exc
        self.post_to_ui(self._preview_done, payload)

    def _preview_done(self, payload: Any) -> None:
        self.preview_running = False
        self.query_one("#table-preview-button", Button).disabled = False
        if isinstance(payload, BaseException):
            self.preview = None
            self.preview_values = {}
            self.query_one("#table-preview-table", DataTable).clear()
            self.query_one("#table-status", Static).update(
                Text(f"preview failed: {payload}", style="red"))
            self.set_confirm_enabled(self._confirm_allowed())
            return
        if self._preview_relevant(self._values()) != \
                self._preview_relevant(self._preview_snapshot):
            # The form moved on while the preview ran: this answer is stale.
            self.preview = None
            self.preview_values = {}
            self.query_one("#table-preview-table", DataTable).clear()
            self.query_one("#table-status", Static).update(
                Text("form changed during the preview — run it again", style="yellow"))
            self.set_confirm_enabled(self._confirm_allowed())
            return
        self.preview = payload
        self.preview_values = self._preview_snapshot
        table = self.query_one("#table-preview-table", DataTable)
        table.clear()
        for item in payload["items"]:
            table.add_row(
                ": ".join(str(value) for value in item["key"]),
                item["action"],
                ", ".join(item["differences"]),
            )
        text = (f"preview: {payload['insert']} insert, {payload['update']} update, "
                f"{payload['unchanged']} unchanged — nothing was written")
        if payload["update"] and not self._values()["on_conflict"]:
            text += (f"\n{payload['update']} existing row(s) would change — pick skip "
                     "or update, or leave blank to apply the CLI's error policy")
        self.query_one("#table-status", Static).update(text)
        self.set_confirm_enabled(True)

    # -- confirm / run ---------------------------------------------------------

    def confirm(self) -> None:
        if self.running:
            return
        values = self._values()
        if values["mode"] == "template":
            if not values["template_out"]:
                self.show_error("a template output path is required (--template)")
                return
            self._start_run("template", values)
            return
        if not values["file"]:
            self.show_error("a table input path is required (--file)")
            return
        if self.preview is None:
            self.show_error("run the preview first")
            return
        if self.preview.get("update") and not values["on_conflict"]:
            self.show_error(
                "existing rows would change; pass --on-conflict error, skip or update")
            return
        self._start_run("import", values)

    def _start_run(self, kind: str, values: dict[str, Any]) -> None:
        self.running = True
        self._run_kind = kind
        self._set_controls_disabled(True)
        self.clear_error()
        if kind == "template":
            self.query_one("#table-status", Static).update("writing template…")
            self.run_action(lambda: actions.table_template(
                self.project, values["table"], values["template_out"]))
        else:
            self.query_one("#table-status", Static).update(
                "importing… (a running import cannot be interrupted)")
            self.run_action(lambda: actions.import_table(
                self.project, table=values["table"], path=values["file"],
                on_conflict=values["on_conflict"] or None))

    def _set_controls_disabled(self, disabled: bool) -> None:
        for widget in self.query("Input, Select"):
            widget.disabled = disabled
        self._update_preview_button(disabled)

    def _update_preview_button(self, base_disabled: bool = False) -> None:
        self.query_one("#table-preview-button", Button).disabled = \
            base_disabled or self._values()["mode"] == "template"

    def _action_done(self, payload: Any) -> None:
        self.running = False
        try:
            self.query_one("#table-status", Static).update("")
        except NoMatches:  # pragma: no cover - modal teardown race
            pass
        self._set_controls_disabled(False)
        self.set_confirm_enabled(self._confirm_allowed())
        if isinstance(payload, BaseException):
            self.show_error(payload)
            return
        self.on_action_success(payload)

    def on_action_success(self, payload: Any) -> None:
        if self._run_kind == "template":
            self.app.notify(f"template written to {payload['path']} ({payload['table']})")
        else:
            self.app.notify(
                f"table import ({payload['table']}): {payload['inserted']} inserted, "
                f"{payload['updated']} updated, {payload['unchanged']} unchanged, "
                f"{payload['skipped']} skipped")
        self.dismiss(payload)

    def action_cancel(self) -> None:
        if self.running or self.preview_running:
            self.app.notify(
                "a running table import cannot be interrupted from the TUI",
                severity="warning",
            )
            return
        self.dismiss(None)
