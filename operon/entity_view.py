"""High-level entity graph lookup by internal ID or external accession."""

from __future__ import annotations

import re
from typing import Any

from operon.database import Database
from operon.errors import EntityNotFoundError, ValidationError
from operon.schema import ENTITY_ID_COLUMNS, ENTITY_TABLES


INTERNAL_ID_RE = re.compile(r"^(ORG|SMP|RUN|ASM|ANN)_\d{6}$", re.IGNORECASE)
PREFIX_TYPES = {"ORG": "organism", "SMP": "sample", "RUN": "run", "ASM": "assembly", "ANN": "annotation"}


def resolve_identifier(db: Database, identifier: str) -> tuple[str, str]:
    value = identifier.strip()
    match = INTERNAL_ID_RE.fullmatch(value)
    if match:
        entity_type = PREFIX_TYPES[match.group(1).upper()]
        entity_id = value.upper()
        db.require_entity(entity_type, entity_id)
        return entity_type, entity_id

    if ":" in value:
        namespace, accession = value.split(":", 1)
        rows = db.conn.execute(
            "SELECT internal_type, internal_id FROM accessions WHERE namespace=? AND accession=?",
            (namespace, accession),
        ).fetchall()
    else:
        rows = db.conn.execute(
            "SELECT internal_type, internal_id FROM accessions WHERE accession=? ORDER BY namespace",
            (value,),
        ).fetchall()
    if not rows:
        raise EntityNotFoundError(f"identifier or accession {identifier!r} was not found")
    unique = {(row["internal_type"], row["internal_id"]) for row in rows}
    if len(unique) > 1:
        choices = ", ".join(f"{row['internal_type']} {row['internal_id']}" for row in rows)
        raise ValidationError(
            f"accession {identifier!r} is ambiguous ({choices}); use NAMESPACE:ACCESSION"
        )
    return next(iter(unique))


def _organism_for(db: Database, entity_type: str, entity_id: str) -> str:
    if entity_type == "organism":
        return entity_id
    if entity_type == "sample":
        row = db.conn.execute("SELECT organism_id FROM samples WHERE sample_id=?", (entity_id,)).fetchone()
    elif entity_type == "run":
        row = db.conn.execute(
            "SELECT s.organism_id FROM runs r JOIN samples s ON s.sample_id=r.sample_id WHERE r.run_id=?",
            (entity_id,),
        ).fetchone()
    elif entity_type == "assembly":
        row = db.conn.execute(
            "SELECT s.organism_id FROM assemblies a JOIN samples s ON s.sample_id=a.sample_id WHERE a.assembly_id=?",
            (entity_id,),
        ).fetchone()
    elif entity_type == "annotation":
        row = db.conn.execute(
            "SELECT s.organism_id FROM annotations n JOIN assemblies a ON a.assembly_id=n.assembly_id "
            "JOIN samples s ON s.sample_id=a.sample_id WHERE n.annotation_id=?",
            (entity_id,),
        ).fetchone()
    else:
        row = None
    if row is None:
        raise EntityNotFoundError(f"cannot resolve organism for {entity_type} {entity_id}")
    organism_id = row["organism_id"]
    if organism_id is None or not str(organism_id).strip():
        raise EntityNotFoundError(f"{entity_type} {entity_id} has no organism reference")
    return str(organism_id)


