"""Derived-artifact modals: extract-domains, select-sequences, adopt, fanout.

The fixture builds the smallest realistic project the four commands need: a
registered protein FASTA with a ``sequences`` registry, one completed analysis
job with alignments (``hit_type``/``short_name`` in ``extra_json``), a
registered unit/seqid assignments table, and a classification profile on disk.
Where a TUI action mirrors a CLI command, the tests assert the *outputs are
byte-identical*, not merely similar.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

import yaml
from rich.text import Text
from textual.widgets import Button, DataTable, Input, Select, Static

from operon.cli import main
from operon.config import Project
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.files import ingest_file
from operon.tui import actions
from operon.tui.app import OperonApp
from operon.tui.screens.derived_ops import (
    AdoptModal,
    ExtractModal,
    FanoutModal,
    SelectSequencesModal,
)
from operon.tui.screens.files import FilesPanel

SCENARIO_TIMEOUT = 60.0
SETTLE_TIMEOUT = 30.0
PROTEINS = {
    "p1": "M" + "A" * 49,
    "p2": "C" * 40,
    "p3": "G" * 20,
    "p4": "D" * 60,
    "p5": "H" * 30,
}
CLASSIFY_PROFILE = {
    "kind": "sequence_classification",
    "version": 1,
    "description": "test tiers",
    "applies_to": {"entity_type": "annotation", "file_role": "protein_fasta"},
    "sources": {
        "cdd": {
            "analysis": "cdd",
            "filter": [{"field": "hit_type", "operator": "in", "values": ["Specific"]}],
        },
    },
    "rules": [
        {"label": "BHLH", "source": "cdd",
         "when": [{"field": "short_name", "operator": "like", "value": "bHLH%"}]},
        {"label": "OTHER", "source": "cdd", "absent": True},
        {"label": "UNKNOWN", "default": True},
    ],
}


def _run(coroutine) -> None:
    asyncio.run(asyncio.wait_for(coroutine, timeout=SCENARIO_TIMEOUT))


async def _settled(app, timeout: float = SETTLE_TIMEOUT) -> None:
    """Wait until no workers are running, with a diagnostic timeout.

    ``app.workers.wait_for_complete()`` is unbounded: a worker blocked in
    ``call_from_thread`` (or spawned again by a timer) blocks the caller
    forever.  Poll the worker set instead and fail fast with the stuck
    worker states when the deadline passes.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    # The splash screen blocks key bindings until the startup worker finishes;
    # that worker is scheduled via call_after_refresh, so the worker set can
    # be momentarily empty before it starts — gate on _starting as well.  This
    # also keeps a modal pushed during the splash from being popped by the
    # splash's own pop_screen (it pops the top of the stack).
    while app.workers or getattr(app, "_starting", False):
        if loop.time() > deadline:
            states = [worker.state.name for worker in app.workers]
            raise TimeoutError(f"workers did not finish within {timeout}s: {states}")
        await asyncio.sleep(0.05)


async def _wait_until(predicate: Callable[[], bool], description: str,
                      timeout: float = SETTLE_TIMEOUT) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError(f"UI did not {description} within {timeout}s")
        await asyncio.sleep(0.05)


async def _click(pilot, selector: str) -> None:
    try:
        widget = pilot.app.screen.query_one(selector)
    except Exception:  # pragma: no cover - the click itself reports a missing target
        widget = None
    if isinstance(widget, Button) and widget.has_class("-active"):
        await _wait_until(
            lambda: not widget.has_class("-active"), f"{selector} to settle",
        )
    assert await pilot.click(selector), f"click did not land on {selector}"


