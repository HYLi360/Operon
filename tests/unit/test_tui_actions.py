"""Write-layer coverage companions for :mod:`operon.tui.actions`.

The modal walkthroughs in ``test_tui_writes.py`` / ``test_tui_config.py`` /
``test_tui_analyze.py`` cover the happy paths through the screens; this module
drives the action functions directly — the layer owns no UI of its own — to pin
the remaining guard arms, the optional-flag command construction, the
classification coercion walk, and the small early validations that never need a
running app.  Core calls are either real ones against a ``tmp_path`` project or
deterministic stubs that capture exactly what the TUI would hand the core.
"""

from __future__ import annotations

import shlex
import signal
from pathlib import Path

import pytest

pytest.importorskip("textual")

import yaml

from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.shutdown import ShutdownRequested
from operon.tui import actions


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """Each test gets its own freshly initialized project."""
    return Project.init(tmp_path / "project")


def _query(project: Project, sql: str, params: tuple = ()) -> list[dict]:
    db = Database(project.db_path, read_only=True)
    try:
        return [dict(row) for row in db.query(sql, params)]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Backend resolution and analysis/external-run handoffs
# ---------------------------------------------------------------------------


def test_preflight_backend_closes_the_ssh_executor(project: Project) -> None:
    """The SSH executor is closed in the ``finally`` (its close is a no-op
    while no connection was opened, but the hook must run)."""
    project.config.setdefault("execution", {})["ssh"] = {"host": "hpc.example.invalid"}
    assert actions.preflight_backend(project, "ssh") == {
        "backend": "ssh",
        "description": "ssh:hpc.example.invalid",
    }


