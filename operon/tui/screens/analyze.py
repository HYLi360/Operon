"""Analyze modal: run a configured external-tool recipe from the TUI.

The execution backend is selectable exactly like the CLI's ``--backend``
(project default / local / slurm / ssh); choosing a scheduler backend
preflights it before the worker starts, so a missing ``sbatch`` or an
incomplete ``execution.ssh`` block shows up as an inline error instead of a
per-file failed run.  Cancellation is cooperative and cancel-event based:
the Cancel button/Escape sets a ``threading.Event`` and cancels the worker,
so the batch stops at the next file/planning/collection boundary and a
still-queued Slurm job or array is cancelled with one ``scancel`` (a direct
SSH payload is terminated on the remote host) — the same interrupt
bookkeeping a signal produces.  The file currently being processed may
still finish, and files already completed keep their results.
"""

from __future__ import annotations

import shlex
import threading
from collections.abc import Iterable
from typing import Any

from rich.text import Text
from textual import work
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, Checkbox, Input, ProgressBar, Select, Static

from operon.config import Project
from operon.errors import ValidationError
from operon.tui import actions, data
from operon.tui.screens.common import (
    ENTITY_TYPE_OPTIONS,
    ErrorDialog,
    WriteModal,
    backend_select_options,
    project_default_backend,
    selected_backend,
)

FAILURE_STATUSES = frozenset({"error", "failed"})

LOCAL_CANCEL_NOTE = (
    "Cancel takes effect between files: the file currently being processed finishes."
)
SCHEDULER_CANCEL_NOTE = (
    "Cancel stops the batch at the next boundary and cancels the submitted work as a "
    "whole — a Slurm job array goes with one scancel, a direct SSH payload is "
    "terminated on the remote host. Files already completed keep their results."
)


