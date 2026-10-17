#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Command line interface for Operon."""

from __future__ import annotations

import argparse
import sqlite3
import sys

from operon.cli._common import (
    MANUAL_METADATA_ENTITIES,
    ShowOperonVersion,
    _nonnegative_int,
    _open_project,
    _positive_int,
    _print_snapshot_document,
    _print_table,
    _workflow_time,
)
from operon.cli._config import (
    _cmd_config,
    _cmd_config_secret,
    _config_check,
    _config_value_text,
    add_config_parser,
)
from operon.cli._decisions import (
    _cmd_curate,
    _cmd_evaluate,
    _cmd_profiles,
    _cmd_run_pipeline,
    _confirm_curated_evaluation,
    add_curate_parser,
    add_evaluate_parser,
    add_profiles_parser,
    add_run_pipeline_parser,
)
from operon.cli._files import (
    _cmd_backup,
    _cmd_backup_verify,
    _cmd_ingest,
    _cmd_recompress,
    _cmd_standardize,
    _cmd_verify,
    add_backup_parser,
    add_ingest_parser,
    add_recompress_parser,
    add_standardize_parser,
    add_verify_parser,
)
from operon.cli._inspect import (
    _cmd_lifecycle,
    _cmd_retired,
    _cmd_show,
    _cmd_workflow,
    _compact_workflow_value,
    _environment_summary_text,
    _print_workflow_field,
    _print_workflow_section,
    _workflow_duration,
    _workflow_json_record,
    _workflow_started_local,
    add_restore_parser,
    add_retire_parser,
    add_retired_parser,
    add_show_parser,
    add_workflow_parser,
)
from operon.cli._metadata import (
    _cmd_add,
    _cmd_add_accession,
    _cmd_import,
    add_add_accession_parser,
    add_add_parser,
    add_import_parser,
)
from operon.cli._ncbi import (
    _cmd_ncbi_datasets,
    _cmd_ncbi_reconcile,
    add_ncbi_datasets_parser,
    add_ncbi_reconcile_parser,
)
from operon.cli._project import (
    _cmd_doctor,
    _cmd_init,
    _cmd_init_demo,
    _cmd_migrate,
    _cmd_next_id,
    _cmd_query,
    _cmd_schema,
    _cmd_set_state,
    _cmd_status,
    _cmd_tui,
    add_doctor_parser,
    add_init_demo_parser,
    add_init_parser,
    add_migrate_parser,
    add_next_id_parser,
    add_query_parser,
    add_schema_parser,
    add_set_state_parser,
    add_status_parser,
    add_tui_parser,
)
from operon.cli._qc import (
    _cmd_alignment_qc,
    _cmd_import_events,
    _cmd_import_hits,
    _cmd_import_qc,
    _cmd_qc,
    _cmd_qc_measure,
    _now_for_cli,
    add_alignment_qc_parser,
    add_import_events_parser,
    add_import_hits_parser,
    add_import_qc_parser,
    add_qc_measure_parser,
    add_qc_parser,
)
from operon.cli._release import (
    _cmd_adopt,
    _cmd_export,
    _cmd_release,
    add_adopt_parser,
    add_export_parser,
    add_release_parser,
)
from operon.cli._remotes import (
    _cmd_evict,
    _cmd_locations,
    _cmd_pull,
    _cmd_push,
    _cmd_remotes,
    _print_sync_results,
    add_evict_parser,
    add_locations_parser,
    add_pull_parser,
    add_push_parser,
    add_remotes_parser,
)
from operon.cli._report import (
    _cmd_analysis_results,
    _cmd_decisions,
    _cmd_qc_table,
    _cmd_report,
    _cmd_taxonomy,
    add_report_parser,
    add_taxonomy_parser,
)
from operon.cli._sequences import (
    _cmd_classify_sequences,
    _cmd_extract_domains,
    _cmd_fanout,
    _cmd_select_sequences,
    add_classify_sequences_parser,
    add_extract_domains_parser,
    add_fanout_parser,
    add_select_sequences_parser,
)
from operon.cli._tools import (
    _cmd_analyze,
    _cmd_environments,
    _cmd_recipes,
    _cmd_run_external,
    _cmd_tools,
    _cmd_tools_check,
    _parse_runtime_parameters,
    add_analyze_parser,
    add_environments_parser,
    add_recipes_parser,
    add_run_external_parser,
    add_tools_check_parser,
    add_tools_parser,
)
from operon.errors import OperonError
from operon.schema import Schema

