"""CLI branch coverage for output, validation, and dispatch behavior."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from operon import cli
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ExternalToolError, ValidationError
from operon.utils import sha256_file


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


def test_positive_int_and_runtime_parameter_validation():
    assert cli._positive_int("3") == 3
    for value in ("0", "-1", "x"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._positive_int(value)
    assert cli._parse_runtime_parameters(["alpha=1", " beta =two=parts"]) == {
        "alpha": "1", "beta": "two=parts"
    }
    for items in (["bad"], ["=x"], ["x="], ["x=1", "x=2"]):
        with pytest.raises(ValidationError):
            cli._parse_runtime_parameters(items)


def test_status_schema_migrate_and_next_id(project_db, capsys):
    project, db = project_db
    assert cli._cmd_status(ns(entity_type=None, entity_id=None), db) == 0
    assert "no entity states" in capsys.readouterr().out
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Example"})
    db.set_entity_state("organism", "ORG_000001", "METADATA_VALIDATED", "ok")
    assert cli._cmd_status(ns(entity_type="organism", entity_id="ORG_000001"), db) == 0
    assert "ORG_000001" in capsys.readouterr().out
    assert cli._cmd_schema(ns(dump=False), project) == 0
    assert str(project.schema_path) in capsys.readouterr().out
    assert cli._cmd_schema(ns(dump=True), project) == 0
    assert "schema_version" in capsys.readouterr().out
    assert cli._cmd_migrate(db) == 0
    assert '"integrity_check": "ok"' in capsys.readouterr().out
    assert cli._cmd_next_id(ns(entity_type="sample"), db) == 0
    assert "SMP_" in capsys.readouterr().out


def test_migrate_returns_failure_for_integrity_or_foreign_keys(capsys):
    class FakeDB:
        def query(self, sql):
            if "integrity_check" in sql:
                return [["corrupt"]]
            if "foreign_key_check" in sql:
                return [[1]]
            return []

    assert cli._cmd_migrate(FakeDB()) == 1
    assert '"foreign_key_violations": 1' in capsys.readouterr().out


def test_add_entities_accession_and_fk_validation(project_db, capsys):
    project, db = project_db
    commands = [
        ("organism", "ORG_000001", ["scientific_name=Example species"]),
        ("sample", "SMP_000001", ["organism_id=ORG_000001"]),
        ("run", "RUN_000001", ["sample_id=SMP_000001"]),
        ("assembly", "ASM_000001", ["sample_id=SMP_000001"]),
        ("annotation", "ANN_000001", ["assembly_id=ASM_000001"]),
    ]
    for entity_type, record_id, fields in commands:
        args = ns(entity_type=entity_type, record_id=record_id, field=fields)
        assert cli._cmd_add(args, project, db) == 0
    assert "added annotation ANN_000001" in capsys.readouterr().out
    assert cli._cmd_add_accession(ns(
        internal_type="organism", internal_id="ORG_000001", namespace="LAB",
        accession="EX-1", acc_version="1", primary=True,
    ), project, db) == 0
    assert "LAB:EX-1" in capsys.readouterr().out

    for entity_type, row in [
        ("sample", {"organism_id": "ORG_MISSING"}),
        ("run", {"sample_id": "SMP_MISSING"}),
        ("assembly", {"sample_id": "SMP_MISSING"}),
        ("annotation", {"assembly_id": "ASM_MISSING"}),
    ]:
        with pytest.raises(Exception):
            cli._check_fks_for_row(db, entity_type, row, True)
    with pytest.raises(ValidationError, match="fasta_file_id"):
        cli._check_fks_for_row(
            db, "assembly", {"assembly_id": "ASM_000001", "fasta_file_id": "FIL_MISSING"}, True
        )


def test_verify_standardize_qc_and_sync_outputs(project_db, monkeypatch, capsys):
    project, db = project_db
    monkeypatch.setattr(cli, "verify_files", lambda *_a: [
        {"file_id": "F1", "relative_path": "a", "status": "CHECKSUM_VERIFIED",
         "remote": None, "current_sha256": "a", "error": None},
        {"file_id": "F2", "relative_path": "b", "status": "MISSING",
         "remote": "r", "current_sha256": None, "error": "gone"},
    ])
    assert cli._cmd_verify(ns(file_id=[]), project, db) == 1
    assert "MISSING" in capsys.readouterr().out
    monkeypatch.setattr(cli, "verify_files", lambda *_a: [])
    assert cli._cmd_verify(ns(file_id=["F1"]), project, db) == 0
    assert "verified 0" in capsys.readouterr().out

    monkeypatch.setattr(cli, "standardize_file", lambda *_a, **_k: {"action": "linked", "target": "x"})
    assert cli._cmd_standardize(ns(file_id=["F1"], link="copy"), project, db) == 0
    monkeypatch.setattr(cli, "standardize_all", lambda *_a, **_k: [
        {"file_id": "F1", "action": "cached", "target": "x"},
        {"file_id": "F2", "error": "bad"},
    ])
    assert cli._cmd_standardize(ns(file_id=[], link="copy"), project, db) == 0
    captured = capsys.readouterr()
    assert "cached" in captured.out and "ERROR bad" in captured.err

    monkeypatch.setattr("operon.qc_module.qc_all", lambda *_a, **_k: [
        {"file_id": "F1", "ok": True}, {"file_id": "F2", "ok": False, "error": "bad"},
    ])
    args = ns(entity_type=None, entity_id=None, file_id=None, sample_size=None,
              phred_offset=None, rehash=False)
    assert cli._cmd_qc(args, project, db) == 1
    assert "FAILED bad" in capsys.readouterr().err
    monkeypatch.setattr("operon.qc_module.qc_all", lambda *_a, **_k: [
        {"file_id": "F1", "ok": True, "file_qc_state": "QC_COMPLETE",
         "entity_qc_state": "QC_FAILED", "file_statuses": []},
    ])
    assert cli._cmd_qc(args, project, db) == 1
    assert cli._print_sync_results("push", "r", [
        {"file_id": "F1", "relative_path": "a", "status": "uploaded"},
        {"file_id": "F2", "relative_path": "b", "status": "error", "error": "bad"},
    ]) == 1
    assert "error: 1" in capsys.readouterr().out
    assert cli._print_sync_results("pull", "r", []) == 0


def test_import_qc_validation_and_numeric_paths(project_db, tmp_path, capsys):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Example"})
    bad = tmp_path / "bad.tsv"
    bad.write_text("entity_type\norganism\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="missing columns"):
        cli._cmd_import_qc(ns(tsv_file=bad), project, db)

    good = tmp_path / "good.tsv"
    good.write_text(
        "entity_type\tentity_id\tqc_stage\tmetric_name\tmetric_value\ttool\ttool_version\tparameter_set\n"
        "organism\tORG_000001\texternal\tlabel\tnot-numeric\ttool\t1\t\n",
        encoding="utf-8",
    )
    assert cli._cmd_import_qc(ns(tsv_file=good), project, db) == 0
    row = db.query("SELECT metric_numeric, input_identity, parameter_set FROM qc_results")[0]
    assert row["metric_numeric"] is None
    assert row["input_identity"] == "entity:organism:ORG_000001"
    assert row["parameter_set"] == "external"
    assert "imported 1" in capsys.readouterr().out

    missing_file = tmp_path / "missing-file.tsv"
    missing_file.write_text(
        "entity_type\tentity_id\tfile_id\tqc_stage\tmetric_name\tmetric_value\ttool\ttool_version\tparameter_set\n"
        "organism\tORG_000001\tFIL_X\texternal\tx\t1\ttool\t1\tp\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="file_id FIL_X does not exist"):
        cli._cmd_import_qc(ns(tsv_file=missing_file), project, db)

    db.insert_row("organisms", {"organism_id": "ORG_000002", "scientific_name": "Other"})
    db.insert_row("files", {
        "file_id": "FIL_000001", "entity_type": "organism", "entity_id": "ORG_000001",
        "file_role": "metadata", "format": "tsv", "compression": "none",
        "relative_path": "raw/metadata.tsv", "sha256": "a" * 64, "size_bytes": 1,
        "status": "CHECKSUM_VERIFIED",
    })
    columns = (
        "entity_type\tentity_id\tfile_id\tfile_sha256\tqc_stage\tmetric_name\t"
        "metric_value\ttool\ttool_version\tparameter_set\n"
    )
    wrong_owner = tmp_path / "wrong-owner.tsv"
    wrong_owner.write_text(
        columns + "organism\tORG_000002\tFIL_000001\t\texternal\tx\t1\ttool\t1\tp\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="belongs to"):
        cli._cmd_import_qc(ns(tsv_file=wrong_owner), project, db)

    wrong_sha = tmp_path / "wrong-sha.tsv"
    wrong_sha.write_text(
        columns + "organism\tORG_000001\tFIL_000001\t" + "b" * 64
        + "\texternal\tx\t1\ttool\t1\tp\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="file_sha256 does not match"):
        cli._cmd_import_qc(ns(tsv_file=wrong_sha), project, db)

    matching_file = tmp_path / "matching-file.tsv"
    matching_file.write_text(
        columns + "organism\tORG_000001\tFIL_000001\t" + "a" * 64
        + "\texternal\tx\t2\ttool\t1\tp\n",
        encoding="utf-8",
    )
    assert cli._cmd_import_qc(ns(tsv_file=matching_file), project, db) == 0
    file_metric = db.query(
        "SELECT input_identity FROM qc_results WHERE file_id='FIL_000001'"
    )[0]
    assert file_metric["input_identity"] == f"file:FIL_000001:{'a' * 64}"


def test_run_external_tools_and_analyze_output(project_db, monkeypatch, capsys):
    project, db = project_db
    with pytest.raises(ValidationError, match="must not be empty"):
        cli._cmd_run_external(ns(
            command_line=" ", step="x", entity_type=None, entity_id=None,
            parameter_set=None, expected_output=[], cwd=None, timeout=None, backend=None,
            tool=None, inputs=[], threads=None,
        ), project, db)
    monkeypatch.setattr("operon.workflow.run_external_command", lambda *_a, **_k: {
        "run_id": "WF1", "step": "x", "status": "completed", "exit_code": 0,
        "finished_at": "now",
    })
    assert cli._cmd_run_external(ns(
        command_line="echo ok", step="x", entity_type=None, entity_id=None,
        parameter_set=None, expected_output=[], cwd=None, timeout=None, backend=None,
        tool=None, inputs=[], threads=None,
    ), project, db) == 0
    assert '"run_id": "WF1"' in capsys.readouterr().out

    monkeypatch.setattr("operon.tools.print_tools_table", lambda _p: ("tools", False))
    assert cli._cmd_tools_check(project) == 1
    monkeypatch.setattr("operon.tools.run_analysis", lambda *_a, **_k: [
        {"file_id": "F1", "entity_type": "assembly", "entity_id": "A1",
         "analysis": "x", "status": "planned", "tool_version": "1", "output": "o"},
        {"file_id": "F2", "status": "error", "error": "bad"},
    ])
    args = ns(analysis="x", entity_type=None, entity_id=None, dry_run=True, force=False,
              limit=None, threads=1, backend=None, keep_partial=False, param=[])
    assert cli._cmd_analyze(args, project, db) == 1
    assert "1 job(s) left" in capsys.readouterr().out
    monkeypatch.setattr("operon.tools.run_analysis", lambda *_a, **_k: [])
    args.dry_run = False
    assert cli._cmd_analyze(args, project, db) == 0
    assert "0/0 succeeded" in capsys.readouterr().out


def _run_external_ns(**overrides):
    values = dict(
        command_line="echo ok", step="x", entity_type=None, entity_id=None,
        parameter_set=None, expected_output=[], cwd=None, timeout=None, backend=None,
        tool=None, inputs=[], threads=None,
    )
    values.update(overrides)
    return ns(**values)


def test_run_external_tool_version_threads_and_inputs(project_db, monkeypatch, capsys):
    project, db = project_db
    captured: dict = {}

    def fake_run(_db, _project, _argv, **kwargs):
        captured.update(kwargs)
        return {"run_id": "WF2", "step": "x", "status": "completed", "exit_code": 0,
                "finished_at": "now"}

    monkeypatch.setattr("operon.workflow.run_external_command", fake_run)
    monkeypatch.setattr("operon.tools.detect_tool_version_record",
                        lambda _tool, _config: ("2.15.0", "blastn: 2.15.0+"))
    # blastn is preconfigured in the default config/tools.yaml.
    assert cli._cmd_run_external(_run_external_ns(
        command_line="blastn -h", tool="blastn", inputs=["in.fa"], threads=8,
    ), project, db) == 0
    assert captured["tool"] == "blastn"
    assert captured["tool_version"] == "2.15.0"
    assert captured["threads"] == 8
    assert captured["inputs"] == ["in.fa"]
    assert captured["extra_details"] == {"tool_version_raw": "blastn: 2.15.0+"}

    # Unconfigured tool name: recorded as-is, version stays None.
    captured.clear()
    assert cli._cmd_run_external(_run_external_ns(tool="notatool"), project, db) == 0
    assert captured["tool"] == "notatool"
    assert captured["tool_version"] is None
    assert captured["extra_details"] is None

    # Version detection failure degrades to a warning and a NULL version.
    def boom(*_a, **_k):
        raise ExternalToolError("cannot launch blastn")

    monkeypatch.setattr("operon.tools.detect_tool_version_record", boom)
    captured.clear()
    assert cli._cmd_run_external(_run_external_ns(tool="blastn"), project, db) == 0
    assert captured["tool"] == "blastn"
    assert captured["tool_version"] is None
    assert "warning: version detection" in capsys.readouterr().err


def test_run_external_input_hashing(project_db, tmp_path):
    from operon.workflow import run_external_command
    project, db = project_db
    source = tmp_path / "input.fa"
    source.write_text(">x\nACGT\n", encoding="utf-8")
    other = tmp_path / "other.fa"
    other.write_text(">y\nTTTT\n", encoding="utf-8")
    record = run_external_command(
        db, project, [sys.executable, "-c", "pass"], step="selftest_inputs",
        inputs=[other, source], extra_details={"tool_version_raw": "raw out"},
    )
    # The combined hash covers the sorted path:sha256 lines, not the order given.
    combined = "\n".join(
        f"{path}:{sha256_file(path)}" for path in sorted([source, other], key=str)
    )
    expected = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    assert record["input_sha256"] == expected
    details = json.loads(record["execution_details"])
    assert sorted(details["inputs"], key=lambda entry: entry["path"]) == [
        {"path": str(path), "sha256": sha256_file(path)}
        for path in sorted([source, other], key=str)
    ]
    assert details["tool_version_raw"] == "raw out"
    row = db.query("SELECT * FROM workflow_runs WHERE step='selftest_inputs'")[0]
    assert row["input_sha256"] == expected

    with pytest.raises(ValidationError, match="declared input does not exist"):
        run_external_command(
            db, project, [sys.executable, "-c", "pass"], step="x",
            inputs=[tmp_path / "missing.fa"],
        )


def test_analysis_results_support_metrics_and_hits(project_db, capsys):
    _project, db = project_db
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute(
        "INSERT INTO analysis_jobs(job_id, analysis_name, entity_type, entity_id, file_id, tool, "
        "tool_version, parameter_set, parameter_sha256, input_sha256, database_identity, status, started_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "a", "organism", "ORG1", "F1", "tool", "1", "p", "sha", "input", "db",
         "completed", "now"),
    )
    db.conn.execute(
        "INSERT INTO analysis_results(job_id, analysis_name, entity_type, entity_id, file_id, "
        "metric_name, metric_value) VALUES(?,?,?,?,?,?,?)",
        (1, "a", "organism", "ORG1", "F1", "score", "1")
    )
    db.conn.execute(
        "INSERT INTO analysis_hits(job_id, analysis_name, entity_type, entity_id, query_id, subject_id, "
        "file_id, metric_name, metric_value, hit_rank) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (1, "a", "organism", "ORG1", "q", "s", "F1", "bitscore", "10", 1),
    )
    db.conn.commit()
    filters = dict(analysis="a", entity_type="organism", entity_id="ORG1", limit=10)
    assert cli._cmd_analysis_results(ns(hits=False, **filters), db) == 0
    assert "score" in capsys.readouterr().out
    assert cli._cmd_analysis_results(ns(hits=True, **filters), db) == 0
    assert "bitscore" in capsys.readouterr().out
    assert cli._cmd_analysis_results(ns(hits=True, analysis="missing", entity_type=None,
                                        entity_id=None, limit=10), db) == 0
    assert "no analysis results" in capsys.readouterr().out
    assert cli._cmd_analysis_results(ns(hits=False, analysis=None, entity_type=None,
                                        entity_id=None, limit=10), db) == 0


def test_remotes_evaluate_pipeline_and_simple_report_branches(project_db, monkeypatch, capsys):
    project, db = project_db
    monkeypatch.setattr("operon.remotes.list_remotes", lambda _p: {})
    assert cli._cmd_remotes(ns(), project) == 0
    assert "no remotes configured" in capsys.readouterr().out
    monkeypatch.setattr("operon.remotes.list_remotes", lambda _p: {"good": {}, "bad": {}})
    monkeypatch.setattr("operon.remotes.check_remote", lambda _p, name: {
        "name": name, "type": "sftp", "address": "h", "root": "/r", "files": 0,
        "status": "ok" if name == "good" else "error", "error": "" if name == "good" else "bad",
    })
    assert cli._cmd_remotes(ns(), project) == 1

    assert cli._reason_list([1, "x"]) == ["1", "x"]
    assert cli._reason_list('["a"]') == ["a"]
    assert cli._reason_list('"a"') == ['"a"']
    assert cli._reason_list("not-json") == ["not-json"]
    assert cli._reason_list(3) == []
    with pytest.raises(ValidationError, match="entity-type is required"):
        cli._cmd_evaluate(ns(entity_id="X", entity_type=None, profile="p"), project, db)
    monkeypatch.setattr(cli, "evaluate_entity", lambda *_a: {
        "entity_type": "organism", "entity_id": "O", "profile": "p",
        "decision": "PASS", "reason_codes": '[]',
    })
    assert cli._cmd_evaluate(ns(entity_id="O", entity_type="organism", profile="p"), project, db) == 0
    monkeypatch.setattr(cli, "evaluate_all", lambda *_a: [])
    assert cli._cmd_evaluate(ns(entity_id=None, entity_type=None, profile="p"), project, db) == 0

    monkeypatch.setattr(cli, "ingest_file", lambda *_a, **_k: {"file_id": "F1", "sha256": "abcdef"})
    monkeypatch.setattr(cli, "standardize_file", lambda *_a, **_k: {"target": "std"})
    monkeypatch.setattr("operon.qc_module.qc_file", lambda *_a: {"ok": False, "error": "bad"})
    pipeline = ns(source="x", entity_type="assembly", entity_id="A", role="genome_fasta",
                  fmt=None, compression=None, source_url=None, profile=None)
    assert cli._cmd_run_pipeline(pipeline, project, db) == 1
    monkeypatch.setattr("operon.qc_module.qc_file", lambda *_a: {"ok": True})
    monkeypatch.setattr(cli, "evaluate_entity", lambda *_a: {"decision": "PASS", "reason_codes": []})
    assert cli._cmd_run_pipeline(pipeline, project, db) == 0


def test_run_pipeline_preflights_curated_evaluation_before_ingest(project_db, monkeypatch):
    project, db = project_db
    profile = project.config["qc"]["default_profile"]
    db.upsert_decision({
        "entity_type": "assembly", "entity_id": "ASM_1", "profile": profile,
        "decision": "PASS", "curated_decision": "PASS", "curated_by": "reviewer",
        "curated_reason": "manual review", "reason_codes": "[]", "observed": "{}",
        "thresholds": "{}", "evaluated_at": "2026-01-01T00:00:00+00:00",
    })

    class NotTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(cli.sys, "stdin", NotTTY())
    monkeypatch.setattr(cli.sys, "stdout", NotTTY())
    monkeypatch.setattr(cli, "ingest_file", lambda *_a, **_k: pytest.fail("ingest ran before confirmation"))
    pipeline = ns(source="x", entity_type="assembly", entity_id="ASM_1", role="genome_fasta",
                  fmt=None, compression=None, source_url=None, profile=None)
    with pytest.raises(ValidationError, match="pipeline will.*pass --yes"):
        cli._cmd_run_pipeline(pipeline, project, db)


def test_report_import_show_query_and_error_dispatch(project_db, monkeypatch, capsys):
    project, db = project_db
    monkeypatch.setattr(cli, "print_qc_table", lambda *_a: "qc")
    monkeypatch.setattr(cli, "export_qc_tsv", lambda *_a: Path("qc.tsv"))
    assert cli._cmd_qc_table(ns(entity_type=None, entity_id=None, export=True), project, db) == 0
    monkeypatch.setattr(cli, "print_decisions", lambda *_a: "decisions")
    assert cli._cmd_decisions(ns(profile=None), project, db) == 0
    monkeypatch.setattr(cli, "report_coverage", lambda *_a, **_k: {
        "scope_kind": "metadata", "scope_value": "all", "reference_set_id": "R1",
        "metrics": [{"rank": "genus", "numerator": 1, "denominator": 2,
                     "coverage_percent": 50, "threshold_percent": 60, "decision": "FAIL"}],
        "decision": "FAIL", "path": "report.tsv", "exit_code": 1,
    })
    args = ns(report_kind="coverage", reference_set="R1", release=None)
    assert cli._cmd_report(args, project, db) == 1
    monkeypatch.setattr(cli, "export_metadata_report", lambda *_a: Path("metadata"))
    assert cli._cmd_report(ns(report_kind="metadata", output=None), project, db) == 0
    with pytest.raises(ValidationError, match="unknown report kind"):
        cli._cmd_report(ns(report_kind="unknown"), project, db)

    monkeypatch.setattr("operon.import_wizard.run_dataset_wizard", lambda *_a: None)
    assert cli._cmd_import(ns(import_kind="dataset"), project, db) == 0
    monkeypatch.setattr("operon.import_wizard.run_dataset_wizard", lambda *_a: {"ok": True})
    assert cli._cmd_import(ns(import_kind="dataset"), project, db) == 0
    with pytest.raises(ValidationError, match="unknown import kind"):
        cli._cmd_import(ns(import_kind="unknown"), project, db)

    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Example"})
    assert cli._cmd_show(ns(identifier="ORG_000001", json=True), db) == 0
    assert '"organism_id": "ORG_000001"' in capsys.readouterr().out
    assert cli._cmd_show(ns(identifier="ORG_000001", json=False), db) == 0
    assert "Samples (0)" in capsys.readouterr().out
    assert cli._cmd_query(ns(sql="SELECT organism_id FROM organisms"), db) == 0
    assert "ORG_000001" in capsys.readouterr().out
    assert cli._cmd_query(ns(sql="SELECT organism_id FROM organisms WHERE 0"), db) == 0
    assert "no rows" in capsys.readouterr().out
    with pytest.raises(ValidationError, match="query failed"):
        cli._cmd_query(ns(sql="not sql"), db)

    assert main(["--project", str(project.root), "query", "not sql"]) == 2
    assert main([
        "--project", str(project.root), "run-external", "--step", "empty", "--command", " "
    ]) == 2
    monkeypatch.setattr(cli, "_open_project", lambda _args: (_ for _ in ()).throw(RuntimeError("boom")))
    assert main(["--project", str(project.root), "status"]) == 1


def test_main_keyboard_interrupt(project_db, monkeypatch):
    project, _db = project_db

    class FakeDB:
        def close(self):
            pass

    monkeypatch.setattr(cli, "_open_project", lambda _args: (project, FakeDB()))
    monkeypatch.setattr(cli, "_cmd_status", lambda *_a: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert main(["--project", str(project.root), "status"]) == 130


@pytest.mark.parametrize("temporary_kind", ["file", "directory"])
def test_ingest_remote_source_cleans_temporary_artifact(
    project_db, tmp_path, monkeypatch, temporary_kind, capsys
):
    project, db = project_db
    temporary = tmp_path / "downloaded"
    if temporary_kind == "directory":
        temporary.mkdir()
        (temporary / "part.fa").write_text(">x\nAC\n", encoding="utf-8")
    else:
        temporary.write_text(">x\nAC\n", encoding="utf-8")

    monkeypatch.setattr("operon.remotes.fetch_url_to_temp", lambda *_a: temporary)
    observed = {}

    def fake_ingest(_db, _project, source, *_args, **kwargs):
        observed.update(source=source, source_url=kwargs["source_url"])
        return {"file_id": "FIL_1", "relative_path": "archive/x", "sha256": "a" * 64}

    monkeypatch.setattr(cli, "ingest_file", fake_ingest)
    args = ns(
        source="remote://mirror/archive/x",
        source_url=None,
        entity_type="assembly",
        entity_id="ASM_1",
        role="genome_fasta",
        fmt=None,
        compression=None,
        move=False,
    )
    assert cli._cmd_ingest(args, project, db) == 0
    assert observed == {
        "source": str(temporary),
        "source_url": "remote://mirror/archive/x",
    }
    assert not temporary.exists()
    assert "registered FIL_1" in capsys.readouterr().out


def test_ingest_remote_source_cleans_temporary_artifact_after_failure(
    project_db, tmp_path, monkeypatch
):
    project, db = project_db
    temporary = tmp_path / "downloaded.fa"
    temporary.write_text(">x\nAC\n", encoding="utf-8")
    monkeypatch.setattr("operon.remotes.fetch_url_to_temp", lambda *_a: temporary)
    monkeypatch.setattr(
        cli, "ingest_file", lambda *_a, **_k: (_ for _ in ()).throw(ValidationError("bad"))
    )
    args = ns(
        source="sftp://host/archive/x.fa",
        source_url="original",
        entity_type="assembly",
        entity_id="ASM_1",
        role="genome_fasta",
        fmt=None,
        compression=None,
        move=False,
    )
    with pytest.raises(ValidationError, match="bad"):
        cli._cmd_ingest(args, project, db)
    assert not temporary.exists()


def test_ingest_local_source_does_not_use_remote_staging(project_db, tmp_path, monkeypatch):
    project, db = project_db
    source = tmp_path / "local.fa"
    source.write_text(">x\nAC\n", encoding="utf-8")
    observed = {}

    def fake_ingest(_db, _project, value, *_args, **kwargs):
        observed.update(source=value, source_url=kwargs["source_url"])
        return {"file_id": "FIL_1", "relative_path": "archive/x", "sha256": "a" * 64}

    monkeypatch.setattr(cli, "ingest_file", fake_ingest)
    assert cli._cmd_ingest(ns(
        source=str(source), source_url=None, entity_type="assembly", entity_id="ASM_1",
        role="genome_fasta", fmt=None, compression=None, move=False,
    ), project, db) == 0
    assert observed == {"source": str(source), "source_url": None}
    assert source.exists()


def test_taxonomy_and_report_dispatch_cover_all_supported_kinds(project_db, monkeypatch, capsys):
    project, db = project_db
    monkeypatch.setattr(cli, "import_ncbi_taxonomy", lambda *_a: {"snapshot": "T1"})
    monkeypatch.setattr(cli, "compile_reference_set", lambda *_a: {"reference_set": "R1"})
    monkeypatch.setattr(cli, "list_taxonomy_snapshots", lambda *_a: [{
        "taxonomy_snapshot_id": "T1", "source": "NCBI", "taxonomy_version": "v1",
        "node_count": 2, "status": "active", "source_sha256": "abc", "imported_at": "now",
    }])
    monkeypatch.setattr(cli, "list_reference_sets", lambda *_a: [{
        "reference_set_id": "R1", "taxonomy_version": "v1", "profile_name": "p",
        "family_count": 1, "genus_count": 2, "tsv_sha256": "def", "compiled_at": "now",
    }])

    assert cli._cmd_taxonomy(ns(
        taxonomy_command="import", input=Path("taxdump.tar.gz"), version="v1"
    ), project, db) == 0
    assert cli._cmd_taxonomy(ns(
        taxonomy_command="compile", profile="p", taxonomy_version="v1"
    ), project, db) == 0
    assert cli._cmd_taxonomy(ns(taxonomy_command="list"), project, db) == 0
    assert cli._cmd_taxonomy(ns(taxonomy_command="reference-sets"), project, db) == 0
    with pytest.raises(ValidationError, match="unknown taxonomy command"):
        cli._cmd_taxonomy(ns(taxonomy_command="other"), project, db)
    output = capsys.readouterr().out
    assert "T1" in output and "R1" in output

    for kind, handler in (("qc", "_cmd_qc_table"), ("decisions", "_cmd_decisions"),
                          ("analysis", "_cmd_analysis_results")):
        routed = []
        monkeypatch.setattr(cli, handler, lambda *_a, routed=routed: routed.append(kind) or 7)
        assert cli._cmd_report(ns(report_kind=kind), project, db) == 7
        assert routed == [kind]


def test_interactive_table_import_conflict_and_confirmation_paths(project_db, monkeypatch):
    project, db = project_db
    schema = object()
    preview = {
        "items": [{"key": ("ORG_1",), "action": "update", "differences": ["name"]}],
        "insert": 0,
        "update": 1,
        "unchanged": 0,
    }
    monkeypatch.setattr(cli.Schema, "from_file", lambda *_a: schema)
    monkeypatch.setattr(cli, "preview_table_import", lambda *_a: preview)
    applied = []
    monkeypatch.setattr(
        cli,
        "apply_table_import",
        lambda *_a, **kwargs: applied.append(kwargs["on_conflict"]) or {"updated": 1},
    )

    def args(**overrides):
        values = dict(
            import_kind="table", template=None, table="organisms", file=Path("rows.tsv"),
            on_conflict=None, yes=False,
        )
        values.update(overrides)
        return ns(**values)

    with pytest.raises(ValidationError, match="on-conflict is required"):
        cli._cmd_import(args(yes=True), project, db)
    with pytest.raises(ValidationError, match="existing rows would change"):
        cli._cmd_import(args(), project, db)

    class TTY:
        def __init__(self, interactive):
            self.interactive = interactive

        def isatty(self):
            return self.interactive

        def write(self, value):
            return len(value)

        def flush(self):
            pass

    class Prompt:
        def __init__(self, answer):
            self.answer = answer

        def ask(self):
            return self.answer

    answers = {"select": "cancel", "confirm": False}
    questionary = SimpleNamespace(
        Choice=lambda label, value: (label, value),
        select=lambda *_a, **_k: Prompt(answers["select"]),
        confirm=lambda *_a, **_k: Prompt(answers["confirm"]),
    )
    monkeypatch.setitem(sys.modules, "questionary", questionary)
    monkeypatch.setattr(cli.sys, "stdin", TTY(True))
    monkeypatch.setattr(cli.sys, "stdout", TTY(True))
    assert cli._cmd_import(args(), project, db) == 0

    answers.update(select="update", confirm=False)
    assert cli._cmd_import(args(), project, db) == 0
    answers.update(select="skip", confirm=True)
    assert cli._cmd_import(args(), project, db) == 0
    assert applied == ["skip"]

    preview["update"] = 0
    preview["items"] = []
    monkeypatch.setattr(cli.sys, "stdin", TTY(False))
    with pytest.raises(ValidationError, match="requires --yes"):
        cli._cmd_import(args(), project, db)


def test_remaining_cli_boolean_branches(project_db, monkeypatch, capsys, tmp_path):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Example"})
    db.insert_row("accessions", {
        "internal_type": "organism", "internal_id": "ORG_000001",
        "namespace": "NCBI_Taxonomy", "accession": "123", "is_primary": 1,
    })

    monkeypatch.setattr(cli, "print_qc_table", lambda *_a: "qc")
    assert cli._cmd_qc_table(ns(entity_type=None, entity_id=None, export=False), project, db) == 0

    monkeypatch.setattr("operon.ncbi_reconcile.apply_ncbi_reconciliation", lambda *_a, **_k: {"ok": True})
    assert cli._cmd_ncbi_reconcile(ns(apply=True, actor=None), project, db) == 0

    assert cli._cmd_locations(ns(file_id=["missing"]), project, db) == 0
    assert cli._cmd_show(ns(identifier="ORG_000001", json=False), db) == 0
    assert "NCBI_Taxonomy:123" in capsys.readouterr().out

    monkeypatch.setattr(cli, "_cmd_init", lambda _args: (_ for _ in ()).throw(OSError("disk")))
    assert main(["init", str(tmp_path / "other")]) == 1
