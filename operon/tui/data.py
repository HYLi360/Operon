"""Read-only data access for the Operon TUI.

Every public function opens its own short-lived read-only ``Database``
connection, fetches plain dicts, and closes it again.  Connections are never
shared or kept open: the UI may call these functions from Textual worker
threads, and no SQLite lock is ever held while the UI idles, so concurrent
CLI writers in WAL mode are never blocked by the TUI.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

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
                _row(db, f"SELECT COUNT(*) AS n FROM {table}")["n"]
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
            "ORDER BY file_id LIMIT ?",  # nosec B608 # fixed mappings and SQL fragments; filter values are bound
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
    """Latest measurement per metric name for built-in QC and external analyses."""
    qc_rows = _rows(
        db,
        "SELECT qc_stage, metric_name, metric_value, metric_unit, tool, tool_version, "
        "evaluated_at, qc_result_id FROM qc_results "
        "WHERE entity_type=? AND entity_id=? "
        "ORDER BY metric_name, julianday(evaluated_at) DESC, qc_result_id DESC",
        (entity_type, entity_id),
    )
    qc: dict[str, dict[str, Any]] = {}
    for row in qc_rows:
        if row["metric_name"] not in qc:
            del row["evaluated_at"], row["qc_result_id"]
            qc[row["metric_name"]] = row
    analysis_rows = _rows(
        db,
        "SELECT r.analysis_name, r.metric_name, r.metric_value, r.metric_unit, "
        "j.finished_at, r.result_id FROM analysis_results r "
        "JOIN analysis_jobs j ON j.job_id = r.job_id "
        "WHERE r.entity_type=? AND r.entity_id=? "
        "ORDER BY r.metric_name, julianday(j.finished_at) DESC, r.result_id DESC",
        (entity_type, entity_id),
    )
    analysis: dict[str, dict[str, Any]] = {}
    for row in analysis_rows:
        if row["metric_name"] not in analysis:
            del row["finished_at"], row["result_id"]
            analysis[row["metric_name"]] = row
    return {
        "qc": sorted(qc.values(), key=lambda row: (row["qc_stage"], row["metric_name"])),
        "analysis": sorted(analysis.values(), key=lambda row: (row["analysis_name"], row["metric_name"])),
    }


def entity_metrics(project: Project, entity_type: str, entity_id: str) -> dict[str, Any]:
    """Return the latest built-in QC and external-analysis metrics for one entity.

    ``"qc"`` holds one row per ``metric_name`` — the newest measurement,
    regardless of stage or tool — ordered by stage and metric; ``"analysis"``
    holds synced external results (BUSCO/QUAST-style) likewise collapsed to
    the newest measurement per ``metric_name`` and ordered by analysis name
    and metric.
    """
    with _open(project) as db:
        return _entity_metrics(db, entity_type, entity_id)


def entity_detail(project: Project, entity_type: str, entity_id: str) -> dict[str, Any] | None:
    """Return one entity's row, accessions, state, and files."""
    table, id_column = ENTITY_TABLES[entity_type]
    with _open(project) as db:
        fields = _row(db, f"SELECT * FROM {table} WHERE {id_column}=?", (entity_id,))  # nosec B608 # fixed mappings and SQL fragments; filter values are bound
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


def _file_labels(db: Database, file_id: str, limit: int = 500) -> list[dict[str, Any]]:
    return _rows(
        db,
        "SELECT seqid, label, profile_name, decided_at FROM sequence_labels "
        "WHERE file_id=? ORDER BY label, seqid LIMIT ?",
        (file_id, int(limit)),
    )


def file_sequence_labels(
        project: Project,
        file_id: str,
        *,
        limit: int = 500,
) -> list[dict[str, Any]]:
    """Return one file's ``sequence_labels`` rows (``classify-sequences`` output)."""
    with _open(project) as db:
        return _file_labels(db, file_id, limit)


def file_detail(project: Project, file_id: str) -> dict[str, Any] | None:
    """Return one manifest file, its residency records, and its labels."""
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
        labels = _file_labels(db, file_id)
    return {"file": record, "locations": locations, "labels": labels}


