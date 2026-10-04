"""Validate and import independent parser evidence without recomputing it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from operon.contracts import read_json, validate_payload
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.utils import now_iso, sha256_file
from operon.workflow import flush_run_log, log_run

EVIDENCE_COLUMNS = {
    "analysis_results": (
        "metric_name",
        "metric_value",
        "metric_numeric",
        "metric_unit",
    ),
    "analysis_hits": (
        "query_id",
        "subject_id",
        "hit_rank",
        "metric_name",
        "metric_value",
        "metric_numeric",
        "metric_unit",
    ),
    "analysis_alignments": (
        "query_id",
        "subject_id",
        "hit_rank",
        "query_start",
        "query_end",
        "subject_start",
        "subject_end",
        "evalue",
        "bitscore",
        "percent_identity",
        "extra_json",
    ),
}
EVIDENCE_KEYS = {
    "analysis_results": ("metric_name",),
    "analysis_hits": ("query_id", "hit_rank", "metric_name"),
    "analysis_alignments": ("query_id", "hit_rank"),
}


def canonical_hash(value: Any) -> str:
    """Hash canonical JSON evidence independently of envelope formatting."""
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def validate_file_identity(db: Database, info: dict[str, Any]) -> dict[str, Any]:
    """Resolve a complete manifest identity and require an active owner."""
    row = db.conn.execute(
        "SELECT * FROM files WHERE file_id=?", (info["file_id"],)
    ).fetchone()
    if row is None:
        raise ValidationError(f"unknown file_id {info['file_id']}")
    if (
        str(row["sha256"]).lower() != info["sha256"].lower()
        or row["size_bytes"] != info["size_bytes"]
    ):
        raise ValidationError(
            f"file identity does not match manifest for {info['file_id']}"
        )
    db.require_active_entity(row["entity_type"], row["entity_id"])
    return dict(row)


def workflow_details(
    db: Database, run_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the producing workflow and preserve all unrelated execution details."""
    row = db.conn.execute(
        "SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise ValidationError(f"unknown workflow run {run_id!r}")
    if row["status"] not in {"completed", "adopted"}:
        raise ValidationError(f"workflow run {run_id!r} is not completed")
    try:
        details = json.loads(row["execution_details"] or "{}")
    except ValueError as exc:
        raise ValidationError(f"invalid execution_details for {run_id}") from exc
    if not isinstance(details, dict):
        raise ValidationError(f"execution_details for {run_id} must be an object")
    return dict(row), details


def _prepare(db: Database, source: str | Path) -> dict[str, Any]:
    payload = read_json(source)
    validate_payload(payload, "hits")
    file = validate_file_identity(db, payload["file"])
    job = db.conn.execute(
        "SELECT * FROM analysis_jobs WHERE job_id=?", (payload["job_id"],)
    ).fetchone()
    if job is None or job["status"] != "completed":
        raise ValidationError("hits import requires an existing completed analysis job")
    if (
        job["file_id"] != file["file_id"]
        or job["input_sha256"].lower() != file["sha256"].lower()
        or job["entity_type"] != file["entity_type"]
        or job["entity_id"] != file["entity_id"]
    ):
        raise ValidationError(
            "analysis job input identity does not match the manifest file"
        )
    parent, details = workflow_details(db, job["workflow_run_id"])
    evidence: dict[str, Any] = {}
    subjects: dict[tuple[str, int], str] = {}
    for table, section in (
        ("analysis_results", "results"),
        ("analysis_hits", "hits"),
        ("analysis_alignments", "alignments"),
    ):
        columns = EVIDENCE_COLUMNS[table]
        rows = []
        keys = set()
        for item in payload[section]:
            row = {column: item.get(column) for column in columns}
            for column in ("metric_numeric", "evalue", "bitscore", "percent_identity"):
                if column in row and row[column] is not None:
                    row[column] = float(row[column])
            if table == "analysis_alignments":
                row["extra_json"] = (
                    json.dumps(item["extra"], sort_keys=True, ensure_ascii=False)
                    if item.get("extra")
                    else None
                )
            key = tuple(row[c] for c in EVIDENCE_KEYS[table])
            if key in keys:
                raise ValidationError(f"duplicate {section} key {key!r}")
            keys.add(key)
            if section != "results":
                rank_key = (row["query_id"], row["hit_rank"])
                if rank_key in subjects and subjects[rank_key] != row["subject_id"]:
                    raise ValidationError(
                        f"inconsistent subject for query/rank {rank_key!r}"
                    )
                subjects[rank_key] = row["subject_id"]
            rows.append(row)
        evidence[table] = sorted(
            rows, key=lambda row: tuple(row[c] for c in EVIDENCE_KEYS[table])
        )
    existing = {}
    for table, columns in EVIDENCE_COLUMNS.items():
        existing[table] = sorted(
            [
                {c: row[c] for c in columns}
                for row in db.conn.execute(
                    f"SELECT * FROM {table} WHERE job_id=?", (job["job_id"],)
                )
            ],
            key=lambda row: tuple(row[c] for c in EVIDENCE_KEYS[table]),
        )
    digest = canonical_hash(evidence)
    ledger = details.get("hits_imports", {})
    if not isinstance(ledger, dict):
        raise ValidationError("hits_imports ledger must be an object")
    previous = ledger.get(str(job["job_id"]))
    populated = any(existing.values()) or previous is not None
    if populated and (
        existing != evidence or (previous is not None and previous != digest)
    ):
        raise ConflictError(
            f"different evidence already exists for job {job['job_id']}"
        )
    return {
        "job": dict(job),
        "parent": parent,
        "details": details,
        "evidence": evidence,
        "sha256": digest,
        "unchanged": populated,
    }


def plan_hits_import(db: Database, source: str | Path) -> dict[str, Any]:
    """Validate identities, row keys and conflicts without any write."""
    plan = _prepare(db, source)
    return {
        "job_id": plan["job"]["job_id"],
        "unchanged": plan["unchanged"],
        "result_count": len(plan["evidence"]["analysis_results"]),
        "hit_count": len(plan["evidence"]["analysis_hits"]),
        "alignment_count": len(plan["evidence"]["analysis_alignments"]),
    }


def import_hits(db: Database, project, source: str | Path) -> dict[str, Any]:
    """Atomically append supplied evidence and linked provenance; identical is a no-op."""
    buffer: list[dict[str, Any]] = []
    with db.transaction():
        plan = _prepare(db, source)
        job = plan["job"]
        if plan["unchanged"]:
            return {"job_id": job["job_id"], "unchanged": True}
        base = {
            c: job[c]
            for c in ("job_id", "entity_type", "entity_id", "file_id", "analysis_name")
        }
        for table, rows in plan["evidence"].items():
            for row in rows:
                data = {**base, **row}
                columns = list(data)
                db.conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    list(data.values()),
                )
        details = plan["details"]
        details.setdefault("hits_imports", {})[str(job["job_id"])] = plan["sha256"]
        db.conn.execute(
            "UPDATE workflow_runs SET execution_details=? WHERE run_id=?",
            (
                json.dumps(details, sort_keys=True, ensure_ascii=False),
                job["workflow_run_id"],
            ),
        )
        record = log_run(
            db,
            project,
            {
                "parent_run_id": job["workflow_run_id"],
                "entity_type": job["entity_type"],
                "entity_id": job["entity_id"],
                "step": "import-hits",
                "status": "completed",
                "tool": "operon",
                "input_sha256": sha256_file(source),
                "execution_details": json.dumps(
                    {
                        "job_id": job["job_id"],
                        "payload_sha256": plan["sha256"],
                        "source": str(source),
                    },
                    sort_keys=True,
                ),
                "finished_at": now_iso(),
            },
            jsonl_buffer=buffer,
        )
        db.record_change(
            "analysis_jobs",
            str(job["job_id"]),
            "evidence",
            None,
            plan["sha256"],
            reason="import independent parser evidence",
            evidence=str(source),
            workflow_run_id=record["run_id"],
        )
    flush_run_log(project, buffer)
    return {"job_id": job["job_id"], "unchanged": False, "run_id": record["run_id"]}
