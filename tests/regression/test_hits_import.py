"""Independent evidence identity, atomicity, conflicts and unchanged values."""

import copy
import json
from contextlib import closing

import pytest

from operon.cli import main
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ConflictError, ValidationError
from operon.hits_import import import_hits, plan_hits_import
from operon.workflow import log_run


@pytest.fixture
def setup(tmp_path):
    project = init_demo(tmp_path / "project")
    db = Database(project.db_path)
    file = dict(
        db.conn.execute(
            "SELECT * FROM files WHERE entity_type='assembly' LIMIT 1"
        ).fetchone()
    )
    parent = log_run(
        db, project, {"run_id": "WF_TOY", "step": "toy", "status": "completed"}
    )
    job = {
        "analysis_name": "toy",
        "entity_type": file["entity_type"],
        "entity_id": file["entity_id"],
        "file_id": file["file_id"],
        "tool": "toy",
        "tool_version": "1",
        "parameter_set": "toy",
        "parameter_sha256": "toy",
        "input_sha256": file["sha256"],
        "database_identity": "none",
        "status": "completed",
        "started_at": parent["started_at"],
        "workflow_run_id": parent["run_id"],
    }
    db.insert_row("analysis_jobs", job)
    job_id = db.conn.execute(
        "SELECT job_id FROM analysis_jobs WHERE analysis_name='toy'"
    ).fetchone()[0]
    source = tmp_path / "hits.json"
    payload = {
        "schema_version": 1,
        "job_id": job_id,
        "file": {c: file[c] for c in ("file_id", "sha256", "size_bytes")},
        "results": [
            {"metric_name": "query_count", "metric_value": "99", "metric_numeric": 99}
        ],
        "hits": [
            {
                "query_id": "q1",
                "subject_id": "s1",
                "hit_rank": 7,
                "metric_name": "evalue",
                "metric_value": "1e-5",
                "metric_numeric": 0.00001,
            }
        ],
        "alignments": [
            {
                "query_id": "q1",
                "subject_id": "s1",
                "hit_rank": 7,
                "query_start": 12,
                "query_end": 1,
                "extra": {"source": "toy"},
            }
        ],
    }
    source.write_text(json.dumps(payload))
    yield project, db, source, payload
    db.close()


def counts(db):
    return tuple(
        db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in [
            "analysis_results",
            "analysis_hits",
            "analysis_alignments",
            "qc_results",
            "changes",
            "workflow_runs",
        ]
    )


