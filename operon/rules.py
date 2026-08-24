"""Versioned rule engine: metrics are observed, profiles decide.

QC programs only measure; this engine reads YAML profiles and records
decisions, reason codes, observed values and thresholds.  Decisions are
data, not print statements.
"""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
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
    return f"value {operator} {rule.get('value')}"


def evaluate_entity(db: Database, project: Project, entity_type: str, entity_id: str,
                    profile_name: str | None = None) -> dict[str, Any]:
    profile_name = profile_name or project.config["qc"]["default_profile"]
    profile = load_profile(project.profiles_dir, profile_name, expected_kind="qc")
    profile_document = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    profile_sha256 = hashlib.sha256(profile_document.encode("utf-8")).hexdigest()
    profile_version = int(profile.get("version", 1))
    profile_snapshot_id = db.record_profile(
        profile_name, profile_version, profile_sha256, profile_document, now_iso()
    )
    observed = db.latest_metrics(entity_type, entity_id)

    reasons: list[str] = []
    details: list[dict[str, Any]] = []
    missing_required = 0
    required_failed = 0
    warnings_triggered = 0

    for rule in profile.get("required", []):
        name = rule["metric"]
        if name not in observed or observed[name] is None:
            missing_required += 1
            reasons.append(f"MISSING_METRIC:{name}")
            details.append({"metric": name, "rule": _describe_rule(rule), "observed": None, "code": f"MISSING_{name.upper()}", "kind": "required"})
            continue
        value = observed[name]
        if not _satisfies(value, rule):
            required_failed += 1
            code = rule.get("code", f"{name.upper()}_FAILED")
            reasons.append(code)
            details.append({"metric": name, "rule": _describe_rule(rule), "observed": value, "threshold": rule, "code": code, "kind": "required"})
        else:
            details.append({"metric": name, "rule": _describe_rule(rule), "observed": value, "code": rule.get("code", ""), "kind": "required_pass"})

    for rule in profile.get("warnings", []):
        name = rule["metric"]
        value = observed.get(name)
        if value is None:
            continue
        if _satisfies(value, rule):
            warnings_triggered += 1
            code = rule.get("code", f"{name.upper()}_WARNING")
            reasons.append(code)
            details.append({"metric": name, "rule": _describe_rule(rule), "observed": value, "threshold": rule, "code": code, "kind": "warning"})

    if required_failed:
        decision = "FAIL"
    elif missing_required:
        decision = "NOT_EVALUATED"
    elif warnings_triggered:
        decision = "PASS_WITH_WARNINGS"
    else:
        decision = "PASS"

    row = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "profile": profile_name,
        "profile_version": profile_version,
        "profile_snapshot_id": profile_snapshot_id,
        "profile_sha256": profile_sha256,
        "decision": decision,
        "reason_codes": json.dumps(reasons, ensure_ascii=False),
        "observed": json.dumps(observed, ensure_ascii=False),
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
    sql = "SELECT DISTINCT entity_type, entity_id FROM qc_results WHERE 1=1"
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
