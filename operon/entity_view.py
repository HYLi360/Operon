"""High-level entity graph lookup by internal ID or external accession."""

from __future__ import annotations

import re
from typing import Any

from operon.database import Database
from operon.errors import EntityNotFoundError, ValidationError

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


def sequence_hits(db: Database, identifier: str) -> list[dict[str, Any]]:
    """Return every sequences-table row whose seqid matches ``identifier``."""
    rows = db.conn.execute(
        """
        SELECT s.seqid, s.length, s.entity_type, s.entity_id, s.file_id, f.relative_path
        FROM sequences s
        JOIN files f ON f.file_id = s.file_id
        WHERE s.seqid=?
        ORDER BY s.entity_type, s.entity_id, s.file_id
        """,
        (identifier.strip(),),
    ).fetchall()
    return [dict(row) for row in rows]


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


def _resolve_match(
        db: Database,
        identifier: str,
        scope: str,
        include_retired: bool,
) -> tuple[str, str]:
    """Validate the scope, resolve the identifier, and enforce the retirement gate."""
    if scope not in {"matched", "organism"}:
        raise ValidationError(f"unknown entity graph scope {scope!r}")
    matched_type, matched_id = resolve_identifier(db, identifier)
    matched_retirements = (
        [] if include_retired
        else db.effective_retirements(matched_type, matched_id)
    )
    if matched_retirements:
        roots = ", ".join(
            f"{row['retired_by_type']} {row['retired_by_id']}"
            for row in matched_retirements
        )
        raise ValidationError(
            f"{matched_type} {matched_id} is retired by {roots}; "
            "use --include-retired to inspect retired metadata and files"
        )
    return matched_type, matched_id


def _fetch_organism_graph(
        db: Database,
        matched_type: str,
        matched_id: str,
        graph: dict[str, Any],
) -> str:
    """Resolve the owning organism and fetch its full descendant lists."""
    organism_id = _organism_for(db, matched_type, matched_id)
    organism_row = db.conn.execute(
        "SELECT * FROM organisms WHERE organism_id=?", (organism_id,)
    ).fetchone()
    if organism_row is None:
        raise EntityNotFoundError(
            f"{matched_type} {matched_id} refers to missing organism {organism_id}"
        )
    graph["organism"] = dict(organism_row)
    graph["samples"] = [dict(row) for row in db.conn.execute(
        "SELECT * FROM samples WHERE organism_id=? ORDER BY sample_id", (organism_id,)
    ).fetchall()]
    sample_ids = [row["sample_id"] for row in graph["samples"]]
    if sample_ids:
        placeholders = ", ".join("?" for _ in sample_ids)
        graph["runs"] = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM runs WHERE sample_id IN ({placeholders}) ORDER BY sample_id, run_id", sample_ids  # nosec B608 # fixed SQL fragments and generated placeholders; values are bound
        ).fetchall()]
        graph["assemblies"] = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM assemblies WHERE sample_id IN ({placeholders}) ORDER BY sample_id, assembly_id", sample_ids  # nosec B608 # fixed SQL fragments and generated placeholders; values are bound
        ).fetchall()]
    else:
        graph["runs"], graph["assemblies"] = [], []
    assembly_ids = [row["assembly_id"] for row in graph["assemblies"]]
    if assembly_ids:
        placeholders = ", ".join("?" for _ in assembly_ids)
        graph["annotations"] = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM annotations WHERE assembly_id IN ({placeholders}) ORDER BY assembly_id, annotation_id",  # nosec B608 # fixed SQL fragments and generated placeholders; values are bound
            assembly_ids,
        ).fetchall()]
    else:
        graph["annotations"] = []
    return organism_id