async def _push(pilot, modal, selector: str | None = None) -> None:
    """Push a screen and wait until its form **and** buttons have composed.

    ``push_screen`` mounts a modal in stages, so a bare ``pilot.pause()`` can
    still observe an uncomposed widget (``query_one`` raises NoMatches); this
    waits for the Confirm button plus a form widget (or an explicit selector).
    If the modal is not on the stack afterwards it is pushed once more —
    ``_settled`` already keeps callers clear of the startup splash, whose own
    ``pop_screen`` would otherwise discard a modal pushed on top of it.
    """
    for attempt in range(2):
        if pilot.app.screen is not modal:
            pilot.app.push_screen(modal)

        def ready() -> bool:
            if len(modal.query("#confirm")) == 0:
                return False
            if selector is None:
                # Any composed form child means compose_form ran (ClassifyModal's
                # form holds Statics only, so looking for inputs is not enough).
                return bool(modal.query("#modal-form > *"))
            return len(modal.query(selector)) > 0

        try:
            await _wait_until(ready, f"{type(modal).__name__} to compose", timeout=10.0)
        except TimeoutError:
            if attempt:
                raise
            await pilot.pause()
            await asyncio.sleep(0.5)
            continue
        await pilot.pause()
        return



async def _q(modal, selector: str, *types):
    """Query a widget inside a modal, waiting until compose has created it.

    ``push_screen`` mounts a modal in stages; querying a nested widget right
    after ``pilot.pause()`` can raise ``NoMatches`` on a loaded machine.  This
    waits for the selector to resolve, then returns ``query_one``.
    """
    await _wait_until(lambda: len(modal.query(selector)) > 0, f"{selector} to exist")
    return modal.query_one(selector, *types)


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


@pytest.fixture
def derived_project(tmp_path: Path) -> Project:
    """Protein FASTA + sequences registry + alignments + assignments + profile."""
    project = Project.init(tmp_path / "derived")
    db = Database(project.db_path)
    try:
        db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Testus", "taxonomy_source": "NCBI",
        })
        db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        db.insert_row("assemblies", {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_level": "contig", "assembly_version": 1,
        })
        db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
            "annotation_source": "test", "annotation_version": 1,
        })
        fasta = tmp_path / "proteins.faa"
        lines: list[str] = []
        for seqid, sequence in PROTEINS.items():
            lines.append(f">{seqid} description of {seqid}" if seqid == "p2" else f">{seqid}")
            lines.append(sequence)
        fasta.write_text("\n".join(lines) + "\n", encoding="utf-8")
        row = ingest_file(db, project, fasta, "annotation", "ANN_000001", "protein_fasta")
        for seqid, sequence in PROTEINS.items():
            db.insert_row("sequences", {
                "file_id": row["file_id"], "file_sha256": row["sha256"],
                "entity_type": "annotation", "entity_id": "ANN_000001",
                "seqid": seqid, "length": len(sequence),
            })
        job_id = _add_job(db, row)
        for seqid, subject, rank, start, end, evalue, extra in (
            ("p1", "bhlh_family", 1, 10, 40, 1e-10, {"hit_type": "Specific", "short_name": "bHLH"}),
            ("p1", "other_dom", 2, 5, 20, 1e-3, {"hit_type": "NonSpecific", "short_name": "other"}),
            ("p2 description of p2", "bhlh_family", 1, 2, 39, 1e-8,
             {"hit_type": "Specific", "short_name": "bHLH"}),
            ("p4", "CD99999", 1, 5, 55, 1e-20, {"hit_type": "Specific", "short_name": "bHLH_MYC"}),
        ):
            db.insert_row("analysis_alignments", {
                "job_id": job_id, "entity_type": "annotation", "entity_id": "ANN_000001",
                "file_id": row["file_id"], "analysis_name": "cdd", "query_id": seqid,
                "subject_id": subject, "hit_rank": rank, "query_start": start,
                "query_end": end, "evalue": evalue,
                "extra_json": json.dumps(extra),
            })
        assignments = tmp_path / "assignments.tsv"
        assignments.write_text("unit\tseqid\nunitA\tp1\nunitA\tp2\nunitB\tp3\n", encoding="utf-8")
        ingest_file(db, project, assignments, "annotation", "ANN_000001", "assignments")
    finally:
        db.close()
    project.profiles_dir.mkdir(parents=True, exist_ok=True)
    (project.profiles_dir / "test_tiers.yaml").write_text(
        yaml.safe_dump(CLASSIFY_PROFILE, sort_keys=False), encoding="utf-8")
    return project


