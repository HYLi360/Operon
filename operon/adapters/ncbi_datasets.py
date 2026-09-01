"""Offline-first adapter for NCBI Datasets genome packages.

The adapter deliberately separates acquisition from normalization.  Existing
JSON/JSONL reports, downloaded ZIP archives and unpacked dataset directories
all pass through the same parser and importer.  Online acquisition only adds a
streamed NCBI Datasets package download in front of that pipeline.
"""

from __future__ import annotations

import asyncio
import csv
import errno
import hashlib
import io
import json
import os
import queue
import random
import re
import shutil
import ssl
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import quote

import yaml

from operon import __version__
from operon.config import Project, project_rel
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.files import ingest_file, raw_bucket, standardize_file
from operon.schema import (
    ENTITY_ID_COLUMNS,
    ENTITY_PREFIXES,
    ENTITY_TABLES,
    METADATA_SCHEMA_VERSION,
    NCBI_SOURCE_FILE_ROLES,
    Schema,
    default_schemas,
)
from operon.utils import atomic_copy, atomic_write_text, now_iso, sha256_file
from operon.workflow import finish_run, new_run_id, start_run


NCBI_DATASETS_API = "https://api.ncbi.nlm.nih.gov/datasets/v2"
NCBI_DATASETS_API_FALLBACK = "https://api.ncbi.nlm.nih.gov/datasets/v2alpha"
ACCESSION_RE = re.compile(r"\bGC[AF]_\d+(?:\.\d+)?\b", re.IGNORECASE)
VERSIONED_ACCESSION_RE = re.compile(r"^(GC[AF]_\d+)(?:\.(\d+))?$", re.IGNORECASE)

INCLUDE_TYPES = {
    "genome": "GENOME_FASTA",
    "gff3": "GENOME_GFF",
    "protein": "PROT_FASTA",
    "cds": "CDS_FASTA",
    "sequence-report": "SEQUENCE_REPORT",
}
DEFAULT_INCLUDES = tuple(INCLUDE_TYPES)
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class _RetryableDownloadError(Exception):
    """Internal marker for transient network failures that should be retried."""


class _DownloadCancelled(Exception):
    """Internal marker used when the caller stops waiting for more batches."""
NCBI_ASSEMBLY_SCHEMA_FIELDS = (
    "assembly_name",
    "bioproject_accession",
    "source_database",
    "assembly_status",
    "assembly_type",
)


@dataclass
class SourceBundle:
    """One user input or downloaded package.

    ZIP packages remain compressed.  Reports are read directly from the
    archive and assets are staged one at a time during ingestion, avoiding a
    second full-size unpacked copy of every package.
    """

    source: Path
    root: Path
    label: str
    preserved_path: Path | None = None
    temporary: tempfile.TemporaryDirectory[str] | None = None

    def close(self) -> None:
        if self.temporary is not None:
            self.temporary.cleanup()


@dataclass
class DatasetAsset:
    path: Path | None
    accession: str
    role: str
    source_url: str | None = None
    archive_path: Path | None = None
    archive_member: str | None = None
    size_bytes: int | None = None

    @property
    def display_path(self) -> str:
        if self.archive_path is not None and self.archive_member is not None:
            return f"{self.archive_path}!/{self.archive_member}"
        return str(self.path)


@dataclass
class ImportPlan:
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {
        "organisms": [],
        "samples": [],
        "assemblies": [],
        "annotations": [],
        "accessions": [],
    })
    assets: list[DatasetAsset] = field(default_factory=list)
    assembly_ids: dict[str, str] = field(default_factory=dict)
    annotation_ids: dict[str, str] = field(default_factory=dict)
    canonical_accessions: dict[str, str] = field(default_factory=dict)
    assembly_records: list[dict[str, Any]] = field(default_factory=list)
    annotation_records: list[dict[str, Any]] = field(default_factory=list)
    new_ids: dict[str, int] = field(default_factory=lambda: {
        "organism": 0, "sample": 0, "assembly": 0, "annotation": 0,
    })

    @property
    def record_count(self) -> int:
        return sum(len(rows) for rows in self.tables.values())


class _IdAllocator:
    def __init__(self, db: Database):
        self.next_numbers: dict[str, int] = {}
        for entity_type, prefix in ENTITY_PREFIXES.items():
            if entity_type == "file":
                continue
            table = ENTITY_TABLES[entity_type]
            id_col = ENTITY_ID_COLUMNS[entity_type]
            maximum = 0
            for row in db.conn.execute(f"SELECT {id_col} AS value FROM {table}"):
                match = re.fullmatch(rf"{re.escape(prefix)}_(\d+)", str(row["value"]))
                if match:
                    maximum = max(maximum, int(match.group(1)))
            self.next_numbers[entity_type] = maximum + 1

    def allocate(self, entity_type: str) -> str:
        number = self.next_numbers[entity_type]
        self.next_numbers[entity_type] += 1
        return f"{ENTITY_PREFIXES[entity_type]}_{number:06d}"


