"""Sequence classification profiles: per-sequence labels from analysis hits.

Alignment hits are observed data; a versioned YAML profile of kind
``sequence_classification`` decides.  A profile declares named hit *sources*
over the stored ``analysis_alignments`` rows (an analysis name, a row filter,
and an ordered best-hit ranking) and ordered first-match *rules* that map each
sequence to a label.  The engine writes one row per labeled sequence into
``sequence_labels`` in a single transaction and audits every label change in
``changes``.  Thresholds are never hard-coded here; they live in the profile.

Condition grammar (filters and rule ``when`` clauses share it): each condition
is a mapping with ``field`` plus an ``operator`` — ``>=`` ``<=`` ``>`` ``<``
``==`` ``!=`` (numeric, with string fallback for equality), ``in`` /
``not_in`` (``values`` list), ``between`` (``min``/``max``), ``exists`` and
``like`` (case-insensitive SQL LIKE pattern with ``%``/``_`` wildcards).
A filter may also nest ``any:`` groups whose conditions are OR-ed and
``not:`` negations of a single condition; the top-level list is always
AND-ed.  Fields resolve against the alignment
columns, the derived ``span``
(``query_end - query_start + 1``) and ``seqid`` (query id up to the first
whitespace), then against keys of the row's ``extra_json``.  A missing field
never satisfies a condition.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.profiles import load_profile
from operon.utils import now_iso
from operon.workflow import finish_run, start_run

CLASSIFICATION_KIND = "sequence_classification"

_OPERATORS = {">=", "<=", ">", "<", "==", "!=", "in", "not_in", "between", "exists", "like"}

_ROW_FIELDS = (
    "alignment_id", "job_id", "analysis_name", "query_id", "subject_id",
    "hit_rank", "query_start", "query_end", "subject_start", "subject_end",
    "evalue", "bitscore", "percent_identity",
)


def _extra_fields(extra_json: str | None) -> dict[str, Any]:
    if not extra_json:
        return {}
    try:
        parsed = json.loads(extra_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_context(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten one alignment row into the field context conditions see."""
    context = _extra_fields(row.get("extra_json"))
    context.update({field: row.get(field) for field in _ROW_FIELDS})
    context["seqid"] = str(row["query_id"]).split()[0]
    start, end = row.get("query_start"), row.get("query_end")
    context["span"] = (
        int(end) - int(start) + 1 if start is not None and end is not None else None
    )
    return context


def _compare(observed: Any, operator: str, expected: Any) -> bool:
    if operator in {"==", "!="}:
        try:
            equal = float(observed) == float(expected)
        except (TypeError, ValueError):
            equal = str(observed) == str(expected)
        return equal if operator == "==" else not equal
    try:
        observed_f = float(observed)
        expected_f = float(expected)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"operator {operator!r} requires numeric operands; "
            f"got {observed!r} and {expected!r}"
        ) from exc
    if operator == ">=":
        return observed_f >= expected_f
    if operator == "<=":
        return observed_f <= expected_f
    if operator == ">":
        return observed_f > expected_f
    if operator == "<":
        return observed_f < expected_f
    raise ValidationError(f"unknown operator {operator!r}")


def _like_match(pattern: str, value: str) -> bool:
    """Case-insensitive SQL LIKE semantics (``%`` and ``_`` wildcards)."""
    regex = "".join(
        ".*" if char == "%" else "." if char == "_" else re.escape(char)
        for char in pattern
    )
    return re.fullmatch(regex, value, flags=re.IGNORECASE | re.DOTALL) is not None