ANALYSIS_HIT_COLUMNS = (
    "analysis_name", "entity_type", "entity_id", "query_id", "subject_id",
    "hit_rank", "query_start", "query_end", "subject_start", "subject_end",
    "evalue", "bitscore", "percent_identity",
)


def analysis_hits(
        project: Project,
        *,
        analysis: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        query_id: str | None = None,
        subject_id: str | None = None,
        evalue_max: float | None = None,
        limit: int = 20,
        include_retired: bool = False,
) -> list[dict[str, Any]]:
    """Return alignment-hit rows exactly like ``report analysis --hits``.

    Same joins, filters, ordering (``entity_id``, ``query_id``, ``hit_rank``)
    and retired-entity handling as the CLI's read-only query; the column set is
    :data:`ANALYSIS_HIT_COLUMNS`, so an exported file matches the CLI's.
    """
    sql = """
        SELECT a.analysis_name, a.entity_type, a.entity_id, a.query_id, a.subject_id,
               a.hit_rank, a.query_start, a.query_end, a.subject_start, a.subject_end,
               a.evalue, a.bitscore, a.percent_identity
        FROM analysis_alignments a
        JOIN analysis_jobs j ON j.job_id = a.job_id
        WHERE j.status='completed'
    """
    with _open(project) as db:
        if not include_retired and db.lifecycle_schema_available():
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM effective_retired_entities er "
                "WHERE er.entity_type=a.entity_type AND er.entity_id=a.entity_id)"
            )
        params: list[Any] = []
        if analysis is not None:
            sql += " AND a.analysis_name=?"
            params.append(analysis)
        if entity_type is not None:
            sql += " AND a.entity_type=?"
            params.append(entity_type)
        if entity_id is not None:
            sql += " AND a.entity_id=?"
            params.append(entity_id)
        if query_id is not None:
            sql += " AND a.query_id=?"
            params.append(query_id)
        if subject_id is not None:
            sql += " AND a.subject_id=?"
            params.append(subject_id)
        if evalue_max is not None:
            sql += " AND a.evalue IS NOT NULL AND a.evalue<=?"
            params.append(float(evalue_max))
        sql += " ORDER BY a.entity_id, a.query_id, a.hit_rank LIMIT ?"
        params.append(int(limit))
        return _rows(db, sql, params)


def label_summary(
        project: Project,
        *,
        profile_name: str | None = None,
        limit: int = 500,
) -> list[dict[str, Any]]:
    """Return ``label`` × how many sequences and files carry it.

    ``sequence_labels`` rows are grouped by label and profile (backed by
    ``idx_sequence_labels_label``); biggest label first.
    """
    sql = (
        "SELECT label, profile_name, COUNT(*) AS sequences, "
        "COUNT(DISTINCT file_id) AS files FROM sequence_labels"
    )
    params: list[Any] = []
    if profile_name is not None:
        sql += " WHERE profile_name=?"
        params.append(profile_name)
    sql += " GROUP BY label, profile_name ORDER BY sequences DESC, label LIMIT ?"
    params.append(int(limit))
    with _open(project) as db:
        return _rows(db, sql, params)


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


def config_version_floor(project: Project, kind: str, name: str, version: int = 0) -> int:
    """Highest known version, including snapshots of deleted/replaced files."""
    table, column, name_column = {
        "profile": ("qc_profiles", "profile_version", "profile_name"),
        "recipe": ("recipe_snapshots", "recipe_version", "recipe_name"),
    }[kind]
    with _open(project) as db:
        row = _row(db, f"SELECT MAX({column}) AS version FROM {table} WHERE {name_column}=?",  # nosec B608 # fixed mappings and SQL fragments; filter values are bound
                   (name,))
    return max(version, int(row["version"] or 0))


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


