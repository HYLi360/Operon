"""Frozen NCBI Taxonomy snapshots and compiled coverage denominators."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import tarfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TextIO

import yaml

from operon import __version__
from operon.config import Project, project_rel
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.profiles import load_profile
from operon.utils import (
    atomic_copy,
    atomic_write_text,
    now_iso,
    sha256_file,
)
from operon.workflow import log_run, new_run_id

PROFILE_KIND = "taxonomy_coverage"
REFERENCE_SET_SCHEMA = "operon-taxonomy-reference-set-1"
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")
TARGET_RANK_ORDER = {"family": 0, "genus": 1}


def canonical_document(document: dict[str, Any]) -> tuple[str, str]:
    text = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_token(value: str, label: str) -> str:
    value = str(value)
    if not SAFE_TOKEN.fullmatch(value):
        raise ValidationError(
            f"invalid {label} {value!r}; use only letters, digits, '.', '_' and '-'"
        )
    return value


def _next_snapshot_id(db: Database) -> str:
    maximum = 0
    for row in db.query("SELECT taxonomy_snapshot_id FROM taxonomy_snapshots"):
        match = re.fullmatch(r"TAX_(\d+)", str(row["taxonomy_snapshot_id"]))
        if match:
            maximum = max(maximum, int(match.group(1)))
    return f"TAX_{maximum + 1:06d}"


def _ensure_taxonomy_metadata_schema(project: Project) -> None:
    """Upgrade only the manifest vocabulary needed by taxonomy source files."""
    try:
        document = yaml.safe_load(project.schema_path.read_text(encoding="utf-8")) or {}
        fields = document["tables"]["files"]["fields"]
    except (OSError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise ValidationError(f"cannot upgrade project metadata schema: {project.schema_path}") from exc
    changed = False
    entity_type = fields["entity_type"]
    allowed_types = list(entity_type.get("allowed", []))
    if "taxonomy_snapshot" not in allowed_types:
        allowed_types.append("taxonomy_snapshot")
        entity_type["allowed"] = allowed_types
        changed = True
    entity_id = fields["entity_id"]
    expected_pattern = r"^(ORG|SMP|RUN|ASM|ANN|TAX)_\d{6}$"
    current_pattern = str(entity_id.get("pattern") or "")
    try:
        supports_taxonomy_id = bool(re.fullmatch(current_pattern, "TAX_000001"))
    except re.error as exc:
        raise ValidationError(
            f"cannot extend invalid files.entity_id pattern in {project.schema_path}: {exc}"
        ) from exc
    if not supports_taxonomy_id:
        legacy_pattern = r"^(ORG|SMP|RUN|ASM|ANN)_\d{6}$"
        entity_id["pattern"] = (
            expected_pattern
            if current_pattern in {"", legacy_pattern}
            else rf"(?:{current_pattern})|(?:^TAX_\d{{6}}$)"
        )
        changed = True
    roles = list(fields["file_role"].get("allowed", []))
    if "taxonomy_package" not in roles:
        insert_at = roles.index("other") if "other" in roles else len(roles)
        roles.insert(insert_at, "taxonomy_package")
        fields["file_role"]["allowed"] = roles
        changed = True
    version_parts = tuple(
        int(part) for part in re.findall(r"\d+", str(document.get("schema_version") or "0"))
    )
    if version_parts < (1, 3):
        document["schema_version"] = "1.3"
        changed = True
    if changed:
        project.schema_path.write_text(
            "# Operon metadata schema (YAML). Extended for NCBI Taxonomy snapshots.\n"
            + yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


def _source_suffix(path: Path) -> str:
    suffixes = path.suffixes
    if len(suffixes) >= 2 and suffixes[-2:] in ([".tar", ".gz"], [".tar", ".bz2"], [".tar", ".xz"]):
        return "".join(suffixes[-2:])
    return path.suffix or ".dat"


def _archive_member_basenames(path: Path) -> set[str]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return {Path(name).name for name in archive.namelist()}
    if tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as archive:
            return {Path(member.name).name for member in archive.getmembers() if member.isfile()}
    return set()


def _taxonomy_source_format(path: Path) -> str:
    members = _archive_member_basenames(path)
    if {"nodes.dmp", "names.dmp"}.issubset(members):
        return "ncbi_taxdump"
    if members and not {"taxonomy_report.jsonl", "taxonomy_report.json"}.intersection(members):
        raise ValidationError(
            f"{path}: expected nodes.dmp + names.dmp or taxonomy_report.jsonl in archive"
        )
    return "ncbi_datasets_jsonl"


@contextmanager
def _archive_text_member(path: Path, basename: str) -> Iterator[TextIO]:
    """Open one archive member by basename without extracting the package."""
    if zipfile.is_zipfile(path):
        archive = zipfile.ZipFile(path)
        try:
            candidates = sorted(name for name in archive.namelist() if Path(name).name == basename)
            if not candidates:
                raise ValidationError(f"{path}: archive member {basename} not found")
            binary = archive.open(candidates[0])
            text = io.TextIOWrapper(binary, encoding="utf-8")
            try:
                yield text
            finally:
                text.close()
        finally:
            archive.close()
        return
    if tarfile.is_tarfile(path):
        archive = tarfile.open(path, "r:*")
        try:
            candidates = sorted(
                (member for member in archive.getmembers()
                 if member.isfile() and Path(member.name).name == basename),
                key=lambda member: member.name,
            )
            if not candidates:
                raise ValidationError(f"{path}: archive member {basename} not found")
            binary = archive.extractfile(candidates[0])
            if binary is None:
                raise ValidationError(f"{path}: cannot read archive member {basename}")
            text = io.TextIOWrapper(binary, encoding="utf-8")
            try:
                yield text
            finally:
                text.close()
        finally:
            archive.close()
        return
    raise ValidationError(f"{path}: taxdump input must be a ZIP or tar archive")


@contextmanager
def _taxonomy_text(path: Path) -> Iterator[TextIO]:
    """Open the taxonomy JSONL member from a Datasets package or direct file."""
    if zipfile.is_zipfile(path):
        archive = zipfile.ZipFile(path)
        try:
            candidates = sorted(
                name for name in archive.namelist()
                if Path(name).name in {"taxonomy_report.jsonl", "taxonomy_report.json"}
            )
            if not candidates:
                raise ValidationError(f"{path}: no taxonomy_report.jsonl found in ZIP")
            binary = archive.open(candidates[0])
            text = io.TextIOWrapper(binary, encoding="utf-8")
            try:
                yield text
            finally:
                text.close()
        finally:
            archive.close()
        return
    if tarfile.is_tarfile(path):
        archive = tarfile.open(path, "r:*")
        try:
            candidates = sorted(
                member for member in archive.getmembers()
                if member.isfile() and Path(member.name).name in {"taxonomy_report.jsonl", "taxonomy_report.json"}
            )
            if not candidates:
                raise ValidationError(f"{path}: no taxonomy_report.jsonl found in archive")
            binary = archive.extractfile(candidates[0])
            if binary is None:
                raise ValidationError(f"{path}: cannot read taxonomy report member")
            text = io.TextIOWrapper(binary, encoding="utf-8")
            try:
                yield text
            finally:
                text.close()
        finally:
            archive.close()
        return
    with open(path, encoding="utf-8") as handle:
        yield handle


def _pick(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _taxid(value: Any) -> int | None:
    if isinstance(value, dict):
        value = _pick(value, "taxId", "tax_id", "taxid", "id")
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _taxids(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    result: list[int] = []
    for item in value:
        parsed = _taxid(item)
        if parsed is not None:
            result.append(parsed)
    return result


def _scientific_name(node: dict[str, Any]) -> str:
    value = _pick(node, "currentScientificName", "current_scientific_name", "scientificName", "taxName")
    if isinstance(value, dict):
        value = _pick(value, "name", "scientificName", "value", "text")
    return str(value or "").strip()


def _unwrap_taxonomy_record(record: dict[str, Any]) -> dict[str, Any]:
    for key in ("taxonomy", "taxonomyNode", "taxonomy_node"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
    return record


def _iter_taxonomy_records(handle: TextIO, source: str) -> Iterator[dict[str, Any]]:
    for line_number, raw in enumerate(handle, 1):
        text = raw.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{source}: invalid taxonomy JSON on line {line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise ValidationError(f"{source}: taxonomy JSON line {line_number} is not an object")
        yield _unwrap_taxonomy_record(record)


def _normalized_node(node: dict[str, Any], source: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    taxid = _taxid(_pick(node, "taxId", "tax_id", "taxid"))
    if taxid is None:
        raise ValidationError(f"{source}: taxonomy record has no valid taxId")
    parents = _taxids(_pick(node, "parents", "parentTaxIds", "parent_tax_ids"))
    parent = _taxid(_pick(node, "parentTaxId", "parent_tax_id"))
    if parent is None and parents:
        parent = parents[0]
    if parent is None and taxid == 1:
        parent = 1
    rank = str(_pick(node, "rank") or "no_rank").strip().lower().replace(" ", "_")
    name = _scientific_name(node)
    if not name:
        raise ValidationError(f"{source}: taxonomy record {taxid} has no current scientific name")
    extinct_value = _pick(node, "extinct", "isExtinct", "is_extinct")
    formal_value = _pick(
        node, "currentScientificNameIsFormal", "current_scientific_name_is_formal", "isFormal"
    )
    normalized = {
        "taxid": taxid,
        "parent_taxid": parent,
        "rank": rank,
        "scientific_name": name,
        # NCBI Datasets JSON represents an omitted boolean as the protobuf
        # default (false).  Taxdump imports deliberately use NULL because the
        # legacy dump has no equivalent field.
        "is_extinct": int(bool(extinct_value)),
        "is_formal": None if formal_value is None else int(bool(formal_value)),
    }
    aliases = [
        {"alias_taxid": alias, "current_taxid": taxid, "status": "secondary"}
        for alias in sorted(set(_taxids(_pick(node, "secondaryTaxIds", "secondary_tax_ids"))))
        if alias != taxid
    ]
    return normalized, aliases


def _dmp_fields(raw: str, source: str, line_number: int, minimum: int) -> list[str]:
    fields = [field.strip() for field in raw.rstrip("\r\n").split("|")]
    if len(fields) < minimum:
        raise ValidationError(
            f"{source}: malformed NCBI taxdump line {line_number}; expected at least {minimum} fields"
        )
    return fields


def _import_taxdump(db: Database, snapshot_id: str, path: Path) -> int:
    """Stream an official NCBI taxdump archive through temporary SQLite tables."""
    members = _archive_member_basenames(path)
    if not {"nodes.dmp", "names.dmp"}.issubset(members):
        raise ValidationError(f"{path}: NCBI taxdump requires nodes.dmp and names.dmp")
    db.conn.execute("DROP TABLE IF EXISTS temp.taxonomy_import_names")
    db.conn.execute("DROP TABLE IF EXISTS temp.taxonomy_import_nodes")
    db.conn.execute(
        "CREATE TEMP TABLE taxonomy_import_names("
        "taxid INTEGER PRIMARY KEY, scientific_name TEXT NOT NULL)"
    )
    db.conn.execute(
        "CREATE TEMP TABLE taxonomy_import_nodes("
        "taxid INTEGER PRIMARY KEY, parent_taxid INTEGER, rank TEXT NOT NULL)"
    )
    try:
        batch: list[tuple[Any, ...]] = []
        with _archive_text_member(path, "names.dmp") as handle:
            for line_number, raw in enumerate(handle, 1):
                fields = _dmp_fields(raw, f"{path}:names.dmp", line_number, 4)
                if fields[3] != "scientific name":
                    continue
                taxid = _taxid(fields[0])
                if taxid is None or not fields[1]:
                    raise ValidationError(
                        f"{path}:names.dmp: invalid scientific-name row {line_number}"
                    )
                batch.append((taxid, fields[1]))
                if len(batch) >= 5000:
                    db.conn.executemany(
                        "INSERT INTO taxonomy_import_names(taxid, scientific_name) VALUES(?,?)",
                        batch,
                    )
                    batch.clear()
        if batch:
            db.conn.executemany(
                "INSERT INTO taxonomy_import_names(taxid, scientific_name) VALUES(?,?)", batch
            )
        batch.clear()
        with _archive_text_member(path, "nodes.dmp") as handle:
            for line_number, raw in enumerate(handle, 1):
                fields = _dmp_fields(raw, f"{path}:nodes.dmp", line_number, 3)
                taxid = _taxid(fields[0])
                parent_taxid = _taxid(fields[1])
                if taxid is None or parent_taxid is None:
                    raise ValidationError(f"{path}:nodes.dmp: invalid TaxID on row {line_number}")
                rank = (fields[2] or "no rank").lower().replace(" ", "_")
                batch.append((taxid, parent_taxid, rank))
                if len(batch) >= 5000:
                    db.conn.executemany(
                        "INSERT INTO taxonomy_import_nodes(taxid, parent_taxid, rank) VALUES(?,?,?)",
                        batch,
                    )
                    batch.clear()
        if batch:
            db.conn.executemany(
                "INSERT INTO taxonomy_import_nodes(taxid, parent_taxid, rank) VALUES(?,?,?)", batch
            )
        missing_name = db.conn.execute(
            "SELECT n.taxid FROM taxonomy_import_nodes n "
            "LEFT JOIN taxonomy_import_names s ON s.taxid=n.taxid "
            "WHERE s.taxid IS NULL ORDER BY n.taxid LIMIT 1"
        ).fetchone()
        if missing_name:
            raise ValidationError(
                f"{path}: names.dmp has no scientific name for TaxID {missing_name['taxid']}"
            )
        node_count = int(db.conn.execute(
            "SELECT COUNT(*) AS n FROM taxonomy_import_nodes"
        ).fetchone()["n"])
        if node_count == 0:
            raise ValidationError(f"{path}: nodes.dmp contains no taxonomy nodes")
        db.conn.execute(
            "INSERT INTO taxonomy_nodes(taxonomy_snapshot_id, taxid, parent_taxid, rank, "
            "scientific_name, is_extinct, is_formal) "
            "SELECT ?, n.taxid, n.parent_taxid, n.rank, s.scientific_name, NULL, NULL "
            "FROM taxonomy_import_nodes n JOIN taxonomy_import_names s ON s.taxid=n.taxid",
            (snapshot_id,),
        )
        if "merged.dmp" in members:
            alias_batch: list[tuple[Any, ...]] = []
            with _archive_text_member(path, "merged.dmp") as handle:
                for line_number, raw in enumerate(handle, 1):
                    fields = _dmp_fields(raw, f"{path}:merged.dmp", line_number, 2)
                    alias_taxid = _taxid(fields[0])
                    current_taxid = _taxid(fields[1])
                    if alias_taxid is None or current_taxid is None:
                        raise ValidationError(f"{path}:merged.dmp: invalid row {line_number}")
                    alias_batch.append((snapshot_id, alias_taxid, current_taxid, "merged"))
                    if len(alias_batch) >= 5000:
                        db.conn.executemany(
                            "INSERT INTO taxonomy_aliases(taxonomy_snapshot_id, alias_taxid, "
                            "current_taxid, status) VALUES(?,?,?,?)",
                            alias_batch,
                        )
                        alias_batch.clear()
            if alias_batch:
                db.conn.executemany(
                    "INSERT INTO taxonomy_aliases(taxonomy_snapshot_id, alias_taxid, "
                    "current_taxid, status) VALUES(?,?,?,?)",
                    alias_batch,
                )
        if "delnodes.dmp" in members:
            deleted_batch: list[tuple[Any, ...]] = []
            with _archive_text_member(path, "delnodes.dmp") as handle:
                for line_number, raw in enumerate(handle, 1):
                    fields = _dmp_fields(raw, f"{path}:delnodes.dmp", line_number, 1)
                    alias_taxid = _taxid(fields[0])
                    if alias_taxid is None:
                        raise ValidationError(f"{path}:delnodes.dmp: invalid row {line_number}")
                    deleted_batch.append((snapshot_id, alias_taxid, None, "deleted"))
                    if len(deleted_batch) >= 5000:
                        db.conn.executemany(
                            "INSERT INTO taxonomy_aliases(taxonomy_snapshot_id, alias_taxid, "
                            "current_taxid, status) VALUES(?,?,?,?)",
                            deleted_batch,
                        )
                        deleted_batch.clear()
            if deleted_batch:
                db.conn.executemany(
                    "INSERT INTO taxonomy_aliases(taxonomy_snapshot_id, alias_taxid, "
                    "current_taxid, status) VALUES(?,?,?,?)",
                    deleted_batch,
                )
        return node_count
    finally:
        db.conn.execute("DROP TABLE IF EXISTS temp.taxonomy_import_nodes")
        db.conn.execute("DROP TABLE IF EXISTS temp.taxonomy_import_names")


def import_ncbi_taxonomy(
        db: Database,
        project: Project,
        source: str | Path,
        taxonomy_version: str,
) -> dict[str, Any]:
    """Archive and import one immutable NCBI Datasets taxonomy JSONL package."""
    taxonomy_version = _safe_token(taxonomy_version, "taxonomy version")
    source_path = Path(source).resolve()
    if not source_path.is_file():
        raise ValidationError(f"NCBI taxonomy input must be a file: {source_path}")
    source_sha = sha256_file(source_path)
    source_size = source_path.stat().st_size
    source_format = _taxonomy_source_format(source_path)
    existing = db.conn.execute(
        "SELECT * FROM taxonomy_snapshots WHERE source='NCBI' AND taxonomy_version=?",
        (taxonomy_version,),
    ).fetchone()
    if existing:
        if existing["source_sha256"] != source_sha or existing["source_size_bytes"] != source_size:
            raise ConflictError(
                f"NCBI taxonomy version {taxonomy_version!r} already refers to different bytes"
            )
        file_row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (existing["source_file_id"],)).fetchone()
        if not file_row:
            raise ConflictError(f"taxonomy snapshot {existing['taxonomy_snapshot_id']} has no source manifest row")
        archived = project.root / file_row["relative_path"]
        if not archived.is_file() or sha256_file(archived) != source_sha:
            raise ConflictError(f"archived taxonomy source is missing or has changed: {archived}")
        return {
            **dict(existing), "source_format": source_format,
            "reused": True, "path": str(archived),
        }

    _ensure_taxonomy_metadata_schema(project)
    snapshot_id = _next_snapshot_id(db)
    target = project.raw_root / "metadata" / "ncbi_taxonomy" / f"{source_sha}{_source_suffix(source_path)}"
    if target.exists():
        if not target.is_file() or sha256_file(target) != source_sha:
            raise ConflictError(f"preserved taxonomy source conflicts with {target}")
    else:
        atomic_copy(source_path, target)
    file_id = db.next_id("file")
    imported_at = now_iso()
    run_id = new_run_id()
    node_count = 0
    try:
        with db.transaction():
            db.conn.execute(
                "INSERT INTO files(file_id, entity_type, entity_id, file_role, format, compression, "
                "relative_path, source_url, size_bytes, sha256, downloaded_at, status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    file_id, "taxonomy_snapshot", snapshot_id, "taxonomy_package", "other", "none",
                    project_rel(project, target), str(source_path), source_size, source_sha,
                    imported_at, "CHECKSUM_VERIFIED",
                ),
            )
            db.conn.execute(
                "INSERT INTO taxonomy_snapshots(taxonomy_snapshot_id, source, taxonomy_version, "
                "source_file_id, source_sha256, source_size_bytes, imported_at, node_count, status) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (snapshot_id, "NCBI", taxonomy_version, file_id, source_sha, source_size, imported_at, 0, "IMPORTING"),
            )
            if source_format == "ncbi_taxdump":
                node_count = _import_taxdump(db, snapshot_id, target)
            else:
                node_batch: list[tuple[Any, ...]] = []
                alias_batch: list[tuple[Any, ...]] = []
                with _taxonomy_text(target) as handle:
                    for raw_node in _iter_taxonomy_records(handle, str(source_path)):
                        node, aliases = _normalized_node(raw_node, str(source_path))
                        node_batch.append((
                            snapshot_id, node["taxid"], node["parent_taxid"], node["rank"],
                            node["scientific_name"], node["is_extinct"], node["is_formal"],
                        ))
                        alias_batch.extend(
                            (snapshot_id, alias["alias_taxid"], alias["current_taxid"], alias["status"])
                            for alias in aliases
                        )
                        if len(node_batch) >= 5000:
                            db.conn.executemany(
                                "INSERT INTO taxonomy_nodes(taxonomy_snapshot_id, taxid, parent_taxid, rank, "
                                "scientific_name, is_extinct, is_formal) VALUES(?,?,?,?,?,?,?)",
                                node_batch,
                            )
                            node_count += len(node_batch)
                            node_batch.clear()
                        if len(alias_batch) >= 5000:
                            db.conn.executemany(
                                "INSERT INTO taxonomy_aliases(taxonomy_snapshot_id, alias_taxid, "
                                "current_taxid, status) VALUES(?,?,?,?)",
                                alias_batch,
                            )
                            alias_batch.clear()
                if node_batch:
                    db.conn.executemany(
                        "INSERT INTO taxonomy_nodes(taxonomy_snapshot_id, taxid, parent_taxid, rank, "
                        "scientific_name, is_extinct, is_formal) VALUES(?,?,?,?,?,?,?)",
                        node_batch,
                    )
                    node_count += len(node_batch)
                if alias_batch:
                    db.conn.executemany(
                        "INSERT INTO taxonomy_aliases(taxonomy_snapshot_id, alias_taxid, "
                        "current_taxid, status) VALUES(?,?,?,?)",
                        alias_batch,
                    )
            if node_count == 0:
                raise ValidationError(f"{source_path}: taxonomy report contains no records")
            missing_parent = db.conn.execute(
                "SELECT n.taxid, n.parent_taxid FROM taxonomy_nodes n "
                "LEFT JOIN taxonomy_nodes p ON p.taxonomy_snapshot_id=n.taxonomy_snapshot_id "
                "AND p.taxid=n.parent_taxid WHERE n.taxonomy_snapshot_id=? "
                "AND n.parent_taxid IS NOT NULL AND n.parent_taxid<>n.taxid "
                "AND p.taxid IS NULL ORDER BY n.taxid LIMIT 1",
                (snapshot_id,),
            ).fetchone()
            if missing_parent:
                raise ValidationError(
                    f"{source_path}: taxonomy snapshot is incomplete; TaxID "
                    f"{missing_parent['taxid']} refers to missing parent "
                    f"{missing_parent['parent_taxid']}"
                )
            missing_alias_target = db.conn.execute(
                "SELECT a.alias_taxid, a.current_taxid FROM taxonomy_aliases a "
                "LEFT JOIN taxonomy_nodes n ON n.taxonomy_snapshot_id=a.taxonomy_snapshot_id "
                "AND n.taxid=a.current_taxid WHERE a.taxonomy_snapshot_id=? "
                "AND a.current_taxid IS NOT NULL AND n.taxid IS NULL "
                "ORDER BY a.alias_taxid LIMIT 1",
                (snapshot_id,),
            ).fetchone()
            if missing_alias_target:
                raise ValidationError(
                    f"{source_path}: alias TaxID {missing_alias_target['alias_taxid']} "
                    f"refers to missing current TaxID {missing_alias_target['current_taxid']}"
                )
            db.conn.execute(
                "UPDATE taxonomy_snapshots SET node_count=?, status='READY' WHERE taxonomy_snapshot_id=?",
                (node_count, snapshot_id),
            )
            db.conn.execute(
                "INSERT INTO entity_state(entity_type, entity_id, state, message, updated_at) VALUES(?,?,?,?,?)",
                ("taxonomy_snapshot", snapshot_id, "CHECKSUM_VERIFIED", f"NCBI Taxonomy {taxonomy_version}",
                 imported_at),
            )
            db.conn.execute(
                "INSERT INTO changes(object_type, object_id, field, old_value, new_value, reason, evidence, actor, changed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "taxonomy_snapshot", snapshot_id, "imported_snapshot", None,
                    json.dumps({
                        "taxonomy_version": taxonomy_version,
                        "source_format": source_format,
                        "source_file_id": file_id,
                        "sha256": source_sha,
                        "size_bytes": source_size,
                        "node_count": node_count,
                    }, sort_keys=True),
                    "explicit NCBI Taxonomy import", project_rel(project, target),
                    os.environ.get("USER"), imported_at,
                ),
            )
        log_run(db, project, {
            "run_id": run_id,
            "entity_type": "taxonomy_snapshot",
            "entity_id": snapshot_id,
            "step": "taxonomy_import",
            "status": "completed",
            "started_at": imported_at,
            "finished_at": now_iso(),
            "tool": "operon.taxonomy",
            "tool_version": __version__,
            "parameter_set": taxonomy_version,
            "input_sha256": source_sha,
            "command": f"taxonomy import {source_path}",
        })
    except Exception as exc:
        try:
            log_run(db, project, {
                "run_id": run_id,
                "entity_type": "taxonomy_snapshot",
                "entity_id": snapshot_id,
                "step": "taxonomy_import",
                "status": "failed",
                "started_at": imported_at,
                "finished_at": now_iso(),
                "tool": "operon.taxonomy",
                "tool_version": __version__,
                "parameter_set": taxonomy_version,
                "input_sha256": source_sha,
                "error": f"{type(exc).__name__}: {exc}",
            })
        except Exception:
            pass
        raise
    return {
        "taxonomy_snapshot_id": snapshot_id,
        "source": "NCBI",
        "source_format": source_format,
        "taxonomy_version": taxonomy_version,
        "source_file_id": file_id,
        "source_sha256": source_sha,
        "source_size_bytes": source_size,
        "node_count": node_count,
        "status": "READY",
        "path": str(target),
        "reused": False,
    }


def _validate_coverage_profile(profile_name: str, profile: dict[str, Any]) -> dict[str, Any]:
    try:
        profile_version = int(profile.get("version"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("coverage profile version must be a positive integer") from exc
    if profile_version < 1:
        raise ValidationError("coverage profile version must be a positive integer")
    declared_name = profile.get("name")
    if declared_name is not None and str(declared_name) != profile_name:
        raise ValidationError(
            f"coverage profile name {declared_name!r} does not match filename {profile_name!r}"
        )
    taxonomy = profile.get("taxonomy") or {}
    if not isinstance(taxonomy, dict):
        raise ValidationError("coverage profile taxonomy must be a mapping")
    if taxonomy.get("source") != "NCBI":
        raise ValidationError("taxonomy coverage profiles currently require taxonomy.source: NCBI")
    scope = profile.get("scope") or {}
    if not isinstance(scope, dict):
        raise ValidationError("coverage profile scope must be a mapping")
    roots = scope.get("root_taxids")
    if not isinstance(roots, list) or not roots:
        raise ValidationError("coverage profile scope.root_taxids must be a non-empty list")
    try:
        root_taxids = sorted({int(item) for item in roots})
    except (TypeError, ValueError) as exc:
        raise ValidationError("coverage profile root_taxids must be integers") from exc
    if any(taxid <= 0 for taxid in root_taxids):
        raise ValidationError("coverage profile root_taxids must be positive integers")
    targets = profile.get("targets") or {}
    if not isinstance(targets, dict):
        raise ValidationError("coverage profile targets must be a mapping")
    ranks = targets.get("ranks")
    if not isinstance(ranks, list) or not ranks:
        raise ValidationError("coverage profile targets.ranks must be a non-empty list")
    ranks = [str(rank).lower() for rank in ranks]
    if len(ranks) != len(set(ranks)) or not set(ranks).issubset(TARGET_RANK_ORDER):
        raise ValidationError("coverage profile target ranks must be unique values from: family, genus")
    ranks = sorted(ranks, key=TARGET_RANK_ORDER.__getitem__)
    filters = profile.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValidationError("coverage profile filters must be a mapping")
    try:
        excluded_taxids = sorted({int(item) for item in filters.get("exclude_subtrees", [])})
    except (TypeError, ValueError) as exc:
        raise ValidationError("filters.exclude_subtrees must contain integer TaxIDs") from exc
    if any(taxid <= 0 for taxid in excluded_taxids):
        raise ValidationError("filters.exclude_subtrees must contain positive TaxIDs")
    exclude_extinct = filters.get("exclude_extinct", False)
    if not isinstance(exclude_extinct, bool):
        raise ValidationError("filters.exclude_extinct must be true or false")
    patterns = filters.get("exclude_name_patterns", [])
    if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
        raise ValidationError("filters.exclude_name_patterns must be a list of regular expressions")
    try:
        compiled_patterns = [re.compile(item) for item in patterns]
    except re.error as exc:
        raise ValidationError(f"invalid coverage exclusion regular expression: {exc}") from exc
    thresholds = profile.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        raise ValidationError("coverage profile thresholds must be a mapping")
    normalized_thresholds: dict[str, float] = {}
    if set(thresholds) != set(ranks):
        raise ValidationError("coverage profile thresholds must contain exactly the configured target ranks")
    for rank in ranks:
        rank_threshold = thresholds.get(rank) or {}
        if not isinstance(rank_threshold, dict):
            raise ValidationError(f"thresholds.{rank} must be a mapping")
        value = rank_threshold.get("min_coverage_percent")
        try:
            value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"thresholds.{rank}.min_coverage_percent must be numeric") from exc
        if not math.isfinite(value) or value < 0 or value > 100:
            raise ValidationError(f"thresholds.{rank}.min_coverage_percent must be between 0 and 100")
        normalized_thresholds[rank] = value
    return {
        "root_taxids": root_taxids,
        "ranks": ranks,
        "exclude_extinct": exclude_extinct,
        "exclude_subtrees": excluded_taxids,
        "exclude_name_patterns": patterns,
        "compiled_name_patterns": compiled_patterns,
        "thresholds": normalized_thresholds,
    }


def _descendant_targets(
        db: Database,
        snapshot_id: str,
        roots: list[int],
        excluded_roots: list[int],
        ranks: list[str],
        exclude_extinct: bool,
) -> list[dict[str, Any]]:
    root_rows = db.conn.execute(
        f"SELECT taxid FROM taxonomy_nodes WHERE taxonomy_snapshot_id=? AND taxid IN ({','.join('?' for _ in roots)})",
        [snapshot_id, *roots],
    ).fetchall()
    found_roots = {int(row["taxid"]) for row in root_rows}
    missing = sorted(set(roots) - found_roots)
    if missing:
        raise ValidationError(f"root TaxID(s) not found in taxonomy snapshot: {missing}")
    if excluded_roots:
        excluded_rows = db.conn.execute(
            f"SELECT taxid FROM taxonomy_nodes WHERE taxonomy_snapshot_id=? AND taxid IN ({','.join('?' for _ in excluded_roots)})",
            [snapshot_id, *excluded_roots],
        ).fetchall()
        missing_excluded = sorted(set(excluded_roots) - {int(row["taxid"]) for row in excluded_rows})
        if missing_excluded:
            raise ValidationError(f"excluded subtree TaxID(s) not found: {missing_excluded}")
    root_values = ",".join("(?)" for _ in roots)
    rank_values = ",".join("?" for _ in ranks)
    params: list[Any] = [*roots, snapshot_id]
    excluded_cte = ""
    excluded_clause = ""
    if excluded_roots:
        excluded_values = ",".join("(?)" for _ in excluded_roots)
        excluded_cte = (
            f", excluded(taxid) AS (SELECT column1 FROM (VALUES {excluded_values}) "
            "UNION SELECT n.taxid FROM excluded e CROSS JOIN taxonomy_nodes n "
            "WHERE n.parent_taxid=e.taxid AND n.taxonomy_snapshot_id=? AND n.taxid<>e.taxid)"
        )
        params.extend(excluded_roots)
        params.append(snapshot_id)
        excluded_clause = "AND NOT EXISTS (SELECT 1 FROM excluded e WHERE e.taxid=n.taxid)"
    extinct_cte = ""
    extinct_clause = ""
    if exclude_extinct:
        unknown = db.conn.execute(
            "SELECT taxid FROM taxonomy_nodes WHERE taxonomy_snapshot_id=? "
            "AND is_extinct IS NULL ORDER BY taxid LIMIT 1",
            (snapshot_id,),
        ).fetchone()
        if unknown:
            raise ValidationError(
                "coverage profile requests exclude_extinct, but this taxonomy snapshot "
                "has no complete extinct annotation (for example NCBI taxdump); set it "
                "to false and use explicit excluded subtrees/name patterns, or import an "
                "NCBI Datasets taxonomy report"
            )
        extinct_cte = (
            ", extinct(taxid) AS (SELECT taxid FROM taxonomy_nodes "
            "WHERE taxonomy_snapshot_id=? AND is_extinct=1 "
            "UNION SELECT n.taxid FROM extinct e CROSS JOIN taxonomy_nodes n "
            "WHERE n.parent_taxid=e.taxid AND n.taxonomy_snapshot_id=? AND n.taxid<>e.taxid)"
        )
        params.extend([snapshot_id, snapshot_id])
        extinct_clause = "AND NOT EXISTS (SELECT 1 FROM extinct x WHERE x.taxid=n.taxid)"
    params.append(snapshot_id)
    params.extend(ranks)
    # CROSS JOIN fixes the join order so every recursive step and the final
    # SELECT drive from the CTE side; otherwise the planner may scan the
    # whole snapshot per step (quadratic on multi-million-node taxonomies).
    sql = (
        f"WITH RECURSIVE scope(taxid) AS (SELECT column1 FROM (VALUES {root_values}) "
        "UNION SELECT n.taxid FROM scope s CROSS JOIN taxonomy_nodes n "
        "WHERE n.parent_taxid=s.taxid AND n.taxonomy_snapshot_id=? AND n.taxid<>s.taxid) "
        f"{excluded_cte}{extinct_cte} "
        "SELECT DISTINCT n.taxid, n.rank, n.scientific_name, n.is_extinct "
        "FROM scope s CROSS JOIN taxonomy_nodes n "
        f"WHERE n.taxonomy_snapshot_id=? AND s.taxid=n.taxid {excluded_clause} {extinct_clause} "
        f"AND n.rank IN ({rank_values})"
    )
    return [dict(row) for row in db.conn.execute(sql, params).fetchall()]


def _tsv_text(columns: list[str], rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if row.get(column) is None else row[column] for column in columns])
    return buffer.getvalue()


def _validate_reference_provenance(
        path: Path,
        *,
        reference_set_id: str,
        taxonomy_snapshot_id: str,
        taxonomy_version: str,
        taxonomy_source_sha256: str,
        profile_sha256: str,
        tsv_sha256: str,
        tsv_size_bytes: int,
) -> dict[str, Any]:
    if not path.is_file():
        raise ConflictError(f"reference-set provenance is missing: {path}")
    try:
        provenance = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConflictError(f"reference-set provenance is invalid: {path}") from exc
    expected = {
        "schema": REFERENCE_SET_SCHEMA,
        "reference_set_id": reference_set_id,
        "taxonomy_snapshot_id": taxonomy_snapshot_id,
        "taxonomy_version": taxonomy_version,
        "taxonomy_source_sha256": taxonomy_source_sha256,
        "profile_sha256": profile_sha256,
        "tsv_sha256": tsv_sha256,
        "tsv_size_bytes": int(tsv_size_bytes),
    }
    mismatches = [key for key, value in expected.items() if provenance.get(key) != value]
    if mismatches:
        raise ConflictError(
            f"reference-set provenance does not match frozen identity ({', '.join(mismatches)}): {path}"
        )
    return provenance


def _compile_reference_set_impl(
        db: Database,
        project: Project,
        profile_name: str,
        taxonomy_version: str,
) -> dict[str, Any]:
    """Compile a versioned coverage profile into one immutable TSV denominator."""
    profile_name = _safe_token(profile_name, "profile name")
    taxonomy_version = _safe_token(taxonomy_version, "taxonomy version")
    profile = load_profile(project.profiles_dir, profile_name, expected_kind=PROFILE_KIND)
    parsed = _validate_coverage_profile(profile_name, profile)
    profile_document, profile_sha = canonical_document(profile)
    snapshot_row = db.conn.execute(
        "SELECT * FROM taxonomy_snapshots WHERE source='NCBI' AND taxonomy_version=? AND status='READY'",
        (taxonomy_version,),
    ).fetchone()
    if not snapshot_row:
        raise ValidationError(f"ready NCBI taxonomy snapshot {taxonomy_version!r} not found")
    snapshot = dict(snapshot_row)
    reference_set_id = f"{profile_name}@{taxonomy_version}"
    target = project.taxonomy_reference_sets_dir / f"{reference_set_id}.tsv"
    sidecar = project.taxonomy_reference_sets_dir / f"{reference_set_id}.provenance.json"
    existing = db.conn.execute(
        "SELECT * FROM taxonomy_reference_sets WHERE reference_set_id=?", (reference_set_id,)
    ).fetchone()
    if existing:
        existing = dict(existing)
        if (
                existing["profile_sha256"] != profile_sha
                or existing["taxonomy_snapshot_id"] != snapshot["taxonomy_snapshot_id"]
                or existing["tsv_sha256"] != (sha256_file(target) if target.is_file() else None)
        ):
            raise ConflictError(
                f"reference set {reference_set_id} already exists with different profile, taxonomy or bytes"
            )
        _validate_reference_provenance(
            sidecar,
            reference_set_id=reference_set_id,
            taxonomy_snapshot_id=snapshot["taxonomy_snapshot_id"],
            taxonomy_version=taxonomy_version,
            taxonomy_source_sha256=snapshot["source_sha256"],
            profile_sha256=profile_sha,
            tsv_sha256=existing["tsv_sha256"],
            tsv_size_bytes=existing["tsv_size_bytes"],
        )
        return {**existing, "path": str(target), "provenance_path": str(sidecar), "reused": True}

    candidates = _descendant_targets(
        db, snapshot["taxonomy_snapshot_id"], parsed["root_taxids"],
        parsed["exclude_subtrees"], parsed["ranks"], parsed["exclude_extinct"],
    )
    rows: list[dict[str, Any]] = []
    for row in candidates:
        if any(pattern.search(str(row["scientific_name"])) for pattern in parsed["compiled_name_patterns"]):
            continue
        rows.append({
            "rank": str(row["rank"]),
            "taxid": int(row["taxid"]),
            "scientific_name": str(row["scientific_name"]),
        })
    rows.sort(key=lambda row: (TARGET_RANK_ORDER[row["rank"]], row["taxid"]))
    counts = {rank: sum(1 for row in rows if row["rank"] == rank) for rank in parsed["ranks"]}
    empty = [rank for rank, count in counts.items() if count == 0]
    if empty:
        raise ValidationError(f"compiled reference denominator is empty for rank(s): {', '.join(empty)}")
    text = _tsv_text(["rank", "taxid", "scientific_name"], rows)
    desired_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if target.exists():
        if not target.is_file() or sha256_file(target) != desired_sha:
            raise ConflictError(f"reference set target already exists with different bytes: {target}")
    else:
        atomic_write_text(target, text)
    size = target.stat().st_size
    compiled_at = now_iso()
    run_id = new_run_id()
    provenance = {
        "schema": REFERENCE_SET_SCHEMA,
        "reference_set_id": reference_set_id,
        "source": "NCBI",
        "taxonomy_snapshot_id": snapshot["taxonomy_snapshot_id"],
        "taxonomy_version": taxonomy_version,
        "taxonomy_source_sha256": snapshot["source_sha256"],
        "taxonomy_source_size_bytes": snapshot["source_size_bytes"],
        "profile_name": profile_name,
        "profile_version": int(profile["version"]),
        "profile_sha256": profile_sha,
        "profile_document": profile,
        "compiler": "operon.taxonomy",
        "compiler_version": __version__,
        "root_taxids": parsed["root_taxids"],
        "target_ranks": parsed["ranks"],
        "row_counts": counts,
        "tsv_sha256": desired_sha,
        "tsv_size_bytes": size,
        "compiled_at": compiled_at,
        "workflow_run_id": run_id,
    }
    atomic_write_text(sidecar, json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    with db.transaction():
        db.conn.execute(
            "INSERT INTO taxonomy_reference_sets(reference_set_id, name, taxonomy_snapshot_id, "
            "taxonomy_version, relative_path, tsv_sha256, tsv_size_bytes, profile_name, "
            "profile_version, profile_sha256, profile_document, family_count, genus_count, "
            "compiled_at, workflow_run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                reference_set_id, profile_name, snapshot["taxonomy_snapshot_id"], taxonomy_version,
                project_rel(project, target), desired_sha, size, profile_name, int(profile["version"]),
                profile_sha, profile_document, counts.get("family", 0), counts.get("genus", 0),
                compiled_at, run_id,
            ),
        )
        db.conn.execute(
            "INSERT INTO changes(object_type, object_id, field, old_value, new_value, reason, evidence, actor, changed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "taxonomy_reference_set", reference_set_id, "compiled_snapshot", None,
                json.dumps({
                    "path": project_rel(project, target), "tsv_sha256": desired_sha,
                    "taxonomy_version": taxonomy_version,
                    "taxonomy_source_sha256": snapshot["source_sha256"],
                    "profile_sha256": profile_sha, **counts,
                }, sort_keys=True),
                "explicit taxonomy reference-set compilation", project_rel(project, sidecar),
                os.environ.get("USER"), compiled_at,
            ),
        )
    log_run(db, project, {
        "run_id": run_id,
        "entity_type": "taxonomy_reference_set",
        "entity_id": reference_set_id,
        "step": "taxonomy_compile",
        "status": "completed",
        "started_at": compiled_at,
        "finished_at": now_iso(),
        "tool": "operon.taxonomy",
        "tool_version": __version__,
        "parameter_set": profile_sha,
        "input_sha256": snapshot["source_sha256"],
        "output_sha256": desired_sha,
        "command": f"taxonomy compile {profile_name} {taxonomy_version}",
    })
    return {
        "reference_set_id": reference_set_id,
        "taxonomy_snapshot_id": snapshot["taxonomy_snapshot_id"],
        "taxonomy_version": taxonomy_version,
        "profile_name": profile_name,
        "profile_version": int(profile["version"]),
        "profile_sha256": profile_sha,
        "tsv_sha256": desired_sha,
        "tsv_size_bytes": size,
        "family_count": counts.get("family", 0),
        "genus_count": counts.get("genus", 0),
        "path": str(target),
        "provenance_path": str(sidecar),
        "reused": False,
    }


def compile_reference_set(
        db: Database,
        project: Project,
        profile_name: str,
        taxonomy_version: str,
) -> dict[str, Any]:
    """Compile a denominator and record failed attempts as workflow provenance."""
    started_at = now_iso()
    try:
        return _compile_reference_set_impl(
            db, project, profile_name, taxonomy_version
        )
    except Exception as exc:
        log_run(db, project, {
            "run_id": new_run_id(),
            "entity_type": "taxonomy_reference_set",
            "entity_id": f"{profile_name}@{taxonomy_version}",
            "step": "taxonomy_compile",
            "status": "failed",
            "started_at": started_at,
            "finished_at": now_iso(),
            "tool": "operon.taxonomy",
            "tool_version": __version__,
            "parameter_set": str(profile_name),
            "command": f"taxonomy compile {profile_name} {taxonomy_version}",
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise


def list_taxonomy_snapshots(db: Database) -> list[dict[str, Any]]:
    return [dict(row) for row in db.conn.execute(
        "SELECT taxonomy_snapshot_id, source, taxonomy_version, source_file_id, source_sha256, "
        "source_size_bytes, node_count, status, imported_at FROM taxonomy_snapshots "
        "ORDER BY imported_at, taxonomy_snapshot_id"
    ).fetchall()]


def list_reference_sets(db: Database) -> list[dict[str, Any]]:
    return [dict(row) for row in db.conn.execute(
        "SELECT reference_set_id, taxonomy_version, profile_name, profile_version, family_count, "
        "genus_count, tsv_sha256, relative_path, compiled_at FROM taxonomy_reference_sets "
        "ORDER BY compiled_at, reference_set_id"
    ).fetchall()]
