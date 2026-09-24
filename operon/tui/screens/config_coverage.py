"""Coverage-profile editor widgets.

``kind: taxonomy_coverage`` profiles (grammar: ``operon/taxonomy.py``
``_validate_coverage_profile``, documented in ``docs/en/guides/taxonomy-coverage.md``)
declare one or more root TaxIDs, family/genus target ranks, optional extinct /
excluded-subtree / name-regex filters, and per-rank minimum-coverage thresholds.

The form is fully static: every field is a control composed once with the
screen, so unlike the qc and classification editors it mounts no rows at
runtime and needs no ``MountTracked`` containers.  Structures the form cannot
represent (a threshold for a rank that is not a target, ranks outside
family/genus, non-mapping sections, …) open read-only — the panel says so and
offers no save — and keys the form does not model are preserved verbatim at
the section level (same round-trip rule as the qc and classification editors).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from textual.widgets import Static

from operon.config import Project
from operon.tui import actions
from operon.tui.screens.common import WriteModal

# Keep in sync with operon.taxonomy.TARGET_RANK_ORDER keys (asserted in
# tests/unit/test_tui_config.py).
COVERAGE_RANKS = ("family", "genus")
TAXONOMY_SOURCES = ("NCBI",)

# Section-level keys the form models; other keys inside a section are
# preserved verbatim on compose.
SECTION_MODELED_KEYS = {
    "taxonomy": frozenset({"source"}),
    "scope": frozenset({"root_taxids"}),
    "targets": frozenset({"ranks"}),
    "filters": frozenset({
        "exclude_subtrees", "exclude_extinct", "exclude_name_patterns",
    }),
}
THRESHOLD_MODELED_KEYS = frozenset({"min_coverage_percent"})
# Document-level keys the form models; every other key (``name``, …) is
# preserved verbatim and shown as a dim note.
COVERAGE_MODELED_KEYS = frozenset({
    "kind", "version", "description", "taxonomy", "scope", "targets",
    "filters", "thresholds",
})


def coverage_form_supported(document: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(supported, reason)``: can the manual form represent this profile?

    The flat coverage grammar (mapping sections, family/genus ranks, string
    name patterns, threshold maps keyed by a configured rank) is editable;
    anything else opens read-only so the form never rewrites structure it
    cannot represent.  Value-level problems (empty roots, an out-of-range
    threshold, an invalid regular expression) are left to the core validator,
    which reports them when the save is confirmed.
    """
    taxonomy = document.get("taxonomy")
    if taxonomy is not None and not isinstance(taxonomy, dict):
        return False, "taxonomy is not a mapping"
    scope = document.get("scope")
    if not isinstance(scope, dict):
        return False, "scope is not a mapping"
    roots = scope.get("root_taxids")
    if not isinstance(roots, list):
        return False, "scope.root_taxids is not a list"
    targets = document.get("targets")
    if not isinstance(targets, dict):
        return False, "targets is not a mapping"
    ranks = targets.get("ranks")
    if not isinstance(ranks, list) or not all(
            isinstance(rank, str) and rank.lower() in COVERAGE_RANKS
            for rank in ranks):
        return False, "targets.ranks must be a list of family/genus names"
    filters = document.get("filters")
    if filters is not None:
        if not isinstance(filters, dict):
            return False, "filters is not a mapping"
        subtrees = filters.get("exclude_subtrees")
        if subtrees is not None and not isinstance(subtrees, list):
            return False, "filters.exclude_subtrees is not a list"
        patterns = filters.get("exclude_name_patterns")
        if patterns is not None and (
                not isinstance(patterns, list)
                or not all(isinstance(pattern, str) for pattern in patterns)):
            return False, "filters.exclude_name_patterns is not a list of strings"
    thresholds = document.get("thresholds")
    if not isinstance(thresholds, dict):
        return False, "thresholds is not a mapping"
    declared = {rank.lower() for rank in ranks}
    for rank, entry in thresholds.items():
        if str(rank).lower() not in declared:
            return False, f"thresholds.{rank} is not a configured target rank"
        if not isinstance(entry, dict):
            return False, f"thresholds.{rank} is not a mapping"
    return True, ""


class CoverageSaveModal(WriteModal):
    """Confirm a coverage-profile save: file path + version + snapshot."""

    def __init__(self, project: Project, name: str, document: dict[str, Any],
                 new_version: int) -> None:
        super().__init__(f"Save coverage profile {name}")
        self.project = project
        self.profile_name = name
        self.document = document
        self.new_version = new_version

    def compose_form(self) -> Iterable[Any]:
        yield Static(
            f"writes config/profiles/{self.profile_name}.yaml as kind "
            f"taxonomy_coverage version {self.new_version} + records a "
            "content-addressed snapshot.  A later `operon taxonomy compile` "
            "consumes the exact snapshot you save here.",
            classes="modal-info",
        )

    def command_text(self) -> str:
        return (f"config/profiles/{self.profile_name}.yaml → kind taxonomy_coverage, "
                f"version {self.new_version} + qc_profiles snapshot")

    def confirm(self) -> None:
        self.run_action(
            lambda: actions.save_coverage_profile(
                self.project, self.profile_name, self.document,
                known_version=self.new_version - 1,
            )
        )

    def on_action_success(self, payload: Any) -> None:
        if payload.get("unchanged"):
            self.app.notify(f"{self.profile_name}: unchanged — version {payload['version']} kept")
        else:
            self.app.notify(
                f"saved {self.profile_name} version {payload['version']} "
                f"(snapshot #{payload['snapshot_id']})"
            )
        self.dismiss(payload)
