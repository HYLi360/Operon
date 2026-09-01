"""English interactive dataset-import wizard backed by questionary."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

import questionary

from operon.config import Project
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.files import canonical_filename, detect_compression, detect_format, ingest_file, raw_bucket
from operon.schema import ENTITY_ID_COLUMNS, ENTITY_TABLES, Schema
from operon.utils import now_iso, sha256_path
from operon.workflow import flush_run_log, log_run, new_run_id


def _required(value: str) -> bool | str:
    return True if value.strip() else "A value is required."


def _answer(prompt: Any) -> Any:
    value = prompt.ask()
    if value is None:
        raise KeyboardInterrupt
    return value


def _text(message: str, default: str = "", *, required: bool = False) -> str:
    return str(_answer(questionary.text(message, default=default, validate=_required if required else None))).strip()


def _select(message: str, choices: list[Any], *, default: Any = None) -> Any:
    return _answer(questionary.select(message, choices=choices, default=default))


def _path(message: str, *, default: str = "") -> str:
    return str(_answer(questionary.path(message, default=default)))


def _autocomplete(
        message: str,
        choices: list[str],
        *,
        default: str = "",
        meta_information: dict[str, str] | None = None,
) -> str:
    allowed = set(choices)
    return str(_answer(questionary.autocomplete(
        message,
        choices=choices,
        default=default,
        meta_information=meta_information,
        validate=lambda value: True if value in allowed else "Choose one of the completion options.",
    ))).strip()


def _confirm(message: str, default: bool = True) -> bool:
    return bool(_answer(questionary.confirm(message, default=default)))


def _choice_rows(rows: list[dict[str, Any]], id_field: str, label: Callable[[dict[str, Any]], str]) -> list[Any]:
    return [questionary.Choice(f"{row[id_field]}  {label(row)}", value=row[id_field]) for row in rows]


def _ask_organism(db: Database, draft: dict[str, Any]) -> None:
    rows = [dict(row) for row in db.conn.execute(
        "SELECT organism_id, scientific_name, taxon_id, taxonomy_source, taxonomy_version "
        "FROM organisms o WHERE NOT EXISTS (SELECT 1 FROM effective_retired_entities r "
        "WHERE r.entity_type='organism' AND r.entity_id=o.organism_id) "
        "ORDER BY scientific_name, organism_id"
    ).fetchall()]
    create_label = "Create a new organism"
    name_counts: dict[str, int] = {}
    for row in rows:
        name = str(row["scientific_name"])
        name_counts[name] = name_counts.get(name, 0) + 1
    labels: dict[str, str] = {}
    metadata: dict[str, str] = {create_label: "Allocate a new internal organism ID"}
    for row in rows:
        name = str(row["scientific_name"])
        label = name if name_counts[name] == 1 else f"{name} [{row['organism_id']}]"
        labels[label] = row["organism_id"]
        details = [row["organism_id"]]
        if row.get("taxon_id"):
            details.append(f"TaxID {row['taxon_id']}")
        metadata[label] = " | ".join(details)
    current_id = draft.get("organism", {}).get("id")
    default = next((label for label, organism_id in labels.items() if organism_id == current_id), "")
    selected = _autocomplete(
        "Select the organism:", [create_label, *labels], default=default,
        meta_information=metadata,
    )
    if selected != create_label:
        draft["organism"] = {"action": "reuse", "id": labels[selected]}
        return
    row = {
        "organism_id": db.next_id("organism"),
        "scientific_name": _text("Scientific name:", required=True),
        "taxon_id": _text("Taxonomy ID (optional):"),
        "taxonomic_rank": _text("Taxonomic rank (optional):", default="species"),
        "taxonomy_source": _select("Taxonomy source:", ["NCBI", "GTDB", "other", questionary.Choice("Skip", value="")]),
        "taxonomy_version": _text("Taxonomy version (optional):"),
    }
    draft["organism"] = {"action": "create", "id": row["organism_id"], "row": row}


def _ask_source(_db: Database, draft: dict[str, Any]) -> None:
    current = draft.get("source", {})
    source_type = _select("Source classification:", [
        questionary.Choice("INSDC (GenBank / ENA / DDBJ)", value="insdc"),
        questionary.Choice("Non-INSDC database, repository, or institution", value="non_insdc"),
    ], default=current.get("source_type"))
    draft["source"] = {
        "source_type": source_type,
        "database_name": _text(
            "Source database or repository:", current.get("database_name", ""), required=True
        ),
        "provider": _text("Data provider or institution:", current.get("provider", ""), required=True),
        "record_url": _text("Source record URL (optional):", current.get("record_url", "")),
        "citation": _text(
            "Reference citation or DOI" + (":" if source_type == "non_insdc" else " (optional):"),
            current.get("citation", ""), required=source_type == "non_insdc",
        ),
        "license_name": _text(
            "License name or SPDX identifier" + (":" if source_type == "non_insdc" else " (optional):"),
            current.get("license_name", ""), required=source_type == "non_insdc",
        ),
        "license_url": _text("License URL (optional):", current.get("license_url", "")),
    }


def _ask_sample(db: Database, draft: dict[str, Any]) -> None:
    organism_id = draft["organism"]["id"]
    rows = [dict(row) for row in db.conn.execute(
        "SELECT sample_id, isolate, strain, biosample_accession FROM samples s "
        "WHERE organism_id=? AND NOT EXISTS (SELECT 1 FROM effective_retired_entities r "
        "WHERE r.entity_type='sample' AND r.entity_id=s.sample_id) ORDER BY sample_id",
        (organism_id,),
    ).fetchall()]
    choices: list[Any] = [questionary.Choice("Create a new sample", value="__new__")]
    choices.extend(_choice_rows(
        rows, "sample_id",
        lambda row: row.get("isolate") or row.get("strain") or row.get("biosample_accession") or "sample"
    ))
    selected = _select("Select the sample:", choices)
    if selected != "__new__":
        draft["sample"] = {"action": "reuse", "id": selected}
        return
    source = draft.get("source", {})
    row = {
        "sample_id": db.next_id("sample"),
        "organism_id": organism_id,
        "biosample_accession": _text("BioSample accession (optional):"),
        "strain": _text("Strain (optional):"),
        "isolate": _text("Isolate (optional):"),
        "source_record": source.get("record_url", ""),
    }
    draft["sample"] = {"action": "create", "id": row["sample_id"], "row": row}


def _ask_sequencing(db: Database, draft: dict[str, Any]) -> None:
    if not _confirm("Record sequencing information?", default=bool(draft.get("run"))):
        draft["run"] = None
        return
    current = (draft.get("run") or {}).get("row", {})
    row = {
        "run_id": current.get("run_id") or db.next_id("run"),
        "sample_id": draft["sample"]["id"],
        "run_accession": _text("Run accession (optional):", current.get("run_accession", "")),
        "experiment_accession": _text("Experiment accession (optional):", current.get("experiment_accession", "")),
        "library_strategy": _select("Library strategy:",
                                    ["WGS", "WGA", "RNA-Seq", "Amplicon", "Hi-C", "ATAC-seq", "other",
                                     questionary.Choice("Skip", value="")]),
        "library_source": _select("Library source:", ["GENOMIC", "TRANSCRIPTOMIC", "METAGENOMIC", "OTHER",
                                                      questionary.Choice("Skip", value="")]),
        "library_layout": _select("Library layout:",
                                  ["PAIRED", "SINGLE", "unknown", questionary.Choice("Skip", value="")]),
        "platform": _select("Sequencing platform:",
                            ["ILLUMINA", "PACBIO_SMRT", "OXFORD_NANOPORE", "BGISEQ", "ION_TORRENT", "other",
                             questionary.Choice("Skip", value="")]),
        "instrument_model": _text("Instrument model (optional):", current.get("instrument_model", "")),
    }
    draft["run"] = {"action": "create", "id": row["run_id"], "row": row}


def _ask_assembly(db: Database, draft: dict[str, Any]) -> None:
    sample_id = draft["sample"]["id"]
    rows = [dict(row) for row in db.conn.execute(
        "SELECT assembly_id, assembly_accession, assembly_name, assembly_version "
        "FROM assemblies a WHERE sample_id=? AND NOT EXISTS ("
        "SELECT 1 FROM effective_retired_entities r "
        "WHERE r.entity_type='assembly' AND r.entity_id=a.assembly_id) ORDER BY assembly_id",
        (sample_id,),
    ).fetchall()]
    choices: list[Any] = [questionary.Choice("Create a new assembly", value="__new__")]
    choices.extend(_choice_rows(
        rows, "assembly_id", lambda row: row.get("assembly_accession") or row.get("assembly_name") or "assembly"
    ))
    selected = _select("Select the assembly:", choices)
    if selected != "__new__":
        draft["assembly"] = {"action": "reuse", "id": selected}
        return
    source = draft.get("source", {})
    row = {
        "assembly_id": db.next_id("assembly"),
        "sample_id": sample_id,
        "assembly_accession": _text("Assembly accession (optional):"),
        "assembly_name": _text("Assembly name (optional):"),
        "assembly_version": _text("Assembly version (optional):", default="1"),
        "assembly_level": _select("Assembly level:", ["complete_genome", "chromosome", "scaffold", "contig",
                                                      questionary.Choice("Skip", value="")]),
        "assembly_method": _text("Assembly software and parameters (optional):"),
        "submitter": source.get("provider", ""),
        "source_database": _select("Source database:",
                                   ["RefSeq", "GenBank", "other", questionary.Choice("Skip", value="")]),
    }
    draft["assembly"] = {"action": "create", "id": row["assembly_id"], "row": row}


def _ask_annotation(db: Database, draft: dict[str, Any]) -> None:
    if not _confirm("Record an annotation release?", default=bool(draft.get("annotation")) or True):
        draft["annotation"] = None
        return
    assembly_id = draft["assembly"]["id"]
    rows = [dict(row) for row in db.conn.execute(
        "SELECT annotation_id, annotation_source, annotation_version FROM annotations n "
        "WHERE assembly_id=? AND NOT EXISTS (SELECT 1 FROM effective_retired_entities r "
        "WHERE r.entity_type='annotation' AND r.entity_id=n.annotation_id) "
        "ORDER BY annotation_id",
        (assembly_id,),
    ).fetchall()]
    choices: list[Any] = [questionary.Choice("Create a new annotation", value="__new__")]
    choices.extend(_choice_rows(
        rows, "annotation_id",
        lambda row: f"{row.get('annotation_source') or 'annotation'} v{row.get('annotation_version') or '?'}"
    ))
    selected = _select("Select the annotation:", choices)
    if selected != "__new__":
        draft["annotation"] = {"action": "reuse", "id": selected}
        return
    source = draft.get("source", {})
    row = {
        "annotation_id": db.next_id("annotation"),
        "assembly_id": assembly_id,
        "annotation_source": _text("Annotation pipeline or source (optional):", source.get("provider", "")),
        "annotation_version": _text("Annotation version (optional):", default="1"),
        "annotation_date": _text("Annotation date, YYYY-MM-DD (optional):"),
    }
    draft["annotation"] = {"action": "create", "id": row["annotation_id"], "row": row}


def _ask_path(label: str, current: str = "") -> str:
    while True:
        value = _path(f"{label} path (optional):", default=current)
        if not value or Path(value).expanduser().is_file():
            return str(Path(value).expanduser().resolve()) if value else ""
        print(f"File does not exist: {value}")


def _ask_files(_db: Database, draft: dict[str, Any]) -> None:
    current = {item["role"]: item["path"] for item in draft.get("files", [])}
    files: list[dict[str, str]] = []
    entries = [("Genome FASTA", "genome_fasta", "assembly")]
    if draft.get("annotation"):
        entries.extend([
            ("GFF3", "annotation_gff3", "annotation"),
            ("CDS FASTA", "cds_fasta", "annotation"),
            ("Protein FASTA", "protein_fasta", "annotation"),
        ])
    if draft.get("run"):
        entries.extend([
            ("Reads R1", "reads_r1", "run"),
            ("Reads R2", "reads_r2", "run"),
            ("Single-end reads", "reads_single", "run"),
        ])
    for label, role, entity_type in entries:
        path = _ask_path(label, current.get(role, ""))
        if path:
            files.append({"label": label, "role": role, "entity_type": entity_type, "path": path})
    draft["files"] = files


def _warnings(db: Database, draft: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    warnings.extend(_source_validation_errors(draft))
    if not draft.get("source", {}).get("record_url"):
        warnings.append("Source record URL is missing.")
    organism = draft.get("organism", {})
    if organism.get("action") == "create" and not organism.get("row", {}).get("taxon_id"):
        warnings.append("Taxonomy ID is missing.")
    if not draft.get("run"):
        warnings.append("Sequencing provenance will not be recorded.")
    if not any(item["role"] == "genome_fasta" for item in draft.get("files", [])):
        warnings.append("Genome FASTA is missing.")
    if draft.get("annotation"):
        for label, role in (("GFF3", "annotation_gff3"), ("CDS FASTA", "cds_fasta"),
                            ("Protein FASTA", "protein_fasta")):
            if not any(item["role"] == role for item in draft.get("files", [])):
                warnings.append(f"{label} is missing from the annotation bundle.")
    sample = draft.get("sample") or {}
    sample_id = sample.get("id")
    if sample_id and sample.get("action") == "reuse":
        row = db.conn.execute("SELECT organism_id FROM samples WHERE sample_id=?", (sample_id,)).fetchone()
        if row and row["organism_id"] != draft.get("organism", {}).get("id"):
            warnings.append("The selected sample does not belong to the selected organism.")
    assembly = draft.get("assembly") or {}
    assembly_id = assembly.get("id")
    if assembly_id and assembly.get("action") == "reuse":
        row = db.conn.execute("SELECT sample_id FROM assemblies WHERE assembly_id=?", (assembly_id,)).fetchone()
        if row and row["sample_id"] != sample_id:
            warnings.append("The selected assembly does not belong to the selected sample.")
    return warnings


def _source_validation_errors(draft: dict[str, Any]) -> list[str]:
    source = draft.get("source", {})
    errors: list[str] = []
    if source.get("source_type") not in {"insdc", "non_insdc"}:
        errors.append("Source classification is missing or invalid.")
    if not source.get("database_name"):
        errors.append("Source database or repository is missing.")
    if not source.get("provider"):
        errors.append("Data provider or institution is missing.")
    if source.get("source_type") == "non_insdc":
        if not source.get("citation"):
            errors.append("Non-INSDC data requires a reference citation or DOI.")
        if not source.get("license_name"):
            errors.append("Non-INSDC data requires a License name or SPDX identifier.")
    return errors


def _synchronize_new_entity_links(draft: dict[str, Any]) -> None:
    """Keep newly drafted descendants attached after a review edit upstream.

    Reused entities are never rewritten: if a user changes an upstream choice
    and an existing descendant no longer belongs to it, the summary shows a
    blocking warning and asks the user to revise that section explicitly.
    """
    organism = draft.get("organism")
    source = draft.get("source", {})
    sample = draft.get("sample")
    run = draft.get("run")
    assembly = draft.get("assembly")
    annotation = draft.get("annotation")
    if organism and sample and sample.get("action") == "create":
        sample["row"]["organism_id"] = organism["id"]
        sample["row"]["source_record"] = source.get("record_url", "")
    if sample and run and run.get("action") == "create":
        run["row"]["sample_id"] = sample["id"]
    if sample and assembly and assembly.get("action") == "create":
        assembly["row"]["sample_id"] = sample["id"]
        assembly["row"]["submitter"] = source.get("provider", "")
    if assembly and annotation and annotation.get("action") == "create":
        annotation["row"]["assembly_id"] = assembly["id"]


def _summary(db: Database, draft: dict[str, Any]) -> str:
    lines = ["", "Import plan", "===========", ""]
    source = draft.get("source", {})
    source_type = {
        "insdc": "INSDC",
        "non_insdc": "non-INSDC",
    }.get(source.get("source_type"), "[missing]")
    lines.extend([
        "[1] Source",
        f"    Classification: {source_type}",
        f"    Database:       {source.get('database_name') or '[missing]'}",
        f"    Provider:       {source.get('provider') or '[missing]'}",
        f"    Record URL:     {source.get('record_url') or '[not provided]'}",
        f"    Citation:       {source.get('citation') or '[not provided]'}",
        f"    License:        {source.get('license_name') or '[not provided]'}",
        f"    License URL:    {source.get('license_url') or '[not provided]'}",
        "",
    ])
    for number, name in ((2, "organism"), (3, "sample"), (5, "assembly"), (6, "annotation")):
        item = draft.get(name)
        lines.append(f"[{number}] {name.capitalize()}")
        if not item:
            lines.append("    [skipped]")
        else:
            lines.append(f"    Action: {item['action']}")
            lines.append(f"    ID:     {item['id']}")
            for key, value in item.get("row", {}).items():
                if key != ENTITY_ID_COLUMNS.get(name) and value not in {None, ""}:
                    lines.append(f"    {key}: {value}")
        lines.append("")
    lines.append("[4] Sequencing")
    run = draft.get("run")
    if not run:
        lines.append("    [skipped]")
    else:
        lines.append(f"    ID: {run['id']}")
        for key, value in run["row"].items():
            if key != "run_id" and value not in {None, ""}:
                lines.append(f"    {key}: {value}")
    lines.extend(["", "[7] Files"])
    if not draft.get("files"):
        lines.append("    [none]")
    for item in draft.get("files", []):
        path = Path(item["path"])
        lines.append(f"    {item['label']}: {path} ({path.stat().st_size} bytes)")
    warnings = _warnings(db, draft)
    lines.extend(["", "Warnings"])
    if warnings:
        lines.extend(f"    ! {warning}" for warning in warnings)
    else:
        lines.append("    None")
    return "\n".join(lines)


def _normalized_new_rows(project: Project, draft: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    schema = Schema.from_file(project.schema_path)
    rows: list[tuple[str, str, dict[str, Any]]] = []
    for entity_type in ("organism", "sample", "run", "assembly", "annotation"):
        item = draft.get(entity_type)
        if not item or item.get("action") != "create":
            continue
        table = ENTITY_TABLES[entity_type]
        normalized, _ = schema.validate_and_normalize(table, [item["row"]])
        rows.append((entity_type, table, normalized[0]))
    return rows


def _preflight(db: Database, project: Project, draft: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    source_errors = _source_validation_errors(draft)
    if source_errors:
        raise ValidationError("\n".join(source_errors))
    rows = _normalized_new_rows(project, draft)
    for entity_type, _table, row in rows:
        entity_id = row[ENTITY_ID_COLUMNS[entity_type]]
        if db.entity_exists(entity_type, entity_id):
            raise ConflictError(f"planned ID already exists: {entity_type} {entity_id}")
    blocking = [warning for warning in _warnings(db, draft) if "does not belong" in warning]
    if blocking:
        raise ValidationError("\n".join(blocking))
    for item in draft.get("files", []):
        entity = draft.get(item["entity_type"])
        if not entity:
            raise ValidationError(f"{item['label']} has no target {item['entity_type']} entity")
        source = Path(item["path"])
        digest = sha256_path(source)
        existing = db.conn.execute(
            "SELECT file_id, sha256 FROM files WHERE entity_type=? AND entity_id=? AND file_role=? LIMIT 1",
            (item["entity_type"], entity["id"], item["role"]),
        ).fetchone()
        if existing and str(existing["sha256"]).lower() != digest.lower():
            raise ConflictError(
                f"{item['entity_type']} {entity['id']} already has different bytes for role {item['role']}"
            )
    return rows


def _commit(db: Database, project: Project, draft: dict[str, Any]) -> dict[str, Any]:
    rows = _preflight(db, project, draft)
    actor = os.environ.get("USER")
    run_id = new_run_id()
    started_at = now_iso()
    provenance_buffer: list[dict[str, Any]] = []
    source_id = ""
    new_targets: list[Path] = []
    for item in draft.get("files", []):
        entity_id = draft[item["entity_type"]]["id"]
        source = Path(item["path"])
        target = project.raw_root / raw_bucket(item["entity_type"]) / entity_id / canonical_filename(
            entity_id, item["role"], detect_format(source, item["role"]), detect_compression(source)
        )
        if not target.exists():
            new_targets.append(target)
    file_rows: list[dict[str, Any]] = []
    try:
        with db.transaction() as conn:
            source_record = db.register_data_source(
                draft["source"], workflow_run_id=run_id
            )
            source_id = source_record["source_id"]
            for entity_type, table, row in rows:
                columns = list(row.keys())
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    [row[column] for column in columns],
                )
                entity_id = row[ENTITY_ID_COLUMNS[entity_type]]
                conn.execute(
                    "INSERT INTO entity_state(entity_type, entity_id, state, message, updated_at) VALUES(?,?,?,?,?)",
                    (entity_type, entity_id, "METADATA_VALIDATED", "created by interactive import", now_iso()),
                )
                db.record_change(entity_type, entity_id, None, None,
                                 json.dumps(row, ensure_ascii=False, sort_keys=True),
                                 "interactive dataset import", actor=actor)
            source_url = draft.get("source", {}).get("record_url") or None
            for item in draft.get("files", []):
                entity_id = draft[item["entity_type"]]["id"]
                file_rows.append(ingest_file(
                    db, project, item["path"], item["entity_type"], entity_id, item["role"],
                    source_url=source_url, actor=actor, run_id=run_id,
                    provenance_buffer=provenance_buffer,
                ))
            source_objects = [
                (entity_type, item["id"])
                for entity_type in ENTITY_TABLES
                if (item := draft.get(entity_type))
            ]
            source_objects.extend(("file", row["file_id"]) for row in file_rows)
            db.link_data_source(
                source_id, source_objects, workflow_run_id=run_id
            )
            db.record_change(
                "data_source", source_id, None, None,
                json.dumps(source_record, ensure_ascii=False, sort_keys=True),
                "external source linked by interactive dataset import", actor=actor,
            )
            log_run(db, project, {
                "run_id": run_id,
                "step": "interactive_dataset_import",
                "status": "completed",
                "started_at": started_at,
                "finished_at": now_iso(),
                "tool": "operon.import_wizard",
                "command": "import dataset",
            }, jsonl_buffer=provenance_buffer)
    except Exception as exc:
        for target in new_targets:
            if target.is_file() or target.is_symlink():
                target.unlink(missing_ok=True)
            elif target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
        try:
            log_run(db, project, {
                "run_id": run_id,
                "step": "interactive_dataset_import",
                "status": "failed",
                "started_at": started_at,
                "finished_at": now_iso(),
                "tool": "operon.import_wizard",
                "command": "import dataset",
                "error": f"{type(exc).__name__}: {exc}",
            })
        except Exception:
            pass
        raise
    flush_run_log(project, provenance_buffer)
    return {
        "run_id": run_id,
        "source_id": source_id,
        "entities": {name: item["id"] for name, item in draft.items() if name in ENTITY_TABLES and item},
        "files": [{"file_id": row["file_id"], "role": row["file_role"], "sha256": row["sha256"]} for row in file_rows],
        "warnings": _warnings(db, draft),
    }


def run_dataset_wizard(db: Database, project: Project) -> dict[str, Any] | None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValidationError("interactive dataset import requires a terminal")
    print("Operon interactive dataset import")
    print("No project data will be changed until you confirm the final review.\n")
    draft: dict[str, Any] = {}
    sections: list[tuple[str, Callable[[Database, dict[str, Any]], None]]] = [
        ("source", _ask_source),
        ("organism", _ask_organism),
        ("sample", _ask_sample),
        ("sequencing", _ask_sequencing),
        ("assembly", _ask_assembly),
        ("annotation", _ask_annotation),
        ("files", _ask_files),
    ]
    for _name, section in sections:
        section(db, draft)
        _synchronize_new_entity_links(draft)
    actions = [
        questionary.Choice("Execute import", value="execute"),
        *[questionary.Choice(f"Edit {name}", value=name) for name, _section in sections],
        questionary.Choice("Cancel", value="cancel"),
    ]
    by_name = dict(sections)
    while True:
        print(_summary(db, draft))
        action = _select("Next action:", actions)
        if action == "cancel":
            return None
        if action == "execute":
            _preflight(db, project, draft)
            if _warnings(db, draft) and not _confirm("Warnings remain. Execute this import anyway?", default=False):
                continue
            return _commit(db, project, draft)
        # Review edits are deliberately non-linear: edit one section, then
        # return directly to the summary instead of resuming the original flow.
        by_name[action](db, draft)
        _synchronize_new_entity_links(draft)
