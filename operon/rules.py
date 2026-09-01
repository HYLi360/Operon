"""Versioned rule engine: metrics are observed, profiles decide.

QC programs only measure; this engine reads YAML profiles and records
decisions, reason codes, observed values and thresholds.  Decisions are
data, not print statements.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.profiles import load_profile
from operon.utils import now_iso
from operon.workflow import set_state_bulk

DECISION_STATES = {
    "PASS": "ACCEPTED",
    "PASS_WITH_WARNINGS": "ACCEPTED",
    "NOT_EVALUATED": "QC_COMPLETE",
    "REVIEW": "REVIEW",
    "FAIL": "REJECTED",
    "EXCLUDED": "REJECTED",
}


def _compare(observed: Any, operator: str, expected: Any) -> bool:
    observed_f = float(observed)
    expected_f = float(expected)
    if operator == ">=":
        return observed_f >= expected_f
    if operator == "<=":
        return observed_f <= expected_f
    if operator == ">":
        return observed_f > expected_f
    if operator == "<":
        return observed_f < expected_f
    if operator == "==":
        return observed_f == expected_f
    if operator == "!=":
        return observed_f != expected_f
    raise ValidationError(f"unknown operator {operator!r}")


def _satisfies(observed: Any, rule: dict[str, Any]) -> bool:
    operator = rule.get("operator")
    if operator == "between":
        return float(rule["min"]) <= float(observed) <= float(rule["max"])
    if operator == "in":
        return str(observed) in {str(v) for v in rule.get("values", [])}
    if operator == "not_in":
        return str(observed) not in {str(v) for v in rule.get("values", [])}
    if operator == "exists":
        return observed is not None
    if operator is None:
        return True
    return _compare(observed, operator, rule.get("value"))


def _describe_rule(rule: dict[str, Any]) -> str:
    operator = rule.get("operator", "exists")
    if operator == "between":
        return f"{rule.get('min')} <= value <= {rule.get('max')}"
    if operator in {"in", "not_in"}:
        return f"{operator} {rule.get('values')}"
    if operator == "exists":
        return "metric exists"
    if "value_by" in rule:
        selector = rule["value_by"].get("metric", "?")
        return f"value {operator} threshold selected by {selector}"
    return f"value {operator} {rule.get('value')}"


def _rule_metrics(db: Database, entity_type: str, entity_id: str,
                  rule: dict[str, Any], default: dict[str, Any]) -> dict[str, Any]:
    source = rule.get("source")
    if source is None:
        return default
    if not isinstance(source, dict) or not source.get("qc_stage"):
        raise ValidationError("rule source must be a mapping with a non-empty qc_stage")
    return db.latest_metrics(entity_type, entity_id, qc_stage=str(source["qc_stage"]))


def _resolve_value_by(rule: dict[str, Any], observed: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return an effective scalar rule, or an unknown-selector policy."""
    value_by = rule.get("value_by")
    if value_by is None:
        return rule, None
    if not isinstance(value_by, dict) or not value_by.get("metric"):
        raise ValidationError("value_by must be a mapping with a non-empty metric")
    selector_name = str(value_by["metric"])
    values = value_by.get("values")
    if not isinstance(values, dict):
        raise ValidationError(f"value_by for {selector_name!r} requires a values mapping")
    selector_value = observed.get(selector_name)
    key = str(selector_value) if selector_value is not None else None
    if key is None or key not in {str(k) for k in values}:
        if "unknown" not in value_by:
            return None, "not_evaluated"
        policy = str(value_by["unknown"])
        if policy not in {"warning", "fail", "ignore"}:
            raise ValidationError(
                f"value_by unknown policy must be warning, fail, or ignore; got {policy!r}"
            )
        return None, policy
    expected = next(value for candidate, value in values.items() if str(candidate) == key)
    effective = dict(rule)
    effective.pop("value_by", None)
    effective["value"] = expected
    return effective, None