def normalize_workflow_time(value: str) -> str:
    """Normalize an ISO-8601 ``workflow list`` time bound like the CLI.

    ``Z`` suffixes become ``+00:00`` and naive values are interpreted in the
    local timezone, exactly like ``--from``/``--to`` on the command line
    (``operon.cli._workflow_time``); anything else raises ``ValidationError``
    with the CLI's message.
    """
    from datetime import datetime

    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValidationError("must be an ISO-8601 date or timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.isoformat()


def list_workflow_runs(
        project: Project,
        *,
        statuses: Iterable[str] = (),
        step: str = "",
        entity: str = "",
        limit: int = 100,
        offset: int = 0,
        started_from: str | None = None,
        started_to: str | None = None,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        resumes_run_id: str | None = None,
        tool: str | None = None,
        executor: str | None = None,
        oldest_first: bool = False,
) -> list[dict[str, Any]]:
    """Return workflow runs with the CLI's ``workflow list`` filters.

    Every filter except ``step`` and ``entity`` mirrors
    :func:`operon.workflow.list_runs` (the CLI's own query) and is delegated
    to it verbatim; ``limit=0`` means no row limit, ``offset``/``oldest_first``
    page and order exactly like ``workflow list``.  ``step`` and ``entity``
    are the TUI's case-insensitive substrings over ``step`` and
    ``entity_type``/``entity_id``; when either is set the same filters are
    applied in one read-only query next to the substring matches.  Time bounds
    accept the CLI's ISO-8601 forms (see :func:`normalize_workflow_time`).
    """
    from datetime import datetime

    if started_from is not None:
        started_from = normalize_workflow_time(started_from)
    if started_to is not None:
        started_to = normalize_workflow_time(started_to)
    if (started_from is not None and started_to is not None
            and datetime.fromisoformat(started_from) >= datetime.fromisoformat(started_to)):
        raise ValidationError("--from must be earlier than --to")
    with _open(project) as db:
        if not step and not entity:
            from operon.workflow import list_runs
            return list_runs(
                db,
                started_from=started_from, started_to=started_to, run_id=run_id,
                statuses=list(statuses), parent_run_id=parent_run_id,
                resumes_run_id=resumes_run_id, tool=tool, executor=executor,
                limit=limit, offset=offset, oldest_first=oldest_first,
            )
        conditions: list[str] = []
        params: list[Any] = []
        status_list = list(statuses)
        if status_list:
            conditions.append(f"status IN ({', '.join('?' for _ in status_list)})")
            params.extend(status_list)
        if started_from is not None:
            conditions.append("julianday(started_at) >= julianday(?)")
            params.append(started_from)
        if started_to is not None:
            conditions.append("julianday(started_at) < julianday(?)")
            params.append(started_to)
        if step:
            conditions.append("step LIKE ?")
            params.append(f"%{step}%")
        if entity:
            conditions.append("(entity_type LIKE ? OR entity_id LIKE ?)")
            params.extend((f"%{entity}%", f"%{entity}%"))
        for column, value in (("run_id", run_id), ("parent_run_id", parent_run_id),
                              ("resumes_run_id", resumes_run_id), ("tool", tool),
                              ("executor", executor)):
            if value is not None:
                conditions.append(f"{column}=?")  # nosec B608 # fixed column names; filter values are bound
                params.append(value)
        sql = "SELECT * FROM workflow_runs WHERE " + " AND ".join(conditions)  # nosec B608 # fixed mappings and SQL fragments; filter values are bound
        direction = "ASC" if oldest_first else "DESC"
        sql += f" ORDER BY julianday(started_at) {direction}, rowid {direction}"
        if limit:
            sql += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        return _rows(db, sql, params)


def workflow_run_status(project: Project, run_id: str) -> dict[str, Any] | None:
    """Return only the fields a log follower polls (no JSON decoding)."""
    from operon.workflow import get_run
    with _open(project) as db:
        record = get_run(db, run_id)
    if record is None:
        return None
    return {key: record.get(key) for key in ("run_id", "status", "exit_code", "finished_at")}


def list_environments(project: Project) -> list[dict[str, Any]]:
    """Return captured execution environments like ``operon environments list``.

    Ordered by ``created_at`` (then id), each row carrying the single-line
    ``environment_summary``; an unparseable stored document degrades to a
    ``"-"`` summary instead of failing the load, exactly like the CLI.
    """
    from operon.environment import environment_summary
    with _open(project) as db:
        rows = _rows(
            db,
            "SELECT environment_id, document, created_at FROM execution_environments "
            "ORDER BY created_at, environment_id",
        )
    for row in rows:
        try:
            row["summary"] = environment_summary(json.loads(row.pop("document"))) or "-"
        except json.JSONDecodeError:
            row["summary"] = "-"
    return rows


def environment_document(project: Project, environment_id: str) -> dict[str, Any]:
    """Return one captured environment document (unknown id raises like the CLI)."""
    with _open(project) as db:
        row = _row(
            db,
            "SELECT document FROM execution_environments WHERE environment_id=?",
            (environment_id,),
        )
    if row is None:
        raise ValidationError(f"unknown environment: {environment_id}")
    return json.loads(row["document"])


def export_environment(project: Project, environment_id: str, fmt: str = "explicit") -> str:
    """Render a Conda reconstruction spec like ``operon environments export``."""
    from operon.environment_capture import export_conda

    return export_conda(environment_document(project, environment_id), fmt)


def workflow_run_detail(project: Project, run_id: str) -> dict[str, Any] | None:
    """Return one workflow run with ``execution_details`` JSON decoded.

    When the run references an execution environment, its rendered summary
    is attached as ``environment_summary`` (None when missing or corrupt).
    """
    from operon.workflow import get_run
    with _open(project) as db:
        record = get_run(db, run_id)
        if record is None:
            return None
        summary = None
        environment_id = record.get("environment_id")
        if environment_id:
            row = db.conn.execute(
                "SELECT document FROM execution_environments WHERE environment_id=?",
                (environment_id,),
            ).fetchone()
            if row is not None:
                try:
                    from operon.environment import environment_summary
                    summary = environment_summary(json.loads(row["document"])) or None
                except json.JSONDecodeError:
                    pass
    record["environment_summary"] = summary
    details = record.get("execution_details")
    if isinstance(details, str) and details:
        try:
            record["execution_details"] = json.loads(details)
        except json.JSONDecodeError:
            pass
    return record


# The statuses `analysis_jobs` rows actually carry: RUNNING for an in-flight
# job (swept to `interrupted` on the next run after a crash), the rest set by
# the finalizing paths in tools.py.
ANALYSIS_JOB_STATUSES = ("RUNNING", "completed", "failed", "interrupted")


def list_analysis_jobs(
        project: Project,
        *,
        analysis: str = "",
        statuses: Iterable[str] = (),
        limit: int = 200,
) -> list[dict[str, Any]]:
    """Return analysis jobs, newest first, with their scheduler job id.

    Reads ``analysis_jobs`` directly rather than ``workflow_runs``: a task
    interrupted inside a job array keeps its job row and never gets a run
    row, so joining from runs would hide exactly the rows an operator needs
    to see.  ``analysis`` is a case-insensitive substring over
    ``analysis_name``; ``statuses`` uses exact matching (OR within the list).
    ``scheduler_job_id``/``executor`` come from the run row when one exists
    and are ``None`` otherwise.
    """
    conditions: list[str] = []
    params: list[Any] = []
    status_list = list(statuses)
    if analysis:
        conditions.append("j.analysis_name LIKE ?")
        params.append(f"%{analysis}%")
    if status_list:
        conditions.append(f"j.status IN ({', '.join('?' for _ in status_list)})")
        params.extend(status_list)
    sql = (
        "SELECT j.job_id, j.analysis_name, j.entity_type, j.entity_id, j.file_id, "
        "j.status, j.tool, j.tool_version, j.output_relative_path, j.output_sha256, "
        "j.stdout_file, j.stderr_file, j.started_at, j.finished_at, j.error, "
        "j.workflow_run_id, r.scheduler_job_id, r.executor "
        "FROM analysis_jobs j LEFT JOIN workflow_runs r ON r.run_id = j.workflow_run_id"
    )
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)  # nosec B608 # fixed condition fragments; filter values are bound
    sql += " ORDER BY j.job_id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    with _open(project) as db:
        return _rows(db, sql, params)