def _condition_holds(context: dict[str, Any], condition: dict[str, Any]) -> bool:
    if "any" in condition:
        return any(_condition_holds(context, sub) for sub in condition["any"])
    if "not" in condition:
        return not _condition_holds(context, condition["not"])
    value = context.get(str(condition["field"]))
    if value is None:
        return False
    operator = condition.get("operator")
    if operator == "between":
        return float(condition["min"]) <= float(value) <= float(condition["max"])
    if operator == "in":
        return str(value) in {str(v) for v in condition.get("values", [])}
    if operator == "not_in":
        return str(value) not in {str(v) for v in condition.get("values", [])}
    if operator == "exists":
        return True
    if operator == "like":
        return _like_match(str(condition["value"]), str(value))
    return _compare(value, operator, condition.get("value"))


def _validate_condition(condition: Any, where: str) -> None:
    if not isinstance(condition, dict):
        raise ValidationError(f"{where}: each condition must be a mapping")
    if "any" in condition:
        group = condition["any"]
        if not isinstance(group, list) or not group:
            raise ValidationError(f"{where}: 'any' must be a non-empty list of conditions")
        for sub in group:
            _validate_condition(sub, where)
        return
    if "not" in condition:
        if not isinstance(condition["not"], dict):
            raise ValidationError(f"{where}: 'not' must wrap a single condition")
        _validate_condition(condition["not"], where)
        return
    if not str(condition.get("field") or ""):
        raise ValidationError(f"{where}: each condition needs a non-empty 'field'")
    operator = condition.get("operator")
    if operator not in _OPERATORS:
        raise ValidationError(
            f"{where}: condition on {condition.get('field')!r} needs an operator "
            f"from {sorted(_OPERATORS)}"
        )
    if operator in {"in", "not_in"} and not isinstance(condition.get("values"), list):
        raise ValidationError(f"{where}: operator {operator!r} requires a 'values' list")
    if operator == "between" and ("min" not in condition or "max" not in condition):
        raise ValidationError(f"{where}: operator 'between' requires 'min' and 'max'")
    if operator in {">=", "<=", ">", "<", "==", "!=", "like"} and "value" not in condition:
        raise ValidationError(f"{where}: operator {operator!r} requires 'value'")


def _validate_best_by(best_by: Any, where: str) -> list[dict[str, Any]]:
    if best_by is None:
        return [{"field": "hit_rank", "direction": "asc"}]
    if not isinstance(best_by, list) or not best_by:
        raise ValidationError(f"{where}: 'best_by' must be a non-empty list")
    entries = []
    for entry in best_by:
        if not isinstance(entry, dict) or not str(entry.get("field") or ""):
            raise ValidationError(f"{where}: each 'best_by' entry needs a non-empty 'field'")
        direction = entry.get("direction", "asc")
        if direction not in {"asc", "desc"}:
            raise ValidationError(f"{where}: 'best_by' direction must be 'asc' or 'desc'")
        rank = entry.get("rank")
        if rank is not None and not isinstance(rank, dict):
            raise ValidationError(f"{where}: 'best_by' rank must be a mapping of value to rank")
        entries.append({
            "field": str(entry["field"]),
            "direction": direction,
            "rank": {str(k): float(v) for k, v in rank.items()} if rank is not None else None,
            "default": (float(entry["default"]) if entry.get("default") is not None else None),
        })
    return entries


