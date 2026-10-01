"""``operon doctor`` and the shared project-health aggregation.

The aggregation lives in :mod:`operon.health` — the CLI-first home of the
checks — and both ``operon doctor`` and the TUI Home dashboard read it.  These
tests pin the contract the two surfaces share: item identity and ordering, the
JSON envelope, the exit-code semantics and the read-only guarantee.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest

from operon import health
from operon.cli import main
from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.utils import sha256_file
from operon.workflow import log_run


def _insert_file(db: Database, file_id: str, status: str) -> None:
    db.insert_row(
        "files",
        {
            "file_id": file_id,
            "entity_type": "assembly",
            "entity_id": "ASM_000001",
            "file_role": "genome_fasta",
            "relative_path": f"raw/{file_id}.fa",
            "sha256": "a" * 64,
            "size_bytes": 1,
            "status": status,
            "format": "fasta",
            "compression": "none",
        },
    )


def _insert_decision(
    db: Database,
    entity_id: str,
    decision: str,
    *,
    profile: str = "demo_profile",
    curated: str | None = None,
) -> None:
    db.insert_row(
        "decisions",
        {
            "entity_type": "assembly",
            "entity_id": entity_id,
            "profile": profile,
            "decision": decision,
            "curated_decision": curated,
            "reason_codes": "[]",
            "observed": "{}",
            "thresholds": "{}",
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        },
    )


def _seeded_project(path: Path) -> Project:
    """One failed and one interrupted run, a corrupt file, REVIEW + FAIL decisions.

    Also seeds the rows that must *not* become attention items: a completed
    run, a healthy file, and a decision with a curated PASS override.
    """
    project = Project.init(path)
    db = Database(project.db_path)
    try:
        log_run(db, project, {"step": "qc", "status": "failed", "error": "injected"})
        log_run(db, project, {"step": "qc", "status": "interrupted"})
        log_run(db, project, {"step": "qc", "status": "completed"})
        _insert_file(db, "FIL_000001", "MISSING")
        _insert_file(db, "FIL_000002", "CHECKSUM_VERIFIED")
        _insert_decision(db, "ASM_000001", "REVIEW")
        _insert_decision(db, "ASM_000002", "FAIL")
        _insert_decision(db, "ASM_000003", "PASS", curated="PASS")
    finally:
        db.close()
    return project


# -- the aggregation ----------------------------------------------------------


def test_attention_report_counts_and_ordering(tmp_path: Path) -> None:
    project = _seeded_project(tmp_path / "seeded")
    report = health.collect_attention_items(project)
    assert report.totals == {
        health.KIND_FAILED_RUN: 2,
        health.KIND_DECISION_REVIEW: 1,
        health.KIND_DECISION_FAIL: 1,
        health.KIND_FILE_UNHEALTHY: 1,
    }
    assert report.total == 5
    kinds = [item.kind for item in report.items]
    assert kinds == [
        health.KIND_FAILED_RUN,
        health.KIND_FAILED_RUN,
        health.KIND_DECISION_REVIEW,
        health.KIND_DECISION_FAIL,
        health.KIND_FILE_UNHEALTHY,
    ], "items come in report order: runs (newest first), decisions, files"
    assert all(
        item.severity == health.SEVERITY_BY_KIND[item.kind] for item in report.items
    )
    assert {item.kind for item in report.items} <= set(health.ATTENTION_KINDS)


def test_attention_report_curated_override_wins(tmp_path: Path) -> None:
    """A curated PASS silences a REVIEW; a curated REVIEW raises a PASS."""
    project = Project.init(tmp_path / "curated")
    db = Database(project.db_path)
    try:
        _insert_decision(db, "ASM_000001", "REVIEW", curated="PASS")
        _insert_decision(db, "ASM_000002", "PASS", curated="REVIEW")
    finally:
        db.close()
    report = health.collect_attention_items(project)
    decisions = {
        item.object: item.details["decision"]
        for item in report.items
        if item.kind.startswith("decision_")
    }
    assert decisions == {"assembly:ASM_000002": "REVIEW"}
    assert report.totals[health.KIND_DECISION_REVIEW] == 1
    assert report.totals[health.KIND_DECISION_FAIL] == 0


def test_attention_report_page_keeps_full_totals(tmp_path: Path) -> None:
    project = _seeded_project(tmp_path / "paged")
    page = health.collect_attention_items(project, limit=1)
    run_items = [item for item in page.items if item.kind == health.KIND_FAILED_RUN]
    assert len(run_items) == 1, "the page honours the per-list limit"
    assert page.totals[health.KIND_FAILED_RUN] == 2, "the totals are not the page"


def test_decision_item_ids_stay_unique_per_profile(tmp_path: Path) -> None:
    """One entity can carry attention decisions under several profiles."""
    project = Project.init(tmp_path / "profiles")
    db = Database(project.db_path)
    try:
        _insert_decision(db, "ASM_000001", "REVIEW", profile="alpha")
        _insert_decision(db, "ASM_000001", "FAIL", profile="beta")
    finally:
        db.close()
    report = health.collect_attention_items(project)
    items = [item for item in report.items if item.kind.startswith("decision_")]
    assert len(items) == 2, "one item per (entity, profile), not one per entity"
    assert len({item.id for item in items}) == 2
    assert {item.id.rsplit(":", 1)[-1] for item in items} == {"alpha", "beta"}


def test_attention_item_ids_are_stable_and_serializable(tmp_path: Path) -> None:
    project = _seeded_project(tmp_path / "stable")
    first = health.collect_attention_items(project)
    second = health.collect_attention_items(project)
    assert [item.id for item in first.items] == [item.id for item in second.items]
    assert len({item.id for item in first.items}) == len(first.items)
    payload = health.attention_payload(first)
    assert json.loads(json.dumps(payload)) == payload
    assert payload["schema_version"] == health.SCHEMA_VERSION
    assert set(payload["summary"]) == set(health.ATTENTION_KINDS)
    for item in payload["items"]:
        assert set(item) == {
            "id",
            "kind",
            "severity",
            "object",
            "suggested_command",
            "details",
        }


def test_suggested_commands_parse_with_the_cli_parser(tmp_path: Path) -> None:
    """Every recommended command is a real one (placeholders substituted)."""
    from operon.cli import _parser

    project = _seeded_project(tmp_path / "commands")
    report = health.collect_attention_items(project)
    assert report.items
    for item in report.items:
        command = item.suggested_command
        command = command.replace("'<reviewer>'", "tester").replace(
            "'<reason>'", "tester_reason"
        )
        argv = shlex.split(command)
        assert argv[0] == "operon", f"not an operon command: {command}"
        parsed = _parser().parse_args(argv[1:])
        assert parsed.command, f"unparsed command: {command}"


# -- the CLI command ----------------------------------------------------------


def test_doctor_reports_nothing_on_a_clean_project(tmp_path: Path, capsys) -> None:
    project = Project.init(tmp_path / "clean")
    assert main(["--project", str(project.root), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "nothing needs attention" in out

    assert main(["--project", str(project.root), "doctor", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"] == []
    assert payload["summary"] == {kind: 0 for kind in health.ATTENTION_KINDS}


def test_doctor_reports_items_and_exits_nonzero(tmp_path: Path, capsys) -> None:
    project = _seeded_project(tmp_path / "busy")
    assert main(["--project", str(project.root), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "5 attention items" in out
    assert "operon workflow show " in out
    assert "operon curate --entity-type assembly --entity-id ASM_000001" in out
    assert "operon verify --file-id FIL_000001" in out
    assert "MISSING" in out

    # JSON stays complete on a non-zero exit: stdout is only the document.
    assert main(["--project", str(project.root), "doctor", "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["summary"] == {
        health.KIND_FAILED_RUN: 2,
        health.KIND_DECISION_REVIEW: 1,
        health.KIND_DECISION_FAIL: 1,
        health.KIND_FILE_UNHEALTHY: 1,
    }
    assert len(payload["items"]) == 5


def test_doctor_limit_pages_items_but_keeps_summary(tmp_path: Path, capsys) -> None:
    project = _seeded_project(tmp_path / "limited")
    assert (
        main(
            [
                "--project",
                str(project.root),
                "doctor",
                "--format",
                "json",
                "--limit",
                "1",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    runs = [item for item in payload["items"] if item["kind"] == health.KIND_FAILED_RUN]
    assert len(runs) == 1
    assert payload["summary"][health.KIND_FAILED_RUN] == 2

    assert main(["--project", str(project.root), "doctor", "--limit", "1"]) == 1
    assert "and 2 more not shown" in capsys.readouterr().out


def test_doctor_on_the_demo_project(tmp_path: Path, capsys) -> None:
    """The demo has REVIEW/FAIL decisions but a clean run history."""
    project = init_demo(tmp_path / "demo")
    assert main(["--project", str(project.root), "doctor", "--format", "json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"][health.KIND_FAILED_RUN] == 0
    assert (
        payload["summary"][health.KIND_DECISION_REVIEW]
        + payload["summary"][health.KIND_DECISION_FAIL]
    ) > 0, "demo project should have REVIEW/FAIL decisions"

    assert main(["--project", str(project.root), "doctor"]) == 1
    out = capsys.readouterr().out
    assert "decision_review" in out or "decision_fail" in out


def test_doctor_failed_check_is_not_a_clean_bill_of_health(
    tmp_path: Path, capsys
) -> None:
    # No project at all: the check itself cannot complete -> exit 2.
    assert main(["--project", str(tmp_path), "doctor"]) == 2
    assert "error:" in capsys.readouterr().err

    # A project whose database is gone is a failed check too, never healthy.
    project = Project.init(tmp_path / "broken")
    project.db_path.unlink()
    assert main(["--project", str(project.root), "doctor"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not inspect the project" in captured.err
    # JSON mode fails the same way, emits no document, and creates nothing.
    assert main(["--project", str(project.root), "doctor", "--format", "json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert not project.db_path.exists()

    # A corrupt database is a failed check too: never exit 0, never a document.
    corrupt = Project.init(tmp_path / "corrupt")
    corrupt.db_path.write_bytes(b"this is not a SQLite database")
    assert main(["--project", str(corrupt.root), "doctor", "--format", "json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not inspect the project" in captured.err


def test_doctor_is_read_only(tmp_path: Path) -> None:
    """Doctor runs against a read-only database and changes no bytes."""
    project = _seeded_project(tmp_path / "readonly")
    before = sha256_file(project.db_path)
    os.chmod(project.db_path, 0o444)
    try:
        assert main(["--project", str(project.root), "doctor"]) == 1
        assert main(["--project", str(project.root), "doctor", "--format", "json"]) == 1
    finally:
        os.chmod(project.db_path, 0o644)
    assert sha256_file(project.db_path) == before
    # And it opened no writable session that would leave a stray migration.
    db = Database(project.db_path, read_only=True)
    try:
        assert db.query("SELECT COUNT(*) AS n FROM changes")[0]["n"] == 0
    finally:
        db.close()


def test_cli_and_tui_reads_are_the_core_aggregation(tmp_path: Path, capsys) -> None:
    """Both surfaces report exactly what the core function returns.

    This is the non-drift guarantee: ``operon doctor`` and the TUI's data
    layer read the same function, so their reports must be identical, not
    merely similar.
    """
    project = _seeded_project(tmp_path / "shared")
    core = health.collect_attention_items(project)

    assert main(["--project", str(project.root), "doctor", "--format", "json"]) == 1
    assert json.loads(capsys.readouterr().out) == health.attention_payload(core)

    from operon.tui import data as tui_data

    assert tui_data.attention_report(project) == core


def test_tui_adapter_reads_the_core_function(monkeypatch) -> None:
    """``data.attention_report`` delegates to the core instead of re-deriving."""
    from operon.tui import data as tui_data

    sentinel = health.AttentionReport(items=(), totals={health.KIND_FAILED_RUN: 7})
    calls: list[int] = []

    def fake(_project, *, limit: int = 0) -> health.AttentionReport:
        calls.append(limit)
        return sentinel

    monkeypatch.setattr(health, "collect_attention_items", fake)
    assert tui_data.attention_report(object()) is sentinel
    assert calls == [0]
    assert tui_data.attention_report(object(), limit=3) is sentinel
    assert calls == [0, 3]


@pytest.mark.parametrize(
    ("kind", "severity"),
    [
        (health.KIND_FAILED_RUN, "error"),
        (health.KIND_DECISION_REVIEW, "warning"),
        (health.KIND_DECISION_FAIL, "error"),
        (health.KIND_FILE_UNHEALTHY, "warning"),
    ],
)
def test_severity_map_covers_every_kind(kind: str, severity: str) -> None:
    assert health.SEVERITY_BY_KIND[kind] == severity
