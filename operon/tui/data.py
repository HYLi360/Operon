"""Read-only data access for the Operon TUI.

Every public function opens its own short-lived read-only ``Database``
connection, fetches plain dicts, and closes it again.  Connections are never
shared or kept open: the UI may call these functions from Textual worker
threads, and no SQLite lock is ever held while the UI idles, so concurrent
CLI writers in WAL mode are never blocked by the TUI.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError

ENTITY_TABLES: dict[str, tuple[str, str]] = {
    "organism": ("organisms", "organism_id"),
    "sample": ("samples", "sample_id"),
    "run": ("runs", "run_id"),
    "assembly": ("assemblies", "assembly_id"),
    "annotation": ("annotations", "annotation_id"),
}

ENTITY_TYPES = list(ENTITY_TABLES)

HEALTHY_FILE_STATUSES = frozenset({"CHECKSUM_VERIFIED", "STANDARDIZED"})
ATTENTION_RUN_STATUSES = frozenset({"failed", "interrupted"})
ATTENTION_DECISIONS = frozenset({"REVIEW", "FAIL"})

_ENTITY_NAMES = {
    "organism": "scientific_name",
    "sample": "strain",
    "run": "run_accession",
    "assembly": "assembly_accession",
    "annotation": "annotation_source",
}


@contextmanager
def _open(project: Project) -> Iterator[Database]:
    db = Database(project.db_path, read_only=True)
    try:
        yield db
    finally:
        db.close()


def _rows(db: Database, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db.query(sql, params)]


def _row(db: Database, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    rows = _rows(db, sql, params)
    return rows[0] if rows else None


def _effective_decision(row: dict[str, Any]) -> str:
    return str(row.get("curated_decision") or row.get("decision") or "")


def project_summary(project: Project) -> dict[str, Any]:
    """Return headline counts for the Home dashboard."""
    with _open(project) as db:
        entity_counts = {
            entity_type: int(
                _row(db, f"SELECT COUNT(*) AS n FROM {table}")["n"]  # noqa: S608 - fixed DDL names
            )
            for entity_type, (table, _id_col) in ENTITY_TABLES.items()
        }
        files_row = _row(db, "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes FROM files")
        decision_rows = _rows(
            db,
            "SELECT COALESCE(curated_decision, decision) AS effective, COUNT(*) AS n "
            "FROM current_decisions GROUP BY effective",
        )
        latest_release = _row(
            db, "SELECT version, created_at, profile, summary FROM releases "
                "ORDER BY created_at DESC LIMIT 1"
        )
    return {
        "entity_counts": entity_counts,
        "file_count": int(files_row["n"]) if files_row else 0,
        "file_bytes": int(files_row["bytes"]) if files_row else 0,
        "decision_counts": {str(r["effective"]): int(r["n"]) for r in decision_rows},
        "latest_release": latest_release,
    }


def attention_items(project: Project, *, limit: int = 10) -> dict[str, Any]:
    """Return the items a curator should look at first."""
    with _open(project) as db:
        run_rows = _rows(
            db,
            "SELECT run_id, step, status, entity_type, entity_id, started_at, error "
            "FROM workflow_runs WHERE status IN ('failed', 'interrupted') "
            "ORDER BY julianday(started_at) DESC LIMIT ?",
            (limit,),
        )
        run_count = int(_row(
            db, "SELECT COUNT(*) AS n FROM workflow_runs WHERE status IN ('failed', 'interrupted')"
        )["n"])
        decision_rows = [
            row for row in _rows(
                db,
                "SELECT entity_type, entity_id, profile, decision, curated_decision, "
                "reason_codes, evaluated_at FROM current_decisions",
            )
            if _effective_decision(row) in ATTENTION_DECISIONS
        ]
        file_rows = _rows(
            db,
            "SELECT file_id, entity_type, entity_id, file_role, relative_path, status "
            f"FROM files WHERE status NOT IN ({', '.join('?' for _ in HEALTHY_FILE_STATUSES)}) "
            "ORDER BY file_id LIMIT ?",
            (*sorted(HEALTHY_FILE_STATUSES), limit),
        )
    return {
        "failed_run_count": run_count,
        "runs": run_rows,
        "decisions": decision_rows,
        "files": file_rows,
    }


def _retired_keys(db: Database) -> set[tuple[str, str]]:
    if not db.lifecycle_schema_available():
        return set()
    return {
        (str(row["entity_type"]), str(row["entity_id"]))
        for row in db.query("SELECT entity_type, entity_id FROM effective_retired_entities")
    }


def _states(db: Database) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(row["entity_type"]), str(row["entity_id"])): dict(row)
        for row in db.query("SELECT entity_type, entity_id, state, message, updated_at FROM entity_state")
    }


def entity_tree(project: Project, *, include_retired: bool = False) -> list[dict[str, Any]]:
    """Return organisms → samples → runs/assemblies → annotations as nested dicts.

    Each node has ``entity_type``, ``entity_id``, ``name`` (a human label field
    when one exists), ``state``, ``retired``, and ``children``.  Retired
    entities are omitted unless ``include_retired`` is set, in which case they
    are kept with ``retired=True`` so the UI can dim them.
    """
    with _open(project) as db:
        retired = _retired_keys(db)
        states = _states(db)

        def node(entity_type: str, record: dict[str, Any], children: list[dict[str, Any]]) -> dict[str, Any]:
            entity_id = str(record.get(ENTITY_TABLES[entity_type][1]) or "")
            state_row = states.get((entity_type, entity_id))
            return {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "name": record.get(_ENTITY_NAMES[entity_type]),
                "state": state_row["state"] if state_row else None,
                "retired": (entity_type, entity_id) in retired,
                "children": children,
            }

        def visible(item: dict[str, Any]) -> bool:
            return include_retired or not item["retired"]

        annotations_by_assembly: dict[str, list[dict[str, Any]]] = {}
        for record in _rows(db, "SELECT * FROM annotations ORDER BY annotation_id"):
            child = node("annotation", record, [])
            if visible(child):
                annotations_by_assembly.setdefault(str(record["assembly_id"]), []).append(child)

        runs_by_sample: dict[str, list[dict[str, Any]]] = {}
        for record in _rows(db, "SELECT * FROM runs ORDER BY run_id"):
            child = node("run", record, [])
            if visible(child):
                runs_by_sample.setdefault(str(record["sample_id"]), []).append(child)

        assemblies_by_sample: dict[str, list[dict[str, Any]]] = {}
        for record in _rows(db, "SELECT * FROM assemblies ORDER BY assembly_id"):
            child = node("assembly", record, annotations_by_assembly.get(str(record["assembly_id"]), []))
            if visible(child):
                assemblies_by_sample.setdefault(str(record["sample_id"]), []).append(child)

        samples_by_organism: dict[str, list[dict[str, Any]]] = {}
        for record in _rows(db, "SELECT * FROM samples ORDER BY sample_id"):
            sample_id = str(record["sample_id"])
            child = node(
                "sample", record,
                runs_by_sample.get(sample_id, []) + assemblies_by_sample.get(sample_id, []),
            )
            if visible(child):
                samples_by_organism.setdefault(str(record["organism_id"]), []).append(child)

        tree = []
        for record in _rows(db, "SELECT * FROM organisms ORDER BY organism_id"):
            child = node("organism", record, samples_by_organism.get(str(record["organism_id"]), []))
            if visible(child):
                tree.append(child)
        return tree


def _entity_metrics(db: Database, entity_type: str, entity_id: str) -> dict[str, Any]:
    """Latest QC metrics per (stage, metric, tool) plus external analysis metrics."""
    qc_rows = _rows(
        db,
        "SELECT qc_stage, metric_name, metric_value, metric_unit, tool, tool_version, "
        "evaluated_at, qc_result_id FROM qc_results "
        "WHERE entity_type=? AND entity_id=? "
        "ORDER BY qc_stage, metric_name, tool, julianday(evaluated_at) DESC, qc_result_id DESC",
        (entity_type, entity_id),
    )
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in qc_rows:
        key = (row["qc_stage"], row["metric_name"], row["tool"])
        if key not in latest:
            del row["evaluated_at"], row["qc_result_id"]
            latest[key] = row
    analysis = _rows(
        db,
        "SELECT analysis_name, metric_name, metric_value, metric_unit FROM analysis_results "
        "WHERE entity_type=? AND entity_id=? ORDER BY analysis_name, metric_name",
        (entity_type, entity_id),
    )
    return {"qc": list(latest.values()), "analysis": analysis}


def entity_metrics(project: Project, entity_type: str, entity_id: str) -> dict[str, Any]:
    """Return the latest built-in QC and external-analysis metrics for one entity.

    ``"qc"`` holds one row per ``(qc_stage, metric_name, tool)`` — the newest
    measurement when several input identities recorded the same metric —
    ordered by stage and metric; ``"analysis"`` holds synced external results
    (BUSCO/QUAST-style) ordered by analysis name and metric.
    """
    with _open(project) as db:
        return _entity_metrics(db, entity_type, entity_id)


def entity_detail(project: Project, entity_type: str, entity_id: str) -> dict[str, Any] | None:
    """Return one entity's row, accessions, state, and files."""
    table, id_column = ENTITY_TABLES[entity_type]
    with _open(project) as db:
        fields = _row(db, f"SELECT * FROM {table} WHERE {id_column}=?", (entity_id,))  # noqa: S608
        if fields is None:
            return None
        accessions = _rows(
            db,
            "SELECT namespace, accession, version, is_primary FROM accessions "
            "WHERE internal_type=? AND internal_id=? ORDER BY namespace, accession",
            (entity_type, entity_id),
        )
        state = _row(
            db,
            "SELECT state, message, updated_at FROM entity_state WHERE entity_type=? AND entity_id=?",
            (entity_type, entity_id),
        )
        files = _rows(
            db,
            "SELECT file_id, file_role, format, compression, relative_path, size_bytes, sha256, status "
            "FROM files WHERE entity_type=? AND entity_id=? ORDER BY file_id",
            (entity_type, entity_id),
        )
        metrics = _entity_metrics(db, entity_type, entity_id)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "fields": fields,
        "accessions": accessions,
        "state": state,
        "files": files,
        "metrics": metrics,
    }