def validate_classification_profile(profile: dict[str, Any], name: str) -> dict[str, Any]:
    """Check the kind-specific structure and return the normalized spec."""
    where = f"profile {name!r}"
    applies_to = profile.get("applies_to")
    if not isinstance(applies_to, dict):
        raise ValidationError(f"{where}: 'applies_to' must be a mapping")
    entity_type = applies_to.get("entity_type")
    file_role = applies_to.get("file_role")
    if not isinstance(entity_type, str) or not entity_type:
        raise ValidationError(f"{where}: 'applies_to.entity_type' must be a non-empty string")
    if not isinstance(file_role, str) or not file_role:
        raise ValidationError(f"{where}: 'applies_to.file_role' must be a non-empty string")

    raw_sources = profile.get("sources")
    if not isinstance(raw_sources, dict) or not raw_sources:
        raise ValidationError(f"{where}: 'sources' must be a non-empty mapping")
    sources: dict[str, dict[str, Any]] = {}
    for source_name, source in raw_sources.items():
        source_where = f"{where} source {source_name!r}"
        if not isinstance(source, dict):
            raise ValidationError(f"{source_where}: must be a mapping")
        analysis = source.get("analysis")
        if not isinstance(analysis, str) or not analysis:
            raise ValidationError(f"{source_where}: 'analysis' must be a non-empty string")
        row_filter = source.get("filter", [])
        if not isinstance(row_filter, list):
            raise ValidationError(f"{source_where}: 'filter' must be a list of conditions")
        for condition in row_filter:
            _validate_condition(condition, source_where)
        sources[str(source_name)] = {
            "analysis": analysis,
            "filter": row_filter,
            "best_by": _validate_best_by(source.get("best_by"), source_where),
        }

    raw_rules = profile.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValidationError(f"{where}: 'rules' must be a non-empty list")
    rules: list[dict[str, Any]] = []
    for index, rule in enumerate(raw_rules):
        rule_where = f"{where} rule {index}"
        if not isinstance(rule, dict) or not str(rule.get("label") or ""):
            raise ValidationError(f"{rule_where}: each rule needs a non-empty 'label'")
        if rule.get("default"):
            if "source" in rule or "when" in rule or "absent" in rule:
                raise ValidationError(
                    f"{rule_where}: a 'default' rule takes no source/when/absent"
                )
            rules.append({"label": str(rule["label"]), "default": True})
            continue
        source = rule.get("source")
        if source not in sources:
            raise ValidationError(
                f"{rule_where}: 'source' must name a declared source; got {source!r}"
            )
        absent = bool(rule.get("absent"))
        when = rule.get("when", [])
        if absent and when:
            raise ValidationError(f"{rule_where}: 'absent' and 'when' are mutually exclusive")
        if not absent and not when:
            raise ValidationError(
                f"{rule_where}: a rule needs 'absent: true' or a non-empty 'when' list"
            )
        if not isinstance(when, list):
            raise ValidationError(f"{rule_where}: 'when' must be a list of conditions")
        for condition in when:
            _validate_condition(condition, rule_where)
        rules.append({
            "label": str(rule["label"]),
            "default": False,
            "source": str(source),
            "absent": absent,
            "when": when,
        })
    return {
        "entity_type": entity_type,
        "file_role": file_role,
        "sources": sources,
        "rules": rules,
    }


def _best_sort_key(best_by: list[dict[str, Any]]):
    def key(context: dict[str, Any]) -> tuple[Any, ...]:
        parts: list[float] = []
        for entry in best_by:
            raw = context.get(entry["field"])
            rank = entry["rank"]
            if raw is None:
                value = None
            elif rank is not None:
                value = rank.get(str(raw), entry["default"])
                if value is None:
                    value = float(len(rank))
            else:
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    value = None
            if value is None:
                parts.append(float("inf"))
            else:
                parts.append(-value if entry["direction"] == "desc" else value)
        parts.append(float(context.get("alignment_id") or 0))
        return tuple(parts)

    return key


