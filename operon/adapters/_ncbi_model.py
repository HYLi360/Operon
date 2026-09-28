"""Constants, data models and pure helpers for the NCBI Datasets adapter.

Nothing here touches the database or the project: every function is a
side-effect-free helper over plain values.  The database-coupled
include-reuse matching lives in `_ncbi_plan`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from operon.errors import ValidationError

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


def _read_report_file(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return _read_report_handle(handle, str(path))
    except UnicodeDecodeError as exc:  # pragma: no cover
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
        return []  # pragma: no cover
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
        "bioproject_accession": _pick(assembly_info, "bioprojectAccession", "bioproject_accession") or _pick(report,
                                                                                                             "bioprojectAccession"),
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
    match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*([NS])?\s+([+-]?\d+(?:\.\d+)?)\s*([EW])?", text, re.IGNORECASE)
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
