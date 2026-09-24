"""Taxonomy modals: import frozen NCBI Taxonomy snapshots and compile
coverage denominators (Coverage screen).

Mirrors ``operon taxonomy import``/``operon taxonomy compile``: the runs go
through the same core functions, so archiving, transactions, audit rows and
workflow provenance are identical to the CLI.  The core has no cooperative
cancel, so a running form refuses to close (the RunExternalModal pattern)
instead of pretending.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import Any

from textual.css.query import NoMatches
from textual.widgets import Button, Input, Select, Static

from operon.config import Project
from operon.tui import actions, data
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
            # Textual dispatches a message to every MRO class defining the
            # handler (ODR-0043); prevent_default keeps WriteModal's own
            # on_button_pressed from dismissing the modal mid-run.
            event.prevent_default()
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


class CompileReferenceSetModal(WriteModal):
    """Form + confirm for ``operon taxonomy compile``."""

    def __init__(self, project: Project) -> None:
        super().__init__("Compile reference set")
        self.project = project
        self.running = False
        self.profiles = data.list_coverage_profiles(project)
        # Same READY condition the core enforces for ``taxonomy compile``.
        self.snapshots = [
            row for row in data.list_taxonomy_snapshots(project)
            if row["source"] == "NCBI" and row["status"] == "READY"
        ]

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            "compile a taxonomy_coverage profile against a READY NCBI Taxonomy "
            "snapshot into an immutable reference-set denominator: "
            "taxonomy/reference_sets/<profile>@<version>.tsv plus its provenance "
            "sidecar.  Identical profile, snapshot and bytes reuse the existing "
            "reference set.",
            classes="modal-info",
        )
        if not self.profiles:
            yield Static(
                "no taxonomy_coverage profiles in config/profiles/ yet — create "
                "one from the Config screen or by hand before compiling",
                classes="modal-info",
            )
        yield Select(
            [(f"{row['name']} (v{row['version']})", row["name"])
             for row in self.profiles],
            prompt="select a coverage profile",
            id="taxonomy-compile-profile",
            allow_blank=True,
        )
        if not self.snapshots:
            yield Static(
                "no READY NCBI taxonomy snapshots yet — import one with the "
                "Import taxonomy… button first",
                classes="modal-info",
            )
        yield Select(
            [(row["taxonomy_version"], row["taxonomy_version"]) for row in self.snapshots],
            prompt="select a taxonomy version",
            id="taxonomy-compile-taxonomy-version",
            allow_blank=True,
        )
        yield Static("", id="taxonomy-compile-status", classes="modal-info")

    def _selection(self) -> tuple[Any, Any]:
        profile = self.query_one("#taxonomy-compile-profile", Select).value
        version = self.query_one("#taxonomy-compile-taxonomy-version", Select).value
        return profile, version

    def command_text(self) -> str:
        profile, version = self._selection()
        parts = ["operon", "taxonomy", "compile"]
        if profile is not Select.NULL:
            parts += ["--profile", shlex.quote(str(profile))]
        if version is not Select.NULL:
            parts += ["--taxonomy-version", shlex.quote(str(version))]
        return " ".join(parts)

    def confirm(self) -> None:
        if self.running:
            return
        profile, version = self._selection()
        if profile is Select.NULL:
            self.show_error("select a coverage profile first")
            return
        if version is Select.NULL:
            self.show_error("select a taxonomy version first")
            return
        self.running = True
        self._set_controls_disabled(True)
        self.clear_error()
        self.query_one("#taxonomy-compile-status", Static).update(
            "compiling… (a running compile cannot be interrupted)"
        )
        self.run_action(lambda: actions.compile_reference_set(
            self.project, str(profile), str(version)))

    def _set_controls_disabled(self, disabled: bool) -> None:
        for widget in self.query("Select"):
            widget.disabled = disabled

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id and event.select.id.startswith("taxonomy-compile-"):
            self.refresh_command()

    def _action_done(self, payload: Any) -> None:
        self.running = False
        self._set_controls_disabled(False)
        try:
            self.query_one("#taxonomy-compile-status", Static).update("")
        except NoMatches:  # pragma: no cover - modal teardown race
            pass
        super()._action_done(payload)

    def on_action_success(self, payload: dict[str, Any]) -> None:
        counts = (f"family {payload['family_count']} / genus "
                  f"{payload['genus_count']} row(s)")
        if payload.get("reused"):
            self.app.notify(
                f"reference set {payload['reference_set_id']}: {counts} "
                "(reused existing reference set)"
            )
        else:
            self.app.notify(f"reference set {payload['reference_set_id']}: {counts}")
        self.dismiss(payload)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel" and self.running:
            # Textual dispatches a message to every MRO class defining the
            # handler (ODR-0043); prevent_default keeps WriteModal's own
            # on_button_pressed from dismissing the modal mid-run.
            event.prevent_default()
            self.action_cancel()
            return
        super().on_button_pressed(event)

    def action_cancel(self) -> None:
        if self.running:
            self.app.notify(
                "a running taxonomy compile cannot be interrupted from the TUI",
                severity="warning",
            )
            return
        self.dismiss(None)