def entity_graph(
    db: Database,
    identifier: str,
    *,
    scope: str = "matched",
    include_superseded: bool = False,
    include_retired: bool = False,
) -> dict[str, Any]:
    """Return an entity-centered graph, optionally expanded to the organism.

    ``matched`` keeps only the lineage and descendants that belong to the
    resolved entity.  ``organism`` preserves the original broad ``show``
    behavior and returns every descendant of the owning organism.
    """
    if scope not in {"matched", "organism"}:
        raise ValidationError(f"unknown entity graph scope {scope!r}")
    matched_type, matched_id = resolve_identifier(db, identifier)
    organism_id = _organism_for(db, matched_type, matched_id)
    organism_row = db.conn.execute(
        "SELECT * FROM organisms WHERE organism_id=?", (organism_id,)
    ).fetchone()
    if organism_row is None:
        raise EntityNotFoundError(
            f"{matched_type} {matched_id} refers to missing organism {organism_id}"
        )
    organism = dict(organism_row)
    samples = [dict(row) for row in db.conn.execute(
        "SELECT * FROM samples WHERE organism_id=? ORDER BY sample_id", (organism_id,)
    ).fetchall()]
    sample_ids = [row["sample_id"] for row in samples]
    if sample_ids:
        placeholders = ", ".join("?" for _ in sample_ids)
        runs = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM runs WHERE sample_id IN ({placeholders}) ORDER BY sample_id, run_id", sample_ids
        ).fetchall()]
        assemblies = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM assemblies WHERE sample_id IN ({placeholders}) ORDER BY sample_id, assembly_id", sample_ids
        ).fetchall()]
    else:
        runs, assemblies = [], []
    assembly_ids = [row["assembly_id"] for row in assemblies]
    if assembly_ids:
        placeholders = ", ".join("?" for _ in assembly_ids)
        annotations = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM annotations WHERE assembly_id IN ({placeholders}) ORDER BY assembly_id, annotation_id",
            assembly_ids,
        ).fetchall()]
    else:
        annotations = []
    if scope == "matched" and matched_type != "organism":
        if matched_type == "sample":
            selected_sample_ids = {matched_id}
            selected_run_ids = {
                row["run_id"] for row in runs if row["sample_id"] == matched_id
            }
            selected_assembly_ids = {
                row["assembly_id"] for row in assemblies if row["sample_id"] == matched_id
            }
            selected_annotation_ids = {
                row["annotation_id"] for row in annotations
                if row["assembly_id"] in selected_assembly_ids
            }
        elif matched_type == "run":
            matched_run = next(row for row in runs if row["run_id"] == matched_id)
            selected_sample_ids = {matched_run["sample_id"]}
            selected_run_ids = {matched_id}
            selected_assembly_ids = set()
            selected_annotation_ids = set()
        elif matched_type == "assembly":
            matched_assembly = next(
                row for row in assemblies if row["assembly_id"] == matched_id
            )
            selected_sample_ids = {matched_assembly["sample_id"]}
            selected_run_ids = set()
            selected_assembly_ids = {matched_id}
            selected_annotation_ids = {
                row["annotation_id"] for row in annotations
                if row["assembly_id"] == matched_id
            }
        else:  # annotation
            matched_annotation = next(
                row for row in annotations if row["annotation_id"] == matched_id
            )
            selected_assembly_ids = {matched_annotation["assembly_id"]}
            parent_assembly = next(
                row for row in assemblies
                if row["assembly_id"] == matched_annotation["assembly_id"]
            )
            selected_sample_ids = {parent_assembly["sample_id"]}
            selected_run_ids = set()
            selected_annotation_ids = {matched_id}
        samples = [row for row in samples if row["sample_id"] in selected_sample_ids]
        runs = [row for row in runs if row["run_id"] in selected_run_ids]
        assemblies = [
            row for row in assemblies if row["assembly_id"] in selected_assembly_ids
        ]
        annotations = [
            row for row in annotations if row["annotation_id"] in selected_annotation_ids
        ]

    candidate_pairs = [
        ("organism", organism_id),
        *(('sample', row["sample_id"]) for row in samples),
        *(('run', row["run_id"]) for row in runs),
        *(('assembly', row["assembly_id"]) for row in assemblies),
        *(('annotation', row["annotation_id"]) for row in annotations),
    ]
    candidate_ids = [object_id for _object_type, object_id in candidate_pairs]
    if candidate_ids:
        placeholders = ", ".join("?" for _ in candidate_ids)
        supersessions = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM entity_supersessions WHERE object_id IN ({placeholders}) "
            "ORDER BY object_type, object_id",
            candidate_ids,
        ).fetchall()]
    else:
        supersessions = []
    candidate_pair_set = set(candidate_pairs)
    supersessions = [
        row for row in supersessions
        if (row["object_type"], row["object_id"]) in candidate_pair_set
    ]
    superseded_pairs = {
        (row["object_type"], row["object_id"]) for row in supersessions
    }
    if not include_superseded:
        def current(row: dict[str, Any], entity_type: str, id_column: str) -> bool:
            pair = (entity_type, row[id_column])
            return pair == (matched_type, matched_id) or pair not in superseded_pairs

        samples = [row for row in samples if current(row, "sample", "sample_id")]
        sample_ids = {row["sample_id"] for row in samples}
        runs = [
            row for row in runs
            if row["sample_id"] in sample_ids and current(row, "run", "run_id")
        ]
        assemblies = [
            row for row in assemblies
            if row["sample_id"] in sample_ids
            and current(row, "assembly", "assembly_id")
        ]
        assembly_ids = {row["assembly_id"] for row in assemblies}
        annotations = [
            row for row in annotations
            if row["assembly_id"] in assembly_ids
            and current(row, "annotation", "annotation_id")
        ]

    retirement_ids = [object_id for _object_type, object_id in candidate_pairs]
    if retirement_ids and db.lifecycle_schema_available():
        placeholders = ", ".join("?" for _ in retirement_ids)
        retirements = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM effective_retired_entities "
            f"WHERE entity_id IN ({placeholders}) "
            "ORDER BY entity_type, entity_id, event_id",
            retirement_ids,
        ).fetchall()]
    else:
        retirements = []
    candidate_pair_set = set(candidate_pairs)
    retirements = [
        row for row in retirements
        if (row["entity_type"], row["entity_id"]) in candidate_pair_set
    ]
    retired_pairs = {
        (row["entity_type"], row["entity_id"]) for row in retirements
    }
    matched_is_retired = (matched_type, matched_id) in retired_pairs
    if not include_retired and not matched_is_retired:
        samples = [
            row for row in samples
            if ("sample", row["sample_id"]) not in retired_pairs
        ]
        sample_ids = {row["sample_id"] for row in samples}
        runs = [
            row for row in runs
            if row["sample_id"] in sample_ids
            and ("run", row["run_id"]) not in retired_pairs
        ]
        assemblies = [
            row for row in assemblies
            if row["sample_id"] in sample_ids
            and ("assembly", row["assembly_id"]) not in retired_pairs
        ]
        assembly_ids = {row["assembly_id"] for row in assemblies}
        annotations = [
            row for row in annotations
            if row["assembly_id"] in assembly_ids
            and ("annotation", row["annotation_id"]) not in retired_pairs
        ]

    sample_ids = [row["sample_id"] for row in samples]
    assembly_ids = [row["assembly_id"] for row in assemblies]
    entity_ids = [organism_id, *sample_ids, *[row["run_id"] for row in runs], *assembly_ids,
                  *[row["annotation_id"] for row in annotations]]
    if entity_ids:
        placeholders = ", ".join("?" for _ in entity_ids)
        accessions = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM accessions WHERE internal_id IN ({placeholders}) ORDER BY internal_type, internal_id, namespace",
            entity_ids,
        ).fetchall()]
        files = [dict(row) for row in db.conn.execute(
            f"SELECT file_id, entity_type, entity_id, file_role, format, size_bytes, sha256, status, relative_path "
            f"FROM files WHERE entity_id IN ({placeholders}) ORDER BY entity_type, entity_id, file_role",
            entity_ids,
        ).fetchall()]
    else:
        accessions, files = [], []
    source_object_ids = [*entity_ids, *[row["file_id"] for row in files]]
    if source_object_ids:
        placeholders = ", ".join("?" for _ in source_object_ids)
        source_links = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM source_links WHERE object_id IN ({placeholders}) "
            "ORDER BY source_id, object_type, object_id",
            source_object_ids,
        ).fetchall()]
    else:
        source_links = []
    source_ids = sorted({row["source_id"] for row in source_links})
    if source_ids:
        placeholders = ", ".join("?" for _ in source_ids)
        sources = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM data_sources WHERE source_id IN ({placeholders}) ORDER BY source_id",
            source_ids,
        ).fetchall()]
    else:
        sources = []
    return {
        "query": identifier,
        "scope": scope,
        "include_superseded": include_superseded,
        "include_retired": include_retired,
        "matched": {"entity_type": matched_type, "entity_id": matched_id},
        "organism": organism,
        "samples": samples,
        "runs": runs,
        "assemblies": assemblies,
        "annotations": annotations,
        "accessions": accessions,
        "files": files,
        "sources": sources,
        "source_links": source_links,
        "supersessions": supersessions,
        "retirements": retirements,
    }


def organism_graph(db: Database, identifier: str) -> dict[str, Any]:
    """Return the complete owning-organism graph (legacy public API)."""
    return entity_graph(
        db, identifier, scope="organism", include_superseded=True,
        include_retired=True,
    )