__all__ = [
    "MANUAL_METADATA_ENTITIES",
    "Schema",
    "ShowOperonVersion",
    "_cmd_add",
    "_cmd_add_accession",
    "_cmd_adopt",
    "_cmd_alignment_qc",
    "_cmd_analysis_results",
    "_cmd_analyze",
    "_cmd_backup",
    "_cmd_backup_verify",
    "_cmd_classify_sequences",
    "_cmd_config",
    "_cmd_config_secret",
    "_cmd_curate",
    "_cmd_decisions",
    "_cmd_doctor",
    "_cmd_environments",
    "_cmd_evaluate",
    "_cmd_evict",
    "_cmd_export",
    "_cmd_extract_domains",
    "_cmd_fanout",
    "_cmd_import",
    "_cmd_import_events",
    "_cmd_import_hits",
    "_cmd_import_qc",
    "_cmd_ingest",
    "_cmd_init",
    "_cmd_init_demo",
    "_cmd_lifecycle",
    "_cmd_locations",
    "_cmd_migrate",
    "_cmd_ncbi_datasets",
    "_cmd_ncbi_reconcile",
    "_cmd_next_id",
    "_cmd_profiles",
    "_cmd_pull",
    "_cmd_push",
    "_cmd_qc",
    "_cmd_qc_measure",
    "_cmd_qc_table",
    "_cmd_query",
    "_cmd_recipes",
    "_cmd_recompress",
    "_cmd_release",
    "_cmd_remotes",
    "_cmd_report",
    "_cmd_retired",
    "_cmd_run_external",
    "_cmd_run_pipeline",
    "_cmd_schema",
    "_cmd_select_sequences",
    "_cmd_set_state",
    "_cmd_show",
    "_cmd_standardize",
    "_cmd_status",
    "_cmd_taxonomy",
    "_cmd_tools",
    "_cmd_tools_check",
    "_cmd_tui",
    "_cmd_verify",
    "_cmd_workflow",
    "_compact_workflow_value",
    "_config_check",
    "_config_value_text",
    "_confirm_curated_evaluation",
    "_environment_summary_text",
    "_nonnegative_int",
    "_now_for_cli",
    "_open_project",
    "_parse_runtime_parameters",
    "_positive_int",
    "_print_snapshot_document",
    "_print_sync_results",
    "_print_table",
    "_print_workflow_field",
    "_print_workflow_section",
    "_workflow_duration",
    "_workflow_json_record",
    "_workflow_started_local",
    "_workflow_time",
    "add_add_accession_parser",
    "add_add_parser",
    "add_adopt_parser",
    "add_alignment_qc_parser",
    "add_analyze_parser",
    "add_backup_parser",
    "add_classify_sequences_parser",
    "add_config_parser",
    "add_curate_parser",
    "add_doctor_parser",
    "add_environments_parser",
    "add_evaluate_parser",
    "add_evict_parser",
    "add_export_parser",
    "add_extract_domains_parser",
    "add_fanout_parser",
    "add_import_events_parser",
    "add_import_hits_parser",
    "add_import_parser",
    "add_import_qc_parser",
    "add_ingest_parser",
    "add_init_demo_parser",
    "add_init_parser",
    "add_locations_parser",
    "add_migrate_parser",
    "add_ncbi_datasets_parser",
    "add_ncbi_reconcile_parser",
    "add_next_id_parser",
    "add_profiles_parser",
    "add_pull_parser",
    "add_push_parser",
    "add_qc_measure_parser",
    "add_qc_parser",
    "add_query_parser",
    "add_recipes_parser",
    "add_recompress_parser",
    "add_release_parser",
    "add_remotes_parser",
    "add_report_parser",
    "add_restore_parser",
    "add_retire_parser",
    "add_retired_parser",
    "add_run_external_parser",
    "add_run_pipeline_parser",
    "add_schema_parser",
    "add_select_sequences_parser",
    "add_set_state_parser",
    "add_show_parser",
    "add_standardize_parser",
    "add_status_parser",
    "add_taxonomy_parser",
    "add_tools_check_parser",
    "add_tools_parser",
    "add_tui_parser",
    "add_verify_parser",
    "add_workflow_parser",
    "main",
]