def test_hits_import_preview_identity_and_idempotency(setup):
    project, db, source, payload = setup
    before = counts(db)
    with closing(Database(project.db_path, read_only=True)) as preview:
        assert plan_hits_import(preview, source)["hit_count"] == 1
    assert counts(db) == before
    result = import_hits(db, project, source)
    assert result["unchanged"] is False
    row = db.conn.execute(
        "SELECT * FROM analysis_hits WHERE job_id=?", (payload["job_id"],)
    ).fetchone()
    assert row["hit_rank"] == 7
    assert row["metric_value"] == "1e-5"
    summary = db.conn.execute(
        "SELECT metric_value FROM analysis_results WHERE job_id=?", (payload["job_id"],)
    ).fetchone()
    assert summary[0] == "99"
    assert counts(db)[3] == before[3]
    after = counts(db)
    assert import_hits(db, project, source)["unchanged"] is True
    assert counts(db) == after
    child = db.conn.execute(
        "SELECT parent_run_id FROM workflow_runs WHERE run_id=?", (result["run_id"],)
    ).fetchone()
    assert child[0] == "WF_TOY"
    assert (
        main(
            [
                "--project",
                str(project.root),
                "import-hits",
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
        lambda p: p.update(schema_version=2),
        lambda p: p.update(job_id=999),
        lambda p: p["file"].update(sha256="0" * 64),
        lambda p: p["file"].update(file_id="missing"),
        lambda p: p["file"].update(size_bytes=0),
        lambda p: p["hits"][0].update(hit_rank=0),
        lambda p: p["hits"].append(copy.deepcopy(p["hits"][0])),
        lambda p: p["alignments"][0].update(subject_id="other"),
        lambda p: p["alignments"][0].update(query_start=1.5),
        lambda p: p["hits"][0].update(metric_numeric=float("inf")),
        lambda p: p["results"][0].update(state="ACCEPTED"),
    ],
)
def test_invalid_hits_write_nothing(setup, change):
    project, db, source, payload = setup
    before = counts(db)
    change(payload)
    source.write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        import_hits(db, project, source)
    assert counts(db) == before


def test_conflicts_and_transaction_rollback(setup, monkeypatch):
    project, db, source, payload = setup
    before = counts(db)
    with monkeypatch.context() as patch:
        patch.setattr(
            db,
            "record_change",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit fail")),
        )
        with pytest.raises(RuntimeError, match="audit fail"):
            import_hits(db, project, source)
    assert counts(db) == before
    import_hits(db, project, source)
    before = counts(db)
    payload["results"][0]["metric_value"] = "different"
    source.write_text(json.dumps(payload))
    with pytest.raises(ConflictError):
        import_hits(db, project, source)
    assert counts(db) == before


def test_empty_evidence_is_idempotent(setup):
    project, db, source, payload = setup
    payload.update(results=[], hits=[], alignments=[])
    source.write_text(json.dumps(payload))
    import_hits(db, project, source)
    before = counts(db)
    assert import_hits(db, project, source)["unchanged"]
    assert counts(db) == before


def test_plugin_parser_declaration_preserves_existing_evidence(setup, monkeypatch):
    from types import SimpleNamespace

    from operon.tools import _results

    project, db, source, payload = setup
    import_hits(db, project, source)
    before = counts(db)
    monkeypatch.setattr(
        _results,
        "parse_hits",
        lambda *a: (_ for _ in ()).throw(AssertionError("core parser called")),
    )
    recipe = SimpleNamespace(name="toy", result_parser="plugin:toy")
    assert _results.parse_and_store_results(
        db, project, recipe, None, "", {}, payload["job_id"], source, ""
    ) == (0, 0, 0, 0, 0)
    assert counts(db) == before
    recipe.result_parser = "plugin:"
    with pytest.raises(ValidationError, match="requires a name"):
        _results.parse_and_store_results(
            db, project, recipe, None, "", {}, payload["job_id"], source, ""
        )


@pytest.mark.bug("ODR-59")
def test_hits_payload_rejects_job_id_outside_sqlite_integer_range(setup):
    project, db, source, payload = setup
    before = counts(db)
    payload["job_id"] = 1 << 80
    source.write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        import_hits(db, project, source)
    assert counts(db) == before


@pytest.mark.bug("ODR-59")
def test_real_metrics_accept_finite_numbers_outside_integer_range(setup):
    project, db, source, payload = setup
    payload["hits"][0]["metric_numeric"] = 1 << 80
    source.write_text(json.dumps(payload))
    import_hits(db, project, source)
    stored = db.conn.execute(
        "SELECT metric_numeric FROM analysis_hits WHERE job_id=?", (payload["job_id"],)
    ).fetchone()[0]
    assert stored == float(1 << 80)


@pytest.mark.bug("ODR-59")
def test_unrepresentable_real_metric_is_rejected(setup):
    project, db, source, payload = setup
    before = counts(db)
    payload["hits"][0]["metric_numeric"] = 10**400
    source.write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        import_hits(db, project, source)
    assert counts(db) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "failed"),
        ("workflow_run_id", "missing"),
        ("input_sha256", "wrong"),
        ("entity_id", "other"),
    ],
)
def test_job_validation_before_evidence_write(setup, field, value):
    project, db, source, payload = setup
    with db.transaction():
        db.conn.execute(
            f"UPDATE analysis_jobs SET {field}=? WHERE job_id=?",
            (value, payload["job_id"]),
        )
    before = counts(db)
    with pytest.raises(ValidationError):
        import_hits(db, project, source)
    assert counts(db) == before


@pytest.mark.parametrize(
    "details,status",
    [
        ("{}", "failed"),
        ("{", "completed"),
        ("[]", "completed"),
        ('{"hits_imports": []}', "completed"),
    ],
)
def test_bad_producing_workflow_is_rejected(setup, details, status):
    project, db, source, _payload = setup
    with db.transaction():
        db.conn.execute(
            "UPDATE workflow_runs SET execution_details=?,status=? WHERE run_id='WF_TOY'",
            (details, status),
        )
    before = counts(db)
    with pytest.raises(ValidationError):
        import_hits(db, project, source)
    assert counts(db) == before
