"""Deterministic taxonomy coverage reports against compiled NCBI denominators."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from operon import __version__
from operon.config import Project, project_rel
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.schema import read_tsv, write_tsv
from operon.taxonomy import (
    _validate_coverage_profile,
    _validate_reference_provenance,
    canonical_document,
)
from operon.utils import now_iso, sha256_file
from operon.workflow import log_run, new_run_id

COVERAGE_REPORT_SCHEMA = "operon-taxonomy-coverage-report-1"
COVERAGE_ALGORITHM_VERSION = "1"
RANK_ORDER = {"family": 0, "genus": 1}
ACCEPTED_FILE_TYPES = {"organism", "sample", "run", "assembly", "annotation"}


def _hash_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunks(values: list[int], size: int = 400) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _load_reference_set(
    db: Database, project: Project, reference_set_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    row = db.conn.execute(
        "SELECT * FROM taxonomy_reference_sets WHERE reference_set_id=?", (reference_set_id,)
    ).fetchone()
    if not row:
        raise ValidationError(f"taxonomy reference set {reference_set_id!r} not found")
    reference = dict(row)
    path = project.root / reference["relative_path"]
    if not path.is_file():
        raise ValidationError(f"reference set file is missing: {path}")
    current_sha = sha256_file(path)
    if current_sha != reference["tsv_sha256"] or path.stat().st_size != reference["tsv_size_bytes"]:
        raise ValidationError(f"reference set checksum or size mismatch: {path}")
    rows = read_tsv(path, required_header=["rank", "taxid", "scientific_name"])
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for raw in rows:
        rank = str(raw["rank"]).lower()
        if rank not in RANK_ORDER:
            raise ValidationError(f"reference set has unsupported rank {rank!r}")
        try:
            taxid = int(raw["taxid"])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"reference set has invalid TaxID {raw['taxid']!r}") from exc
        key = (rank, taxid)
        if key in seen:
            raise ValidationError(f"reference set contains duplicate target {rank}:{taxid}")
        seen.add(key)
        targets.append({"rank": rank, "taxid": taxid, "scientific_name": str(raw["scientific_name"])})
    targets.sort(key=lambda item: (RANK_ORDER[item["rank"]], item["taxid"]))
    profile = json.loads(reference["profile_document"])
    profile_document, profile_sha = canonical_document(profile)
    if profile_document != reference["profile_document"] or profile_sha != reference["profile_sha256"]:
        raise ValidationError(f"reference set {reference_set_id} has inconsistent frozen profile identity")
    parsed = _validate_coverage_profile(reference["profile_name"], profile)
    expected_counts = {
        "family": reference["family_count"],
        "genus": reference["genus_count"],
    }
    for rank in parsed["ranks"]:
        actual = sum(1 for item in targets if item["rank"] == rank)
        if actual == 0:
            raise ValidationError(f"reference set denominator is empty for rank {rank}")
        if actual != int(expected_counts[rank]):
            raise ValidationError(f"reference set {rank} row count does not match its database record")
    snapshot_row = db.conn.execute(
        "SELECT * FROM taxonomy_snapshots WHERE taxonomy_snapshot_id=? AND status='READY'",
        (reference["taxonomy_snapshot_id"],),
    ).fetchone()
    if not snapshot_row:
        raise ValidationError(f"taxonomy snapshot {reference['taxonomy_snapshot_id']} is unavailable")
    snapshot = dict(snapshot_row)
    _validate_reference_provenance(
        path.with_suffix(".provenance.json"),
        reference_set_id=reference_set_id,
        taxonomy_snapshot_id=reference["taxonomy_snapshot_id"],
        taxonomy_version=reference["taxonomy_version"],
        taxonomy_source_sha256=snapshot["source_sha256"],
        profile_sha256=reference["profile_sha256"],
        tsv_sha256=reference["tsv_sha256"],
        tsv_size_bytes=reference["tsv_size_bytes"],
    )
    return reference, targets, profile, parsed


def _metadata_scope(db: Database) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    rows = [dict(row) for row in db.conn.execute(
        "SELECT organism_id, scientific_name, taxon_id, taxonomy_source "
        "FROM organisms o WHERE NOT EXISTS ("
        "SELECT 1 FROM effective_retired_entities r "
        "WHERE r.entity_type='organism' AND r.entity_id=o.organism_id) "
        "ORDER BY organism_id"
    ).fetchall()]
    identity_rows = [
        [
            row["organism_id"], row.get("scientific_name"),
            row.get("taxonomy_source"), row.get("taxon_id"),
        ]
        for row in rows
    ]
    membership_sha = _hash_json(identity_rows)
    for row in rows:
        row.update({
            "release_version": None,
            "member_entity_ids": "",
            "file_ids": "",
            "file_sha256s": "",
        })
    return rows, membership_sha, {"organism_count": len(rows)}


def _indexed(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(row[key]): row for row in rows}


def _release_scope(
    db: Database, project: Project, release_version: str
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    release_row = db.conn.execute("SELECT * FROM releases WHERE version=?", (release_version,)).fetchone()
    if not release_row:
        raise ValidationError(f"release {release_version!r} not found")
    release = dict(release_row)
    root = Path(release["path"])
    if not root.is_absolute():
        root = project.root / root
    if not root.is_dir():
        raise ValidationError(f"release directory is missing: {root}")
    manifest_path = root / "manifest.tsv"
    if not manifest_path.is_file() or sha256_file(manifest_path) != release["manifest_sha256"]:
        raise ValidationError(f"release manifest checksum mismatch: {manifest_path}")
    manifest = read_tsv(manifest_path)
    db_members = [dict(row) for row in db.conn.execute(
        "SELECT file_id, entity_type, entity_id, sha256, size_bytes FROM release_members "
        "WHERE release_version=? ORDER BY file_id",
        (release_version,),
    ).fetchall()]
    manifest_identity = sorted(
        (
            str(row["file_id"]), str(row["entity_type"]), str(row["entity_id"]),
            str(row["sha256"]), int(row["size_bytes"]),
        )
        for row in manifest
    )
    database_identity = sorted(
        (
            str(row["file_id"]), str(row["entity_type"]), str(row["entity_id"]),
            str(row["sha256"]), int(row["size_bytes"]),
        )
        for row in db_members
    )
    if manifest_identity != database_identity:
        raise ValidationError(f"release {release_version} manifest and release_members disagree")

    table_names = ["organisms", "samples", "runs", "assemblies", "annotations"]
    tables: dict[str, list[dict[str, Any]]] = {}
    metadata_hashes: dict[str, str] = {}
    try:
        release_summary = json.loads(str(release["summary"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"release {release_version} has invalid summary provenance") from exc
    expected_metadata_hashes = release_summary.get("metadata_sha256")
    if not isinstance(expected_metadata_hashes, dict):
        raise ValidationError(
            f"release {release_version} predates frozen metadata checksums; recreate it "
            "before calculating release-scope taxonomy coverage"
        )
    for table in table_names:
        path = root / f"{table}.tsv"
        if not path.is_file():
            raise ValidationError(f"release metadata snapshot is missing: {path}")
        tables[table] = read_tsv(path)
        metadata_hashes[table] = sha256_file(path)
        expected_sha = expected_metadata_hashes.get(f"{table}.tsv")
        if metadata_hashes[table] != expected_sha:
            raise ValidationError(f"release metadata checksum mismatch: {path}")
    organisms = _indexed(tables["organisms"], "organism_id")
    samples = _indexed(tables["samples"], "sample_id")
    runs = _indexed(tables["runs"], "run_id")
    assemblies = _indexed(tables["assemblies"], "assembly_id")
    annotations = _indexed(tables["annotations"], "annotation_id")

    def organism_for(member: dict[str, Any]) -> str:
        entity_type = str(member["entity_type"])
        entity_id = str(member["entity_id"])
        if entity_type == "organism":
            return entity_id
        if entity_type == "sample":
            sample = samples.get(entity_id)
            return str(sample["organism_id"]) if sample else ""
        if entity_type == "run":
            run = runs.get(entity_id)
            sample = samples.get(str(run["sample_id"])) if run else None
            return str(sample["organism_id"]) if sample else ""
        if entity_type == "assembly":
            assembly = assemblies.get(entity_id)
            sample = samples.get(str(assembly["sample_id"])) if assembly else None
            return str(sample["organism_id"]) if sample else ""
        if entity_type == "annotation":
            annotation = annotations.get(entity_id)
            assembly = assemblies.get(str(annotation["assembly_id"])) if annotation else None
            sample = samples.get(str(assembly["sample_id"])) if assembly else None
            return str(sample["organism_id"]) if sample else ""
        return ""

    evidence_by_organism: dict[str, list[dict[str, Any]]] = {}
    for member in db_members:
        if member["entity_type"] not in ACCEPTED_FILE_TYPES:
            raise ValidationError(
                f"release member {member['file_id']} has unsupported entity type {member['entity_type']!r}"
            )
        organism_id = organism_for(member)
        if not organism_id or organism_id not in organisms:
            raise ValidationError(
                f"release member {member['file_id']} cannot be traced to frozen organism metadata"
            )
        evidence_by_organism.setdefault(organism_id, []).append(member)
    observations: list[dict[str, Any]] = []
    for organism_id in sorted(evidence_by_organism):
        organism = organisms[organism_id]
        evidence = sorted(evidence_by_organism[organism_id], key=lambda item: item["file_id"])
        observations.append({
            "organism_id": organism_id,
            "scientific_name": organism.get("scientific_name"),
            "taxon_id": organism.get("taxon_id"),
            "taxonomy_source": organism.get("taxonomy_source"),
            "release_version": release_version,
            "member_entity_ids": ";".join(
                f"{item['entity_type']}:{item['entity_id']}" for item in evidence
            ),
            "file_ids": ";".join(str(item["file_id"]) for item in evidence),
            "file_sha256s": ";".join(str(item["sha256"]) for item in evidence),
        })
    membership_payload = {
        "release_version": release_version,
        "manifest_sha256": release["manifest_sha256"],
        "members": database_identity,
        "metadata_sha256": metadata_hashes,
    }
    membership_sha = _hash_json(membership_payload)
    return observations, membership_sha, {
        "release_version": release_version,
        "release_manifest_sha256": release["manifest_sha256"],
        "release_metadata_sha256": metadata_hashes,
        "release_member_count": len(db_members),
        "organism_count": len(observations),
    }


def _resolve_taxids(
    db: Database, snapshot_id: str, taxids: list[int]
) -> dict[int, tuple[int | None, str]]:
    resolved: dict[int, tuple[int | None, str]] = {}
    for batch in _chunks(sorted(set(taxids))):
        placeholders = ",".join("?" for _ in batch)
        exact = {
            int(row["taxid"])
            for row in db.conn.execute(
                f"SELECT taxid FROM taxonomy_nodes WHERE taxonomy_snapshot_id=? AND taxid IN ({placeholders})",
                [snapshot_id, *batch],
            ).fetchall()
        }
        aliases = {
            int(row["alias_taxid"]): (row["current_taxid"], str(row["status"]))
            for row in db.conn.execute(
                f"SELECT alias_taxid, current_taxid, status FROM taxonomy_aliases "
                f"WHERE taxonomy_snapshot_id=? AND alias_taxid IN ({placeholders})",
                [snapshot_id, *batch],
            ).fetchall()
        }
        for taxid in batch:
            if taxid in exact:
                resolved[taxid] = (taxid, "EXACT")
            elif taxid in aliases:
                current, status = aliases[taxid]
                if current is None or status == "deleted":
                    resolved[taxid] = (None, "DELETED_TAXID")
                else:
                    resolved[taxid] = (int(current), "MAPPED_ALIAS")
            else:
                resolved[taxid] = (None, "UNKNOWN_TAXID")
    return resolved


def _lineages(
    db: Database, snapshot_id: str, resolved_taxids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {taxid: [] for taxid in resolved_taxids}
    for batch in _chunks(sorted(set(resolved_taxids)), size=250):
        values = ",".join("(?)" for _ in batch)
        sql = (
            f"WITH RECURSIVE requested(input_taxid) AS (VALUES {values}), "
            "ancestry(input_taxid, taxid, parent_taxid, rank, scientific_name, is_extinct, depth) AS ("
            "SELECT r.input_taxid, n.taxid, n.parent_taxid, n.rank, n.scientific_name, n.is_extinct, 0 "
            "FROM requested r JOIN taxonomy_nodes n ON n.taxid=r.input_taxid "
            "WHERE n.taxonomy_snapshot_id=? "
            "UNION ALL "
            "SELECT a.input_taxid, p.taxid, p.parent_taxid, p.rank, p.scientific_name, p.is_extinct, a.depth+1 "
            "FROM ancestry a JOIN taxonomy_nodes p ON p.taxid=a.parent_taxid "
            "WHERE p.taxonomy_snapshot_id=? AND a.parent_taxid IS NOT NULL "
            "AND a.parent_taxid<>a.taxid AND a.depth<100) "
            "SELECT * FROM ancestry ORDER BY input_taxid, depth"
        )
        for row in db.conn.execute(sql, [*batch, snapshot_id, snapshot_id]).fetchall():
            result[int(row["input_taxid"])].append(dict(row))
    return result


def _resolve_observations(
    db: Database,
    snapshot_id: str,
    raw_observations: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    parsed_profile: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, int], set[str]]]:
    valid_taxids: list[int] = []
    preliminary_excluded: list[dict[str, Any]] = []
    for observation in raw_observations:
        source = str(observation.get("taxonomy_source") or "")
        raw_taxid = observation.get("taxon_id")
        if source != "NCBI":
            preliminary_excluded.append({**observation, "reason": "UNSUPPORTED_TAXONOMY_SOURCE"})
            continue
        if raw_taxid in {None, ""}:
            preliminary_excluded.append({**observation, "reason": "MISSING_TAXID"})
            continue
        try:
            valid_taxids.append(int(raw_taxid))
        except (TypeError, ValueError):
            preliminary_excluded.append({**observation, "reason": "INVALID_TAXID"})
    resolution = _resolve_taxids(db, snapshot_id, valid_taxids)
    lineage_by_taxid = _lineages(
        db, snapshot_id,
        [current for current, _status in resolution.values() if current is not None],
    )
    target_keys = {(item["rank"], item["taxid"]) for item in targets}
    observed_by_target: dict[tuple[str, int], set[str]] = {key: set() for key in target_keys}
    accepted: list[dict[str, Any]] = []
    excluded = list(preliminary_excluded)
    excluded_subtrees = set(parsed_profile["exclude_subtrees"])
    patterns = parsed_profile["compiled_name_patterns"]
    for observation in raw_observations:
        if str(observation.get("taxonomy_source") or "") != "NCBI":
            continue
        if observation.get("taxon_id") in {None, ""}:
            continue
        try:
            input_taxid = int(observation["taxon_id"])
        except (TypeError, ValueError):
            continue
        current_taxid, mapping_status = resolution[input_taxid]
        if current_taxid is None:
            excluded.append({**observation, "reason": mapping_status})
            continue
        lineage = lineage_by_taxid.get(current_taxid, [])
        if not lineage:
            excluded.append({**observation, "reason": "UNKNOWN_RESOLVED_TAXID"})
            continue
        if parsed_profile["exclude_extinct"] and any(
            item.get("is_extinct") == 1 for item in lineage
        ):
            excluded.append({**observation, "reason": "EXCLUDED_EXTINCT"})
            continue
        if any(
            pattern.search(str(item["scientific_name"]))
            for pattern in patterns
            for item in lineage
        ):
            excluded.append({**observation, "reason": "EXCLUDED_NAME_PATTERN"})
            continue
        ancestor_ids = {int(item["taxid"]) for item in lineage}
        if excluded_subtrees.intersection(ancestor_ids):
            excluded.append({**observation, "reason": "EXCLUDED_SUBTREE"})
            continue
        rank_taxids: dict[str, int | None] = {"family": None, "genus": None}
        for item in lineage:
            rank = str(item["rank"])
            if rank in rank_taxids and rank_taxids[rank] is None:
                rank_taxids[rank] = int(item["taxid"])
        configured_keys = [
            (rank, rank_taxids[rank])
            for rank in parsed_profile["ranks"]
            if rank_taxids[rank] is not None
        ]
        if not configured_keys:
            excluded.append({**observation, "reason": "MISSING_TARGET_RANK"})
            continue
        if not any(key in target_keys for key in configured_keys):
            excluded.append({**observation, "reason": "OUTSIDE_REFERENCE_SCOPE"})
            continue
        organism_id = str(observation["organism_id"])
        for rank, taxid in rank_taxids.items():
            if taxid is not None and (rank, taxid) in observed_by_target:
                observed_by_target[(rank, taxid)].add(organism_id)
        accepted.append({
            "organism_id": organism_id,
            "scientific_name": observation.get("scientific_name"),
            "input_taxid": input_taxid,
            "resolved_taxid": current_taxid,
            "mapping_status": mapping_status,
            "family_taxid": rank_taxids["family"],
            "genus_taxid": rank_taxids["genus"],
            "family_in_reference_set": (
                rank_taxids["family"] is not None
                and ("family", rank_taxids["family"]) in target_keys
            ),
            "genus_in_reference_set": (
                rank_taxids["genus"] is not None
                and ("genus", rank_taxids["genus"]) in target_keys
            ),
            "release_version": observation.get("release_version"),
            "member_entity_ids": observation.get("member_entity_ids", ""),
            "file_ids": observation.get("file_ids", ""),
            "file_sha256s": observation.get("file_sha256s", ""),
        })
    accepted.sort(key=lambda item: item["organism_id"])
    unique_excluded: dict[tuple[str, str], dict[str, Any]] = {}
    for item in excluded:
        unique_excluded[(str(item["organism_id"]), str(item["reason"]))] = item
    excluded_rows = [
        {
            "organism_id": item["organism_id"],
            "scientific_name": item.get("scientific_name"),
            "taxonomy_source": item.get("taxonomy_source"),
            "taxid": item.get("taxon_id"),
            "reason": item["reason"],
            "release_version": item.get("release_version"),
            "member_entity_ids": item.get("member_entity_ids", ""),
            "file_ids": item.get("file_ids", ""),
        }
        for item in sorted(unique_excluded.values(), key=lambda row: (str(row["organism_id"]), str(row["reason"])))
    ]
    return accepted, excluded_rows, observed_by_target


def _percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        raise ValidationError("coverage denominator must be greater than zero")
    value = (Decimal(numerator) * Decimal(100) / Decimal(denominator)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    return float(value)


def _result_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in (
        "coverage_summary.tsv", "coverage_targets.tsv", "coverage_missing.tsv",
        "coverage_observations.tsv", "coverage_excluded_observations.tsv",
    ):
        path = directory / name
        data = path.read_bytes()
        digest.update(name.encode("utf-8") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data)
    return digest.hexdigest()


def _cached_report(db: Database, project: Project, row: dict[str, Any]) -> dict[str, Any]:
    path = project.root / str(row["relative_path"])
    provenance_path = path / "provenance.json"
    if not path.is_dir() or not provenance_path.is_file():
        raise ConflictError(f"cached coverage report files are missing: {path}")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    reference = db.conn.execute(
        "SELECT profile_sha256 FROM taxonomy_reference_sets WHERE reference_set_id=?",
        (row["reference_set_id"],),
    ).fetchone()
    expected_provenance = {
        "schema": COVERAGE_REPORT_SCHEMA,
        "report_id": row["report_id"],
        "reference_set_id": row["reference_set_id"],
        "reference_set_sha256": row["reference_set_sha256"],
        "profile_sha256": reference["profile_sha256"] if reference else None,
        "scope_kind": row["scope_kind"],
        "scope_value": row["scope_value"],
        "scope_membership_sha256": row["scope_membership_sha256"],
        "input_sha256": row["input_sha256"],
        "result_sha256": row["result_sha256"],
        "decision": row["decision"],
    }
    if (
        any(provenance.get(key) != value for key, value in expected_provenance.items())
        or _result_hash(path) != row["result_sha256"]
    ):
        raise ConflictError(f"cached coverage report has changed: {path}")
    metrics = [dict(metric) for metric in db.conn.execute(
        "SELECT * FROM coverage_report_metrics WHERE report_id=? ORDER BY rank",
        (row["report_id"],),
    ).fetchall()]
    return {
        **row,
        "metrics": sorted(metrics, key=lambda item: RANK_ORDER[item["rank"]]),
        "path": str(path),
        "reused": True,
        "exit_code": 0 if row["decision"] == "PASS" else 1,
    }


def _report_coverage_impl(
    db: Database,
    project: Project,
    reference_set_id: str,
    *,
    release_version: str | None = None,
) -> dict[str, Any]:
    """Compute metadata or frozen-release coverage against one compiled denominator."""
    reference, targets, profile, parsed = _load_reference_set(db, project, reference_set_id)
    if release_version:
        scope_kind = "release"
        scope_value = release_version
        raw_observations, membership_sha, scope_details = _release_scope(
            db, project, release_version
        )
    else:
        scope_kind = "metadata"
        scope_value = None
        raw_observations, membership_sha, scope_details = _metadata_scope(db)
    input_payload = {
        "algorithm_version": COVERAGE_ALGORITHM_VERSION,
        "reference_set_id": reference_set_id,
        "reference_set_sha256": reference["tsv_sha256"],
        "profile_sha256": reference["profile_sha256"],
        "scope_kind": scope_kind,
        "scope_value": scope_value,
        "scope_membership_sha256": membership_sha,
    }
    input_sha = _hash_json(input_payload)
    existing = db.conn.execute(
        "SELECT * FROM coverage_reports WHERE input_sha256=?", (input_sha,)
    ).fetchone()
    if existing:
        return _cached_report(db, project, dict(existing))

    accepted, excluded, observed_by_target = _resolve_observations(
        db, reference["taxonomy_snapshot_id"], raw_observations, targets, parsed
    )
    metrics: list[dict[str, Any]] = []
    reason_codes: list[str] = []
    for rank in parsed["ranks"]:
        rank_targets = [target for target in targets if target["rank"] == rank]
        denominator = len(rank_targets)
        numerator = sum(
            1 for target in rank_targets if observed_by_target[(rank, target["taxid"])]
        )
        coverage = _percentage(numerator, denominator)
        threshold = float(parsed["thresholds"][rank])
        decision = "PASS" if coverage >= threshold else "FAIL"
        if decision == "FAIL":
            reason_codes.append(f"{rank.upper()}_COVERAGE_BELOW_THRESHOLD")
        metrics.append({
            "rank": rank,
            "numerator": numerator,
            "denominator": denominator,
            "coverage_percent": coverage,
            "threshold_percent": threshold,
            "decision": decision,
        })
    overall = "PASS" if all(metric["decision"] == "PASS" for metric in metrics) else "FAIL"
    target_rows: list[dict[str, Any]] = []
    for target in targets:
        organisms = observed_by_target[(target["rank"], target["taxid"])]
        target_rows.append({
            **target,
            "status": "COVERED" if organisms else "MISSING",
            "organism_count": len(organisms),
        })
    missing_rows = [
        {key: row[key] for key in ("rank", "taxid", "scientific_name")}
        for row in target_rows if row["status"] == "MISSING"
    ]
    report_id = f"COV_{input_sha[:16].upper()}"
    reports_parent = project.reports_root / "coverage"
    reports_parent.mkdir(parents=True, exist_ok=True)
    final_dir = reports_parent / report_id
    if final_dir.exists():
        raise ConflictError(f"coverage report target already exists without matching history: {final_dir}")
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{report_id}.", dir=str(reports_parent)))
    created_at = now_iso()
    run_id = new_run_id()
    try:
        write_tsv(
            temp_dir / "coverage_summary.tsv",
            ["rank", "numerator", "denominator", "coverage_percent", "min_coverage_percent", "decision"],
            [
                {
                    "rank": item["rank"], "numerator": item["numerator"],
                    "denominator": item["denominator"],
                    "coverage_percent": f"{item['coverage_percent']:.4f}",
                    "min_coverage_percent": f"{item['threshold_percent']:.4f}",
                    "decision": item["decision"],
                }
                for item in metrics
            ],
        )
        write_tsv(
            temp_dir / "coverage_targets.tsv",
            ["rank", "taxid", "scientific_name", "status", "organism_count"], target_rows,
        )
        write_tsv(
            temp_dir / "coverage_missing.tsv",
            ["rank", "taxid", "scientific_name"], missing_rows,
        )
        write_tsv(
            temp_dir / "coverage_observations.tsv",
            [
                "organism_id", "scientific_name", "input_taxid", "resolved_taxid",
                "mapping_status", "family_taxid", "genus_taxid",
                "family_in_reference_set", "genus_in_reference_set", "release_version",
                "member_entity_ids", "file_ids", "file_sha256s",
            ],
            accepted,
        )
        write_tsv(
            temp_dir / "coverage_excluded_observations.tsv",
            [
                "organism_id", "scientific_name", "taxonomy_source", "taxid", "reason",
                "release_version", "member_entity_ids", "file_ids",
            ],
            excluded,
        )
        result_sha = _result_hash(temp_dir)
        provenance = {
            "schema": COVERAGE_REPORT_SCHEMA,
            "report_id": report_id,
            "created_at": created_at,
            "created_by": "operon.coverage",
            "package_version": __version__,
            "algorithm_version": COVERAGE_ALGORITHM_VERSION,
            "reference_set_id": reference_set_id,
            "reference_set_sha256": reference["tsv_sha256"],
            "taxonomy_snapshot_id": reference["taxonomy_snapshot_id"],
            "taxonomy_version": reference["taxonomy_version"],
            "profile_name": reference["profile_name"],
            "profile_version": reference["profile_version"],
            "profile_sha256": reference["profile_sha256"],
            "profile_document": profile,
            "scope_kind": scope_kind,
            "scope_value": scope_value,
            "scope_membership_sha256": membership_sha,
            "scope_details": scope_details,
            "input_sha256": input_sha,
            "result_sha256": result_sha,
            "decision": overall,
            "reason_codes": reason_codes,
            "metrics": metrics,
            "observed_organism_count": len(raw_observations),
            "accepted_observation_count": len(accepted),
            "excluded_observation_count": len(excluded),
            "missing_target_count": len(missing_rows),
            "workflow_run_id": run_id,
        }
        (temp_dir / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_dir, final_dir)
        with db.transaction():
            db.conn.execute(
                "INSERT INTO coverage_reports(report_id, reference_set_id, reference_set_sha256, "
                "scope_kind, scope_value, scope_membership_sha256, input_sha256, status, decision, "
                "reason_codes, summary, relative_path, result_sha256, created_at, workflow_run_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report_id, reference_set_id, reference["tsv_sha256"], scope_kind, scope_value,
                    membership_sha, input_sha, "completed", overall,
                    json.dumps(reason_codes, ensure_ascii=False),
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    project_rel(project, final_dir), result_sha, created_at, run_id,
                ),
            )
            db.conn.executemany(
                "INSERT INTO coverage_report_metrics(report_id, rank, numerator, denominator, "
                "coverage_percent, threshold_percent, decision) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        report_id, item["rank"], item["numerator"], item["denominator"],
                        item["coverage_percent"], item["threshold_percent"], item["decision"],
                    )
                    for item in metrics
                ],
            )
        log_run(db, project, {
            "run_id": run_id,
            "entity_type": "coverage_report",
            "entity_id": report_id,
            "step": "coverage_report",
            "status": "completed",
            "started_at": created_at,
            "finished_at": now_iso(),
            "tool": "operon.coverage",
            "tool_version": __version__,
            "parameter_set": reference["profile_sha256"],
            "input_sha256": input_sha,
            "output_sha256": result_sha,
            "command": f"report coverage {reference_set_id} {scope_kind}:{scope_value or ''}",
        })
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        persisted = db.conn.execute(
            "SELECT 1 FROM coverage_reports WHERE input_sha256=?", (input_sha,)
        ).fetchone()
        if final_dir.exists() and not persisted:
            shutil.rmtree(final_dir, ignore_errors=True)
        raise
    return {
        "report_id": report_id,
        "reference_set_id": reference_set_id,
        "scope_kind": scope_kind,
        "scope_value": scope_value,
        "scope_membership_sha256": membership_sha,
        "input_sha256": input_sha,
        "decision": overall,
        "reason_codes": reason_codes,
        "metrics": metrics,
        "path": str(final_dir),
        "result_sha256": result_sha,
        "reused": False,
        "exit_code": 0 if overall == "PASS" else 1,
    }


def report_coverage(
    db: Database,
    project: Project,
    reference_set_id: str,
    *,
    release_version: str | None = None,
) -> dict[str, Any]:
    """Generate a report and record failed attempts as workflow provenance."""
    started_at = now_iso()
    try:
        return _report_coverage_impl(
            db, project, reference_set_id, release_version=release_version
        )
    except Exception as exc:
        scope = f"release:{release_version}" if release_version else "metadata"
        log_run(db, project, {
            "run_id": new_run_id(),
            "entity_type": "coverage_report",
            "entity_id": str(reference_set_id),
            "step": "coverage_report",
            "status": "failed",
            "started_at": started_at,
            "finished_at": now_iso(),
            "tool": "operon.coverage",
            "tool_version": __version__,
            "parameter_set": str(reference_set_id),
            "command": f"report coverage {reference_set_id} {scope}",
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise
