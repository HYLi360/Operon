"""TUI/CLI parity registry: every CLI leaf command is accounted for.

The CLI is the normative surface: the TUI must never run ahead of it.
Every leaf command of the ``operon`` argument tree appears exactly once in
:data:`REGISTRY`, with one of three statuses:

- ``implemented`` — the TUI has an entry point for the command.  ``actions``
  names the backing function (``"actions.run_qc"`` for
  :mod:`operon.tui.actions`, ``"data.list_decisions"`` for the read-only
  :mod:`operon.tui.data` layer) and ``modal`` the form class as
  ``"module::Class"`` (empty for read-only commands without a dialog).
- ``cli-only`` — deliberately not in the TUI; ``note`` says why.
- ``planned`` — a registered gap; ``note`` names the owning milestone
  (see ``HPC/cli_tui_gaps.md`` section 九).

Parameter mapping convention (checked by ``tests/unit/test_tui_cli_parity.py``):

- ``params`` and ``waived`` are keyed by the argparse **dest** of the CLI
  flag (``"entity_type"`` for ``--entity-type``), never by the flag string.
- ``params`` values name the TUI widget id that models the flag
  (``"analyze-limit"``), or a ``"context: ..."`` string when the value comes
  from screen context (current selection, fixed recipe, screen toggle).
- ``waived`` values are non-empty reasons the flag is not modeled in the
  TUI, with milestone attribution where the gap is scheduled.
- Positional arguments and ``-h/--help`` are excluded from the mapping:
  positionals are supplied by screen context (selected row, wizard flow) and
  carry no flag to mirror; ``--help`` is argparse plumbing.

This module is intentionally free of Textual imports so the parity tests
run in environments without the ``tui`` extra; modal classes are resolved
lazily by string reference.

Set ``OPERON_PARITY_STRICT=1`` to turn ``planned`` entries into failures
(intended for the M5 close-out).
"""

from __future__ import annotations

import argparse
import importlib
import os
from dataclasses import dataclass, field
from typing import Any

STATUS_IMPLEMENTED = "implemented"
STATUS_CLI_ONLY = "cli-only"
STATUS_PLANNED = "planned"
STATUSES = frozenset({STATUS_IMPLEMENTED, STATUS_CLI_ONLY, STATUS_PLANNED})

_ACTIONS_MODULES = ("operon.tui.actions", "operon.tui.data")


@dataclass(frozen=True)
class ParityEntry:
    """One CLI leaf command's TUI parity record."""

    command: tuple[str, ...]
    status: str
    note: str = ""
    actions: str = ""
    modal: str = ""
    params: dict[str, str] = field(default_factory=dict)
    waived: dict[str, str] = field(default_factory=dict)

    @property
    def command_text(self) -> str:
        return " ".join(self.command)


def iter_cli_commands() -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    """Walk the CLI argparse tree and return ``(path, parser)`` for leaves.

    A parser is a leaf when it has no *required* subparsers action of its
    own; container groups (``report``, ``workflow``, ``timetree``, ...) are
    not executable and are excluded.
    """
    from operon.cli import _parser

    def walk(
        parser: argparse.ArgumentParser,
        prefix: tuple[str, ...],
    ) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
        subparsers = next(
            (
                action
                for action in parser._actions
                if isinstance(action, argparse._SubParsersAction)
            ),
            None,
        )
        if subparsers is None or not subparsers.required:
            return [(prefix, parser)]
        leaves: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = []
        for name, sub in subparsers.choices.items():
            leaves.extend(walk(sub, prefix + (name,)))
        return leaves

    return walk(_parser(), ())


