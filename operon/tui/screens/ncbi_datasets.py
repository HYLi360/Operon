"""NCBI Datasets import modal: offline/online adapter with a preflight gate.

Mirrors ``operon ncbi-datasets``: offline inputs (assembly_data_report
JSON/JSONL, Datasets ZIP, or an unpacked directory) and/or accession downloads
through the NCBI Datasets API.  The *Dry run (preflight)* button is mandatory
before Confirm (the FanoutModal pattern): offline inputs are parsed and
planned with ``dry_run=True`` (nothing written), accession-only requests run
``plan_only=True`` (the download plan, no downloads, no workflow rows); any
form change locks Confirm again.  The real run is cooperative-cancel based
(the QcModal pattern): Cancel sets a fresh ``threading.Event`` that the core
turns into the signal-style interrupt, so the run is recorded as
``interrupted`` and stays resumable with ``--resume-run``.
"""

from __future__ import annotations

import shlex
import threading
from collections.abc import Iterable
from time import monotonic
from typing import Any

from rich.text import Text
from textual import work
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Button, Checkbox, Input, RichLog, Static

from operon.config import Project
from operon.errors import ValidationError
from operon.tui import actions
from operon.tui.screens.common import WriteModal


def _split_csv(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


class NcbiDatasetsModal(WriteModal):
    """Form + dry-run preflight + confirm for ``operon ncbi-datasets``."""

    def __init__(self, project: Project) -> None:
        super().__init__("NCBI Datasets import")
        self.project = project
        self.preflight: dict[str, Any] | None = None
        self._preflight_snapshot: dict[str, Any] = {}
        self.preflight_running = False
        self.running = False
        self._run_kwargs: dict[str, Any] = {}
        self._cancel_event: threading.Event | None = None
        self._worker: Any = None
        self._started = 0.0
        self._status_timer: Any = None

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            "offline import of existing reports/ZIP/directories, online download by "
            "accession, or both.  The dry run is the mandatory preflight: offline "
            "inputs are parsed without writes; accession-only requests show the "
            "download plan without downloading",
            classes="modal-info",
        )
        yield Input(placeholder="input paths, comma separated (JSON/JSONL/ZIP/dir)",
                    id="ncbi-inputs")
        yield Input(placeholder="accessions, comma separated (GCF_/GCA_…)",
                    id="ncbi-accessions")
        yield Input(placeholder="accession file (one GCF/GCA per line)", id="ncbi-accession-file")
        yield Input(placeholder="include types, comma separated "
                    "(genome,gff3,protein,cds,sequence-report; blank = all)",
                    id="ncbi-include")
        yield Checkbox("Import metadata only (--no-archive-files)", id="ncbi-no-archive-files")
        yield Checkbox("Also create standardized/ copies (--standardize)", id="ncbi-standardize")
        yield Checkbox("Dry run (--dry-run)", id="ncbi-dry-run")
        yield Checkbox("Do not preserve sources (--no-preserve-source)",
                       id="ncbi-no-preserve-source")
        yield Checkbox("Plan only, no download or run rows (--plan-only)", id="ncbi-plan-only")
        yield Input(placeholder="NCBI contact email (or NCBI_EMAIL)", id="ncbi-email")
        yield Input(placeholder="NCBI API key (or NCBI_API_KEY)", id="ncbi-api-key")
        yield Input(value="300.0", placeholder="timeout seconds", id="ncbi-timeout")
        yield Input(value="10", placeholder="batch size (1-100)", id="ncbi-batch-size")
        yield Input(value="3", placeholder="download workers (1-10)", id="ncbi-download-workers")
        yield Input(value="4", placeholder="retries per batch (0-10)", id="ncbi-retries")
        yield Input(value="1.0", placeholder="retry backoff seconds", id="ncbi-retry-backoff")
        yield Input(placeholder="resume run id (WF_…, optional)", id="ncbi-resume-run")
        yield Static("", id="ncbi-status", classes="modal-info")
        with Horizontal(classes="config-buttons"):
            yield Button("Dry run (preflight)", id="ncbi-preflight-button")
        yield RichLog(id="ncbi-preview", max_lines=200, wrap=True, markup=False)

    def on_mount(self) -> None:
        super().on_mount()
        # Running for real only unlocks after a clean dry run.
        self.set_confirm_enabled(False)

    # -- form values ---------------------------------------------------------

    def _values(self) -> dict[str, Any]:
        """Raw form state (numbers stay text; ``_action_kwargs`` parses them)."""
        return {
            "inputs": _split_csv(self.query_one("#ncbi-inputs", Input).value),
            "accessions": _split_csv(self.query_one("#ncbi-accessions", Input).value),
            "accession_file": self.query_one("#ncbi-accession-file", Input).value.strip(),
            "include": _split_csv(self.query_one("#ncbi-include", Input).value),
            "archive_files": not self.query_one("#ncbi-no-archive-files", Checkbox).value,
            "standardize": self.query_one("#ncbi-standardize", Checkbox).value,
            "dry_run": self.query_one("#ncbi-dry-run", Checkbox).value,
            "preserve_sources": not self.query_one("#ncbi-no-preserve-source", Checkbox).value,
            "plan_only": self.query_one("#ncbi-plan-only", Checkbox).value,
            "email": self.query_one("#ncbi-email", Input).value.strip(),
            "api_key": self.query_one("#ncbi-api-key", Input).value.strip(),
            "timeout": self.query_one("#ncbi-timeout", Input).value.strip(),
            "batch_size": self.query_one("#ncbi-batch-size", Input).value.strip(),
            "download_workers": self.query_one("#ncbi-download-workers", Input).value.strip(),
            "retries": self.query_one("#ncbi-retries", Input).value.strip(),
            "retry_backoff": self.query_one("#ncbi-retry-backoff", Input).value.strip(),
            "resume_run": self.query_one("#ncbi-resume-run", Input).value.strip(),
        }

    def _action_kwargs(self) -> dict[str, Any]:
        """Parse the form into ``actions.ncbi_datasets`` kwargs."""
        values = self._values()
        numbers: dict[str, Any] = {}
        for key, label, kind in (
                ("timeout", "timeout", float),
                ("batch_size", "batch size", int),
                ("download_workers", "download workers", int),
                ("retries", "retries", int),
                ("retry_backoff", "retry backoff", float),
        ):
            try:
                numbers[key] = kind(values[key])
            except ValueError:
                raise ValidationError(f"{label} must be a number") from None
        return {
            "inputs": values["inputs"],
            "accessions": values["accessions"],
            "accession_file": values["accession_file"] or None,
            "include": values["include"] or None,
            "archive_files": values["archive_files"],
            "standardize": values["standardize"],
            "dry_run": values["dry_run"],
            "preserve_sources": values["preserve_sources"],
            "email": values["email"] or None,
            "api_key": values["api_key"] or None,
            "resume_run_id": values["resume_run"] or None,
            "plan_only": values["plan_only"],
            **numbers,
        }

    def command_text(self) -> str:
        values = self._values()
        parts = ["operon", "ncbi-datasets"]
        for path in values["inputs"]:
            parts += ["--input", shlex.quote(path)]
        for accession in values["accessions"]:
            parts += ["--accession", shlex.quote(accession)]
        if values["accession_file"]:
            parts += ["--accession-file", shlex.quote(values["accession_file"])]
        for include in values["include"]:
            parts += ["--include", shlex.quote(include)]
        if not values["archive_files"]:
            parts.append("--no-archive-files")
        if values["standardize"]:
            parts.append("--standardize")
        if values["dry_run"]:
            parts.append("--dry-run")
        if not values["preserve_sources"]:
            parts.append("--no-preserve-source")
        if values["email"]:
            parts += ["--email", shlex.quote(values["email"])]
        if values["api_key"]:
            parts += ["--api-key", shlex.quote(values["api_key"])]
        for key, flag, default in (
                ("timeout", "--timeout", "300.0"),
                ("batch_size", "--batch-size", "10"),
                ("download_workers", "--download-workers", "3"),
                ("retries", "--retries", "4"),
                ("retry_backoff", "--retry-backoff", "1.0"),
        ):
            if values[key] != default:
                parts += [flag, shlex.quote(values[key] or "…")]
        if values["resume_run"]:
            parts += ["--resume-run", shlex.quote(values["resume_run"])]
        if values["plan_only"]:
            parts.append("--plan-only")
        return " ".join(parts)

    # -- change tracking: any edit invalidates the preflight -------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("ncbi-"):
            self._invalidate_preflight()
            self.refresh_command()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id and event.checkbox.id.startswith("ncbi-"):
            self._invalidate_preflight()
            self.refresh_command()

    def _invalidate_preflight(self) -> None:
        if self.preflight is None:
            return
        self.preflight = None
        self.set_confirm_enabled(False)
        preview = self.query_one("#ncbi-preview", RichLog)
        preview.clear()
        preview.write("form changed — run the dry run again")

    # -- preflight (mandatory before Confirm) ---------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ncbi-preflight-button":
            event.stop()
            event.prevent_default()
            self.run_preflight()
            return
        if event.button.id == "cancel" and self.running:
            # prevent_default keeps WriteModal's own on_button_pressed — Textual
            # dispatches a message to every MRO class defining the handler —
            # from dismissing the modal underneath the running worker.
            event.prevent_default()
            self._request_cancel()
            return
        super().on_button_pressed(event)

    def run_preflight(self) -> None:
        if self.running or self.preflight_running:
            return
        try:
            kwargs = self._action_kwargs()
        except ValidationError as exc:
            self.show_error(exc)
            return
        if not kwargs["inputs"] and not kwargs["accessions"] and not kwargs["accession_file"]:
            self.show_error("provide at least one --input, --accession, or --accession-file")
            return
        self.clear_error()
        self.preflight_running = True
        self._preflight_snapshot = self._values()
        self.query_one("#ncbi-preflight-button", Button).disabled = True
        self.query_one("#ncbi-status", Static).update("preflight running…")
        self._preflight(kwargs)

    @work(thread=True)
    def _preflight(self, kwargs: dict[str, Any]) -> None:
        options = dict(kwargs)
        if options["inputs"]:
            # Offline packages: parse and normalize without writing anything.
            options["dry_run"] = True
            options["plan_only"] = False
        else:
            # Pure accession requests: show the download plan without
            # downloading or writing workflow rows.
            options["dry_run"] = False
            options["plan_only"] = True
        try:
            payload: Any = actions.ncbi_datasets(self.project, **options)
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal  # pylint: disable=broad-exception-caught
            payload = exc
        self.post_to_ui(self._preflight_done, payload)

    def _preflight_done(self, payload: Any) -> None:
        self.preflight_running = False
        self.query_one("#ncbi-preflight-button", Button).disabled = False
        self.query_one("#ncbi-status", Static).update("")
        preview = self.query_one("#ncbi-preview", RichLog)
        preview.clear()
        if self._values() != self._preflight_snapshot:
            # The form moved on while the preflight ran: this answer is stale.
            self.preflight = None
            self.set_confirm_enabled(False)
            preview.write("form changed during the dry run — run it again")
            return
        if isinstance(payload, BaseException):
            self.preflight = None
            self.set_confirm_enabled(False)
            preview.write(Text(f"dry run failed: {payload}", style="red"))
            return
        self.preflight = payload
        preview.write(self._render_preflight(payload))
        self.set_confirm_enabled(True)

    @staticmethod
    def _render_preflight(payload: dict[str, Any]) -> str:
        lines = [
            f"sources parsed: {len(payload.get('sources') or [])}",
            f"assembly records: {payload.get('assembly_records', 0)}",
        ]
        plan = payload.get("download_plan") or []
        planned = sum(len(group["accessions"]) for group in plan)
        lines.append(f"download plan: {planned} accession(s) in {len(plan)} group(s)")
        for group in plan:
            lines.append(f"  includes={','.join(group['includes'])}: "
                         f"{', '.join(group['accessions'])}")
        skipped = payload.get("skipped_existing") or []
        if skipped:
            lines.append(f"skipped (already archived): {', '.join(skipped)}")
        messages = str(payload.get("messages") or "").strip()
        if messages:
            lines.append(messages)
        lines.append("preflight clean — nothing was written; Confirm runs for real")
        return "\n".join(lines)

    # -- real run (cooperative cancellation) -----------------------------------

    def confirm(self) -> None:
        if self.running:
            return
        if self.preflight is None:
            self.show_error("run the dry run (preflight) first")
            return
        try:
            kwargs = self._action_kwargs()
        except ValidationError as exc:
            self.show_error(exc)
            return
        self._run_kwargs = kwargs
        self._cancel_event = threading.Event()
        self._started = monotonic()
        self.running = True
        self._set_controls_disabled(True)
        self.set_confirm_enabled(False)
        self.clear_error()
        self.query_one("#ncbi-status", Static).update("running… (0s elapsed)")
        self._start_status_timer()
        self._worker = self._run()

    def _set_controls_disabled(self, disabled: bool) -> None:
        for widget in self.query("Input, Checkbox"):
            widget.disabled = disabled
        self.query_one("#ncbi-preflight-button", Button).disabled = disabled

    def _request_cancel(self) -> None:
        """Cancel cooperatively: the core stops at the next chunk/batch boundary."""
        if self._cancel_event is not None:
            self._cancel_event.set()
        try:
            self.query_one("#ncbi-status", Static).update(
                "cancelling… (in-flight downloads stop at the next batch boundary)"
            )
        except NoMatches:  # pragma: no cover - modal teardown race
            pass

    def action_cancel(self) -> None:
        if self.running:
            self._request_cancel()
            return
        self.dismiss(None)

    def _start_status_timer(self) -> None:
        self._stop_status_timer()
        self._status_timer = self.set_interval(1.0, self._tick_status)

    def _stop_status_timer(self) -> None:
        if self._status_timer is not None:
            self._status_timer.stop()
            self._status_timer = None

    def _tick_status(self) -> None:
        if not self.running:
            self._stop_status_timer()
            return
        elapsed = int(monotonic() - self._started)
        try:
            self.query_one("#ncbi-status", Static).update(f"running… ({elapsed}s elapsed)")
        except NoMatches:  # pragma: no cover - modal teardown race
            self._stop_status_timer()

    def on_unmount(self) -> None:
        self._stop_status_timer()

    @work(thread=True)
    def _run(self) -> None:
        try:
            payload: Any = actions.ncbi_datasets(
                self.project, cancel_event=self._cancel_event, **self._run_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - routed to _run_done  # pylint: disable=broad-exception-caught
            payload = exc
        self.post_to_ui(self._run_done, payload)

    def _run_done(self, payload: Any) -> None:
        self.running = False
        self._stop_status_timer()
        self._set_controls_disabled(False)
        self.set_confirm_enabled(True)
        if isinstance(payload, actions.NcbiDatasetsCancelled):
            self.app.notify(
                "NCBI Datasets import cancelled; the run is recorded as interrupted "
                "and can be resumed with --resume-run",
                severity="warning",
            )
            self.query_one("#ncbi-status", Static).update("cancelled")
            return
        if isinstance(payload, BaseException):
            self.query_one("#ncbi-status", Static).update("")
            self.show_error(payload)
            return
        self.app.notify(
            f"NCBI Datasets import: {payload.get('assembly_records', 0)} assembly record(s) "
            f"(run {payload.get('run_id', '-')})"
        )
        self.dismiss(payload)