def _apply_matched_scope(
        matched_type: str,
        matched_id: str,
        graph: dict[str, Any],
) -> None:
    """Prune the organism-wide lists down to the matched lineage (in place)."""
    if matched_type == "organism":
        return
    samples = graph["samples"]
    runs = graph["runs"]
    assemblies = graph["assemblies"]
    annotations = graph["annotations"]
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
    graph["samples"] = [row for row in samples if row["sample_id"] in selected_sample_ids]
    graph["runs"] = [row for row in runs if row["run_id"] in selected_run_ids]
    graph["assemblies"] = [
        row for row in assemblies if row["assembly_id"] in selected_assembly_ids
    ]
    graph["annotations"] = [
        row for row in annotations if row["annotation_id"] in selected_annotation_ids
    ]


def _candidate_pairs(organism_id: str, graph: dict[str, Any]) -> list[tuple[str, str]]:
    """Build the (type, id) candidate pairs for supersession/retirement lookups."""
    return [
        ("organism", organism_id),
        *(('sample', row["sample_id"]) for row in graph["samples"]),
        *(('run', row["run_id"]) for row in graph["runs"]),
        *(('assembly', row["assembly_id"]) for row in graph["assemblies"]),
        *(('annotation', row["annotation_id"]) for row in graph["annotations"]),
    ]


def _fetch_supersessions(
        db: Database,
        candidate_pairs: list[tuple[str, str]],
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """Fetch supersession candidates and keep rows matching a candidate pair."""
    candidate_ids = [object_id for _object_type, object_id in candidate_pairs]
    if candidate_ids:
        placeholders = ", ".join("?" for _ in candidate_ids)
        supersessions = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM entity_supersessions WHERE object_id IN ({placeholders}) "
            "ORDER BY object_type, object_id",  # nosec B608 # fixed SQL fragments and generated placeholders; values are bound
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
    return supersessions, superseded_pairs


def _fetch_retirements(
        db: Database,
        candidate_pairs: list[tuple[str, str]],
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """Fetch effective-retirement candidates and keep rows matching a candidate pair."""
    retirement_ids = [object_id for _object_type, object_id in candidate_pairs]
    if retirement_ids and db.lifecycle_schema_available():
        placeholders = ", ".join("?" for _ in retirement_ids)
        retirements = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM effective_retired_entities "
            f"WHERE entity_id IN ({placeholders}) "
            "ORDER BY entity_type, entity_id, event_id",  # nosec B608 # fixed SQL fragments and generated placeholders; values are bound
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
    return retirements, retired_pairs


def _drop_excluded(
        graph: dict[str, Any],
        excluded: set[tuple[str, str]],
        *,
        keep: tuple[str, str] | None,
) -> None:
    """Cascade-prune the entity lists, dropping pairs present in ``excluded``.

    ``keep`` exempts exactly one (type, id) pair: supersession pruning passes
    the matched entity, which stays visible even when superseded.  Retirement
    pruning passes ``keep=None`` so no pair is exempt.
    """
    def current(row: dict[str, Any], entity_type: str, id_column: str) -> bool:
        pair = (entity_type, row[id_column])
        return (keep is not None and pair == keep) or pair not in excluded

    samples = [row for row in graph["samples"] if current(row, "sample", "sample_id")]
    graph["samples"] = samples
    sample_ids = {row["sample_id"] for row in samples}
    runs = [
        row for row in graph["runs"]
        if row["sample_id"] in sample_ids and current(row, "run", "run_id")
    ]
    graph["runs"] = runs
    assemblies = [
        row for row in graph["assemblies"]
        if row["sample_id"] in sample_ids and current(row, "assembly", "assembly_id")
    ]
    graph["assemblies"] = assemblies
    assembly_ids = {row["assembly_id"] for row in assemblies}
    graph["annotations"] = [
        row for row in graph["annotations"]
        if row["assembly_id"] in assembly_ids and current(row, "annotation", "annotation_id")
    ]


def _fetch_accessions_and_files(
        db: Database,
        organism_id: str,
        graph: dict[str, Any],
) -> list[str]:
    """Fetch accessions and files for the pruned entity lists.

    The id set is recomputed from the pruned entity lists and is kept
    separate from the candidate pair ids on purpose.
    """
    sample_ids = [row["sample_id"] for row in graph["samples"]]
    assembly_ids = [row["assembly_id"] for row in graph["assemblies"]]
    entity_ids = [organism_id, *sample_ids, *[row["run_id"] for row in graph["runs"]], *assembly_ids,
                  *[row["annotation_id"] for row in graph["annotations"]]]
    if entity_ids:
        placeholders = ", ".join("?" for _ in entity_ids)
        graph["accessions"] = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM accessions WHERE internal_id IN ({placeholders}) ORDER BY internal_type, internal_id, namespace",  # nosec B608 # fixed SQL fragments and generated placeholders; values are bound
            entity_ids,
        ).fetchall()]
        graph["files"] = [dict(row) for row in db.conn.execute(
            f"SELECT file_id, entity_type, entity_id, file_role, format, size_bytes, sha256, status, relative_path "
            f"FROM files WHERE entity_id IN ({placeholders}) ORDER BY entity_type, entity_id, file_role",  # nosec B608 # fixed SQL fragments and generated placeholders; values are bound
            entity_ids,
        ).fetchall()]
    else:
        graph["accessions"], graph["files"] = [], []
    return entity_ids


