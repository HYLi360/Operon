"""Audited reconciliation for development-era NCBI adapter anomalies."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.errors import ConflictError
from operon.utils import now_iso
from operon.workflow import finish_run, new_run_id, start_run


_ACCESSION_RE = re.compile(r"GC[AF]_\d+(?:\.\d+)?", re.IGNORECASE)
_EARLY_STATES = {
    "DISCOVERED", "METADATA_FETCHED", "METADATA_VALIDATED", "DOWNLOAD_PENDING",
    "DOWNLOADED", "DOWNLOAD_FAILED", "CHECKSUM_VERIFIED", "CHECKSUM_FAILED",
    "STANDARDIZED",
}


def _annotation_files(db: Database, annotation_id: str) -> dict[str, dict[str, Any]]:
    return {
        str(row["file_role"]): dict(row)
        for row in db.conn.execute(
            "SELECT file_id, file_role, sha256, size_bytes FROM files "
            "WHERE entity_type='annotation' AND entity_id=?",
            (annotation_id,),
        )
    }


def _reference_score(db: Database, annotation_id: str) -> tuple[int, int, int]:
    qc = int(db.conn.execute(
        "SELECT COUNT(*) FROM qc_results WHERE entity_type='annotation' AND entity_id=?",
        (annotation_id,),
    ).fetchone()[0])
    analysis = int(db.conn.execute(
        "SELECT COUNT(*) FROM analysis_jobs WHERE entity_type='annotation' AND entity_id=?",
        (annotation_id,),
    ).fetchone()[0])
    releases = int(db.conn.execute(
        "SELECT COUNT(*) FROM release_members WHERE entity_type='annotation' AND entity_id=?",
        (annotation_id,),
    ).fetchone()[0])
    file_count = len(_annotation_files(db, annotation_id))
    number = int(str(annotation_id).rsplit("_", 1)[-1])
    return qc + analysis + releases, file_count, -number


def _compatible_duplicate(db: Database, canonical: str, duplicate: str) -> bool:
    left = _annotation_files(db, canonical)
    right = _annotation_files(db, duplicate)
    for role in set(left) & set(right):
        if (
            str(left[role]["sha256"]).lower() != str(right[role]["sha256"]).lower()
            or int(left[role]["size_bytes"]) != int(right[role]["size_bytes"])
        ):
            return False
    return True


def plan_ncbi_reconciliation(db: Database) -> dict[str, Any]:
    """Build a conservative, database-only repair plan without changing state."""
    plan: dict[str, Any] = {
        "annotation_supersessions": [],
        "assembly_updates": [],
        "file_role_updates": [],
        "accession_primary_updates": [],
        "state_restorations": [],
        "warnings": [],
    }

    has_supersessions = db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_supersessions'"
    ).fetchone() is not None
    superseded_ids: set[str] = (
        {
            str(row["object_id"])
            for row in db.conn.execute(
                "SELECT object_id FROM entity_supersessions WHERE object_type='annotation'"
            )
        }
        if has_supersessions else set()
    )
    groups: dict[tuple[Any, ...], list[str]] = defaultdict(list)
    for row in db.conn.execute(
        "SELECT annotation_id, assembly_id, annotation_source, annotation_version, annotation_date "
        "FROM annotations ORDER BY annotation_id"
    ):
        if str(row["annotation_id"]) in superseded_ids:
            continue
        key = (
            row["assembly_id"], str(row["annotation_source"] or "").strip().casefold(),
            int(row["annotation_version"] or 1), str(row["annotation_date"] or ""),
        )
        groups[key].append(str(row["annotation_id"]))
    for key, annotation_ids in groups.items():
        if len(annotation_ids) < 2:
            continue
        canonical = max(annotation_ids, key=lambda value: _reference_score(db, value))
        for duplicate in annotation_ids:
            if duplicate == canonical:
                continue
            if not _compatible_duplicate(db, canonical, duplicate):
                plan["warnings"].append({
                    "kind": "annotation_bytes_differ",
                    "annotation_id": duplicate,
                    "candidate": canonical,
                })
                continue
            superseded_ids.add(duplicate)
            plan["annotation_supersessions"].append({
                "annotation_id": duplicate,
                "superseded_by": canonical,
                "identity": {
                    "assembly_id": key[0], "provider": key[1],
                    "version": key[2], "date": key[3],
                },
            })

    aliases: dict[str, list[str]] = defaultdict(list)
    for row in db.conn.execute(
        "SELECT internal_id, accession FROM accessions WHERE internal_type='assembly' "
        "AND namespace IN ('NCBI_Assembly','NCBI_GenBank_Assembly','NCBI_RefSeq_Assembly')"
    ):
        value = str(row["accession"]).upper()
        if value not in aliases[str(row["internal_id"])]:
            aliases[str(row["internal_id"])].append(value)
    for assembly_id, values in aliases.items():
        gcf = sorted(value for value in values if value.startswith("GCF_"))
        gca = sorted(value for value in values if value.startswith("GCA_"))
        if not gcf or not gca:
            continue
        assembly = db.conn.execute(
            "SELECT assembly_accession, source_database, fasta_file_id FROM assemblies "
            "WHERE assembly_id=?",
            (assembly_id,),
        ).fetchone()
        current_accession = str(assembly["assembly_accession"] or "").upper() if assembly else ""
        historical_accession = ""
        for file_row in db.conn.execute(
            "SELECT f.file_id, f.source_url FROM files f LEFT JOIN annotations an "
            "ON f.entity_type='annotation' AND f.entity_id=an.annotation_id "
            "WHERE (f.entity_type='assembly' AND f.entity_id=?) "
            "OR (f.entity_type='annotation' AND an.assembly_id=?) ORDER BY f.file_id",
            (assembly_id, assembly_id),
        ):
            matches = _ACCESSION_RE.findall(str(file_row["source_url"] or ""))
            candidate = matches[-1].upper() if matches else ""
            if candidate in values:
                historical_accession = candidate
                break
        canonical = (
            historical_accession
            or (current_accession if current_accession in values else "")
            or gcf[-1]
        )
        canonical_database = "RefSeq" if canonical.startswith("GCF_") else "GenBank"
        if assembly and (
            current_accession != canonical
            or str(assembly["source_database"] or "") != canonical_database
        ):
            plan["assembly_updates"].append({
                "assembly_id": assembly_id,
                "old_accession": assembly["assembly_accession"],
                "new_accession": canonical,
                "old_source_database": assembly["source_database"],
                "new_source_database": canonical_database,
            })
        for row in db.conn.execute(
            "SELECT file_id, file_role, source_url, sha256 FROM files "
            "WHERE entity_type='assembly' AND entity_id=? "
            "AND file_role IN ('genome_fasta','assembly_report')",
            (assembly_id,),
        ):
            matches = _ACCESSION_RE.findall(str(row["source_url"] or ""))
            source_accession = matches[-1].upper() if matches else ""
            if not source_accession or source_accession == canonical:
                continue
            suffix = "refseq" if source_accession.startswith("GCF_") else "genbank"
            new_role = f"{row['file_role']}_{suffix}"
            conflict = db.conn.execute(
                "SELECT file_id, sha256 FROM files WHERE entity_type='assembly' AND entity_id=? "
                "AND file_role=? AND file_id<>? LIMIT 1",
                (assembly_id, new_role, row["file_id"]),
            ).fetchone()
            if conflict and str(conflict["sha256"]).lower() != str(row["sha256"]).lower():
                plan["warnings"].append({
                    "kind": "alternate_role_conflict", "file_id": row["file_id"],
                    "existing_file_id": conflict["file_id"], "role": new_role,
                })
                continue
            plan["file_role_updates"].append({
                "file_id": row["file_id"], "assembly_id": assembly_id,
                "old_role": row["file_role"], "new_role": new_role,
                "source_accession": source_accession,
                "clear_fasta_link": bool(
                    row["file_role"] == "genome_fasta"
                    and assembly and assembly["fasta_file_id"] == row["file_id"]
                ),
            })
        for value in values:
            generic = db.conn.execute(
                "SELECT is_primary FROM accessions WHERE namespace='NCBI_Assembly' AND accession=?",
                (value,),
            ).fetchone()
            desired = 1 if value == canonical else 0
            if generic is not None and int(generic["is_primary"] or 0) != desired:
                plan["accession_primary_updates"].append({
                    "namespace": "NCBI_Assembly",
                    "accession": value,
                    "is_primary": desired,
                })

    for row in db.conn.execute(
        "SELECT e.entity_id, e.state FROM entity_state e WHERE e.entity_type='annotation' "
        "AND EXISTS (SELECT 1 FROM qc_results q WHERE q.entity_type='annotation' "
        "AND q.entity_id=e.entity_id)"
    ):
        annotation_id = str(row["entity_id"])
        if annotation_id in superseded_ids or str(row["state"]) not in _EARLY_STATES:
            continue
        plan["state_restorations"].append({
            "annotation_id": annotation_id,
            "old_state": row["state"],
            "new_state": "QC_COMPLETE",
        })
    plan["summary"] = {
        key: len(value) for key, value in plan.items() if isinstance(value, list)
    }
    return plan


def apply_ncbi_reconciliation(
    db: Database,
    project: Project,
    *,
    actor: str | None = None,
) -> dict[str, Any]:
    """Apply a freshly computed conservative plan as one audited repair run."""
    from operon.adapters.ncbi_datasets import _adapter_schema

    plan = plan_ncbi_reconciliation(db)
    blocking = [item for item in plan["warnings"] if item["kind"] == "alternate_role_conflict"]
    if blocking:
        raise ConflictError(
            "NCBI reconciliation has alternate-role byte conflicts; review the dry-run plan first"
        )
    actor = actor or os.environ.get("USER")
    _adapter_schema(project, persist=True)
    run_id = new_run_id()
    plan_sha256 = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    start_run(db, {
        "run_id": run_id,
        "step": "ncbi_datasets_reconcile",
        "status": "running",
        "started_at": now_iso(),
        "tool": "Operon NCBI reconciliation",
        "input_sha256": plan_sha256,
        "command": "operon ncbi-reconcile --apply",
    })
    try:
        with db.transaction():
            for item in plan["annotation_supersessions"]:
                inserted = db.supersede_entity(
                    "annotation", item["annotation_id"], "annotation", item["superseded_by"],
                    reason="identical NCBI annotation identity reconciled without deleting history",
                    evidence=json.dumps(item["identity"], ensure_ascii=False, sort_keys=True),
                    workflow_run_id=run_id,
                )
                if inserted:
                    db.record_change(
                        "annotation", item["annotation_id"], "superseded_by", None,
                        item["superseded_by"], "NCBI annotation reconciliation",
                        evidence=json.dumps(item["identity"], ensure_ascii=False, sort_keys=True),
                        actor=actor, workflow_run_id=run_id,
                    )
            for item in plan["assembly_updates"]:
                db.conn.execute(
                    "UPDATE assemblies SET assembly_accession=?, source_database=? WHERE assembly_id=?",
                    (item["new_accession"], item["new_source_database"], item["assembly_id"]),
                )
                for field, old_key, new_key in (
                    ("assembly_accession", "old_accession", "new_accession"),
                    ("source_database", "old_source_database", "new_source_database"),
                ):
                    db.record_change(
                        "assemblies", item["assembly_id"], field, item[old_key], item[new_key],
                        "restore stable canonical accession for paired GCA/GCF",
                        actor=actor, workflow_run_id=run_id,
                    )
            for item in plan["file_role_updates"]:
                db.conn.execute(
                    "UPDATE files SET file_role=? WHERE file_id=?",
                    (item["new_role"], item["file_id"]),
                )
                if item["clear_fasta_link"]:
                    db.conn.execute(
                        "UPDATE assemblies SET fasta_file_id=NULL WHERE assembly_id=? AND fasta_file_id=?",
                        (item["assembly_id"], item["file_id"]),
                    )
                db.record_change(
                    "files", item["file_id"], "file_role", item["old_role"], item["new_role"],
                    "preserve paired GCA/GCF source-specific assembly artifact",
                    evidence=item["source_accession"], actor=actor, workflow_run_id=run_id,
                )
            for item in plan["accession_primary_updates"]:
                row = db.conn.execute(
                    "SELECT is_primary FROM accessions WHERE namespace=? AND accession=?",
                    (item["namespace"], item["accession"]),
                ).fetchone()
                if row is None or int(row["is_primary"] or 0) == item["is_primary"]:
                    continue
                db.conn.execute(
                    "UPDATE accessions SET is_primary=? WHERE namespace=? AND accession=?",
                    (item["is_primary"], item["namespace"], item["accession"]),
                )
                db.record_change(
                    "accessions", f"{item['namespace']}:{item['accession']}", "is_primary",
                    row["is_primary"], item["is_primary"],
                    "make paired GCA/GCF generic canonical mapping deterministic",
                    actor=actor, workflow_run_id=run_id,
                )
            for item in plan["state_restorations"]:
                db.conn.execute(
                    "UPDATE entity_state SET state=?, message=?, updated_at=? "
                    "WHERE entity_type='annotation' AND entity_id=?",
                    (
                        item["new_state"], "restored from existing QC evidence by NCBI reconciliation",
                        now_iso(), item["annotation_id"],
                    ),
                )
                db.record_change(
                    "entity_state", f"annotation:{item['annotation_id']}", "state",
                    item["old_state"], item["new_state"],
                    "restore state from existing QC results after idempotent re-import downgrade",
                    actor=actor, workflow_run_id=run_id,
                )
        db.record_change(
            "adapter_repair", run_id, None, None,
            json.dumps(plan["summary"], ensure_ascii=False, sort_keys=True),
            "NCBI Datasets reconciliation applied", actor=actor,
            workflow_run_id=run_id,
        )
        finish_run(
            db, project, run_id, status="completed", exit_code=0,
            output_sha256=plan_sha256,
            execution_details=json.dumps(plan, ensure_ascii=False, sort_keys=True),
        )
    except Exception as exc:
        finish_run(db, project, run_id, status="failed", exit_code=1, error=str(exc))
        raise
    return {"run_id": run_id, "plan_sha256": plan_sha256, **plan}