class _PlanBuilder:
    def __init__(self, db: Database):
        self.db = db
        self.ids = _IdAllocator(db)
        self.plan = ImportPlan()
        self.rows: dict[str, dict[str, dict[str, Any]]] = {}
        for entity_type, table in ENTITY_TABLES.items():
            id_col = ENTITY_ID_COLUMNS[entity_type]
            self.rows[table] = {
                str(row[id_col]): dict(row)
                for row in db.conn.execute(f"SELECT * FROM {table}")
            }
        self.rows["accessions"] = {
            f"{row['namespace']}\0{row['accession']}": dict(row)
            for row in db.conn.execute("SELECT * FROM accessions")
        }
        self.planned: dict[str, dict[str, dict[str, Any]]] = {
            table: {} for table in self.plan.tables
        }

    def build(self, records: Sequence[dict[str, Any]], assets: Sequence[DatasetAsset]) -> ImportPlan:
        normalized_records = _deduplicate_reports(records)
        for record in normalized_records:
            self._add_record(record)
        for asset in assets:
            full = _canonical_accession(asset.accession)
            assembly_id = self.plan.assembly_ids.get(full)
            if not assembly_id:
                # Fall back to the unversioned accession base (asset paths may
                # carry only the base), but never when the base is ambiguous
                # within this plan.
                base = _split_accession(full)[0]
                candidates = {
                    aid for key, aid in self.plan.assembly_ids.items()
                    if _split_accession(key)[0] == base
                }
                if len(candidates) == 1:
                    assembly_id = next(iter(candidates))
            if not assembly_id:
                # An unpacked package can contain extra files that were not in
                # its report.  Ignore those rather than attaching them to the
                # wrong assembly.
                continue
            target_type = "assembly"
            target_id = assembly_id
            if asset.role in {"annotation_gff3", "cds_fasta", "protein_fasta"}:
                target_type = "annotation"
                target_id = self._ensure_annotation(assembly_id, full, {})
            else:
                canonical = self.plan.canonical_accessions[assembly_id]
                asset_role = _assembly_asset_role(asset.role, full, canonical)
            self.plan.assets.append(DatasetAsset(
                path=asset.path,
                accession=full,
                role=(asset.role if target_type == "annotation" else asset_role),
                source_url=asset.source_url,
                archive_path=asset.archive_path,
                archive_member=asset.archive_member,
                size_bytes=asset.size_bytes,
            ))
            # Store target identity without adding another public dataclass;
            # the maps remain authoritative when assets are ingested.
            if target_type == "annotation":
                self.plan.annotation_ids[full] = target_id
        for table in self.plan.tables:
            self.plan.tables[table] = list(self.planned[table].values())
        return self.plan

    def _add_record(self, report: dict[str, Any]) -> None:
        meta = _extract_metadata(report)
        primary = meta["accession"]
        if not primary:
            raise ValidationError("NCBI Datasets record has no assembly accession")
        related = _unique([primary, meta.get("current_accession"), meta.get("paired_accession")])

        existing_assembly_ids = {
            value for accession in related
            if (value := self._find_assembly(accession)) is not None
        }
        if len(existing_assembly_ids) > 1:
            raise ConflictError(
                f"NCBI paired accessions {related} already map to different assemblies: "
                f"{sorted(existing_assembly_ids)}"
            )
        if existing_assembly_ids:
            assembly_id = next(iter(existing_assembly_ids))
        else:
            assembly_id = self.ids.allocate("assembly")
            self.plan.new_ids["assembly"] += 1

        organism_id = self._ensure_organism(meta)
        sample_id = self._ensure_sample(meta, organism_id, assembly_id)
        current = self._current("assemblies", assembly_id)
        canonical = _select_canonical_assembly_accession(current, related, primary)
        assembly_row = _merge_nonempty(current, {
            "assembly_id": assembly_id,
            "sample_id": sample_id,
            "assembly_accession": canonical,
            "assembly_name": meta.get("assembly_name"),
            "assembly_version": _accession_version(canonical) or 1,
            "assembly_level": _normalize_assembly_level(meta.get("assembly_level")),
            "assembly_method": meta.get("assembly_method"),
            "submitter": meta.get("submitter"),
            "release_date": _date_only(meta.get("release_date")),
            "reference_status": _normalize_reference_status(meta.get("reference_status")),
            "bioproject_accession": meta.get("bioproject_accession"),
            "source_database": _normalize_source_database(None, canonical),
            "assembly_status": meta.get("assembly_status"),
            "assembly_type": meta.get("assembly_type"),
        })
        self._put("assemblies", assembly_id, assembly_row)
        self.plan.canonical_accessions[assembly_id] = canonical

        for accession in related:
            if not accession:
                continue
            accession = _canonical_accession(accession)
            self.plan.assembly_ids[accession] = assembly_id
            namespace = _assembly_namespace(accession)
            self._put_accession("assembly", assembly_id, namespace, accession,
                                _accession_version(accession), accession == canonical)
            self.plan.assembly_records.append({
                "accession": accession,
                "assembly_id": assembly_id,
                "source_database": _normalize_source_database(None, accession),
                "is_canonical": 1 if accession == canonical else 0,
                "metadata_sha256": _metadata_identity(meta, accession),
            })
        self._put_accession("assembly", assembly_id, "NCBI_Assembly", canonical,
                            _accession_version(canonical), True)
        annotation = meta.get("annotation") or {}
        if any(annotation.values()):
            annotation_id = self._ensure_annotation(assembly_id, primary, annotation)
            row = self._current("annotations", annotation_id)
            source_db = "RefSeq" if primary.startswith("GCF_") else "GenBank"
            self._put("annotations", annotation_id, _merge_nonempty(row, {
                "annotation_id": annotation_id,
                "assembly_id": assembly_id,
                "annotation_source": annotation.get("provider") or f"NCBI {source_db}",
                "annotation_version": _integer_or_none(annotation.get("version")) or 1,
                "annotation_date": _date_only(annotation.get("release_date")),
            }))

    def _find_assembly(self, accession: str | None) -> str | None:
        if not accession:
            return None
        accession = _canonical_accession(accession)
        for namespace in ("NCBI_Assembly", _assembly_namespace(accession)):
            row = self._accession(namespace, accession)
            if row and row["internal_type"] == "assembly":
                assembly_id = str(row["internal_id"])
                self._require_active_existing("assembly", assembly_id)
                return assembly_id
        base, version = _split_accession(accession)
        for assembly_id, row in {**self.rows["assemblies"], **self.planned["assemblies"]}.items():
            stored = str(row.get("assembly_accession") or "").upper()
            stored_base, stored_version = _split_accession(stored)
            explicit_version = _integer_or_none(row.get("assembly_version"))
            if stored == accession or (
                stored_base == base and (stored_version or explicit_version) == version
            ):
                self._require_active_existing("assembly", assembly_id)
                return assembly_id
        return None

    def _ensure_organism(self, meta: dict[str, Any]) -> str:
        taxon_id = _integer_or_none(meta.get("taxon_id"))
        scientific_name = str(meta.get("scientific_name") or "").strip()
        if taxon_id is not None:
            acc = self._accession("NCBI_Taxonomy", str(taxon_id))
            if acc and acc["internal_type"] == "organism":
                organism_id = str(acc["internal_id"])
            else:
                organism_id = next((
                    oid for oid, row in {**self.rows["organisms"], **self.planned["organisms"]}.items()
                    if _integer_or_none(row.get("taxon_id")) == taxon_id
                ), "")
        else:
            organism_id = ""
        if not organism_id and scientific_name:
            folded = scientific_name.casefold()
            organism_id = next((
                oid for oid, row in {**self.rows["organisms"], **self.planned["organisms"]}.items()
                if str(row.get("scientific_name") or "").casefold() == folded
            ), "")
        if not organism_id:
            if not scientific_name:
                raise ValidationError("NCBI Datasets record has neither organism name nor taxon ID")
            organism_id = self.ids.allocate("organism")
            self.plan.new_ids["organism"] += 1
        self._require_active_existing("organism", organism_id)
        row = _merge_nonempty(self._current("organisms", organism_id), {
            "organism_id": organism_id,
            "scientific_name": scientific_name,
            "taxon_id": taxon_id,
            "taxonomy_source": "NCBI",
        })
        self._put("organisms", organism_id, row)
        if taxon_id is not None:
            self._put_accession("organism", organism_id, "NCBI_Taxonomy", str(taxon_id), None, True)
        return organism_id

    def _ensure_sample(self, meta: dict[str, Any], organism_id: str, assembly_id: str) -> str:
        biosample = str(meta.get("biosample_accession") or "").strip().upper()
        sample_id = ""
        if biosample:
            acc = self._accession("NCBI_BioSample", biosample)
            if acc and acc["internal_type"] == "sample":
                sample_id = str(acc["internal_id"])
            if not sample_id:
                sample_id = next((
                    sid for sid, row in {**self.rows["samples"], **self.planned["samples"]}.items()
                    if str(row.get("biosample_accession") or "").upper() == biosample
                ), "")
        if not sample_id:
            existing_assembly = self._current("assemblies", assembly_id)
            sample_id = str(existing_assembly.get("sample_id") or "")
        if not sample_id:
            sample_id = self.ids.allocate("sample")
            self.plan.new_ids["sample"] += 1
        self._require_active_existing("sample", sample_id)
        sample_row = _merge_nonempty(self._current("samples", sample_id), {
            "sample_id": sample_id,
            "organism_id": organism_id,
            "biosample_accession": biosample or None,
            "strain": meta.get("strain"),
            "isolate": meta.get("isolate"),
            "cultivar": meta.get("cultivar"),
            "sex": _normalize_sex(meta.get("sex")),
            "collection_date": _date_only(meta.get("collection_date")),
            "country": meta.get("country"),
            "latitude": _float_or_none(meta.get("latitude")),
            "longitude": _float_or_none(meta.get("longitude")),
            "host": meta.get("host"),
            "source_record": (
                f"https://www.ncbi.nlm.nih.gov/biosample/{biosample}" if biosample
                else f"https://www.ncbi.nlm.nih.gov/datasets/genome/{meta['accession']}"
            ),
        })
        self._put("samples", sample_id, sample_row)
        if biosample:
            self._put_accession("sample", sample_id, "NCBI_BioSample", biosample, None, True)
        return sample_id

    def _ensure_annotation(
        self,
        assembly_id: str,
        accession: str,
        annotation: dict[str, Any],
    ) -> str:
        accession = _canonical_accession(accession)
        if accession in self.plan.annotation_ids:
            return self.plan.annotation_ids[accession]
        source_db = "RefSeq" if accession.startswith("GCF_") else "GenBank"
        provider = str(annotation.get("provider") or f"NCBI {source_db}").strip()
        version = _integer_or_none(annotation.get("version")) or 1
        release_date = _date_only(annotation.get("release_date"))
        identity_sha256 = _annotation_identity(
            assembly_id, accession, provider, version, release_date,
        )
        mapped = (
            self.db.conn.execute(
                "SELECT annotation_id FROM ncbi_annotation_records WHERE identity_sha256=?",
                (identity_sha256,),
            ).fetchone()
            if _table_exists(self.db, "ncbi_annotation_records") else None
        )
        annotation_id = str(mapped["annotation_id"]) if mapped else ""
        if not annotation_id:
            canonical = self.plan.canonical_accessions.get(assembly_id)
            # Compatibility bridge for pre-2.6 rows: only reuse an exact
            # metadata identity when this report is for the assembly's
            # canonical accession.  Paired-source annotations remain distinct.
            if canonical == accession:
                # Never bridge to an annotation already claimed by a
                # different accession: paired GCA/GCF packages can carry
                # identical annotationInfo with different GFF bytes, and
                # reusing the paired source's annotation would collide at
                # ingest time (and break re-import idempotency).
                claimed: set[str] = {
                    aid for other, aid in self.plan.annotation_ids.items()
                    if other != accession
                }
                if _table_exists(self.db, "ncbi_annotation_records"):
                    claimed.update(
                        str(rec["annotation_id"])
                        for rec in self.db.conn.execute(
                            "SELECT annotation_id, assembly_accession "
                            "FROM ncbi_annotation_records"
                        )
                        if _canonical_accession(str(rec["assembly_accession"])) != accession
                    )
                annotation_id = next((
                    aid for aid, row in {
                        **self.rows["annotations"], **self.planned["annotations"],
                    }.items()
                    if aid not in claimed
                    and row.get("assembly_id") == assembly_id
                    and str(row.get("annotation_source") or "").strip().casefold()
                    == provider.casefold()
                    and (_integer_or_none(row.get("annotation_version")) or 1) == version
                    and _date_only(row.get("annotation_date")) == release_date
                ), "")
        if not annotation_id:
            annotation_id = self.ids.allocate("annotation")
            self.plan.new_ids["annotation"] += 1
        self._require_active_existing("annotation", annotation_id)
        row = _merge_nonempty(self._current("annotations", annotation_id), {
            "annotation_id": annotation_id,
            "assembly_id": assembly_id,
            "annotation_source": provider,
            "annotation_version": version,
            "annotation_date": release_date,
        })
        self._put("annotations", annotation_id, row)
        self.plan.annotation_ids[accession] = annotation_id
        self.plan.annotation_records.append({
            "identity_sha256": identity_sha256,
            "annotation_id": annotation_id,
            "assembly_accession": accession,
            "provider": provider,
            "annotation_version": version,
            "annotation_date": release_date,
        })
        return annotation_id

    def _current(self, table: str, key: str) -> dict[str, Any]:
        return dict(self.planned[table].get(key) or self.rows[table].get(key) or {})

    def _require_active_existing(self, entity_type: str, entity_id: str) -> None:
        table = ENTITY_TABLES[entity_type]
        if entity_id in self.rows[table] and self.db.is_entity_retired(entity_type, entity_id):
            raise ValidationError(
                f"NCBI import resolved to retired {entity_type} {entity_id}; "
                f"run `operon restore {entity_id} --reason TEXT --apply` before re-importing"
            )

    def _put(self, table: str, key: str, row: dict[str, Any]) -> None:
        self.planned[table][key] = row

    def _accession(self, namespace: str, accession: str) -> dict[str, Any] | None:
        key = f"{namespace}\0{accession}"
        return self.planned["accessions"].get(key) or self.rows["accessions"].get(key)

    def _put_accession(self, internal_type: str, internal_id: str, namespace: str,
                       accession: str, version: int | str | None, primary: bool) -> None:
        accession = str(accession).strip()
        key = f"{namespace}\0{accession}"
        current = self._accession(namespace, accession)
        if current and (
            current.get("internal_type") != internal_type
            or current.get("internal_id") != internal_id
        ):
            raise ConflictError(
                f"{namespace}:{accession} already maps to "
                f"{current.get('internal_type')} {current.get('internal_id')}, "
                f"not {internal_type} {internal_id}"
            )
        self.planned["accessions"][key] = _merge_nonempty(current or {}, {
            "internal_type": internal_type,
            "internal_id": internal_id,
            "namespace": namespace,
            "accession": accession,
            "version": str(version) if version is not None else None,
            "is_primary": 1 if primary else None,
        })


