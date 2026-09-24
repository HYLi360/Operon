"""``operon.pipeline`` — the shared core behind `operon run-pipeline`.

The four stages are the manifest's normal path (ingest → standardize → QC →
evaluate), so these tests use the demo project's own shapes: a fresh metadata
row with no ``entity_state`` yet is what a brand-new entity looks like, and the
state machine lets it through ingest → CHECKSUM_VERIFIED → STANDARDIZED → QC →
decision.  The demo's own entities are all in terminal states, which is why the
run tests add one.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ValidationError
from operon.pipeline import PIPELINE_STEPS, plan_pipeline, reason_list, run_pipeline


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("pipeline-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    target = tmp_path / "project"
    shutil.copytree(demo_template.root, target)
    return Project.find(target)


def _fresh_assembly(project: Project, assembly_id: str = "ASM_000900") -> None:
    """A demo-shaped assembly with no entity_state row yet (a new entity)."""
    db = Database(project.db_path)
    try:
        db.insert_row("assemblies", {
            "assembly_id": assembly_id,
            "sample_id": "SMP_000001",
            "assembly_accession": f"GCA_{assembly_id[-6:]}",
            "assembly_version": 1,
            "assembly_level": "contig",
            "assembly_method": "test fixture",
            "reference_status": "representative",
        })
    finally:
        db.close()


def _source_fasta(tmp_path: Path, name: str = "fixture.fasta") -> Path:
    path = tmp_path / name
    path.write_text(">ctg1\n" + "ACGTACGTACGT" * 25 + "\n", encoding="utf-8")
    return path


def _read_only(project: Project) -> Database:
    return Database(project.db_path, read_only=True)


def test_plan_pipeline_validates_the_pipeline_preconditions(project: Project,
                                                            tmp_path: Path) -> None:
    db = _read_only(project)
    try:
        base = {"source": str(_source_fasta(tmp_path)), "entity_type": "assembly",
                "entity_id": "ASM_000001", "role": "genome_fasta"}
        blank_source = dict(base, source="   ")
        with pytest.raises(ValidationError):
            plan_pipeline(db, project, **blank_source)
        with pytest.raises(ValidationError):
            plan_pipeline(db, project, **dict(base, entity_id=""))
        with pytest.raises(ValidationError):
            plan_pipeline(db, project, **dict(base, role=""))
        with pytest.raises(ValidationError):
            plan_pipeline(db, project, **dict(base, profile="no_such_profile"))
    finally:
        db.close()


def test_plan_pipeline_reports_the_resolved_profile_and_curated_gate(
        project: Project, tmp_path: Path) -> None:
    source = _source_fasta(tmp_path)
    db = _read_only(project)
    try:
        plan = plan_pipeline(db, project, source=source, entity_type="assembly",
                             entity_id="ASM_000001", role="genome_fasta")
    finally:
        db.close()

    assert plan["profile"] == "assembly_production_v1"  # the project default
    assert plan["default_profile"] == "assembly_production_v1"
    assert plan["steps"] == list(PIPELINE_STEPS)
    assert plan["source_exists"] is True
    assert plan["curated_targets"] == []  # the demo's decisions are not curated

    db = Database(project.db_path)
    try:
        db.upsert_decision({
            "entity_type": "assembly", "entity_id": "ASM_000001",
            "profile": "assembly_production_v1",
            "decision": "PASS", "curated_decision": "PASS", "curated_by": "reviewer",
            "reason_codes": "[]", "observed": "{}", "thresholds": "{}",
            "evaluated_at": "2026-01-01T00:00:00+00:00",
        })
    finally:
        db.close()

    db = _read_only(project)
    try:
        plan = plan_pipeline(db, project, source=source, entity_type="assembly",
                             entity_id="ASM_000001", role="genome_fasta")
        assert plan["curated_targets"] == [("assembly", "ASM_000001")]
        explicit = plan_pipeline(db, project, source=source, entity_type="assembly",
                                 entity_id="ASM_000001", role="genome_fasta",
                                 profile="reads_qc_v1")
    finally:
        db.close()
    assert explicit["profile"] == "reads_qc_v1"
    assert explicit["curated_targets"] == []  # a different profile has no curated row


def test_run_pipeline_walks_the_four_stages(project: Project, tmp_path: Path) -> None:
    _fresh_assembly(project)
    source = _source_fasta(tmp_path)
    lines: list[str] = []
    db = Database(project.db_path)
    try:
        result = run_pipeline(db, project, source=source, entity_type="assembly",
                              entity_id="ASM_000900", role="genome_fasta",
                              progress=lines.append)
    finally:
        db.close()

    assert lines[0] == f"[1/4] ingest {source}"
    assert any(line.startswith("[4/4] evaluate with profile assembly_production_v1")
               for line in lines), lines
    assert result["qc_ok"] is True
    assert result["decision"] in {"PASS", "REVIEW", "REJECTED", "FAIL"}
    assert result["file_id"] and result["sha256"]
    assert Path(result["target"]).exists()

    db = _read_only(project)
    try:
        file_row = dict(db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (result["file_id"],)).fetchone())
        assert file_row["entity_id"] == "ASM_000900"
        assert file_row["status"] == "STANDARDIZED"
        assert db.get_entity_state("assembly", "ASM_000900") in {"ACCEPTED", "REVIEW", "REJECTED"}
    finally:
        db.close()


def test_run_pipeline_stops_before_evaluation_when_qc_fails(
        project: Project, tmp_path: Path, monkeypatch) -> None:
    from operon import pipeline as pipeline_module

    _fresh_assembly(project, "ASM_000901")
    monkeypatch.setattr(pipeline_module, "qc_file",
                        lambda *_args, **_kwargs: {"ok": False, "error": "synthetic failure"})
    lines: list[str] = []
    db = Database(project.db_path)
    try:
        result = run_pipeline(db, project, source=_source_fasta(tmp_path, "broken.fasta"),
                              entity_type="assembly", entity_id="ASM_000901",
                              role="genome_fasta", progress=lines.append)
    finally:
        db.close()

    assert result["qc_ok"] is False
    assert result["qc_error"] == "synthetic failure"
    assert result["decision"] is None
    assert not any(line.startswith("[4/4]") for line in lines), lines


@pytest.mark.bug("ODR-0042")
def test_run_pipeline_fetches_remote_sources_like_ingest(
        project: Project, tmp_path: Path, monkeypatch) -> None:
    """`sftp://`/`remote://` sources are pre-fetched and the temporary copy removed."""
    staged = _source_fasta(tmp_path, "fetched.fasta")
    calls: list[str] = []

    def fake_fetch(_project, url):
        calls.append(url)
        return staged

    monkeypatch.setattr("operon.remotes.fetch_url_to_temp", fake_fetch)
    _fresh_assembly(project, "ASM_000902")
    lines: list[str] = []
    db = Database(project.db_path)
    try:
        result = run_pipeline(db, project, source="sftp://host/incoming.fasta",
                              entity_type="assembly", entity_id="ASM_000902",
                              role="genome_fasta", progress=lines.append)
        file_row = dict(db.conn.execute(
            "SELECT * FROM files WHERE file_id=?", (result["file_id"],)).fetchone())
    finally:
        db.close()

    assert calls == ["sftp://host/incoming.fasta"]
    assert lines[0] == "[1/4] ingest sftp://host/incoming.fasta"
    assert file_row["source_url"] == "sftp://host/incoming.fasta"
    assert not staged.exists()  # the temporary fetch is gone


def test_reason_list_normalizes_decision_payloads() -> None:
    assert reason_list(["a", "b"]) == ["a", "b"]
    assert reason_list('["a", "b"]') == ["a", "b"]
    assert reason_list("not json") == ["not json"]
    assert reason_list('{"a": 1}') == ['{"a": 1}']
    assert reason_list(None) == []
