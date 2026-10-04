"""Event schema, deduplication, draft publication and workflow attribution."""

import json
import shlex
import sys
from contextlib import closing
from pathlib import Path

import pytest

from operon.cli import main
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ConflictError, EntityNotFoundError, ValidationError
from operon.events import import_events, plan_events_import
from operon.workflow import log_run, run_external_command


@pytest.fixture
def setup(tmp_path):
    project = init_demo(tmp_path / "project")
    db = Database(project.db_path)
    file = dict(
        db.conn.execute(
            "SELECT * FROM files WHERE entity_type='assembly' LIMIT 1"
        ).fetchone()
    )
    log_run(
        db,
        project,
        {
            "run_id": "WF_EVENTS",
            "step": "toy",
            "status": "completed",
            "entity_type": file["entity_type"],
            "entity_id": file["entity_id"],
            "execution_details": json.dumps({"keep": "untouched"}),
        },
    )
    source = tmp_path / "events.jsonl"
    events = [
        {
            "schema_version": 1,
            "event_id": "m1",
            "type": "metric",
            "metric": {
                "entity_type": file["entity_type"],
                "entity_id": file["entity_id"],
                "qc_stage": "toy_events",
                "metric_name": "toy_length",
                "metric_value": "4",
                "metric_numeric": 4,
                "tool": "toy",
                "tool_version": "1",
                "parameter_set": "toy",
                "file": {k: file[k] for k in ("file_id", "sha256", "size_bytes")},
            },
        },
        {
            "schema_version": 1,
            "event_id": "a1",
            "type": "artifact",
            "artifact": {
                "entity_type": file["entity_type"],
                "entity_id": file["entity_id"],
                "path": "analysis/toy.fa",
                "role": "toy_result",
                "format": "fasta",
                "derived_from": [file["file_id"]],
            },
        },
        {"schema_version": 1, "event_id": "u1", "type": "future", "data": "anything"},
    ]
    source.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    yield project, db, source, events
    db.close()


def counts(db):
    return tuple(
        db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ["qc_results", "changes", "workflow_runs", "files"]
    )


def test_events_preview_dedup_and_draft_reconstruction(setup, tmp_path):
    project, db, source, events = setup
    out = tmp_path / "adopt.json"
    before = counts(db)
    with closing(Database(project.db_path, read_only=True)) as preview:
        assert (
            plan_events_import(preview, project, "WF_EVENTS", source, output=out)[
                "skipped"
            ]
            == 1
        )
    assert counts(db) == before
    assert not out.exists()
    result = import_events(db, project, "WF_EVENTS", source, output=out)
    assert result == {
        "imported": 3,
        "skipped": 1,
        "duplicates": 0,
        "metric_count": 1,
        "draft": str(out),
    }
    artifact = json.loads(out.read_text())[0]
    assert artifact["workflow_run_id"] == "WF_EVENTS"
    assert artifact["derived_from"] == events[1]["artifact"]["derived_from"]
    assert counts(db)[3] == before[3]
    after = counts(db)
    bytes_before = out.read_bytes()
    assert (
        import_events(db, project, "WF_EVENTS", source, output=out)["duplicates"] == 3
    )
    assert counts(db) == after
    out.unlink()
    import_events(db, project, "WF_EVENTS", source, output=out)
    assert out.read_bytes() == bytes_before
    details = json.loads(
        db.conn.execute(
            "SELECT execution_details FROM workflow_runs WHERE run_id='WF_EVENTS'"
        ).fetchone()[0]
    )
    assert details["keep"] == "untouched"
    assert (
        main(
            [
                "--project",
                str(project.root),
                "import-events",
                "--run",
                "WF_EVENTS",
                "--file",
                str(source),
                "--dry-run",
            ]
        )
        == 0
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda e: e[-1].update(schema_version=2),
        lambda e: e[0].update(schema_version=True),
        lambda e: e[0]["metric"].pop("metric_name"),
        lambda e: e[0]["metric"].update(decision="ACCEPTED"),
        lambda e: e[0]["metric"]["file"].update(sha256="0" * 64),
        lambda e: e[0]["metric"].update(metric_numeric=float("nan")),
        lambda e: e[1]["artifact"].update(workflow_run_id="other"),
        lambda e: e[1]["artifact"].update(derived_from=["missing"]),
        lambda e: e[0]["metric"].update(entity_id="missing"),
    ],
)
def test_invalid_later_event_writes_nothing(setup, tmp_path, change):
    project, db, source, events = setup
    before = counts(db)
    change(events)
    source.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    with pytest.raises((ValidationError, EntityNotFoundError)):
        import_events(db, project, "WF_EVENTS", source, output=tmp_path / "adopt.json")
    assert counts(db) == before
    assert not (tmp_path / "adopt.json").exists()