# ---------------------------------------------------------------------------
# Import wizard pickers (read-only mirrors of the questionary wizard queries)
# ---------------------------------------------------------------------------


def _not_retired(db: Database, entity_type: str, alias: str, id_column: str) -> str:
    if not db.lifecycle_schema_available():
        return ""
    return (
        f" AND NOT EXISTS (SELECT 1 FROM effective_retired_entities r "
        f"WHERE r.entity_type='{entity_type}' AND r.entity_id={alias}.{id_column})"  # nosec B608 # fixed mappings and SQL fragments; filter values are bound
    )


def list_organisms_for_picker(project: Project) -> list[dict[str, Any]]:
    """Return non-retired organisms for the import wizard's organism picker."""
    with _open(project) as db:
        return _rows(
            db,
            "SELECT organism_id, scientific_name, taxon_id, taxonomy_source, taxonomy_version "
            "FROM organisms o WHERE 1=1"
            + _not_retired(db, "organism", "o", "organism_id")  # nosec B608 # fixed mappings and SQL fragments; filter values are bound
            + " ORDER BY scientific_name, organism_id",
        )


def list_samples_for_picker(project: Project, organism_id: str) -> list[dict[str, Any]]:
    """Return non-retired samples of one organism for the sample picker."""
    with _open(project) as db:
        return _rows(
            db,
            "SELECT sample_id, isolate, strain, biosample_accession FROM samples s "
            "WHERE organism_id=?"
            + _not_retired(db, "sample", "s", "sample_id")  # nosec B608 # fixed mappings and SQL fragments; filter values are bound
            + " ORDER BY sample_id",
            (organism_id,),
        )