def run_ncbi_datasets_adapter(
    db: Database,
    project: Project,
    *,
    inputs: Sequence[str | Path] = (),
    accessions: Sequence[str] = (),
    accession_file: str | Path | None = None,
    includes: Sequence[str] = DEFAULT_INCLUDES,
    archive_files: bool = True,
    standardize: bool = False,
    dry_run: bool = False,
    preserve_sources: bool = True,
    email: str | None = None,
    api_key: str | None = None,
    timeout: float = 300.0,
    batch_size: int = 10,
    download_workers: int = 3,
    max_retries: int = 4,
    retry_backoff: float = 1.0,
    resume_run_id: str | None = None,
    plan_only: bool = False,
) -> dict[str, Any]:
    """Import existing NCBI Datasets outputs and optionally download packages."""

    requested = _collect_accessions(accessions, accession_file)
    if not inputs and not requested:
        raise ValidationError("provide at least one --input, --accession, or --accession-file")
    if plan_only and inputs:
        raise ValidationError("--plan-only supports accession requests, not offline --input packages")
    unknown_includes = sorted(set(includes) - set(INCLUDE_TYPES))
    if unknown_includes:
        raise ValidationError(f"unknown NCBI include type(s): {unknown_includes}")
    if batch_size < 1 or batch_size > 100:
        raise ValidationError("--batch-size must be between 1 and 100")
    if download_workers < 1 or download_workers > 10:
        raise ValidationError("--download-workers must be between 1 and 10")
    if max_retries < 0 or max_retries > 10:
        raise ValidationError("--retries must be between 0 and 10")
    if retry_backoff < 0:
        raise ValidationError("--retry-backoff must be >= 0")

    run_id = new_run_id()
    started_at = now_iso()
    preview_schema = _adapter_schema(project, persist=False)
    persisted_schema: Schema | None = None
    imported_assembly_ids: set[str] = set()
    observed_assembly_groups: dict[str, set[str]] = {}
    accession_group: dict[str, str] = {}
    download_failures: list[dict[str, str]] = []
    summary: dict[str, Any] = {
        "run_id": run_id,
        "dry_run": dry_run,
        "sources": [],
        "assembly_records": 0,
        "metadata_rows": {
            "organisms": 0,
            "samples": 0,
            "assemblies": 0,
            "annotations": 0,
            "accessions": 0,
        },
        "new_ids": {
            "organism": 0,
            "sample": 0,
            "assembly": 0,
            "annotation": 0,
        },
        "discovered_files": 0,
        "archived_files": [],
        "standardized_files": [],
        "download": {
            "batch_size": batch_size,
            "workers": download_workers,
            "retries": max_retries,
            "retry_backoff": retry_backoff,
        },
        "download_failures": [],
        "skipped_existing": [],
    }

    download_groups: dict[tuple[str, ...], list[str]] = {
        tuple(includes): list(requested),
    } if requested else {}
    skipped_existing: list[str] = []
    if requested and archive_files:
        download_groups, skipped_existing = _plan_missing_downloads(
            db, project, requested, includes, standardize=standardize,
        )
        summary["skipped_existing"] = skipped_existing
    summary["download_plan"] = [
        {"includes": list(signature), "accessions": list(values)}
        for signature, values in download_groups.items()
    ]
    to_download_count = sum(len(values) for values in download_groups.values())
    command_text = (
        "offline import" if not requested
        else f"download {to_download_count} accession(s) "
             f"(workers={download_workers}, retries={max_retries})"
             + (f"; skipped {len(skipped_existing)} already archived" if skipped_existing else "")
    )
    request_document = {
        "inputs": [str(Path(value).resolve()) for value in inputs],
        "accessions": requested,
        "includes": list(includes),
        "archive_files": archive_files,
        "standardize": standardize,
    }
    request_sha256 = hashlib.sha256(
        json.dumps(request_document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if plan_only:
        summary["plan_only"] = True
        summary["request_sha256"] = request_sha256
        return summary
    if resume_run_id:
        previous = db.conn.execute(
            "SELECT run_id, input_sha256 FROM workflow_runs WHERE run_id=?",
            (resume_run_id,),
        ).fetchone()
        if previous is None:
            raise ValidationError(f"resume workflow run does not exist: {resume_run_id}")
        if previous["input_sha256"] and previous["input_sha256"] != request_sha256:
            raise ValidationError(
                "--resume-run request differs from the original run; use the same inputs, "
                "accessions, include set and archival options"
            )
    if not dry_run:
        start_run(db, {
            "run_id": run_id,
            "resumes_run_id": resume_run_id,
            "step": "ncbi_datasets_import",
            "status": "running",
            "started_at": started_at,
            "tool": "NCBI Datasets adapter",
            "parameter_set": ",".join(includes),
            "command": command_text,
            "input_sha256": request_sha256,
            "execution_details": json.dumps(request_document, ensure_ascii=False, sort_keys=True),
        })
        for accession in requested:
            status = "skipped" if accession in skipped_existing else "pending"
            db.upsert_adapter_run_item(
                run_id, accession, json.dumps(list(includes)), status,
                started_at=started_at,
                finished_at=now_iso() if status == "skipped" else None,
                result_json=(
                    json.dumps({"reason": "requested roles already archived"})
                    if status == "skipped" else None
                ),
            )

    def process_source(
        source_path: Path,
        *,
        label: str,
        requested_batch: Sequence[str] = (),
        already_preserved: Path | None = None,
    ) -> dict[str, Any]:
        nonlocal persisted_schema
        bundle = _open_source(source_path, project, False, label=label)
        try:
            bundle_reports = load_dataset_reports(
                bundle.root,
                direct_file=bundle.source if bundle.source.is_file() else None,
            )
            if not bundle_reports and requested_batch and (email or os.environ.get("NCBI_EMAIL")):
                bundle_reports.extend(fetch_entrez_assembly_reports(
                    requested_batch,
                    email=email or os.environ.get("NCBI_EMAIL"),
                    api_key=api_key or os.environ.get("NCBI_API_KEY"),
                ))
            bundle_assets = discover_dataset_assets(bundle.root, bundle_reports, bundle.label)
            source_summary = {
                "source": bundle.label,
                "preserved_path": (
                    project_rel(project, already_preserved) if already_preserved else None
                ),
                "reports": len(bundle_reports),
                "assets": len(bundle_assets),
            }
            summary["sources"].append(source_summary)
            if not bundle_reports:
                return {}

            # Track report identity independently of allocated IDs.  This
            # keeps large --dry-run summaries correct even though dry runs do
            # not write one batch's ID allocations for the next batch to see.
            for report in bundle_reports:
                meta = _extract_metadata(report)
                related = set(_unique([
                    meta.get("accession"),
                    meta.get("current_accession"),
                    meta.get("paired_accession"),
                ]))
                if not related:
                    continue
                roots = {accession_group[item] for item in related if item in accession_group}
                root = min(roots) if roots else min(related)
                members = set(related)
                for old_root in roots:
                    members.update(observed_assembly_groups.pop(old_root, set()))
                observed_assembly_groups[root] = members
                for item in members:
                    accession_group[item] = root

            plan = _PlanBuilder(db).build(bundle_reports, bundle_assets)
            _preflight_assets(db, plan)
            _validate_plan_rows(preview_schema, plan)
            imported_assembly_ids.update(plan.assembly_ids.values())
            for table, rows in plan.tables.items():
                summary["metadata_rows"][table] += len(rows)
            for entity_type, count in plan.new_ids.items():
                summary["new_ids"][entity_type] += count
            summary["discovered_files"] += len(plan.assets)
            if dry_run:
                return {
                    "assembly_ids": sorted(set(plan.assembly_ids.values())),
                    "annotation_ids": sorted(set(plan.annotation_ids.values())),
                    "file_ids": [],
                }

            if preserve_sources and already_preserved is None and bundle.source.is_file():
                bundle.preserved_path = _preserve_source(bundle.source, project)
                source_summary["preserved_path"] = project_rel(project, bundle.preserved_path)

            if persisted_schema is None:
                persisted_schema = _adapter_schema(project, persist=True)
            _apply_plan(
                db, project, plan, persisted_schema, workflow_run_id=run_id,
            )
            source_file_ids: list[str] = []
            if archive_files:
                for asset in plan.assets:
                    accession = _canonical_accession(asset.accession)
                    if asset.role in {"annotation_gff3", "cds_fasta", "protein_fasta"}:
                        entity_type = "annotation"
                        entity_id = plan.annotation_ids[accession]
                    else:
                        entity_type = "assembly"
                        entity_id = plan.assembly_ids[accession]
                    row = _ingest_dataset_asset(
                        db,
                        project,
                        asset,
                        entity_type,
                        entity_id,
                        run_id=run_id,
                        standardize=standardize,
                    )
                    summary["archived_files"].append(row["file_id"])
                    source_file_ids.append(row["file_id"])
                    if entity_type == "assembly":
                        pointer = (
                            "genome_file_id" if asset.role.startswith("genome_fasta")
                            else "report_file_id" if asset.role.startswith("assembly_report")
                            else None
                        )
                        if pointer:
                            with db.transaction():
                                db.conn.execute(
                                    f"UPDATE ncbi_assembly_records SET {pointer}=?, "
                                    "workflow_run_id=?, updated_at=? WHERE accession=?",
                                    (row["file_id"], run_id, now_iso(), accession),
                                )
                    if row.get("standardized_file_id"):
                        summary["standardized_files"].append(row["standardized_file_id"])
            return {
                "assembly_ids": sorted(set(plan.assembly_ids.values())),
                "annotation_ids": sorted(set(plan.annotation_ids.values())),
                "file_ids": source_file_ids,
            }
        finally:
            # Critical for large accession lists: no source bundle or staging
            # directory is allowed to survive into the next batch.
            bundle.close()

    try:
        for raw_input in inputs:
            source = Path(raw_input).resolve()
            process_source(source, label=str(source))
        if download_groups:
            # Keep downloads off /tmp: it is commonly a small tmpfs.  The
            # staging directory lives on the project filesystem.  Batches are
            # downloaded concurrently and consumed as soon as each finishes.
            with tempfile.TemporaryDirectory(
                prefix=".operon-ncbi-download-", dir=str(project.root)
            ) as temp_name:
                for missing_signature, group_accessions in download_groups.items():
                    batches = list(_chunks(group_accessions, batch_size))

                    def consume_batch(batch: Sequence[str], zip_path: Path) -> None:
                        preserved_path: Path | None = None
                        source_path = zip_path
                        if not dry_run:
                            for accession in batch:
                                db.upsert_adapter_run_item(
                                    run_id, accession, json.dumps(list(missing_signature)),
                                    "downloading", started_at=now_iso(),
                                )
                        try:
                            if preserve_sources and not dry_run:
                                preserved_path = _preserve_source(zip_path, project, move=True)
                                source_path = preserved_path
                            result = process_source(
                                source_path,
                                label=f"download:{','.join(batch)}",
                                requested_batch=batch,
                                already_preserved=preserved_path,
                            )
                            if not dry_run:
                                for accession in batch:
                                    db.upsert_adapter_run_item(
                                        run_id, accession, json.dumps(list(missing_signature)),
                                        "completed", started_at=started_at, finished_at=now_iso(),
                                        result_json=json.dumps(result, ensure_ascii=False, sort_keys=True),
                                    )
                        except BaseException as exc:
                            if not dry_run:
                                status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
                                for accession in batch:
                                    db.upsert_adapter_run_item(
                                        run_id, accession, json.dumps(list(missing_signature)),
                                        status, started_at=started_at, finished_at=now_iso(),
                                        error=f"{type(exc).__name__}: {exc}",
                                    )
                            raise
                        finally:
                            zip_path.unlink(missing_ok=True)

                    def record_download_failure(batch: Sequence[str], error: BaseException) -> None:
                        download_failures.append({
                            "accessions": ",".join(batch),
                            "includes": ",".join(missing_signature),
                            "error": f"{type(error).__name__}: {error}",
                        })
                        summary["download_failures"] = download_failures
                        if not dry_run:
                            for accession in batch:
                                db.upsert_adapter_run_item(
                                    run_id, accession, json.dumps(list(missing_signature)),
                                    "failed", started_at=started_at, finished_at=now_iso(),
                                    error=f"{type(error).__name__}: {error}",
                                )

                    download_ncbi_datasets_parallel(
                        batches,
                        Path(temp_name),
                        includes=missing_signature,
                        email=email,
                        api_key=api_key,
                        timeout=timeout,
                        max_workers=download_workers,
                        max_retries=max_retries,
                        retry_backoff=retry_backoff,
                        on_complete=consume_batch,
                        on_error=record_download_failure,
                    )

        if download_failures and dry_run:
            summary["assembly_records"] = len(observed_assembly_groups)
            return summary
        if not imported_assembly_ids and not skipped_existing:
            if download_failures:
                details = "\n".join(
                    f"- {item['accessions']}: {item['error']}" for item in download_failures[:20]
                )
                raise ValidationError(
                    "no NCBI assembly records could be imported; download batch failures:\n" + details
                )
            raise ValidationError("no NCBI assembly records found in the supplied input/download")
        summary["assembly_records"] = len(observed_assembly_groups)
        if dry_run:
            return summary
        evidence = ", ".join(
            item["preserved_path"] for item in summary["sources"] if item["preserved_path"]
        ) or None
        db.record_change(
            "adapter",
            run_id,
            None,
            None,
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
            "NCBI Datasets import",
            evidence=evidence,
            actor=os.environ.get("USER"),
            workflow_run_id=run_id,
        )
        if download_failures:
            failed_count = len(download_failures)
            details = "\n".join(
                f"- {item['accessions']}: {item['error']}" for item in download_failures[:20]
            )
            if failed_count > 20:
                details += f"\n- ... and {failed_count - 20} more failed batch(es)"
            raise ValidationError(
                f"{failed_count} NCBI download batch(es) failed while other batches were imported successfully:\n"
                + details
            )
        finish_run(
            db, project, run_id, status="completed", exit_code=0,
            execution_details=json.dumps(summary, ensure_ascii=False, sort_keys=True),
        )
        return summary
    except KeyboardInterrupt as exc:
        # SIGINT/SIGTERM (ShutdownRequested included): record the interruption
        # so an aborted run is visible in the audit trail instead of looking
        # like it never happened.
        if not dry_run:
            try:
                signum = getattr(exc, "signum", None)
                finish_run(
                    db, project, run_id, status="interrupted", exit_code=130,
                    error=(f"interrupted by signal {signum}" if signum is not None
                           else "interrupted"),
                    execution_details=json.dumps(summary, ensure_ascii=False, sort_keys=True),
                )
            except Exception:
                pass
        raise
    except Exception as exc:
        reported_exc: Exception = exc
        if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
            reported_exc = _no_space_error(project.root, "NCBI Datasets import", exc)
        if not dry_run:
            try:
                finish_run(
                    db, project, run_id, status="failed", exit_code=1,
                    error=str(reported_exc),
                    execution_details=json.dumps(summary, ensure_ascii=False, sort_keys=True),
                )
            except Exception:
                pass
        if reported_exc is not exc:
            raise reported_exc from exc
        raise


_ANNOTATION_INCLUDE_ROLES = {
    "gff3": "annotation_gff3",
    "protein": "protein_fasta",
    "cds": "cds_fasta",
}


def _table_exists(db: Database, table: str) -> bool:
    return db.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _find_archived_assembly(db: Database, accession: str) -> str | None:
    """Resolve a requested accession to an existing assembly ID, if any.

    The accessions table is the identity mapping written for every imported
    assembly; a miss here simply means "download", which is always safe.
    Unversioned requests match any archived version of the same accession.
    """
    accession = _canonical_accession(accession)
    base, version = _split_accession(accession)
    for namespace in ("NCBI_Assembly", _assembly_namespace(accession)):
        if version is None:
            row = db.conn.execute(
                "SELECT internal_type, internal_id FROM accessions "
                "WHERE namespace=? AND (accession=? OR accession LIKE ?) "
                "ORDER BY accession DESC LIMIT 1",
                (namespace, base, f"{base}.%"),
            ).fetchone()
        else:
            row = db.conn.execute(
                "SELECT internal_type, internal_id FROM accessions "
                "WHERE namespace=? AND accession=? LIMIT 1",
                (namespace, accession),
            ).fetchone()
        if row and row["internal_type"] == "assembly":
            assembly_id = str(row["internal_id"])
            if db.is_entity_retired("assembly", assembly_id):
                raise ValidationError(
                    f"accession {accession} belongs to retired assembly {assembly_id}; "
                    f"run `operon restore {assembly_id} --reason TEXT --apply` before re-importing"
                )
            return assembly_id
    return None


def _file_satisfies_include(
    project: Project,
    row: Any | None,
    *,
    entity_type: str,
    standardize: bool,
) -> bool:
    if row is None or str(row["status"]) not in {
        "CHECKSUM_VERIFIED", "STANDARDIZED", "REMOTE_ONLY",
    }:
        return False
    local_path = project.root / str(row["relative_path"])
    if str(row["status"]) != "REMOTE_ONLY" and not local_path.exists():
        return False
    if standardize:
        standardized = (
            project.standardized_root / raw_bucket(entity_type)
            / str(row["entity_id"]) / Path(str(row["relative_path"])).name
        )
        if not standardized.exists():
            return False
    return True


def _missing_includes(
    db: Database,
    project: Project,
    accession: str,
    assembly_id: str,
    includes: Sequence[str],
    *,
    standardize: bool,
) -> tuple[str, ...]:
    """Return the exact requested include subset not already verified."""
    accession = _canonical_accession(accession)
    assembly = db.conn.execute(
        "SELECT assembly_accession FROM assemblies WHERE assembly_id=?", (assembly_id,)
    ).fetchone()
    canonical = _canonical_accession(str(assembly["assembly_accession"])) if assembly else accession
    missing: list[str] = []
    for include in includes:
        if include in {"genome", "sequence-report"}:
            base_role = "genome_fasta" if include == "genome" else "assembly_report"
            role = _assembly_asset_role(base_role, accession, canonical)
            row = db.conn.execute(
                "SELECT entity_id, relative_path, status FROM files "
                "WHERE entity_type='assembly' AND entity_id=? AND file_role=? LIMIT 1",
                (assembly_id, role),
            ).fetchone()
            if not _file_satisfies_include(
                project, row, entity_type="assembly", standardize=standardize,
            ):
                missing.append(include)

    requested_annotation = [
        include for include in includes if include in _ANNOTATION_INCLUDE_ROLES
    ]
    if requested_annotation:
        mapped_ids = (
            [
                str(row["annotation_id"])
                for row in db.conn.execute(
                    "SELECT DISTINCT n.annotation_id FROM ncbi_annotation_records n "
                    "WHERE n.assembly_accession=? "
                    + (
                        "AND NOT EXISTS (SELECT 1 FROM effective_retired_entities r "
                        "WHERE r.entity_type='annotation' AND r.entity_id=n.annotation_id)"
                        if db.lifecycle_schema_available() else ""
                    ),
                    (accession,),
                )
            ]
            if _table_exists(db, "ncbi_annotation_records") else []
        )
        if not mapped_ids and accession == canonical:
            supersession_filter = (
                "AND NOT EXISTS (SELECT 1 FROM entity_supersessions s "
                "WHERE s.object_type='annotation' AND s.object_id=annotations.annotation_id)"
                if _table_exists(db, "entity_supersessions") else ""
            )
            retirement_filter = (
                "AND NOT EXISTS (SELECT 1 FROM effective_retired_entities r "
                "WHERE r.entity_type='annotation' AND r.entity_id=annotations.annotation_id)"
                if db.lifecycle_schema_available() else ""
            )
            mapped_ids = [
                str(row["annotation_id"])
                for row in db.conn.execute(
                    "SELECT annotation_id FROM annotations WHERE assembly_id=? "
                    + supersession_filter + retirement_filter,
                    (assembly_id,),
                )
            ]
        satisfied: set[str] = set()
        # Roles must coexist on one annotation identity; never assemble a
        # false complete set from unrelated ANN rows.
        for annotation_id in mapped_ids:
            present: set[str] = set()
            for include in requested_annotation:
                role = _ANNOTATION_INCLUDE_ROLES[include]
                row = db.conn.execute(
                    "SELECT entity_id, relative_path, status FROM files "
                    "WHERE entity_type='annotation' AND entity_id=? AND file_role=? LIMIT 1",
                    (annotation_id, role),
                ).fetchone()
                if _file_satisfies_include(
                    project, row, entity_type="annotation", standardize=standardize,
                ):
                    present.add(include)
            if len(present) > len(satisfied):
                satisfied = present
        missing.extend(
            include for include in requested_annotation if include not in satisfied
        )
    return tuple(include for include in includes if include in set(missing))


def _plan_missing_downloads(
    db: Database,
    project: Project,
    accessions: Sequence[str],
    includes: Sequence[str],
    *,
    standardize: bool,
) -> tuple[dict[tuple[str, ...], list[str]], list[str]]:
    """Group accessions by their exact missing include signature."""
    groups: dict[tuple[str, ...], list[str]] = {}
    already_archived: list[str] = []
    for accession in accessions:
        assembly_id = _find_archived_assembly(db, accession)
        missing = (
            _missing_includes(
                db, project, accession, assembly_id, includes, standardize=standardize,
            )
            if assembly_id else tuple(includes)
        )
        if not missing:
            already_archived.append(accession)
        else:
            groups.setdefault(missing, []).append(accession)
    return groups, already_archived


def _local_zip_entry_names(path: Path, limit: int = 200) -> list[str]:
    """List local-file-header entries even when the central directory is absent."""
    import struct

    try:
        data = path.read_bytes()[: 16 * 1024 * 1024]
    except OSError:
        return []
    names: list[str] = []
    offset = 0
    while offset + 30 <= len(data) and len(names) < limit:
        if data[offset:offset + 4] != b"PK\x03\x04":
            break
        try:
            (_sig, _version, _flags, _method, _mtime, _mdate,
             _crc, comp_size, _uncomp_size, name_len, extra_len) = struct.unpack_from(
                "<IHHHHHIIIHH", data, offset
            )
        except struct.error:
            break
        start = offset + 30
        end = start + name_len
        if end > len(data):
            break
        try:
            names.append(data[start:end].decode("utf-8", errors="replace"))
        except Exception:
            names.append("<undecodable>")
        if comp_size == 0xFFFFFFFF:
            break
        offset = end + extra_len + comp_size
    return names


def _zip_package_diagnostic(path: Path, accessions: Sequence[str]) -> tuple[bool, str]:
    """Return (is_retryable, human-readable diagnostic) for a bad ZIP payload."""
    entries = _local_zip_entry_names(path)
    names = [name.lower() for name in entries]
    has_report = any(
        name.endswith(("assembly_data_report.jsonl", "assembly_data_report.json",
                       "dataset_report.jsonl", "dataset_report.json"))
        for name in names
    )
    has_data = any(name.startswith("ncbi_dataset/data/") for name in names)
    joined = ",".join(accessions)
    if entries and not has_report and not has_data:
        return False, (
            f"NCBI returned an empty/README-only package for accession(s) {joined}; "
            f"the accession may be invalid, withdrawn, or unavailable "
            f"(local ZIP entries: {', '.join(entries[:5])})"
        )
    if entries:
        return True, (
            f"ZIP payload is truncated or has no central directory for accession(s) {joined}; "
            f"local entries start with: {', '.join(entries[:5])}"
        )
    return True, (
        f"ZIP payload for accession(s) {joined} has no recognizable ZIP content; "
        f"the server may have returned a transient error page"
    )


def download_ncbi_dataset(
    accessions: Sequence[str],
    destination: str | Path,
    *,
    includes: Sequence[str] = DEFAULT_INCLUDES,
    email: str | None = None,
    api_key: str | None = None,
    timeout: float = 300.0,
    session: Any | None = None,
    max_retries: int = 4,
    retry_backoff: float = 1.0,
) -> Path:
    """Download one NCBI Datasets package with explicit SSL/network retries.

    Retries happen outside urllib3 as well: [SSL] record layer failure and
    other transport-level errors are transient in practice and should not
    force the operator to re-run the whole accession list by hand.
    """

    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except ImportError as exc:  # pragma: no cover - dependency installation error
        raise ValidationError("online NCBI download requires the 'requests' dependency") from exc

    canonical = [_canonical_accession(value) for value in accessions]
    if not canonical:
        raise ValidationError("no NCBI assembly accessions supplied for download")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    own_session = session is None
    if session is None:
        session = requests.Session()
        retry = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=retry_backoff,
            status_forcelist=tuple(sorted(RETRYABLE_HTTP_STATUS)),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))

    last_error: Exception | None = None
    try:
        for attempt in range(max_retries + 1):
            if attempt:
                time.sleep(retry_backoff * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5))
            try:
                return _download_ncbi_dataset_once(
                    canonical=canonical,
                    destination=destination,
                    includes=includes,
                    email=email,
                    api_key=api_key,
                    timeout=timeout,
                    session=session,
                )
            except _RetryableDownloadError as exc:
                last_error = exc
            except ssl.SSLError as exc:
                last_error = exc
            except requests.exceptions.ConnectionError as exc:
                last_error = exc
            except requests.exceptions.Timeout as exc:
                last_error = exc
            except requests.exceptions.ChunkedEncodingError as exc:
                last_error = exc
    finally:
        if own_session:
            session.close()

    raise ValidationError(
        f"NCBI Datasets download failed after {max_retries + 1} attempt(s): {last_error}"
    ) from last_error


