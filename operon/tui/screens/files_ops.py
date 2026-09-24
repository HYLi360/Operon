"""Write-operation modals for the Files screen: ingest, verify, QC."""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import Any

from rich.text import Text
from textual import work
from textual.widgets import Button, Checkbox, Input, ProgressBar, Select, Static

from operon.config import Project
from operon.tui import actions
from operon.tui.screens.common import ENTITY_TYPE_OPTIONS, WriteModal

HEALTHY_VERIFY_STATUSES = frozenset({"CHECKSUM_VERIFIED", "REMOTE_ONLY"})

#: The `operon standardize --link` choices, shown by :class:`StandardizeModal`.
LINK_KIND_OPTIONS = [("copy (independent copy)", "copy"),
                     ("hardlink", "hardlink"),
                     ("symlink", "symlink")]

#: `operon run-pipeline --entity-type` choices (narrower than ingest's).
PIPELINE_ENTITY_TYPE_OPTIONS = [("assembly", "assembly"),
                                ("annotation", "annotation"),
                                ("run", "run")]


class IngestModal(WriteModal):
    """Form + confirm for `operon ingest`.  ConflictError stays inline."""

    def __init__(self, project: Project, selected: dict[str, Any] | None = None) -> None:
        super().__init__("Ingest file")
        self.project = project
        self.selected = selected or {}

    def compose_form(self) -> Iterable[Any]:
        yield Input(placeholder="source path or sftp:// / remote:// URL (required)", id="ingest-source")
        yield Static("Entity type", classes="modal-label")
        yield Select(
            ENTITY_TYPE_OPTIONS,
            value=self.selected.get("entity_type") or "assembly",
            id="ingest-entity-type", allow_blank=False,
        )
        yield Input(
            value=str(self.selected.get("entity_id") or ""),
            placeholder="entity id (required)", id="ingest-entity-id",
        )
        yield Input(
            value=str(self.selected.get("file_role") or ""),
            placeholder="role (required)", id="ingest-role",
        )
        yield Input(placeholder="format (auto-detect)", id="ingest-format")
        yield Input(placeholder="compression (auto-detect)", id="ingest-compression")
        yield Input(placeholder="source url (optional)", id="ingest-source-url")
        yield Checkbox("Move source instead of copying", id="ingest-move")

    def _values(self) -> dict[str, Any]:
        entity_type = self.query_one("#ingest-entity-type", Select).value
        return {
            "source": self.query_one("#ingest-source", Input).value.strip(),
            "entity_type": "" if entity_type is Select.NULL else str(entity_type),
            "entity_id": self.query_one("#ingest-entity-id", Input).value.strip(),
            "role": self.query_one("#ingest-role", Input).value.strip(),
            "fmt": self.query_one("#ingest-format", Input).value.strip() or None,
            "compression": self.query_one("#ingest-compression", Input).value.strip() or None,
            "source_url": self.query_one("#ingest-source-url", Input).value.strip() or None,
            "move": self.query_one("#ingest-move", Checkbox).value,
        }

    def command_text(self) -> str:
        values = self._values()
        parts = ["operon", "ingest", "--source", shlex.quote(values["source"] or "…")]
        for field, flag in (("entity_type", "--entity-type"), ("entity_id", "--entity-id"),
                            ("role", "--role"), ("fmt", "--format"),
                            ("compression", "--compression"), ("source_url", "--source-url")):
            if values[field]:
                parts += [flag, shlex.quote(str(values[field]))]
        if values["move"]:
            parts.append("--move")
        return " ".join(parts)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("ingest-"):
            self.refresh_command()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "ingest-entity-type":
            self.refresh_command()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "ingest-move":
            self.refresh_command()

    def confirm(self) -> None:
        values = self._values()
        for field, label in (("source", "source"), ("entity_id", "entity id"), ("role", "role")):
            if not values[field]:
                self.show_error(f"{label} is required")
                return
        self.run_action(lambda: actions.ingest(self.project, **values))

    def on_action_success(self, payload: Any) -> None:
        self.app.notify(f"registered {payload['file_id']} -> {payload['relative_path']}")
        self.dismiss(payload)