def _add_job(db: Database, row: dict) -> int:
    db.insert_row("analysis_jobs", {
        "analysis_name": "cdd", "entity_type": "annotation", "entity_id": "ANN_000001",
        "file_id": row["file_id"], "tool": "faketool", "tool_version": "1.0",
        "parameter_set": "default", "parameter_sha256": "p" * 64,
        "input_sha256": row["sha256"], "database_identity": "db",
        "status": "completed", "started_at": "2026-01-01T00:00:00+00:00",
    })
    return db.query("SELECT max(job_id) AS j FROM analysis_jobs")[0]["j"]


def _file_id(project: Project, role: str) -> str:
    db = Database(project.db_path)
    try:
        return db.query("SELECT file_id FROM files WHERE file_role=?", (role,))[0]["file_id"]
    finally:
        db.close()


def _query(project: Project, sql: str, params: tuple = ()) -> list[Any]:
    db = Database(project.db_path)
    try:
        return db.query(sql, params)
    finally:
        db.close()


def _notifications(app) -> list[tuple[str, str]]:
    return [(n.severity, str(n.message)) for n in app._notifications]


# -- actions ----------------------------------------------------------------


def test_extract_domains_preflight_and_cli_equivalence(derived_project: Project,
                                                       tmp_path: Path) -> None:
    """The action mirrors the core's mutual exclusion and matches the CLI byte for byte."""
    file_id = _file_id(derived_project, "protein_fasta")
    out = tmp_path / "domains.faa"
    with pytest.raises(ValidationError, match="exactly one of --analysis or --regions-tsv"):
        actions.extract_domains(derived_project, file_id=file_id, out=str(out))
    with pytest.raises(ValidationError, match="exactly one of --analysis or --regions-tsv"):
        actions.extract_domains(derived_project, file_id=file_id, out=str(out),
                                analysis="cdd", regions_tsv="x.tsv")
    with pytest.raises(ValidationError, match="only apply to --analysis regions"):
        actions.extract_domains(derived_project, file_id=file_id, out=str(out),
                                regions_tsv="x.tsv", subject_like="bHLH%")
    with pytest.raises(ValidationError, match="--out is required"):
        actions.extract_domains(derived_project, file_id=file_id, out="  ", analysis="cdd")

    result = actions.extract_domains(derived_project, file_id=file_id, out=str(out),
                                     analysis="cdd", subject_like="bHLH%", flank=3)
    assert result["extracted"] > 0
    assert Path(result["output"]).read_text(encoding="utf-8") == out.read_text(encoding="utf-8")
    cli_out = tmp_path / "cli-domains.faa"
    assert main(["--project", str(derived_project.root), "extract-domains",
                 "--file-id", file_id, "--analysis", "cdd", "--flank", "3",
                 "--subject-like", "bHLH%", "--out", str(cli_out)]) == 0
    assert out.read_text(encoding="utf-8") == cli_out.read_text(encoding="utf-8")
    # The output is deliberately unregistered: the run row exists, no files row does.
    assert _query(derived_project,
                  "SELECT * FROM workflow_runs WHERE step='extract-domains'")
    assert _query(derived_project, "SELECT * FROM files WHERE relative_path LIKE '%domains%'") == []


def test_select_sequences_preflight_and_cli_equivalence(derived_project: Project,
                                                        tmp_path: Path) -> None:
    file_id = _file_id(derived_project, "protein_fasta")
    out = tmp_path / "selected.faa"
    with pytest.raises(ValidationError, match="no hit criteria given"):
        actions.select_sequences(derived_project, file_id=file_id, out=str(out))
    with pytest.raises(ValidationError, match="--out is required"):
        actions.select_sequences(derived_project, file_id=file_id, out="", analyses=["cdd"])

    result = actions.select_sequences(derived_project, file_id=file_id, out=str(out),
                                      analyses=["cdd"], require_hit=True)
    assert 0 < result["selected"] <= result["total"]
    cli_out = tmp_path / "cli-selected.faa"
    assert main(["--project", str(derived_project.root), "select-sequences",
                 "--file-id", file_id, "--analysis", "cdd", "--require-hit",
                 "--out", str(cli_out)]) == 0
    assert out.read_text(encoding="utf-8") == cli_out.read_text(encoding="utf-8")
    assert _query(derived_project,
                  "SELECT * FROM workflow_runs WHERE step='select-sequences'")