def _download_ncbi_dataset_once(
    *,
    canonical: Sequence[str],
    destination: Path,
    includes: Sequence[str],
    email: str | None,
    api_key: str | None,
    timeout: float,
    session: Any,
) -> Path:
    """One download attempt over primary and fallback API bases."""
    import requests

    headers = {
        "Accept": "application/zip",
        "User-Agent": f"Operon/{__version__} NCBI-Datasets-Adapter ({email or 'email-not-provided'})",
    }
    if api_key:
        headers["api-key"] = api_key
    params = [("include_annotation_type", INCLUDE_TYPES[name]) for name in includes]
    joined = ",".join(canonical)
    response = None
    last_error: Exception | None = None
    try:
        for base in (NCBI_DATASETS_API, NCBI_DATASETS_API_FALLBACK):
            url = f"{base}/genome/accession/{quote(joined, safe=',._')}/download"
            try:
                response = session.get(
                    url,
                    params=params,
                    headers=headers,
                    stream=True,
                    timeout=(30.0, timeout),
                )
                if response is None:
                    raise _RetryableDownloadError("HTTP client returned no response")
                if response.status_code in {404, 410} and base == NCBI_DATASETS_API:
                    response.close()
                    response = None
                    continue
                if response.status_code in RETRYABLE_HTTP_STATUS:
                    raise _RetryableDownloadError(f"HTTP {response.status_code} from NCBI Datasets")
                response.raise_for_status()
                break
            except _RetryableDownloadError as exc:
                last_error = exc
                if response is not None:
                    response.close()
                    response = None
                if base == NCBI_DATASETS_API_FALLBACK:
                    raise
            except ssl.SSLError as exc:
                last_error = exc
                if response is not None:
                    response.close()
                    response = None
                if base == NCBI_DATASETS_API_FALLBACK:
                    raise _RetryableDownloadError(str(exc)) from exc
            except requests.exceptions.ConnectionError as exc:
                last_error = exc
                if response is not None:
                    response.close()
                    response = None
                if base == NCBI_DATASETS_API_FALLBACK:
                    raise _RetryableDownloadError(str(exc)) from exc
            except requests.exceptions.Timeout as exc:
                last_error = exc
                if response is not None:
                    response.close()
                    response = None
                if base == NCBI_DATASETS_API_FALLBACK:
                    raise _RetryableDownloadError(str(exc)) from exc

        if response is None:
            if last_error is None:
                last_error = ValidationError("NCBI Datasets returned no downloadable package")
            raise ValidationError(f"NCBI Datasets download failed: {last_error}") from last_error

        content_length = None
        response_headers = getattr(response, "headers", {}) or {}
        try:
            content_length = int(response_headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            content_length = None
        if content_length is not None and content_length > 0:
            _require_disk_space(destination.parent, content_length, "download NCBI dataset package")

        fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if not zipfile.is_zipfile(tmp_name):
                retryable, detail = _zip_package_diagnostic(Path(tmp_name), canonical)
                if retryable:
                    raise _RetryableDownloadError(detail) from None
                raise ValidationError(detail) from None
            os.replace(tmp_name, destination)
        except BaseException as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                raise _no_space_error(destination.parent, "download NCBI dataset package", exc) from exc
            if isinstance(exc, (ssl.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError)):
                raise _RetryableDownloadError(str(exc)) from exc
            raise
        return destination
    finally:
        if response is not None:
            response.close()


def download_ncbi_datasets_parallel(
    batches: Sequence[Sequence[str]],
    staging_dir: str | Path,
    *,
    includes: Sequence[str] = DEFAULT_INCLUDES,
    email: str | None = None,
    api_key: str | None = None,
    timeout: float = 300.0,
    max_workers: int = 3,
    max_retries: int = 4,
    retry_backoff: float = 1.0,
    on_complete: Any,
    on_error: Any | None = None,
) -> list[Path]:
    """Download accession batches concurrently and consume each as it lands.

    Downloads run in a dedicated asyncio thread.  Completed batches are handed
    to `on_complete(batch, zip_path)` on the caller thread, so SQLite writes
    stay on their original connection/thread while network I/O continues in
    the background.  Failed batches are isolated: other batches keep going and
    are processed normally, then an aggregate ValidationError is raised unless
    `on_error` is provided to collect failures instead.
    """
    import threading

    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    sentinel = object()
    # Bound the number of finished-but-not-yet-imported ZIPs.  When the queue
    # is full the asyncio producer blocks, which also backs off the network.
    completed_queue: queue.Queue[Any] = queue.Queue(maxsize=max_workers)
    cancel_event = threading.Event()
    runner_errors: list[BaseException] = []

    def runner() -> None:
        try:
            asyncio.run(_download_batches_async(
                batches=batches,
                staging_dir=staging_dir,
                includes=includes,
                email=email,
                api_key=api_key,
                timeout=timeout,
                max_workers=max_workers,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
                completed_queue=completed_queue,
                cancel_event=cancel_event,
            ))
        except BaseException as exc:
            runner_errors.append(exc)
        finally:
            # Give up once the consumer has gone (error/shutdown): a blocking
            # sentinel put on the full bounded queue would deadlock this
            # thread and hang the consumer's join.
            while not cancel_event.is_set():
                try:
                    completed_queue.put(sentinel, timeout=0.2)
                    break
                except queue.Full:
                    continue

    # daemon=True is only a last-resort backstop: the finally below cancels
    # pending work and joins this thread promptly on any exit path, but the
    # interpreter must never hang on it during process teardown.
    thread = threading.Thread(target=runner, name="operon-ncbi-download", daemon=True)
    thread.start()
    completed: list[Path] = []
    failures: list[tuple[Sequence[str], Exception]] = []
    try:
        while True:
            item = completed_queue.get()
            if item is sentinel:
                break
            batch, zip_path, error = item
            if error is not None:
                failures.append((batch, error))
                if on_error is not None:
                    on_error(batch, error)
                continue
            if zip_path is None:
                continue
            on_complete(batch, zip_path)
            completed.append(zip_path)
    finally:
        # Stop pending work before joining so a processing error cannot leave
        # the download thread running indefinitely.
        cancel_event.set()
        thread.join()

    if runner_errors:
        raise runner_errors[0]
    if failures and on_error is None:
        details = "\n".join(
            f"- {','.join(batch)}: {error}" for batch, error in failures[:20]
        )
        if len(failures) > 20:
            details += f"\n- ... and {len(failures) - 20} more failed batch(es)"
        raise ValidationError(
            f"{len(failures)}/{len(batches)} NCBI download batch(es) failed:\n{details}"
        )
    return completed


async def _download_batches_async(
    *,
    batches: Sequence[Sequence[str]],
    staging_dir: Path,
    includes: Sequence[str],
    email: str | None,
    api_key: str | None,
    timeout: float,
    max_workers: int,
    max_retries: int,
    retry_backoff: float,
    completed_queue: Any,
    cancel_event: Any,
) -> None:
    semaphore = asyncio.Semaphore(max_workers)

    async def one_batch(batch: Sequence[str], index: int) -> tuple[Sequence[str], Path | None, BaseException | None]:
        if cancel_event.is_set():
            return batch, None, _DownloadCancelled("cancelled")
        destination = staging_dir / f"ncbi_dataset_{index:05d}.zip"
        async with semaphore:
            if cancel_event.is_set():
                return batch, None, _DownloadCancelled("cancelled")
            try:
                await _download_batch_aiohttp(
                    batch,
                    destination,
                    includes=includes,
                    email=email,
                    api_key=api_key,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_backoff=retry_backoff,
                    cancel_event=cancel_event,
                )
            except BaseException as exc:
                return batch, None, exc
        return batch, destination, None

    tasks = [asyncio.create_task(one_batch(batch, index)) for index, batch in enumerate(batches)]
    try:
        for finished in asyncio.as_completed(tasks):
            batch, zip_path, error = await finished
            if error is None and zip_path is None:
                continue
            # The consumer may have exited on error or shutdown without
            # draining the bounded queue; a plain blocking put would then
            # deadlock this thread forever (and hang process exit).
            while not cancel_event.is_set():
                try:
                    completed_queue.put((batch, zip_path, error), timeout=0.2)
                    break
                except queue.Full:
                    continue
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _interruptible_retry_sleep(seconds: float, cancel_event: Any) -> None:
    """Backoff sleep that stays responsive to shutdown cancellation."""
    remaining = seconds
    while remaining > 0:
        if cancel_event.is_set():
            raise _DownloadCancelled()
        step = min(0.2, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def _download_batch_aiohttp(
    accessions: Sequence[str],
    destination: Path,
    *,
    includes: Sequence[str],
    email: str | None,
    api_key: str | None,
    timeout: float,
    max_retries: int,
    retry_backoff: float,
    cancel_event: Any,
) -> Path:
    """One concurrent download task with SSL/transient-error retries."""
    import aiohttp

    canonical = [_canonical_accession(value) for value in accessions]
    if not canonical:
        raise ValidationError("no NCBI assembly accessions supplied for download")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    headers = {
        "Accept": "application/zip",
        "User-Agent": f"Operon/{__version__} NCBI-Datasets-Adapter ({email or 'email-not-provided'})",
    }
    if api_key:
        headers["api-key"] = api_key
    params = [("include_annotation_type", INCLUDE_TYPES[name]) for name in includes]
    joined = ",".join(canonical)
    urls = [
        f"{base}/genome/accession/{quote(joined, safe=',._')}/download"
        for base in (NCBI_DATASETS_API, NCBI_DATASETS_API_FALLBACK)
    ]
    client_timeout = aiohttp.ClientTimeout(total=None, connect=30.0, sock_read=timeout)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        if cancel_event.is_set():
            raise _DownloadCancelled()
        if attempt:
            await _interruptible_retry_sleep(
                retry_backoff * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5),
                cancel_event,
            )
        try:
            async with aiohttp.ClientSession(headers=headers, timeout=client_timeout) as session:
                response = None
                for index, url in enumerate(urls):
                    try:
                        response = await session.get(url, params=params)
                        if response.status in {404, 410} and index == 0:
                            response.release()
                            response = None
                            continue
                        if response.status in RETRYABLE_HTTP_STATUS:
                            raise _RetryableDownloadError(f"HTTP {response.status} from NCBI Datasets")
                        response.raise_for_status()
                        break
                    except _RetryableDownloadError:
                        if response is not None:
                            response.release()
                            response = None
                        if index == len(urls) - 1:
                            raise
                    except (aiohttp.ClientSSLError, aiohttp.ClientConnectionError,
                            aiohttp.ServerDisconnectedError, asyncio.TimeoutError, ssl.SSLError) as exc:
                        if response is not None:
                            response.release()
                            response = None
                        if index == len(urls) - 1:
                            raise _RetryableDownloadError(str(exc)) from exc
                if response is None:
                    raise _RetryableDownloadError("NCBI Datasets returned no downloadable package")

                try:
                    content_length = int(response.headers.get("Content-Length", "") or 0)
                except (TypeError, ValueError):
                    content_length = 0
                if content_length > 0:
                    _require_disk_space(destination.parent, content_length, "download NCBI dataset package")

                fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
                try:
                    with os.fdopen(fd, "wb") as handle:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            if cancel_event.is_set():
                                raise _DownloadCancelled()
                            handle.write(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                    if not zipfile.is_zipfile(tmp_name):
                        retryable, detail = _zip_package_diagnostic(Path(tmp_name), canonical)
                        if retryable:
                            raise _RetryableDownloadError(detail) from None
                        raise ValidationError(detail) from None
                    os.replace(tmp_name, destination)
                finally:
                    if response is not None:
                        response.release()
                    response = None
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                return destination
        except _DownloadCancelled:
            raise
        except _RetryableDownloadError as exc:
            last_error = exc
        except (aiohttp.ClientSSLError, aiohttp.ClientConnectionError,
                aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError,
                asyncio.TimeoutError, ssl.SSLError) as exc:
            last_error = exc
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise _no_space_error(destination.parent, "download NCBI dataset package", exc) from exc
            last_error = exc
        except aiohttp.ClientResponseError as exc:
            if exc.status in RETRYABLE_HTTP_STATUS:
                last_error = exc
            else:
                raise ValidationError(f"NCBI Datasets download failed: {exc}") from exc

    raise ValidationError(
        f"NCBI Datasets download failed after {max_retries + 1} attempt(s): {last_error}"
    ) from last_error


def fetch_entrez_assembly_reports(
    accessions: Sequence[str], *, email: str | None, api_key: str | None = None
) -> list[dict[str, Any]]:
    """Use Biopython Entrez as a metadata fallback for unusual packages."""

    if not email:
        raise ValidationError("Biopython Entrez fallback requires --email or NCBI_EMAIL")
    try:
        from Bio import Entrez
    except ImportError as exc:  # pragma: no cover - dependency installation error
        raise ValidationError("Entrez fallback requires the 'biopython' dependency") from exc
    Entrez.email = email
    Entrez.api_key = api_key
    Entrez.tool = "Operon"
    reports: list[dict[str, Any]] = []
    for accession in accessions:
        canonical = _canonical_accession(accession)
        with Entrez.esearch(db="assembly", term=f"{canonical}[Assembly Accession]", retmax=2) as handle:
            found = Entrez.read(handle)
        ids = list(found.get("IdList") or [])
        if not ids:
            continue
        with Entrez.esummary(db="assembly", id=ids[0], report="full") as handle:
            summary = Entrez.read(handle, validate=False)
        documents = summary.get("DocumentSummarySet", {}).get("DocumentSummary", [])
        if not documents:
            continue
        doc = documents[0]
        synonym = doc.get("Synonym") or {}
        reports.append({
            "accession": str(doc.get("AssemblyAccession") or canonical),
            "organism": {
                "organismName": str(doc.get("SpeciesName") or ""),
                "taxId": _integer_or_none(doc.get("Taxid")),
            },
            "assemblyInfo": {
                "assemblyLevel": str(doc.get("AssemblyStatus") or ""),
                "assemblyName": str(doc.get("AssemblyName") or ""),
                "biosample": {"accession": str(doc.get("BioSampleAccn") or "")},
                "bioprojectAccession": str(doc.get("BioProjectAccn") or ""),
                "pairedAssembly": {
                    "accession": str(synonym.get("Genbank") or synonym.get("RefSeq") or "")
                },
                "refseqCategory": str(doc.get("RefSeq_category") or ""),
                "releaseDate": str(doc.get("SubmissionDate") or ""),
                "submitter": str(doc.get("SubmitterOrganization") or ""),
            },
            "sourceDatabase": "SOURCE_DATABASE_REFSEQ" if canonical.startswith("GCF_") else "SOURCE_DATABASE_GENBANK",
        })
    return reports


def load_dataset_reports(root: str | Path, direct_file: Path | None = None) -> list[dict[str, Any]]:
    root = Path(root)
    if root.is_file() and zipfile.is_zipfile(root):
        reports: list[dict[str, Any]] = []
        with zipfile.ZipFile(root) as archive:
            infos = _validated_zip_infos(archive)
            exact_names = {
                "assembly_data_report.jsonl",
                "assembly_data_report.json",
                "dataset_report.jsonl",
                "dataset_report.json",
            }
            candidates = [info for info in infos if PurePosixPath(info.filename).name in exact_names]
            if not candidates:
                candidates = [
                    info for info in infos
                    if info.filename.lower().endswith(".jsonl")
                    and "sequence_report" not in PurePosixPath(info.filename).name
                ]
            for info in candidates:
                with archive.open(info) as raw_handle:
                    text_handle = io.TextIOWrapper(raw_handle, encoding="utf-8-sig")
                    reports.extend(_read_report_handle(text_handle, f"{root}!/{info.filename}"))
        return _deduplicate_reports(reports)

    candidates: list[Path] = []
    if direct_file and direct_file.exists() and direct_file.suffix.lower() not in {".zip"}:
        candidates.append(direct_file)
    if root.is_file():
        candidates.append(root)
    elif root.is_dir():
        exact_names = {
            "assembly_data_report.jsonl",
            "assembly_data_report.json",
            "dataset_report.jsonl",
            "dataset_report.json",
        }
        candidates.extend(path for path in root.rglob("*") if path.is_file() and path.name in exact_names)
        if not candidates:
            candidates.extend(path for path in root.rglob("*.jsonl") if "sequence_report" not in path.name)
    unique_candidates = list(dict.fromkeys(path.resolve() for path in candidates))
    reports: list[dict[str, Any]] = []
    for path in unique_candidates:
        reports.extend(_read_report_file(path))
    return _deduplicate_reports(reports)


def discover_dataset_assets(
    root: str | Path,
    reports: Sequence[dict[str, Any]],
    source_label: str,
) -> list[DatasetAsset]:
    root = Path(root)
    report_accessions = [
        _extract_metadata(report)["accession"] for report in reports
        if _extract_metadata(report)["accession"]
    ]
    if root.is_file() and zipfile.is_zipfile(root):
        assets: list[DatasetAsset] = []
        with zipfile.ZipFile(root) as archive:
            for info in _validated_zip_infos(archive):
                if info.is_dir():
                    continue
                member_path = Path(*PurePosixPath(info.filename).parts)
                role = _asset_role(member_path)
                if role is None:
                    continue
                accession = _accession_from_path(member_path)
                if not accession and len(report_accessions) == 1:
                    accession = report_accessions[0]
                if not accession:
                    continue
                assets.append(DatasetAsset(
                    path=None,
                    accession=accession,
                    role=role,
                    source_url=f"ncbi-datasets:{source_label}:{info.filename}",
                    archive_path=root,
                    archive_member=info.filename,
                    size_bytes=info.file_size,
                ))
        return assets
    if not root.is_dir():
        return []
    assets: list[DatasetAsset] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        role = _asset_role(path)
        if role is None:
            continue
        accession = _accession_from_path(path)
        if not accession and len(report_accessions) == 1:
            accession = report_accessions[0]
        if not accession:
            continue
        assets.append(DatasetAsset(
            path=path,
            accession=accession,
            role=role,
            source_url=f"ncbi-datasets:{source_label}:{path.relative_to(root).as_posix()}",
            size_bytes=path.stat().st_size,
        ))
    return assets


def _validate_plan_rows(schema: Schema, plan: ImportPlan) -> dict[str, list[dict[str, Any]]]:
    normalized: dict[str, list[dict[str, Any]]] = {}
    for table, rows in plan.tables.items():
        if not rows:
            normalized[table] = []
            continue
        columns = set(schema.columns(table))
        # TODO(1.0): remove this field projection with old project-schema
        # support; validated 1.4+ schemas contain every adapter-owned field.
        compatible_rows = [{key: value for key, value in row.items() if key in columns} for row in rows]
        normalized[table], _ = schema.validate_and_normalize(table, compatible_rows)
    return normalized


def _apply_plan(
    db: Database,
    project: Project,
    plan: ImportPlan,
    schema: Schema,
    *,
    workflow_run_id: str,
) -> None:
    normalized = _validate_plan_rows(schema, plan)
    with db.transaction() as conn:
        db.ensure_metadata_columns(schema)
        for table in ("organisms", "samples", "assemblies", "annotations", "accessions"):
            rows = normalized[table]
            if not rows:
                continue
            columns = schema.columns(table)
            keys = db._primary_keys(table)
            assignments = ", ".join(f"{col}=excluded.{col}" for col in columns if col not in keys)
            sql = (
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)}) "
                f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {assignments}"
            )
            keys = db._primary_keys(table)
            for row in rows:
                where = " AND ".join(f"{key}=?" for key in keys)
                existing = conn.execute(
                    f"SELECT * FROM {table} WHERE {where}", [row.get(key) for key in keys]
                ).fetchone()
                conn.execute(sql, [row.get(col) for col in columns])
                before = dict(existing) if existing else {}
                object_id = ":".join(str(row.get(key)) for key in keys)
                for column in columns:
                    old_value = before.get(column)
                    new_value = row.get(column)
                    if old_value == new_value:
                        continue
                    conn.execute(
                        "INSERT INTO changes "
                        "(object_type, object_id, field, old_value, new_value, reason, evidence, "
                        "actor, changed_at, workflow_run_id, reverts_change_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                        (
                            table, object_id, column,
                            str(old_value) if old_value is not None else None,
                            str(new_value) if new_value is not None else None,
                            "NCBI Datasets metadata import", None, os.environ.get("USER"),
                            now_iso(), workflow_run_id,
                        ),
                    )
        timestamp = now_iso()
        for record in plan.assembly_records:
            conn.execute(
                "INSERT INTO ncbi_assembly_records "
                "(accession, assembly_id, source_database, is_canonical, metadata_sha256, "
                "workflow_run_id, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(accession) DO UPDATE SET assembly_id=excluded.assembly_id, "
                "source_database=excluded.source_database, is_canonical=excluded.is_canonical, "
                "metadata_sha256=excluded.metadata_sha256, workflow_run_id=excluded.workflow_run_id, "
                "updated_at=excluded.updated_at",
                (
                    record["accession"], record["assembly_id"], record["source_database"],
                    record["is_canonical"], record["metadata_sha256"], workflow_run_id, timestamp,
                ),
            )
        for record in plan.annotation_records:
            conn.execute(
                "INSERT INTO ncbi_annotation_records "
                "(identity_sha256, annotation_id, assembly_accession, provider, annotation_version, "
                "annotation_date, workflow_run_id, created_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(identity_sha256) DO UPDATE SET annotation_id=excluded.annotation_id, "
                "workflow_run_id=excluded.workflow_run_id",
                (
                    record["identity_sha256"], record["annotation_id"],
                    record["assembly_accession"], record["provider"],
                    record["annotation_version"], record["annotation_date"],
                    workflow_run_id, timestamp,
                ),
            )
        for entity_type, table in ENTITY_TABLES.items():
            if table not in normalized:
                continue
            id_col = ENTITY_ID_COLUMNS[entity_type]
            for row in normalized[table]:
                conn.execute(
                    "INSERT INTO entity_state(entity_type, entity_id, state, message, updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(entity_type, entity_id) DO NOTHING",
                    (entity_type, row[id_col], "METADATA_VALIDATED",
                     "metadata imported by NCBI Datasets adapter", timestamp),
                )


