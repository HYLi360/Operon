"""Publish screen: immutable release builder + selective export builder.

Both tabs follow the standard write pattern: a form with a read-only preview
(members/exclusions for releases, file count/bytes for exports), an explicit
confirm dialog showing the equivalent CLI command, then the mutation in a
thread worker through :mod:`operon.tui.actions`.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Iterable

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from operon.config import Project
from operon.tui import actions, data
from operon.tui.screens.common import (
    ENTITY_TYPE_OPTIONS,
    Panel,
    WriteModal,
    human_size,
    styled_decision,
)
from operon.tui.screens.decisions import DECISION_VALUES

LINK_KIND_OPTIONS = [("copy", "copy"), ("hardlink", "hardlink")]
EXPORT_LINK_KIND_OPTIONS = [("copy", "copy"), ("hardlink", "hardlink"), ("symlink", "symlink")]


def _select_text(widget: Select) -> str:
    value = widget.value
    return "" if value is Select.NULL else str(value)


class CreateReleaseModal(WriteModal):
    """Confirm + create for ``operon release``."""

    def __init__(self, project: Project, version: str, profile: str,
                 copy_files: bool, link_kind: str) -> None:
        super().__init__(f"Create release {version}")
        self.project = project
        self.version = version
        self.profile = profile
        self.copy_files = copy_files
        self.link_kind = link_kind

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            f"Create immutable release {self.version} with profile {self.profile}?\n"
            "Accepted files are re-hashed before they enter the release; the "
            "release directory is published atomically and never overwritten.",
            classes="modal-info",
        )

    def command_text(self) -> str:
        parts = ["operon", "release", "--version", shlex.quote(self.version),
                 "--profile", shlex.quote(self.profile)]
        if self.copy_files:
            parts.append("--copy-files")
        elif self.link_kind != "copy":
            parts += ["--link", self.link_kind]
        return " ".join(parts)

    def confirm(self) -> None:
        self.run_action(lambda: actions.create_release(
            self.project, self.version, self.profile,
            copy_files=self.copy_files, link_kind=self.link_kind,
        ))

    def on_action_success(self, payload: Any) -> None:
        self.app.notify(
            f"release {payload['version']}: {payload['accepted_file_count']} files, "
            f"{payload['excluded_entity_count']} excluded"
        )
        self.dismiss(payload)


class ExportModal(WriteModal):
    """Confirm + run for ``operon export``."""

    def __init__(self, project: Project, output_dir: str, filters: dict[str, Any]) -> None:
        super().__init__("Export files")
        self.project = project
        self.output_dir = output_dir
        self.filters = filters

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            f"Materialize the selected files into {self.output_dir}?\n"
            "Every source is re-hashed against the manifest before it is "
            "copied, hardlinked or symlinked; existing directories are never "
            "overwritten.",
            classes="modal-info",
        )

    def command_text(self) -> str:
        filters = self.filters
        parts = ["operon", "export", "--output", shlex.quote(self.output_dir)]
        for key, flag in (("entity_type", "--entity-type"), ("file_role", "--file-role"),
                          ("fmt", "--format"), ("state", "--state"),
                          ("decision", "--decision"), ("profile", "--profile")):
            if filters.get(key):
                parts += [flag, shlex.quote(str(filters[key]))]
        for entity_id in filters.get("entity_ids") or []:
            parts += ["--entity-id", shlex.quote(entity_id)]
        if filters.get("link_kind") not in (None, "copy"):
            parts += ["--link", str(filters["link_kind"])]
        if not filters.get("include_qc", True):
            parts.append("--no-qc")
        return " ".join(parts)

    def confirm(self) -> None:
        self.run_action(lambda: actions.export(self.project, self.output_dir, **self.filters))

    def on_action_success(self, payload: Any) -> None:
        self.app.notify(
            f"exported {payload['file_count']} file(s) to {payload['output_dir']} "
            f"(manifest {payload['manifest_sha256'][:12]}…)"
        )
        self.dismiss(payload)


class PublishPanel(Panel):
    """Release builder + export builder."""

    def __init__(self, project: Project) -> None:
        super().__init__(id="publish")
        self.project = project
        self.releases: list[dict[str, Any]] = []
        self.profiles: list[str] = []
        self.release_preview_data: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        with TabbedContent(id="publish-tabs"):
            with TabPane("Release", id="tab-release"):
                with Vertical(id="release-layout"):
                    yield DataTable(id="releases-table", cursor_type="row")
                    with VerticalScroll(id="release-form"):
                        yield Static("New release", classes="modal-label")
                        yield Input(placeholder="version (required, e.g. 2026.09)",
                                    id="release-version")
                        yield Static("Profile", classes="modal-label")
                        yield Select([], id="release-profile", allow_blank=True)
                        yield Checkbox("Copy files (--copy-files)", value=True,
                                       id="release-copy-files")
                        yield Static("Link kind (when not copying)", classes="modal-label")
                        yield Select(LINK_KIND_OPTIONS, value="copy",
                                     id="release-link", allow_blank=False)
                        with Horizontal(classes="config-buttons"):
                            yield Button("Preview", id="release-preview-btn")
                            yield Button("Create release", id="release-create",
                                         variant="primary")
                        yield Static("select a profile and press Preview",
                                     id="release-preview-summary", classes="modal-info")
                        yield DataTable(id="release-exclusions-table")
                        yield Static("", id="release-error")
            with TabPane("Export", id="tab-export"):
                with VerticalScroll(id="export-layout"):
                    yield Static("Selection filters (at least one is required)",
                                 classes="modal-label")
                    with Horizontal(id="export-fields"):
                        with Vertical(classes="export-column"):
                            yield Static("Entity type", classes="modal-label")
                            yield Select(ENTITY_TYPE_OPTIONS, id="export-entity-type",
                                         allow_blank=True)
                            yield Input(placeholder="entity id (comma-separated)",
                                        id="export-entity-id")
                            yield Input(placeholder="file role", id="export-file-role")
                            yield Input(placeholder="format", id="export-format")
                            yield Input(placeholder="entity state", id="export-state")
                        with Vertical(classes="export-column"):
                            yield Static("Decision (requires profile)", classes="modal-label")
                            yield Select([(value, value) for value in DECISION_VALUES],
                                         id="export-decision", allow_blank=True)
                            yield Static("Profile", classes="modal-label")
                            yield Select([], id="export-profile", allow_blank=True)
                            yield Static("Link kind", classes="modal-label")
                            yield Select(EXPORT_LINK_KIND_OPTIONS, value="copy",
                                         id="export-link", allow_blank=False)
                            yield Checkbox("Include qc.tsv metrics snapshot", value=True,
                                           id="export-include-qc")
                    yield Input(placeholder="output directory (required; must not exist or be empty)",
                                id="export-output")
                    with Horizontal(classes="config-buttons"):
                        yield Button("Preview", id="export-preview-btn")
                        yield Button("Run export", id="export-run", variant="primary")
                    yield Static("", id="export-preview-summary", classes="modal-info")
                    yield Static("", id="export-error")

    def on_mount(self) -> None:
        table = self.query_one("#releases-table", DataTable)
        table.add_columns("version", "created_at", "profile", "files", "excluded")
        exclusions = self.query_one("#release-exclusions-table", DataTable)
        exclusions.add_columns("entity", "decision", "reason", "codes")
        super().on_mount()

    # -- data loading -------------------------------------------------------

    def _fetch(self) -> dict[str, Any]:
        return {
            "releases": data.list_releases(self.project),
            "profiles": data.list_profiles(self.project),
        }

    def render_data(self, payload: dict[str, Any]) -> None:
        self.releases = payload["releases"]
        self.profiles = payload["profiles"]
        table = self.query_one("#releases-table", DataTable)
        table.clear()
        for release in self.releases:
            summary = release.get("summary")
            if not isinstance(summary, dict):
                summary = {}
            table.add_row(
                release["version"],
                release["created_at"],
                release["profile"],
                str(summary.get("accepted_file_count", "?")),
                str(summary.get("excluded_entity_count", "?")),
                key=str(release["version"]),
            )
        options = [(name, name) for name in self.profiles]
        for widget_id in ("#release-profile", "#export-profile"):
            select = self.query_one(widget_id, Select)
            current = select.value
            select.set_options(options)
            if current is not Select.NULL and current in self.profiles:
                select.value = current

    def show_error(self, exc: BaseException) -> None:
        self.query_one("#release-error", Static).update(Text(f"error: {exc}", style="red"))
        self.query_one("#export-error", Static).update(Text(f"error: {exc}", style="red"))

    # -- release tab ----------------------------------------------------------

    def _release_version_exists(self, version: str) -> bool:
        return any(str(release["version"]) == version for release in self.releases)

    def _show_release_error(self, message: BaseException | str) -> None:
        self.query_one("#release-error", Static).update(Text(str(message), style="red"))

    def _start_release_preview(self) -> None:
        profile = _select_text(self.query_one("#release-profile", Select))
        if not profile:
            self._show_release_error("select a profile first")
            return
        self.query_one("#release-error", Static).update("")
        self.query_one("#release-preview-summary", Static).update("loading preview…")
        self._load_release_preview(profile)

    @work(thread=True, exclusive=True, group="release-preview")
    def _load_release_preview(self, profile: str) -> None:
        try:
            payload: Any = data.release_preview(self.project, profile)
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._apply_release_preview, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _apply_release_preview(self, payload: Any) -> None:
        if isinstance(payload, BaseException):
            self.query_one("#release-preview-summary", Static).update("")
            self._show_release_error(payload)
            return
        self.release_preview_data = payload
        self.query_one("#release-preview-summary", Static).update(
            f"{len(payload['members'])} member file(s), "
            f"{human_size(payload['member_bytes'])} total; "
            f"{len(payload['exclusions'])} excluded entit(ies)"
        )
        table = self.query_one("#release-exclusions-table", DataTable)
        table.clear()
        for row in payload["exclusions"]:
            table.add_row(
                f"{row['entity_type']}:{row['entity_id']}",
                styled_decision(row.get("effective_decision")),
                str(row.get("exclusion_reason") or ""),
                ", ".join(_reason_codes(row.get("reason_codes"))),
            )

    def _create_release(self) -> None:
        version = self.query_one("#release-version", Input).value.strip()
        profile = _select_text(self.query_one("#release-profile", Select))
        if not version:
            self._show_release_error("version is required")
            return
        if not profile:
            self._show_release_error("select a profile first")
            return
        if self._release_version_exists(version):
            self._show_release_error(f"release {version} already exists")
            return
        self.query_one("#release-error", Static).update("")
        copy_files = self.query_one("#release-copy-files", Checkbox).value
        link_kind = _select_text(self.query_one("#release-link", Select)) or "copy"
        self.app.push_screen(
            CreateReleaseModal(self.project, version, profile, copy_files, link_kind),
            self._after_write,
        )

    # -- export tab -------------------------------------------------------------

    def _export_filters(self) -> dict[str, Any]:
        entity_ids = [
            item.strip()
            for item in self.query_one("#export-entity-id", Input).value.split(",")
            if item.strip()
        ]
        return {
            "entity_type": _select_text(self.query_one("#export-entity-type", Select)) or None,
            "entity_ids": entity_ids,
            "file_role": self.query_one("#export-file-role", Input).value.strip() or None,
            "fmt": self.query_one("#export-format", Input).value.strip() or None,
            "state": self.query_one("#export-state", Input).value.strip() or None,
            "decision": _select_text(self.query_one("#export-decision", Select)) or None,
            "profile": _select_text(self.query_one("#export-profile", Select)) or None,
            "link_kind": _select_text(self.query_one("#export-link", Select)) or "copy",
            "include_qc": self.query_one("#export-include-qc", Checkbox).value,
        }

    def _show_export_error(self, message: BaseException | str) -> None:
        self.query_one("#export-error", Static).update(Text(str(message), style="red"))

    def _validate_export_filters(self, filters: dict[str, Any]) -> str | None:
        if not any([filters["entity_type"], filters["entity_ids"], filters["file_role"],
                    filters["fmt"], filters["state"], filters["decision"]]):
            return "export requires at least one selection criterion"
        if filters["decision"] and not filters["profile"]:
            return "--decision requires --profile"
        return None

    def _start_export_preview(self) -> None:
        filters = self._export_filters()
        error = self._validate_export_filters(filters)
        if error:
            self._show_export_error(error)
            return
        self.query_one("#export-error", Static).update("")
        self.query_one("#export-preview-summary", Static).update("loading preview…")
        self._load_export_preview(filters)

    @work(thread=True, exclusive=True, group="export-preview")
    def _load_export_preview(self, filters: dict[str, Any]) -> None:
        try:
            payload: Any = data.export_preview(
                self.project,
                entity_type=filters["entity_type"],
                entity_ids=filters["entity_ids"],
                file_role=filters["file_role"],
                fmt=filters["fmt"],
                state=filters["state"],
                decision=filters["decision"],
                profile=filters["profile"],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._apply_export_preview, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _apply_export_preview(self, payload: Any) -> None:
        if isinstance(payload, BaseException):
            self.query_one("#export-preview-summary", Static).update("")
            self._show_export_error(payload)
            return
        self.query_one("#export-preview-summary", Static).update(
            f"{payload['count']} file(s), {human_size(payload['bytes'])} total"
        )

    def _run_export(self) -> None:
        filters = self._export_filters()
        error = self._validate_export_filters(filters)
        if error:
            self._show_export_error(error)
            return
        output = self.query_one("#export-output", Input).value.strip()
        if not output:
            self._show_export_error("output directory is required")
            return
        output_path = Path(output).expanduser()
        if output_path.exists() and (not output_path.is_dir() or any(output_path.iterdir())):
            self._show_export_error(f"export output directory is not empty: {output_path}")
            return
        self.query_one("#export-error", Static).update("")
        self.app.push_screen(
            ExportModal(self.project, str(output_path), filters),
            self._after_write,
        )

    # -- shared ------------------------------------------------------------------

    def _after_write(self, result: Any) -> None:
        if result:
            self.app.reload_after_write()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "release-preview-btn":
            self._start_release_preview()
        elif button_id == "release-create":
            self._create_release()
        elif button_id == "export-preview-btn":
            self._start_export_preview()
        elif button_id == "export-run":
            self._run_export()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "release-profile" and event.value is not Select.NULL:
            self._start_release_preview()


def _reason_codes(raw: Any) -> list[str]:
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            return [raw]
    return []