def test_adopt_reuse_conflict_and_manifest(derived_project: Project, tmp_path: Path) -> None:
    file_id = _file_id(derived_project, "protein_fasta")
    source = tmp_path / "derived.faa"
    source.write_text(">p1\n" + "A" * 30 + "\n", encoding="utf-8")
    item = {"path": str(source), "entity_type": "annotation", "entity_id": "ANN_000001",
            "role": "extracted_domain", "derived_from": [file_id]}

    first = actions.adopt(derived_project, items=[dict(item)])
    assert first["registered"] == 1 and first["reused"] == 0
    again = actions.adopt(derived_project, items=[dict(item)])
    assert again["registered"] == 1 and again["reused"] == 1       # idempotent reuse
    assert again["file_ids"] == first["file_ids"]

    source.write_text(">p1\n" + "C" * 30 + "\n", encoding="utf-8")
    with pytest.raises(ConflictError):
        actions.adopt(derived_project, items=[dict(item)])

    with pytest.raises(ValidationError, match="derived-from"):
        actions.adopt(derived_project, items=[{
            "path": str(source), "entity_type": "annotation", "entity_id": "ANN_000001",
            "role": "other", "derived_from": [],
        }])
    with pytest.raises(ValidationError, match="not registered"):
        actions.adopt(derived_project, items=[{
            "path": str(source), "entity_type": "annotation", "entity_id": "ANN_000001",
            "role": "other", "derived_from": ["FIL_999999"],
        }])

    manifest = tmp_path / "adopt.json"
    manifest.write_text(json.dumps([{
        "path": str(source), "entity_type": "annotation", "entity_id": "ANN_000001",
        "role": "manifest_role", "derived_from": [file_id],
    }]), encoding="utf-8")
    from_manifest = actions.adopt(derived_project, manifest=str(manifest))
    assert from_manifest["registered"] == 1
    assert _query(derived_project, "SELECT * FROM workflow_runs WHERE step='adopt'") != []


def test_fanout_dry_run_then_real_run_and_reuse(derived_project: Project) -> None:
    assignments = _file_id(derived_project, "assignments")
    source = _file_id(derived_project, "protein_fasta")
    values: dict[str, Any] = {
        "assignments_file_id": assignments, "source_file_ids": [source],
        "entity_type": "annotation", "entity_id": "ANN_000001", "role_prefix": "units",
    }

    preview = actions.fanout(derived_project, dry_run=True, **values)
    assert preview["dry_run"] is True
    assert sorted(unit["status"] for unit in preview["units"]) == ["would_create", "would_create"]
    assert _query(derived_project, "SELECT * FROM workflow_runs WHERE step='fanout'") == []
    assert _query(derived_project, "SELECT * FROM files WHERE file_role LIKE 'units:%'") == []

    with pytest.raises(ValidationError, match="requires --role-prefix"):
        actions.fanout(derived_project, dry_run=True, **{**values, "role_prefix": ""})
    with pytest.raises(ValidationError, match="at least one --source-file"):
        actions.fanout(derived_project, dry_run=True, **{**values, "source_file_ids": []})

    result = actions.fanout(derived_project, dry_run=False, **values)
    assert result["created"] == 2 and result["reused"] == 0
    roles = sorted(row["file_role"] for row in
                   _query(derived_project, "SELECT file_role FROM files WHERE file_role LIKE 'units:%'"))
    assert roles == ["units:unitA", "units:unitB"]
    lineage = _query(derived_project, "SELECT COUNT(*) AS n FROM file_lineage")
    assert lineage[0]["n"] >= 4  # two unit files, each derived from the assignments + the source
    rerun = actions.fanout(derived_project, dry_run=False, **values)
    assert rerun["created"] == 0 and rerun["reused"] == 2


# -- modals -----------------------------------------------------------------


