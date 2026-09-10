"""Terminal live echo: ``workflow show --follow`` and batch progress lines."""

from __future__ import annotations

import io
import json
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from operon.cli import main
from operon.config import Project
from operon.database import Database
from operon.files import ingest_file
from operon.tools import run_analysis
from operon.workflow import follow_run_logs, log_run, read_log_tail


def _write_run_logs(logs_root: Path, run_id: str, stdout: str = "", stderr: str = "") -> None:
    logs_root.mkdir(parents=True, exist_ok=True)
    (logs_root / f"{run_id}.stdout.log").write_text(stdout, encoding="utf-8")
    (logs_root / f"{run_id}.stderr.log").write_text(stderr, encoding="utf-8")


def test_read_log_tail_incremental_missing_and_truncated(tmp_path):
    log = tmp_path / "x.log"
    assert read_log_tail(log, 0) == ("", 0)
    log.write_text("line1\n", encoding="utf-8")
    text, offset = read_log_tail(log, 0)
    assert text == "line1\n"
    assert read_log_tail(log, offset) == ("", offset)
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("line2\n")
    text, offset = read_log_tail(log, offset)
    assert text == "line2\n"
    log.write_text("new\n", encoding="utf-8")
    text, _offset = read_log_tail(log, offset)
    assert text == "new\n"


def test_follow_finished_run_dumps_logs_and_exits(tmp_path):
    logs = tmp_path / "logs"
    _write_run_logs(logs, "WF_X", stdout="out1\nout2\n", stderr="err1\nerr2\n")
    out = io.StringIO()
    rc = follow_run_logs(
        lambda: {"status": "completed", "exit_code": 0},
        logs, "WF_X", out=out, poll_interval=0.01,
    )
    assert rc == 0
    text = out.getvalue()
    assert "out1\nout2\n" in text
    assert "stderr: err1\nstderr: err2\n" in text
    assert "run WF_X finished: status=completed exit_code=0" in text


def test_follow_failed_run_returns_one(tmp_path):
    logs = tmp_path / "logs"
    _write_run_logs(logs, "WF_F", stdout="partial\n")
    out = io.StringIO()
    rc = follow_run_logs(
        lambda: {"status": "failed", "exit_code": 7},
        logs, "WF_F", out=out, poll_interval=0.01,
    )
    assert rc == 1
    assert "status=failed exit_code=7" in out.getvalue()


def test_follow_missing_run_returns_one(tmp_path):
    out = io.StringIO()
    rc = follow_run_logs(lambda: None, tmp_path / "logs", "WF_GONE",
                         out=out, poll_interval=0.01)
    assert rc == 1
    assert "disappeared" in out.getvalue()


def test_follow_streaming_run_reads_each_line_exactly_once(tmp_path):
    logs = tmp_path / "logs"
    _write_run_logs(logs, "WF_S")
    out = io.StringIO()
    calls = {"n": 0}

    def status() -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            with open(logs / "WF_S.stdout.log", "a", encoding="utf-8") as handle:
                handle.write("first\n")
            return {"status": "running", "exit_code": None}
        if calls["n"] == 2:
            with open(logs / "WF_S.stdout.log", "a", encoding="utf-8") as handle:
                handle.write("second\n")
            with open(logs / "WF_S.stderr.log", "a", encoding="utf-8") as handle:
                handle.write("warn\n")
            return {"status": "running", "exit_code": None}
        return {"status": "completed", "exit_code": 0}

    rc = follow_run_logs(status, logs, "WF_S", out=out, poll_interval=0.01)
    assert rc == 0
    text = out.getvalue()
    assert text.count("first") == 1
    assert text.count("second") == 1
    assert text.count("stderr: warn") == 1
    assert "status=completed" in text


def test_follow_keyboard_interrupt_returns_130_without_touching_run(tmp_path):
    logs = tmp_path / "logs"
    _write_run_logs(logs, "WF_I", stdout="partial\n")
    out = io.StringIO()

    def status() -> dict:
        raise KeyboardInterrupt

    rc = follow_run_logs(status, logs, "WF_I", out=out, poll_interval=0.01)
    assert rc == 130
    text = out.getvalue()
    assert "partial\n" in text
    assert "the run itself keeps going" in text


