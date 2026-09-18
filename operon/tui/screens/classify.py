"""Classify-sequences modal: run a ``sequence_classification`` profile.

Mirrors ``operon classify-sequences --profile``: the run goes through the same
core function (one transaction, one run row, audit rows only for changed
labels) and the summary is the CLI's own report — labeled/unlabeled counts, a
label table, labels written/removed with the run id, and the two warnings the
CLI prints (ignored completed jobs, target files without registered sequences).
The modal stays open after a run so the profile can be re-run: with unchanged
inputs and profile the summary reports 0 changes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from rich.text import Text
from textual.widgets import Button, Static

from operon.config import Project
from operon.tui import actions
from operon.tui.screens.common import WriteModal


class ClassifyModal(WriteModal):
    """Confirm + run for `operon classify-sequences`."""

    def __init__(self, project: Project, profile_name: str,
                 document: dict[str, Any] | None = None) -> None:
        super().__init__(f"Run classify {profile_name}")
        self.project = project
        self.profile_name = profile_name
        self.document = document or {}

    def compose_form(self) -> Iterable[Any]:
        applies_to = self.document.get("applies_to")
        applies_to = applies_to if isinstance(applies_to, dict) else {}
        sources = self.document.get("sources")
        rules = self.document.get("rules")
        yield Static(
            f"labels sequences of {applies_to.get('entity_type', '?')} / "
            f"{applies_to.get('file_role', '?')} files from "
            f"{len(sources) if isinstance(sources, dict) else 0} source(s) with "
            f"{len(rules) if isinstance(rules, list) else 0} rule(s).  The run reads the "
            "profile from disk (the saved snapshot), writes sequence_labels rows in one "
            "transaction, and audits every label change.",
            classes="modal-info",
        )
        yield Static("", id="classify-summary")

    def command_text(self) -> str:
        return f"operon classify-sequences --profile {self.profile_name}"

    def confirm(self) -> None:
        self.run_action(lambda: actions.run_classify(self.project, self.profile_name))

    def on_action_success(self, payload: Any) -> None:
        self.query_one("#classify-summary", Static).update(self.summary_text(payload))
        self.query_one("#confirm", Button).label = "Run again"
        labeled = payload["sequences"] - payload["unlabeled"]
        self.app.notify(
            f"classify {payload['profile']}: {labeled}/{payload['sequences']} labeled, "
            f"{payload['labels_written']} written, {payload['labels_removed']} removed "
            f"(run {payload['run_id']})"
        )

    @staticmethod
    def summary_text(payload: dict[str, Any]) -> Text:
        """The CLI's classify report, rendered for the modal."""
        labeled = payload["sequences"] - payload["unlabeled"]
        text = Text()
        text.append(
            f"labeled {labeled} of {payload['sequences']} sequence(s) across "
            f"{payload['files']} file(s) with profile {payload['profile']}; "
            f"unlabeled: {payload['unlabeled']}\n"
        )
        counts = payload.get("label_counts") or {}
        if counts:
            width = max(len(str(label)) for label in counts) + 1
            text.append("\n")
            for label, count in counts.items():
                text.append(f"  {label!s:<{width}} {count}\n", style="bold")
        text.append(
            f"\nlabels written: {payload['labels_written']}, "
            f"removed: {payload['labels_removed']} (run {payload['run_id']})\n"
        )
        if not payload["labels_written"] and not payload["labels_removed"]:
            text.append("unchanged — no label row was rewritten\n", style="dim")
        if payload.get("ignored_completed_jobs"):
            text.append(
                f"warning: ignored {payload['ignored_completed_jobs']} older completed "
                "analysis job(s); only the latest completed job per analysis and file "
                "contributes hits\n",
                style="yellow",
            )
        if payload.get("files_without_sequences"):
            text.append(
                f"warning: skipped {payload['files_without_sequences']} target file(s) "
                "with no registered sequences\n",
                style="yellow",
            )
        return text