def test_extract_modal_chains_into_adopt(derived_project: Project, tmp_path: Path) -> None:
    out = tmp_path / "ui-domains.faa"

    async def scenario() -> None:
        app = OperonApp(derived_project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(FilesPanel)
            table = app.query_one("#files-table", DataTable)
            table.focus()
            target = next(
                index for index, row in enumerate(panel.files)
                if row["file_role"] == "protein_fasta"
            )
            table.move_cursor(row=target, animate=False)
            await pilot.pause()
            panel.action_extract()
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, ExtractModal)
            (await _q(modal, "#extract-analysis", Input)).value = "cdd"
            (await _q(modal, "#extract-out", Input)).value = str(out)
            await pilot.pause()
            assert "--analysis cdd" in modal.command_text()
            # The selector makes the two region sources mutually exclusive: exactly
            # one input is live at a time (the action re-checks both ways).
            (await _q(modal, "#extract-source-kind", Select)).value = "tsv"
            await pilot.pause()
            assert (await _q(modal, "#extract-analysis", Input)).disabled is True
            assert (await _q(modal, "#extract-regions-tsv", Input)).disabled is False
            (await _q(modal, "#extract-out", Input)).value = ""
            modal.confirm()
            assert "out path is required" in _static_text(
                await _q(modal, "#modal-error", Static))
            (await _q(modal, "#extract-source-kind", Select)).value = "analysis"
            await pilot.pause()
            assert (await _q(modal, "#extract-analysis", Input)).disabled is False
            (await _q(modal, "#extract-analysis", Input)).value = "cdd"
            (await _q(modal, "#extract-out", Input)).value = str(out)
            await pilot.pause()
            await _click(pilot, "#confirm")
            await _wait_until(lambda: not isinstance(app.screen, ExtractModal),
                              "extract modal to close")
            await pilot.pause()
            # The success callback hands the unregistered output to the adopt dialog.
            adopt = app.screen
            assert isinstance(adopt, AdoptModal)
            assert (await _q(adopt, "#adopt-path", Input)).value == str(out)
            assert (await _q(adopt, "#adopt-derived-from", Input)).value != ""
            assert any("extracted" in message for _, message in _notifications(app))
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())
    assert out.exists()


def test_select_modal_runs_and_reports(derived_project: Project, tmp_path: Path) -> None:
    out = tmp_path / "ui-selected.faa"

    async def scenario() -> None:
        app = OperonApp(derived_project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            app.action_switch_screen("files")
            await pilot.pause()
            await _settled(app)
            panel = app.query_one(FilesPanel)
            table = app.query_one("#files-table", DataTable)
            target = next(index for index, row in enumerate(panel.files)
                          if row["file_role"] == "protein_fasta")
            table.focus()
            table.move_cursor(row=target, animate=False)
            await pilot.pause()
            panel.action_select()
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, SelectSequencesModal)
            (await _q(modal, "#select-out", Input)).value = str(out)
            modal.confirm()
            assert "no hit criteria given" in _static_text(
                await _q(modal, "#modal-error", Static))
            (await _q(modal, "#select-analysis-1", Input)).value = "cdd"
            await pilot.pause()
            assert "--analysis cdd" in modal.command_text()
            await _click(pilot, "#confirm")
            await _wait_until(lambda: not isinstance(app.screen, SelectSequencesModal),
                              "select modal to close", timeout=30.0)
            await pilot.pause()
            assert any("selected" in message for _, message in _notifications(app))
            adopt = app.screen
            assert isinstance(adopt, AdoptModal)
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())
    assert out.exists()