def list_files(
        project: Project,
        *,
        status: str | None = None,
        text: str = "",
        entity: str = "",
        limit: int = 0,
) -> list[dict[str, Any]]:
    """Return manifest files with an aggregated residency summary.

    ``text`` is a case-insensitive substring over file_id and relative_path;
    ``entity`` is a substring over entity_type/entity_id.  ``limit=0`` means
    no row limit.
    """
    conditions: list[str] = []
    params: list[Any] = []
    if status:
        conditions.append("f.status=?")
        params.append(status)
    if text:
        conditions.append("(f.file_id LIKE ? OR f.relative_path LIKE ?)")
        params.extend((f"%{text}%", f"%{text}%"))
    if entity:
        conditions.append("(f.entity_type LIKE ? OR f.entity_id LIKE ?)")
        params.extend((f"%{entity}%", f"%{entity}%"))
    sql = (
        "SELECT f.*, "
        "(SELECT GROUP_CONCAT(location_name || ':' || status, ', ') "
        " FROM file_locations fl WHERE fl.file_id=f.file_id) AS locations "
        "FROM files f"
    )
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY f.file_id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with _open(project) as db:
        return _rows(db, sql, params)


def file_statuses(project: Project) -> list[str]:
    """Return the distinct file statuses present, for filter selectors."""
    with _open(project) as db:
        return [str(row["status"]) for row in db.query("SELECT DISTINCT status FROM files ORDER BY status")]


