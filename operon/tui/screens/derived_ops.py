"""Derived-artifact modals for the Files screen: extract, select, adopt, fanout.

These four commands all read registered FASTAs (or a table) and produce
artifacts the manifest does not know yet:

* ``extract-domains`` writes a domain FASTA, ``select-sequences`` writes a
  sequence subset — both leave the output **unregistered**, so each modal hands
  its output path to the adopt dialog with ``derived_from`` prefilled.
* ``adopt`` registers such an output (or a whole manifest) with lineage edges
  back to every input; identical bytes for the same entity+role reuse the
  existing registration, different bytes raise a conflict inline.
* ``fanout`` splits registered FASTAs into per-unit files; the ``--dry-run``
  preflight is the mandatory first step here — the preview shows what would be
  created or reused, and only then does Confirm run for real.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from typing import Any

from rich.text import Text
from textual import work
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Select, Static

from operon.config import Project
from operon.tui import actions
from operon.tui.screens.common import ENTITY_TYPE_OPTIONS, WriteModal


def _split_ids(text: str) -> list[str]:
    return [item.strip() for item in text.split(",") if item.strip()]


def _int_or_none(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.lstrip("-").isdigit() else None


def _float_or_none(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


class ExtractModal(WriteModal):
    """Form + confirm for `operon extract-domains` (output stays unregistered)."""

    def __init__(self, project: Project, file_id: str | None) -> None:
        super().__init__("Extract domains")
        self.project = project
        self.file_id = file_id or ""

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            f"file: {self.file_id or '(no file selected)'}  —  the output FASTA is not "
            "registered; adopt it afterwards to keep the lineage",
            classes="modal-info",
        )
        yield Static("Region source (mutually exclusive)", classes="modal-label")
        yield Select([("analysis hits", "analysis"), ("regions tsv", "tsv")],
                     value="analysis", id="extract-source-kind", allow_blank=False)
        yield Input(placeholder="analysis name (e.g. blastn_nt)", id="extract-analysis")
        yield Input(placeholder="regions tsv path", id="extract-regions-tsv", disabled=True)
        yield Select([("best hit only", "best"), ("all regions", "all")],
                     value="best", id="extract-region-mode", allow_blank=False)
        yield Input(value="5", placeholder="flank", id="extract-flank",
                    type="integer", restrict=r"\d*")
        yield Input(value="30", placeholder="min length", id="extract-min-length",
                    type="integer", restrict=r"\d*")
        yield Input(placeholder="subject-like (analysis only)", id="extract-subject-like")
        yield Input(placeholder="evalue max (analysis only)", id="extract-evalue-max",
                    restrict=r"[0-9eE.+-]*")
        yield Input(placeholder="out path (required)", id="extract-out")
        yield Input(placeholder="manifest tsv (optional)", id="extract-manifest")

    def _values(self) -> dict[str, Any]:
        kind_value = self.query_one("#extract-source-kind", Select).value
        kind = "analysis" if kind_value is Select.NULL else str(kind_value)
        mode_value = self.query_one("#extract-region-mode", Select).value
        return {
            "file_id": self.file_id,
            "out": self.query_one("#extract-out", Input).value.strip(),
            "analysis": (self.query_one("#extract-analysis", Input).value.strip() or None)
            if kind == "analysis" else None,
            "regions_tsv": (self.query_one("#extract-regions-tsv", Input).value.strip() or None)
            if kind == "tsv" else None,
            "flank": _int_or_none(self.query_one("#extract-flank", Input).value) or 0,
            "min_length": _int_or_none(self.query_one("#extract-min-length", Input).value) or 1,
            "best_only": mode_value != "all",
            "subject_like": (self.query_one("#extract-subject-like", Input).value.strip() or None)
            if kind == "analysis" else None,
            "evalue_max": _float_or_none(self.query_one("#extract-evalue-max", Input).value)
            if kind == "analysis" else None,
            "manifest": self.query_one("#extract-manifest", Input).value.strip() or None,
        }

    def command_text(self) -> str:
        values = self._values()
        parts = ["operon", "extract-domains", "--file-id", shlex.quote(values["file_id"] or "…")]
        if values["analysis"]:
            parts += ["--analysis", shlex.quote(str(values["analysis"]))]
        else:
            parts += ["--regions-tsv", shlex.quote(str(values["regions_tsv"] or "…"))]
        parts += ["--flank", str(values["flank"]), "--min-length", str(values["min_length"])]
        parts.append("--best-only" if values["best_only"] else "--all-regions")
        for field, flag in (("subject_like", "--subject-like"), ("evalue_max", "--evalue-max")):
            if values[field] is not None:
                parts += [flag, shlex.quote(str(values[field]))]
        parts += ["--out", shlex.quote(values["out"] or "…")]
        if values["manifest"]:
            parts += ["--manifest", shlex.quote(str(values["manifest"]))]
        return " ".join(parts)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "extract-source-kind":
            self.query_one("#extract-regions-tsv", Input).disabled = str(event.value) != "tsv"
            self.query_one("#extract-analysis", Input).disabled = str(event.value) == "tsv"
        self.refresh_command()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("extract-"):
            self.refresh_command()

    def confirm(self) -> None:
        values = self._values()
        if not values["file_id"]:
            self.show_error("select a file first")
            return
        if not values["out"]:
            self.show_error("out path is required")
            return
        if not values["analysis"] and not values["regions_tsv"]:
            self.show_error("exactly one of --analysis or --regions-tsv is required")
            return
        self.run_action(lambda: actions.extract_domains(self.project, **values))

    def on_action_success(self, payload: Any) -> None:
        self.app.notify(
            f"extracted {payload['extracted']} region record(s), excluded "
            f"{payload['excluded']} → {payload['output']}"
        )
        self.dismiss(payload)


class SelectSequencesModal(WriteModal):
    """Form + confirm for `operon select-sequences` (output stays unregistered)."""

    def __init__(self, project: Project, file_id: str | None) -> None:
        super().__init__("Select sequences")
        self.project = project
        self.file_id = file_id or ""

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            f"file: {self.file_id or '(no file selected)'}  —  the output FASTA is not "
            "registered; adopt it afterwards to keep the lineage",
            classes="modal-info",
        )
        yield Static("Analyses (one per line; OR-ed)", classes="modal-label")
        yield Input(placeholder="analysis name", id="select-analysis-1")
        yield Input(placeholder="analysis name", id="select-analysis-2")
        yield Input(placeholder="subject-like", id="select-subject-like")
        yield Input(placeholder="evalue max", id="select-evalue-max", restrict=r"[0-9eE.+-]*")
        yield Input(placeholder="min span", id="select-min-span",
                    type="integer", restrict=r"\d*")
        yield Input(placeholder="hit type (extra_json hit_type)", id="select-hit-type")
        yield Static("Hit requirement (mutually exclusive)", classes="modal-label")
        yield Select([("require a hit", "hit"), ("require no hit", "no-hit")],
                     value="hit", id="select-requirement", allow_blank=False)
        yield Static("Entity filter (optional)", classes="modal-label")
        yield Select(ENTITY_TYPE_OPTIONS, id="select-entity-type", allow_blank=True)
        yield Input(placeholder="entity id", id="select-entity-id")
        yield Input(placeholder="out path (required)", id="select-out")
        yield Input(placeholder="manifest tsv (optional)", id="select-manifest")

    def _values(self) -> dict[str, Any]:
        entity_type = self.query_one("#select-entity-type", Select).value
        requirement = self.query_one("#select-requirement", Select).value
        return {
            "file_id": self.file_id,
            "out": self.query_one("#select-out", Input).value.strip(),
            "analyses": [
                value for value in (
                    self.query_one(f"#select-analysis-{index}", Input).value.strip()
                    for index in (1, 2)
                ) if value
            ],
            "subject_like": self.query_one("#select-subject-like", Input).value.strip() or None,
            "evalue_max": _float_or_none(self.query_one("#select-evalue-max", Input).value),
            "min_span": _int_or_none(self.query_one("#select-min-span", Input).value),
            "hit_type": self.query_one("#select-hit-type", Input).value.strip() or None,
            "require_hit": requirement != "no-hit",
            "entity_type": None if entity_type is Select.NULL else str(entity_type),
            "entity_id": self.query_one("#select-entity-id", Input).value.strip() or None,
            "manifest": self.query_one("#select-manifest", Input).value.strip() or None,
        }

    def command_text(self) -> str:
        values = self._values()
        parts = ["operon", "select-sequences", "--file-id", shlex.quote(values["file_id"] or "…")]
        for name in values["analyses"]:
            parts += ["--analysis", shlex.quote(name)]
        for field, flag in (("subject_like", "--subject-like"),
                            ("evalue_max", "--evalue-max"), ("min_span", "--min-span"),
                            ("hit_type", "--hit-type")):
            if values[field] is not None:
                parts += [flag, shlex.quote(str(values[field]))]
        parts.append("--require-hit" if values["require_hit"] else "--require-no-hit")
        for field, flag in (("entity_type", "--entity-type"), ("entity_id", "--entity-id")):
            if values[field]:
                parts += [flag, shlex.quote(str(values[field]))]
        parts += ["--out", shlex.quote(values["out"] or "…")]
        if values["manifest"]:
            parts += ["--manifest", shlex.quote(str(values["manifest"]))]
        return " ".join(parts)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in {"select-entity-type", "select-requirement"}:
            self.refresh_command()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("select-"):
            self.refresh_command()

    def confirm(self) -> None:
        values = self._values()
        if not values["file_id"]:
            self.show_error("select a file first")
            return
        if not values["out"]:
            self.show_error("out path is required")
            return
        if not values["analyses"] and values["subject_like"] is None \
                and values["evalue_max"] is None and values["min_span"] is None \
                and values["hit_type"] is None:
            self.show_error(
                "no hit criteria given; pass at least one of --analysis, --subject-like, "
                "--evalue-max, --min-span or --hit-type"
            )
            return
        self.run_action(lambda: actions.select_sequences(self.project, **values))

    def on_action_success(self, payload: Any) -> None:
        self.app.notify(
            f"selected {payload['selected']} of {payload['total']} sequence(s), "
            f"excluded {payload['excluded']} → {payload['output']}"
        )
        self.dismiss(payload)


class AdoptModal(WriteModal):
    """Form + confirm for `operon adopt` (single item or a manifest)."""

    def __init__(
            self,
            project: Project,
            *,
            path: str | None = None,
            derived_from: Iterable[str] = (),
            entity_type: str | None = None,
            entity_id: str | None = None,
    ) -> None:
        super().__init__("Adopt derived file")
        self.project = project
        self.initial_path = path or ""
        self.initial_derived_from = list(derived_from)
        self.initial_entity_type = entity_type
        self.initial_entity_id = entity_id
        self.manifest_count: int | None = None
        self.previewed_path = ""

    def compose_form(self) -> Iterable[Any]:
        yield Static("Mode", classes="modal-label")
        yield Select([("single file", "single"), ("manifest file", "manifest")],
                     value="single", id="adopt-mode", allow_blank=False)
        yield Static("Single item", classes="modal-label")
        yield Input(value=self.initial_path, placeholder="path (required)", id="adopt-path")
        yield Select(
            ENTITY_TYPE_OPTIONS,
            value=self.initial_entity_type or "annotation",
            id="adopt-entity-type", allow_blank=False,
        )
        yield Input(value=str(self.initial_entity_id or ""), placeholder="entity id (required)",
                    id="adopt-entity-id")
        yield Input(placeholder="role (required)", id="adopt-role")
        yield Input(placeholder="format (auto-detect)", id="adopt-format")
        yield Input(placeholder="compression (auto-detect)", id="adopt-compression")
        yield Input(value=", ".join(self.initial_derived_from),
                    placeholder="derived_from file ids (comma separated; at least one)",
                    id="adopt-derived-from")
        yield Input(placeholder="workflow run id (optional)", id="adopt-workflow-run-id")
        yield Input(placeholder="actor (default $USER)", id="adopt-actor")
        yield Static("Manifest", classes="modal-label")
        yield Input(placeholder="manifest path (json or tsv)", id="adopt-manifest", disabled=True)
        yield Static("", id="adopt-preview")
        with Horizontal(classes="config-buttons"):
            yield Button("Preview manifest", id="adopt-preview-button", disabled=True)

    def _mode(self) -> str:
        value = self.query_one("#adopt-mode", Select).value
        return "single" if value is Select.NULL else str(value)

    def _single_values(self) -> dict[str, Any]:
        entity_type = self.query_one("#adopt-entity-type", Select).value
        return {
            "path": self.query_one("#adopt-path", Input).value.strip(),
            "entity_type": "" if entity_type is Select.NULL else str(entity_type),
            "entity_id": self.query_one("#adopt-entity-id", Input).value.strip(),
            "role": self.query_one("#adopt-role", Input).value.strip(),
            "format": self.query_one("#adopt-format", Input).value.strip() or None,
            "compression": self.query_one("#adopt-compression", Input).value.strip() or None,
            "derived_from": _split_ids(self.query_one("#adopt-derived-from", Input).value),
            "workflow_run_id": self.query_one("#adopt-workflow-run-id", Input).value.strip() or None,
        }

    def _actor(self) -> str | None:
        return self.query_one("#adopt-actor", Input).value.strip() or None

    def command_text(self) -> str:
        actor = self._actor()
        actor_part = f" --actor {shlex.quote(actor)}" if actor else ""
        if self._mode() == "manifest":
            manifest = self.query_one("#adopt-manifest", Input).value.strip() or "…"
            return f"operon adopt --from-manifest {shlex.quote(manifest)}{actor_part}"
        values = self._single_values()
        parts = ["operon", "adopt", "--file", shlex.quote(values["path"] or "…")]
        for field, flag in (("entity_type", "--entity-type"), ("entity_id", "--entity-id"),
                            ("role", "--role"), ("format", "--format"),
                            ("compression", "--compression")):
            if values[field]:
                parts += [flag, shlex.quote(str(values[field]))]
        for file_id in values["derived_from"]:
            parts += ["--derived-from", shlex.quote(file_id)]
        if values["workflow_run_id"]:
            parts += ["--workflow-run-id", shlex.quote(str(values["workflow_run_id"]))]
        return " ".join(parts) + actor_part

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "adopt-mode":
            manifest_mode = self._mode() == "manifest"
            for widget_id in ("adopt-path", "adopt-entity-type", "adopt-entity-id",
                              "adopt-role", "adopt-format", "adopt-compression",
                              "adopt-derived-from", "adopt-workflow-run-id"):
                self.query_one(f"#{widget_id}").disabled = manifest_mode
            self.query_one("#adopt-manifest", Input).disabled = not manifest_mode
            self.query_one("#adopt-preview-button", Button).disabled = not manifest_mode
            self.manifest_count = None
            self.query_one("#adopt-preview", Static).update("")
            # The manifest form only runs once its contents were previewed.
            self.set_confirm_enabled(not manifest_mode)
        self.refresh_command()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("adopt-"):
            if event.input.id == "adopt-manifest":
                # A queued/repeated change for the same path must not discard a
                # fresh preview; only a different path invalidates it.
                if self.manifest_count is not None and event.value.strip() == self.previewed_path:
                    self.refresh_command()
                    return
                self.manifest_count = None
                self.query_one("#adopt-preview", Static).update("")
                self.set_confirm_enabled(False)
            self.refresh_command()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "adopt-preview-button":
            event.stop()
            self._preview_manifest()
            return
        super().on_button_pressed(event)

    @work(thread=True)
    def _preview_manifest(self) -> None:
        from operon.lineage import load_adopt_manifest

        path = self.query_one("#adopt-manifest", Input).value.strip()
        try:
            payload: Any = len(load_adopt_manifest(path))
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._apply_preview, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _apply_preview(self, payload: Any) -> None:
        view = self.query_one("#adopt-preview", Static)
        if isinstance(payload, BaseException):
            view.update(Text(f"error: {payload}", style="red"))
            self.set_confirm_enabled(False)
            return
        self.manifest_count = int(payload)
        self.previewed_path = self.query_one("#adopt-manifest", Input).value.strip()
        view.update(f"manifest parsed: {self.manifest_count} item(s) — confirm to register")
        self.set_confirm_enabled(self.manifest_count > 0)
        self.refresh_command()

    def confirm(self) -> None:
        if self._mode() == "manifest":
            manifest = self.query_one("#adopt-manifest", Input).value.strip()
            if not manifest:
                self.show_error("manifest path is required")
                return
            if not self.manifest_count:
                self.show_error("preview the manifest first")
                return
            self.run_action(lambda: actions.adopt(self.project, manifest=manifest,
                                                  actor=self._actor()))
            return
        values = self._single_values()
        missing = [label for label, key in (("path", "path"), ("entity id", "entity_id"),
                                            ("role", "role"))
                   if not values[key]]
        if missing:
            self.show_error(f"single-file adopt requires {', '.join(missing)}")
            return
        if not values["derived_from"]:
            self.show_error("single-file adopt requires at least one derived_from FILE_ID")
            return
        item = {key: value for key, value in values.items()}
        self.run_action(lambda: actions.adopt(self.project, items=[item], actor=self._actor()))

    def on_action_success(self, payload: Any) -> None:
        if payload.get("reused"):
            self.app.notify(
                f"registered {payload['registered']} file(s); "
                f"{payload['reused']} reused an identical registration"
            )
        else:
            self.app.notify(f"registered {payload['registered']}: "
                            f"{', '.join(payload['file_ids'])}")
        self.dismiss(payload)


class FanoutModal(WriteModal):
    """Form + dry-run preflight + confirm for `operon fanout`."""

    def __init__(self, project: Project, source_file_id: str | None = None) -> None:
        super().__init__("Fan out units")
        self.project = project
        self.initial_source = source_file_id or ""
        self.preview: dict[str, Any] | None = None
        self.preview_values: dict[str, Any] = {}

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            "splits registered FASTAs into per-unit files under analysis/derived/ using an "
            "assignments table; the dry run below performs the real preflight and writes "
            "nothing",
            classes="modal-info",
        )
        yield Input(placeholder="assignments file id (required)", id="fanout-assignments")
        yield Input(value=self.initial_source,
                    placeholder="source file ids (comma separated; at least one)",
                    id="fanout-sources")
        yield Select(ENTITY_TYPE_OPTIONS, value="annotation", id="fanout-entity-type",
                     allow_blank=False)
        yield Input(placeholder="entity id (required)", id="fanout-entity-id")
        yield Input(placeholder="role prefix (required)", id="fanout-role-prefix")
        yield Input(value="unit", placeholder="unit column", id="fanout-unit-column")
        yield Input(value="seqid", placeholder="seqid column", id="fanout-seqid-column")
        yield Input(placeholder="parent run id (optional)", id="fanout-parent-run")
        yield Input(placeholder="actor (default $USER)", id="fanout-actor")
        yield Static("", id="fanout-preview")
        with Horizontal(classes="config-buttons"):
            yield Button("Dry run (preflight)", id="fanout-preview-button")
        yield DataTable(id="fanout-preview-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#fanout-preview-table", DataTable)
        table.add_columns("unit", "sequences", "role", "status")
        # Running for real only unlocks after a clean dry run.
        self.set_confirm_enabled(False)

    def _values(self) -> dict[str, Any]:
        entity_type = self.query_one("#fanout-entity-type", Select).value
        return {
            "assignments_file_id": self.query_one("#fanout-assignments", Input).value.strip(),
            "source_file_ids": _split_ids(self.query_one("#fanout-sources", Input).value),
            "entity_type": "" if entity_type is Select.NULL else str(entity_type),
            "entity_id": self.query_one("#fanout-entity-id", Input).value.strip(),
            "role_prefix": self.query_one("#fanout-role-prefix", Input).value.strip(),
            "unit_column": self.query_one("#fanout-unit-column", Input).value.strip() or "unit",
            "seqid_column": self.query_one("#fanout-seqid-column", Input).value.strip() or "seqid",
            "parent_run_id": self.query_one("#fanout-parent-run", Input).value.strip() or None,
            "actor": self.query_one("#fanout-actor", Input).value.strip() or None,
        }

    def command_text(self) -> str:
        values = self._values()
        parts = ["operon", "fanout",
                 "--assignments-file", shlex.quote(values["assignments_file_id"] or "…")]
        for file_id in values["source_file_ids"]:
            parts += ["--source-file", shlex.quote(file_id)]
        parts += ["--entity-type", values["entity_type"] or "…",
                  "--entity-id", shlex.quote(values["entity_id"] or "…"),
                  "--role-prefix", shlex.quote(values["role_prefix"] or "…"),
                  "--unit-column", values["unit_column"],
                  "--seqid-column", values["seqid_column"]]
        if values["parent_run_id"]:
            parts += ["--parent-run-id", shlex.quote(str(values["parent_run_id"]))]
        if values["actor"]:
            parts += ["--actor", shlex.quote(str(values["actor"]))]
        return " ".join(parts)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "fanout-entity-type":
            self.refresh_command()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("fanout-"):
            # Only a real edit invalidates the dry run: a queued/repeated change
            # event carrying the same values must not discard a fresh preview.
            if self.preview is not None and self._values() == self.preview_values:
                self.refresh_command()
                return
            self.preview = None
            self.set_confirm_enabled(False)
            self.refresh_command()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "fanout-preview-button":
            event.stop()
            self.run_dry_run()
            return
        super().on_button_pressed(event)

    def _missing(self) -> list[str]:
        values = self._values()
        missing = [name for name, value in (
            ("assignments file", values["assignments_file_id"]),
            ("entity id", values["entity_id"]),
            ("role prefix", values["role_prefix"]),
        ) if not value]
        if not values["source_file_ids"]:
            missing.append("source file ids")
        return missing

    def run_dry_run(self) -> None:
        missing = self._missing()
        if missing:
            self.show_error(f"fanout requires {', '.join(missing)}")
            return
        self.clear_error()
        self._dry_run(self._values())

    @work(thread=True)
    def _dry_run(self, values: dict[str, Any]) -> None:
        try:
            payload: Any = actions.fanout(self.project, dry_run=True, **values)
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal
            payload = exc
        if self.app.is_running:
            try:
                self.app.call_from_thread(self._dry_run_done, payload)
            except RuntimeError:  # pragma: no cover - app is shutting down
                pass

    def _dry_run_done(self, payload: Any) -> None:
        table = self.query_one("#fanout-preview-table", DataTable)
        view = self.query_one("#fanout-preview", Static)
        if isinstance(payload, BaseException):
            self.preview = None
            self.set_confirm_enabled(False)
            table.clear()
            view.update(Text(f"dry run failed: {payload}", style="red"))
            return
        self.preview = payload
        self.preview_values = self._values()
        table.clear()
        for unit in payload["units"]:
            table.add_row(unit["unit"], str(unit["sequences"]), unit["role"], unit["status"])
        created = sum(1 for unit in payload["units"] if unit["status"] == "would_create")
        view.update(
            f"dry run: {len(payload['units'])} planned unit(s) ({created} would_create, "
            f"{len(payload['units']) - created} would_reuse); nothing was written"
        )
        self.set_confirm_enabled(bool(payload["units"]))

    def confirm(self) -> None:
        if self.preview is None:
            self.show_error("run the dry run first")
            return
        values = self._values()
        self.run_action(lambda: actions.fanout(self.project, dry_run=False, **values))

    def on_action_success(self, payload: Any) -> None:
        self.app.notify(
            f"fanout: {payload['created']} created, {payload['reused']} reused "
            f"(run {payload['run_id']})"
        )
        self.dismiss(payload)