def test_adopt_modal_single_item(derived_project: Project, tmp_path: Path) -> None:
    """One app session per mode: the flows are independent of each other."""
    file_id = _file_id(derived_project, "protein_fasta")
    source = tmp_path / "ui-adopt.faa"
    source.write_text(">p1\n" + "G" * 25 + "\n", encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(derived_project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            modal = AdoptModal(derived_project, path=str(source), derived_from=[file_id],
                               entity_type="annotation", entity_id="ANN_000001")
            await _push(pilot, modal, "#adopt-preview-button")
            (await _q(modal, "#adopt-role", Input)).value = "ui_role"
            await pilot.pause()
            assert "--file" in modal.command_text() and "--derived-from" in modal.command_text()
            (await _q(modal, "#confirm", Button)).press()
            await _wait_until(lambda: not isinstance(app.screen, AdoptModal),
                              "adopt modal to close", timeout=30.0)
            await pilot.pause()
            assert any("registered" in message for _severity, message in _notifications(app))

    _run(scenario())
    roles = [row["file_role"] for row in
             _query(derived_project, "SELECT file_role FROM files WHERE file_role='ui_role'")]
    assert roles == ["ui_role"]


def test_adopt_modal_manifest_requires_preview(derived_project: Project,
                                               tmp_path: Path) -> None:
    file_id = _file_id(derived_project, "protein_fasta")
    source = tmp_path / "ui-manifest-source.faa"
    source.write_text(">p1\n" + "G" * 25 + "\n", encoding="utf-8")
    manifest = tmp_path / "ui-manifest.json"
    manifest.write_text(json.dumps([{
        "path": str(source), "entity_type": "annotation", "entity_id": "ANN_000001",
        "role": "manifest_ui_role", "derived_from": [file_id],
    }]), encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(derived_project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            modal = AdoptModal(derived_project)
            await _push(pilot, modal, "#adopt-preview-button")
            (await _q(modal, "#adopt-mode", Select)).value = "manifest"
            await pilot.pause()
            assert (await _q(modal, "#adopt-manifest", Input)).disabled is False
            assert (await _q(modal, "#adopt-path", Input)).disabled is True
            assert (await _q(modal, "#confirm", Button)).disabled is True
            (await _q(modal, "#adopt-manifest", Input)).value = str(manifest)
            await pilot.pause()
            # The modal form scrolls: press directly (Pilot.click needs the
            # target inside the visible area).
            (await _q(modal, "#adopt-preview-button", Button)).press()
            preview_view = await _q(modal, "#adopt-preview", Static)
            await _wait_until(
                lambda: "manifest parsed" in _static_text(preview_view), "manifest preview",
            )
            assert "1 item(s)" in _static_text(await _q(modal, "#adopt-preview", Static))
            assert (await _q(modal, "#confirm", Button)).disabled is False
            assert "--from-manifest" in modal.command_text()
            # The growing form can place the preview row over the button row, so
            # press (the button is always visible) instead of clicking a position.
            (await _q(modal, "#confirm", Button)).press()
            await _wait_until(lambda: not isinstance(app.screen, AdoptModal),
                              "manifest adopt to close", timeout=30.0)
            await pilot.pause()

    _run(scenario())
    assert [row["file_role"] for row in
            _query(derived_project, "SELECT file_role FROM files "
                                    "WHERE file_role='manifest_ui_role'")] == ["manifest_ui_role"]


def test_adopt_modal_conflict_stays_inline(derived_project: Project, tmp_path: Path) -> None:
    file_id = _file_id(derived_project, "protein_fasta")
    source = tmp_path / "conflict-source.faa"
    source.write_text(">p1\n" + "G" * 25 + "\n", encoding="utf-8")
    # Same entity + role as the file adopted above, different bytes.
    adopted = actions.adopt(derived_project, items=[{
        "path": str(source), "entity_type": "annotation", "entity_id": "ANN_000001",
        "role": "conflict_role", "derived_from": [file_id],
    }])
    assert adopted["registered"] == 1
    conflict = tmp_path / "conflict.faa"
    conflict.write_text(">p1\n" + "A" * 25 + "\n", encoding="utf-8")

    async def scenario() -> None:
        app = OperonApp(derived_project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            modal = AdoptModal(derived_project, path=str(conflict), derived_from=[file_id],
                               entity_type="annotation", entity_id="ANN_000001")
            await _push(pilot, modal, "#adopt-preview-button")
            (await _q(modal, "#adopt-role", Input)).value = "conflict_role"
            await pilot.pause()
            (await _q(modal, "#confirm", Button)).press()
            error_view = await _q(modal, "#modal-error", Static)
            await _wait_until(
                lambda: "conflict" in _static_text(error_view).lower(),
                "adopt conflict to surface", timeout=30.0,
            )
            assert isinstance(app.screen, AdoptModal)
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())


def test_fanout_modal_requires_the_dry_run(derived_project: Project) -> None:
    assignments = _file_id(derived_project, "assignments")

    async def scenario() -> None:
        app = OperonApp(derived_project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            modal = FanoutModal(derived_project, _file_id(derived_project, "protein_fasta"))
            await _push(pilot, modal, "#fanout-preview-table")
            table = (await _q(modal, "#fanout-preview-table", DataTable))
            assert (await _q(modal, "#confirm", Button)).disabled  # no dry run yet
            modal.confirm()
            assert "run the dry run first" in _static_text(
                await _q(modal, "#modal-error", Static))

            (await _q(modal, "#fanout-assignments", Input)).value = assignments
            (await _q(modal, "#fanout-entity-type", Select)).value = "annotation"
            (await _q(modal, "#fanout-entity-id", Input)).value = "ANN_000001"
            (await _q(modal, "#fanout-role-prefix", Input)).value = "preview_units"
            await pilot.pause()
            assert "--assignments-file" in modal.command_text()
            (await _q(modal, "#fanout-preview-button", Button)).press()
            await _wait_until(lambda: table.row_count == 2, "fanout dry run")
            assert "would_create" in _static_text(await _q(modal, "#fanout-preview", Static))
            assert (await _q(modal, "#confirm", Button)).disabled is False
            assert _query(derived_project, "SELECT * FROM files WHERE file_role LIKE 'preview_units:%'") == []

            # Editing an input invalidates the preview and disables Confirm again.
            (await _q(modal, "#fanout-role-prefix", Input)).value = "ui_units"
            await pilot.pause()
            assert (await _q(modal, "#confirm", Button)).disabled is True
            # The preview table grows the form, which can shift the buttons out of
            # reach for a positional click; press them directly and assert on the
            # result (the same discovery-then-run flow a user gets).
            (await _q(modal, "#fanout-preview-button", Button)).press()
            confirm_button = await _q(modal, "#confirm", Button)
            await _wait_until(lambda: confirm_button.disabled is False, "second dry run")
            (await _q(modal, "#confirm", Button)).press()
            await _wait_until(lambda: not isinstance(app.screen, FanoutModal),
                              "fanout modal to close", timeout=30.0)
            await pilot.pause()
            # The outcome is asserted on disk below; the notification itself is
            # only display text and Textual retires notifications on a timer.

    _run(scenario())
    roles = sorted(row["file_role"] for row in
                   _query(derived_project,
                          "SELECT file_role FROM files WHERE file_role LIKE 'ui_units:%'"))
    assert roles == ["ui_units:unitA", "ui_units:unitB"]


def test_classify_action_runs_the_profile(derived_project: Project) -> None:
    result = actions.run_classify(derived_project, "test_tiers")
    assert result["profile"] == "test_tiers"
    assert result["labels_written"] == result["sequences"]
    labels = _query(derived_project,
                    "SELECT seqid, label FROM sequence_labels ORDER BY seqid, label")
    # p1/p2/p4 carry a Specific bHLH short_name; p3 and p5 have no matching hit, so
    # the explicit ``absent`` rule labels them before the ``default`` rule can.
    # classify labels by the seqid parsed from the query id (up to the first space),
    # so p2's header becomes "p2".
    assert {row["seqid"]: row["label"] for row in labels} == {
        "p1": "BHLH", "p2": "BHLH", "p3": "OTHER", "p4": "BHLH", "p5": "OTHER",
    }
    rerun = actions.run_classify(derived_project, "test_tiers")
    assert rerun["labels_written"] == 0 and rerun["labels_removed"] == 0
    assert _query(derived_project,
                  "SELECT * FROM workflow_runs WHERE step='classify-sequences'")
    with pytest.raises(ValidationError):
        actions.run_classify(derived_project, "../escape")