def _fetch_sources(
        db: Database,
        entity_ids: list[str],
        graph: dict[str, Any],
) -> None:
    """Fetch source links for the entities and their files, then the sources."""
    source_object_ids = [*entity_ids, *[row["file_id"] for row in graph["files"]]]
    if source_object_ids:
        placeholders = ", ".join("?" for _ in source_object_ids)
        source_links = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM source_links WHERE object_id IN ({placeholders}) "
            "ORDER BY source_id, object_type, object_id",  # nosec B608 # fixed SQL fragments and generated placeholders; values are bound
            source_object_ids,
        ).fetchall()]
    else:
        source_links = []
    source_ids = sorted({row["source_id"] for row in source_links})
    if source_ids:
        placeholders = ", ".join("?" for _ in source_ids)
        sources = [dict(row) for row in db.conn.execute(
            f"SELECT * FROM data_sources WHERE source_id IN ({placeholders}) ORDER BY source_id",  # nosec B608 # fixed SQL fragments and generated placeholders; values are bound
            source_ids,
        ).fetchall()]
    else:
        sources = []
    graph["sources"] = sources
    graph["source_links"] = source_links


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
    matched_type, matched_id = _resolve_match(db, identifier, scope, include_retired)
    graph: dict[str, Any] = {
        "query": identifier,
        "scope": scope,
        "include_superseded": include_superseded,
        "include_retired": include_retired,
        "matched": {"entity_type": matched_type, "entity_id": matched_id},
    }
    organism_id = _fetch_organism_graph(db, matched_type, matched_id, graph)
    if scope == "matched":
        _apply_matched_scope(matched_type, matched_id, graph)
    candidate_pairs = _candidate_pairs(organism_id, graph)
    supersessions, superseded_pairs = _fetch_supersessions(db, candidate_pairs)
    if not include_superseded:
        _drop_excluded(graph, superseded_pairs, keep=(matched_type, matched_id))
    retirements, retired_pairs = _fetch_retirements(db, candidate_pairs)
    if not include_retired:
        _drop_excluded(graph, retired_pairs, keep=None)
    entity_ids = _fetch_accessions_and_files(db, organism_id, graph)
    _fetch_sources(db, entity_ids, graph)
    graph["supersessions"] = supersessions
    graph["retirements"] = retirements
    return graph


def organism_graph(db: Database, identifier: str) -> dict[str, Any]:
    """Return the complete owning-organism graph (legacy public API)."""
    return entity_graph(
        db, identifier, scope="organism", include_superseded=True,
        include_retired=True,
    )