def cli_options(command: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Optional flags of one leaf command, keyed by argparse dest.

    Positional arguments and ``-h/--help`` are excluded.  Each value carries
    ``flags`` (option strings), ``default``, ``choices`` and ``required``.
    """
    for path, parser in iter_cli_commands():
        if path != command:
            continue
        options: dict[str, dict[str, Any]] = {}
        for action in parser._actions:
            if not action.option_strings or action.dest == "help":
                continue
            options[action.dest] = {
                "flags": list(action.option_strings),
                "default": action.default,
                "choices": list(action.choices) if action.choices is not None else None,
                "required": bool(action.required),
            }
        return options
    raise KeyError(f"unknown CLI command: {' '.join(command)}")


def resolve_action(entry: ParityEntry) -> Any:
    """Resolve ``entry.actions`` (``"actions.run_qc"`` / ``"data.list_x"``)."""
    module_name, _, attribute = entry.actions.partition(".")
    module = importlib.import_module(f"operon.tui.{module_name}")
    return getattr(module, attribute)


def resolve_modal(entry: ParityEntry) -> type:
    """Import and return the ``entry.modal`` class (``"module::Class"``)."""
    module_name, _, class_name = entry.modal.partition("::")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def strict_mode() -> bool:
    """True when ``OPERON_PARITY_STRICT`` demands zero planned gaps."""
    return os.environ.get("OPERON_PARITY_STRICT", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def strict_violations(registry: tuple[ParityEntry, ...] = ()) -> list[str]:
    """Commands whose ``planned`` status fails strict mode (empty if off)."""
    if not strict_mode():
        return []
    entries = registry or REGISTRY
    return [entry.command_text for entry in entries if entry.status == STATUS_PLANNED]


_M2B = "milestone M2b (remote execution and run introspection)"
_M3 = "milestone M3 (classification and derived-artifact loop)"
_M4 = "milestone M4 (import, QC and coverage self-service)"
_M5 = "milestone M5 (storage, administration and remaining alignment)"

REGISTRY: tuple[ParityEntry, ...] = (
    ParityEntry(
        ("evaluate",),
        STATUS_IMPLEMENTED,
        actions="actions.evaluate",
        modal="operon.tui.screens.decisions::EvaluateModal",
        params={
            "profile": "evaluate-profile",
            "entity_type": "evaluate-scope",
            "entity_id": "evaluate-scope",
        },
        waived={"yes": "the explicit Confirm step replaces --yes"},
    ),
    ParityEntry(
        ("curate",),
        STATUS_IMPLEMENTED,
        actions="actions.curate",
        modal="operon.tui.screens.decisions::CurateModal",
        params={
            "entity_type": "context: selected decision row",
            "entity_id": "context: selected decision row",
            "profile": "context: selected decision row",
            "decision": "curate-decision",
            "reviewer": "curate-reviewer",
            "reason": "curate-reason",
            "evidence": "curate-evidence",
        },
    ),
    ParityEntry(
        ("retire",),
        STATUS_IMPLEMENTED,
        actions="actions.lifecycle_apply",
        modal="operon.tui.screens.entities::LifecycleModal",
        params={
            "reason_code": "lifecycle-reason-code",
            "reason": "lifecycle-reason",
            "evidence": "lifecycle-evidence",
            "actor": "lifecycle-actor",
        },
        waived={
            "apply": "the plan preview plus Confirm step is the --apply flow",
            "yes": "the explicit Confirm step replaces --yes",
        },
    ),
    ParityEntry(
        ("restore",),
        STATUS_IMPLEMENTED,
        actions="actions.lifecycle_apply",
        modal="operon.tui.screens.entities::LifecycleModal",
        params={
            "reason": "lifecycle-reason",
            "evidence": "lifecycle-evidence",
            "actor": "lifecycle-actor",
        },
        waived={
            "apply": "the plan preview plus Confirm step is the --apply flow",
            "yes": "the explicit Confirm step replaces --yes",
        },
    ),
    ParityEntry(
        ("retired",),
        STATUS_IMPLEMENTED,
        note="partial: the Entities screen `t` toggle shows retired entities "
        "inline; a standalone retired list is not modeled",
        actions="data.entity_tree",
        waived={
            "direct_only": f"no standalone retired list in the TUI ({_M5})",
            "json": f"the TUI renders views, not JSON output ({_M5})",
        },
    ),
    ParityEntry(
        ("ingest",),
        STATUS_IMPLEMENTED,
        actions="actions.ingest",
        modal="operon.tui.screens.files_ops::IngestModal",
        params={
            "source": "ingest-source",
            "entity_type": "ingest-entity-type",
            "entity_id": "ingest-entity-id",
            "role": "ingest-role",
            "fmt": "ingest-format",
            "compression": "ingest-compression",
            "source_url": "ingest-source-url",
            "move": "ingest-move",
        },
    ),
    ParityEntry(
        ("verify",),
        STATUS_IMPLEMENTED,
        actions="actions.verify",
        modal="operon.tui.screens.files_ops::VerifyModal",
        params={"file_id": "context: Files screen selection (blank = all files)"},
    ),
    ParityEntry(
        ("qc",),
        STATUS_IMPLEMENTED,
        actions="actions.run_qc",
        modal="operon.tui.screens.files_ops::QcModal",
        params={
            "file_id": "context: Files screen selection (blank = all files)",
            "entity_type": "context: Files screen selection",
            "entity_id": "context: Files screen selection",
            "sample_size": "qc-sample-size",
            "phred_offset": "qc-phred-offset",
            "rehash": "qc-rehash",
        },
    ),
    ParityEntry(
        ("import", "dataset"),
        STATUS_IMPLEMENTED,
        actions="actions.import_dataset",
        modal="operon.tui.screens.import_wizard::ImportWizardScreen",
    ),
    ParityEntry(
        ("release",),
        STATUS_IMPLEMENTED,
        actions="actions.create_release",
        modal="operon.tui.screens.publish::CreateReleaseModal",
        params={
            "version": "release-version",
            "profile": "release-profile",
            "link": "release-link",
            "copy_files": "release-copy-files",
        },
    ),
    ParityEntry(
        ("export",),
        STATUS_IMPLEMENTED,
        actions="actions.export",
        modal="operon.tui.screens.publish::ExportModal",
        params={
            "output": "export-output",
            "entity_type": "export-entity-type",
            "entity_id": "export-entity-id",
            "file_id": "export-file-id",
            "file_role": "export-file-role",
            "fmt": "export-format",
            "state": "export-state",
            "decision": "export-decision",
            "profile": "export-profile",
            "link": "export-link",
            "no_qc": "export-include-qc",
        },
    ),
    ParityEntry(
        ("report", "coverage"),
        STATUS_IMPLEMENTED,
        actions="actions.run_coverage",
        modal="operon.tui.screens.coverage::CoverageModal",
        params={
            "reference_set": "coverage-reference-set",
            "scope": "coverage-scope",
            "release": "coverage-release",
        },
    ),
    ParityEntry(
        ("tools-check",),
        STATUS_IMPLEMENTED,
        actions="actions.check_tools",
        note="Config screen button with per-row streaming updates; no dialog",
    ),
    ParityEntry(
        ("recipes", "list"),
        STATUS_IMPLEMENTED,
        actions="data.list_recipes",
    ),
    ParityEntry(
        ("recipes", "history"),
        STATUS_IMPLEMENTED,
        actions="data.recipe_history",
        modal="operon.tui.screens.config::HistoryModal",
    ),
    ParityEntry(
        ("recipes", "show"),
        STATUS_IMPLEMENTED,
        actions="data.get_recipe_document",
        modal="operon.tui.screens.config::SnapshotViewModal",
        params={"snapshot_id": "context: HistoryModal snapshot selection"},
    ),
    ParityEntry(
        ("profiles", "history"),
        STATUS_IMPLEMENTED,
        note="qc and sequence_classification profiles share one snapshot table "
        f"(qc_profiles); taxonomy_coverage profiles are {_M4}",
        actions="data.profile_history",
        modal="operon.tui.screens.config::HistoryModal",
    ),
    ParityEntry(
        ("profiles", "show"),
        STATUS_IMPLEMENTED,
        note="qc and sequence_classification profiles share one snapshot table "
        f"(qc_profiles); taxonomy_coverage profiles are {_M4}",
        actions="data.get_profile_document",
        modal="operon.tui.screens.config::SnapshotViewModal",
        params={"snapshot_id": "context: HistoryModal snapshot selection"},
    ),
    ParityEntry(
        ("workflow", "list"),
        STATUS_IMPLEMENTED,
        note="partial: RunsPanel filters plus delegated time/id/tool/executor filters",
        actions="data.list_workflow_runs",
        params={
            "status": "runs-status",
            "step": "runs-step",
            "entity_type": "runs-entity",
            "entity_id": "runs-entity",
            "limit": "runs-limit",
            "started_from": "runs-from (ISO-8601)",
            "started_to": "runs-to (ISO-8601)",
            "run_id": "runs-run-id",
            "parent_run_id": "runs-parent-run-id",
            "tool": "runs-tool",
            "executor": "runs-executor",
            "offset": "runs-offset",
            "oldest_first": "runs-oldest-first",
        },
        waived={
            "resumes_run_id": f"no lineage filters in the Runs screen ({_M2B})",
            "format": f"the TUI renders a table, not machine formats ({_M2B})",
        },
    ),
    ParityEntry(
        ("workflow", "show"),
        STATUS_IMPLEMENTED,
        note="partial: RunDetailScreen with log follow; no JSON output",
        actions="data.workflow_run_detail",
        params={
            "follow": "run-follow (switch on the run detail screen, enabled while running)",
        },
        waived={
            "format": f"the TUI renders a detail view, not JSON ({_M2B})",
        },
    ),
    ParityEntry(
        ("report", "decisions"),
        STATUS_IMPLEMENTED,
        actions="data.list_decisions",
        params={"profile": "decisions-profile"},
        waived={
            "include_retired": "the Decisions screen shows effective decisions "
            f"without a retired filter ({_M5})",
        },
    ),
    ParityEntry(
        ("report", "qc"),
        STATUS_IMPLEMENTED,
        note="partial: QC metrics are embedded in the entity detail view",
        actions="data.entity_metrics",
        params={
            "entity_type": "context: Entities screen selection",
            "entity_id": "context: Entities screen selection",
            "include_retired": "context: Entities screen `t` toggle",
        },
        waived={"export": f"no TSV export from the TUI ({_M5})"},
    ),
    ParityEntry(
        ("status",),
        STATUS_IMPLEMENTED,
        note="read-only equivalent: Home dashboard plus Entities browser",
        actions="data.project_summary",
        params={
            "entity_type": "context: Entities screen selection",
            "entity_id": "context: Entities screen selection",
            "include_retired": "context: Entities screen `t` toggle",
        },
    ),
    ParityEntry(
        ("show",),
        STATUS_IMPLEMENTED,
        note="partial: entity detail view; no accession/NAMESPACE:ACC lookup "
        "and no organism-scope graph",
        actions="data.entity_detail",
        params={"include_retired": "context: Entities screen `t` toggle"},
        waived={
            "json": f"the TUI renders a detail view, not JSON ({_M5})",
            "scope": f"no organism-scope graph view ({_M5})",
            "include_superseded": f"the detail view shows current records ({_M5})",
        },
    ),
    ParityEntry(
        ("locations",),
        STATUS_IMPLEMENTED,
        note="partial: file locations are embedded in the Files detail view; "
        "no project-wide location listing",
        actions="data.file_detail",
        params={"file_id": "context: Files screen selection"},
    ),
    ParityEntry(
        ("taxonomy", "list"),
        STATUS_IMPLEMENTED,
        actions="data.list_taxonomy_snapshots",
    ),
    ParityEntry(
        ("taxonomy", "reference-sets"),
        STATUS_IMPLEMENTED,
        actions="data.list_reference_sets",
    ),
    ParityEntry(
        ("analyze",),
        STATUS_IMPLEMENTED,
        actions="actions.run_analysis",
        modal="operon.tui.screens.analyze::AnalyzeModal",
        params={
            "analysis": "analyze-recipe",
            "param": "analyze-param-<name> (one widget per recipe parameter)",
            "entity_type": "analyze-entity-type",
            "entity_id": "analyze-entity-id",
            "threads": "analyze-threads",
            "limit": "analyze-limit",
            "backend": "analyze-backend",
            "dry_run": "analyze-dry-run",
            "force": "analyze-force",
            "keep_partial": "analyze-keep-partial",
        },
    ),
    # -- intentionally CLI-only ---------------------------------------------
    ParityEntry(
        ("init",),
        STATUS_CLI_ONLY,
        note="the TUI starts on an existing project; project creation is a "
        "one-shot CLI act",
    ),
    ParityEntry(
        ("init-demo",),
        STATUS_CLI_ONLY,
        note="the TUI starts on an existing project; demo scaffolding is a "
        "one-shot CLI act",
    ),
    ParityEntry(
        ("tui",),
        STATUS_CLI_ONLY,
        note="launches the TUI itself; not applicable inside the TUI",
    ),
    ParityEntry(
        ("query",),
        STATUS_CLI_ONLY,
        note="arbitrary read-only SQL is deliberately kept out of the TUI",
    ),
    ParityEntry(
        ("migrate",),
        STATUS_CLI_ONLY,
        note="administrative schema migration; diagnostic/maintenance only",
    ),
    ParityEntry(
        ("schema",),
        STATUS_CLI_ONLY,
        note="administrative schema dump; diagnostic/maintenance only",
    ),
    ParityEntry(
        ("ncbi-reconcile",),
        STATUS_CLI_ONLY,
        note="development-era adapter anomaly repair tool; not a workflow",
    ),
    ParityEntry(
        ("qc-measure",),
        STATUS_CLI_ONLY,
        note="project-independent measurement command for the remote QC "
        "workflow; no project is open in the TUI context",
    ),
    ParityEntry(
        ("alignment-qc",),
        STATUS_CLI_ONLY,
        note="project-independent measurement command; no project is open "
        "in the TUI context",
    ),
    # -- planned gaps (milestone attribution per HPC/cli_tui_gaps.md 九) -----
    ParityEntry(("import", "table"), STATUS_PLANNED, note=_M4),
    ParityEntry(("add",), STATUS_PLANNED, note=_M4),
    ParityEntry(("add-accession",), STATUS_PLANNED, note=_M4),
    ParityEntry(
        ("next-id",),
        STATUS_PLANNED,
        note=f"{_M4}; id reservation already happens inside the import wizard",
    ),
    ParityEntry(("ncbi-datasets",), STATUS_PLANNED, note=_M4),
    ParityEntry(("standardize",), STATUS_PLANNED, note=_M4),
    ParityEntry(("import-qc",), STATUS_PLANNED, note=_M4),
    ParityEntry(("run-pipeline",), STATUS_PLANNED, note=_M4),
    ParityEntry(("taxonomy", "import"), STATUS_PLANNED, note=_M4),
    ParityEntry(("taxonomy", "compile"), STATUS_PLANNED, note=_M4),
    ParityEntry(
        ("run-external",),
        STATUS_IMPLEMENTED,
        actions="actions.run_external",
        modal="operon.tui.screens.run_external::RunExternalModal",
        params={
            "step": "external-step",
            "command_line": "external-command",
            "entity_type": "external-entity-type",
            "entity_id": "external-entity-id",
            "parameter_set": "external-parameter-set",
            "tool": "external-tool",
            "inputs": "external-inputs (comma-separated -> repeated --input)",
            "expected_output": "external-expected-outputs "
            "(comma-separated -> repeated --expected-output)",
            "threads": "external-threads",
            "cwd": "external-cwd",
            "timeout": "external-timeout",
            "backend": "external-backend",
        },
    ),
    ParityEntry(
        ("environments", "list"),
        STATUS_IMPLEMENTED,
        actions="data.list_environments",
        modal="operon.tui.screens.environments::EnvironmentsModal",
    ),
    ParityEntry(
        ("environments", "show"),
        STATUS_IMPLEMENTED,
        note="the environment id comes from the selected row",
        actions="data.environment_document",
        modal="operon.tui.screens.environments::EnvironmentsModal",
    ),
    ParityEntry(
        ("environments", "export"),
        STATUS_IMPLEMENTED,
        note="the environment id comes from the selected row; the conda spec is "
        "rendered read-only, so writing it to a file stays a CLI redirection",
        actions="data.export_environment",
        modal="operon.tui.screens.environments::EnvironmentsModal",
        params={
            "format": "environments-explicit / environments-yaml (two buttons)",
        },
    ),
    ParityEntry(("extract-domains",), STATUS_PLANNED, note=_M3),
    ParityEntry(("select-sequences",), STATUS_PLANNED, note=_M3),
    ParityEntry(("classify-sequences",), STATUS_PLANNED, note=_M3),
    ParityEntry(("adopt",), STATUS_PLANNED, note=_M3),
    ParityEntry(("fanout",), STATUS_PLANNED, note=_M3),
    ParityEntry(
        ("report", "analysis"),
        STATUS_IMPLEMENTED,
        note="partial: alignment hits only — the job-summary view stays CLI-only",
        modal="operon.tui.screens.hits::AnalysisHitsModal",
        actions="actions.write_analysis_report",
        params={
            "analysis": "hits-analysis",
            "entity_type": "hits-entity-type",
            "entity_id": "hits-entity-id",
            "query_id": "hits-query-id",
            "subject_id": "hits-subject-id",
            "evalue_max": "hits-evalue-max",
            "limit": "hits-limit",
            "include_retired": "hits-include-retired",
            "format": "hits-format",
            "out": "hits-out (Export button; same renderer as the CLI)",
        },
        waived={
            "hits": "the dialog always browses the --hits view; job summaries stay CLI-only",
        },
    ),
    ParityEntry(("remotes",), STATUS_PLANNED, note=_M5),
    ParityEntry(("push",), STATUS_PLANNED, note=_M5),
    ParityEntry(("evict",), STATUS_PLANNED, note=_M5),
    ParityEntry(("pull",), STATUS_PLANNED, note=_M5),
    ParityEntry(("backup", "create"), STATUS_PLANNED, note=_M5),
    ParityEntry(("backup", "verify"), STATUS_PLANNED, note=_M5),
    ParityEntry(("set-state",), STATUS_PLANNED, note=_M5),
    ParityEntry(("report", "metadata"), STATUS_PLANNED, note=_M5),
    ParityEntry(("timetree", "fetch"), STATUS_PLANNED, note=_M5),
    ParityEntry(("timetree", "calibrate"), STATUS_PLANNED, note=_M5),
    ParityEntry(("timetree", "taxon"), STATUS_PLANNED, note=_M5),
    ParityEntry(("timetree", "pairwise"), STATUS_PLANNED, note=_M5),
    ParityEntry(("timetree", "mrca"), STATUS_PLANNED, note=_M5),
    ParityEntry(("timetree", "timeline"), STATUS_PLANNED, note=_M5),
    ParityEntry(("timetree", "calibrations"), STATUS_PLANNED, note=_M5),
)
