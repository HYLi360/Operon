"""Exercise standalone toy producers/consumers through the real CLI/core loop."""

import json
import shlex
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from operon.cli import main
from operon.database import Database
from operon.demo import init_demo
from operon.execution import LocalExecutor
from operon.tools import run_analysis
from operon.tools._presets import add_preset
from operon.workflow import run_external_command

SAMPLES = Path(__file__).resolve().parents[2] / "examples" / "plugins"


def argv(file, input_path, output):
    return [
        sys.executable,
        str(SAMPLES / "toy_analysis.py"),
        "--input",
        str(input_path),
        "--out",
        str(output),
        "--file-id",
        file["file_id"],
        "--entity-type",
        file["entity_type"],
        "--entity-id",
        file["entity_id"],
    ]


def test_standalone_plugin_contract_full_file_loop(tmp_path):
    project = init_demo(tmp_path / "project")
    out = project.analysis_root / "toy.json"
    with closing(Database(project.db_path)) as db:
        file = dict(
            db.conn.execute(
                "SELECT * FROM files WHERE entity_type='assembly' ORDER BY file_id LIMIT 1"
            ).fetchone()
        )
        input_path = project.root / file["relative_path"]
        command = argv(file, input_path, out)
        result = run_external_command(
            db,
            project,
            command,
            step="toy_contract",
            entity_type=file["entity_type"],
            entity_id=file["entity_id"],
            inputs=[input_path],
            expected_outputs=[out],
            events=str(out) + ".events.jsonl",
            tool="toy-analysis",
            tool_version="1",
        )
        assert result["status"] == "completed"
        assert result["input_sha256"]
    base = ["--project", str(project.root)]
    draft = tmp_path / "adopt.json"
    assert (
        main(
            [
                *base,
                "import-events",
                "--run",
                result["run_id"],
                "--file",
                str(out) + ".events.jsonl",
                "--out",
                str(draft),
            ]
        )
        == 0
    )
    assert main([*base, "adopt", "--from-manifest", str(draft)]) == 0
    assert main([*base, "import-qc", "--file", str(out) + ".qc.tsv"]) == 0
    with closing(Database(project.db_path)) as db:
        adopted = db.conn.execute(
            "SELECT * FROM files WHERE file_role='toy_copy'"
        ).fetchone()
        assert adopted["sha256"] == file["sha256"]
        assert adopted["size_bytes"] == file["size_bytes"]
        lineage = db.conn.execute(
            "SELECT * FROM file_lineage WHERE derived_file_id=?", (adopted["file_id"],)
        ).fetchone()
        assert lineage["input_file_id"] == file["file_id"]
        assert lineage["workflow_run_id"] == result["run_id"]
        assert db.conn.execute(
            "SELECT 1 FROM workflow_runs WHERE step='import-events' AND parent_run_id=?",
            (result["run_id"],),
        ).fetchone()
        assert db.conn.execute(
            "SELECT 1 FROM changes WHERE object_id=? AND field='event_imports'",
            (result["run_id"],),
        ).fetchone()
        assert db.conn.execute(
            "SELECT 1 FROM qc_results WHERE tool='toy-analysis'"
        ).fetchone()
    bundle = tmp_path / "bundle"
    assert main([*base, "report", "view", "--out", str(bundle)]) == 0
    svg = tmp_path / "view.svg"
    before = project.db_path.read_bytes()
    subprocess.run(
        [
            sys.executable,
            str(SAMPLES / "toy_viz.py"),
            "--bundle",
            str(bundle),
            "--out",
            str(svg),
        ],
        check=True,
    )
    assert "<svg " in svg.read_text()
    assert project.db_path.read_bytes() == before
    (bundle / "entities.tsv").write_text("tampered")
    bad = subprocess.run(
        [
            sys.executable,
            str(SAMPLES / "toy_viz.py"),
            "--bundle",
            str(bundle),
            "--out",
            str(svg),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 2
    assert "checksum mismatch" in bad.stderr


@pytest.mark.parametrize("array", [False, True])
def test_recipe_plugin_events_and_hits_loop(tmp_path, monkeypatch, array):
    project = init_demo(tmp_path / "project")
    preset = tmp_path / "preset.yaml"
    template = "${output}.${file_id}.events.jsonl"
    preset.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {
                    "toy_analysis": {
                        "executable": sys.executable,
                        "run_method": "",
                        "version_args": [str(SAMPLES / "toy_analysis.py"), "--version"],
                        "recipes": {
                            "toy_analysis": {
                                "version": 1,
                                "entity_type": "assembly",
                                "file_role": "genome_fasta",
                                "format": "fasta",
                                "output_suffix": ".json",
                                "result_parser": "plugin:toy-analysis",
                                "arguments": [
                                    str(SAMPLES / "toy_analysis.py"),
                                    "--input",
                                    "${input}",
                                    "--out",
                                    "${output}",
                                    "--file-id",
                                    "${file_id}",
                                    "--entity-type",
                                    "${entity_type}",
                                    "--entity-id",
                                    "${entity_id}",
                                    "--events",
                                    template,
                                ],
                            }
                        },
                    }
                },
            }
        )
    )
    if array:

        class ToyArrayExecutor(LocalExecutor):
            name = "slurm"
            slurm = SimpleNamespace(array=True, array_concurrency=2)

            def run_array(self, tasks, *, cwd, threads, array_concurrency):
                assert array_concurrency == 2
                return [
                    self.run(
                        shlex.split(task["command"]),
                        cwd=cwd,
                        stdout_path=task["stdout_path"],
                        stderr_path=task["stderr_path"],
                        run_id=task["run_id"],
                        threads=threads,
                    )
                    for task in tasks
                ]

        monkeypatch.setattr(
            "operon.execution.get_executor", lambda *a, **k: ToyArrayExecutor()
        )
    with closing(Database(project.db_path)) as db:
        add_preset(project, db, preset)
        results = run_analysis(project, db, "toy_analysis", limit=2, events=template)
        assert len(results) == 2
        assert all(result["status"] == "completed" for result in results)
        for result in results:
            file = dict(
                db.conn.execute(
                    "SELECT * FROM files WHERE file_id=?", (result["file_id"],)
                ).fetchone()
            )
            job = db.conn.execute(
                "SELECT * FROM analysis_jobs WHERE job_id=?", (result["job_id"],)
            ).fetchone()
            parent = db.conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?", (job["workflow_run_id"],)
            ).fetchone()
            events = json.loads(parent["execution_details"])["events_path"]
            assert Path(events).is_file()
            assert (
                main(
                    [
                        "--project",
                        str(project.root),
                        "import-events",
                        "--run",
                        parent["run_id"],
                        "--file",
                        events,
                    ]
                )
                == 0
            )
            payload = tmp_path / f"hits-{result['job_id']}.json"
            subprocess.run(
                [
                    *argv(file, project.root / file["relative_path"], payload),
                    "--job-id",
                    str(result["job_id"]),
                ],
                check=True,
            )
            assert (
                main(
                    [
                        "--project",
                        str(project.root),
                        "import-hits",
                        "--file",
                        str(payload),
                    ]
                )
                == 0
            )
            assert db.conn.execute(
                "SELECT metric_value FROM analysis_results WHERE job_id=?",
                (result["job_id"],),
            ).fetchone()


def test_bundle_snapshot_ignores_a_concurrent_writer(tmp_path, monkeypatch):
    import operon.view_bundle as bundles
    from operon.utils import now_iso

    project = init_demo(tmp_path / "project")
    original = bundles.write_tsv

    def write_after_first_read(path, columns, rows):
        original(path, columns, rows)
        if path.name == "entities.tsv":
            with closing(Database(project.db_path)) as writer:
                writer.insert_qc_result(
                    {
                        "entity_type": "assembly",
                        "entity_id": "ASM_000001",
                        "qc_stage": "toy_snapshot",
                        "metric_name": "later_metric",
                        "metric_value": "1",
                        "metric_numeric": 1,
                        "tool": "toy",
                        "tool_version": "1",
                        "parameter_set": "snapshot",
                        "evaluated_at": now_iso(),
                    }
                )

    monkeypatch.setattr(bundles, "write_tsv", write_after_first_read)
    out = tmp_path / "view"
    with closing(Database(project.db_path, read_only=True)) as reader:
        bundles.export_view_bundle(reader, out)
        assert "later_metric" not in (out / "qc_wide.tsv").read_text()
        assert "later_metric" in bundles.render_qc_report(reader, "tsv", wide=True)