def test_event_conflicts_and_draft_publication_failure(setup, tmp_path, monkeypatch):
    import operon.events as module

    project, db, source, events = setup
    before = counts(db)
    out = tmp_path / "adopt.json"
    with monkeypatch.context() as patch:
        patch.setattr(
            module,
            "atomic_write_text",
            lambda *a: (_ for _ in ()).throw(OSError("disk full")),
        )
        with pytest.raises(OSError):
            import_events(db, project, "WF_EVENTS", source, output=out)
    assert counts(db) == before
    assert not out.exists()
    import_events(db, project, "WF_EVENTS", source, output=out)
    before = counts(db)
    saved = out.read_bytes()
    events[0]["metric"]["metric_value"] = "different"
    source.write_text("\n".join(json.dumps(e) for e in events))
    with pytest.raises(ConflictError):
        import_events(db, project, "WF_EVENTS", source, output=out)
    assert counts(db) == before
    assert out.read_bytes() == saved


def test_run_external_event_path_and_cli(setup, tmp_path):
    project, db, _source, _events = setup
    out = project.analysis_root / "events.jsonl"
    command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; Path({str(out)!r}).write_text('event\\n')",
    ]
    result = run_external_command(db, project, command, step="toy_sidecar", events=out)
    assert json.loads(result["execution_details"])["events_path"] == str(out)
    assert result["status"] == "completed"
    assert (
        main(
            [
                "--project",
                str(project.root),
                "run-external",
                "--step",
                "toy_cli",
                "--command",
                shlex.join(command),
                "--events",
                str(out),
            ]
        )
        == 0
    )


def test_analyze_event_sidecar_is_recorded_and_requires_fresh_execution(
    setup, tmp_path
):
    import yaml

    from operon.tools import run_analysis
    from operon.tools._presets import add_preset

    project, db, _source, _events = setup
    script = tmp_path / "producer.py"
    script.write_text(
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('payload')\nPath(sys.argv[2]).write_text('event')\n"
    )
    preset = tmp_path / "preset.yaml"
    preset.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": {
                    "toy_sidecar": {
                        "executable": sys.executable,
                        "run_method": "",
                        "version_args": ["--version"],
                        "recipes": {
                            "toy_sidecar": {
                                "version": 1,
                                "entity_type": "assembly",
                                "file_role": "genome_fasta",
                                "format": "fasta",
                                "output_suffix": ".json",
                                "result_parser": "plugin:toy",
                                "arguments": [
                                    str(script),
                                    "${output}",
                                    "${output}.events.jsonl",
                                ],
                            }
                        },
                    }
                },
            }
        )
    )
    add_preset(project, db, preset)
    legacy = run_analysis(project, db, "toy_sidecar", limit=1)[0]
    first = run_analysis(
        project, db, "toy_sidecar", limit=1, events="${output}.events.jsonl"
    )[0]
    second = run_analysis(
        project, db, "toy_sidecar", limit=1, events="${output}.events.jsonl"
    )[0]
    assert first["job_id"] != legacy["job_id"] != second["job_id"]
    assert first["job_id"] != second["job_id"]
    row = db.conn.execute(
        "SELECT w.execution_details FROM workflow_runs w JOIN analysis_jobs j ON j.workflow_run_id=w.run_id WHERE j.job_id=?",
        (second["job_id"],),
    ).fetchone()
    assert Path(json.loads(row[0])["events_path"]).is_file()
    with pytest.raises(ValidationError, match="multiple analysis inputs"):
        run_analysis(project, db, "toy_sidecar", events="analysis/shared.jsonl")
    for template in ("${unknown}", "../outside.jsonl", "${output}"):
        outcome = run_analysis(project, db, "toy_sidecar", limit=1, events=template)[0]
        assert outcome["status"] == "error"