def file_detail(project: Project, file_id: str) -> dict[str, Any] | None:
    """Return one manifest file and its residency records."""
    with _open(project) as db:
        record = _row(db, "SELECT * FROM files WHERE file_id=?", (file_id,))
        if record is None:
            return None
        locations = _rows(
            db,
            "SELECT location_name, location_type, uri, relative_path, sha256, size_bytes, "
            "status, verified_at FROM file_locations WHERE file_id=? ORDER BY location_name",
            (file_id,),
        )
    return {"file": record, "locations": locations}


def list_decisions(
        project: Project,
        *,
        profile: str | None = None,
        decision: str | None = None,
        text: str = "",
        limit: int = 500,
) -> list[dict[str, Any]]:
    """Return rows from the ``current_decisions`` view.

    The effective decision is ``COALESCE(curated_decision, decision)``; the
    ``decision`` filter matches that effective value.  ``text`` is a
    case-insensitive substring over entity_type/entity_id.
    """
    conditions: list[str] = []
    params: list[Any] = []
    if profile:
        conditions.append("profile=?")
        params.append(profile)
    if decision:
        conditions.append("COALESCE(curated_decision, decision)=?")
        params.append(decision)
    if text:
        conditions.append("(entity_type LIKE ? OR entity_id LIKE ?)")
        params.extend((f"%{text}%", f"%{text}%"))
    sql = (
        "SELECT entity_type, entity_id, profile, decision, curated_decision, "
        "curated_by, curated_reason, curated_at, reason_codes, evaluated_at "
        "FROM current_decisions"
    )
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY entity_type, entity_id, profile"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with _open(project) as db:
        return _rows(db, sql, params)


