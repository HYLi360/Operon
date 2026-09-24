"""Metadata schema definitions and validation.

The metadata model uses normalized tables, stable internal IDs, external
accessions stored as mappings, and every field having a declared
type/required/allowed contract.  Schemas are YAML files so projects can
extend them without changing code.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from operon.errors import ValidationError
from operon.utils import escape_formula_text

if TYPE_CHECKING:
    from operon.config import Project
    from operon.database import Database

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
METADATA_SCHEMA_VERSION = "1.4"
NCBI_SOURCE_FILE_ROLES = [
    "genome_fasta_genbank",
    "genome_fasta_refseq",
    "assembly_report_genbank",
    "assembly_report_refseq",
]
MISSING_VALUES = {"", "na", "n/a", "null", "none"}


def default_schemas() -> dict[str, Any]:
    """Return the built-in metadata schema, serialized as project YAML on init."""
    fields = {
        "organisms": {
            "file": "organisms.tsv",
            "primary_key": "organism_id",
            "description": "Taxonomic organisms referenced by samples.",
            "fields": {
                "organism_id": {"type": "id", "pattern": r"^ORG_\d{6}$", "required": True,
                                "description": "Internal stable organism ID"},
                "scientific_name": {"type": "string", "required": True, "description": "Scientific name"},
                "taxon_id": {"type": "integer", "description": "NCBI/GTDB taxonomy ID"},
                "taxonomic_rank": {"type": "string", "description": "Taxonomic rank"},
                "taxonomy_source": {"type": "string", "allowed": ["NCBI", "GTDB", "other"],
                                    "description": "Taxonomy database"},
                "taxonomy_version": {"type": "string", "description": "Version of taxonomy database"},
            },
        },
        "samples": {
            "file": "samples.tsv",
            "primary_key": "sample_id",
            "description": "Biological samples; the link between organism and experiment/assembly.",
            "fields": {
                "sample_id": {"type": "id", "pattern": r"^SMP_\d{6}$", "required": True,
                              "description": "Internal stable sample ID"},
                "organism_id": {"type": "id", "pattern": r"^ORG_\d{6}$", "required": True,
                                "description": "Internal organism ID"},
                "biosample_accession": {"type": "string", "description": "NCBI BioSample accession"},
                "strain": {"type": "string", "description": "Strain name (original value)"},
                "isolate": {"type": "string", "description": "Isolate identifier"},
                "cultivar": {"type": "string", "description": "Cultivar"},
                "sex": {"type": "string",
                        "allowed": ["female", "male", "hermaphrodite", "unknown", "not collected", "not applicable"],
                        "description": "Sex, using controlled vocabulary"},
                "tissue": {"type": "string", "description": "Original tissue description"},
                "tissue_normalized": {"type": "string", "description": "Normalized tissue term"},
                "tissue_ontology_id": {"type": "string", "pattern": r"^(PO|UBERON|ENVO):\d+$",
                                       "description": "Ontology term ID"},
                "collection_date": {"type": "date", "description": "ISO 8601 collection date"},
                "country": {"type": "string", "description": "Original country text"},
                "country_iso": {"type": "string", "pattern": r"^[A-Z]{2}$",
                                "description": "ISO 3166-1 alpha-2 country code"},
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
                "run_id": {"type": "id", "pattern": r"^RUN_\d{6}$", "required": True,
                           "description": "Internal stable run ID"},
                "sample_id": {"type": "id", "pattern": r"^SMP_\d{6}$", "required": True,
                              "description": "Internal sample ID"},
                "run_accession": {"type": "string", "description": "SRA/ENA run accession"},
                "experiment_accession": {"type": "string", "description": "Sequencing experiment accession"},
                "library_strategy": {"type": "string",
                                     "allowed": ["WGS", "WGA", "RNA-Seq", "Amplicon", "Hi-C", "ATAC-seq", "other"],
                                     "description": "INSDC library strategy"},
                "library_source": {"type": "string", "allowed": ["GENOMIC", "TRANSCRIPTOMIC", "METAGENOMIC", "OTHER"],
                                   "description": "INSDC library source"},
                "library_layout": {"type": "string", "allowed": ["PAIRED", "SINGLE", "unknown"],
                                   "description": "Library layout"},
                "platform": {"type": "string",
                             "allowed": ["ILLUMINA", "PACBIO_SMRT", "OXFORD_NANOPORE", "BGISEQ", "ION_TORRENT",
                                         "other"], "description": "Sequencing platform"},
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
                "assembly_id": {"type": "id", "pattern": r"^ASM_\d{6}$", "required": True,
                                "description": "Internal stable assembly ID"},
                "sample_id": {"type": "id", "pattern": r"^SMP_\d{6}$", "required": True,
                              "description": "Internal sample ID"},
                "assembly_accession": {"type": "string", "description": "NCBI/ENA assembly accession"},
                "assembly_name": {"type": "string", "description": "Source assembly name"},
                "assembly_version": {"type": "integer", "min": 1, "description": "Assembly version number"},
                "assembly_level": {"type": "string", "allowed": ["complete_genome", "chromosome", "scaffold", "contig"],
                                   "description": "Standardized assembly level"},
                "assembly_method": {"type": "string", "description": "Assembly software and parameters"},
                "submitter": {"type": "string", "description": "Submitter or source institution"},
                "release_date": {"type": "date", "description": "Release date of the source assembly"},
                "reference_status": {"type": "string", "allowed": ["reference", "representative", "alternate", "other"],
                                     "description": "Reference status"},
                "bioproject_accession": {"type": "string",
                                         "description": "Source BioProject/project accession (not unique per assembly)"},
                "source_database": {"type": "string", "allowed": ["RefSeq", "GenBank", "other"],
                                    "description": "Source assembly database"},
                "assembly_status": {"type": "string", "description": "Source database assembly status"},
                "assembly_type": {"type": "string", "description": "Source database assembly type"},
                "fasta_file_id": {"type": "id", "pattern": r"^FIL_\d{6}$",
                                  "description": "Registered assembly FASTA file"},
            },
        },
        "annotations": {
            "file": "annotations.tsv",
            "primary_key": "annotation_id",
            "description": "Annotation releases; an assembly may have several annotation versions.",
            "fields": {
                "annotation_id": {"type": "id", "pattern": r"^ANN_\d{6}$", "required": True,
                                  "description": "Internal stable annotation ID"},
                "assembly_id": {"type": "id", "pattern": r"^ASM_\d{6}$", "required": True,
                                "description": "Internal assembly ID"},
                "annotation_source": {"type": "string", "description": "Annotation source or pipeline"},
                "annotation_version": {"type": "integer", "min": 1, "description": "Annotation version"},
                "annotation_date": {"type": "date", "description": "Annotation release date"},
                "gff_file_id": {"type": "id", "pattern": r"^FIL_\d{6}$", "description": "Registered GFF3 file"},
                "cds_file_id": {"type": "id", "pattern": r"^FIL_\d{6}$", "description": "Registered CDS FASTA file"},
                "protein_file_id": {"type": "id", "pattern": r"^FIL_\d{6}$",
                                    "description": "Registered protein FASTA file"},
            },
        },
        "accessions": {
            "file": "accessions.tsv",
            "primary_key": None,
            "description": "External accessions mapped to internal stable IDs (never used as primary keys).",
            "fields": {
                "internal_type": {"type": "string", "required": True, "allowed": list(ENTITY_TABLES.keys()),
                                  "description": "Internal entity type"},
                "internal_id": {"type": "id", "pattern": r"^(ORG|SMP|RUN|ASM|ANN)_\d{6}$", "required": True,
                                "description": "Internal stable ID"},
                "namespace": {"type": "string", "required": True,
                              "description": "Accession namespace (NCBI_Assembly, SRA, ...)"},
                "accession": {"type": "string", "required": True, "description": "External accession"},
                "version": {"type": "string", "description": "External record version"},
                "is_primary": {"type": "boolean",
                               "description": "Whether this is the primary accession for the entity"},
            },
            "unique": [["namespace", "accession"]],
        },
        "files": {
            "file": "files.tsv",
            "primary_key": "file_id",
            "description": "File manifest: path is only the current location; identity is file_id + sha256 + size.",
            "fields": {
                "file_id": {"type": "id", "pattern": r"^FIL_\d{6}$", "required": True,
                            "description": "Internal stable file ID"},
                "entity_type": {"type": "string", "required": True, "allowed": FILE_ENTITY_TYPES,
                                "description": "Entity type this file belongs to"},
                "entity_id": {"type": "id", "pattern": r"^(ORG|SMP|RUN|ASM|ANN|TAX)_\d{6}$", "required": True,
                              "description": "Internal entity ID"},
                "file_role": {"type": "string", "required": True, "allowed": [
                    "genome_fasta", "cds_fasta", "protein_fasta", "annotation_gff3",
                    "reads_r1", "reads_r2", "reads_single", "assembly_report",
                    *NCBI_SOURCE_FILE_ROLES,
                    "taxonomy_package", "other",
                ], "description": "Biological role of the file"},
                "format": {"type": "string", "required": True,
                           "allowed": ["fasta", "fastq", "gff3", "bam", "cram", "tsv", "txt", "html", "json",
                                       "directory", "other"], "description": "File or directory artifact format"},
                "compression": {"type": "string", "required": True, "allowed": ["none", "gzip", "bgzip"],
                                "description": "Compression type"},
                "relative_path": {"type": "string", "required": True,
                                  "description": "Current path relative to project root"},
                "source_url": {"type": "string", "description": "Original source URL or path"},
                "size_bytes": {"type": "integer", "required": True, "min": 0, "description": "File size in bytes"},
                "sha256": {"type": "string", "required": True, "pattern": r"^[a-f0-9]{64}$",
                           "description": "SHA-256 of the stored bytes"},
                "downloaded_at": {"type": "datetime", "description": "When the file was archived"},
                "status": {"type": "string", "required": True,
                           "allowed": ["DISCOVERED", "DOWNLOADED", "CHECKSUM_VERIFIED", "STANDARDIZED", "REMOTE_ONLY",
                                       "MISSING", "CHECKSUM_FAILED", "CONFLICT"], "description": "File-level status"},
            },
        },
    }
    return {"schema_version": METADATA_SCHEMA_VERSION, "tables": fields}


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
    def from_file(cls, path: str | Path) -> Schema:
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

    def validate_and_normalize(self, table: str, rows: list[dict[str, Any]]) -> tuple[
        list[dict[str, Any]], list[SchemaError]]:
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
                    errors.append(SchemaError(table, row_no, col, raw_row.get(col),
                                              "unknown field; update schema instead of silently accepting it"))
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
                        errors.append(SchemaError(table, row_no, pk, value,
                                                  f"duplicate primary key (first seen at row {seen_primary[value]})"))
                    seen_primary[value] = row_no
                for combo in self.unique_combinations(table):
                    values = tuple(row.get(c) for c in combo)
                    if any(v is None for v in values):
                        continue
                    if values in seen_unique[tuple(combo)]:
                        errors.append(SchemaError(table, row_no, ", ".join(combo), values,
                                                  f"duplicate unique combination (first seen at row {seen_unique[tuple(combo)][values]})"))
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


_TSV_QUOTE_CHARS = ("\t", '"', "\r", "\n")


def _tsv_cell(value: Any) -> str:
    """Render one TSV cell: escape spreadsheet triggers, then quote if needed.

    Quoting is decided here rather than by ``csv.writer`` because the csv
    module quotes a field only when it contains a character of *its*
    ``lineterminator``, and CPython 3.11 additionally started quoting every
    field containing CR or LF.  Deriving the decision from the cell itself
    keeps the bytes identical on every supported interpreter, which matters
    because these files are hashed for provenance (ODR-0044).
    """
    text = escape_formula_text(value)
    if not isinstance(text, str):
        text = str(text)
    if any(char in text for char in _TSV_QUOTE_CHARS):
        return '"' + text.replace('"', '""') + '"'
    return text


def write_tsv(path: str | Path, columns: list[str], rows: Iterable[dict[str, Any] | list[Any]]) -> None:
    """Write rows as TSV, escaping spreadsheet formula triggers in text cells.

    A string cell beginning with ``=``, ``+``, ``-``, ``@``, TAB or CR is
    prefixed with an apostrophe so report files cannot execute as formulas
    when opened in a spreadsheet application (ODR-0040).  Non-string values
    are written verbatim.  A cell containing TAB, CR, LF or ``"`` is written
    quoted with its own quotes doubled; that decision is made here and not by
    ``csv.writer``, so a given row produces the same bytes on every supported
    interpreter (ODR-0044).  This is safe for the release re-ingestion path:
    release-scope coverage reads back only generated identity/join columns
    (entity ids, sha256, size_bytes) that can never begin with a trigger
    character, and provenance hashes are computed over the escaped bytes at
    write time, so they still match on re-read.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\t".join(_tsv_cell(column) for column in columns) + "\n")
        for row in rows:
            if isinstance(row, dict):
                cells = ["" if row.get(c) is None else _tsv_cell(row.get(c)) for c in columns]
            else:
                cells = ["" if v is None else _tsv_cell(v) for v in row]
            handle.write("\t".join(cells) + "\n")