def _adapter_schema(project: Project, *, persist: bool) -> Schema:
    """Merge adapter fields into development-era schemas without data loss.

    TODO(1.0): require metadata schema 1.4+ and remove this automatic upgrade
    after pre-1.0 project compatibility is retired.
    """

    try:
        document = yaml.safe_load(project.schema_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValidationError(f"cannot read project metadata schema: {project.schema_path}") from exc
    if not isinstance(document.get("tables"), dict):
        raise ValidationError("schema document must contain a 'tables' mapping")
    try:
        assembly_fields = document["tables"]["assemblies"]["fields"]
    except (KeyError, TypeError) as exc:
        raise ValidationError("project schema has no assemblies.fields mapping") from exc
    defaults = default_schemas()["tables"]["assemblies"]["fields"]
    changed = False
    for name in NCBI_ASSEMBLY_SCHEMA_FIELDS:
        if name not in assembly_fields:
            assembly_fields[name] = dict(defaults[name])
            changed = True
    try:
        allowed_roles = document["tables"]["files"]["fields"]["file_role"]["allowed"]
    except (KeyError, TypeError) as exc:
        raise ValidationError("project schema has no files.file_role.allowed list") from exc
    for role in NCBI_SOURCE_FILE_ROLES:
        if role not in allowed_roles:
            allowed_roles.append(role)
            changed = True
    if _version_tuple(document.get("schema_version")) < _version_tuple(METADATA_SCHEMA_VERSION):
        document["schema_version"] = METADATA_SCHEMA_VERSION
        changed = True
    if persist and changed:
        atomic_write_text(
            project.schema_path,
            "# Operon metadata schema (YAML). Extended for the NCBI Datasets adapter.\n"
            + yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        )
    return Schema(document)


def _preflight_assets(db: Database, plan: ImportPlan) -> None:
    """Detect package-internal and manifest conflicts before metadata changes."""

    unique_assets: list[DatasetAsset] = []
    seen: dict[tuple[str, str, str], tuple[str, DatasetAsset]] = {}
    for asset in plan.assets:
        accession = _canonical_accession(asset.accession)
        if asset.role in {"annotation_gff3", "cds_fasta", "protein_fasta"}:
            entity_type = "annotation"
            entity_id = plan.annotation_ids[accession]
        else:
            entity_type = "assembly"
            entity_id = plan.assembly_ids[accession]
        key = (entity_type, entity_id, asset.role)
        digest = _asset_sha256(asset)
        previous = seen.get(key)
        if previous:
            if previous[0] != digest:
                raise ConflictError(
                    f"NCBI package contains multiple different files for "
                    f"{entity_type} {entity_id} role {asset.role}: "
                    f"{previous[1].display_path} and {asset.display_path}"
                )
            continue
        seen[key] = (digest, asset)
        existing = db.conn.execute(
            "SELECT file_id, sha256 FROM files WHERE entity_type=? AND entity_id=? AND file_role=? LIMIT 1",
            key,
        ).fetchone()
        if existing and str(existing["sha256"]).lower() != digest.lower():
            raise ConflictError(
                f"{entity_type} {entity_id} role {asset.role} already has file "
                f"{existing['file_id']} with different bytes; import a new assembly/annotation version"
            )
        unique_assets.append(asset)
    plan.assets = unique_assets


def _asset_sha256(asset: DatasetAsset) -> str:
    if asset.path is not None:
        return sha256_file(asset.path)
    if asset.archive_path is None or asset.archive_member is None:
        raise ValidationError(f"NCBI asset has no readable source: {asset.display_path}")
    digest = hashlib.sha256()
    try:
        with zipfile.ZipFile(asset.archive_path) as archive:
            info = archive.getinfo(asset.archive_member)
            _validate_zip_info(info)
            with archive.open(info) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise ValidationError(f"cannot read NCBI ZIP asset {asset.display_path}: {exc}") from exc
    return digest.hexdigest()


def _ingest_dataset_asset(
    db: Database,
    project: Project,
    asset: DatasetAsset,
    entity_type: str,
    entity_id: str,
    *,
    run_id: str,
    standardize: bool,
) -> dict[str, Any]:
    """Ingest one asset while bounding temporary storage to one member."""

    source = asset.path
    move = False
    staging: tempfile.TemporaryDirectory[str] | None = None
    try:
        if source is None:
            if asset.archive_path is None or asset.archive_member is None:
                raise ValidationError(f"NCBI asset has no readable source: {asset.display_path}")
            staging_parent = project.raw_root / ".ncbi_datasets_staging"
            staging_parent.mkdir(parents=True, exist_ok=True)
            required = int(asset.size_bytes or 0) * (2 if standardize else 1)
            _require_disk_space(staging_parent, required, f"archive {asset.archive_member}")
            staging = tempfile.TemporaryDirectory(prefix="asset-", dir=str(staging_parent))
            source = Path(staging.name) / PurePosixPath(asset.archive_member).name
            try:
                with zipfile.ZipFile(asset.archive_path) as archive:
                    info = archive.getinfo(asset.archive_member)
                    _validate_zip_info(info)
                    with archive.open(info) as input_handle, open(source, "wb") as output_handle:
                        shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
                    if source.stat().st_size != info.file_size:
                        raise ValidationError(
                            f"truncated NCBI ZIP member {asset.display_path}: "
                            f"expected {info.file_size} bytes, wrote {source.stat().st_size}"
                        )
            except (KeyError, zipfile.BadZipFile, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                    raise _no_space_error(staging_parent, f"extract {asset.archive_member}", exc) from exc
                raise ValidationError(f"cannot extract NCBI ZIP asset {asset.display_path}: {exc}") from exc
            # Staging and raw live on the same filesystem, so ingest can
            # atomically move the extracted member instead of copying it.
            move = True
        else:
            required = int(asset.size_bytes or source.stat().st_size) * (2 if standardize else 1)
            _require_disk_space(project.raw_root, required, f"archive {source.name}")

        row = ingest_file(
            db,
            project,
            source,
            entity_type,
            entity_id,
            asset.role,
            source_url=asset.source_url,
            move=move,
            run_id=run_id,
            actor=os.environ.get("USER"),
        )
        result = dict(row)
        if standardize:
            standardized = standardize_file(db, project, row["file_id"])
            result["standardized_file_id"] = standardized["file_id"]
        return result
    finally:
        if staging is not None:
            staging.cleanup()


def _require_disk_space(path: Path, required_bytes: int, action: str) -> None:
    """Fail before a large write when the target filesystem is clearly full."""

    path = Path(path)
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    free = shutil.disk_usage(existing).free
    # Keep a small reserve for SQLite, metadata exports and filesystem
    # bookkeeping.  This is intentionally fixed rather than proportional so
    # multi-gigabyte genomes do not receive an excessive safety multiplier.
    reserve = 64 * 1024 * 1024
    needed = max(0, int(required_bytes)) + reserve
    if free < needed:
        raise ValidationError(
            f"insufficient space to {action} on filesystem containing {existing}: "
            f"need about {_format_bytes(needed)}, only {_format_bytes(free)} available. "
            "Free space, reduce --batch-size/--include content, or use "
            "--no-preserve-source when the original package is already archived elsewhere."
        )


def _no_space_error(path: Path, action: str, exc: OSError) -> ValidationError:
    existing = Path(path)
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        free = shutil.disk_usage(existing).free
        available = f" ({_format_bytes(free)} currently available)"
    except OSError:
        available = ""
    return ValidationError(
        f"filesystem ran out of space while attempting to {action} at {existing}{available}. "
        "The NCBI adapter processes one batch at a time; free space, reduce --batch-size or "
        "download fewer --include file types, then rerun (completed batches are idempotent)."
    )


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"


def _open_source(path: Path, project: Project, preserve: bool, label: str | None = None) -> SourceBundle:
    path = path.resolve()
    if not path.exists():
        raise ValidationError(f"NCBI Datasets input does not exist: {path}")
    preserved: Path | None = None
    if preserve and path.is_file():
        preserved = _preserve_source(path, project)
    if path.is_dir():
        return SourceBundle(source=path, root=path, label=label or str(path), preserved_path=preserved)
    if zipfile.is_zipfile(path):
        # Validate archive paths eagerly, but deliberately do not extract the
        # package.  Reports and assets are streamed from the ZIP later.
        with zipfile.ZipFile(path) as archive:
            _validated_zip_infos(archive)
        return SourceBundle(source=path, root=path, label=label or str(path), preserved_path=preserved)
    return SourceBundle(source=path, root=path, label=label or str(path), preserved_path=preserved)


def _preserve_source(path: Path, project: Project, *, move: bool = False) -> Path:
    digest = sha256_file(path)
    suffix = "".join(path.suffixes[-2:]) if len(path.suffixes) > 1 else path.suffix
    suffix = suffix or ".dat"
    target = project.raw_root / "metadata" / "ncbi_datasets" / f"{digest}{suffix}"
    if target.exists():
        if sha256_file(target) != digest:
            raise ConflictError(f"preserved NCBI source {target} has unexpected content")
        if move and path != target:
            path.unlink(missing_ok=True)
        return target
    if move:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, target)
    else:
        _require_disk_space(target.parent, path.stat().st_size, "preserve NCBI source package")
        atomic_copy(path, target)
    return target


def _validated_zip_infos(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Validate member paths/symlinks without materializing archive content."""

    infos = archive.infolist()
    for info in infos:
        _validate_zip_info(info)
    return infos


def _validate_zip_info(info: zipfile.ZipInfo) -> None:
    member = PurePosixPath(info.filename)
    if member.is_absolute() or ".." in member.parts:
        raise ValidationError(f"unsafe path in NCBI dataset ZIP: {info.filename}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ValidationError(f"symbolic link is not allowed in NCBI dataset ZIP: {info.filename}")


def _safe_extract_zip(path: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(path) as archive:
        for info in _validated_zip_infos(archive):
            member = PurePosixPath(info.filename)
            target = (destination / Path(*member.parts)).resolve()
            if destination != target and destination not in target.parents:
                raise ValidationError(f"unsafe path in NCBI dataset ZIP: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _read_report_file(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return _read_report_handle(handle, str(path))
    except UnicodeDecodeError as exc:
        raise ValidationError(f"NCBI report is not UTF-8 text: {path}") from exc


def _read_report_handle(handle: Any, source_name: str) -> list[dict[str, Any]]:
    """Parse a report, streaming the normal Datasets JSONL representation."""

    if source_name.lower().split("!/")[-1].endswith(".jsonl"):
        records: list[dict[str, Any]] = []
        try:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValidationError(
                        f"{source_name}: invalid JSON on line {line_no}: {exc}"
                    ) from exc
                if isinstance(row, dict):
                    records.append(row)
        except UnicodeDecodeError as exc:
            raise ValidationError(f"NCBI report is not UTF-8 text: {source_name}") from exc
        return records

    try:
        text = handle.read()
    except UnicodeDecodeError as exc:
        raise ValidationError(f"NCBI report is not UTF-8 text: {source_name}") from exc
    stripped = text.lstrip()
    if not stripped:
        return []
    if stripped[0] in "[{":
        if stripped[0] == "[":
            value = json.loads(text)
            return [row for row in value if isinstance(row, dict)]
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            records = []
            for line_no, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValidationError(f"{source_name}: invalid JSON on line {line_no}: {exc}") from exc
                if isinstance(row, dict):
                    records.append(row)
            return records
        if isinstance(value, dict):
            for key in ("reports", "assemblies", "data"):
                rows = value.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]
            return [value]
        return []
    return _read_report_tsv(io.StringIO(text), Path(source_name))


def _read_report_tsv(handle: Any, path: Path) -> list[dict[str, Any]]:
    sample = handle.read(4096)
    handle.seek(0)
    delimiter = "\t" if "\t" in sample.partition("\n")[0] else ","
    reader = csv.DictReader(handle, delimiter=delimiter)
    if not reader.fieldnames:
        raise ValidationError(f"{path}: no metadata header found")
    return [_tsv_row_to_report(row) for row in reader]


def _tsv_row_to_report(row: dict[str, str]) -> dict[str, Any]:
    normalized = {_normalize_key(key): value for key, value in row.items() if key is not None}
    def get(*names: str) -> str:
        for name in names:
            value = normalized.get(_normalize_key(name), "").strip()
            if value:
                return value
        return ""
    return {
        "accession": get("assembly accession", "accession", "current accession"),
        "organism": {
            "organismName": get("organism name", "organism", "species name"),
            "taxId": get("organism taxonomic id", "tax id", "taxid"),
            "infraspecificNames": {
                "strain": get("strain"),
                "isolate": get("isolate"),
                "cultivar": get("cultivar"),
            },
        },
        "assemblyInfo": {
            "assemblyLevel": get("assembly level", "level"),
            "assemblyMethod": get("assembly method"),
            "biosample": {"accession": get("assembly biosample accession", "biosample accession", "biosample")},
            "bioprojectAccession": get("assembly bioproject accession", "bioproject accession", "bioproject"),
            "pairedAssembly": {"accession": get("paired assembly accession")},
            "refseqCategory": get("refseq category", "reference status"),
            "releaseDate": get("assembly release date", "release date"),
            "submitter": get("assembly submitter", "submitter"),
        },
    }


def _extract_metadata(report: dict[str, Any]) -> dict[str, Any]:
    assembly_info = _mapping(_pick(report, "assemblyInfo", "assembly_info"))
    organism = _mapping(_pick(report, "organism"))
    biosample = _mapping(_pick(assembly_info, "biosample") or _pick(report, "biosample"))
    infra = _mapping(_pick(organism, "infraspecificNames", "infraspecific_names"))
    paired = _mapping(_pick(assembly_info, "pairedAssembly", "paired_assembly"))
    annotation_info = _mapping(_pick(report, "annotationInfo", "annotation_info"))
    attributes = _biosample_attributes(biosample)
    latitude, longitude = _lat_lon(attributes.get("lat_lon") or attributes.get("latitude_and_longitude"))
    accession = _canonical_accession(str(
        _pick(report, "accession", "currentAccession", "current_accession") or ""
    ))
    current = str(_pick(report, "currentAccession", "current_accession") or "").strip()
    paired_accession = str(_pick(paired, "accession") or "").strip()
    return {
        "accession": accession,
        "current_accession": _canonical_accession(current) if current else None,
        "paired_accession": _canonical_accession(paired_accession) if paired_accession else None,
        "scientific_name": _pick(organism, "organismName", "organism_name", "name"),
        "taxon_id": _pick(organism, "taxId", "tax_id", "taxid"),
        "biosample_accession": _pick(biosample, "accession") or _pick(assembly_info, "biosampleAccession"),
        "bioproject_accession": _pick(assembly_info, "bioprojectAccession", "bioproject_accession") or _pick(report, "bioprojectAccession"),
        "strain": _pick(infra, "strain") or attributes.get("strain"),
        "isolate": _pick(infra, "isolate") or attributes.get("isolate"),
        "cultivar": _pick(infra, "cultivar") or attributes.get("cultivar"),
        "sex": _pick(infra, "sex") or attributes.get("sex"),
        "collection_date": attributes.get("collection_date"),
        "country": attributes.get("geo_loc_name") or attributes.get("country"),
        "latitude": latitude,
        "longitude": longitude,
        "host": attributes.get("host"),
        "assembly_name": _pick(assembly_info, "assemblyName", "assembly_name"),
        "assembly_level": _pick(assembly_info, "assemblyLevel", "assembly_level"),
        "assembly_method": _pick(assembly_info, "assemblyMethod", "assembly_method"),
        "submitter": _pick(assembly_info, "submitter") or _pick(report, "submitter"),
        "release_date": _pick(assembly_info, "releaseDate", "release_date") or _pick(report, "releaseDate"),
        "reference_status": _pick(assembly_info, "refseqCategory", "refseq_category", "referenceStatus"),
        "source_database": _pick(report, "sourceDatabase", "source_database"),
        "assembly_status": _pick(assembly_info, "assemblyStatus", "assembly_status"),
        "assembly_type": _pick(assembly_info, "assemblyType", "assembly_type"),
        "annotation": {
            "provider": _pick(annotation_info, "provider", "name", "annotationProvider"),
            "version": _pick(annotation_info, "version", "annotationVersion"),
            "release_date": _pick(annotation_info, "releaseDate", "release_date"),
        },
    }


def _biosample_attributes(biosample: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("attributes", "sampleAttributes", "sample_attributes"):
        values = biosample.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            name = str(_pick(item, "name", "attributeName", "harmonizedName") or "").strip()
            value = str(_pick(item, "value", "attributeValue") or "").strip()
            if name and value:
                result[_normalize_key(name)] = value
    return result


def _asset_role(path: Path) -> str | None:
    name = path.name.lower()
    if name in {"assembly_data_report.jsonl", "assembly_data_report.json", "dataset_catalog.json"}:
        return None
    if "sequence_report" in name or "assembly_report" in name:
        return "assembly_report"
    if name.endswith((".gff", ".gff3", ".gff.gz", ".gff3.gz")):
        return "annotation_gff3"
    if name.endswith((".faa", ".faa.gz")) and ("protein" in name or name == "protein.faa"):
        return "protein_fasta"
    if "cds" in name and name.endswith((".fna", ".fa", ".fasta", ".fna.gz", ".fa.gz", ".fasta.gz")):
        return "cds_fasta"
    if name.endswith((".fna", ".fa", ".fasta", ".fna.gz", ".fa.gz", ".fasta.gz")):
        if any(token in name for token in ("rna", "cds", "protein")):
            return None
        return "genome_fasta"
    return None


def _accession_from_path(path: Path) -> str:
    # NCBI packages embed the accession in member filenames followed by an
    # underscore suffix (e.g. "GCF_000001405.40_GRCh38.p14_genomic.fna"), where
    # ACCESSION_RE can only match the unversioned base.  A versioned match in
    # any path part (typically the per-accession directory) is more specific
    # and wins over such truncated matches.
    fallback = ""
    for part in reversed(path.parts):
        match = ACCESSION_RE.search(part)
        if not match:
            continue
        value = _canonical_accession(match.group(0))
        if _split_accession(value)[1] is not None:
            return value
        if not fallback:
            fallback = value
    return fallback


def _deduplicate_reports(reports: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_accession: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    for report in reports:
        meta = _extract_metadata(report)
        accession = meta["accession"]
        if not accession:
            continue
        related = _unique([accession, meta.get("current_accession"), meta.get("paired_accession")])
        canonical_key = next((aliases[item] for item in related if item in aliases), accession)
        current = by_accession.get(canonical_key)
        if current is None:
            by_accession[canonical_key] = report
        else:
            by_accession[canonical_key] = _deep_merge(current, report)
        for item in related:
            aliases[item] = canonical_key
    return list(by_accession.values())


def _collect_accessions(values: Sequence[str], accession_file: str | Path | None) -> list[str]:
    collected = list(values)
    if accession_file:
        path = Path(accession_file)
        if not path.exists():
            raise ValidationError(f"accession file does not exist: {path}")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                collected.append(value.split()[0])
    return _unique([_canonical_accession(value) for value in collected])


def _canonical_accession(value: str) -> str:
    value = str(value or "").strip().upper()
    if not value:
        return ""
    match = VERSIONED_ACCESSION_RE.fullmatch(value)
    if not match:
        raise ValidationError(f"invalid NCBI assembly accession: {value!r}")
    return value


def _split_accession(value: str) -> tuple[str, int | None]:
    match = VERSIONED_ACCESSION_RE.fullmatch(str(value or "").strip().upper())
    if not match:
        return str(value or "").strip().upper(), None
    return match.group(1), int(match.group(2)) if match.group(2) else None


def _select_canonical_assembly_accession(
    current: dict[str, Any],
    related: Sequence[str],
    primary: str,
) -> str:
    """Choose a stable canonical accession without arrival-order rewrites."""
    normalized = [_canonical_accession(value) for value in related if value]
    stored = str(current.get("assembly_accession") or "").strip().upper()
    if stored and stored in normalized:
        return stored
    refseq = sorted(value for value in normalized if value.startswith("GCF_"))
    if refseq:
        return refseq[-1]
    return _canonical_accession(primary)


def _assembly_asset_role(role: str, accession: str, canonical: str) -> str:
    """Give alternate GenBank/RefSeq assembly artifacts independent roles."""
    if role not in {"genome_fasta", "assembly_report"}:
        return role
    accession = _canonical_accession(accession)
    canonical = _canonical_accession(canonical)
    if accession == canonical:
        return role
    suffix = "refseq" if accession.startswith("GCF_") else "genbank"
    return f"{role}_{suffix}"


def _annotation_identity(
    assembly_id: str,
    accession: str,
    provider: str,
    version: int,
    release_date: str | None,
) -> str:
    document = {
        "assembly_id": assembly_id,
        "assembly_accession": _canonical_accession(accession),
        "provider": provider.strip().casefold(),
        "version": int(version),
        "release_date": release_date,
    }
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _metadata_identity(meta: dict[str, Any], accession: str) -> str:
    document = dict(meta)
    document["accession"] = _canonical_accession(accession)
    return hashlib.sha256(
        json.dumps(document, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _version_tuple(value: Any) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(part) for part in parts) if parts else (0,)


def _accession_version(value: str) -> int | None:
    return _split_accession(value)[1]


def _assembly_namespace(accession: str) -> str:
    return "NCBI_RefSeq_Assembly" if accession.upper().startswith("GCF_") else "NCBI_GenBank_Assembly"


def _normalize_assembly_level(value: Any) -> str | None:
    normalized = _normalize_key(value)
    mapping = {
        "complete_genome": "complete_genome",
        "chromosome": "chromosome",
        "scaffold": "scaffold",
        "contig": "contig",
    }
    return mapping.get(normalized)


def _normalize_reference_status(value: Any) -> str | None:
    normalized = _normalize_key(value)
    if "reference" in normalized:
        return "reference"
    if "representative" in normalized:
        return "representative"
    if normalized in {"alternate", "alternate_locus"}:
        return "alternate"
    return "other" if normalized else None


def _normalize_source_database(value: Any, accession: str) -> str:
    normalized = _normalize_key(value)
    if "refseq" in normalized or accession.startswith("GCF_"):
        return "RefSeq"
    if "genbank" in normalized or accession.startswith("GCA_"):
        return "GenBank"
    return "other"


def _normalize_sex(value: Any) -> str | None:
    normalized = str(value or "").strip().casefold()
    if normalized in {"female", "male", "hermaphrodite", "unknown", "not collected", "not applicable"}:
        return normalized
    return "unknown" if normalized else None


def _date_only(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d", "%b %d, %Y", "%Y-%m"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def _lat_lon(value: Any) -> tuple[float | None, float | None]:
    text = str(value or "").strip()
    match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*([NS])?\s+([+-]?\d+(?:\.\d+)?)\s*([EW])?", text, re.I)
    if not match:
        return None, None
    lat = float(match.group(1))
    lon = float(match.group(3))
    if (match.group(2) or "").upper() == "S":
        lat = -abs(lat)
    if (match.group(4) or "").upper() == "W":
        lon = -abs(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def _normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def _pick(mapping: dict[str, Any], *names: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    by_key = {_normalize_key(key): value for key, value in mapping.items()}
    for name in names:
        value = by_key.get(_normalize_key(name))
        if value not in (None, "", [], {}):
            return value
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _merge_nonempty(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(existing or {})
    for key, value in incoming.items():
        if value not in (None, ""):
            result[key] = value
        elif key not in result:
            result[key] = None
    return result


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        elif value not in (None, "", [], {}):
            result[key] = value
    return result


def _unique(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        if value in (None, "") or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _integer_or_none(value: Any) -> int | None:
    try:
        return int(str(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(str(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _report_has_accession(report: dict[str, Any], accession: str) -> bool:
    meta = _extract_metadata(report)
    canonical = _canonical_accession(accession)
    return canonical in {meta.get("accession"), meta.get("current_accession"), meta.get("paired_accession")}


def _chunks(values: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(values), size):
        yield list(values[start:start + size])
