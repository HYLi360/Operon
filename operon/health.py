"""Project health: the attention items a curator should look at first.

One read-only aggregation, shared by ``operon doctor`` (the CLI) and the TUI's
Home dashboard, so the two surfaces cannot drift apart.  The CLI-first rule
(GATE-1) is why this module exists: the aggregation used to live in
``operon.tui.data``, where no script or CI job could reach it.

Nothing here judges data.  The items are observations of recorded state
(failed/interrupted workflow runs, REVIEW/FAIL current decisions, files whose
status is not healthy); the decision thresholds stay in the versioned profiles
(INV-4) and no new rule is added here.  Every function opens its own
short-lived read-only database connection and writes nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from operon.config import Project
from operon.database import Database

SCHEMA_VERSION = 1
"""Version of the ``operon doctor --format json`` envelope (``attention_payload``)."""

KIND_FAILED_RUN = "failed_run"
KIND_DECISION_REVIEW = "decision_review"
KIND_DECISION_FAIL = "decision_fail"
KIND_FILE_UNHEALTHY = "file_unhealthy"

ATTENTION_KINDS: tuple[str, ...] = (
    KIND_FAILED_RUN,
    KIND_DECISION_REVIEW,
    KIND_DECISION_FAIL,
    KIND_FILE_UNHEALTHY,
)
"""Every kind an attention item can carry, in report order."""

SEVERITY_BY_KIND: dict[str, str] = {
    KIND_FAILED_RUN: "error",
    KIND_DECISION_REVIEW: "warning",
    KIND_DECISION_FAIL: "error",
    KIND_FILE_UNHEALTHY: "warning",
}
"""The severity each kind reports (a display priority derived from the kind)."""

ATTENTION_RUN_STATUSES = frozenset({"failed", "interrupted"})
"""Workflow-run statuses that need a look."""

ATTENTION_DECISIONS = frozenset({"REVIEW", "FAIL"})
"""Effective decisions that need a look."""

HEALTHY_FILE_STATUSES = frozenset({"CHECKSUM_VERIFIED", "STANDARDIZED"})
"""Settled file statuses; every other status needs a look."""


@dataclass(frozen=True, slots=True)
class AttentionItem:
    """One recorded condition a curator should look at, with its addressing command.

    ``id`` is stable across runs (``"<kind>:<object>"``; a decision item also
    appends its profile, because one entity can carry decisions under several
    profiles), so a script can dedupe or track the same condition between
    invocations.  ``object`` names what the condition is about as
    ``"<type>:<id>"`` (``"run:WF_…"``, ``"assembly:ASM_…"``, ``"file:FIL_…"``).
    ``suggested_command`` is an ``operon …`` command line that addresses the
    item; a flag only the operator can supply stays a placeholder
    (``--reviewer '<reviewer>'``).  ``details`` carries the record's extra
    machine-readable facts (step, status, profile, path, …) for renderers.
    """

    id: str
    kind: str
    severity: str
    object: str
    suggested_command: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def object_type(self) -> str:
        """What the object identifier names (``"run"``, an entity type, ``"file"``)."""
        return self.object.split(":", 1)[0]

    @property
    def object_id(self) -> str:
        """The identifier of the object the condition is about."""
        return self.object.split(":", 1)[1]

    def as_dict(self) -> dict[str, Any]:
        """The stable JSON record of this item (the ``doctor --format json`` shape)."""
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "object": self.object,
            "suggested_command": self.suggested_command,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class AttentionReport:
    """Attention items plus the untruncated per-kind totals.

    ``items`` is what a renderer shows (at most ``limit`` per source list when
    the caller paged it); ``totals`` counts every recorded condition per kind,
    so "and N more" stays truthful on a page.
    """

    items: tuple[AttentionItem, ...]
    totals: dict[str, int]

    @property
    def total(self) -> int:
        """Total number of attention items across all kinds (not just the page)."""
        return sum(self.totals.values())


@contextmanager
def _open(project: Project) -> Iterator[Database]:
    db = Database(project.db_path, read_only=True)
    try:
        yield db
    finally:
        db.close()


def _effective_decision(row: Any) -> str:
    return str(row["curated_decision"] or row["decision"] or "")


def collect_attention_items(project: Project, *, limit: int = 0) -> AttentionReport:
    """Aggregate the open attention items of ``project`` (read-only).

    ``limit=0`` returns every recorded condition — the CLI's mode, where
    nothing may be hidden.  ``limit=N`` caps each source list (runs,
    decisions, files) at its first N rows in the same deterministic order and
    keeps the untruncated counts in :attr:`AttentionReport.totals`: that is
    the TUI dashboard's mode, which renders a page and says how many more
    there are.
    """
    page = max(0, int(limit))
    with _open(project) as db:
        run_sql = (
            "SELECT run_id, step, status, entity_type, entity_id, started_at, error "
            "FROM workflow_runs WHERE status IN (?, ?) "
            "ORDER BY julianday(started_at) DESC, run_id DESC"
        )
        run_params: list[Any] = sorted(ATTENTION_RUN_STATUSES)
        if page:
            run_sql += " LIMIT ?"
            run_params.append(page)
        run_rows = db.query(run_sql, run_params)
        run_total = int(
            db.query(
                "SELECT COUNT(*) AS n FROM workflow_runs WHERE status IN (?, ?)",
                sorted(ATTENTION_RUN_STATUSES),
            )[0]["n"]
        )
        decision_rows = [
            row
            for row in db.query(
                "SELECT entity_type, entity_id, profile, decision, curated_decision, "
                "reason_codes, evaluated_at FROM current_decisions "
                "ORDER BY entity_type, entity_id, profile"
            )
            if _effective_decision(row) in ATTENTION_DECISIONS
        ]
        # The per-kind totals are computed over the full filtered set; the
        # page-capped list below feeds only the items.
        decision_totals = {
            KIND_DECISION_REVIEW: sum(
                1 for row in decision_rows if _effective_decision(row) == "REVIEW"
            ),
            KIND_DECISION_FAIL: sum(
                1 for row in decision_rows if _effective_decision(row) == "FAIL"
            ),
        }
        if page:
            decision_rows = decision_rows[:page]
        file_sql = (
            "SELECT file_id, entity_type, entity_id, file_role, relative_path, status "
            "FROM files WHERE status NOT IN (?, ?) ORDER BY file_id"
        )
        file_params: list[Any] = sorted(HEALTHY_FILE_STATUSES)
        if page:
            file_sql += " LIMIT ?"
            file_params.append(page)
        file_rows = db.query(file_sql, file_params)
        file_total = int(
            db.query(
                "SELECT COUNT(*) AS n FROM files WHERE status NOT IN (?, ?)",
                sorted(HEALTHY_FILE_STATUSES),
            )[0]["n"]
        )
    items: list[AttentionItem] = []
    for row in run_rows:
        items.append(
            AttentionItem(
                id=f"{KIND_FAILED_RUN}:run:{row['run_id']}",
                kind=KIND_FAILED_RUN,
                severity=SEVERITY_BY_KIND[KIND_FAILED_RUN],
                object=f"run:{row['run_id']}",
                suggested_command=f"operon workflow show {row['run_id']}",
                details={
                    "step": row["step"],
                    "status": row["status"],
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "started_at": row["started_at"],
                    "error": row["error"],
                },
            )
        )
    for row in decision_rows:
        effective = _effective_decision(row)
        kind = KIND_DECISION_REVIEW if effective == "REVIEW" else KIND_DECISION_FAIL
        entity_type = str(row["entity_type"])
        entity_id = str(row["entity_id"])
        profile = str(row["profile"])
        items.append(
            AttentionItem(
                id=f"{kind}:{entity_type}:{entity_id}:{profile}",
                kind=kind,
                severity=SEVERITY_BY_KIND[kind],
                object=f"{entity_type}:{entity_id}",
                suggested_command=(
                    f"operon curate --entity-type {entity_type} "
                    f"--entity-id {entity_id} --profile {profile} "
                    f"--decision {effective} --reviewer '<reviewer>' "
                    "--reason '<reason>'"
                ),
                details={
                    "profile": profile,
                    "decision": effective,
                    "reason_codes": row["reason_codes"],
                    "evaluated_at": row["evaluated_at"],
                },
            )
        )
    for row in file_rows:
        items.append(
            AttentionItem(
                id=f"{KIND_FILE_UNHEALTHY}:file:{row['file_id']}",
                kind=KIND_FILE_UNHEALTHY,
                severity=SEVERITY_BY_KIND[KIND_FILE_UNHEALTHY],
                object=f"file:{row['file_id']}",
                suggested_command=f"operon verify --file-id {row['file_id']}",
                details={
                    "status": row["status"],
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "file_role": row["file_role"],
                    "relative_path": row["relative_path"],
                },
            )
        )
    totals = {
        KIND_FAILED_RUN: run_total,
        KIND_DECISION_REVIEW: decision_totals[KIND_DECISION_REVIEW],
        KIND_DECISION_FAIL: decision_totals[KIND_DECISION_FAIL],
        KIND_FILE_UNHEALTHY: file_total,
    }
    return AttentionReport(items=tuple(items), totals=totals)


def item_context(item: AttentionItem) -> str:
    """One factual line of context for the item's object (medium-neutral text)."""
    details = item.details
    if item.kind == KIND_FAILED_RUN:
        return (
            f"run {item.object_id} (step {details.get('step')}, "
            f"status {details.get('status')})"
        )
    if item.kind in (KIND_DECISION_REVIEW, KIND_DECISION_FAIL):
        return f"{item.object} (profile {details.get('profile')})"
    if item.kind == KIND_FILE_UNHEALTHY:
        return (
            f"file {item.object_id} (status {details.get('status')}, "
            f"{details.get('relative_path')})"
        )
    return item.object


def render_attention_text(report: AttentionReport) -> str:
    """The plain-text ``operon doctor`` report (stable lines, no colors)."""
    total = report.total
    if not total:
        return "nothing needs attention"
    counts = ", ".join(
        f"{report.totals[kind]} {kind}"
        for kind in ATTENTION_KINDS
        if report.totals[kind]
    )
    label = "item" if total == 1 else "items"
    lines = [f"{total} attention {label}: {counts}", ""]
    lines.extend(
        f"  [{item.severity}] {item.kind}  {item_context(item)}  ->  "
        f"{item.suggested_command}"
        for item in report.items
    )
    hidden = total - len(report.items)
    if hidden:
        lines.append(f"... and {hidden} more not shown (re-run without a limit)")
    return "\n".join(lines)


def attention_payload(report: AttentionReport) -> dict[str, Any]:
    """The JSON document behind ``operon doctor --format json``.

    Stable contract: ``schema_version``, ``summary`` (the project-wide total
    per kind — every :data:`ATTENTION_KINDS` entry, zeros included — even when
    a ``limit`` paged the items) and ``items`` (:meth:`AttentionItem.as_dict`
    records in report order).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "summary": {kind: report.totals.get(kind, 0) for kind in ATTENTION_KINDS},
        "items": [item.as_dict() for item in report.items],
    }
