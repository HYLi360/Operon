"""Database savepoint, source-provenance, and generic helper branches."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import EntityNotFoundError, ValidationError


@pytest.fixture
def db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    database = Database(project.db_path)
    try:
        yield database
    finally:
        database.close()


def test_nested_transaction_rollback_and_upsert_rows(db):
    with db.transaction():
        db.conn.execute("INSERT INTO organisms(organism_id, scientific_name) VALUES('ORG_000001','outer')")
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.conn.execute("INSERT INTO organisms(organism_id, scientific_name) VALUES('ORG_000002','inner')")
                raise RuntimeError("rollback savepoint")
    assert db.entity_exists("organism", "ORG_000001")
    assert not db.entity_exists("organism", "ORG_000002")
    assert db.upsert_rows("organisms", ["organism_id", "scientific_name"], []) == 0
    assert db.upsert_rows("organisms", ["organism_id", "scientific_name"], [
        {"organism_id": "ORG_000001", "scientific_name": "updated"},
        {"organism_id": "ORG_000003", "scientific_name": "new"},
    ]) == 2
    assert db.query("SELECT scientific_name FROM organisms WHERE organism_id='ORG_000001'")[0][0] == "updated"


def test_readonly_query_authorizer_and_entity_id_validation(db):
    assert db.readonly_query("PRAGMA table_info(organisms)")
    with pytest.raises(sqlite3.DatabaseError):
        db.readonly_query("PRAGMA journal_mode=WAL")
    with pytest.raises(sqlite3.DatabaseError):
        db.readonly_query("DELETE FROM organisms")
    assert db.entity_exists("unknown", "X") is False
    with pytest.raises(EntityNotFoundError):
        db.require_entity("organism", "ORG_999999")
    assert db.next_id("source") == "SRC_000001"
    with pytest.raises(ValidationError, match="unknown entity type"):
        db.next_id("unknown")


def test_next_id_reserves_numbers_across_connections(db):
    other = Database(db.path)
    try:
        first = db.next_id("organism")
        second = other.next_id("organism")
    finally:
        other.close()
    assert first == "ORG_000001"
    assert second == "ORG_000002"


def test_nested_transaction_interrupt_rolls_back_only_inner_savepoint(db):
    with db.transaction():
        db.conn.execute("INSERT INTO organisms(organism_id,scientific_name) VALUES('ORG_000001','outer')")
        with pytest.raises(KeyboardInterrupt):
            with db.transaction():
                db.conn.execute("INSERT INTO organisms(organism_id,scientific_name) VALUES('ORG_000002','inner')")
                raise KeyboardInterrupt()
        assert db.entity_exists("organism", "ORG_000001")
        assert not db.entity_exists("organism", "ORG_000002")
    assert not db.conn.in_transaction


def test_data_source_requires_source_type(db):
    with pytest.raises(ValidationError, match="source_type"):
        db.register_data_source({})


def test_data_source_full_record_is_accepted(db):
    source = db.register_data_source({
        "source_type": "non_insdc", "provider": "X", "database_name": "D",
        "citation": "doi:10.1/x", "license_name": "CC0-1.0",
    })
    assert source["source_id"] == "SRC_000001"


def test_data_source_idempotency_and_link_validation(db):
    source = db.register_data_source({
        "source_type": "insdc", "provider": "NCBI", "database_name": "Assembly",
    })
    assert db.register_data_source({
        "source_type": "insdc", "provider": "NCBI", "database_name": "Assembly",
    })["source_id"] == source["source_id"]
    with pytest.raises(ValidationError, match="unsupported source link"):
        db.link_data_source(source["source_id"], [("bad", "X")])
    with pytest.raises(EntityNotFoundError, match="data source"):
        db.link_data_source("SRC_999999", [])
    with pytest.raises(EntityNotFoundError, match="organism"):
        db.link_data_source(source["source_id"], [("organism", "ORG_999999")])
    with pytest.raises(EntityNotFoundError, match="file"):
        db.link_data_source(source["source_id"], [("file", "FIL_999999")])
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "O"})
    assert db.link_data_source(source["source_id"], [("organism", "ORG_000001")]) == 1
    assert db.link_data_source(source["source_id"], [("organism", "ORG_000001")]) == 0


def test_metadata_columns_export_empty_and_metric_conservative_fallback(db):
    schema = SimpleNamespace(tables={
        "not-manual": {"fields": {"x": {"type": "string"}}},
        "organisms": {"fields": {
            "organism_id": {"type": "id"},
            "custom_score": {"type": "float"},
        }},
    })
    db.ensure_metadata_columns(schema)
    assert "custom_score" in db.table_columns("organisms")
    unsafe = SimpleNamespace(tables={
        "organisms": {"fields": {"bad-name": {"type": "string"}}},
    })
    with pytest.raises(ValidationError, match="unsafe metadata column"):
        db.ensure_metadata_columns(unsafe)
    assert db.export_rows("organisms", ["does_not_exist"]) == []

    base = {
        "entity_type": "organism", "entity_id": "ORG_000001", "qc_stage": "s",
        "metric_name": "file_exists", "tool": "t", "tool_version": "1",
        "parameter_set": "p", "metric_unit": None,
    }
    db.insert_qc_result({**base, "input_identity": "a", "metric_value": "bad",
                         "metric_numeric": None, "evaluated_at": "2026-02-01"})
    db.insert_qc_result({**base, "input_identity": "b", "metric_value": "1",
                         "metric_numeric": 1, "evaluated_at": "2026-01-01"})
    assert db.latest_metrics("organism", "ORG_000001")["file_exists"] == "bad"


def test_missing_file_status_raises(db):
    with pytest.raises(EntityNotFoundError, match="does not exist"):
        db.set_file_status("FIL_999999", "REMOTE_ONLY", reason="x", actor="test")