def list_profiles(project: Project) -> list[str]:
    """Return profile names from recorded decisions and the profiles directory."""
    with _open(project) as db:
        names = {
            str(row["profile"])
            for row in db.query("SELECT DISTINCT profile FROM current_decisions")
        }
    for pattern in ("*.yaml", "*.yml"):
        names.update(path.stem for path in project.profiles_dir.glob(pattern))
    return sorted(names)


def list_qc_profiles(project: Project) -> list[dict[str, Any]]:
    """Return the on-disk ``kind: qc`` profiles (name, version, description)."""
    from operon.profiles import load_profiles

    profiles = load_profiles(project.profiles_dir, kind="qc")
    return [
        {
            "name": name,
            "version": int(doc.get("version", 1)),
            "description": str(doc.get("description", "")),
        }
        for name, doc in sorted(profiles.items())
    ]


def get_profile_document(project: Project, name: str) -> dict[str, Any]:
    """Return the current on-disk qc profile document for the editor."""
    from operon.profiles import load_profile

    return load_profile(project.profiles_dir, name, expected_kind="qc")


def profile_history(project: Project, name: str) -> list[dict[str, Any]]:
    """Return the recorded snapshot history of one profile (CLI ``profiles history``)."""
    with _open(project) as db:
        return _rows(
            db,
            "SELECT s.profile_snapshot_id AS snapshot_id, s.profile_version AS version, "
            "s.profile_sha256 AS sha256, s.recorded_at, "
            "(SELECT COUNT(*) FROM decisions d WHERE d.profile_snapshot_id = s.profile_snapshot_id) "
            "AS uses FROM qc_profiles s WHERE s.profile_name=? ORDER BY s.profile_snapshot_id",
            (name,),
        )


def get_profile_snapshot(project: Project, name: str, snapshot_id: int) -> dict[str, Any]:
    """Return the parsed document of one recorded profile snapshot."""
    with _open(project) as db:
        row = _row(
            db,
            "SELECT profile_document FROM qc_profiles "
            "WHERE profile_name=? AND profile_snapshot_id=?",
            (name, snapshot_id),
        )
    if row is None:
        raise ValidationError(f"no snapshot recorded for profile {name!r} with snapshot id {snapshot_id}")
    return json.loads(str(row["profile_document"]))


def list_tools(project: Project) -> list[dict[str, Any]]:
    """Return the configured external tools (no version probing)."""
    from operon.tools import get_tool, load_tools_config

    config = load_tools_config(project)
    rows: list[dict[str, Any]] = []
    for tool_name in config.get("tools", {}):
        try:
            tool = get_tool(project, str(tool_name))
            rows.append({
                "name": tool.name,
                "executable": tool.executable,
                "run_method": tool.run_method,
                "description": tool.description,
                "recipes": sorted(tool.recipes),
            })
        except ValidationError as exc:
            rows.append({
                "name": str(tool_name),
                "executable": "",
                "run_method": "",
                "description": f"invalid: {exc}",
                "recipes": [],
            })
    return rows


