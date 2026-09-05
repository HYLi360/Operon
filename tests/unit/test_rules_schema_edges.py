"""Rule-engine policies and metadata-schema validation edge cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from operon import rules
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ValidationError
from operon.schema import Schema, SchemaError, read_tsv, write_tsv


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Example"})
    try:
        yield project, db
    finally:
        db.close()


@pytest.mark.parametrize(
    ("operator", "observed", "expected", "result"),
    [
        (">=", 2, 1, True), (">=", 1, 2, False),
        ("<=", 1, 1, True), ("<=", 2, 1, False),
        (">", 2, 1, True), (">", 1, 1, False),
        ("<", 1, 2, True), ("<", 2, 2, False),
        ("==", 1, 1, True), ("==", 1, 2, False),
        ("!=", 1, 2, True), ("!=", 1, 1, False),
    ],
)
def test_compare_operators(operator, observed, expected, result):
    assert rules._compare(observed, operator, expected) is result


def test_rule_satisfaction_descriptions_and_validation(project_db):
    _project, db = project_db
    assert rules._satisfies(5, {"operator": "between", "min": 1, "max": 10})
    assert rules._satisfies("a", {"operator": "in", "values": ["a"]})
    assert rules._satisfies("b", {"operator": "not_in", "values": ["a"]})
    assert rules._satisfies(0, {"operator": "exists"})
    assert rules._satisfies(0, {})
    with pytest.raises(ValidationError, match="unknown operator"):
        rules._compare(1, "bad", 1)
    assert rules._describe_rule({"operator": "between", "min": 1, "max": 2}) == "1 <= value <= 2"
    assert rules._describe_rule({"operator": "in", "values": [1]}) == "in [1]"
    assert rules._describe_rule({"operator": "exists"}) == "metric exists"
    assert "selected by lineage" in rules._describe_rule({
        "operator": ">=", "value_by": {"metric": "lineage"}
    })
    assert rules._describe_rule({"operator": ">=", "value": 1}) == "value >= 1"
    with pytest.raises(ValidationError, match="rule source"):
        rules._rule_metrics(db, "organism", "ORG_000001", {"source": []}, {})


@pytest.mark.parametrize(
    ("value_by", "message"),
    [
        ([], "must be a mapping"),
        ({"metric": "x", "values": []}, "requires a values mapping"),
        ({"metric": "x", "values": {}, "unknown": "bad"}, "unknown policy"),
        ({"metric": "x", "values": {}, "unknown": "not_evaluated"}, "unknown policy"),
    ],
)
def test_value_by_validation(value_by, message):
    with pytest.raises(ValidationError, match=message):
        rules._resolve_value_by({"value_by": value_by}, {})


def test_value_by_resolution_policies_and_matching_keys():
    rule = {"metric": "score", "operator": ">=", "value_by": {
        "metric": "lineage", "values": {1: 80}, "unknown": "warning",
    }}
    effective, policy = rules._resolve_value_by(rule, {"lineage": 1})
    assert policy is None and effective["value"] == 80 and "value_by" not in effective
    assert rules._resolve_value_by(rule, {"lineage": "missing"}) == (None, "warning")
    assert rules._resolve_value_by({"metric": "x"}, {})[0] == {"metric": "x"}


def _write_profile(project, name, document):
    (project.profiles_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )


def test_evaluate_entity_covers_missing_fail_warning_and_source_snapshots(project_db):
    project, db = project_db
    db.insert_qc_result({
        "entity_type": "organism", "entity_id": "ORG_000001", "qc_stage": "base",
        "metric_name": "score", "metric_value": "50", "metric_numeric": 50,
        "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
    })
    db.insert_qc_result({
        "entity_type": "organism", "entity_id": "ORG_000001", "qc_stage": "analysis:x",
        "metric_name": "lineage", "metric_value": "unknown", "metric_numeric": None,
        "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
    })
    db.insert_qc_result({
        "entity_type": "organism", "entity_id": "ORG_000001", "qc_stage": "analysis:x",
        "metric_name": "score", "metric_value": "50", "metric_numeric": 50,
        "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
    })
    base = {"kind": "qc", "version": 1, "applies_to": ["organism"]}
    profiles_to_decisions = {
        "missing": ({**base, "required": [{"metric": "missing", "operator": "exists"}]}, "NOT_EVALUATED"),
        "fail": ({**base, "required": [{"metric": "score", "operator": ">=", "value": 80}]}, "FAIL"),
        "pass": ({**base, "required": [{"metric": "score", "operator": ">=", "value": 40}]}, "PASS"),
        "warning": ({**base, "warnings": [{"metric": "score", "operator": "<", "value": 80}]}, "PASS_WITH_WARNINGS"),
        "unknown_warning": ({**base, "required": [{
            "metric": "score", "operator": ">=", "source": {"qc_stage": "analysis:x"},
            "value_by": {"metric": "lineage", "values": {"known": 80}, "unknown": "warning"},
            "unknown_code": "UNKNOWN_LINEAGE",
        }]}, "PASS_WITH_WARNINGS"),
        "unknown_fail": ({**base, "required": [{
            "metric": "score", "operator": ">=", "source": {"qc_stage": "analysis:x"},
            "value_by": {"metric": "lineage", "values": {"known": 80}, "unknown": "fail"},
        }]}, "FAIL"),
        "unknown_missing": ({**base, "required": [{
            "metric": "score", "operator": ">=", "source": {"qc_stage": "analysis:x"},
            "value_by": {"metric": "lineage", "values": {"known": 80}},
        }]}, "NOT_EVALUATED"),
        "unknown_ignore": ({**base, "warnings": [{
            "metric": "score", "operator": "<", "source": {"qc_stage": "analysis:x"},
            "value_by": {"metric": "lineage", "values": {"known": 80}, "unknown": "ignore"},
        }]}, "PASS"),
    }
    for name, (document, expected) in profiles_to_decisions.items():
        _write_profile(project, name, document)
        result = rules.evaluate_entity(db, project, "organism", "ORG_000001", name)
        assert result["decision"] == expected
        if name == "unknown_missing":
            assert result["details"][0]["kind"] == "value_by_unknown_missing"
            assert json.loads(result["reason_codes"]) == ["LINEAGE_UNKNOWN"]
    observed = db.query(
        "SELECT observed FROM current_decisions "
        "WHERE entity_type = ? AND entity_id = ? AND profile = ?",
        ("organism", "ORG_000001", "unknown_ignore"),
    )[0]["observed"]
    assert "_rule_sources" in observed


def _insert_unknown_lineage_metrics(db):
    for metric, value, numeric in (("lineage", "unknown", None), ("score", "50", 50)):
        db.insert_qc_result({
            "entity_type": "organism", "entity_id": "ORG_000001", "qc_stage": "analysis:x",
            "metric_name": metric, "metric_value": value, "metric_numeric": numeric,
            "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
        })


def test_required_rule_with_ignore_policy_leaves_persistent_trace(project_db):
    project, db = project_db
    _insert_unknown_lineage_metrics(db)
    db.insert_qc_result({
        "entity_type": "organism", "entity_id": "ORG_000001", "qc_stage": "base",
        "metric_name": "score", "metric_value": "50", "metric_numeric": 50,
        "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
    })
    _write_profile(project, "required_ignore", {
        "kind": "qc", "version": 1, "applies_to": ["organism"],
        "required": [
            {"metric": "score", "operator": ">=", "source": {"qc_stage": "analysis:x"},
             "value_by": {"metric": "lineage", "values": {"known": 80}, "unknown": "ignore"}},
            {"metric": "score", "operator": ">=", "value": 40},
        ],
    })
    result = rules.evaluate_entity(db, project, "organism", "ORG_000001", "required_ignore")
    assert result["decision"] == "PASS"
    persisted = db.query(
        "SELECT reason_codes FROM current_decisions "
        "WHERE entity_type = ? AND entity_id = ? AND profile = ?",
        ("organism", "ORG_000001", "required_ignore"),
    )[0]["reason_codes"]
    assert json.loads(persisted) == ["LINEAGE_IGNORED"]
    assert result["details"][0] == {
        "metric": "score", "rule": "value >= threshold selected by lineage",
        "observed": 50.0, "selector": "lineage", "selector_value": "unknown",
        "source": {"qc_stage": "analysis:x"}, "code": "LINEAGE_IGNORED",
        "kind": "value_by_unknown_ignore",
    }


def test_warning_rule_with_ignore_policy_leaves_persistent_trace(project_db):
    project, db = project_db
    _insert_unknown_lineage_metrics(db)
    _write_profile(project, "warn_ignore", {
        "kind": "qc", "version": 1, "applies_to": ["organism"],
        "warnings": [{
            "metric": "score", "operator": "<", "source": {"qc_stage": "analysis:x"},
            "value_by": {"metric": "lineage", "values": {"known": 80}, "unknown": "ignore"},
            "unknown_code": "LINEAGE_IGNORED_CUSTOM",
        }],
    })
    result = rules.evaluate_entity(db, project, "organism", "ORG_000001", "warn_ignore")
    assert result["decision"] == "PASS"
    persisted = db.query(
        "SELECT reason_codes FROM current_decisions "
        "WHERE entity_type = ? AND entity_id = ? AND profile = ?",
        ("organism", "ORG_000001", "warn_ignore"),
    )[0]["reason_codes"]
    assert json.loads(persisted) == ["LINEAGE_IGNORED_CUSTOM"]
    assert result["details"] == [{
        "metric": "score", "rule": "value < threshold selected by lineage",
        "observed": 50.0, "selector": "lineage", "selector_value": "unknown",
        "source": {"qc_stage": "analysis:x"}, "code": "LINEAGE_IGNORED_CUSTOM",
        "kind": "value_by_unknown_ignore",
    }]


def test_warning_rule_with_unknown_selector_emits_warning_detail(project_db):
    project, db = project_db
    db.insert_qc_result({
        "entity_type": "organism", "entity_id": "ORG_000001", "qc_stage": "analysis:x",
        "metric_name": "lineage", "metric_value": "unknown", "metric_numeric": None,
        "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
    })
    db.insert_qc_result({
        "entity_type": "organism", "entity_id": "ORG_000001", "qc_stage": "analysis:x",
        "metric_name": "score", "metric_value": "50", "metric_numeric": 50,
        "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
    })
    _write_profile(project, "warn_unknown", {
        "kind": "qc", "version": 1, "applies_to": ["organism"],
        "warnings": [{
            "metric": "score", "operator": "<", "source": {"qc_stage": "analysis:x"},
            "value_by": {"metric": "lineage", "values": {"known": 80}, "unknown": "warning"},
            "unknown_code": "LINEAGE_UNKNOWN",
        }],
    })
    result = rules.evaluate_entity(db, project, "organism", "ORG_000001", "warn_unknown")
    assert result["decision"] == "PASS_WITH_WARNINGS"
    assert json.loads(result["reason_codes"]) == ["LINEAGE_UNKNOWN"]
    assert result["details"] == [{
        "metric": "score", "rule": "value < threshold selected by lineage",
        "observed": 50.0, "selector": "lineage", "selector_value": "unknown",
        "source": {"qc_stage": "analysis:x"}, "code": "LINEAGE_UNKNOWN",
        "kind": "value_by_unknown_warning",
    }]


def test_evaluate_all_skips_entities_outside_profile_applies_to(project_db):
    project, db = project_db
    db.insert_qc_result({
        "entity_type": "organism", "entity_id": "ORG_000001", "qc_stage": "base",
        "metric_name": "score", "metric_value": "50", "metric_numeric": 50,
        "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
    })
    _write_profile(project, "assemblies_only", {
        "kind": "qc", "version": 1, "applies_to": ["assembly"],
        "required": [{"metric": "score", "operator": ">=", "value": 1}],
    })
    assert rules.evaluate_all(db, project, "assemblies_only") == []
    assert db.query("SELECT COUNT(*) FROM decisions")[0][0] == 0


def test_evaluate_all_filter_and_curate_missing(project_db):
    project, db = project_db
    _write_profile(project, "none", {
        "kind": "qc", "version": 1, "applies_to": ["assembly"], "required": [],
    })
    assert rules.evaluate_all(db, project, "none", entity_type="organism") == []
    with pytest.raises(ValidationError, match="no automatic decision"):
        rules.curate_decision(db, "organism", "ORG_000001", "none", "PASS", "r", "why")


def test_re_evaluation_carries_curated_decision_forward(project_db):
    project, db = project_db
    db.insert_qc_result({
        "entity_type": "organism", "entity_id": "ORG_000001", "qc_stage": "base",
        "metric_name": "score", "metric_value": "50", "metric_numeric": 50,
        "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
    })
    _write_profile(project, "curation_lifecycle", {
        "kind": "qc", "version": 1, "applies_to": ["organism"],
        "required": [{"metric": "score", "operator": ">=", "value": 80}],
    })
    first = rules.evaluate_entity(db, project, "organism", "ORG_000001", "curation_lifecycle")
    assert first["decision"] == "FAIL"
    rules.curate_decision(
        db, "organism", "ORG_000001", "curation_lifecycle", "PASS",
        reviewer="reviewer", reason="manual evidence",
    )
    second = rules.evaluate_entity(db, project, "organism", "ORG_000001", "curation_lifecycle")
    assert second["decision"] == "FAIL"
    assert db.get_entity_state("organism", "ORG_000001") == "ACCEPTED"
    current = db.query(
        "SELECT decision, curated_decision, curated_by, curated_reason "
        "FROM current_decisions WHERE entity_type='organism' AND entity_id='ORG_000001' "
        "AND profile='curation_lifecycle'"
    )[0]
    assert current["decision"] == "FAIL"
    assert current["curated_decision"] == "PASS"
    assert current["curated_by"] == "reviewer"
    assert current["curated_reason"] == "manual evidence"


def test_schema_construction_columns_and_error_rendering(tmp_path):
    with pytest.raises(ValidationError, match="tables.*mapping"):
        Schema([])
    with pytest.raises(ValidationError, match="schema file not found"):
        Schema.from_file(tmp_path / "missing")
    schema = Schema({"schema_version": "x", "tables": {"t": {
        "primary_key": "id", "unique": [["name"]], "fields": {"id": {"type": "id"}}
    }}})
    assert schema.table_names() == ["t"]
    assert schema.columns("t") == ["id"]
    assert schema.primary_key("t") == "id"
    assert schema.unique_combinations("t") == [["name"]]
    with pytest.raises(ValidationError, match="unknown schema table"):
        schema.columns("missing")
    assert "row 1" in str(SchemaError("t", 1, "f", "v", "bad"))


@pytest.mark.parametrize(
    ("spec", "value", "expected", "error"),
    [
        ({"required": True}, "", None, "required"),
        ({"type": "id"}, 1, None, "ID must be"),
        ({"type": "id", "pattern": r"^A\d+$"}, "bad", None, "does not match"),
        ({"type": "string"}, 1, "1", None),
        ({"type": "integer"}, "2", 2, None),
        ({"type": "integer", "min": 3}, "2", None, "must be >="),
        ({"type": "integer", "max": 1}, "2", None, "must be <="),
        ({"type": "float"}, "2.5", 2.5, None),
        ({"type": "boolean"}, "yes", 1, None),
        ({"type": "boolean"}, "no", 0, None),
        ({"type": "boolean"}, object(), 1, None),
        ({"type": "date"}, "2024-01-02", "2024-01-02", None),
        ({"type": "datetime"}, "2024-01-02T03:04:05", "2024-01-02T03:04:05", None),
        ({"type": "bad"}, "x", None, "unknown schema type"),
        ({"type": "integer"}, "x", None, "expected integer"),
        ({"type": "string", "allowed": ["PASS"]}, "pass", "PASS", None),
        ({"type": "string", "allowed": ["PASS"]}, "bad", None, "not in allowed"),
        ({"type": "string", "allowed": ["none"]}, "none", "none", None),
    ],
)
def test_schema_field_normalization(spec, value, expected, error):
    schema = Schema({"tables": {"t": {"fields": {"f": spec}}}})
    actual, message = schema._normalize_field("f", spec, value)
    assert actual == expected
    if error:
        assert error in message
    else:
        assert message is None


def test_schema_row_duplicates_unknown_fields_and_tsv_edges(tmp_path):
    schema = Schema({"tables": {"t": {
        "primary_key": "id", "unique": [["name"]],
        "fields": {"id": {"type": "id", "required": True}, "name": {"type": "string"}},
    }}})
    with pytest.raises(ValidationError, match="unknown field"):
        schema.validate_and_normalize("t", [{"id": "A", "extra": 1}])
    with pytest.raises(ValidationError, match="duplicate primary key"):
        schema.validate_and_normalize("t", [{"id": "A", "name": "x"}, {"id": "A", "name": "y"}])
    with pytest.raises(ValidationError, match="duplicate unique combination"):
        schema.validate_and_normalize("t", [{"id": "A", "name": "x"}, {"id": "B", "name": "x"}])
    with pytest.raises(ValidationError, match="schema has no table"):
        schema.validate_and_normalize("missing", [])

    empty = tmp_path / "empty.tsv"
    empty.write_text("# comment\n\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="no header"):
        read_tsv(empty)
    missing = tmp_path / "missing.tsv"
    missing.write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="missing columns"):
        read_tsv(missing, required_header=["b"])
    bad = tmp_path / "bad.tsv"
    bad.write_text("a\tb\n1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="has 1 fields"):
        read_tsv(bad)
    trailing = tmp_path / "trailing.tsv"
    trailing.write_text("a\t\n1\n", encoding="utf-8")
    assert read_tsv(trailing) == [{"a": "1", "": ""}]
    output = tmp_path / "out.tsv"
    write_tsv(output, ["a", "b"], [{"a": None, "b": 1}, [2, None]])
    assert read_tsv(output) == [{"a": "", "b": "1"}, {"a": "2", "b": ""}]