class AnalyzeModal(WriteModal):
    """Form + live progress for ``operon analyze`` on one recipe.

    With ``recipe_name`` the recipe is fixed (Config screen entry point);
    without it the form starts with a recipe picker (Runs screen entry
    point).  Runtime parameter controls are generated from the recipe's
    ``parameters`` spec: ``choices`` becomes a Select, everything else an
    Input prefilled with the default; a required parameter without a default
    is marked with ``*``.
    """

    def __init__(self, project: Project, recipe_name: str | None = None) -> None:
        super().__init__(f"Run analysis — {recipe_name}" if recipe_name else "Run analysis")
        self.project = project
        self.fixed_recipe = recipe_name
        self.recipes = [] if recipe_name else data.list_recipes(project)
        self.recipe_name: str | None = None
        self.recipe_doc: dict[str, Any] = {}
        self._param_names: list[str] = []
        self._options: dict[str, Any] = {}
        self._analysis_name = ""
        self._worker: Any = None
        # Cooperative cancellation: set on Cancel/Escape, forwarded to the
        # core so a queued/running job or array is cancelled at the next
        # boundary (and while a scheduler poll loop waits).
        self._cancel_event: threading.Event | None = None
        self._executor_name = "local"
        self._executor_description = "local"
        self.running = False
        self.done = 0
        self.total = 0

    def _backend_options(self) -> list[tuple[str, str]]:
        """Backend choices: CLI ``--backend`` values plus the project default."""
        return backend_select_options(self.project)

    def _resolved_backend(self) -> str:
        return (selected_backend(self, "analyze-backend")
                or project_default_backend(self.project))

    def _update_cancel_note(self) -> None:
        """Describe cancel semantics for the selected backend."""
        note = (LOCAL_CANCEL_NOTE if self._resolved_backend() == "local"
                else SCHEDULER_CANCEL_NOTE)
        try:
            self.query_one("#analyze-cancel-note", Static).update(note)
        except NoMatches:  # not mounted yet
            pass

    def compose_form(self) -> Iterable[Any]:
        if self.fixed_recipe:
            yield Static(f"Recipe: {self.fixed_recipe}", classes="modal-info")
        else:
            yield Static("Recipe", classes="modal-label")
            yield Select(
                [(f"{row['tool']}.{row['name']}", row["name"]) for row in self.recipes],
                prompt="select a recipe", id="analyze-recipe", allow_blank=True,
            )
        yield Static("Entity type (blank = recipe default)", classes="modal-label")
        yield Select(ENTITY_TYPE_OPTIONS, id="analyze-entity-type", allow_blank=True)
        yield Input(placeholder="entity id (blank = all)", id="analyze-entity-id")
        yield Input(placeholder="limit (blank = all files)", id="analyze-limit")
        yield Input(placeholder="threads (blank = project default)", id="analyze-threads")
        yield Static("Execution backend", classes="modal-label")
        yield Select(self._backend_options(), value="", id="analyze-backend")
        yield Vertical(id="analyze-parameters")
        yield Checkbox("Dry run (plan only — nothing is executed or written)",
                       id="analyze-dry-run")
        yield Checkbox("Force re-run (--force)", id="analyze-force")
        yield Checkbox("Keep partial outputs on interrupt (--keep-partial)",
                       id="analyze-keep-partial")
        yield Static(LOCAL_CANCEL_NOTE, id="analyze-cancel-note", classes="modal-info")
        yield ProgressBar(total=1, id="analyze-progress")
        yield Static("", id="analyze-status", classes="modal-info")
        yield Static("", id="analyze-plan")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#analyze-progress", ProgressBar).display = False
        self.query_one("#analyze-plan", Static).display = False
        if self.fixed_recipe:
            self._set_recipe(self.fixed_recipe)
        self._update_cancel_note()

    # -- recipe and parameter controls --------------------------------------

    def _set_recipe(self, name: str) -> None:
        try:
            info = data.get_recipe_document(self.project, name)
        except ValidationError as exc:
            self.show_error(exc)
            return
        self.recipe_name = name
        self.recipe_doc = info["document"]
        entity_type = str(self.recipe_doc.get("entity_type", "") or "")
        entity_select = self.query_one("#analyze-entity-type", Select)
        entity_select.value = (
            entity_type if entity_type in dict(ENTITY_TYPE_OPTIONS) else Select.NULL
        )
        container = self.query_one("#analyze-parameters", Vertical)
        container.remove_children()
        self._param_names = []
        widgets: list[Any] = []
        parameters = self.recipe_doc.get("parameters", {}) or {}
        for param_name, spec in parameters.items():
            spec = spec if isinstance(spec, dict) else {}
            required = bool(spec.get("required")) and "default" not in spec
            label = f"{param_name} *" if required else param_name
            widgets.append(Static(label, classes="modal-label"))
            default = spec.get("default")
            choices = spec.get("choices")
            widget_id = f"analyze-param-{param_name}"
            if isinstance(choices, list) and choices:
                options = [(str(choice), str(choice)) for choice in choices]
                widgets.append(Select(
                    options,
                    value=str(default) if default is not None else Select.NULL,
                    id=widget_id, allow_blank=default is None,
                ))
            else:
                widgets.append(Input(
                    value="" if default is None else str(default),
                    placeholder=param_name, id=widget_id,
                ))
            self._param_names.append(str(param_name))
        if widgets:
            container.mount(*widgets)
        self.refresh_command()
        # Parameter widgets mount asynchronously; refresh again once they exist.
        self.call_after_refresh(self.refresh_command)

    def _parameter_values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for param_name in self._param_names:
            try:
                widget = self.query_one(f"#analyze-param-{param_name}")
            except NoMatches:  # not mounted yet
                continue
            if isinstance(widget, Select):
                value = "" if widget.value is Select.NULL else str(widget.value)
            else:
                value = widget.value.strip()
            if value:
                values[param_name] = value
        return values

    # -- command preview ------------------------------------------------------

    def _form_values(self) -> dict[str, Any]:
        entity_value = self.query_one("#analyze-entity-type", Select).value
        backend_value = self.query_one("#analyze-backend", Select).value
        return {
            "analysis": self.recipe_name or "",
            "parameters": self._parameter_values(),
            "entity_type": "" if entity_value is Select.NULL else str(entity_value),
            "entity_id": self.query_one("#analyze-entity-id", Input).value.strip(),
            "limit": self.query_one("#analyze-limit", Input).value.strip(),
            "threads": self.query_one("#analyze-threads", Input).value.strip(),
            "backend": "" if backend_value is Select.NULL else str(backend_value),
            "dry_run": self.query_one("#analyze-dry-run", Checkbox).value,
            "force": self.query_one("#analyze-force", Checkbox).value,
            "keep_partial": self.query_one("#analyze-keep-partial", Checkbox).value,
        }

    def command_text(self) -> str:
        values = self._form_values()
        parts = ["operon", "analyze", "--analysis", shlex.quote(values["analysis"] or "…")]
        for name, value in values["parameters"].items():
            parts += ["--param", shlex.quote(f"{name}={value}")]
        for field, flag in (("entity_type", "--entity-type"), ("entity_id", "--entity-id"),
                            ("limit", "--limit"), ("threads", "--threads"),
                            ("backend", "--backend")):
            if values[field]:
                parts += [flag, shlex.quote(values[field])]
        for field, flag in (("dry_run", "--dry-run"), ("force", "--force"),
                            ("keep_partial", "--keep-partial")):
            if values[field]:
                parts.append(flag)
        return " ".join(parts)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("analyze-"):
            self.refresh_command()

    def on_select_changed(self, event: Select.Changed) -> None:
        widget_id = event.select.id or ""
        if widget_id == "analyze-recipe":
            if event.value is not Select.NULL:
                self._set_recipe(str(event.value))
        elif widget_id.startswith("analyze-"):
            if widget_id == "analyze-backend":
                self._update_cancel_note()
            self.refresh_command()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id and event.checkbox.id.startswith("analyze-"):
            self.refresh_command()

    # -- execution ------------------------------------------------------------

    @staticmethod
    def _positive_int(text: str, label: str) -> int | None:
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            raise ValidationError(f"{label} must be a positive integer") from None
        if value <= 0:
            raise ValidationError(f"{label} must be a positive integer")
        return value

    def confirm(self) -> None:
        if self.running:
            return
        values = self._form_values()
        if not values["analysis"]:
            self.show_error("select a recipe")
            return
        try:
            limit = self._positive_int(values["limit"], "limit")
            threads = self._positive_int(values["threads"], "threads")
            # Same validation the CLI applies, before any worker starts —
            # including the execution backend: a missing sbatch or an
            # incomplete SSH block is an inline error, not a failed run.
            from operon.tools import get_recipe, resolve_runtime_parameters
            recipe = get_recipe(self.project, values["analysis"])
            resolve_runtime_parameters(recipe, values["parameters"])
            backend_info = actions.preflight_backend(
                self.project, values["backend"] or None,
                recipe_name=values["analysis"],
            )
        except ValidationError as exc:
            self.show_error(exc)
            return
        self._options = {
            "entity_type": values["entity_type"] or None,
            "entity_id": values["entity_id"] or None,
            "limit": limit,
            "threads": threads,
            "backend": values["backend"] or None,
            "dry_run": values["dry_run"],
            "force": values["force"],
            "keep_partial": values["keep_partial"],
            "parameters": values["parameters"],
        }
        self._analysis_name = values["analysis"]
        self._executor_name = str(backend_info["backend"])
        self._executor_description = str(backend_info["description"])
        self._cancel_event = threading.Event()
        self.running = True
        self.done = 0
        self.total = 0
        self._set_controls_disabled(True)
        self.set_confirm_enabled(False)
        self.clear_error()
        self.query_one("#analyze-plan", Static).display = False
        self.query_one("#analyze-progress", ProgressBar).display = not values["dry_run"]
        self.query_one("#analyze-status", Static).update(
            f"running… (backend: {self._executor_description})"
        )
        self._worker = self._run_analysis()

    def _set_controls_disabled(self, disabled: bool) -> None:
        for widget in self.query("Input, Select, Checkbox"):
            widget.disabled = disabled

    def _request_cancel(self) -> None:
        """Cancel cooperatively: set the event, then cancel the worker."""
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._worker is not None:
            self._worker.cancel()
        note = ("cancelling… (the submitted job is cancelled at the next scheduler poll)"
                if self._executor_name != "local" else "cancelling…")
        try:
            self.query_one("#analyze-status", Static).update(note)
        except NoMatches:  # pragma: no cover - modal teardown race
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel" and self.running:
            self._request_cancel()
            return
        super().on_button_pressed(event)

    def action_cancel(self) -> None:
        if self.running:
            self._request_cancel()
            return
        self.dismiss(None)

    @work(thread=True)
    def _run_analysis(self) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()

        def progress(index: int, total: int, file_id: str, phase: str) -> None:
            if worker.is_cancelled:
                raise actions.AnalysisCancelled()
            try:
                self.app.call_from_thread(self._progress, index, total, file_id, phase)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

        try:
            payload: Any = actions.run_analysis(
                self.project, self._analysis_name, progress=progress,
                cancel_event=self._cancel_event, **self._options,
            )
        except Exception as exc:  # noqa: BLE001 - routed to _analysis_done
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._analysis_done, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _progress(self, index: int, total: int, file_id: str, phase: str) -> None:
        self.done = index
        self.total = total
        self.query_one("#analyze-progress", ProgressBar).update(total=total, progress=index)
        self.query_one("#analyze-status", Static).update(f"{index}/{total} · {file_id}: {phase}")

    def _analysis_done(self, payload: Any) -> None:
        self.running = False
        if isinstance(payload, actions.AnalysisCancelled):
            self.app.notify(
                f"analysis cancelled after {self.done}/{self.total} file(s); "
                "completed files kept",
                severity="warning",
            )
            self.dismiss({"cancelled": True, "done": self.done, "total": self.total})
            return
        if isinstance(payload, BaseException):
            self._set_controls_disabled(False)
            self.set_confirm_enabled(True)
            self.show_error(payload)
            return
        if payload["dry_run"]:
            self._set_controls_disabled(False)
            self.set_confirm_enabled(True)
            self.query_one("#analyze-progress", ProgressBar).display = False
            self.query_one("#analyze-status", Static).update("")
            self._show_plan(payload)
            return
        severity = "warning" if payload["errors"] else "information"
        self.app.notify(
            f"analysis {payload['analysis']}: {payload['succeeded']}/{payload['total']} succeeded",
            severity=severity,
        )
        self.dismiss(payload)

    def _show_plan(self, payload: dict[str, Any]) -> None:
        lines = [
            f"{result.get('file_id', '')}  {result.get('status', '')}  "
            f"{result.get('command', '')}"
            for result in payload["results"]
        ]
        text = "dry-run plan (nothing executed, nothing written)\n"
        note = payload.get("messages", "").strip()
        if note:
            text += note + "\n"
        text += "\n".join(lines) if lines else "(no candidate files)"
        text += "\n\nUncheck dry-run and confirm again to execute."
        plan = self.query_one("#analyze-plan", Static)
        plan.update(Text(text))
        plan.display = True


def analysis_finished(app: Any, payload: Any) -> None:
    """Shared dismiss callback for the Config and Runs entry points."""
    if not payload or payload.get("cancelled"):
        return
    failures = [
        result for result in payload.get("results", [])
        if result.get("status") in FAILURE_STATUSES
    ]
    if failures:
        lines = "\n".join(
            f"{result['file_id']}: {result.get('error') or result.get('status')}"
            for result in failures[:20]
        )
        app.push_screen(ErrorDialog(
            f"{len(failures)} of {payload['total']} file(s) failed analysis", lines,
        ))
    app.reload_after_write()
    app.action_switch_screen("runs")
