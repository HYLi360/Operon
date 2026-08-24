"""Metadata schema definitions and validation.

The metadata model uses normalized tables, stable internal IDs, external
accessions stored as mappings, and every field having a declared
type/required/allowed contract.  Schemas are YAML files so projects can
extend them without changing code.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from operon.errors import ValidationError
from operon.utils import SHA256_RE

# Entity table -> prefix and id column.  These prefixes are deliberately
# independent from any external accession namespace.
ENTITY_TABLES: dict[str, str] = {
    "organism": "organisms",
    "sample": "samples",
    "run": "runs",
    "assembly": "assemblies",
    "annotation": "annotations",
}
ENTITY_ID_COLUMNS: dict[str, str] = {
    "organism": "organism_id",
    "sample": "sample_id",
    "run": "run_id",
    "assembly": "assembly_id",
    "annotation": "annotation_id",
}
ENTITY_PREFIXES: dict[str, str] = {
    "organism": "ORG",
    "sample": "SMP",
    "run": "RUN",
    "assembly": "ASM",
    "annotation": "ANN",
    "file": "FIL",
}
FILE_ENTITY_TYPES = [*ENTITY_TABLES.keys(), "taxonomy_snapshot"]
MISSING_VALUES = {"", "na", "n/a", "null", "none"}


def default_schemas() -> dict[str, Any]:
    """Return the built-in metadata schema, serialized as project YAML on init."""
    fields = {
        "organisms": {
            "file": "organisms.tsv",
            "primary_key": "organism_id",
            "description": "Taxonomic organisms referenced by samples.",
            "fields": {
                "organism_id": {"type": "id", "pattern": r"^ORG_\d{6}$", "required": True, "description": "Internal stable organism ID"},
                "scientific_name": {"type": "string", "required": True, "description": "Scientific name"},
                "taxon_id": {"type": "integer", "description": "NCBI/GTDB taxonomy ID"},
                "taxonomic_rank": {"type": "string", "description": "Taxonomic rank"},
                "taxonomy_source": {"type": "string", "allowed": ["NCBI", "GTDB", "other"], "description": "Taxonomy database"},
                "taxonomy_version": {"type": "string", "description": "Version of taxonomy database"},
            },
        },
        "samples": {
            "file": "samples.tsv",
            "primary_key": "sample_id",
            "description": "Biological samples; the link between organism and experiment/assembly.",
            "fields": {
                "sample_id": {"type": "id", "pattern": r"^SMP_\d{6}$", "required": True, "description": "Internal stable sample ID"},
                "organism_id": {"type": "id", "pattern": r"^ORG_\d{6}$", "required": True, "description": "Internal organism ID"},
                "biosample_accession": {"type": "string", "description": "NCBI BioSample accession"},
                "strain": {"type": "string", "description": "Strain name (original value)"},
                "isolate": {"type": "string", "description": "Isolate identifier"},
                "cultivar": {"type": "string", "description": "Cultivar"},
                "sex": {"type": "string", "allowed": ["female", "male", "hermaphrodite", "unknown", "not collected", "not applicable"], "description": "Sex, using controlled vocabulary"},
                "tissue": {"type": "string", "description": "Original tissue description"},
                "tissue_normalized": {"type": "string", "description": "Normalized tissue term"},
                "tissue_ontology_id": {"type": "string", "pattern": r"^(PO|UBERON|ENVO):\d+$", "description": "Ontology term ID"},
                "collection_date": {"type": "date", "description": "ISO 8601 collection date"},
                "country": {"type": "string", "description": "Original country text"},
                "country_iso": {"type": "string", "pattern": r"^[A-Z]{2}$", "description": "ISO 3166-1 alpha-2 country code"},
                "latitude": {"type": "float", "min": -90, "max": 90, "description": "Decimal latitude (WGS84)"},
                "longitude": {"type": "float", "min": -180, "max": 180, "description": "Decimal longitude (WGS84)"},
                "host": {"type": "string", "description": "Host organism"},
                "environment_biome": {"type": "string", "description": "Environment biome (ENVO preferred)"},
                "source_record": {"type": "string", "description": "Source database or record URL"},
            },
        },
        "runs": {
            "file": "runs.tsv",
            "primary_key": "run_id",
            "description": "Sequencing runs producing raw reads.",
            "fields": {
                "run_id": {"type": "id", "pattern": r"^RUN_\d{6}$", "required": True, "description": "Internal stable run ID"},
                "sample_id": {"type": "id", "pattern": r"^SMP_\d{6}$", "required": True, "description": "Internal sample ID"},
                "run_accession": {"type": "string", "description": "SRA/ENA run accession"},
                "experiment_accession": {"type": "string", "description": "Sequencing experiment accession"},
                "library_strategy": {"type": "string", "allowed": ["WGS", "WGA", "RNA-Seq", "Amplicon", "Hi-C", "ATAC-seq", "other"], "description": "INSDC library strategy"},
                "library_source": {"type": "string", "allowed": ["GENOMIC", "TRANSCRIPTOMIC", "METAGENOMIC", "OTHER"], "description": "INSDC library source"},
                "library_layout": {"type": "string", "allowed": ["PAIRED", "SINGLE", "unknown"], "description": "Library layout"},
                "platform": {"type": "string", "allowed": ["ILLUMINA", "PACBIO_SMRT", "OXFORD_NANOPORE", "BGISEQ", "ION_TORRENT", "other"], "description": "Sequencing platform"},
                "instrument_model": {"type": "string", "description": "Instrument model"},
                "read_length": {"type": "integer", "min": 0, "description": "Nominal read length"},
                "download_url": {"type": "string", "description": "Original download URL"},
            },
        },
        "assemblies": {
            "file": "assemblies.tsv",
            "primary_key": "assembly_id",
            "description": "Genome assemblies; one sample may have several assembly versions.",
            "fields": {
                "assembly_id": {"type": "id", "pattern": r"^ASM_\d{6}$", "required": True, "description": "Internal stable assembly ID"},
                "sample_id": {"type": "id", "pattern": r"^SMP_\d{6}$", "required": True, "description": "Internal sample ID"},
                "assembly_accession": {"type": "string", "description": "NCBI/ENA assembly accession"},
                "assembly_name": {"type": "string", "description": "Source assembly name"},
                "assembly_version": {"type": "integer", "min": 1, "description": "Assembly version number"},
                "assembly_level": {"type": "string", "allowed": ["complete_genome", "chromosome", "scaffold", "contig"], "description": "Standardized assembly level"},
                "assembly_method": {"type": "string", "description": "Assembly software and parameters"},
                "submitter": {"type": "string", "description": "Submitter or source institution"},
                "release_date": {"type": "date", "description": "Release date of the source assembly"},
                "reference_status": {"type": "string", "allowed": ["reference", "representative", "alternate", "other"], "description": "Reference status"},
                "bioproject_accession": {"type": "string", "description": "Source BioProject/project accession (not unique per assembly)"},
                "source_database": {"type": "string", "allowed": ["RefSeq", "GenBank", "other"], "description": "Source assembly database"},
                "assembly_status": {"type": "string", "description": "Source database assembly status"},
                "assembly_type": {"type": "string", "description": "Source database assembly type"},
                "fasta_file_id": {"type": "id", "pattern": r"^FIL_\d{6}$", "description": "Registered assembly FASTA file"},
            },
        },
        "annotations": {
            "file": "annotations.tsv",
            "primary_key": "annotation_id",
            "description": "Annotation releases; an assembly may have several annotation versions.",
            "fields": {
                "annotation_id": {"type": "id", "pattern": r"^ANN_\d{6}$", "required": True, "description": "Internal stable annotation ID"},
                "assembly_id": {"type": "id", "pattern": r"^ASM_\d{6}$", "required": True, "description": "Internal assembly ID"},
                "annotation_source": {"type": "string", "description": "Annotation source or pipeline"},
                "annotation_version": {"type": "integer", "min": 1, "description": "Annotation version"},
                "annotation_date": {"type": "date", "description": "Annotation release date"},
                "gff_file_id": {"type": "id", "pattern": r"^FIL_\d{6}$", "description": "Registered GFF3 file"},
                "cds_file_id": {"type": "id", "pattern": r"^FIL_\d{6}$", "description": "Registered CDS FASTA file"},
                "protein_file_id": {"type": "id", "pattern": r"^FIL_\d{6}$", "description": "Registered protein FASTA file"},
            },
        },
        "accessions": {
            "file": "accessions.tsv",
            "primary_key": None,
            "description": "External accessions mapped to internal stable IDs (never used as primary keys).",
            "fields": {
                "internal_type": {"type": "string", "required": True, "allowed": list(ENTITY_TABLES.keys()), "description": "Internal entity type"},
                "internal_id": {"type": "id", "pattern": r"^(ORG|SMP|RUN|ASM|ANN)_\d{6}$", "required": True, "description": "Internal stable ID"},
                "namespace": {"type": "string", "required": True, "description": "Accession namespace (NCBI_Assembly, SRA, ...)"},
                "accession": {"type": "string", "required": True, "description": "External accession"},
                "version": {"type": "string", "description": "External record version"},
                "is_primary": {"type": "boolean", "description": "Whether this is the primary accession for the entity"},
            },
            "unique": [["namespace", "accession"]],
        },
        "files": {
            "file": "files.tsv",
            "primary_key": "file_id",
            "description": "File manifest: path is only the current location; identity is file_id + sha256 + size.",
            "fields": {
                "file_id": {"type": "id", "pattern": r"^FIL_\d{6}$", "required": True, "description": "Internal stable file ID"},
                "entity_type": {"type": "string", "required": True, "allowed": FILE_ENTITY_TYPES, "description": "Entity type this file belongs to"},
                "entity_id": {"type": "id", "pattern": r"^(ORG|SMP|RUN|ASM|ANN|TAX)_\d{6}$", "required": True, "description": "Internal entity ID"},
                "file_role": {"type": "string", "required": True, "allowed": [
                    "genome_fasta", "cds_fasta", "protein_fasta", "annotation_gff3",
                    "reads_r1", "reads_r2", "reads_single", "assembly_report",
                    "taxonomy_package", "other",
                ], "description": "Biological role of the file"},
                "format": {"type": "string", "required": True, "allowed": ["fasta", "fastq", "gff3", "bam", "cram", "tsv", "txt", "html", "json", "directory", "other"], "description": "File or directory artifact format"},
                "compression": {"type": "string", "required": True, "allowed": ["none", "gzip", "bgzip"], "description": "Compression type"},
                "relative_path": {"type": "string", "required": True, "description": "Current path relative to project root"},
                "source_url": {"type": "string", "description": "Original source URL or path"},
                "size_bytes": {"type": "integer", "required": True, "min": 0, "description": "File size in bytes"},
                "sha256": {"type": "string", "required": True, "pattern": r"^[a-f0-9]{64}$", "description": "SHA-256 of the stored bytes"},
                "downloaded_at": {"type": "datetime", "description": "When the file was archived"},
                "status": {"type": "string", "required": True, "allowed": ["DISCOVERED", "DOWNLOADED", "CHECKSUM_VERIFIED", "STANDARDIZED", "REMOTE_ONLY", "MISSING", "CHECKSUM_FAILED", "CONFLICT"], "description": "File-level status"},
            },
        },
    }
    return {"schema_version": "1.3", "tables": fields}


@dataclass
class SchemaError:
    table: str
    row: int
    field: str
    value: Any
    message: str

    def __str__(self) -> str:
        return f"{self.table}: row {self.row}, field {self.field}={self.value!r}: {self.message}"


class Schema:
    """A loaded metadata schema."""

    def __init__(self, document: dict[str, Any]):
        if not isinstance(document, dict) or "tables" not in document:
            raise ValidationError("schema document must contain a 'tables' mapping")
        self.version = document.get("schema_version", "unknown")
        self.tables: dict[str, dict[str, Any]] = document["tables"]

    @classmethod
    def from_file(cls, path: str | Path) -> "Schema":
        path = Path(path)
        if not path.exists():
            raise ValidationError(f"schema file not found: {path}")
        with open(path, encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}
        return cls(document)

    def table_names(self) -> list[str]:
        return list(self.tables.keys())

    def columns(self, table: str) -> list[str]:
        try:
            return list(self.tables[table]["fields"].keys())
        except KeyError as exc:
            raise ValidationError(f"unknown schema table {table!r}") from exc

    def primary_key(self, table: str) -> str | None:
        return self.tables[table].get("primary_key")

    def unique_combinations(self, table: str) -> list[list[str]]:
        return self.tables[table].get("unique", [])

    def validate_and_normalize(self, table: str, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[SchemaError]]:
        if table not in self.tables:
            raise ValidationError(f"schema has no table {table!r}")
        spec = self.tables[table]
        columns = self.columns(table)
        errors: list[SchemaError] = []
        normalized: list[dict[str, Any]] = []
        seen_primary: dict[Any, int] = {}
        seen_unique: dict[tuple[str, ...], dict[tuple[Any, ...], int]] = {
            key: {} for key in [tuple(c) for c in self.unique_combinations(table)]
        }

        for row_no, raw_row in enumerate(rows, start=1):
            unknown = set(raw_row.keys()) - set(columns)
            if unknown:
                for col in sorted(unknown):
                    errors.append(SchemaError(table, row_no, col, raw_row.get(col), "unknown field; update schema instead of silently accepting it"))
            row: dict[str, Any] = {}
            for field, field_spec in spec["fields"].items():
                raw_value = raw_row.get(field, "")
                value, err = self._normalize_field(field, field_spec, raw_value)
                if err:
                    errors.append(SchemaError(table, row_no, field, raw_value, err))
                else:
                    row[field] = value
            # Duplicate detection is only meaningful for rows that had no
            # field-level error.
            if not errors:
                pk = spec.get("primary_key")
                if pk:
                    value = row.get(pk)
                    if value in seen_primary:
                        errors.append(SchemaError(table, row_no, pk, value, f"duplicate primary key (first seen at row {seen_primary[value]})"))
                    seen_primary[value] = row_no
                for combo in self.unique_combinations(table):
                    values = tuple(row.get(c) for c in combo)
                    if any(v is None for v in values):
                        continue
                    if values in seen_unique[tuple(combo)]:
                        errors.append(SchemaError(table, row_no, ", ".join(combo), values, f"duplicate unique combination (first seen at row {seen_unique[tuple(combo)][values]})"))
                    seen_unique[tuple(combo)][values] = row_no
            normalized.append(row)
        if errors:
            raise ValidationError("\n".join(str(e) for e in errors))
        return normalized, []

    def _normalize_field(self, field: str, spec: dict[str, Any], raw_value: Any) -> tuple[Any, str | None]:
        if raw_value is None:
            raw_value = ""
        if isinstance(raw_value, str):
            value: Any = raw_value.strip()
        else:
            value = raw_value
        if isinstance(value, str) and value.lower() in MISSING_VALUES:
            # Some controlled vocabularies legitimately use a token such as
            # compression="none". Treat it as missing only when the field does
            # not explicitly declare that token as an allowed value.
            allowed_lower = {str(item).lower() for item in spec.get("allowed", [])}
            if value.lower() not in allowed_lower:
                value = ""
        required = bool(spec.get("required", False))
        if value == "" or value is None:
            if required:
                return None, "required field is missing"
            return None, None

        type_name = spec.get("type", "string")
        try:
            if type_name == "id":
                if not isinstance(value, str):
                    return None, "ID must be a string"
                value = value.strip().upper()
            elif type_name == "string":
                if not isinstance(value, str):
                    value = str(value)
                value = value.strip()
            elif type_name == "integer":
                value = int(str(value))
            elif type_name == "float":
                value = float(str(value))
            elif type_name == "boolean":
                if isinstance(value, str) and value.lower() in {"true", "yes", "1"}:
                    value = 1
                elif isinstance(value, str) and value.lower() in {"false", "no", "0"}:
                    value = 0
                else:
                    value = int(bool(value))
            elif type_name == "date":
                value = _normalize_date(str(value), with_time=False)
            elif type_name == "datetime":
                value = _normalize_date(str(value), with_time=True)
            else:
                return None, f"unknown schema type {type_name!r}"
        except (TypeError, ValueError) as exc:
            return None, f"expected {type_name}: {exc}"

        pattern = spec.get("pattern")
        if pattern and type_name in {"id", "string"}:
            if re.fullmatch(str(pattern), str(value)) is None:
                return None, f"does not match pattern {pattern!r}"

        if "min" in spec and type_name in {"integer", "float"} and value < spec["min"]:
            return None, f"must be >= {spec['min']}"
        if "max" in spec and type_name in {"integer", "float"} and value > spec["max"]:
            return None, f"must be <= {spec['max']}"
        if type_name == "integer":
            value = int(value)
        elif type_name == "float":
            value = float(value)

        allowed = spec.get("allowed")
        if allowed is not None:
            if value not in allowed and isinstance(value, str):
                # Be tolerant of case-only differences but always store the
                # controlled vocabulary's canonical spelling.
                by_upper = {str(item).upper(): item for item in allowed}
                canonical = by_upper.get(value.upper())
                if canonical is not None:
                    value = canonical
            if value not in allowed:
                return None, f"not in allowed values {allowed!r}"
        return value, None


def _normalize_date(value: str, with_time: bool) -> str:
    if with_time:
        parsed = datetime.fromisoformat(value)
        return parsed.isoformat(timespec="seconds")
    parsed = date.fromisoformat(value)
    return parsed.isoformat()


def read_tsv(path: str | Path, required_header: list[str] | None = None) -> list[dict[str, Any]]:
    """Read a TSV file as a list of dictionaries.

    Blank lines and comment lines are ignored.  The first non-comment line is
    the header.  Extra tabs are tolerated only if all values are empty.
    """
    path = Path(path)
    rows: list[dict[str, Any]] = []
    header: list[str] | None = None
    with open(path, encoding="utf-8-sig", newline="") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\n\r")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split("\t")
            if header is None:
                header = [f.strip() for f in fields]
                if required_header is not None:
                    missing = [c for c in required_header if c not in header]
                    if missing:
                        raise ValidationError(f"{path}: missing columns {missing}")
                continue
            if len(fields) != len(header):
                if len(fields) == len(header) - 1 and header[-1] == "":
                    fields.append("")
                else:
                    raise ValidationError(
                        f"{path}: line {line_no} has {len(fields)} fields, expected {len(header)}"
                    )
            rows.append(dict(zip(header, fields, strict=True)))
    if header is None:
        raise ValidationError(f"{path}: no header row found")
    return rows


def write_tsv(path: str | Path, columns: list[str], rows: Iterable[dict[str, Any] | list[Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            if isinstance(row, dict):
                writer.writerow(["" if row.get(c) is None else row.get(c) for c in columns])
            else:
                writer.writerow(["" if v is None else v for v in row])