@pytest.fixture
def project_db(tmp_path: Path):
    project = Project.init(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def test_workflow_show_follow_finished_run_via_cli(project_db, capsys):
    project, db = project_db
    log_run(db, project, {"run_id": "WF_DONE", "step": "selftest", "exit_code": 0})
    _write_run_logs(project.logs_root, "WF_DONE", stdout="hello\n", stderr="oops\n")
    rc = main(["--project", str(project.root), "workflow", "show", "WF_DONE", "--follow"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Workflow run" in out
    assert "hello\n" in out
    assert "stderr: oops\n" in out
    assert "run WF_DONE finished: status=completed exit_code=0" in out


def test_workflow_show_follow_failed_run_exit_code(project_db, capsys):
    project, db = project_db
    log_run(db, project, {"run_id": "WF_BAD", "step": "selftest",
                          "status": "failed", "exit_code": 3, "error": "boom"})
    _write_run_logs(project.logs_root, "WF_BAD", stdout="dying\n")
    rc = main(["--project", str(project.root), "workflow", "show", "WF_BAD", "--follow"])
    assert rc == 1
    assert "status=failed exit_code=3" in capsys.readouterr().out


def test_workflow_show_follow_rejects_json_format(project_db, capsys):
    project, db = project_db
    log_run(db, project, {"run_id": "WF_J", "step": "selftest"})
    rc = main(["--project", str(project.root), "workflow", "show", "WF_J",
               "--follow", "--format", "json"])
    assert rc == 2
    assert "--follow cannot be combined with --format json" in capsys.readouterr().err


@pytest.fixture
def assembly_project(project_db):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
    genome = project.root / "genome.fa"
    genome.write_text(">ctg1\n" + "ACGT" * 500 + "\n", encoding="utf-8")
    file_row = ingest_file(db, project, genome, "assembly", "ASM_000001", "genome_fasta")
    return project, db, file_row


def test_qc_cli_progress_lines(assembly_project, capsys):
    project, _db, file_row = assembly_project
    rc = main(["--project", str(project.root), "qc"])
    assert rc == 0
    assert f"[1/1] {file_row['file_id']}: OK" in capsys.readouterr().out


def _write_fake_tool(project) -> None:
    script = project.root / "faketool.py"
    script.write_text(textwrap.dedent("""
        import sys
        args = sys.argv[1:]
        if '-version' in args:
            print('faketool: 1.0.0')
            raise SystemExit(0)
        out = args[args.index('--out') + 1]
        with open(out, 'w') as handle:
            handle.write('done\\n')
    """).strip(), encoding="utf-8")
    document = {
        "version": 1,
        "tools": {
            "faketool": {
                "executable": str(script),
                "run_method": sys.executable,
                "version_args": ["-version"],
                "version_pattern": r"faketool:\s*([^\s]+)",
                "recipes": {
                    "fake_recipe": {
                        "entity_type": "assembly",
                        "file_role": "genome_fasta",
                        "format": "fasta",
                        "output_subdir": "fake",
                        "output_suffix": ".out.tsv",
                        "arguments": ["--out", "${output}"],
                        "result_parser": "none",
                    },
                },
            },
        },
    }
    project.tools_config_path.write_text(yaml.safe_dump(document, sort_keys=False),
                                         encoding="utf-8")


def test_run_analysis_progress_callback_phases(assembly_project):
    project, db, file_row = assembly_project
    _write_fake_tool(project)
    events: list[tuple[int, int, str, str]] = []
    results = run_analysis(
        project, db, "fake_recipe",
        progress_callback=lambda i, t, fid, phase: events.append((i, t, fid, phase)),
    )
    assert results[0]["status"] == "completed"
    file_id = file_row["file_id"]
    assert events == [(1, 1, file_id, "start"), (1, 1, file_id, "completed")]

    events.clear()
    results = run_analysis(
        project, db, "fake_recipe",
        progress_callback=lambda i, t, fid, phase: events.append((i, t, fid, phase)),
    )
    assert results[0]["status"] == "cached"
    assert events == [(1, 1, file_id, "start"), (1, 1, file_id, "cached")]


def test_analyze_cli_progress_lines(assembly_project, capsys):
    project, _db, file_row = assembly_project
    _write_fake_tool(project)
    rc = main(["--project", str(project.root), "analyze", "--analysis", "fake_recipe"])
    assert rc == 0
    out = capsys.readouterr().out
    assert f"[1/1] {file_row['file_id']}: running" in out
    assert f"[1/1] {file_row['file_id']}: done" in out


def test_run_external_prints_run_id_and_watch_hint(project_db, capsys):
    project, _db = project_db
    rc = main(["--project", str(project.root), "run-external",
               "--step", "selftest", "--command", "true"])
    assert rc == 0
    out = capsys.readouterr().out
    first_line, record_line = out.strip().splitlines()[0], out.strip().splitlines()[-1]
    assert first_line.startswith("run WF_")
    assert ".stdout.log" in first_line and ".stderr.log" in first_line
    assert "watch: operon workflow show" in first_line and "--follow" in first_line
    run_id = first_line.split()[1].rstrip(":")
    assert json.loads(record_line)["run_id"] == run_id
    assert (project.logs_root / f"{run_id}.stdout.log").exists()
