"""SQLite-backed file-based database.

SQLite was chosen deliberately: it is a single file, supports SQL constraints
and indexes, handles hundreds of thousands of metadata rows, and can be
rebuilt from the TSV exchange files.  Large sequence files never go inside the
database; only their manifest records, QC metrics and provenance do.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from operon.errors import ConflictError, EntityNotFoundError, ValidationError
from operon.schema import ENTITY_ID_COLUMNS, ENTITY_PREFIXES, ENTITY_TABLES, Schema

SCHEMA_VERSION = "2.4"

MANUAL_TABLES = [
    "organisms",
    "samples",
    "runs",
    "assemblies",
    "annotations",
    "accessions",
    "files",
]

DDL = """
CREATE TABLE IF NOT EXISTS organisms (
    organism_id TEXT PRIMARY KEY,
    scientific_name TEXT NOT NULL,
    taxon_id INTEGER,
    taxonomic_rank TEXT,
    taxonomy_source TEXT,
    taxonomy_version TEXT
);
CREATE TABLE IF NOT EXISTS samples (
    sample_id TEXT PRIMARY KEY,
    organism_id TEXT NOT NULL REFERENCES organisms(organism_id),
    biosample_accession TEXT,
    strain TEXT,
    isolate TEXT,
    cultivar TEXT,
    sex TEXT,
    tissue TEXT,
    tissue_normalized TEXT,
    tissue_ontology_id TEXT,
    collection_date TEXT,
    country TEXT,
    country_iso TEXT,
    latitude REAL,
    longitude REAL,
    host TEXT,
    environment_biome TEXT,
    source_record TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL REFERENCES samples(sample_id),
    run_accession TEXT,
    experiment_accession TEXT,
    library_strategy TEXT,
    library_source TEXT,
    library_layout TEXT,
    platform TEXT,
    instrument_model TEXT,
    read_length INTEGER,
    download_url TEXT
);
CREATE TABLE IF NOT EXISTS assemblies (
    assembly_id TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL REFERENCES samples(sample_id),
    assembly_accession TEXT,
    assembly_name TEXT,
    assembly_version INTEGER,
    assembly_level TEXT,
    assembly_method TEXT,
    submitter TEXT,
    release_date TEXT,
    reference_status TEXT,
    bioproject_accession TEXT,
    source_database TEXT,
    assembly_status TEXT,
    assembly_type TEXT,
    fasta_file_id TEXT
);
CREATE TABLE IF NOT EXISTS annotations (
    annotation_id TEXT PRIMARY KEY,
    assembly_id TEXT NOT NULL REFERENCES assemblies(assembly_id),
    annotation_source TEXT,
    annotation_version INTEGER,
    annotation_date TEXT,
    gff_file_id TEXT,
    cds_file_id TEXT,
    protein_file_id TEXT
);
CREATE TABLE IF NOT EXISTS accessions (
    internal_type TEXT NOT NULL,
    internal_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    accession TEXT NOT NULL,
    version TEXT,
    is_primary INTEGER,
    PRIMARY KEY (namespace, accession)
);
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    file_role TEXT NOT NULL,
    format TEXT NOT NULL,
    compression TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    source_url TEXT,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    downloaded_at TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qc_results (
    qc_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    file_id TEXT REFERENCES files(file_id),
    file_sha256 TEXT,
    input_identity TEXT NOT NULL,
    qc_stage TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value TEXT NOT NULL,
    metric_numeric REAL,
    metric_unit TEXT,
    tool TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    parameter_set TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    UNIQUE (input_identity, qc_stage, metric_name, tool, tool_version, parameter_set)
);
CREATE TABLE IF NOT EXISTS qc_profiles (
    profile_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    profile_sha256 TEXT NOT NULL,
    profile_document TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (profile_name, profile_version, profile_sha256)
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    profile TEXT NOT NULL,
    profile_version INTEGER,
    profile_snapshot_id INTEGER REFERENCES qc_profiles(profile_snapshot_id),
    profile_sha256 TEXT,
    decision TEXT NOT NULL,
    curated_decision TEXT,
    reason_codes TEXT NOT NULL,
    observed TEXT NOT NULL,
    thresholds TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    curated_by TEXT,
    curated_reason TEXT,
    curated_evidence TEXT,
    curated_at TEXT
);
CREATE TABLE IF NOT EXISTS entity_state (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    state TEXT NOT NULL,
    message TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    entity_type TEXT,
    entity_id TEXT,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    command TEXT,
    tool TEXT,
    tool_version TEXT,
    parameter_set TEXT,
    input_sha256 TEXT,
    output_sha256 TEXT,
    threads INTEGER,
    max_rss_mb REAL,
    log_file TEXT,
    stdout_file TEXT,
    stderr_file TEXT,
    executor TEXT,
    scheduler_job_id TEXT,
    execution_details TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS file_locations (
    file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
    location_name TEXT NOT NULL,
    location_type TEXT NOT NULL,
    uri TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    status TEXT NOT NULL,
    verified_at TEXT,
    PRIMARY KEY (file_id, location_name)
);
CREATE TABLE IF NOT EXISTS releases (
    version TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    profile TEXT,
    path TEXT NOT NULL,
    manifest_sha256 TEXT,
    summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_members (
    release_version TEXT NOT NULL REFERENCES releases(version),
    file_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    release_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    PRIMARY KEY (release_version, file_id)
);
CREATE TABLE IF NOT EXISTS changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    reason TEXT,
    evidence TEXT,
    actor TEXT,
    changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    file_id TEXT NOT NULL REFERENCES files(file_id),
    tool TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    tool_version_raw TEXT,
    launcher TEXT,
    command TEXT,
    parameter_set TEXT NOT NULL,
    parameter_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    database_identity TEXT NOT NULL,
    status TEXT NOT NULL,
    output_relative_path TEXT,
    output_sha256 TEXT,
    stdout_file TEXT,
    stderr_file TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT,
    workflow_run_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_jobs_completed_cache
    ON analysis_jobs(analysis_name, file_id, parameter_sha256, input_sha256, database_identity)
    WHERE status='completed';
CREATE TABLE IF NOT EXISTS analysis_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES analysis_jobs(job_id),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    analysis_name TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value TEXT NOT NULL,
    metric_numeric REAL,
    metric_unit TEXT
);
CREATE TABLE IF NOT EXISTS analysis_hits (
    hit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES analysis_jobs(job_id),
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    analysis_name TEXT NOT NULL,
    query_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value TEXT NOT NULL,
    metric_numeric REAL,
    metric_unit TEXT,
    hit_rank INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_jobs_entity ON analysis_jobs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_analysis_jobs_name ON analysis_jobs(analysis_name, status);
CREATE INDEX IF NOT EXISTS idx_analysis_hits_job ON analysis_hits(job_id, query_id, hit_rank);
CREATE INDEX IF NOT EXISTS idx_analysis_hits_query ON analysis_hits(analysis_name, query_id);
CREATE INDEX IF NOT EXISTS idx_files_entity ON files(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_accessions_internal ON accessions(internal_type, internal_id);
CREATE INDEX IF NOT EXISTS idx_qc_entity ON qc_results(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_qc_metric ON qc_results(metric_name);
CREATE INDEX IF NOT EXISTS idx_decisions_entity ON decisions(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_decisions_current ON decisions(entity_type, entity_id, profile, decision_id);
CREATE INDEX IF NOT EXISTS idx_workflow_entity ON workflow_runs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_file_locations_status ON file_locations(location_name, status);
"""

CURRENT_DECISIONS_VIEW = """
CREATE VIEW current_decisions AS
SELECT d.*
FROM decisions d
WHERE d.decision_id = (
    SELECT MAX(newer.decision_id)
    FROM decisions newer
    WHERE newer.entity_type=d.entity_type
      AND newer.entity_id=d.entity_id
      AND newer.profile=d.profile
)
"""

TAXONOMY_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS taxonomy_snapshots (
    taxonomy_snapshot_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL,
    source_file_id TEXT NOT NULL REFERENCES files(file_id),
    source_sha256 TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    node_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (source, taxonomy_version)
);
CREATE TABLE IF NOT EXISTS taxonomy_nodes (
    taxonomy_snapshot_id TEXT NOT NULL REFERENCES taxonomy_snapshots(taxonomy_snapshot_id) ON DELETE CASCADE,
    taxid INTEGER NOT NULL,
    parent_taxid INTEGER,
    rank TEXT NOT NULL,
    scientific_name TEXT NOT NULL,
    is_extinct INTEGER,
    is_formal INTEGER,
    PRIMARY KEY (taxonomy_snapshot_id, taxid)
);
CREATE TABLE IF NOT EXISTS taxonomy_aliases (
    taxonomy_snapshot_id TEXT NOT NULL REFERENCES taxonomy_snapshots(taxonomy_snapshot_id) ON DELETE CASCADE,
    alias_taxid INTEGER NOT NULL,
    current_taxid INTEGER,
    status TEXT NOT NULL,
    PRIMARY KEY (taxonomy_snapshot_id, alias_taxid)
);
CREATE TABLE IF NOT EXISTS taxonomy_reference_sets (
    reference_set_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    taxonomy_snapshot_id TEXT NOT NULL REFERENCES taxonomy_snapshots(taxonomy_snapshot_id),
    taxonomy_version TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    tsv_sha256 TEXT NOT NULL,
    tsv_size_bytes INTEGER NOT NULL,
    profile_name TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    profile_sha256 TEXT NOT NULL,
    profile_document TEXT NOT NULL,
    family_count INTEGER NOT NULL,
    genus_count INTEGER NOT NULL,
    compiled_at TEXT NOT NULL,
    workflow_run_id TEXT,
    UNIQUE (name, taxonomy_version)
);
CREATE TABLE IF NOT EXISTS coverage_reports (
    report_id TEXT PRIMARY KEY,
    reference_set_id TEXT NOT NULL REFERENCES taxonomy_reference_sets(reference_set_id),
    reference_set_sha256 TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    scope_value TEXT,
    scope_membership_sha256 TEXT NOT NULL,
    input_sha256 TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    decision TEXT,
    reason_codes TEXT NOT NULL,
    summary TEXT NOT NULL,
    relative_path TEXT,
    result_sha256 TEXT,
    created_at TEXT NOT NULL,
    workflow_run_id TEXT
);
CREATE TABLE IF NOT EXISTS coverage_report_metrics (
    report_id TEXT NOT NULL REFERENCES coverage_reports(report_id) ON DELETE CASCADE,
    rank TEXT NOT NULL,
    numerator INTEGER NOT NULL,
    denominator INTEGER NOT NULL,
    coverage_percent REAL NOT NULL,
    threshold_percent REAL NOT NULL,
    decision TEXT NOT NULL,
    PRIMARY KEY (report_id, rank)
);
CREATE INDEX IF NOT EXISTS idx_taxonomy_nodes_parent
    ON taxonomy_nodes(taxonomy_snapshot_id, parent_taxid);
CREATE INDEX IF NOT EXISTS idx_taxonomy_nodes_rank
    ON taxonomy_nodes(taxonomy_snapshot_id, rank);
CREATE INDEX IF NOT EXISTS idx_taxonomy_aliases_current
    ON taxonomy_aliases(taxonomy_snapshot_id, current_taxid);
CREATE INDEX IF NOT EXISTS idx_coverage_reports_reference
    ON coverage_reports(reference_set_id, scope_kind, scope_value);
"""

SOURCE_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS data_sources (
    source_id TEXT PRIMARY KEY,
    identity_sha256 TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL CHECK (source_type IN ('insdc', 'non_insdc')),
    provider TEXT NOT NULL,
    database_name TEXT NOT NULL,
    record_url TEXT,
    citation TEXT,
    license_name TEXT,
    license_url TEXT,
    created_at TEXT NOT NULL,
    workflow_run_id TEXT,
    CHECK (
        source_type = 'insdc'
        OR (
            length(trim(COALESCE(citation, ''))) > 0
            AND length(trim(COALESCE(license_name, ''))) > 0
        )
    )
);
CREATE TABLE IF NOT EXISTS source_links (
    source_id TEXT NOT NULL REFERENCES data_sources(source_id) ON DELETE CASCADE,
    object_type TEXT NOT NULL CHECK (
        object_type IN ('organism', 'sample', 'run', 'assembly', 'annotation', 'file')
    ),
    object_id TEXT NOT NULL,
    relationship TEXT NOT NULL DEFAULT 'derived_from',
    linked_at TEXT NOT NULL,
    workflow_run_id TEXT,
    PRIMARY KEY (source_id, object_type, object_id, relationship)
);
CREATE INDEX IF NOT EXISTS idx_source_links_object
    ON source_links(object_type, object_id);
"""


class Database:
    """Thin wrapper around sqlite3 with Operon-specific helpers."""

    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        self._savepoint_counter = 0
        if read_only:
            uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=30)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA query_only=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(DDL)
        # TODO(1.0): remove the pre-1.0 migration call after the final release
        # stops accepting databases created by development versions.
        self._migrate_pre_1_0_schema()
        self._migrate_remote_schema_2_2()
        self._migrate_taxonomy_schema_2_3()
        self._migrate_source_schema_2_4()
        self._ensure_current_schema_objects()
        self._conn.execute(
            "INSERT INTO entity_state (entity_type, entity_id, state, message, updated_at) "
            "SELECT 'database', 'SCHEMA', 'ACTIVE', 'schema version " + SCHEMA_VERSION + "', datetime('now') "
            "ON CONFLICT(entity_type, entity_id) DO UPDATE SET state=excluded.state, message=excluded.message, "
            "updated_at=excluded.updated_at WHERE entity_state.message<>excluded.message"
        )
        self._conn.commit()

    def _migrate_pre_1_0_schema(self) -> None:
        """Upgrade development-era databases without discarding history.

        TODO(1.0): delete this method when pre-1.0 database compatibility is
        retired. Current-schema indexes and views live separately in
        ``_ensure_current_schema_objects`` and must remain.
        """
        assembly_columns = set(self.table_columns("assemblies"))
        for column in (
            "assembly_name",
            "bioproject_accession",
            "source_database",
            "assembly_status",
            "assembly_type",
        ):
            if column not in assembly_columns:
                self._conn.execute(f'ALTER TABLE assemblies ADD COLUMN "{column}" TEXT')
                assembly_columns.add(column)
        qc_columns = set(self.table_columns("qc_results"))
        if "input_identity" not in qc_columns:
            self._conn.executescript(
                """
                ALTER TABLE qc_results RENAME TO qc_results_v1;
                CREATE TABLE qc_results (
                    qc_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    file_id TEXT REFERENCES files(file_id),
                    file_sha256 TEXT,
                    input_identity TEXT NOT NULL,
                    qc_stage TEXT NOT NULL,
                    metric_name TEXT NOT NULL,
                    metric_value TEXT NOT NULL,
                    metric_numeric REAL,
                    metric_unit TEXT,
                    tool TEXT NOT NULL,
                    tool_version TEXT NOT NULL,
                    parameter_set TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    UNIQUE (input_identity, qc_stage, metric_name, tool, tool_version, parameter_set)
                );
                INSERT INTO qc_results (
                    qc_result_id, entity_type, entity_id, file_id, file_sha256,
                    input_identity, qc_stage, metric_name, metric_value,
                    metric_numeric, metric_unit, tool, tool_version,
                    parameter_set, evaluated_at
                )
                SELECT qc_result_id, entity_type, entity_id, NULL, NULL,
                       'legacy:' || entity_type || ':' || entity_id,
                       qc_stage, metric_name, metric_value, metric_numeric,
                       metric_unit, tool, tool_version, parameter_set, evaluated_at
                FROM qc_results_v1;
                DROP TABLE qc_results_v1;
                """
            )

        decision_columns = set(self.table_columns("decisions"))
        if "profile_snapshot_id" not in decision_columns:
            self._conn.executescript(
                """
                ALTER TABLE decisions RENAME TO decisions_v1;
                CREATE TABLE decisions (
                    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    profile_version INTEGER,
                    profile_snapshot_id INTEGER REFERENCES qc_profiles(profile_snapshot_id),
                    profile_sha256 TEXT,
                    decision TEXT NOT NULL,
                    curated_decision TEXT,
                    reason_codes TEXT NOT NULL,
                    observed TEXT NOT NULL,
                    thresholds TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    curated_by TEXT,
                    curated_reason TEXT,
                    curated_evidence TEXT,
                    curated_at TEXT
                );
                INSERT INTO decisions (
                    decision_id, entity_type, entity_id, profile, profile_version,
                    decision, curated_decision, reason_codes, observed, thresholds,
                    evaluated_at, curated_by, curated_reason, curated_evidence, curated_at
                )
                SELECT decision_id, entity_type, entity_id, profile, profile_version,
                       decision, curated_decision, reason_codes, observed, thresholds,
                       evaluated_at, curated_by, curated_reason, curated_evidence, curated_at
                FROM decisions_v1;
                DROP TABLE decisions_v1;
                """
            )

    def _migrate_remote_schema_2_2(self) -> None:
        """Add structured remote-location and executor provenance fields.

        Databases created before 0.3 already have ``workflow_runs``; SQLite's
        ``CREATE TABLE IF NOT EXISTS`` cannot add the new columns, so keep this
        small additive migration separate from the pre-1.0 compatibility shim.
        """
        workflow_columns = set(self.table_columns("workflow_runs"))
        for column in ("executor", "scheduler_job_id", "execution_details"):
            if column not in workflow_columns:
                self._conn.execute(f'ALTER TABLE workflow_runs ADD COLUMN "{column}" TEXT')
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS file_locations (
                file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                location_name TEXT NOT NULL,
                location_type TEXT NOT NULL,
                uri TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                verified_at TEXT,
                PRIMARY KEY (file_id, location_name)
            );
            CREATE INDEX IF NOT EXISTS idx_file_locations_status
                ON file_locations(location_name, status);
            """
        )

    def _migrate_taxonomy_schema_2_3(self) -> None:
        """Add NCBI taxonomy snapshots, frozen denominators and coverage history."""
        self._conn.executescript(TAXONOMY_SCHEMA_DDL)

    def _migrate_source_schema_2_4(self) -> None:
        """Add normalized source, citation and license records and object links."""
        self._conn.executescript(SOURCE_SCHEMA_DDL)

    def _ensure_current_schema_objects(self) -> None:
        """Create current indexes and rebuild the latest-decision view."""
        self._conn.execute("DROP VIEW IF EXISTS current_decisions")
        self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_qc_entity ON qc_results(entity_type, entity_id);
            CREATE INDEX IF NOT EXISTS idx_qc_file ON qc_results(file_id, file_sha256);
            CREATE INDEX IF NOT EXISTS idx_qc_metric ON qc_results(metric_name);
            CREATE INDEX IF NOT EXISTS idx_decisions_entity ON decisions(entity_type, entity_id);
            CREATE INDEX IF NOT EXISTS idx_decisions_current ON decisions(entity_type, entity_id, profile, decision_id);
            """
        )
        self._conn.execute(CURRENT_DECISIONS_VIEW)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        outermost = not self._conn.in_transaction
        savepoint: str | None = None
        if outermost:
            self._conn.execute("BEGIN")
        else:
            self._savepoint_counter += 1
            savepoint = f"operon_sp_{self._savepoint_counter}"
            self._conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield self._conn
            if outermost:
                self._conn.commit()
            else:
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            if outermost:
                self._conn.rollback()
            else:
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ------------------------------------------------------------------
    # Generic row helpers
    # ------------------------------------------------------------------
    def table_columns(self, table: str) -> list[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [row["name"] for row in rows]

    def upsert_rows(self, table: str, columns: list[str], rows: Iterable[dict[str, Any]]) -> int:
        """Insert or update rows by primary key. Generated tables are replaceable."""
        columns = list(columns)
        assignments = ", ".join(f"{c}=excluded.{c}" for c in columns if c != "pk" and c not in self._primary_keys(table))
        insert_cols = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO {table} ({insert_cols}) VALUES ({placeholders}) "
            f"ON CONFLICT({','.join(self._primary_keys(table))}) DO UPDATE SET {assignments}"
        )
        count = 0
        with self.transaction():
            for row in rows:
                self._conn.execute(sql, [row.get(c) for c in columns])
                count += 1
        return count

    @staticmethod
    def _primary_keys(table: str) -> list[str]:
        return {
            "files": ["file_id"],
            "organisms": ["organism_id"],
            "samples": ["sample_id"],
            "runs": ["run_id"],
            "assemblies": ["assembly_id"],
            "annotations": ["annotation_id"],
            "accessions": ["namespace", "accession"],
            "qc_results": ["qc_result_id"],
            "qc_profiles": ["profile_snapshot_id"],
            "decisions": ["decision_id"],
        }.get(table, [])

    def insert_row(self, table: str, row: dict[str, Any]) -> None:
        columns = list(row.keys())
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        with self.transaction():
            self._conn.execute(sql, [row[c] for c in columns])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, list(params)).fetchall()

    def readonly_query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """Execute SQL on a dedicated read-only connection.

        The CLI uses this instead of the project connection so DML, DDL,
        writable PRAGMAs, ATTACH, VACUUM and extension loading cannot mutate
        the database (or another database as a side effect).
        """
        uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        denied = {
            getattr(sqlite3, name)
            for name in (
                "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE",
                "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX",
                "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW",
                "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_DROP_INDEX",
                "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE",
                "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER",
                "SQLITE_DROP_VIEW", "SQLITE_ALTER_TABLE", "SQLITE_REINDEX", "SQLITE_ANALYZE",
                "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_TRANSACTION", "SQLITE_SAVEPOINT",
            )
            if hasattr(sqlite3, name)
        }
        safe_pragmas = {
            "table_info", "table_xinfo", "index_info", "index_xinfo", "index_list",
            "foreign_key_list", "database_list", "compile_options", "schema_version",
            "user_version", "application_id", "encoding",
        }

        def authorize(action: int, arg1: str | None, arg2: str | None, _db: str | None, _source: str | None) -> int:
            if action in denied:
                return sqlite3.SQLITE_DENY
            if action == getattr(sqlite3, "SQLITE_PRAGMA", -1):
                if (arg1 or "").lower() not in safe_pragmas:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        try:
            return conn.execute(sql, list(params)).fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Entity and ID helpers
    # ------------------------------------------------------------------
    def entity_exists(self, entity_type: str, entity_id: str) -> bool:
        if entity_type not in ENTITY_TABLES:
            return False
        table = ENTITY_TABLES[entity_type]
        id_col = ENTITY_ID_COLUMNS[entity_type]
        row = self._conn.execute(
            f"SELECT 1 FROM {table} WHERE {id_col}=?", (entity_id,)
        ).fetchone()
        return row is not None

    def require_entity(self, entity_type: str, entity_id: str) -> None:
        if not self.entity_exists(entity_type, entity_id):
            raise EntityNotFoundError(f"{entity_type} {entity_id} does not exist")

    def next_id(self, entity_type: str) -> str:
        if entity_type == "source":
            prefix = "SRC"
        elif entity_type in ENTITY_PREFIXES:
            prefix = ENTITY_PREFIXES[entity_type]
        else:
            raise ValidationError(f"unknown entity type {entity_type!r}")
        table = ENTITY_TABLES.get(entity_type)
        if entity_type == "source":
            table = "data_sources"
        max_n = 0
        if table:
            id_col = "source_id" if entity_type == "source" else ENTITY_ID_COLUMNS[entity_type]
            try:
                rows = self._conn.execute(f"SELECT {id_col} AS id FROM {table}").fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                match = re.fullmatch(rf"{prefix}_(\d+)", str(row["id"]))
                if match:
                    max_n = max(max_n, int(match.group(1)))
        # Also scan files table for FIL.
        if entity_type == "file":
            try:
                rows = self._conn.execute("SELECT file_id AS id FROM files").fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                match = re.fullmatch(r"FIL_(\d+)", str(row["id"]))
                if match:
                    max_n = max(max_n, int(match.group(1)))
        return f"{prefix}_{max_n + 1:06d}"

    def register_data_source(
        self,
        source: dict[str, Any],
        *,
        workflow_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Normalize and idempotently register one external data source."""
        normalized = {
            "source_type": str(source.get("source_type") or "").strip().lower(),
            "provider": str(source.get("provider") or "").strip(),
            "database_name": str(source.get("database_name") or "").strip(),
            "record_url": str(source.get("record_url") or "").strip() or None,
            "citation": str(source.get("citation") or "").strip() or None,
            "license_name": str(source.get("license_name") or "").strip() or None,
            "license_url": str(source.get("license_url") or "").strip() or None,
        }
        if normalized["source_type"] not in {"insdc", "non_insdc"}:
            raise ValidationError("source_type must be 'insdc' or 'non_insdc'")
        if not normalized["provider"]:
            raise ValidationError("data source provider is required")
        if not normalized["database_name"]:
            raise ValidationError("data source database or repository is required")
        if normalized["source_type"] == "non_insdc":
            if not normalized["citation"]:
                raise ValidationError("non-INSDC data requires a reference citation or DOI")
            if not normalized["license_name"]:
                raise ValidationError("non-INSDC data requires a License name or SPDX identifier")
        identity_sha256 = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        existing = self._conn.execute(
            "SELECT * FROM data_sources WHERE identity_sha256=?", (identity_sha256,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        from operon.utils import now_iso

        record = {
            "source_id": self.next_id("source"),
            "identity_sha256": identity_sha256,
            **normalized,
            "created_at": now_iso(),
            "workflow_run_id": workflow_run_id,
        }
        columns = list(record)
        with self.transaction():
            self._conn.execute(
                f"INSERT INTO data_sources ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [record[column] for column in columns],
            )
        return record

    def link_data_source(
        self,
        source_id: str,
        objects: Iterable[tuple[str, str]],
        *,
        workflow_run_id: str | None = None,
        relationship: str = "derived_from",
    ) -> int:
        """Link a registered source to entities or files, idempotently."""
        allowed = {"organism", "sample", "run", "assembly", "annotation", "file"}
        normalized = sorted({(str(kind), str(object_id)) for kind, object_id in objects})
        unknown = sorted({kind for kind, _object_id in normalized} - allowed)
        if unknown:
            raise ValidationError(f"unsupported source link object type(s): {unknown}")
        if self._conn.execute(
            "SELECT 1 FROM data_sources WHERE source_id=?", (source_id,)
        ).fetchone() is None:
            raise EntityNotFoundError(f"data source {source_id} does not exist")
        for kind, object_id in normalized:
            exists = (
                self._conn.execute(
                    "SELECT 1 FROM files WHERE file_id=?", (object_id,)
                ).fetchone() is not None
                if kind == "file"
                else self.entity_exists(kind, object_id)
            )
            if not exists:
                raise EntityNotFoundError(f"{kind} {object_id} does not exist")
        from operon.utils import now_iso

        linked_at = now_iso()
        before = self._conn.total_changes
        with self.transaction():
            self._conn.executemany(
                "INSERT OR IGNORE INTO source_links "
                "(source_id, object_type, object_id, relationship, linked_at, workflow_run_id) "
                "VALUES(?,?,?,?,?,?)",
                [
                    (source_id, kind, object_id, relationship, linked_at, workflow_run_id)
                    for kind, object_id in normalized
                ],
            )
        return self._conn.total_changes - before

    # ------------------------------------------------------------------
    # Metadata import/export
    # ------------------------------------------------------------------
    def ensure_metadata_columns(self, schema: Schema) -> None:
        """Add project-defined metadata fields atomically (nesting if needed)."""
        type_map = {
            "integer": "INTEGER", "boolean": "INTEGER", "float": "REAL",
            "id": "TEXT", "string": "TEXT", "date": "TEXT", "datetime": "TEXT",
        }
        with self.transaction():
            for table, spec in schema.tables.items():
                if table not in MANUAL_TABLES or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
                    continue
                existing = set(self.table_columns(table))
                for column, field_spec in spec["fields"].items():
                    if column in existing:
                        continue
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", column):
                        raise ValidationError(f"unsafe metadata column name {column!r}")
                    sqlite_type = type_map.get(field_spec.get("type", "string"), "TEXT")
                    self._conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sqlite_type}')
                    existing.add(column)

    def replace_manual_table(self, table: str, rows: list[dict[str, Any]], columns: list[str]) -> None:
        with self.transaction():
            self._conn.execute(f"DELETE FROM {table}")
            placeholders = ", ".join("?" for _ in columns)
            self._conn.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                [[row.get(c) for c in columns] for row in rows],
            )

    def export_rows(self, table: str, columns: list[str] | None = None) -> list[dict[str, Any]]:
        cols = columns or self.table_columns(table)
        existing = set(self.table_columns(table))
        selected = [c for c in cols if c in existing]
        if not selected:
            return []
        rows = self._conn.execute(f"SELECT {', '.join(selected)} FROM {table}").fetchall()
        return [{c: row[c] if c in existing else None for c in cols} for row in rows]

    def latest_metrics(self, entity_type: str, entity_id: str,
                       qc_stage: str | None = None) -> dict[str, float | str]:
        """Most recent metric value per name, optionally restricted to one QC stage."""
        stage_filter = " AND qc_stage=?" if qc_stage is not None else ""
        params: tuple[Any, ...] = (
            (entity_type, entity_id, qc_stage)
            if qc_stage is not None else (entity_type, entity_id)
        )
        rows = self._conn.execute(
            f"""
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY input_identity, metric_name
                    ORDER BY evaluated_at DESC, qc_result_id DESC
                ) AS rn
                FROM qc_results
                WHERE entity_type=? AND entity_id=?{stage_filter}
            )
            SELECT metric_name, metric_numeric, metric_value, evaluated_at, qc_result_id
            FROM ranked WHERE rn=1
            ORDER BY evaluated_at DESC, qc_result_id DESC
            """,
            params,
        ).fetchall()
        result: dict[str, float | str] = {}
        conservative_min = {"file_exists", "sha256_match", "parseable", "paired_read_count_match"}
        for row in rows:
            name = row["metric_name"]
            value = row["metric_numeric"] if row["metric_numeric"] is not None else row["metric_value"]
            if name in conservative_min and name in result:
                try:
                    result[name] = min(float(result[name]), float(value))
                except (TypeError, ValueError):
                    pass
            elif name not in result:
                result[name] = value
        return result

    def insert_qc_result(self, metric: dict[str, Any]) -> None:
        columns = [
            "entity_type", "entity_id", "file_id", "file_sha256", "input_identity",
            "qc_stage", "metric_name", "metric_value",
            "metric_numeric", "metric_unit", "tool", "tool_version", "parameter_set", "evaluated_at",
        ]
        metric = dict(metric)
        metric.setdefault("input_identity", (
            f"file:{metric.get('file_id')}:{metric.get('file_sha256')}"
            if metric.get("file_id") else f"entity:{metric.get('entity_type')}:{metric.get('entity_id')}"
        ))
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO qc_results ({', '.join(columns)}) VALUES ({placeholders}) "
            "ON CONFLICT(input_identity, qc_stage, metric_name, tool, tool_version, parameter_set) "
            "DO UPDATE SET metric_value=excluded.metric_value, metric_numeric=excluded.metric_numeric, "
            "metric_unit=excluded.metric_unit, file_id=excluded.file_id, "
            "file_sha256=excluded.file_sha256, evaluated_at=excluded.evaluated_at"
        )
        with self.transaction():
            self._conn.execute(sql, [metric.get(c) for c in columns])

    def insert_many_qc(self, metrics: Iterable[dict[str, Any]]) -> int:
        count = 0
        with self.transaction():
            for metric in metrics:
                metric = dict(metric)
                metric.setdefault("input_identity", (
                    f"file:{metric.get('file_id')}:{metric.get('file_sha256')}"
                    if metric.get("file_id") else f"entity:{metric.get('entity_type')}:{metric.get('entity_id')}"
                ))
                columns = [
                    "entity_type", "entity_id", "file_id", "file_sha256", "input_identity",
                    "qc_stage", "metric_name", "metric_value",
                    "metric_numeric", "metric_unit", "tool", "tool_version", "parameter_set", "evaluated_at",
                ]
                placeholders = ", ".join("?" for _ in columns)
                sql = (
                    f"INSERT INTO qc_results ({', '.join(columns)}) VALUES ({placeholders}) "
                    "ON CONFLICT(input_identity, qc_stage, metric_name, tool, tool_version, parameter_set) "
                    "DO UPDATE SET metric_value=excluded.metric_value, metric_numeric=excluded.metric_numeric, "
                    "metric_unit=excluded.metric_unit, file_id=excluded.file_id, "
                    "file_sha256=excluded.file_sha256, evaluated_at=excluded.evaluated_at"
                )
                self._conn.execute(sql, [metric.get(c) for c in columns])
                count += 1
        return count

    def set_entity_state(self, entity_type: str, entity_id: str, state: str, message: str | None = None, updated_at: str | None = None) -> None:
        from operon.utils import now_iso
        updated_at = updated_at or now_iso()
        with self.transaction():
            self._conn.execute(
                "INSERT INTO entity_state(entity_type, entity_id, state, message, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(entity_type, entity_id) DO UPDATE SET state=excluded.state, message=excluded.message, updated_at=excluded.updated_at",
                (entity_type, entity_id, state, message, updated_at),
            )

    def get_entity_state(self, entity_type: str, entity_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT state FROM entity_state WHERE entity_type=? AND entity_id=?", (entity_type, entity_id)
        ).fetchone()
        return row["state"] if row else None

    def record_change(self, object_type: str, object_id: str, field: str | None, old_value: Any, new_value: Any,
                      reason: str, evidence: str | None = None, actor: str | None = None) -> None:
        from operon.utils import now_iso
        with self.transaction():
            self._conn.execute(
                "INSERT INTO changes(object_type, object_id, field, old_value, new_value, reason, evidence, actor, changed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (object_type, object_id, field, str(old_value) if old_value is not None else None,
                 str(new_value) if new_value is not None else None, reason, evidence, actor, now_iso()),
            )

    def set_file_status(self, file_id: str, status: str, *, reason: str,
                        actor: str, evidence: str | None = None) -> bool:
        """Set one file status and append its audit row in the same transaction."""
        from operon.utils import now_iso
        row = self._conn.execute("SELECT status FROM files WHERE file_id=?", (file_id,)).fetchone()
        if row is None:
            raise EntityNotFoundError(f"file {file_id} does not exist")
        old_status = str(row["status"])
        if old_status == status:
            return False
        with self.transaction():
            self._conn.execute("UPDATE files SET status=? WHERE file_id=?", (status, file_id))
            self._conn.execute(
                "INSERT INTO changes(object_type, object_id, field, old_value, new_value, reason, "
                "evidence, actor, changed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "files", file_id, "status", old_status, status, reason,
                    evidence, actor, now_iso(),
                ),
            )
        return True

    def record_profile(self, name: str, version: int, sha256: str, document: str, recorded_at: str) -> int:
        with self.transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO qc_profiles(profile_name, profile_version, profile_sha256, profile_document, recorded_at) "
                "VALUES(?,?,?,?,?)",
                (name, version, sha256, document, recorded_at),
            )
            row = self._conn.execute(
                "SELECT profile_snapshot_id FROM qc_profiles WHERE profile_name=? AND profile_version=? AND profile_sha256=?",
                (name, version, sha256),
            ).fetchone()
        return int(row["profile_snapshot_id"])

    def upsert_decision(self, decision: dict[str, Any]) -> int:
        """Append an automatic decision; retained name preserves API compatibility."""
        columns = [
            "entity_type", "entity_id", "profile", "profile_version", "profile_snapshot_id", "profile_sha256",
            "decision", "curated_decision",
            "reason_codes", "observed", "thresholds", "evaluated_at", "curated_by", "curated_reason",
            "curated_evidence", "curated_at",
        ]
        placeholders = ", ".join("?" for _ in columns)
        with self.transaction():
            cursor = self._conn.execute(
                f"INSERT INTO decisions ({', '.join(columns)}) VALUES ({placeholders})",
                [decision.get(c) for c in columns],
            )
        return int(cursor.lastrowid)

    def effective_decision(self, entity_type: str, entity_id: str, profile: str) -> str | None:
        row = self._conn.execute(
            "SELECT COALESCE(curated_decision, decision) AS effective FROM current_decisions "
            "WHERE entity_type=? AND entity_id=? AND profile=?",
            (entity_type, entity_id, profile),
        ).fetchone()
        return row["effective"] if row else None