_PARSER_BUILDERS = (
    add_init_parser,
    add_init_demo_parser,
    add_status_parser,
    add_doctor_parser,
    add_schema_parser,
    add_migrate_parser,
    add_import_parser,
    add_add_parser,
    add_add_accession_parser,
    add_ncbi_datasets_parser,
    add_ncbi_reconcile_parser,
    add_next_id_parser,
    add_ingest_parser,
    add_verify_parser,
    add_standardize_parser,
    add_recompress_parser,
    add_qc_parser,
    add_import_qc_parser,
    add_import_hits_parser,
    add_import_events_parser,
    add_qc_measure_parser,
    add_alignment_qc_parser,
    add_run_external_parser,
    add_tools_parser,
    add_tools_check_parser,
    add_analyze_parser,
    add_extract_domains_parser,
    add_select_sequences_parser,
    add_classify_sequences_parser,
    add_remotes_parser,
    add_push_parser,
    add_evict_parser,
    add_locations_parser,
    add_pull_parser,
    add_evaluate_parser,
    add_curate_parser,
    add_release_parser,
    add_export_parser,
    add_adopt_parser,
    add_fanout_parser,
    add_environments_parser,
    add_recipes_parser,
    add_profiles_parser,
    add_run_pipeline_parser,
    add_taxonomy_parser,
    add_report_parser,
    add_workflow_parser,
    add_query_parser,
    add_show_parser,
    add_retire_parser,
    add_restore_parser,
    add_retired_parser,
    add_backup_parser,
    add_set_state_parser,
    add_config_parser,
    add_tui_parser,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="operon",
        description="Operon the Database System: Archive, Quality-Control, Organize, Analyze and Release Your Bio-Data",
    )
    parser.add_argument(
        "--project",
        default=".",
        help="project root or project.yaml path (default: current directory)",
    )
    parser.add_argument("--version", "-v", action=ShowOperonVersion)
    sub = parser.add_subparsers(dest="command", required=True)
    from operon.timetree import add_parser as add_timetree_parser

    add_timetree_parser(sub)
    for add_command_parser in _PARSER_BUILDERS:
        add_command_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            return _cmd_init(args)
        if args.command == "init-demo":
            return _cmd_init_demo(args)
        if args.command == "qc-measure":
            return _cmd_qc_measure(args)
        if args.command == "alignment-qc":
            return _cmd_alignment_qc(args)
        if args.command == "timetree":
            from operon.timetree import run_cli

            return run_cli(args)
        if args.command == "backup" and args.backup_command == "verify":
            return _cmd_backup_verify(args)
        if args.command == "tui":
            return _cmd_tui(args)
        if args.command == "config":
            return _cmd_config(args)
        if args.command == "doctor":
            return _cmd_doctor(args)
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
                "recompress": lambda: _cmd_recompress(args, project, db),
                "qc": lambda: _cmd_qc(args, project, db),
                "import-events": lambda: _cmd_import_events(args, project, db),
                "import-hits": lambda: _cmd_import_hits(args, project, db),
                "import-qc": lambda: _cmd_import_qc(args, project, db),
                "run-external": lambda: _cmd_run_external(args, project, db),
                "tools": lambda: _cmd_tools(args, project, db),
                "tools-check": lambda: _cmd_tools_check(project),
                "analyze": lambda: _cmd_analyze(args, project, db),
                "extract-domains": lambda: _cmd_extract_domains(args, project, db),
                "select-sequences": lambda: _cmd_select_sequences(args, project, db),
                "classify-sequences": lambda: _cmd_classify_sequences(
                    args, project, db
                ),
                "remotes": lambda: _cmd_remotes(args, project),
                "push": lambda: _cmd_push(args, project, db),
                "pull": lambda: _cmd_pull(args, project, db),
                "evict": lambda: _cmd_evict(args, project, db),
                "locations": lambda: _cmd_locations(args, project, db),
                "evaluate": lambda: _cmd_evaluate(args, project, db),
                "curate": lambda: _cmd_curate(args, project, db),
                "release": lambda: _cmd_release(args, project, db),
                "export": lambda: _cmd_export(args, project, db),
                "adopt": lambda: _cmd_adopt(args, project, db),
                "fanout": lambda: _cmd_fanout(args, project, db),
                "recipes": lambda: _cmd_recipes(args, project, db),
                "environments": lambda: _cmd_environments(args, db),
                "profiles": lambda: _cmd_profiles(args, project, db),
                "run-pipeline": lambda: _cmd_run_pipeline(args, project, db),
                "taxonomy": lambda: _cmd_taxonomy(args, project, db),
                "report": lambda: _cmd_report(args, project, db),
                "workflow": lambda: _cmd_workflow(args, db),
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
        print(
            "interrupted: progress so far was saved; re-run the same command to resume",
            file=sys.stderr,
        )
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