def _source_hits(db: Database, file_id: str, source: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Filtered, best-first alignment contexts per seqid for one source/file.

    Only the latest completed job for the source's analysis on this file
    contributes rows.
    """
    job = db.conn.execute(
        "SELECT MAX(job_id) AS job_id FROM analysis_jobs "
        "WHERE analysis_name=? AND file_id=? AND status='completed'",
        (source["analysis"], file_id),
    ).fetchone()
    if job is None or job["job_id"] is None:
        return {}
    rows = db.conn.execute(
        "SELECT a.alignment_id, a.job_id, a.analysis_name, a.query_id, a.subject_id, "
        "a.hit_rank, a.query_start, a.query_end, a.subject_start, a.subject_end, "
        "a.evalue, a.bitscore, a.percent_identity, a.extra_json "
        "FROM analysis_alignments a "
        "JOIN analysis_jobs j ON j.job_id = a.job_id "
        "WHERE j.status='completed' AND a.job_id=?",
        (job["job_id"],),
    ).fetchall()
    hits: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        context = _row_context(dict(row))
        if not all(_condition_holds(context, condition) for condition in source["filter"]):
            continue
        hits.setdefault(context["seqid"], []).append(context)
    sort_key = _best_sort_key(source["best_by"])
    for contexts in hits.values():
        contexts.sort(key=sort_key)
    return hits


def _target_files(db: Database, entity_type: str, file_role: str) -> list[dict[str, Any]]:
    """Manifest files in scope; superseded and retired entities are excluded."""
    rows = db.conn.execute(
        "SELECT * FROM files WHERE file_role=? AND entity_type=? AND NOT EXISTS ("
        "SELECT 1 FROM entity_supersessions s WHERE s.object_type=files.entity_type "
        "AND s.object_id=files.entity_id) AND NOT EXISTS ("
        "SELECT 1 FROM effective_retired_entities r WHERE r.entity_type=files.entity_type "
        "AND r.entity_id=files.entity_id) ORDER BY file_id",
        (file_role, entity_type),
    ).fetchall()
    return [dict(row) for row in rows]


def _decide(
        rules: list[dict[str, Any]],
        source_hits: dict[str, dict[str, list[dict[str, Any]]]],
        seqid: str,
) -> tuple[str, dict[str, Any]] | None:
    """First matching rule wins; ``None`` leaves the sequence unlabeled."""
    for index, rule in enumerate(rules):
        if rule["default"]:
            return rule["label"], {"rule_index": index, "default": True}
        rows = source_hits[rule["source"]].get(seqid, [])
        if rule["absent"]:
            if not rows:
                return rule["label"], {
                    "rule_index": index, "source": rule["source"], "absent": True,
                }
            continue
        if not rows:
            continue
        best = rows[0]
        if all(_condition_holds(best, condition) for condition in rule["when"]):
            return rule["label"], {
                "rule_index": index,
                "source": rule["source"],
                "job_id": best.get("job_id"),
                "alignment_id": best.get("alignment_id"),
                "subject_id": best.get("subject_id"),
                "observed": best,
            }
    return None


def classify_sequences(
        db: Database,
        project: Project,
        *,
        profile_name: str,
        command: str,
) -> dict[str, Any]:
    """Label every sequence of the profile's target files in one transaction.

    Re-running with the same profile content and the same inputs changes
    nothing and appends no ``changes`` rows; a changed profile rewrites the
    affected labels and audits each change.
    """
    profile = load_profile(project.profiles_dir, profile_name, expected_kind=CLASSIFICATION_KIND)
    spec = validate_classification_profile(profile, profile_name)
    profile_document = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    profile_sha256 = hashlib.sha256(profile_document.encode("utf-8")).hexdigest()
    profile_version = int(profile.get("version", 1))

    run = start_run(db, {
        "step": "classify-sequences",
        "command": command,
        "tool": "operon",
        "parameter_set": json.dumps(
            {"profile": profile_name, "profile_version": profile_version,
             "profile_sha256": profile_sha256},
            ensure_ascii=False, sort_keys=True,
        ),
    })
    try:
        files = _target_files(db, spec["entity_type"], spec["file_role"])
        assignments: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        sequences_total = 0
        files_without_sequences = 0
        for file_row in files:
            seqids = [
                str(row["seqid"]) for row in db.conn.execute(
                    "SELECT seqid FROM sequences WHERE file_id=? ORDER BY seqid",
                    (file_row["file_id"],),
                ).fetchall()
            ]
            if not seqids:
                files_without_sequences += 1
                continue
            sequences_total += len(seqids)
            source_hits = {
                name: _source_hits(db, file_row["file_id"], source)
                for name, source in spec["sources"].items()
            }
            for seqid in seqids:
                decision = _decide(spec["rules"], source_hits, seqid)
                if decision is not None:
                    assignments[(file_row["file_id"], seqid)] = decision

        decided_at = now_iso()
        target_file_ids = {file_row["file_id"] for file_row in files}
        existing = {
            (str(row["file_id"]), str(row["seqid"])): dict(row)
            for row in db.conn.execute(
                "SELECT * FROM sequence_labels WHERE profile_name=?", (profile_name,),
            ).fetchall()
            if str(row["file_id"]) in target_file_ids
        }

        written = removed = unchanged = 0
        reason = f"classify-sequences profile {profile_name}"
        with db.transaction():
            db.record_profile(
                profile_name, profile_version, profile_sha256, profile_document, decided_at
            )
            for key in sorted(assignments):
                label, details = assignments[key]
                details_json = json.dumps(details, ensure_ascii=False, sort_keys=True)
                previous = existing.get(key)
                if previous is not None and previous["label"] == label \
                        and previous["details_json"] == details_json:
                    # The decision stands; only refresh the provenance hash
                    # when the profile content changed. No audit row: the
                    # label itself did not change.
                    if previous["profile_sha256"] != profile_sha256:
                        db.conn.execute(
                            "UPDATE sequence_labels SET profile_sha256=? "
                            "WHERE file_id=? AND seqid=? AND profile_name=?",
                            (profile_sha256, key[0], key[1], profile_name),
                        )
                    unchanged += 1
                    continue
                db.conn.execute(
                    "INSERT INTO sequence_labels "
                    "(file_id, seqid, label, profile_name, profile_sha256, details_json, decided_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(file_id, seqid, profile_name) DO UPDATE SET "
                    "label=excluded.label, profile_sha256=excluded.profile_sha256, "
                    "details_json=excluded.details_json, decided_at=excluded.decided_at",
                    (key[0], key[1], label, profile_name, profile_sha256,
                     details_json, decided_at),
                )
                db.record_change(
                    "sequence_label", f"{key[0]}:{key[1]}:{profile_name}", "label",
                    previous["label"] if previous is not None else None, label,
                    reason=reason, workflow_run_id=run["run_id"],
                )
                written += 1
            for key in sorted(set(existing) - set(assignments)):
                db.conn.execute(
                    "DELETE FROM sequence_labels WHERE file_id=? AND seqid=? AND profile_name=?",
                    (key[0], key[1], profile_name),
                )
                db.record_change(
                    "sequence_label", f"{key[0]}:{key[1]}:{profile_name}", "label",
                    existing[key]["label"], None,
                    reason=reason, workflow_run_id=run["run_id"],
                )
                removed += 1
    except Exception as exc:
        finish_run(db, project, run["run_id"], status="failed", exit_code=1, error=str(exc))
        raise

    label_counts: dict[str, int] = {}
    for label, _details in assignments.values():
        label_counts[label] = label_counts.get(label, 0) + 1
    execution_details = {
        "profile": profile_name,
        "profile_version": profile_version,
        "profile_sha256": profile_sha256,
        "files": len(files),
        "files_without_sequences": files_without_sequences,
        "sequences": sequences_total,
        "label_counts": dict(sorted(label_counts.items())),
        "unlabeled": sequences_total - len(assignments),
        "labels_written": written,
        "labels_removed": removed,
        "labels_unchanged": unchanged,
    }
    finish_run(
        db, project, run["run_id"], status="completed", exit_code=0,
        execution_details=json.dumps(execution_details, ensure_ascii=False, sort_keys=True),
    )
    return {
        "profile": profile_name,
        "profile_sha256": profile_sha256,
        "files": len(files),
        "sequences": sequences_total,
        "label_counts": dict(sorted(label_counts.items())),
        "unlabeled": execution_details["unlabeled"],
        "labels_written": written,
        "labels_removed": removed,
        "run_id": run["run_id"],
    }