def list_assemblies_for_picker(project: Project, sample_id: str) -> list[dict[str, Any]]:
    """Return non-retired assemblies of one sample for the assembly picker."""
    with _open(project) as db:
        return _rows(
            db,
            "SELECT assembly_id, assembly_accession, assembly_name, assembly_version "
            "FROM assemblies a WHERE sample_id=?"
            + _not_retired(db, "assembly", "a", "assembly_id")  # nosec B608 # fixed mappings and SQL fragments; filter values are bound
            + " ORDER BY assembly_id",
            (sample_id,),
        )


def list_annotations_for_picker(project: Project, assembly_id: str) -> list[dict[str, Any]]:
    """Return non-retired annotations of one assembly for the annotation picker."""
    with _open(project) as db:
        return _rows(
            db,
            "SELECT annotation_id, annotation_source, annotation_version FROM annotations n "
            "WHERE assembly_id=?"
            + _not_retired(db, "annotation", "n", "annotation_id")  # nosec B608 # fixed mappings and SQL fragments; filter values are bound
            + " ORDER BY annotation_id",
            (assembly_id,),
        )


def import_summary(project: Project, draft: dict[str, Any]) -> str:
    """Render the import plan summary with exactly the wizard's own renderer."""
    from operon.import_wizard import _summary

    with _open(project) as db:
        return _summary(db, draft)


# ---------------------------------------------------------------------------
# Publish screen: releases, release preview, export preview
# ---------------------------------------------------------------------------


def list_releases(project: Project) -> list[dict[str, Any]]:
    """Return recorded releases (version, created_at, profile, decoded summary)."""
    with _open(project) as db:
        rows = _rows(
            db,
            "SELECT version, created_at, profile, path, manifest_sha256, summary "
            "FROM releases ORDER BY julianday(created_at) DESC, version DESC",
        )
    for row in rows:
        summary = row.get("summary")
        if isinstance(summary, str) and summary:
            try:
                row["summary"] = json.loads(summary)
            except json.JSONDecodeError:
                pass
    return rows


def release_preview(project: Project, profile: str) -> dict[str, Any]:
    """Return the included members and exclusions a release of ``profile`` would have."""
    from operon.release import release_exclusions_for, release_files_for

    with _open(project) as db:
        members = release_files_for(db, profile)
        exclusions = release_exclusions_for(db, profile)
    return {
        "members": members,
        "member_bytes": sum(int(row.get("size_bytes") or 0) for row in members),
        "exclusions": exclusions,
    }


