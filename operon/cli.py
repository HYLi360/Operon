"""Command line interface for Operon."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from operon import __version__
from operon.config import Project, load_project
from operon.coverage import report_coverage
from operon.database import Database
from operon.errors import OperonError, ValidationError
from operon.files import ingest_file, standardize_all, standardize_file, verify_files
from operon.release import create_release
from operon.reports import export_qc_tsv, print_decisions, print_qc_table, print_status
from operon.rules import curate_decision, evaluate_all, evaluate_entity
from operon.schema import (
    ENTITY_ID_COLUMNS,
    ENTITY_PREFIXES,
    ENTITY_TABLES,
    Schema,
    read_tsv,
    write_tsv,
)
from operon.taxonomy import (
    compile_reference_set,
    import_ncbi_taxonomy,
    list_reference_sets,
    list_taxonomy_snapshots,
)
from operon.utils import format_table, parse_key_values
from operon.workflow import set_state

MANUAL_METADATA_ENTITIES = ["organism", "sample", "run", "assembly", "annotation"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="operon",
        description="Operon the System: Archive, Quality-Control, Organize, Analyze and Release Your Bio-Data",
    )
    parser.add_argument("--project", default=".", help="project root or project.yaml path (default: current directory)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="initialize a new Operon project")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--project-id", default="PRJ_000001")
    p.add_argument("--name", default="")

    p = sub.add_parser("init-demo", help="initialize a project with synthetic assemblies/annotations/reads and run the pipeline")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--project-id", default="PRJ_DEMO_001")

    p = sub.add_parser("status", help="show entity states")
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")

    p = sub.add_parser("schema", help="show schema path or dump it")
    p.add_argument("--dump", action="store_true")

    p = sub.add_parser("import-metadata", help="validate and import metadata/*.tsv into SQLite")
    p.add_argument("--replace", action="store_true", help="replace manual tables before import")

    p = sub.add_parser("export-metadata", help="export SQLite manual tables back to metadata/*.tsv")
    p.add_argument("--include-generated", action="store_true", help="also export qc_results and decisions to reports/")

    p = sub.add_parser("add", help="add one metadata record")
    p.add_argument("entity_type", choices=MANUAL_METADATA_ENTITIES)
    p.add_argument("--id", dest="record_id", help="explicit internal ID (otherwise next ID is allocated)")
    p.add_argument("--field", action="append", default=[], metavar="KEY=VALUE", help="repeatable field values")

    p = sub.add_parser("add-accession", help="map an external accession to an internal stable ID")
    p.add_argument("--internal-type", required=True, choices=MANUAL_METADATA_ENTITIES)
    p.add_argument("--internal-id", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--accession", required=True)
    p.add_argument("--version", dest="acc_version")
    p.add_argument("--primary", action="store_true")

    p = sub.add_parser(
        "ncbi-datasets",
        help="offline-first import/download adapter for NCBI Datasets genome packages",
    )
    p.add_argument(
        "--input", dest="inputs", action="append", default=[], metavar="PATH",
        help="existing assembly_data_report JSON/JSONL, Datasets ZIP, or unpacked directory (repeatable)",
    )
    p.add_argument(
        "--accession", action="append", default=[], metavar="GCF_OR_GCA",
        help="download an assembly through the NCBI Datasets API, then import and archive it (repeatable)",
    )
    p.add_argument(
        "--accession-file", metavar="FILE",
        help="text file containing one GCF/GCA accession per line",
    )
    p.add_argument(
        "--include", action="append",
        choices=["genome", "gff3", "protein", "cds", "sequence-report"],
        help="downloaded package content; repeatable (default: all supported types)",
    )
    p.add_argument(
        "--no-archive-files", action="store_true",
        help="import metadata but do not ingest sequence/annotation files found in packages",
    )
    p.add_argument(
        "--standardize", action="store_true",
        help="also create standardized/ copies for automatically archived files",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="parse, normalize and show the plan without changing the project",
    )
    p.add_argument(
        "--no-preserve-source", action="store_true",
        help="do not preserve input reports/downloaded ZIPs under raw/metadata/ncbi_datasets",
    )
    p.add_argument("--email", help="NCBI contact email (or set NCBI_EMAIL)")
    p.add_argument("--api-key", help="NCBI API key (or set NCBI_API_KEY)")
    p.add_argument("--timeout", type=float, default=300.0, help="download read timeout in seconds")
    p.add_argument(
        "--batch-size", type=int, default=10,
        help="accessions per Datasets request (default: 10; max: 100)",
    )
    p.add_argument(
        "--download-workers", type=int, default=3,
        help="parallel download workers (default: 3; max: 10)",
    )
    p.add_argument(
        "--retries", type=int, default=4,
        help="retries per batch for SSL/transient failures (default: 4; max: 10)",
    )
    p.add_argument(
        "--retry-backoff", type=float, default=1.0,
        help="initial retry backoff in seconds, doubled each attempt (default: 1.0)",
    )

    p = sub.add_parser("next-id", help="allocate the next stable internal ID for an entity type")
    p.add_argument("entity_type", choices=["organism", "sample", "run", "assembly", "annotation", "file"])

    p = sub.add_parser("ingest", help="archive a file or directory (local path, sftp:// or remote:// URL) into raw/ with a content hash and manifest record")
    p.add_argument("--source", required=True, help="local path, sftp://[user@]host[:port]/path, or remote://<name>/<path>")
    p.add_argument("--entity-type", required=True, choices=["organism", "sample", "run", "assembly", "annotation"])
    p.add_argument("--entity-id", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--format", dest="fmt")
    p.add_argument("--compression")
    p.add_argument("--source-url")
    p.add_argument("--move", action="store_true", help="move source instead of copying")

    p = sub.add_parser(
        "verify",
        help="verify local artifacts and live-check recorded remotes for remote-only files",
    )
    p.add_argument("--file-id", action="append", default=[])

    p = sub.add_parser("standardize", help="create standardized/ links or copies from verified raw files")
    p.add_argument("--file-id", action="append", default=[])
    p.add_argument("--link", choices=["hardlink", "symlink", "copy"], default="copy")

    p = sub.add_parser("qc", help="run built-in file/assembly/annotation/read QC and write long metrics")
    p.add_argument("--file-id")
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")
    p.add_argument("--sample-size", type=int, default=1000000)

    p = sub.add_parser("import-qc", help="import external QC metrics (e.g. BUSCO, QUAST, FastQC) from TSV")
    p.add_argument("--file", dest="tsv_file", required=True)

    p = sub.add_parser("run-external", help="run an external tool with structured provenance (stdout/stderr, exit code, expected outputs)")
    p.add_argument("--step", required=True, help="workflow step name, e.g. busco / quast / fastp")
    p.add_argument("--command", dest="command_line", required=True, help="quoted command line, e.g. 'busco -i in.fa -o out -m genome'")
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")
    p.add_argument("--parameter-set")
    p.add_argument("--expected-output", action="append", default=[], help="path that must exist and be non-empty")
    p.add_argument("--cwd")
    p.add_argument("--timeout", type=float)
    p.add_argument("--backend", choices=["local", "slurm", "ssh"],
                   help="execution backend (default: execution.backend in project.yaml)")

    p = sub.add_parser("tools-check", help="detect configured external tools and their versions")
    p = sub.add_parser("analyze", help="run a configured external-tool recipe over matching manifest artifacts")
    p.add_argument("--analysis", required=True, help="recipe name, e.g. blastn_nt / hmmsearch_pfam / busco_autolineage")
    p.add_argument(
        "--param", action="append", default=[], metavar="NAME=VALUE",
        help="set a runtime parameter declared by the recipe; repeat for multiple values",
    )
    p.add_argument("--entity-type", help="restrict to entity type")
    p.add_argument("--entity-id", help="restrict to one entity")
    p.add_argument("--threads", type=int, help="override default threads")
    p.add_argument("--limit", type=int, help="only process the first N matching files")
    p.add_argument("--dry-run", action="store_true", help="show planned commands and cache status without executing")
    p.add_argument("--force", action="store_true", help="re-run even when a completed cached job exists")
    p.add_argument("--keep-partial", action="store_true",
                   help="on Ctrl+C/SIGTERM, keep the interrupted step's partial output instead of deleting it")
    p.add_argument("--backend", choices=["local", "slurm", "ssh"],
                   help="execution backend (default: execution.backend in project.yaml)")

    p = sub.add_parser("remotes", help="list configured remotes (project.yaml remotes:) and test connectivity")

    p = sub.add_parser("push", help="upload manifest files to a configured remote mirror (SFTP, checksum-verified, idempotent)")
    p.add_argument("--remote", required=True, help="remote name from the project.yaml remotes: section")
    p.add_argument("--file-id", action="append", default=[], help="restrict to these manifest files (default: all)")

    p = sub.add_parser("evict", help="remove local bytes after an exact remote mirror copy is verified")
    p.add_argument("--remote", required=True, help="remote name holding the verified copy")
    p.add_argument("--file-id", action="append", default=[], help="restrict to these manifest files (default: all)")

    p = sub.add_parser("locations", help="show local/remote residency for manifest files")
    p.add_argument("--file-id", action="append", default=[], help="restrict to these manifest files")

    p = sub.add_parser("pull", help="restore manifest files from a configured remote mirror (SFTP, checksum-verified)")
    p.add_argument("--remote", required=True, help="remote name from the project.yaml remotes: section")
    p.add_argument("--file-id", action="append", default=[], help="restrict to these manifest files (default: everything in the remote manifest)")

    p = sub.add_parser("evaluate", help="apply a versioned QC profile and record decisions")
    p.add_argument("--profile")
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")

    p = sub.add_parser("curate", help="record an audited human override of an automatic decision")
    p.add_argument("--entity-type", required=True)
    p.add_argument("--entity-id", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--decision", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence")

    p = sub.add_parser("release", help="create an immutable dataset release snapshot")
    p.add_argument("--version", required=True)
    p.add_argument("--profile", required=True)
    p.add_argument("--link", choices=["hardlink", "copy"], default="copy",
                   help="release file storage mode (default: copy)")
    p.add_argument("--copy-files", action="store_true",
                   help="compatibility alias for --link copy")

    p = sub.add_parser("run-pipeline", help="ingest -> verify -> standardize -> QC -> evaluate for one file")
    p.add_argument("--source", required=True)
    p.add_argument("--entity-type", required=True, choices=["run", "assembly", "annotation"])
    p.add_argument("--entity-id", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--format", dest="fmt")
    p.add_argument("--compression")
    p.add_argument("--source-url")
    p.add_argument("--profile")

    p = sub.add_parser("taxonomy", help="manage immutable NCBI Taxonomy snapshots and denominators")
    taxonomy_sub = p.add_subparsers(dest="taxonomy_command", required=True)
    tp = taxonomy_sub.add_parser("import", help="archive and import an NCBI Datasets taxonomy package")
    tp.add_argument(
        "--input", required=True,
        help="taxonomy_report.jsonl/Datasets package or official NCBI taxdump archive",
    )
    tp.add_argument("--version", required=True, help="explicit immutable taxonomy version label")
    taxonomy_sub.add_parser("list", help="list imported NCBI Taxonomy snapshots")
    tp = taxonomy_sub.add_parser("compile", help="compile a coverage profile into a frozen denominator")
    tp.add_argument("--profile", required=True)
    tp.add_argument("--taxonomy-version", required=True)
    taxonomy_sub.add_parser("reference-sets", help="list compiled taxonomy reference sets")

    p = sub.add_parser("report", help="render queryable project and release reports")
    report_sub = p.add_subparsers(dest="report_kind", required=True)
    rp = report_sub.add_parser("qc", help="show or export long-form QC results")
    rp.add_argument("--entity-type")
    rp.add_argument("--entity-id")
    rp.add_argument("--export", action="store_true", help="write qc/aggregate TSV files")
    rp = report_sub.add_parser("decisions", help="show current QC decisions")
    rp.add_argument("--profile")
    rp = report_sub.add_parser("analysis", help="show synchronized analysis summaries or hits")
    rp.add_argument("--analysis")
    rp.add_argument("--entity-type")
    rp.add_argument("--entity-id")
    rp.add_argument("--hits", action="store_true", help="show top-hit rows instead of job summaries")
    rp.add_argument("--limit", type=int, default=20)
    rp = report_sub.add_parser("coverage", help="measure NCBI family/genus coverage against a frozen reference set")
    rp.add_argument("--reference-set", required=True)
    scope = rp.add_mutually_exclusive_group()
    scope.add_argument("--scope", choices=["metadata"], default="metadata")
    scope.add_argument("--release", help="restrict observations to one immutable release")

    p = sub.add_parser("query", help="run arbitrary read-only SQL against the file database")
    p.add_argument("sql")

    p = sub.add_parser("set-state", help="manually set workflow state (audited; use --force for non-standard transition)")
    p.add_argument("--entity-type", required=True)
    p.add_argument("--entity-id", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--message")
    p.add_argument("--force", action="store_true")

    return parser


def _open_project(args: argparse.Namespace) -> tuple[Project, Database]:
    project = load_project(args.project)
    db = Database(project.db_path, read_only=args.command == "query")
    return project, db


def _print_table(headers: list[str], rows: list[list[Any]]) -> None:
    print(format_table(headers, rows))


def _cmd_init(args: argparse.Namespace) -> int:
    project = Project.init(args.path, project_id=args.project_id, name=args.name)
    print(f"initialized Operon project {project.project_id} at {project.root}")
    print("next steps:")
    print("  1. edit metadata/*.tsv (or use `operon add ...`)")
    print("  2. operon import-metadata --replace")
    print("  3. operon ingest --source genome.fa --entity-type assembly --entity-id ASM_000001 --role genome_fasta")
    print("  4. operon run-pipeline ...")
    return 0


def _cmd_init_demo(args: argparse.Namespace) -> int:
    from operon.demo import init_demo
    result = init_demo(Path(args.path), project_id=args.project_id)
    print(f"demo project ready at {result.root}")
    print("run `operon --project . status` or `operon --project . report qc` to inspect it")
    return 0


def _cmd_status(args: argparse.Namespace, db: Database) -> int:
    sql = "SELECT entity_type, entity_id, state, message, updated_at FROM entity_state WHERE entity_type != 'database'"
    params: list[Any] = []
    if args.entity_type:
        sql += " AND entity_type=?"
        params.append(args.entity_type)
    if args.entity_id:
        sql += " AND entity_id=?"
        params.append(args.entity_id)
    sql += " ORDER BY entity_type, entity_id"
    rows = db.conn.execute(sql, params).fetchall()
    if not rows:
        print("no entity states recorded yet")
    else:
        print(format_table(["entity_type", "entity_id", "state", "message", "updated_at"], ([r[c] for c in r.keys()] for r in rows)))
    return 0


def _cmd_schema(args: argparse.Namespace, project: Project) -> int:
    if args.dump:
        print(project.schema_path.read_text(encoding="utf-8"))
    else:
        print(project.schema_path)
    return 0


def _cmd_import_metadata(args: argparse.Namespace, project: Project, db: Database) -> int:
    schema = Schema.from_file(project.schema_path)
    loaded: dict[str, list[dict[str, Any]]] = {}
    for table, spec in schema.tables.items():
        path = project.metadata_dir / spec["file"]
        if not path.exists():
            continue
        rows = read_tsv(path)
        normalized, _ = schema.validate_and_normalize(table, rows)
        loaded[table] = normalized
    with db.transaction() as conn:
        if args.replace:
            conn.execute("PRAGMA defer_foreign_keys=ON")
        db.ensure_metadata_columns(schema)
        _validate_cross_references(db, loaded, replace=args.replace)
        import_order = ["organisms", "samples", "runs", "assemblies", "annotations", "files", "accessions"]
        if args.replace:
            # Delete children before parents, then rebuild the complete TSV
            # snapshot in dependency order. Empty header-only TSVs therefore
            # correctly clear their corresponding tables.
            for table in ["accessions", "files", "annotations", "runs", "assemblies", "samples", "organisms"]:
                if table in loaded:
                    conn.execute(f"DELETE FROM {table}")
            replaced_types = [entity_type for entity_type, table in ENTITY_TABLES.items() if table in loaded]
            if replaced_types:
                placeholders = ", ".join("?" for _ in replaced_types)
                conn.execute(f"DELETE FROM entity_state WHERE entity_type IN ({placeholders})", replaced_types)
        for table in import_order:
            if table not in loaded:
                continue
            columns = schema.columns(table)
            placeholders = ", ".join("?" for _ in columns)
            if args.replace:
                sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
            else:
                primary_keys = db._primary_keys(table)
                assignments = ", ".join(f"{c}=excluded.{c}" for c in columns if c not in primary_keys)
                sql = (
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
                    f"ON CONFLICT({','.join(primary_keys)}) DO UPDATE SET {assignments}"
                )
            conn.executemany(sql, [[row.get(c) for c in columns] for row in loaded[table]])
        updated_at = _now_for_cli()
        for entity_type, table in ENTITY_TABLES.items():
            if table not in loaded:
                continue
            id_col = ENTITY_ID_COLUMNS[entity_type]
            conn.executemany(
                "INSERT INTO entity_state(entity_type, entity_id, state, message, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(entity_type, entity_id) DO UPDATE SET state=excluded.state, message=excluded.message, updated_at=excluded.updated_at",
                [(entity_type, row[id_col], "METADATA_VALIDATED", "metadata imported and schema-validated", updated_at)
                 for row in loaded[table]],
            )
    print(f"imported {sum(len(v) for v in loaded.values())} metadata rows into {db.path}")
    return 0


def _validate_cross_references(db: Database, loaded: dict[str, list[dict[str, Any]]], replace: bool = False) -> None:
    errors: list[str] = []
    final_rows: dict[str, list[dict[str, Any]]] = {}
    for table in ["organisms", "samples", "runs", "assemblies", "annotations", "accessions", "files"]:
        incoming = loaded.get(table)
        if replace and incoming is not None:
            final_rows[table] = incoming
            continue
        current = db.export_rows(table)
        if incoming is None:
            final_rows[table] = current
            continue
        keys = db._primary_keys(table)
        by_key = {tuple(row.get(key) for key in keys): row for row in current}
        for row in incoming:
            by_key[tuple(row.get(key) for key in keys)] = row
        final_rows[table] = list(by_key.values())

    entity_ids = {
        entity_type: {row[ENTITY_ID_COLUMNS[entity_type]] for row in final_rows[table]}
        for entity_type, table in ENTITY_TABLES.items()
    }
    for row in final_rows["samples"]:
        if row.get("organism_id") and row["organism_id"] not in entity_ids["organism"]:
            errors.append(f"samples {row['sample_id']}: organism_id {row['organism_id']} does not exist")
    for row in final_rows["runs"]:
        if row.get("sample_id") and row["sample_id"] not in entity_ids["sample"]:
            errors.append(f"runs {row['run_id']}: sample_id {row['sample_id']} does not exist")
    for row in final_rows["assemblies"]:
        if row.get("sample_id") and row["sample_id"] not in entity_ids["sample"]:
            errors.append(f"assemblies {row['assembly_id']}: sample_id {row['sample_id']} does not exist")
    for row in final_rows["annotations"]:
        if row.get("assembly_id") and row["assembly_id"] not in entity_ids["assembly"]:
            errors.append(f"annotations {row['annotation_id']}: assembly_id {row['assembly_id']} does not exist")
    for row in final_rows["accessions"]:
        entity_type = row["internal_type"]
        if entity_type in entity_ids and row["internal_id"] not in entity_ids[entity_type]:
            errors.append(f"accessions {row['namespace']}:{row['accession']}: internal_id {row['internal_id']} does not exist")
    for row in final_rows["files"]:
        if row["entity_type"] in entity_ids and row["entity_id"] not in entity_ids[row["entity_type"]]:
            errors.append(f"files {row['file_id']}: {row['entity_type']} {row['entity_id']} does not exist")
    file_ids = {row["file_id"] for row in final_rows["files"]}
    for row in final_rows["assemblies"]:
        if row.get("fasta_file_id") and row["fasta_file_id"] not in file_ids:
            errors.append(f"assemblies {row['assembly_id']}: fasta_file_id {row['fasta_file_id']} does not exist")
    for row in final_rows["annotations"]:
        for field in ("gff_file_id", "cds_file_id", "protein_file_id"):
            if row.get(field) and row[field] not in file_ids:
                errors.append(f"annotations {row['annotation_id']}: {field} {row[field]} does not exist")
    if errors:
        raise ValidationError("\n".join(errors))


def _cmd_export_metadata(args: argparse.Namespace, project: Project, db: Database) -> int:
    schema = Schema.from_file(project.schema_path)
    total = 0
    for table in ["organisms", "samples", "runs", "assemblies", "annotations", "accessions", "files"]:
        columns = schema.columns(table)
        rows = db.export_rows(table, columns)
        write_tsv(project.metadata_dir / schema.tables[table]["file"], columns, rows)
        total += len(rows)
    if args.include_generated:
        export_qc_tsv(db, project)
        decisions = db.export_rows("decisions", ["decision_id", "entity_type", "entity_id", "profile", "profile_version", "profile_snapshot_id", "profile_sha256", "decision", "curated_decision", "reason_codes", "observed", "thresholds", "evaluated_at", "curated_by", "curated_reason", "curated_evidence", "curated_at"])
        write_tsv(project.reports_root / "decisions.tsv", list(decisions[0].keys()) if decisions else ["entity_type", "entity_id", "profile"], decisions)
    print(f"exported {total} rows to {project.metadata_dir}")
    return 0


def _cmd_add(args: argparse.Namespace, project: Project, db: Database) -> int:
    entity_type = args.entity_type
    table = ENTITY_TABLES[entity_type]
    id_col = ENTITY_ID_COLUMNS[entity_type]
    fields = parse_key_values(args.field)
    schema = Schema.from_file(project.schema_path)
    record_id = args.record_id or db.next_id(entity_type)
    row = dict(fields)
    row[id_col] = record_id
    db.ensure_metadata_columns(schema)
    for extra in list(row.keys()):
        if extra not in schema.columns(table):
            print(f"warning: unknown field {extra!r} for {entity_type}; add it to {project.schema_path} to remove this warning", file=sys.stderr)
    normalized, _ = schema.validate_and_normalize(table, [row])
    row = normalized[0]
    _check_fks_for_row(db, entity_type, row, require_target=True)
    db.insert_row(table, row)
    _append_metadata_tsv(project, table, row)
    db.set_entity_state(entity_type, record_id, "METADATA_VALIDATED", "record added via CLI and schema-validated")
    db.record_change(entity_type, record_id, None, None, json.dumps({k: str(v) for k, v in row.items()}), "record added", actor=os.environ.get("USER"))
    print(f"added {entity_type} {record_id}")
    return 0


def _check_fks_for_row(db: Database, entity_type: str, row: dict[str, Any], require_target: bool) -> None:
    if entity_type == "sample" and row.get("organism_id"):
        db.require_entity("organism", row["organism_id"])
    elif entity_type == "run" and row.get("sample_id"):
        db.require_entity("sample", row["sample_id"])
    elif entity_type == "assembly" and row.get("sample_id"):
        db.require_entity("sample", row["sample_id"])
    elif entity_type == "annotation" and row.get("assembly_id"):
        db.require_entity("assembly", row["assembly_id"])
    for field in ("fasta_file_id", "gff_file_id", "cds_file_id", "protein_file_id"):
        if row.get(field) and db.conn.execute("SELECT 1 FROM files WHERE file_id=?", (row[field],)).fetchone() is None:
            raise ValidationError(f"{entity_type} {row.get(ENTITY_ID_COLUMNS.get(entity_type, 'id'))}: {field} {row[field]} does not exist")


def _append_metadata_tsv(project: Project, table: str, row: dict[str, Any]) -> None:
    schema = Schema.from_file(project.schema_path)
    path = project.metadata_dir / schema.tables[table]["file"]
    columns = schema.columns(table)
    existed = path.exists()
    with open(path, "a", encoding="utf-8", newline="") as handle:
        import csv
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        if not existed or path.stat().st_size == 0:
            writer.writerow(columns)
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in columns])


def _cmd_add_accession(args: argparse.Namespace, project: Project, db: Database) -> int:
    db.require_entity(args.internal_type, args.internal_id)
    row = {
        "internal_type": args.internal_type,
        "internal_id": args.internal_id,
        "namespace": args.namespace,
        "accession": args.accession,
        "version": args.acc_version,
        "is_primary": 1 if args.primary else None,
    }
    db.insert_row("accessions", row)
    _append_metadata_tsv(project, "accessions", row)
    print(f"mapped {args.namespace}:{args.accession} -> {args.internal_type} {args.internal_id}")
    return 0


def _cmd_ncbi_datasets(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.adapters.ncbi_datasets import DEFAULT_INCLUDES, run_ncbi_datasets_adapter

    result = run_ncbi_datasets_adapter(
        db,
        project,
        inputs=args.inputs,
        accessions=args.accession,
        accession_file=args.accession_file,
        includes=args.include or DEFAULT_INCLUDES,
        archive_files=not args.no_archive_files,
        standardize=args.standardize,
        dry_run=args.dry_run,
        preserve_sources=not args.no_preserve_source,
        email=args.email or os.environ.get("NCBI_EMAIL"),
        api_key=args.api_key or os.environ.get("NCBI_API_KEY"),
        timeout=args.timeout,
        batch_size=args.batch_size,
        download_workers=args.download_workers,
        max_retries=args.retries,
        retry_backoff=args.retry_backoff,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_next_id(args: argparse.Namespace, db: Database) -> int:
    print(db.next_id(args.entity_type))
    return 0


def _cmd_ingest(args: argparse.Namespace, project: Project, db: Database) -> int:
    source = args.source
    source_url = args.source_url
    temp_path: Path | None = None
    if source.startswith(("sftp://", "remote://")):
        from operon.remotes import fetch_url_to_temp
        temp_path = fetch_url_to_temp(project, source)
        source = str(temp_path)
        source_url = source_url or args.source
    try:
        row = ingest_file(
            db, project, source, args.entity_type, args.entity_id, args.role,
            fmt=args.fmt, compression=args.compression, source_url=source_url, move=args.move,
        )
    finally:
        if temp_path is not None:
            if temp_path.is_dir() and not temp_path.is_symlink():
                import shutil
                shutil.rmtree(temp_path, ignore_errors=True)
            else:
                temp_path.unlink(missing_ok=True)
    print(f"registered {row['file_id']} -> {row['relative_path']} (sha256 {row['sha256'][:16]}...)")
    return 0


def _cmd_verify(args: argparse.Namespace, project: Project, db: Database) -> int:
    results = verify_files(db, project, args.file_id or None)
    failed = [r for r in results if r["status"] not in {"CHECKSUM_VERIFIED", "REMOTE_ONLY"}]
    print(format_table(["file_id", "relative_path", "status", "remote", "current_sha256", "error"], (
        [r["file_id"], r["relative_path"], r["status"], r.get("remote") or "",
         r["current_sha256"] or "", r["error"] or ""] for r in results
    )))
    if failed:
        return 1
    print(f"verified {len(results)} file(s)")
    return 0


def _cmd_standardize(args: argparse.Namespace, project: Project, db: Database) -> int:
    if args.file_id:
        for file_id in args.file_id:
            result = standardize_file(db, project, file_id, link_kind=args.link)
            print(f"{file_id}: {result['action']} -> {result['target']}")
    else:
        for result in standardize_all(db, project, link_kind=args.link):
            if "error" in result:
                print(f"{result.get('file_id', '?')}: ERROR {result['error']}", file=sys.stderr)
            else:
                print(f"{result['file_id']}: {result['action']} -> {result['target']}")
    return 0


def _cmd_qc(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.qc_module import qc_all
    results = qc_all(db, project, entity_type=args.entity_type, entity_id=args.entity_id, file_id=args.file_id, sample_size=args.sample_size)
    ok = sum(1 for r in results if r["ok"])
    for r in results:
        if not r["ok"]:
            print(f"{r['file_id']}: FAILED {r['error']}", file=sys.stderr)
    print(f"QC complete: {ok}/{len(results)} file(s) passed built-in stages")
    return 0 if ok == len(results) else 1


def _cmd_import_qc(args: argparse.Namespace, project: Project, db: Database) -> int:
    rows = read_tsv(args.tsv_file)
    required = ["entity_type", "entity_id", "qc_stage", "metric_name", "metric_value", "tool", "tool_version", "parameter_set"]
    missing = [c for c in required if not rows or c not in rows[0]]
    if missing:
        raise ValidationError(f"{args.tsv_file}: missing columns {missing}")
    count = 0
    for row in rows:
        db.require_entity(row["entity_type"], row["entity_id"])
        file_id = (row.get("file_id") or "").strip() or None
        file_sha256 = (row.get("file_sha256") or "").strip() or None
        if file_id:
            file_row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
            if not file_row:
                raise ValidationError(f"{args.tsv_file}: file_id {file_id} does not exist")
            if file_row["entity_type"] != row["entity_type"] or file_row["entity_id"] != row["entity_id"]:
                raise ValidationError(
                    f"{args.tsv_file}: file_id {file_id} belongs to "
                    f"{file_row['entity_type']} {file_row['entity_id']}, not {row['entity_type']} {row['entity_id']}"
                )
            if file_sha256 and file_sha256.lower() != str(file_row["sha256"]).lower():
                raise ValidationError(f"{args.tsv_file}: file_sha256 does not match manifest for {file_id}")
            file_sha256 = str(file_row["sha256"])
        try:
            numeric = float(row["metric_value"])
        except (TypeError, ValueError):
            numeric = None
        record = {
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "file_id": file_id,
            "file_sha256": file_sha256,
            "input_identity": (
                f"file:{file_id}:{file_sha256}" if file_id
                else f"entity:{row['entity_type']}:{row['entity_id']}"
            ),
            "qc_stage": row["qc_stage"],
            "metric_name": row["metric_name"],
            "metric_value": row["metric_value"],
            "metric_numeric": numeric,
            "metric_unit": row.get("metric_unit"),
            "tool": row["tool"],
            "tool_version": row["tool_version"],
            "parameter_set": row.get("parameter_set") or "external",
            "evaluated_at": row.get("evaluated_at") or _now_for_cli(),
        }
        db.insert_qc_result(record)
        count += 1
    print(f"imported {count} external QC metric(s)")
    return 0


def _now_for_cli() -> str:
    from operon.utils import now_iso
    return now_iso()


def _cmd_run_external(args: argparse.Namespace, project: Project, db: Database) -> int:
    import shlex
    from operon.workflow import run_external_command
    argv = shlex.split(args.command_line)
    if not argv:
        raise ValidationError("--command must not be empty")
    record = run_external_command(
        db, project, argv, step=args.step, entity_type=args.entity_type,
        entity_id=args.entity_id, parameter_set=args.parameter_set,
        expected_outputs=args.expected_output, cwd=args.cwd, timeout=args.timeout,
        backend=args.backend,
    )
    print(json.dumps({k: record.get(k) for k in ("run_id", "step", "status", "exit_code", "finished_at")}, ensure_ascii=False))
    return 0


def _cmd_tools_check(project: Project) -> int:
    from operon.tools import print_tools_table
    table, all_ok = print_tools_table(project)
    print(table)
    return 0 if all_ok else 1


def _parse_runtime_parameters(items: list[str]) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValidationError(f"--param must use NAME=VALUE syntax, got {item!r}")
        name, value = item.split("=", 1)
        name = name.strip()
        if not name or not value:
            raise ValidationError(f"--param must use non-empty NAME=VALUE syntax, got {item!r}")
        if name in parameters:
            raise ValidationError(f"duplicate --param {name!r}")
        parameters[name] = value
    return parameters


def _cmd_analyze(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.tools import run_analysis
    results = run_analysis(
        project, db, args.analysis,
        entity_type=args.entity_type, entity_id=args.entity_id,
        dry_run=args.dry_run, force=args.force, limit=args.limit,
        threads=args.threads, backend=args.backend, keep_partial=args.keep_partial,
        runtime_parameters=_parse_runtime_parameters(args.param),
    )
    headers = ["file_id", "entity", "analysis", "status", "tool_version", "output", "error"]
    rows = []
    errors = 0
    planned = 0
    for r in results:
        if r.get("status") in {"error", "failed"}:
            errors += 1
        if r.get("status") == "planned":
            planned += 1
        rows.append([
            r.get("file_id", ""),
            f"{r.get('entity_type', '')} {r.get('entity_id', '')}",
            r.get("analysis", ""),
            r.get("status", ""),
            r.get("tool_version", ""),
            r.get("output", ""),
            r.get("error", ""),
        ])
    print(format_table(headers, rows))
    if args.dry_run:
        print(f"dry-run: {len(results)} planned job(s), {planned} job(s) left. no command executed")
    else:
        print(f"analysis {args.analysis}: {len(results) - errors}/{len(results)} succeeded")
    return 1 if errors else 0


def _cmd_analysis_results(args: argparse.Namespace, db: Database) -> int:
    if args.hits:
        sql = """
            SELECT h.entity_type, h.entity_id, h.analysis_name, h.query_id, h.subject_id,
                   h.metric_name, h.metric_value, h.hit_rank, j.tool_version
            FROM analysis_hits h
            JOIN analysis_jobs j ON j.job_id = h.job_id
            WHERE j.status='completed'
        """
    else:
        sql = """
            SELECT r.entity_type, r.entity_id, r.analysis_name, r.metric_name,
                   r.metric_value, r.metric_unit, j.tool_version, j.job_id
            FROM analysis_results r
            JOIN analysis_jobs j ON j.job_id = r.job_id
            WHERE j.status='completed'
        """
    params: list[Any] = []
    if args.analysis:
        if args.hits:
            sql += " AND h.analysis_name=?"
        else:
            sql += " AND r.analysis_name=?"
        params.append(args.analysis)
    if args.entity_type:
        if args.hits:
            sql += " AND h.entity_type=?"
        else:
            sql += " AND r.entity_type=?"
        params.append(args.entity_type)
    if args.entity_id:
        if args.hits:
            sql += " AND h.entity_id=?"
        else:
            sql += " AND r.entity_id=?"
        params.append(args.entity_id)
    if args.hits:
        sql += " ORDER BY h.entity_id, h.query_id, h.hit_rank, h.metric_name LIMIT ?"
    else:
        sql += " ORDER BY r.entity_id, r.metric_name, j.job_id DESC LIMIT ?"
    params.append(int(args.limit))
    rows = db.conn.execute(sql, params).fetchall()
    if not rows:
        print("(no analysis results)")
    else:
        print(format_table(list(rows[0].keys()), ([r[c] for c in rows[0].keys()] for r in rows)))
    return 0


def _cmd_remotes(args: argparse.Namespace, project: Project) -> int:
    from operon.remotes import check_remote, list_remotes
    names = sorted(list_remotes(project))
    if not names:
        print("no remotes configured; add a 'remotes:' section to project.yaml")
        return 0
    results = [check_remote(project, name) for name in names]
    print(format_table(
        ["name", "type", "address", "root", "files", "status", "error"],
        ([r["name"], r["type"], r["address"], r["root"], r["files"], r["status"], r["error"]] for r in results),
    ))
    return 0 if all(r["status"] == "ok" for r in results) else 1


def _cmd_push(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.remotes import push
    results = push(db, project, args.remote, file_ids=args.file_id or None)
    return _print_sync_results("push", args.remote, results)


def _cmd_pull(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.remotes import pull
    results = pull(db, project, args.remote, file_ids=args.file_id or None)
    return _print_sync_results("pull", args.remote, results)


def _cmd_evict(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.remotes import evict_local
    results = evict_local(db, project, args.remote, file_ids=args.file_id or None)
    return _print_sync_results("evict", args.remote, results)


def _cmd_locations(args: argparse.Namespace, project: Project, db: Database) -> int:
    params: list[Any] = []
    where = ""
    if args.file_id:
        where = f"WHERE f.file_id IN ({', '.join('?' for _ in args.file_id)})"
        params.extend(args.file_id)
    rows = db.conn.execute(
        "SELECT f.file_id, f.relative_path, f.status AS local_status, "
        "COALESCE(l.location_name, '') AS remote, COALESCE(l.status, '') AS remote_status, "
        "COALESCE(l.verified_at, '') AS verified_at "
        "FROM files f LEFT JOIN file_locations l ON l.file_id=f.file_id "
        f"{where} ORDER BY f.file_id, l.location_name",
        params,
    ).fetchall()
    print(format_table(
        ["file_id", "relative_path", "local_status", "remote", "remote_status", "verified_at"],
        ([row[column] for column in row.keys()] for row in rows),
    ))
    return 0


def _print_sync_results(action: str, remote: str, results: list[dict[str, Any]]) -> int:
    print(format_table(["file_id", "relative_path", "status", "error"], (
        [r.get("file_id", ""), r["relative_path"], r["status"], r.get("error") or ""] for r in results
    )))
    errors = sum(1 for r in results if r["status"] == "error")
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = ", ".join(f"{status}: {count}" for status, count in sorted(counts.items()))
    print(f"{action} {remote}: {summary}")
    return 1 if errors else 0


def _reason_list(reason_codes: Any) -> list[str]:
    if isinstance(reason_codes, list):
        return [str(x) for x in reason_codes]
    if isinstance(reason_codes, str):
        try:
            value = json.loads(reason_codes)
            return [str(x) for x in value] if isinstance(value, list) else [reason_codes]
        except json.JSONDecodeError:
            return [reason_codes]
    return []


def _cmd_evaluate(args: argparse.Namespace, project: Project, db: Database) -> int:
    if args.entity_id:
        if not args.entity_type:
            raise ValidationError("--entity-type is required when --entity-id is given")
        result = evaluate_entity(db, project, args.entity_type, args.entity_id, args.profile)
        results = [result]
    else:
        results = evaluate_all(db, project, args.profile, args.entity_type)
    print(format_table(["entity_type", "entity_id", "profile", "decision", "reasons"], (
        [r["entity_type"], r["entity_id"], r["profile"], r["decision"], ", ".join(_reason_list(r["reason_codes"])) or "-"] for r in results
    )))
    return 0


def _cmd_curate(args: argparse.Namespace, project: Project, db: Database) -> int:
    curate_decision(db, args.entity_type, args.entity_id, args.profile, args.decision,
                    reviewer=args.reviewer, reason=args.reason, evidence=args.evidence)
    print(f"recorded curated decision {args.decision} for {args.entity_type} {args.entity_id}")
    return 0


def _cmd_release(args: argparse.Namespace, project: Project, db: Database) -> int:
    link_kind = "copy" if args.copy_files else args.link
    result = create_release(db, project, args.version, args.profile, link_kind=link_kind)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_run_pipeline(args: argparse.Namespace, project: Project, db: Database) -> int:
    print(f"[1/4] ingest {args.source}")
    row = ingest_file(db, project, args.source, args.entity_type, args.entity_id, args.role,
                      fmt=args.fmt, compression=args.compression, source_url=args.source_url)
    print(f"       -> {row['file_id']} {row['sha256'][:16]}...")
    print(f"[2/4] standardize {row['file_id']}")
    result = standardize_file(db, project, row["file_id"])
    print(f"       -> {result['target']}")
    print(f"[3/4] QC {row['file_id']}")
    from operon.qc_module import qc_file
    qc_result = qc_file(db, project, row["file_id"])
    if not qc_result["ok"]:
        print(f"       -> QC FAILED: {qc_result['error']}", file=sys.stderr)
        return 1
    print("       -> metrics written to qc_results")
    print(f"[4/4] evaluate with profile {args.profile or project.config['qc']['default_profile']}")
    decision = evaluate_entity(db, project, args.entity_type, args.entity_id, args.profile)
    reasons = ", ".join(_reason_list(decision["reason_codes"]))
    print(f"       -> {decision['decision']}: {reasons or 'no issues'}")
    return 0


def _cmd_qc_table(args: argparse.Namespace, project: Project, db: Database) -> int:
    print(print_qc_table(db, args.entity_type, args.entity_id))
    if args.export:
        path = export_qc_tsv(db, project, args.entity_type)
        print(f"wrote {path}")
    return 0


def _cmd_decisions(args: argparse.Namespace, project: Project, db: Database) -> int:
    print(print_decisions(db, args.profile))
    return 0


def _cmd_taxonomy(args: argparse.Namespace, project: Project, db: Database) -> int:
    if args.taxonomy_command == "import":
        result = import_ncbi_taxonomy(db, project, args.input, args.version)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.taxonomy_command == "compile":
        result = compile_reference_set(db, project, args.profile, args.taxonomy_version)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.taxonomy_command == "list":
        rows = list_taxonomy_snapshots(db)
        print(format_table(
            ["snapshot_id", "source", "version", "nodes", "status", "sha256", "imported_at"],
            ([
                row["taxonomy_snapshot_id"], row["source"], row["taxonomy_version"],
                row["node_count"], row["status"], row["source_sha256"], row["imported_at"],
            ] for row in rows),
        ))
        return 0
    if args.taxonomy_command == "reference-sets":
        rows = list_reference_sets(db)
        print(format_table(
            ["reference_set", "taxonomy", "profile", "family", "genus", "sha256", "compiled_at"],
            ([
                row["reference_set_id"], row["taxonomy_version"], row["profile_name"],
                row["family_count"], row["genus_count"], row["tsv_sha256"], row["compiled_at"],
            ] for row in rows),
        ))
        return 0
    raise ValidationError(f"unknown taxonomy command {args.taxonomy_command!r}")


def _cmd_report(args: argparse.Namespace, project: Project, db: Database) -> int:
    if args.report_kind == "qc":
        return _cmd_qc_table(args, project, db)
    if args.report_kind == "decisions":
        return _cmd_decisions(args, project, db)
    if args.report_kind == "analysis":
        return _cmd_analysis_results(args, db)
    if args.report_kind == "coverage":
        result = report_coverage(db, project, args.reference_set, release_version=args.release)
        scope_text = result["scope_kind"]
        if result.get("scope_value"):
            scope_text += f":{result['scope_value']}"
        print(f"reference set: {result['reference_set_id']}")
        print(f"scope: {scope_text}")
        print(format_table(
            ["rank", "numerator", "denominator", "coverage_pct", "threshold_pct", "decision"],
            ([
                metric["rank"], metric["numerator"], metric["denominator"],
                f"{float(metric['coverage_percent']):.4f}",
                f"{float(metric['threshold_percent']):.4f}", metric["decision"],
            ] for metric in result["metrics"]),
        ))
        print(f"decision: {result['decision']}")
        print(f"report: {result['path']}")
        return int(result["exit_code"])
    raise ValidationError(f"unknown report kind {args.report_kind!r}")


def _cmd_query(args: argparse.Namespace, db: Database) -> int:
    try:
        rows = db.readonly_query(args.sql)
    except Exception as exc:
        raise ValidationError(f"query failed: {exc}") from exc
    if rows:
        print(format_table(list(rows[0].keys()), ([r[c] for c in rows[0].keys()] for r in rows)))
    else:
        print("(no rows)")
    return 0


def _cmd_set_state(args: argparse.Namespace, db: Database) -> int:
    set_state(db, args.entity_type, args.entity_id, args.state, message=args.message, force=args.force,
              actor=os.environ.get("USER"))
    print(f"{args.entity_type} {args.entity_id} -> {args.state.upper()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return _cmd_init(args)
        if args.command == "init-demo":
            return _cmd_init_demo(args)
        project, db = _open_project(args)
        try:
            handlers = {
                "status": lambda: _cmd_status(args, db),
                "schema": lambda: _cmd_schema(args, project),
                "import-metadata": lambda: _cmd_import_metadata(args, project, db),
                "export-metadata": lambda: _cmd_export_metadata(args, project, db),
                "add": lambda: _cmd_add(args, project, db),
                "add-accession": lambda: _cmd_add_accession(args, project, db),
                "ncbi-datasets": lambda: _cmd_ncbi_datasets(args, project, db),
                "next-id": lambda: _cmd_next_id(args, db),
                "ingest": lambda: _cmd_ingest(args, project, db),
                "verify": lambda: _cmd_verify(args, project, db),
                "standardize": lambda: _cmd_standardize(args, project, db),
                "qc": lambda: _cmd_qc(args, project, db),
                "import-qc": lambda: _cmd_import_qc(args, project, db),
                "run-external": lambda: _cmd_run_external(args, project, db),
                "tools-check": lambda: _cmd_tools_check(project),
                "analyze": lambda: _cmd_analyze(args, project, db),
                "remotes": lambda: _cmd_remotes(args, project),
                "push": lambda: _cmd_push(args, project, db),
                "pull": lambda: _cmd_pull(args, project, db),
                "evict": lambda: _cmd_evict(args, project, db),
                "locations": lambda: _cmd_locations(args, project, db),
                "evaluate": lambda: _cmd_evaluate(args, project, db),
                "curate": lambda: _cmd_curate(args, project, db),
                "release": lambda: _cmd_release(args, project, db),
                "run-pipeline": lambda: _cmd_run_pipeline(args, project, db),
                "taxonomy": lambda: _cmd_taxonomy(args, project, db),
                "report": lambda: _cmd_report(args, project, db),
                "query": lambda: _cmd_query(args, db),
                "set-state": lambda: _cmd_set_state(args, db),
            }
            return handlers[args.command]()
        finally:
            db.close()
    except KeyboardInterrupt:
        # SIGINT/SIGTERM during a batch command (ShutdownRequested included):
        # bookkeeping was already finalized on the way up; just report.
        print("interrupted: progress so far was saved; re-run the same command to resume",
              file=sys.stderr)
        return 130
    except OperonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(f"error: database error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
