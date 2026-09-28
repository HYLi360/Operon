"""Import planning and database application for the NCBI Datasets adapter."""

from __future__ import annotations

import errno
import hashlib
import re
import shutil
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from operon.config import Project, resolve_actor
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
from operon.sql import quote_identifier
from operon.utils import atomic_write_text, now_iso, sha256_file

from ._ncbi_model import (
    NCBI_ASSEMBLY_SCHEMA_FIELDS,
    DatasetAsset,
    ImportPlan,
    _accession_version,
    _annotation_identity,
    _assembly_asset_role,
    _assembly_namespace,
    _canonical_accession,
    _date_only,
    _deduplicate_reports,
    _extract_metadata,
    _float_or_none,
    _integer_or_none,
    _merge_nonempty,
    _metadata_identity,
    _normalize_assembly_level,
    _normalize_reference_status,
    _normalize_sex,
    _normalize_source_database,
    _select_canonical_assembly_accession,
    _split_accession,
    _unique,
    _version_tuple,
)
from ._ncbi_sources import _validate_zip_info
from ._ncbi_storage import _no_space_error, _require_disk_space

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
                ),  # nosec B608 # fixed mappings or validated schema identifiers; values are bound
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
                    + supersession_filter + retirement_filter,  # nosec B608 # fixed mappings or validated schema identifiers; values are bound
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


class _IdAllocator:
    def __init__(self, db: Database):
        self.next_numbers: dict[str, int] = {}
        for entity_type, prefix in ENTITY_PREFIXES.items():
            if entity_type == "file":
                continue
            table = ENTITY_TABLES[entity_type]
            id_col = ENTITY_ID_COLUMNS[entity_type]
            maximum = 0
            for row in db.conn.execute(f"SELECT {id_col} AS value FROM {table}"):  # nosec B608 # fixed mappings or validated schema identifiers; values are bound
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
                for row in db.conn.execute(f"SELECT * FROM {table}")  # nosec B608 # fixed mappings or validated schema identifiers; values are bound
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
            if not accession:  # pragma: no cover
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
        normalized: dict[str, list[dict[str, Any]]],
) -> None:
    """Persist one batch's plan rows.

    ``normalized`` is the batch's row set already run through
    :func:`_validate_plan_rows` by the caller; re-validating here would repeat
    the whole schema normalization once more per batch.  ``schema`` is the
    same adapter schema with the legacy-field upgrade persisted, so the
    column projections stay aligned.
    """
    with db.transaction() as conn:
        db.ensure_metadata_columns(schema)
        for table in ("organisms", "samples", "assemblies", "annotations", "accessions"):
            rows = normalized[table]
            if not rows:
                continue
            columns = schema.columns(table)
            keys = db._primary_keys(table)
            assignments = ", ".join(
                f"{quote_identifier(col)}=excluded.{quote_identifier(col)}"
                for col in columns if col not in keys
            )
            sql = (
                f"INSERT INTO {quote_identifier(table)} ({', '.join(quote_identifier(c) for c in columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)}) "
                f"ON CONFLICT({','.join(quote_identifier(key) for key in keys)}) DO UPDATE SET {assignments}"  # nosec B608 # fixed mappings or validated schema identifiers; values are bound
            )
            for row in rows:
                where = " AND ".join(f"{key}=?" for key in keys)
                existing = conn.execute(
                    f"SELECT * FROM {table} WHERE {where}", [row.get(key) for key in keys]  # nosec B608 # fixed mappings or validated schema identifiers; values are bound
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
                            "NCBI Datasets metadata import", None, resolve_actor(),
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
            actor=resolve_actor(),
        )
        result = dict(row)
        if standardize:
            standardized = standardize_file(db, project, row["file_id"])
            result["standardized_file_id"] = standardized["file_id"]
        return result
    finally:
        if staging is not None:
            staging.cleanup()
