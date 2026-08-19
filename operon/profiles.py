"""Versioned QC profiles: metrics are measured, rules are configured separately.

By design, QC tools compute metrics while the rule engine reads a YAML
profile and emits decisions + reasons.  Thresholds are never hard-coded
inside the QC programs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from operon.errors import ValidationError


def default_profiles() -> dict[str, Any]:
    return {
        "file_integrity_v1": {
            "version": 1,
            "description": "Every registered file must parse and match its recorded checksum.",
            "applies_to": ["assembly", "annotation", "run"],
            "required": [
                {"metric": "sha256_match", "operator": "==", "value": 1, "code": "SHA256_MISMATCH"},
                {"metric": "parseable", "operator": "==", "value": 1, "code": "FORMAT_INVALID"},
            ],
            "warnings": [],
        },
        "assembly_production_v1": {
            "version": 1,
            "description": "Generic assembly profile for comparative genomics (tune per taxon/purpose).",
            "applies_to": ["assembly"],
            "required": [
                {"metric": "sha256_match", "operator": "==", "value": 1, "code": "SHA256_MISMATCH"},
                {"metric": "parseable", "operator": "==", "value": 1, "code": "FORMAT_INVALID"},
                {"metric": "total_length", "operator": ">=", "value": 1000, "code": "ASSEMBLY_TOO_SHORT"},
                {"metric": "contig_n50", "operator": ">=", "value": 1000, "code": "LOW_CONTIGUITY"},
                {"metric": "ambiguous_base_percent", "operator": "<=", "value": 5, "code": "HIGH_AMBIGUOUS_BASE_CONTENT"},
                {"metric": "empty_sequence_count", "operator": "==", "value": 0, "code": "EMPTY_SEQUENCE"},
            ],
            "warnings": [
                {"metric": "n_percent", "operator": ">", "value": 1, "code": "HIGH_GAP_CONTENT"},
                {"metric": "duplicate_sequence_id_count", "operator": ">", "value": 0, "code": "DUPLICATE_SEQUENCE_ID"},
            ],
        },
        "annotation_release_v1": {
            "version": 1,
            "description": "Annotation release sanity checks before using proteins/genes in analysis.",
            "applies_to": ["annotation"],
            "required": [
                {"metric": "parseable", "operator": "==", "value": 1, "code": "FORMAT_INVALID"},
                {"metric": "gene_count", "operator": ">=", "value": 1, "code": "NO_GENES"},
                {"metric": "cds_count", "operator": ">=", "value": 1, "code": "NO_CDS"},
                {"metric": "cds_length_multiple3_percent", "operator": ">=", "value": 99, "code": "CDS_NOT_MULTIPLE_OF_3"},
                {"metric": "missing_parent_count", "operator": "==", "value": 0, "code": "BROKEN_GFF3_PARENTS"},
                {"metric": "coordinate_error_count", "operator": "==", "value": 0, "code": "INVALID_GFF3_COORDINATES"},
            ],
            "warnings": [
                {"metric": "internal_stop_count", "operator": ">", "value": 0, "code": "INTERNAL_STOP_CODONS"},
                {"metric": "protein_duplicate_id_count", "operator": ">", "value": 0, "code": "DUPLICATE_PROTEIN_ID"},
                {"metric": "seqid_mismatch_count", "operator": ">", "value": 0, "code": "SEQID_NOT_IN_FASTA"},
            ],
        },
        "reads_qc_v1": {
            "version": 1,
            "description": "Raw read QC gate before assembly or variant calling.",
            "applies_to": ["run"],
            "required": [
                {"metric": "parseable", "operator": "==", "value": 1, "code": "FORMAT_INVALID"},
                {"metric": "read_count", "operator": ">=", "value": 1, "code": "NO_READS"},
                {"metric": "q20_percent", "operator": ">=", "value": 80, "code": "LOW_Q20"},
                {"metric": "q30_percent", "operator": ">=", "value": 70, "code": "LOW_Q30"},
            ],
            "warnings": [
                {"metric": "duplicate_percent", "operator": ">", "value": 30, "code": "HIGH_DUPLICATION"},
                {"metric": "overrepresented_sequence_count", "operator": ">", "value": 1, "code": "OVERREPRESENTED_SEQUENCES"},
            ],
        },
    }


def write_default_profiles(directory: str | Path) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, profile in default_profiles().items():
        path = directory / f"{name}.yaml"
        path.write_text(
            f"# QC profile {name} (versioned; do not edit thresholds silently)\n"
            + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


def load_profiles(directory: str | Path) -> dict[str, dict[str, Any]]:
    directory = Path(directory)
    profiles: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return {}
    for path in sorted(directory.glob("*.yaml")):
        with open(path, encoding="utf-8") as handle:
            doc = yaml.safe_load(handle) or {}
        if not isinstance(doc, dict) or "version" not in doc:
            raise ValidationError(f"invalid QC profile {path}: missing 'version'")
        profiles[path.stem] = doc
    return profiles
