"""Write-operation modals for the Files screen: ingest, verify, QC."""

from __future__ import annotations

import shlex
from typing import Any, Iterable

from textual import work
from textual.app import ComposeResult
from textual.widgets import Button, Checkbox, Input, ProgressBar, Select, Static

from operon.config import Project
from operon.tui import actions
from operon.tui.screens.common import ENTITY_TYPE_OPTIONS, WriteModal

HEALTHY_VERIFY_STATUSES = frozenset({"CHECKSUM_VERIFIED", "REMOTE_ONLY"})


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

    def compose_form(self) -> Iterable[Any]:
        if self.file_id:
            scope = f"Run built-in QC stages for file {self.file_id}?"
        else:
            scope = f"Run built-in QC stages for all {self.total} files?"
        yield Static(scope, id="qc-scope", classes="modal-info")
        yield ProgressBar(total=max(self.total, 1), id="qc-progress")
        yield Static("", id="qc-status", classes="modal-info")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#qc-progress", ProgressBar).display = False

    def command_text(self) -> str:
        if self.file_id:
            return f"operon qc --file-id {self.file_id}"
        return "operon qc"

    def confirm(self) -> None:
        if self.running:
            return
        self.running = True
        self.set_confirm_enabled(False)
        self.clear_error()
        self.query_one("#qc-progress", ProgressBar).display = True
        self._worker = self._run_qc()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel" and self.running:
            if self._worker is not None:
                self._worker.cancel()
            return
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
            try:
                self.app.call_from_thread(self._progress, done, total, result)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

        try:
            payload: Any = actions.run_qc(self.project, file_id=self.file_id, progress=progress)
        except Exception as exc:  # noqa: BLE001 - routed to _qc_done
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._qc_done, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

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
            self.set_confirm_enabled(True)
            self.show_error(payload)
            return
        ok = sum(1 for result in payload if result["ok"])
        failures = [result for result in payload if not result["ok"]]
        self.app.notify(f"QC complete: {ok}/{len(payload)} file(s) passed built-in stages")
        self.dismiss({"ok": ok, "total": len(payload), "failures": failures})
