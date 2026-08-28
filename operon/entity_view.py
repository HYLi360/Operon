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


def organism_graph(db: Database, identifier: str) -> dict[str, Any]:
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
    }