def evaluate_entity(db: Database, project: Project, entity_type: str, entity_id: str,
                    profile_name: str | None = None) -> dict[str, Any]:
    db.require_not_retired(entity_type, entity_id)
    profile_name = profile_name or project.config["qc"]["default_profile"]
    profile = load_profile(project.profiles_dir, profile_name, expected_kind="qc")
    profile_document = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    profile_sha256 = hashlib.sha256(profile_document.encode("utf-8")).hexdigest()
    profile_version = int(profile.get("version", 1))
    profile_snapshot_id = db.record_profile(
        profile_name, profile_version, profile_sha256, profile_document, now_iso()
    )
    observed = db.latest_metrics(entity_type, entity_id)
    decision_observed: dict[str, Any] = dict(observed)
    source_snapshots: dict[str, dict[str, Any]] = {}

    reasons: list[str] = []
    details: list[dict[str, Any]] = []
    missing_required = 0
    required_failed = 0
    warnings_triggered = 0

    for rule in profile.get("required", []):
        rule_observed = _rule_metrics(db, entity_type, entity_id, rule, observed)
        if rule.get("source"):
            stage = str(rule["source"]["qc_stage"])
            source_snapshots[stage] = dict(rule_observed)
        name = rule["metric"]
        if name not in rule_observed or rule_observed[name] is None:
            missing_required += 1
            reasons.append(f"MISSING_METRIC:{name}")
            details.append({"metric": name, "rule": _describe_rule(rule), "observed": None,
                            "source": rule.get("source"), "code": f"MISSING_{name.upper()}",
                            "kind": "required"})
            continue
        effective_rule, unknown_policy = _resolve_value_by(rule, rule_observed)
        if unknown_policy is not None:
            selector = str(rule.get("value_by", {}).get("metric", "selector"))
            selector_value = rule_observed.get(selector)
            default_code = (f"{selector.upper()}_IGNORED" if unknown_policy == "ignore"
                            else f"{selector.upper()}_UNKNOWN")
            code = rule.get("unknown_code", default_code)
            kind = ("value_by_unknown_missing" if unknown_policy == "not_evaluated"
                    else f"value_by_unknown_{unknown_policy}")
            details.append({"metric": name, "rule": _describe_rule(rule),
                            "observed": rule_observed[name], "selector": selector,
                            "selector_value": selector_value, "source": rule.get("source"),
                            "code": code, "kind": kind})
            if unknown_policy == "warning":
                warnings_triggered += 1
                reasons.append(code)
            elif unknown_policy == "fail":
                required_failed += 1
                reasons.append(code)
            elif unknown_policy == "not_evaluated":
                missing_required += 1
                reasons.append(code)
            elif unknown_policy == "ignore":
                reasons.append(code)
            continue
        value = rule_observed[name]
        assert effective_rule is not None
        if not _satisfies(value, effective_rule):
            required_failed += 1
            code = rule.get("code", f"{name.upper()}_FAILED")
            reasons.append(code)
            details.append({"metric": name, "rule": _describe_rule(rule), "observed": value,
                            "threshold": effective_rule, "source": rule.get("source"),
                            "code": code, "kind": "required"})
        else:
            details.append({"metric": name, "rule": _describe_rule(rule), "observed": value,
                            "threshold": effective_rule, "source": rule.get("source"),
                            "code": rule.get("code", ""), "kind": "required_pass"})

    for rule in profile.get("warnings", []):
        rule_observed = _rule_metrics(db, entity_type, entity_id, rule, observed)
        if rule.get("source"):
            stage = str(rule["source"]["qc_stage"])
            source_snapshots[stage] = dict(rule_observed)
        name = rule["metric"]
        value = rule_observed.get(name)
        if value is None:
            continue
        effective_rule, unknown_policy = _resolve_value_by(rule, rule_observed)
        if unknown_policy is not None:
            if unknown_policy == "warning":
                selector = str(rule.get("value_by", {}).get("metric", "selector"))
                code = rule.get("unknown_code", f"{selector.upper()}_UNKNOWN")
                warnings_triggered += 1
                reasons.append(code)
                details.append({"metric": name, "rule": _describe_rule(rule),
                                "observed": value, "selector": selector,
                                "selector_value": rule_observed.get(selector),
                                "source": rule.get("source"), "code": code,
                                "kind": "value_by_unknown_warning"})
            elif unknown_policy == "ignore":
                selector = str(rule.get("value_by", {}).get("metric", "selector"))
                code = rule.get("unknown_code", f"{selector.upper()}_IGNORED")
                reasons.append(code)
                details.append({"metric": name, "rule": _describe_rule(rule),
                                "observed": value, "selector": selector,
                                "selector_value": rule_observed.get(selector),
                                "source": rule.get("source"), "code": code,
                                "kind": "value_by_unknown_ignore"})
            continue
        assert effective_rule is not None
        if _satisfies(value, effective_rule):
            warnings_triggered += 1
            code = rule.get("code", f"{name.upper()}_WARNING")
            reasons.append(code)
            details.append({"metric": name, "rule": _describe_rule(rule), "observed": value,
                            "threshold": effective_rule, "source": rule.get("source"),
                            "code": code, "kind": "warning"})

    if required_failed:
        decision = "FAIL"
    elif missing_required:
        decision = "NOT_EVALUATED"
    elif warnings_triggered:
        decision = "PASS_WITH_WARNINGS"
    else:
        decision = "PASS"

    if source_snapshots:
        decision_observed["_rule_sources"] = source_snapshots

    row = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "profile": profile_name,
        "profile_version": profile_version,
        "profile_snapshot_id": profile_snapshot_id,
        "profile_sha256": profile_sha256,
        "decision": decision,
        "reason_codes": json.dumps(reasons, ensure_ascii=False),
        "observed": json.dumps(decision_observed, ensure_ascii=False),
        "thresholds": json.dumps(profile, ensure_ascii=False),
        "evaluated_at": now_iso(),
    }
    db.upsert_decision(row)
    set_state_bulk(db, entity_type, entity_id, DECISION_STATES.get(decision, "QC_COMPLETE"),
                   f"profile {profile_name}: {decision} ({', '.join(reasons) or 'no issues'})")
    row["details"] = details
    return row


