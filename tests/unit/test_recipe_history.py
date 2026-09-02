"""Recipe versioning, recipe/profile snapshots, and the history CLI."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from operon import tools
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ValidationError
from operon.files import ingest_file
from operon.rules import evaluate_entity


def _config(recipe_raw: dict) -> dict:
    return {"tools": {"tool": {"executable": "exe", "run_method": "", "recipes": {"a": recipe_raw}}}}


def _fake_project(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        root=tmp_path,
        tools_config_path=tmp_path / "tools.yaml",
        logs_root=tmp_path / "logs",
        analysis_root=tmp_path / "analysis",
    )


def test_recipe_version_defaults_and_explicit(tmp_path, monkeypatch):
    project = _fake_project(tmp_path)
    monkeypatch.setattr(tools, "load_tools_config", lambda _p: _config({"format": "fasta"}))
    assert tools.get_recipe(project, "a").version == 1
    monkeypatch.setattr(tools, "load_tools_config",
                        lambda _p: _config({"format": "fasta", "version": 3}))
    assert tools.get_recipe(project, "a").version == 3


@pytest.mark.parametrize("version", [0, -2, "3", 1.5, True])
def test_recipe_version_must_be_a_positive_integer(tmp_path, monkeypatch, version):
    project = _fake_project(tmp_path)
    monkeypatch.setattr(tools, "load_tools_config",
                        lambda _p: _config({"format": "fasta", "version": version}))
    with pytest.raises(ValidationError, match="version must be a positive integer"):
        tools.get_recipe(project, "a")


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
    genome = tmp_path / "genome.fa"
    genome.write_text(">ctg1\n" + "ACGT" * 500 + "\n", encoding="utf-8")
    file_row = ingest_file(db, project, genome, "assembly", "ASM_000001", "genome_fasta")
    try:
        yield project, db, file_row
    finally:
        db.close()


def test_record_recipe_is_content_addressed(project_db):
    _project, db, _file_row = project_db
    first = db.record_recipe("r", 1, {"recipe": {"format": "fasta"}, "tool": {"executable": "x"}})
    again = db.record_recipe("r", 1, {"tool": {"executable": "x"}, "recipe": {"format": "fasta"}})
    assert again == first
    changed = db.record_recipe("r", 1, {"recipe": {"format": "tsv"}, "tool": {"executable": "x"}})
    assert changed != first
    bumped = db.record_recipe("r", 2, {"recipe": {"format": "tsv"}, "tool": {"executable": "x"}})
    assert bumped not in {first, changed}
    assert db.conn.execute("SELECT COUNT(*) AS n FROM recipe_snapshots").fetchone()["n"] == 3


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
    return script


def _write_tools_yaml(project, description: str, version: int | None = None) -> None:
    import yaml
    recipe = {
        "entity_type": "assembly",
        "file_role": "genome_fasta",
        "format": "fasta",
        "output_subdir": "fake",
        "output_suffix": ".out.tsv",
        "arguments": ["--out", "${output}"],
        "result_parser": "none",
    }
    if version is not None:
        recipe["version"] = version
    document = {
        "version": 1,
        "tools": {
            "faketool": {
                "description": description,
                "executable": str(project.root / "faketool.py"),
                "run_method": sys.executable,
                "version_args": ["-version"],
                "version_pattern": r"faketool:\s*([^\s]+)",
                "recipes": {"fake_recipe": recipe},
            },
        },
    }
    project.tools_config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_analyze_records_recipe_snapshot(project_db, tmp_path):
    project, db, _file_row = project_db
    _write_fake_tool(project)
    _write_tools_yaml(project, "v1 config")
    assert main(["--project", str(tmp_path), "analyze", "--analysis", "fake_recipe"]) == 0

    jobs = [dict(row) for row in db.conn.execute("SELECT * FROM analysis_jobs").fetchall()]
    assert len(jobs) == 1
    snapshot_id = jobs[0]["recipe_snapshot_id"]
    assert snapshot_id is not None
    snapshot = db.conn.execute(
        "SELECT * FROM recipe_snapshots WHERE recipe_snapshot_id=?", (snapshot_id,)
    ).fetchone()
    assert snapshot["recipe_name"] == "fake_recipe"
    assert snapshot["recipe_version"] == 1
    document = json.loads(snapshot["recipe_document"])
    assert set(document) == {"recipe", "tool"}
    assert document["recipe"]["file_role"] == "genome_fasta"
    assert document["tool"]["executable"].endswith("faketool.py")

    # Editing the tool spec (not just the recipe) produces a new snapshot,
    # even when the run itself is served from the cache.
    _write_tools_yaml(project, "v2 config", version=2)
    assert main(["--project", str(tmp_path), "analyze", "--analysis", "fake_recipe"]) == 0
    snapshots = [dict(row) for row in db.conn.execute(
        "SELECT * FROM recipe_snapshots ORDER BY recipe_snapshot_id").fetchall()]
    assert len(snapshots) == 2
    assert snapshots[1]["recipe_version"] == 2

    # A forced re-run references the new snapshot.
    assert main(["--project", str(tmp_path), "analyze", "--analysis", "fake_recipe", "--force"]) == 0
    latest = db.conn.execute(
        "SELECT * FROM analysis_jobs WHERE status='completed' ORDER BY job_id DESC LIMIT 1"
    ).fetchone()
    assert latest["recipe_snapshot_id"] == snapshots[1]["recipe_snapshot_id"]


def test_recipes_list_history_show_cli(project_db, tmp_path, capsys):
    project, db, _file_row = project_db
    _write_fake_tool(project)
    _write_tools_yaml(project, "v1 config", version=4)

    assert main(["--project", str(tmp_path), "recipes", "list"]) == 0
    out = capsys.readouterr().out
    assert "fake_recipe" in out and "faketool" in out

    # No snapshots yet: history/show report the empty state.
    assert main(["--project", str(tmp_path), "recipes", "history", "fake_recipe"]) == 0
    assert "no snapshots" in capsys.readouterr().out
    assert main(["--project", str(tmp_path), "recipes", "show", "fake_recipe"]) == 2

    assert main(["--project", str(tmp_path), "analyze", "--analysis", "fake_recipe"]) == 0
    capsys.readouterr()
    assert main(["--project", str(tmp_path), "recipes", "history", "fake_recipe"]) == 0
    out = capsys.readouterr().out
    assert "snapshot_id" in out and "jobs" in out

    assert main(["--project", str(tmp_path), "recipes", "show", "fake_recipe"]) == 0
    out = capsys.readouterr().out
    assert "recipe:" in out and "tool:" in out and "file_role: genome_fasta" in out

    snapshot_id = db.conn.execute(
        "SELECT recipe_snapshot_id FROM recipe_snapshots LIMIT 1").fetchone()["recipe_snapshot_id"]
    assert main(["--project", str(tmp_path), "recipes", "show", "fake_recipe",
                 "--snapshot-id", str(snapshot_id)]) == 0
    assert "fake_recipe" not in capsys.readouterr().err
    assert main(["--project", str(tmp_path), "recipes", "show", "fake_recipe",
                 "--snapshot-id", "9999"]) == 2


def test_profiles_history_show_cli(project_db, tmp_path, capsys):
    project, db, _file_row = project_db

    # Empty states.
    assert main(["--project", str(tmp_path), "profiles", "history"]) == 0
    assert "no profile snapshots" in capsys.readouterr().out

    # Evaluating records a content-addressed profile snapshot.
    evaluate_entity(db, project, "assembly", "ASM_000001")
    profile_name = project.config["qc"]["default_profile"]

    assert main(["--project", str(tmp_path), "profiles", "history"]) == 0
    assert profile_name in capsys.readouterr().out

    assert main(["--project", str(tmp_path), "profiles", "history", profile_name]) == 0
    out = capsys.readouterr().out
    assert "snapshot_id" in out and "decisions" in out

    assert main(["--project", str(tmp_path), "profiles", "show", profile_name]) == 0
    out = capsys.readouterr().out
    assert "kind: qc" in out or "version:" in out

    assert main(["--project", str(tmp_path), "profiles", "history", "missing_profile"]) == 0
    assert "no snapshots" in capsys.readouterr().out
    assert main(["--project", str(tmp_path), "profiles", "show", "missing_profile"]) == 2