def list_recipes(project: Project) -> list[dict[str, Any]]:
    """Return one summary row per recipe (CLI ``recipes list``)."""
    from operon.tools import list_analyses

    return [
        {
            "name": recipe.name,
            "version": recipe.version,
            "tool": recipe.tool_name,
            "entity_type": recipe.entity_type or "*",
            "file_role": recipe.file_role,
            "format": recipe.fmt,
        }
        for recipe in list_analyses(project)
    ]


def get_recipe_document(project: Project, name: str) -> dict[str, Any]:
    """Return ``{"tool": ..., "document": raw recipe mapping}`` for the editor."""
    from operon.tools import get_recipe

    recipe = get_recipe(project, name)
    return {"tool": recipe.tool_name, "document": dict(recipe.raw)}


def recipe_history(project: Project, name: str) -> list[dict[str, Any]]:
    """Return the recorded snapshot history of one recipe (CLI ``recipes history``)."""
    with _open(project) as db:
        return _rows(
            db,
            "SELECT s.recipe_snapshot_id AS snapshot_id, s.recipe_version AS version, "
            "s.recipe_sha256 AS sha256, s.recorded_at, "
            "(SELECT COUNT(*) FROM analysis_jobs j WHERE j.recipe_snapshot_id = s.recipe_snapshot_id) "
            "AS uses FROM recipe_snapshots s WHERE s.recipe_name=? ORDER BY s.recipe_snapshot_id",
            (name,),
        )


def get_recipe_snapshot(project: Project, name: str, snapshot_id: int) -> dict[str, Any]:
    """Return the parsed document of one recorded recipe snapshot.

    The document has the same shape ``run_analysis`` records:
    ``{"recipe": <raw recipe mapping>, "tool": <raw tool mapping>}``.
    """
    with _open(project) as db:
        row = _row(
            db,
            "SELECT recipe_document FROM recipe_snapshots "
            "WHERE recipe_name=? AND recipe_snapshot_id=?",
            (name, snapshot_id),
        )
    if row is None:
        raise ValidationError(f"no snapshot recorded for recipe {name!r} with snapshot id {snapshot_id}")
    return json.loads(str(row["recipe_document"]))


def list_workflow_runs(
        project: Project,
        *,
        statuses: Iterable[str] = (),
        step: str = "",
        entity: str = "",
        limit: int = 100,
        offset: int = 0,
) -> list[dict[str, Any]]:
    """Return workflow runs, newest first.

    ``statuses`` uses exact matching (OR within the list); ``step`` and
    ``entity`` are case-insensitive substrings over ``step`` and
    ``entity_type``/``entity_id`` respectively.  With no substring filters
    this delegates to :func:`operon.workflow.list_runs`.
    """
    with _open(project) as db:
        if not step and not entity:
            from operon.workflow import list_runs
            return list_runs(db, statuses=list(statuses), limit=limit, offset=offset)
        conditions: list[str] = []
        params: list[Any] = []
        status_list = list(statuses)
        if status_list:
            conditions.append(f"status IN ({', '.join('?' for _ in status_list)})")
            params.extend(status_list)
        if step:
            conditions.append("step LIKE ?")
            params.append(f"%{step}%")
        if entity:
            conditions.append("(entity_type LIKE ? OR entity_id LIKE ?)")
            params.extend((f"%{entity}%", f"%{entity}%"))
        sql = "SELECT * FROM workflow_runs WHERE " + " AND ".join(conditions)
        sql += " ORDER BY julianday(started_at) DESC, rowid DESC"
        if limit:
            sql += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        return _rows(db, sql, params)


def workflow_run_detail(project: Project, run_id: str) -> dict[str, Any] | None:
    """Return one workflow run with ``execution_details`` JSON decoded."""
    from operon.workflow import get_run
    with _open(project) as db:
        record = get_run(db, run_id)
    if record is None:
        return None
    details = record.get("execution_details")
    if isinstance(details, str) and details:
        try:
            record["execution_details"] = json.loads(details)
        except json.JSONDecodeError:
            pass
    return record