class VerifyModal(WriteModal):
    """Confirm + run for `operon verify` (all files or one selected file)."""

    def __init__(self, project: Project, file_id: str | None, total: int) -> None:
        super().__init__("Verify files")
        self.project = project
        self.file_id = file_id
        self.total = total

    def compose_form(self) -> Iterable[Any]:
        if self.file_id:
            text = f"Verify file {self.file_id}?  SHA-256 is recomputed and statuses are updated."
        else:
            text = (f"Verify all {self.total} files?  SHA-256 is recomputed for every local "
                    "artifact and recorded remotes are live-checked.")
        yield Static(text, classes="modal-info")

    def command_text(self) -> str:
        if self.file_id:
            return f"operon verify --file-id {self.file_id}"
        return "operon verify"

    def confirm(self) -> None:
        file_ids = [self.file_id] if self.file_id else None
        self.run_action(lambda: actions.verify(self.project, file_ids))

    def on_action_success(self, payload: Any) -> None:
        self.dismiss(payload)


class StandardizeModal(WriteModal):
    """Link-kind choice + confirm for `operon standardize` (one file or all)."""

    def __init__(self, project: Project, file_id: str | None, link_kind: str = "copy") -> None:
        super().__init__("Standardize files")
        self.project = project
        self.file_id = file_id
        self.link_kind = link_kind

    def compose_form(self) -> Iterable[Any]:
        if self.file_id:
            text = (f"Stage {self.file_id} into standardized/?  raw/ stays immutable; "
                    "the source checksum is verified first.")
        else:
            text = ("Stage every verified file into standardized/?  raw/ stays immutable; "
                    "each source checksum is verified first.")
        yield Static(text, classes="modal-info")
        yield Static("Link kind", classes="modal-label")
        yield Select(LINK_KIND_OPTIONS, value=self.link_kind,
                     id="standardize-link", allow_blank=False)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "standardize-link":
            self.link_kind = str(event.value)
            self.refresh_command()

    def command_text(self) -> str:
        scope = f" --file-id {self.file_id}" if self.file_id else ""
        return f"operon standardize{scope} --link {self.link_kind}"

    def confirm(self) -> None:
        self.run_action(lambda: actions.standardize(self.project, self.file_id, self.link_kind))

    def on_action_success(self, payload: Any) -> None:
        self.dismiss(payload)


