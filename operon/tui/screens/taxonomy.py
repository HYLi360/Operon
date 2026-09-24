"""Taxonomy modals: import frozen NCBI Taxonomy snapshots (Coverage screen).

Mirrors ``operon taxonomy import``: the run goes through the same core
function, so archiving, transactions, audit rows and workflow provenance are
identical to the CLI.  The core has no cooperative cancel, so a running form
refuses to close (the RunExternalModal pattern) instead of pretending.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import Any

from textual.css.query import NoMatches
from textual.widgets import Button, Input, Static

from operon.config import Project
from operon.tui import actions
from operon.tui.screens.common import WriteModal


class TaxonomyImportModal(WriteModal):
    """Form + confirm for ``operon taxonomy import``."""

    def __init__(self, project: Project) -> None:
        super().__init__("Import NCBI Taxonomy")
        self.project = project
        self.running = False

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            "archive and import an NCBI taxonomy package: a taxonomy_report.jsonl "
            "(plain or inside a Datasets ZIP/tar archive) or an official NCBI "
            "taxdump archive.  The source is content-addressed under "
            "raw/metadata/ncbi_taxonomy/ and the snapshot is immutable; the same "
            "version and bytes reuse the existing snapshot.",
            classes="modal-info",
        )
        yield Input(
            placeholder="input path (taxonomy_report.jsonl / package / taxdump archive)",
            id="taxonomy-import-input",
        )
        yield Input(
            placeholder="immutable taxonomy version label (--version)",
            id="taxonomy-import-version",
        )
        yield Static("", id="taxonomy-import-status", classes="modal-info")

    def _values(self) -> dict[str, str]:
        return {
            "input": self.query_one("#taxonomy-import-input", Input).value.strip(),
            "version": self.query_one("#taxonomy-import-version", Input).value.strip(),
        }

    def command_text(self) -> str:
        values = self._values()
        parts = ["operon", "taxonomy", "import"]
        if values["input"]:
            parts += ["--input", shlex.quote(values["input"])]
        if values["version"]:
            parts += ["--version", shlex.quote(values["version"])]
        return " ".join(parts)

    def confirm(self) -> None:
        if self.running:
            return
        values = self._values()
        if not values["input"]:
            self.show_error("--input is required")
            return
        if not values["version"]:
            self.show_error("--version is required")
            return
        self.running = True
        self._set_controls_disabled(True)
        self.clear_error()
        self.query_one("#taxonomy-import-status", Static).update(
            "importing… (a running import cannot be interrupted)"
        )
        self.run_action(lambda: actions.import_taxonomy(
            self.project, values["input"], values["version"]))

    def _set_controls_disabled(self, disabled: bool) -> None:
        for widget in self.query("Input"):
            widget.disabled = disabled

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("taxonomy-import-"):
            self.refresh_command()

    def _action_done(self, payload: Any) -> None:
        self.running = False
        self._set_controls_disabled(False)
        try:
            self.query_one("#taxonomy-import-status", Static).update("")
        except NoMatches:  # pragma: no cover - modal teardown race
            pass
        super()._action_done(payload)

    def on_action_success(self, payload: dict[str, Any]) -> None:
        if payload.get("reused"):
            self.app.notify(
                f"taxonomy {payload['taxonomy_version']}: reused existing snapshot "
                f"{payload['taxonomy_snapshot_id']}"
            )
        else:
            self.app.notify(
                f"taxonomy {payload['taxonomy_version']}: {payload['node_count']} "
                f"node(s) imported as {payload['taxonomy_snapshot_id']}"
            )
        self.dismiss(payload)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel" and self.running:
            self.action_cancel()
            return
        super().on_button_pressed(event)

    def action_cancel(self) -> None:
        if self.running:
            self.app.notify(
                "a running taxonomy import cannot be interrupted from the TUI",
                severity="warning",
            )
            return
        self.dismiss(None)