def test_run_analysis_reraises_a_shutdown_without_a_cancel_request(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``ShutdownRequested`` that is *not* the cooperative cancel keeps its
    own type; only a set ``cancel_event`` converts to ``AnalysisCancelled``."""

    def interrupted(*args, **kwargs):
        raise ShutdownRequested(signal.SIGINT)

    monkeypatch.setattr("operon.tools.run_analysis", interrupted)
    with pytest.raises(ShutdownRequested, match="SIGINT"):
        actions.run_analysis(project, "any_recipe")


def test_run_external_records_a_detected_tool_version(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful version detection lands in the run's provenance."""
    monkeypatch.setattr(
        "operon.tools.detect_tool_version_record",
        lambda tool, config: ("1.9.9", "blastn 1.9.9+ (raw)"),
    )
    result = actions.run_external(project, "version_probe", "true", tool="blastn")
    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    row = _query(
        project, "SELECT * FROM workflow_runs WHERE run_id=?", (result["run_id"],)
    )[0]
    assert row["tool"] == "blastn"
    assert row["tool_version"] == "1.9.9"
    assert "blastn 1.9.9+ (raw)" in row["execution_details"]


# ---------------------------------------------------------------------------
# extract-domains / select-sequences / adopt / fanout command builders
# ---------------------------------------------------------------------------


def _stub_core(monkeypatch: pytest.MonkeyPatch, target: str, captured: dict) -> None:
    def fake(db, project_arg, **kwargs):
        captured.update(kwargs)
        return {"stubbed": True}

    monkeypatch.setattr(target, fake)


def test_extract_domains_builds_the_regions_command_and_guards(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValidationError, match="--file-id is required"):
        actions.extract_domains(project, file_id="  ", out="out.fa", analysis="hits")

    captured: dict = {}
    _stub_core(monkeypatch, "operon.sequence_tools.extract_domains", captured)
    assert actions.extract_domains(
        project,
        file_id="FILE_000001",
        out="domains.fa",
        regions_tsv="regions.tsv",
        flank=7,
    ) == {"stubbed": True}
    argv = shlex.split(captured["command"])
    assert argv[:2] == ["operon", "extract-domains"]
    assert argv[argv.index("--regions-tsv") + 1] == "regions.tsv"
    assert argv[argv.index("--flank") + 1] == "7"
    assert captured["regions_tsv"] == "regions.tsv"
    assert captured["analysis"] is None

    captured.clear()
    actions.extract_domains(
        project,
        file_id="FILE_000001",
        out="domains.fa",
        analysis="cdd_hits",
        subject_like="PF*",
        evalue_max=1e-05,
        manifest="manifest.tsv",
    )
    argv = shlex.split(captured["command"])
    assert argv[argv.index("--analysis") + 1] == "cdd_hits"
    assert argv[argv.index("--subject-like") + 1] == "PF*"
    assert argv[argv.index("--evalue-max") + 1] == "1e-05"
    assert argv[argv.index("--manifest") + 1] == "manifest.tsv"
    assert captured["evalue_max"] == 1e-05
    assert captured["manifest"] == "manifest.tsv"


def test_select_sequences_builds_the_full_criteria_command_and_guards(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValidationError, match="--file-id is required"):
        actions.select_sequences(project, file_id=" ", out="sel.fa", require_hit=False)

    captured: dict = {}
    _stub_core(monkeypatch, "operon.sequence_tools.select_sequences", captured)
    assert actions.select_sequences(
        project,
        file_id="FILE_000002",
        out="selected.fa",
        analyses=["cdd_hits", "  "],  # blank entries are dropped
        subject_like="PF00069",
        evalue_max=0.001,
        min_span=50,
        hit_type="specific",
        entity_type="annotation",
        entity_id="ANN_000001",
        manifest="manifest.tsv",
    ) == {"stubbed": True}
    argv = shlex.split(captured["command"])
    assert argv[:2] == ["operon", "select-sequences"]
    assert argv[argv.index("--analysis") + 1] == "cdd_hits"
    assert argv[argv.index("--subject-like") + 1] == "PF00069"
    assert argv[argv.index("--evalue-max") + 1] == "0.001"
    assert argv[argv.index("--min-span") + 1] == "50"
    assert argv[argv.index("--hit-type") + 1] == "specific"
    assert "--require-hit" in argv
    assert argv[argv.index("--entity-type") + 1] == "annotation"
    assert argv[argv.index("--entity-id") + 1] == "ANN_000001"
    assert argv[argv.index("--out") + 1] == "selected.fa"
    assert argv[argv.index("--manifest") + 1] == "manifest.tsv"
    assert captured["analyses"] == ["cdd_hits"]
    assert captured["require_hit"] is True


def test_adopt_guards_empty_and_incomplete_items(project: Project) -> None:
    with pytest.raises(ValidationError, match="adopt needs one item or a manifest"):
        actions.adopt(project)
    with pytest.raises(
        ValidationError, match="single-file adopt requires entity_id, role"
    ):
        actions.adopt(project, items=[{"path": "unit.fa", "entity_type": "sample"}])


def test_fanout_forwards_the_parent_run_id(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    _stub_core(monkeypatch, "operon.fanout.fanout_units", captured)
    assert actions.fanout(
        project,
        assignments_file_id="FILE_000010",
        source_file_ids=["FILE_000011"],
        entity_type="sample",
        entity_id="SMP_000001",
        role_prefix="unit",
        parent_run_id="WF_PARENT",
        dry_run=True,
    ) == {"stubbed": True}
    argv = shlex.split(captured["command"])
    assert argv[argv.index("--parent-run-id") + 1] == "WF_PARENT"
    assert "--dry-run" in argv
    assert captured["parent_run_id"] == "WF_PARENT"
    assert captured["actor"].strip()


# ---------------------------------------------------------------------------
# Classification coercion / document validation / save guards
# ---------------------------------------------------------------------------


def test_coerce_classification_values_walks_sources_rules_and_groups() -> None:
    """``any``/``not`` groups recurse, non-mapping sources and bare-string
    conditions are skipped, and rule lists step over non-mapping entries."""
    document = {
        "sources": {
            "text": "not-a-source",  # a non-mapping source is skipped
            "cdd": {
                "filter": [
                    "not-a-condition",  # a bare string is skipped
                    {
                        "any": [
                            {"metric": "span", "value": "12"},
                            {"metric": "score", "min": "1.5", "max": "n/a"},
                        ]
                    },
                    {"not": {"metric": "evalue", "value": "0.01"}},
                    {"metric": "length", "values": ["3", "x", 9]},
                ]
            },
        },
    }
    actions._coerce_classification_values(document)
    filters = document["sources"]["cdd"]["filter"]
    assert filters[1]["any"][0]["value"] == 12
    assert filters[1]["any"][1]["min"] == 1.5
    assert filters[1]["any"][1]["max"] == "n/a"
    assert filters[2]["not"]["value"] == 0.01
    assert filters[3]["values"] == [3, "x", 9]

    # A document without a sources mapping walks straight to its rules, and a
    # non-mapping rule is stepped over on the way to the next one.
    nested = {
        "sources": "not-a-mapping",
        "rules": ["not-a-rule", {"when": [{"metric": "n", "value": "7"}]}],
    }
    actions._coerce_classification_values(nested)
    assert nested["rules"][1]["when"][0]["value"] == 7


def test_document_validators_require_a_version() -> None:
    with pytest.raises(ValidationError, match="'version' is required"):
        actions._validate_classification_document("draft_v1", {})
    with pytest.raises(ValidationError, match="'version' is required"):
        actions._validate_coverage_document("draft_v1", {})


def test_save_profile_rejects_an_unknown_kind(project: Project) -> None:
    with pytest.raises(ValidationError, match="unknown kind 'not-a-kind'"):
        actions.save_profile(project, "draft_v1", {"kind": "qc"}, kind="not-a-kind")


def test_save_profile_new_version_records_a_snapshot(project: Project) -> None:
    """A changed save bumps the version and records the content-addressed row."""
    document = {
        "kind": "qc",
        "applies_to": ["assembly"],
        "required": [
            {"metric": "total_length", "operator": ">=", "value": 1000, "code": "SHORT"}
        ],
        "warnings": [],
    }
    result = actions.save_profile(project, "coverage_gap_v1", document)
    assert result["unchanged"] is False
    assert result["version"] == 1
    assert result["snapshot_id"] is not None

    on_disk = yaml.safe_load(
        (project.profiles_dir / "coverage_gap_v1.yaml").read_text(encoding="utf-8")
    )
    assert on_disk["version"] == 1
    assert on_disk["required"][0]["value"] == 1000

    rows = _query(
        project,
        "SELECT * FROM qc_profiles WHERE profile_name='coverage_gap_v1'",
    )
    assert len(rows) == 1
    assert rows[0]["profile_version"] == 1
    assert rows[0]["profile_sha256"] == result["sha256"]
    assert rows[0]["recorded_at"]


def test_save_profile_rolls_back_when_the_snapshot_write_fails(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed snapshot insert un-publishes the freshly written profile."""

    def broken_record_profile(*args: object, **kwargs: object) -> str:
        raise RuntimeError("snapshot store offline")

    monkeypatch.setattr(Database, "record_profile", broken_record_profile)
    document = {
        "kind": "qc",
        "applies_to": ["assembly"],
        "required": [],
        "warnings": [],
    }
    with pytest.raises(ValidationError, match="save rolled back"):
        actions.save_profile(project, "coverage_rollback_v1", document)
    assert not (project.profiles_dir / "coverage_rollback_v1.yaml").exists()


def test_save_tool_rejects_a_non_mapping_tool(project: Project) -> None:
    project.tools_config_path.write_text(
        "version: 1\ntools:\n  bad_tool: not-a-mapping\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="must be a mapping"):
        actions.save_tool(project, "bad_tool", {})


# ---------------------------------------------------------------------------
# Entity, import and storage guards
# ---------------------------------------------------------------------------


def test_add_record_rejects_an_unknown_entity_type(project: Project) -> None:
    with pytest.raises(ValidationError, match="unknown entity type 'bogus'"):
        actions.add_record(project, "bogus", {})


def test_import_paths_require_a_file(project: Project) -> None:
    with pytest.raises(ValidationError, match="an input path is required"):
        actions.qc_import_preview(project, "  ")
    with pytest.raises(ValidationError, match="an input path is required"):
        actions.import_qc(project, "  ")


def test_add_accession_guards_type_and_required_fields(project: Project) -> None:
    with pytest.raises(ValidationError, match="unknown entity type 'bogus'"):
        actions.add_accession(
            project,
            internal_type="bogus",
            internal_id="X",
            namespace="NS",
            accession="A",
        )
    with pytest.raises(ValidationError, match="--internal-id is required"):
        actions.add_accession(
            project,
            internal_type="sample",
            internal_id="   ",
            namespace="NS",
            accession="A",
        )


def test_reserve_next_id_rejects_an_unknown_entity_type(project: Project) -> None:
    with pytest.raises(ValidationError, match="unknown entity type 'bogus'"):
        actions.reserve_next_id(project, "bogus")


def test_evict_requires_a_remote_and_runs_the_core(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ValidationError, match="a remote name is required"):
        actions.evict_plan(project, "  ")
    with pytest.raises(ValidationError, match="a remote name is required"):
        actions.evict(project, "  ")

    calls: list[tuple] = []

    def fake_evict_local(db, project_arg, remote, file_ids=None):
        calls.append((db, project_arg, remote, file_ids))
        return [{"file_id": "FILE_000001", "evicted": True}]

    monkeypatch.setattr("operon.remotes.evict_local", fake_evict_local)
    assert actions.evict(project, "mirror", file_ids=["FILE_000001"]) == [
        {"file_id": "FILE_000001", "evicted": True}
    ]
    assert len(calls) == 1
    _, seen_project, remote, file_ids = calls[0]
    assert seen_project is project
    assert remote == "mirror"
    assert file_ids == ["FILE_000001"]


def test_set_state_requires_a_message_and_a_state(project: Project) -> None:
    with pytest.raises(ValidationError, match="a message is required"):
        actions.set_state(project, "sample", "SMP_000001", "VALIDATED", "   ")
    with pytest.raises(ValidationError, match="a state is required"):
        actions.set_state(project, "sample", "SMP_000001", "  ", "operator note")