class ImportQcModal(WriteModal):
    """Form + mandatory preview + confirm for `operon import-qc`.

    The preview is the preflight (the table-import dialog's shape): it parses
    the input — a ``qc-measure`` JSON payload or an external TSV table, told
    apart by content — and validates it against the manifest without writing,
    so Confirm only unlocks once the input has been read end to end and any
    problem stays inline.  Importing appends metrics; it never replaces
    earlier ones, so a repeated import is a new set of rows, exactly like the
    CLI.
    """

    def __init__(self, project: Project) -> None:
        super().__init__("Import QC metrics")
        self.project = project
        self.preview: dict[str, Any] | None = None
        self.preview_running = False

    def compose_form(self) -> Iterable[Any]:
        yield Input(placeholder="qc-measure JSON payload or external TSV (required)",
                    id="qc-import-path")
        yield Button("Preview import", id="qc-import-preview-button")
        yield Static("", id="qc-import-status", classes="modal-info")

    def on_mount(self) -> None:
        super().on_mount()
        self.set_confirm_enabled(False)

    def _path(self) -> str:
        return self.query_one("#qc-import-path", Input).value.strip()

    def command_text(self) -> str:
        return f"operon import-qc --file {shlex.quote(self._path() or '…')}"

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "qc-import-path":
            self._invalidate_preview()
            self.refresh_command()

    def _invalidate_preview(self) -> None:
        if self.preview is None:
            return
        self.preview = None
        self.set_confirm_enabled(False)
        self.query_one("#qc-import-status", Static).update(
            Text("path changed — run the preview again", style="yellow"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "qc-import-preview-button":
            event.stop()
            event.prevent_default()
            self.run_preview()
            return
        # Textual dispatches a message to every MRO class defining the handler
        # (ODR-0043), so without prevent_default WriteModal's own handler
        # would run the Confirm a second time.
        event.prevent_default()
        super().on_button_pressed(event)

    # -- preview (mandatory preflight) ----------------------------------------

    def run_preview(self) -> None:
        path = self._path()
        if not path:
            self.show_error("an input path is required (--file)")
            return
        self.clear_error()
        self.preview_running = True
        self.set_confirm_enabled(False)
        self.query_one("#qc-import-preview-button", Button).disabled = True
        self.query_one("#qc-import-status", Static).update("preview running…")
        self._preview(path)

    @work(thread=True)
    def _preview(self, path: str) -> None:
        try:
            payload: Any = actions.qc_import_preview(self.project, path)
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal
            payload = exc
        self.post_to_ui(self._preview_done, payload)

    def _preview_done(self, payload: Any) -> None:
        self.preview_running = False
        self.query_one("#qc-import-preview-button", Button).disabled = False
        if isinstance(payload, BaseException):
            self.preview = None
            self.show_error(payload)
            self.query_one("#qc-import-status", Static).update("")
            self.set_confirm_enabled(False)
            return
        self.preview = payload
        self.query_one("#qc-import-status", Static).update(self._preview_text(payload))
        self.set_confirm_enabled(True)

    @staticmethod
    def _preview_text(payload: dict[str, Any]) -> Text:
        lines = [f"format: {payload['format']}", f"metrics: {payload['metric_count']}"]
        if payload["format"] == "json":
            lines.append(
                f"file: {payload['file_id']} "
                f"({payload['entity_type']} {payload['entity_id']})")
        else:
            lines.append("entities: " + ", ".join(
                f"{kind} {ident}" for kind, ident in payload["entities"]))
        lines.append("stages: " + ", ".join(payload["stages"]))
        if payload.get("warning"):
            lines.append(f"warning: {payload['warning']}")
        return Text("\n".join(lines))

    def confirm(self) -> None:
        path = self._path()
        if not path:
            self.show_error("an input path is required (--file)")
            return
        if self.preview is None:
            self.show_error("run the preview first")
            return
        self.run_action(lambda: actions.import_qc(self.project, path))


class PipelineModal(WriteModal):
    """Form + mandatory preview + confirm for `operon run-pipeline`.

    One source file goes through ingest → standardize → QC → evaluate.  The
    preview is the preflight (the table-import dialog's shape): it resolves the
    profile, checks the entity and reports whether evaluation would re-use a
    curated decision — without writing anything.  When it would, Confirm also
    requires the explicit re-evaluation checkbox: this dialog's stand-in for
    the CLI's ``--yes`` (a non-tty CLI run refuses instead).
    """

    def __init__(self, project: Project, selected: dict[str, Any] | None = None) -> None:
        super().__init__("Run pipeline")
        self.project = project
        self.selected = selected or {}
        self.preview: dict[str, Any] | None = None
        self.preview_running = False

    def _default_profile(self) -> str:
        return str(self.project.config["qc"]["default_profile"])

    def _profile_options(self) -> list[tuple[str, str]]:
        from operon.tui.data import list_profiles

        names = sorted({self._default_profile(), *list_profiles(self.project)})
        return [
            (f"{name} (project default)" if name == self._default_profile() else name, name)
            for name in names
        ]

    def compose_form(self) -> Iterable[Any]:
        yield Input(placeholder="source path or sftp:// / remote:// URL (required)",
                    id="pipeline-source")
        yield Static("Entity type", classes="modal-label")
        yield Select(PIPELINE_ENTITY_TYPE_OPTIONS,
                     value=self.selected.get("entity_type") or "assembly",
                     id="pipeline-entity-type", allow_blank=False)
        yield Input(value=str(self.selected.get("entity_id") or ""),
                    placeholder="entity id (required)", id="pipeline-entity-id")
        yield Input(value=str(self.selected.get("file_role") or ""),
                    placeholder="role (required)", id="pipeline-role")
        yield Static("QC profile", classes="modal-label")
        yield Select(self._profile_options(), value=self._default_profile(),
                     id="pipeline-profile", allow_blank=False)
        yield Input(placeholder="format (auto-detect)", id="pipeline-format")
        yield Input(placeholder="compression (auto-detect)", id="pipeline-compression")
        yield Input(placeholder="source url (optional)", id="pipeline-source-url")
        yield Checkbox("Re-run evaluation over a curated decision (--yes)", id="pipeline-yes")
        yield Button("Preview pipeline", id="pipeline-preview-button")
        yield Static("", id="pipeline-status", classes="modal-info")

    def on_mount(self) -> None:
        super().on_mount()
        self.set_confirm_enabled(False)

    def _values(self) -> dict[str, Any]:
        entity_type = self.query_one("#pipeline-entity-type", Select).value
        profile = self.query_one("#pipeline-profile", Select).value
        return {
            "source": self.query_one("#pipeline-source", Input).value.strip(),
            "entity_type": "assembly" if entity_type is Select.NULL else str(entity_type),
            "entity_id": self.query_one("#pipeline-entity-id", Input).value.strip(),
            "role": self.query_one("#pipeline-role", Input).value.strip(),
            "profile": None if profile is Select.NULL else str(profile),
            "fmt": self.query_one("#pipeline-format", Input).value.strip() or None,
            "compression": self.query_one("#pipeline-compression", Input).value.strip() or None,
            "source_url": self.query_one("#pipeline-source-url", Input).value.strip() or None,
        }

    def command_text(self) -> str:
        values = self._values()
        parts = ["operon", "run-pipeline", "--source", shlex.quote(values["source"] or "…")]
        for field, flag in (("entity_type", "--entity-type"), ("entity_id", "--entity-id"),
                            ("role", "--role"), ("profile", "--profile"), ("fmt", "--format"),
                            ("compression", "--compression"), ("source_url", "--source-url")):
            if values[field]:
                parts += [flag, shlex.quote(str(values[field]))]
        if self.query_one("#pipeline-yes", Checkbox).value:
            parts.append("--yes")
        return " ".join(parts)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("pipeline-"):
            self._invalidate_preview()
            self.refresh_command()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id and event.select.id.startswith("pipeline-"):
            self._invalidate_preview()
            self.refresh_command()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "pipeline-yes":
            self.refresh_command()

    def _invalidate_preview(self) -> None:
        if self.preview is None:
            return
        self.preview = None
        self.set_confirm_enabled(False)
        self.query_one("#pipeline-status", Static).update(
            Text("form changed — run the preview again", style="yellow"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pipeline-preview-button":
            event.stop()
            event.prevent_default()
            self.run_preview()
            return
        # Textual dispatches a message to every MRO class defining the handler
        # (ODR-0043), so without prevent_default WriteModal's own handler would
        # run the Confirm a second time.
        event.prevent_default()
        super().on_button_pressed(event)

    # -- preview (mandatory preflight) ----------------------------------------

    def run_preview(self) -> None:
        values = self._values()
        for field, label in (("source", "source"), ("entity_id", "entity id"), ("role", "role")):
            if not values[field]:
                self.show_error(f"{label} is required")
                return
        self.clear_error()
        self.preview_running = True
        self.set_confirm_enabled(False)
        self.query_one("#pipeline-preview-button", Button).disabled = True
        self.query_one("#pipeline-status", Static).update("preview running…")
        self._preview(values)

    @work(thread=True)
    def _preview(self, values: dict[str, Any]) -> None:
        try:
            payload: Any = actions.pipeline_preview(self.project, **values)
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal
            payload = exc
        self.post_to_ui(self._preview_done, payload)

    def _preview_done(self, payload: Any) -> None:
        self.preview_running = False
        self.query_one("#pipeline-preview-button", Button).disabled = False
        if isinstance(payload, BaseException):
            self.preview = None
            self.show_error(payload)
            self.query_one("#pipeline-status", Static).update("")
            self.set_confirm_enabled(False)
            return
        self.preview = payload
        self.query_one("#pipeline-status", Static).update(self._preview_text(payload))
        self.set_confirm_enabled(True)

    @staticmethod
    def _preview_text(payload: dict[str, Any]) -> Text:
        lines = [
            f"profile: {payload['profile']}",
            "steps: " + " → ".join(payload["steps"]),
            f"entity: {payload['entity_type']} {payload['entity_id']}",
        ]
        if payload["source_exists"] is False:
            lines.append("warning: the source path does not exist yet")
        if payload["curated_targets"]:
            lines.append(
                "warning: evaluation will re-use a curated decision — "
                "tick the re-run box to confirm")
        return Text("\n".join(lines))

    def confirm(self) -> None:
        if self.preview is None:
            self.show_error("run the preview first")
            return
        values = self._values()
        if self.preview["curated_targets"] and not self.query_one("#pipeline-yes", Checkbox).value:
            self.show_error("evaluation will re-use a curated decision; tick the re-run box")
            return
        self.run_action(lambda: actions.run_pipeline(self.project, **values))


class QcCancelled(Exception):
    """Raised between files when the QC worker is cancelled."""


class QcModal(WriteModal):
    """Confirm + live progress for `operon qc` on one file or all files.

    Cancellation is cooperative: the Textual worker is cancelled and the
    ``qc_all`` progress callback raises :class:`QcCancelled`, so the batch
    stops between files.  Files already processed keep their QC results.
    """

    def __init__(self, project: Project, file_id: str | None, total: int) -> None:
        super().__init__("Run QC")
        self.project = project
        self.file_id = file_id
        self.total = total
        self.done = 0
        self.running = False
        self._worker: Any = None
        self._qc_options: dict[str, Any] = {}

    def compose_form(self) -> Iterable[Any]:
        if self.file_id:
            scope = f"Run built-in QC stages for file {self.file_id}?"
        else:
            scope = f"Run built-in QC stages for all {self.total} files?"
        yield Static(scope, id="qc-scope", classes="modal-info")
        yield Static("FASTQ sample size (reads)", classes="modal-label")
        yield Input(value="1000000", id="qc-sample-size")
        yield Static("FASTQ Phred offset (auto assumes 33 when ambiguous)", classes="modal-label")
        yield Select([("33", "33"), ("64", "64"), ("auto", "auto")],
                     value="33", allow_blank=False, id="qc-phred-offset")
        yield Checkbox("Recompute input SHA-256 (--rehash)", id="qc-rehash")
        yield ProgressBar(total=max(self.total, 1), id="qc-progress")
        yield Static("", id="qc-status", classes="modal-info")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#qc-progress", ProgressBar).display = False

    def command_text(self) -> str:
        parts = ["operon", "qc"]
        if self.file_id:
            parts += ["--file-id", shlex.quote(self.file_id)]
        sample = self.query_one("#qc-sample-size", Input).value.strip()
        if sample != "1000000":
            parts += ["--sample-size", shlex.quote(sample or "…")]
        phred = str(self.query_one("#qc-phred-offset", Select).value)
        if phred != "33":
            parts += ["--phred-offset", phred]
        if self.query_one("#qc-rehash", Checkbox).value:
            parts.append("--rehash")
        return " ".join(parts)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "qc-sample-size":
            self.refresh_command()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "qc-phred-offset":
            self.refresh_command()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "qc-rehash":
            self.refresh_command()

    def confirm(self) -> None:
        if self.running:
            return
        try:
            sample_size = int(self.query_one("#qc-sample-size", Input).value.strip())
            if sample_size <= 0:
                raise ValueError
        except ValueError:
            self.show_error("sample size must be a positive integer")
            return
        self._qc_options = {
            "sample_size": sample_size,
            "phred_offset": str(self.query_one("#qc-phred-offset", Select).value),
            "rehash": self.query_one("#qc-rehash", Checkbox).value,
        }
        self.running = True
        self._set_options_disabled(True)
        self.set_confirm_enabled(False)
        self.clear_error()
        self.query_one("#qc-progress", ProgressBar).display = True
        self._worker = self._run_qc()

    def _set_options_disabled(self, disabled: bool) -> None:
        for selector in ("#qc-sample-size", "#qc-phred-offset", "#qc-rehash"):
            self.query_one(selector).disabled = disabled

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel" and self.running:
            # ODR-0043: stop the MRO walk so WriteModal cannot dismiss mid-run.
            event.prevent_default()
            if self._worker is not None:
                self._worker.cancel()
            return
        # ODR-0047: the MRO dispatch would run WriteModal's handler a second time.
        event.prevent_default()
        super().on_button_pressed(event)

    def action_cancel(self) -> None:
        if self.running:
            if self._worker is not None:
                self._worker.cancel()
            return
        self.dismiss(None)

    @work(thread=True)
    def _run_qc(self) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()

        def progress(done: int, total: int, result: dict[str, Any]) -> None:
            if worker.is_cancelled:
                raise QcCancelled()
            self.post_to_ui(self._progress, done, total, result)

        try:
            payload: Any = actions.run_qc(
                self.project, file_id=self.file_id, progress=progress, **self._qc_options,
            )
        except Exception as exc:  # noqa: BLE001 - routed to _qc_done  # pylint: disable=broad-exception-caught
            payload = exc
        self.post_to_ui(self._qc_done, payload)

    def _progress(self, done: int, total: int, result: dict[str, Any]) -> None:
        self.done = done
        self.total = total
        self.query_one("#qc-progress", ProgressBar).update(total=total, progress=done)
        marker = "" if result.get("ok") else "  (FAILED)"
        self.query_one("#qc-status", Static).update(f"{done}/{total} · {result['file_id']}{marker}")

    def _qc_done(self, payload: Any) -> None:
        self.running = False
        if isinstance(payload, QcCancelled):
            self.app.notify(
                f"QC cancelled after {self.done}/{self.total} file(s); completed files kept",
                severity="warning",
            )
            self.dismiss({"cancelled": True, "done": self.done, "total": self.total})
            return
        if isinstance(payload, BaseException):
            self._set_options_disabled(False)
            self.set_confirm_enabled(True)
            self.show_error(payload)
            return
        ok = sum(1 for result in payload if result["ok"])
        failures = [result for result in payload if not result["ok"]]
        self.app.notify(f"QC complete: {ok}/{len(payload)} file(s) passed built-in stages")
        self.dismiss({"ok": ok, "total": len(payload), "failures": failures})