def export_preview(
        project: Project,
        *,
        entity_type: str | None = None,
        entity_ids: Iterable[str] = (),
        file_ids: Iterable[str] = (),
        file_role: str | None = None,
        fmt: str | None = None,
        state: str | None = None,
        decision: str | None = None,
        profile: str | None = None,
) -> dict[str, Any]:
    """Count the files an export with these filters would materialize, without writing."""
    from operon.export import _select_files

    with _open(project) as db:
        rows = _select_files(
            db, entity_type=entity_type, entity_ids=entity_ids, file_ids=file_ids,
            file_role=file_role, fmt=fmt, state=state, decision=decision, profile=profile,
        )
    return {
        "count": len(rows),
        "bytes": sum(int(row.get("size_bytes") or 0) for row in rows),
        "files": [
            {
                "file_id": row["file_id"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "file_role": row["file_role"],
                "size_bytes": row["size_bytes"],
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# Coverage screen: taxonomy snapshots, reference sets, coverage reports
# ---------------------------------------------------------------------------


def list_taxonomy_snapshots(project: Project) -> list[dict[str, Any]]:
    """Return imported NCBI Taxonomy snapshots (CLI ``taxonomy list``)."""
    from operon.taxonomy import list_taxonomy_snapshots as _list

    with _open(project) as db:
        return _list(db)


def list_reference_sets(project: Project) -> list[dict[str, Any]]:
    """Return compiled taxonomy reference sets (CLI ``taxonomy reference-sets``)."""
    from operon.taxonomy import list_reference_sets as _list

    with _open(project) as db:
        return _list(db)


COVERAGE_TABLE_NAMES = (
    "coverage_summary",
    "coverage_targets",
    "coverage_missing",
    "coverage_observations",
    "coverage_excluded_observations",
)

COVERAGE_REPORT_LIMIT = 500


def list_coverage_reports(project: Project) -> list[dict[str, Any]]:
    """Return coverage report directories with their provenance headline.

    Reports are discovered on the filesystem (``reports/coverage/COV_*``), so
    the browser keeps working even for reports written before the
    ``coverage_reports`` table existed.
    """
    import re

    root = project.reports_root / "coverage"
    reports: list[dict[str, Any]] = []
    if not root.is_dir():
        return reports
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not re.fullmatch(r"COV_[0-9A-Fa-f]+", path.name):
            continue
        provenance_path = path / "provenance.json"
        provenance: dict[str, Any] = {}
        if provenance_path.is_file():
            try:
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                provenance = {}
        reports.append({
            "report_id": path.name,
            "path": str(path),
            "reference_set_id": provenance.get("reference_set_id", "?"),
            "scope_kind": provenance.get("scope_kind", "?"),
            "scope_value": provenance.get("scope_value"),
            "decision": provenance.get("decision", "?"),
            "created_at": provenance.get("created_at", "?"),
        })
    reports.sort(key=lambda row: str(row["created_at"]), reverse=True)
    return reports


def read_coverage_report(project: Project, report_id: str) -> dict[str, Any]:
    """Parse one coverage report's provenance and TSV tables (stdlib only).

    Each table is returned as ``{"columns": [...], "rows": [[...], ...]}``;
    rows are capped at :data:`COVERAGE_REPORT_LIMIT` with a ``truncated`` flag.
    """
    import csv
    import re

    if not re.fullmatch(r"COV_[0-9A-Fa-f]+", report_id):
        raise ValidationError(f"invalid coverage report id {report_id!r}")
    path = project.reports_root / "coverage" / report_id
    if not path.is_dir():
        raise ValidationError(f"coverage report not found: {report_id}")
    provenance: dict[str, Any] = {}
    provenance_path = path / "provenance.json"
    if provenance_path.is_file():
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            provenance = {}
    tables: dict[str, Any] = {}
    for name in COVERAGE_TABLE_NAMES:
        table_path = path / f"{name}.tsv"
        if not table_path.is_file():
            continue
        with open(table_path, newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle, delimiter="\t")
            columns = next(reader, [])
            if reader.line_num and not columns:
                raise ValidationError(f"{table_path.name}: line 1: missing TSV header")
            body = []
            total = 0
            for row in reader:
                if len(row) != len(columns):
                    raise ValidationError(
                        f"{table_path.name}: line {reader.line_num}: expected "
                        f"{len(columns)} fields, found {len(row)}"
                    )
                total += 1
                if total <= COVERAGE_REPORT_LIMIT:
                    body.append(row)
        tables[name] = {
            "columns": columns,
            "rows": body,
            "truncated": total > COVERAGE_REPORT_LIMIT,
            "total": total,
        }
    return {"report_id": report_id, "path": str(path), "provenance": provenance, "tables": tables}
