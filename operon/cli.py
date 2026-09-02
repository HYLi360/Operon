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
from operon.backup import create_backup, verify_backup
from operon.config import Project, load_project
from operon.coverage import report_coverage
from operon.database import Database
from operon.entity_view import entity_graph
from operon.errors import OperonError, ValidationError
from operon.files import ingest_file, standardize_all, standardize_file, verify_files
from operon.release import create_release
from operon.reports import export_metadata_report, export_qc_tsv, print_decisions, print_qc_table
from operon.rules import curate_decision, evaluate_all, evaluate_entity
from operon.schema import (
    ENTITY_ID_COLUMNS,
    ENTITY_TABLES,
    Schema,
    read_tsv,
)
from operon.table_import import (
    IMPORTABLE_TABLES,
    apply_table_import,
    preview_table_import,
    write_table_template,
)
from operon.taxonomy import (
    compile_reference_set,
    import_ncbi_taxonomy,
    list_reference_sets,
    list_taxonomy_snapshots,
)
from operon.utils import format_table, parse_key_values
from operon.workflow import flush_run_log, log_run, new_run_id, set_state

MANUAL_METADATA_ENTITIES = ["organism", "sample", "run", "assembly", "annotation"]


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


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

    p = sub.add_parser("init-demo",
                       help="initialize a project with synthetic assemblies/annotations/reads and run the pipeline")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--project-id", default="PRJ_DEMO_001")

    p = sub.add_parser("status", help="show entity states")
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")
    p.add_argument("--include-retired", action="store_true")

    p = sub.add_parser("schema", help="show schema path or dump it")
    p.add_argument("--dump", action="store_true")

    sub.add_parser(
        "migrate",
        help="apply additive database schema migrations and report integrity",
    )

    p = sub.add_parser("import", help="import a dataset interactively or load one controlled metadata table")
    import_sub = p.add_subparsers(dest="import_kind", required=True)
    import_sub.add_parser("dataset", help="launch the English interactive dataset-import wizard")
    ip = import_sub.add_parser("table", help="generate or import a CSV/XLSX metadata table")
    ip.add_argument("--table", required=True, choices=IMPORTABLE_TABLES)
    source = ip.add_mutually_exclusive_group(required=True)
    source.add_argument("--template", metavar="OUTPUT", help="write an empty .csv or .xlsx template")
    source.add_argument("--file", metavar="INPUT", help="preview and import an existing .csv or .xlsx file")
    ip.add_argument("--on-conflict", choices=["error", "skip", "update"],
                    help="how to handle existing rows after preview")
    ip.add_argument("--yes", action="store_true", help="apply the preview without an interactive confirmation")

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
    p.add_argument(
        "--resume-run", metavar="WF_ID",
        help="link this attempt to a previous failed/interrupted NCBI import; request must match",
    )
    p.add_argument(
        "--plan-only", action="store_true",
        help="show missing-include download groups without downloading or writing workflow rows",
    )

    p = sub.add_parser(
        "ncbi-reconcile",
        help="preview or apply an audited repair of legacy NCBI adapter duplicates and paired-accession drift",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="apply the freshly computed plan; default is database-only dry-run",
    )
    p.add_argument("--actor", help="actor recorded in changes (default: current user)")

    p = sub.add_parser("next-id", help="allocate the next stable internal ID for an entity type")
    p.add_argument("entity_type", choices=["organism", "sample", "run", "assembly", "annotation", "file"])

    p = sub.add_parser("ingest",
                       help="archive a file or directory (local path, sftp:// or remote:// URL) into raw/ with a content hash and manifest record")
    p.add_argument("--source", required=True,
                   help="local path, sftp://[user@]host[:port]/path, or remote://<name>/<path>")
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
    p.add_argument("--sample-size", type=_positive_int, default=1000000)
    p.add_argument(
        "--phred-offset", choices=["33", "64", "auto"], default="33",
        help="FASTQ quality offset (default: 33; auto assumes 33 when ambiguous)",
    )
    p.add_argument(
        "--rehash", action="store_true",
        help="ignore the unchanged-file verification cache and recompute every input SHA-256",
    )

    p = sub.add_parser("import-qc", help="import external QC metrics (e.g. BUSCO, QUAST, FastQC) from TSV")
    p.add_argument("--file", dest="tsv_file", required=True)

    p = sub.add_parser("run-external",
                       help="run an external tool with structured provenance (stdout/stderr, exit code, expected outputs)")
    p.add_argument("--step", required=True, help="workflow step name, e.g. busco / quast / fastp")
    p.add_argument("--command", dest="command_line", required=True,
                   help="quoted command line, e.g. 'busco -i in.fa -o out -m genome'")
    p.add_argument("--entity-type")
    p.add_argument("--entity-id")
    p.add_argument("--parameter-set")
    p.add_argument("--tool", help="tool name from config/tools.yaml; its version is detected and recorded")
    p.add_argument("--input", dest="inputs", action="append", default=[],
                   help="declared input file/directory; hashed into the run's input_sha256")
    p.add_argument("--threads", type=int, help="threads requested from the execution backend")
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

    p = sub.add_parser("push",
                       help="upload manifest files to a configured remote mirror (SFTP, checksum-verified, idempotent)")
    p.add_argument("--remote", required=True, help="remote name from the project.yaml remotes: section")
    p.add_argument("--file-id", action="append", default=[], help="restrict to these manifest files (default: all)")

    p = sub.add_parser("evict", help="remove local bytes after an exact remote mirror copy is verified")
    p.add_argument("--remote", required=True, help="remote name holding the verified copy")
    p.add_argument("--file-id", action="append", default=[], help="restrict to these manifest files (default: all)")

    p = sub.add_parser("locations", help="show local/remote residency for manifest files")
    p.add_argument("--file-id", action="append", default=[], help="restrict to these manifest files")

    p = sub.add_parser("pull", help="restore manifest files from a configured remote mirror (SFTP, checksum-verified)")
    p.add_argument("--remote", required=True, help="remote name from the project.yaml remotes: section")
    p.add_argument("--file-id", action="append", default=[],
                   help="restrict to these manifest files (default: everything in the remote manifest)")

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

    p = sub.add_parser("export",
                       help="materialize a filtered subset of manifest files with manifest/QC/provenance sidecars")
    p.add_argument("--output", required=True,
                   help="output directory (must not exist or must be empty; never overwritten)")
    p.add_argument("--entity-type")
    p.add_argument("--entity-id", action="append", default=[])
    p.add_argument("--file-id", action="append", default=[])
    p.add_argument("--file-role")
    p.add_argument("--format", dest="fmt")
    p.add_argument("--state", help="only entities currently in this workflow state")
    p.add_argument("--decision", help="only entities with this effective decision (requires --profile)")
    p.add_argument("--profile", help="QC profile used together with --decision")
    p.add_argument("--link", choices=["copy", "hardlink", "symlink"], default="copy",
                   help="export file storage mode (default: copy)")
    p.add_argument("--no-qc", action="store_true", help="skip the qc.tsv metrics snapshot")

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
    rp.add_argument("--include-retired", action="store_true")
    rp = report_sub.add_parser("decisions", help="show current QC decisions")
    rp.add_argument("--profile")
    rp.add_argument("--include-retired", action="store_true")
    rp = report_sub.add_parser("analysis", help="show synchronized analysis summaries or hits")
    rp.add_argument("--analysis")
    rp.add_argument("--entity-type")
    rp.add_argument("--entity-id")
    rp.add_argument("--hits", action="store_true", help="show top-hit rows instead of job summaries")
    rp.add_argument("--limit", type=int, default=20)
    rp.add_argument("--include-retired", action="store_true")
    rp = report_sub.add_parser("coverage", help="measure NCBI family/genus coverage against a frozen reference set")
    rp.add_argument("--reference-set", required=True)
    scope = rp.add_mutually_exclusive_group()
    scope.add_argument("--scope", choices=["metadata"], default="metadata")
    scope.add_argument("--release", help="restrict observations to one immutable release")
    rp = report_sub.add_parser("metadata", help="export a derived read-only metadata TSV snapshot")
    rp.add_argument("--output", help="output directory (default: reports/metadata)")
    rp.add_argument("--include-retired", action="store_true")

    p = sub.add_parser("query", help="run arbitrary read-only SQL against the file database")
    p.add_argument("sql")

    p = sub.add_parser("show", help="show a matched entity lineage and its descendants")
    p.add_argument("identifier", help="internal ID, accession, or NAMESPACE:ACCESSION")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.add_argument(
        "--scope", choices=["matched", "organism"], default="matched",
        help="display only the matched lineage/subtree, or the complete organism graph",
    )
    p.add_argument(
        "--include-superseded", action="store_true",
        help="include logically superseded descendant entities and their files",
    )
    p.add_argument(
        "--include-retired", action="store_true",
        help="include logically retired descendants and their files",
    )

    p = sub.add_parser(
        "retire", help="preview or apply an audited logical entity retirement",
    )
    p.add_argument("identifier", help="internal ID, accession, or NAMESPACE:ACCESSION")
    p.add_argument(
        "--reason-code", required=True,
        choices=[
            "accidental_import", "wrong_source", "duplicate", "withdrawn_upstream",
            "policy_exclusion", "metadata_error", "other",
        ],
    )
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence")
    p.add_argument("--actor")
    p.add_argument("--apply", action="store_true", help="append the RETIRE event")
    p.add_argument("--yes", action="store_true", help="apply without interactive confirmation")

    p = sub.add_parser(
        "restore", help="preview or reverse a target's direct logical retirement",
    )
    p.add_argument("identifier", help="internal ID, accession, or NAMESPACE:ACCESSION")
    p.add_argument("--reason", required=True)
    p.add_argument("--evidence")
    p.add_argument("--actor")
    p.add_argument("--apply", action="store_true", help="append the RESTORE event")
    p.add_argument("--yes", action="store_true", help="apply without interactive confirmation")

    p = sub.add_parser("retired", help="list current logical retirements")
    p.add_argument("--direct-only", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("backup", help="create or verify a checksum-manifested project backup")
    backup_sub = p.add_subparsers(dest="backup_command", required=True)
    bp = backup_sub.add_parser("create", help="create a consistent SQLite-centered backup")
    bp.add_argument("--output", required=True)
    bp.add_argument("--scope", choices=["control", "results", "full"], default="control")
    bp = backup_sub.add_parser("verify", help="verify every file in an existing backup")
    bp.add_argument("--input", required=True)

    p = sub.add_parser("set-state",
                       help="manually set workflow state (audited; use --force for non-standard transition)")
    p.add_argument("--entity-type", required=True)
    p.add_argument("--entity-id", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--message")
    p.add_argument("--force", action="store_true")

    return parser


def _open_project(args: argparse.Namespace) -> tuple[Project, Database]:
    project = load_project(args.project)
    read_only = (
        args.command == "query"
        or args.command == "show"
        or args.command == "retired"
        or args.command == "status"
        or (
            args.command == "report"
            and args.report_kind in {"qc", "decisions", "analysis", "metadata"}
        )
        or (args.command in {"retire", "restore"} and not args.apply)
        or (args.command == "backup" and args.backup_command == "create")
        or (args.command == "ncbi-datasets" and (args.dry_run or args.plan_only))
        or (args.command == "ncbi-reconcile" and not args.apply)
    )
    db = Database(project.db_path, read_only=read_only)
    return project, db


def _print_table(headers: list[str], rows: list[list[Any]]) -> None:
    print(format_table(headers, rows))


def _cmd_init(args: argparse.Namespace) -> int:
    project = Project.init(args.path, project_id=args.project_id, name=args.name)
    print(f"initialized Operon project {project.project_id} at {project.root}")
    print("next steps:")
    print("  1. operon import dataset")
    print("  2. operon import table --table samples --template samples.xlsx")
    print("  3. operon report metadata")
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
    if (
        not getattr(args, "include_retired", False)
        and db.lifecycle_schema_available()
    ):
        sql += (
            " AND NOT EXISTS (SELECT 1 FROM effective_retired_entities r "
            "WHERE r.entity_type=entity_state.entity_type "
            "AND r.entity_id=entity_state.entity_id)"
        )
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
        print(format_table(["entity_type", "entity_id", "state", "message", "updated_at"],
                           ([r[c] for c in r.keys()] for r in rows)))
    return 0


def _cmd_schema(args: argparse.Namespace, project: Project) -> int:
    if args.dump:
        print(project.schema_path.read_text(encoding="utf-8"))
    else:
        print(project.schema_path)
    return 0


def _cmd_migrate(db: Database) -> int:
    from operon.database import SCHEMA_VERSION
    integrity = str(db.query("PRAGMA integrity_check")[0][0])
    foreign_key_violations = len(db.query("PRAGMA foreign_key_check"))
    migrations = [dict(row) for row in db.query(
        "SELECT migration_id, migration_sha256, applied_at, workflow_run_id "
        "FROM schema_migrations ORDER BY applied_at, migration_id"
    )]
    result = {
        "schema_version": SCHEMA_VERSION,
        "integrity_check": integrity,
        "foreign_key_violations": foreign_key_violations,
        "migrations": migrations,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if integrity == "ok" and foreign_key_violations == 0 else 1


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
            print(
                f"warning: unknown field {extra!r} for {entity_type}; add it to {project.schema_path} to remove this warning",
                file=sys.stderr)
    normalized, _ = schema.validate_and_normalize(table, [row])
    row = normalized[0]
    _check_fks_for_row(db, entity_type, row, require_target=True)
    db.insert_row(table, row)
    db.set_entity_state(entity_type, record_id, "METADATA_VALIDATED", "record added via CLI and schema-validated")
    db.record_change(entity_type, record_id, None, None, json.dumps({k: str(v) for k, v in row.items()}),
                     "record added", actor=os.environ.get("USER"))
    print(f"added {entity_type} {record_id}")
    return 0


def _check_fks_for_row(db: Database, entity_type: str, row: dict[str, Any], require_target: bool) -> None:
    if entity_type == "sample" and row.get("organism_id"):
        db.require_active_entity("organism", row["organism_id"])
    elif entity_type == "run" and row.get("sample_id"):
        db.require_active_entity("sample", row["sample_id"])
    elif entity_type == "assembly" and row.get("sample_id"):
        db.require_active_entity("sample", row["sample_id"])
    elif entity_type == "annotation" and row.get("assembly_id"):
        db.require_active_entity("assembly", row["assembly_id"])
    for field in ("fasta_file_id", "gff_file_id", "cds_file_id", "protein_file_id"):
        if row.get(field) and db.conn.execute("SELECT 1 FROM files WHERE file_id=?", (row[field],)).fetchone() is None:
            raise ValidationError(
                f"{entity_type} {row.get(ENTITY_ID_COLUMNS.get(entity_type, 'id'))}: {field} {row[field]} does not exist")


def _cmd_add_accession(args: argparse.Namespace, project: Project, db: Database) -> int:
    db.require_active_entity(args.internal_type, args.internal_id)
    row = {
        "internal_type": args.internal_type,
        "internal_id": args.internal_id,
        "namespace": args.namespace,
        "accession": args.accession,
        "version": args.acc_version,
        "is_primary": 1 if args.primary else None,
    }
    db.insert_row("accessions", row)
    db.record_change(
        "accession", f"{args.namespace}:{args.accession}", None, None,
        json.dumps(row, ensure_ascii=False, sort_keys=True), "accession added",
        actor=os.environ.get("USER"),
    )
    print(f"mapped {args.namespace}:{args.accession} -> {args.internal_type} {args.internal_id}")
    return 0


def _cmd_ncbi_datasets(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.adapters.ncbi_datasets import DEFAULT_INCLUDES, run_ncbi_datasets_adapter
    from operon.shutdown import graceful_shutdown

    with graceful_shutdown():
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
            resume_run_id=args.resume_run,
            plan_only=args.plan_only,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_ncbi_reconcile(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.ncbi_reconcile import apply_ncbi_reconciliation, plan_ncbi_reconciliation
    result = (
        apply_ncbi_reconciliation(db, project, actor=args.actor)
        if args.apply else plan_ncbi_reconciliation(db)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if not args.apply:
        print(
            "dry-run: no business row or file was changed; rerun with --apply after review",
            file=sys.stderr,
        )
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
    results = qc_all(
        db, project, entity_type=args.entity_type, entity_id=args.entity_id,
        file_id=args.file_id, sample_size=args.sample_size,
        phred_offset=args.phred_offset, force_checksum=args.rehash,
    )
    ok = sum(1 for r in results if r["ok"])
    for r in results:
        if not r["ok"]:
            print(f"{r['file_id']}: FAILED {r['error']}", file=sys.stderr)
    print(f"QC complete: {ok}/{len(results)} file(s) passed built-in stages")
    return 0 if ok == len(results) else 1


def _cmd_import_qc(args: argparse.Namespace, project: Project, db: Database) -> int:
    rows = read_tsv(args.tsv_file)
    required = ["entity_type", "entity_id", "qc_stage", "metric_name", "metric_value", "tool", "tool_version",
                "parameter_set"]
    missing = [c for c in required if not rows or c not in rows[0]]
    if missing:
        raise ValidationError(f"{args.tsv_file}: missing columns {missing}")
    count = 0
    for row in rows:
        db.require_active_entity(row["entity_type"], row["entity_id"])
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
    tool_version = None
    extra_details: dict[str, Any] | None = None
    if args.tool:
        from operon.tools import detect_tool_version_record, get_tool, load_tools_config
        try:
            tool_spec = get_tool(project, args.tool)
        except ValidationError:
            tool_spec = None  # unconfigured name: record the name, no version
        if tool_spec is not None:
            try:
                config = load_tools_config(project)
                tool_version, raw_output = detect_tool_version_record(tool_spec, config)
                extra_details = {"tool_version_raw": raw_output}
            except Exception as exc:
                # Version detection must never block the actual command.
                print(f"warning: version detection for {args.tool!r} failed: {exc}",
                      file=sys.stderr)
    record = run_external_command(
        db, project, argv, step=args.step, entity_type=args.entity_type,
        entity_id=args.entity_id, parameter_set=args.parameter_set,
        expected_outputs=args.expected_output, cwd=args.cwd, timeout=args.timeout,
        tool=args.tool, tool_version=tool_version,
        backend=args.backend, threads=args.threads, inputs=args.inputs,
        extra_details=extra_details,
    )
    print(json.dumps({k: record.get(k) for k in ("run_id", "step", "status", "exit_code", "finished_at")},
                     ensure_ascii=False))
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
        if (
            not getattr(args, "include_retired", False)
            and db.lifecycle_schema_available()
        ):
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM effective_retired_entities er "
                "WHERE er.entity_type=h.entity_type AND er.entity_id=h.entity_id)"
            )
    else:
        sql = """
            SELECT r.entity_type, r.entity_id, r.analysis_name, r.metric_name,
                   r.metric_value, r.metric_unit, j.tool_version, j.job_id
            FROM analysis_results r
            JOIN analysis_jobs j ON j.job_id = r.job_id
            WHERE j.status='completed'
        """
        if (
            not getattr(args, "include_retired", False)
            and db.lifecycle_schema_available()
        ):
            sql += (
                " AND NOT EXISTS (SELECT 1 FROM effective_retired_entities er "
                "WHERE er.entity_type=r.entity_type AND er.entity_id=r.entity_id)"
            )
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
        [r["entity_type"], r["entity_id"], r["profile"], r["decision"],
         ", ".join(_reason_list(r["reason_codes"])) or "-"] for r in results
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


def _cmd_export(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.export import export_files
    summary = export_files(
        db, project, output_dir=args.output,
        entity_type=args.entity_type, entity_ids=args.entity_id, file_ids=args.file_id,
        file_role=args.file_role, fmt=args.fmt, state=args.state,
        decision=args.decision, profile=args.profile,
        link_kind=args.link, include_qc=not args.no_qc,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
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
    include_retired = getattr(args, "include_retired", False)
    if include_retired:
        table = print_qc_table(
            db, args.entity_type, args.entity_id, include_retired=True,
        )
    else:
        table = print_qc_table(db, args.entity_type, args.entity_id)
    print(table)
    if args.export:
        path = (
            export_qc_tsv(db, project, args.entity_type, include_retired=True)
            if include_retired else export_qc_tsv(db, project, args.entity_type)
        )
        print(f"wrote {path}")
    return 0


def _cmd_decisions(args: argparse.Namespace, project: Project, db: Database) -> int:
    if getattr(args, "include_retired", False):
        output = print_decisions(db, args.profile, include_retired=True)
    else:
        output = print_decisions(db, args.profile)
    print(output)
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
    if args.report_kind == "metadata":
        path = (
            export_metadata_report(db, project, args.output, include_retired=True)
            if getattr(args, "include_retired", False)
            else export_metadata_report(db, project, args.output)
        )
        print(f"wrote metadata report to {path}")
        return 0
    raise ValidationError(f"unknown report kind {args.report_kind!r}")


def _cmd_import(args: argparse.Namespace, project: Project, db: Database) -> int:
    if args.import_kind == "dataset":
        from operon.import_wizard import run_dataset_wizard
        result = run_dataset_wizard(db, project)
        if result is None:
            print("Import cancelled; no project data was changed.")
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.import_kind != "table":
        raise ValidationError(f"unknown import kind {args.import_kind!r}")
    schema = Schema.from_file(project.schema_path)
    if args.template:
        path = write_table_template(schema, args.table, args.template)
        print(f"wrote {args.table} template to {path}")
        return 0
    preview = preview_table_import(db, schema, args.table, args.file)
    print(format_table(
        ["key", "action", "changed_fields"],
        ([": ".join(str(value) for value in item["key"]), item["action"], ", ".join(item["differences"])]
         for item in preview["items"]),
    ))
    print(
        f"preview: {preview['insert']} insert, {preview['update']} update, "
        f"{preview['unchanged']} unchanged"
    )
    on_conflict = args.on_conflict
    if preview["update"] and on_conflict is None:
        if args.yes:
            raise ValidationError("--on-conflict is required with --yes when existing rows would change")
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValidationError("existing rows would change; pass --on-conflict error, skip or update")
        import questionary
        on_conflict = questionary.select(
            "Existing rows differ. How should they be handled?",
            choices=[
                questionary.Choice("Cancel", value="cancel"),
                questionary.Choice("Skip existing rows", value="skip"),
                questionary.Choice("Update existing rows (audited)", value="update"),
            ],
        ).ask()
        if on_conflict in {None, "cancel"}:
            print("Import cancelled; no rows were changed.")
            return 0
    on_conflict = on_conflict or "error"
    if not args.yes:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValidationError("table import requires --yes outside an interactive terminal")
        import questionary
        confirmed = questionary.confirm(
            f"Apply this {args.table} import with on-conflict={on_conflict}?", default=False
        ).ask()
        if not confirmed:
            print("Import cancelled; no rows were changed.")
            return 0
    result = apply_table_import(
        db, schema, preview, on_conflict=on_conflict, actor=os.environ.get("USER")
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_show(args: argparse.Namespace, db: Database) -> int:
    graph = entity_graph(
        db,
        args.identifier,
        scope=getattr(args, "scope", "matched"),
        include_superseded=getattr(args, "include_superseded", False),
        include_retired=getattr(args, "include_retired", False),
    )
    if args.json:
        print(json.dumps(graph, ensure_ascii=False, indent=2))
        return 0
    organism = graph["organism"]
    print(f"Organism: {organism['organism_id']}  {organism['scientific_name']}")
    print(f"Matched:  {graph['matched']['entity_type']} {graph['matched']['entity_id']}")
    print(f"Scope:    {graph['scope']}")
    accession_rows = [row for row in graph["accessions"] if row["internal_id"] == organism["organism_id"]]
    if accession_rows:
        print("Accessions: " + ", ".join(f"{row['namespace']}:{row['accession']}" for row in accession_rows))
    sections = [
        ("Sources", graph["sources"], [
            "source_id", "source_type", "database_name", "provider", "citation", "license_name"
        ]),
        ("Samples", graph["samples"], ["sample_id", "isolate", "strain", "biosample_accession"]),
        ("Runs", graph["runs"], ["run_id", "sample_id", "run_accession", "platform", "instrument_model"]),
        ("Assemblies", graph["assemblies"],
         ["assembly_id", "sample_id", "assembly_accession", "assembly_name", "assembly_level"]),
        ("Annotations", graph["annotations"],
         ["annotation_id", "assembly_id", "annotation_source", "annotation_version"]),
        ("Files", graph["files"], ["file_id", "entity_type", "entity_id", "file_role", "status", "relative_path"]),
    ]
    if graph["supersessions"]:
        sections.append(("Supersessions", graph["supersessions"], [
            "object_type", "object_id", "superseded_by_type", "superseded_by_id",
            "reason", "superseded_at",
        ]))
    if graph["retirements"]:
        sections.append(("Retirements", graph["retirements"], [
            "entity_type", "entity_id", "retired_by_type", "retired_by_id",
            "reason_code", "reason", "actor", "retired_at",
        ]))
    for title, rows, columns in sections:
        print(f"\n{title} ({len(rows)})")
        print(format_table(columns, ([row.get(column) for column in columns] for row in rows)) if rows else "(none)")
    return 0


def _cmd_lifecycle(args: argparse.Namespace, project: Project, db: Database) -> int:
    from operon.lifecycle import apply_lifecycle_event, lifecycle_plan
    from operon.utils import now_iso

    action = "RETIRE" if args.command == "retire" else "RESTORE"
    plan = lifecycle_plan(db, args.identifier, action=action)
    if not args.apply:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if not plan["will_change"]:
        if plan["blocker"]:
            raise ValidationError(plan["blocker"])
        print(json.dumps({**plan, "applied": False}, ensure_ascii=False, indent=2))
        return 0
    if not args.yes:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValidationError(
                f"{args.command} --apply requires --yes outside an interactive terminal"
            )
        import questionary
        target = plan["target"]
        confirmed = questionary.confirm(
            f"Apply {action} to {target['entity_type']} {target['entity_id']}? ",
            default=False,
        ).ask()
        if not confirmed:
            print(f"{args.command.capitalize()} cancelled; no rows were changed.")
            return 0
    actor = (args.actor or os.environ.get("USER") or "").strip()
    if not actor:
        raise ValidationError("--actor is required when USER is not set")
    target = plan["target"]
    run_id = new_run_id()
    started_at = now_iso()
    jsonl_buffer: list[dict[str, Any]] = []
    with db.transaction():
        result = apply_lifecycle_event(
            db,
            target["entity_type"],
            target["entity_id"],
            action=action,
            reason=args.reason,
            reason_code=getattr(args, "reason_code", None),
            evidence=args.evidence,
            actor=actor,
            workflow_run_id=run_id,
        )
        log_run(
            db,
            project,
            {
                "run_id": run_id,
                "entity_type": target["entity_type"],
                "entity_id": target["entity_id"],
                "step": f"lifecycle_{action.lower()}",
                "status": "completed",
                "started_at": started_at,
                "finished_at": now_iso(),
                "exit_code": 0,
                "command": f"operon {args.command} {args.identifier}",
                "tool": "operon",
                "parameter_set": json.dumps({
                    "reason_code": getattr(args, "reason_code", "manual_restore"),
                    "reason": args.reason,
                    "actor": actor,
                }, ensure_ascii=False, sort_keys=True),
                "execution_details": json.dumps({
                    "entity_counts": plan["entity_counts"],
                    "reference_counts": plan["reference_counts"],
                    "physical_changes": plan["physical_changes"],
                }, ensure_ascii=False, sort_keys=True),
            },
            jsonl_buffer=jsonl_buffer,
        )
    flush_run_log(project, jsonl_buffer)
    refreshed = lifecycle_plan(db, args.identifier, action=action)
    print(json.dumps({
        "applied": True,
        "action": action,
        "target": target,
        "event": result["event"],
        "effectively_retired": db.is_entity_retired(
            target["entity_type"], target["entity_id"]
        ),
        "plan": refreshed,
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_retired(args: argparse.Namespace, db: Database) -> int:
    from operon.lifecycle import list_retired_entities

    rows = list_retired_entities(db, direct_only=args.direct_only)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif not rows:
        print("no retired entities")
    else:
        columns = [
            "entity_type", "entity_id", "retired_by_type", "retired_by_id",
            "reason_code", "reason", "actor", "retired_at",
        ]
        print(format_table(
            columns,
            ([row.get(column) for column in columns] for row in rows),
        ))
    return 0


def _cmd_backup(args: argparse.Namespace, project: Project, db: Database) -> int:
    result = create_backup(db, project, args.output, scope=args.scope)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_backup_verify(args: argparse.Namespace) -> int:
    result = verify_backup(args.input)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


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
    db.require_active_entity(args.entity_type, args.entity_id)
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
        if args.command == "backup" and args.backup_command == "verify":
            return _cmd_backup_verify(args)
        project, db = _open_project(args)
        try:
            handlers = {
                "status": lambda: _cmd_status(args, db),
                "schema": lambda: _cmd_schema(args, project),
                "migrate": lambda: _cmd_migrate(db),
                "import": lambda: _cmd_import(args, project, db),
                "add": lambda: _cmd_add(args, project, db),
                "add-accession": lambda: _cmd_add_accession(args, project, db),
                "ncbi-datasets": lambda: _cmd_ncbi_datasets(args, project, db),
                "ncbi-reconcile": lambda: _cmd_ncbi_reconcile(args, project, db),
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
                "export": lambda: _cmd_export(args, project, db),
                "run-pipeline": lambda: _cmd_run_pipeline(args, project, db),
                "taxonomy": lambda: _cmd_taxonomy(args, project, db),
                "report": lambda: _cmd_report(args, project, db),
                "query": lambda: _cmd_query(args, db),
                "show": lambda: _cmd_show(args, db),
                "retire": lambda: _cmd_lifecycle(args, project, db),
                "restore": lambda: _cmd_lifecycle(args, project, db),
                "retired": lambda: _cmd_retired(args, db),
                "backup": lambda: _cmd_backup(args, project, db),
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