class MetadataRecordError(ValidationError):
    """A rejected metadata record that already collected field warnings.

    ``operon add`` historically warned about unknown fields on stderr and then
    refused to store the row; after the CLI/TUI core extraction the refusal
    raises inside the shared core, so the collected warnings ride the exception
    and the CLI prints them before the error, keeping the stderr shape.
    """

    def __init__(self, message: str, warnings: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.warnings = list(warnings)


def check_row_references(db: Database, entity_type: str, row: dict[str, Any]) -> None:
    """Validate the foreign keys one metadata row carries (``operon add`` time).

    The historical ``operon.cli._check_fks_for_row`` logic, shared by the CLI
    and the TUI so both reject the same dangling references.
    """
    if entity_type == "sample" and row.get("organism_id"):
        db.require_active_entity("organism", row["organism_id"])
    elif (entity_type in {"run", "assembly"}) and row.get("sample_id"):
        db.require_active_entity("sample", row["sample_id"])
    elif entity_type == "annotation" and row.get("assembly_id"):
        db.require_active_entity("assembly", row["assembly_id"])
    for field in ("fasta_file_id", "gff_file_id", "cds_file_id", "protein_file_id"):
        if row.get(field) and db.conn.execute(
                "SELECT 1 FROM files WHERE file_id=?", (row[field],)).fetchone() is None:
            raise ValidationError(
                f"{entity_type} {row.get(ENTITY_ID_COLUMNS.get(entity_type, 'id'))}: "
                f"{field} {row[field]} does not exist")


def add_metadata_record(
        db: Database,
        project: Project,
        entity_type: str,
        fields: dict[str, Any],
        *,
        record_id: str | None = None,
        actor: str | None = None,
) -> dict[str, Any]:
    """Add one schema-validated metadata record — the core behind ``operon add``.

    The whole sequence (ID reservation when no explicit ID is given, column
    reconciliation, normalization, reference checks, the row insert, the
    initial entity state and the audit row) runs in one transaction, so a
    failed add no longer consumes a reserved ID.  Returns
    ``{"entity_type", "entity_id", "warnings"}``; callers surface the warnings
    (the CLI prints them to stderr) because ``validate_and_normalize`` rejects
    unknown fields right after.
    """
    table = ENTITY_TABLES[entity_type]
    id_col = ENTITY_ID_COLUMNS[entity_type]
    schema = Schema.from_file(project.schema_path)
    warnings: list[str] = []
    with db.transaction():
        resolved_id = record_id or db.next_id(entity_type)
        row = dict(fields)
        row[id_col] = resolved_id
        db.ensure_metadata_columns(schema)
        for extra in list(row.keys()):
            if extra not in schema.columns(table):
                warnings.append(
                    f"unknown field {extra!r} for {entity_type}; add it to "
                    f"{project.schema_path} to remove this warning")
        try:
            normalized, _ = schema.validate_and_normalize(table, [row])
        except ValidationError as exc:
            # Warn-then-refuse: the collected field warnings ride the error so
            # the CLI can print them before the refusal (its historical shape).
            raise MetadataRecordError(str(exc), warnings) from exc
        row = normalized[0]
        check_row_references(db, entity_type, row)
        db.insert_row(table, row)
        # Historical wording, kept byte-identical for both entry points.
        db.set_entity_state(entity_type, resolved_id, "METADATA_VALIDATED",
                            "record added via CLI and schema-validated")
        db.record_change(entity_type, resolved_id, None, None,
                         json.dumps({k: str(v) for k, v in row.items()}),
                         "record added", actor=actor)
    return {"entity_type": entity_type, "entity_id": resolved_id, "warnings": warnings}


def add_accession_record(
        db: Database,
        *,
        internal_type: str,
        internal_id: str,
        namespace: str,
        accession: str,
        version: str | None = None,
        primary: bool = False,
        actor: str | None = None,
) -> dict[str, Any]:
    """Map an external accession to an internal stable ID — ``operon add-accession``.

    The target entity must exist and be active.  The mapping row and its audit
    record commit in one transaction; the inserted row is returned.
    """
    db.require_active_entity(internal_type, internal_id)
    row = {
        "internal_type": internal_type,
        "internal_id": internal_id,
        "namespace": namespace,
        "accession": accession,
        "version": version,
        "is_primary": 1 if primary else None,
    }
    with db.transaction():
        db.insert_row("accessions", row)
        db.record_change(
            "accession", f"{namespace}:{accession}", None, None,
            json.dumps(row, ensure_ascii=False, sort_keys=True), "accession added",
            actor=actor,
        )
    return row