def evaluate_all(db: Database, project: Project, profile_name: str | None = None,
                 entity_type: str | None = None) -> list[dict[str, Any]]:
    profile_name = profile_name or project.config["qc"]["default_profile"]
    profile = load_profile(project.profiles_dir, profile_name, expected_kind="qc")
    applies_to = set(profile.get("applies_to", ["assembly", "annotation", "run"]))
    sql = (
        "SELECT DISTINCT entity_type, entity_id FROM qc_results WHERE NOT EXISTS ("
        "SELECT 1 FROM effective_retired_entities r "
        "WHERE r.entity_type=qc_results.entity_type AND r.entity_id=qc_results.entity_id)"
    )
    params: list[Any] = []
    if entity_type:
        sql += " AND entity_type=?"
        params.append(entity_type)
    sql += " ORDER BY entity_type, entity_id"
    rows = db.conn.execute(sql, params).fetchall()
    results = []
    for row in rows:
        if row["entity_type"] not in applies_to:
            continue
        results.append(evaluate_entity(db, project, row["entity_type"], row["entity_id"], profile_name))
    return results


def curate_decision(db: Database, entity_type: str, entity_id: str, profile: str,
                    decision: str, reviewer: str, reason: str, evidence: str | None = None) -> None:
    """Record a human override as audited data, never as a silent edit."""
    db.require_not_retired(entity_type, entity_id)
    row = db.conn.execute(
        "SELECT * FROM decisions WHERE entity_type=? AND entity_id=? AND profile=? "
        "ORDER BY decision_id DESC LIMIT 1",
        (entity_type, entity_id, profile),
    ).fetchone()
    if not row:
        raise ValidationError(f"no automatic decision for {entity_type} {entity_id} under profile {profile}")
    old = row["curated_decision"] or row["decision"]
    decision = decision.upper()
    db.record_change(
        "decision", f"{entity_type}:{entity_id}:{profile}", "curated_decision", old, decision,
        reason=reason, evidence=evidence, actor=reviewer,
    )
    db.conn.execute(
        "UPDATE decisions SET curated_decision=?, curated_by=?, curated_reason=?, curated_evidence=?, curated_at=? "
        "WHERE decision_id=?",
        (decision, reviewer, reason, evidence, now_iso(), row["decision_id"]),
    )
    db.conn.commit()
    state = "ACCEPTED" if decision in {"PASS", "PASS_WITH_WARNINGS", "ACCEPT_WITH_WARNING"} else (
        "REJECTED" if decision in {"FAIL", "EXCLUDED"} else "REVIEW" if decision == "REVIEW" else "QC_COMPLETE"
    )
    set_state_bulk(db, entity_type, entity_id, state, f"curated decision {decision} by {reviewer}: {reason}")
