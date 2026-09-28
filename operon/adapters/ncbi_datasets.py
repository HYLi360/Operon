"""Offline-first adapter for NCBI Datasets genome packages.

The adapter deliberately separates acquisition from normalization.  Existing
JSON/JSONL reports, downloaded ZIP archives and unpacked dataset directories
all pass through the same parser and importer.  Online acquisition only adds a
streamed NCBI Datasets package download in front of that pipeline.
"""

# Facade module: run orchestration plus explicit re-exports of the internal
# _ncbi_model/_ncbi_sources/_ncbi_download/_ncbi_plan submodules for the CLI, the
# TUI and the tests.  Every deliberate re-export is listed in __all__ below, so
# ruff's F401 check stays meaningful for everything else in this module.
from __future__ import annotations

import errno
import hashlib
import json
import shutil  # noqa: F401 - the adapter tests patch shutil.* through this namespace
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from operon.config import Project, project_rel, resolve_actor, resolve_ncbi_email
from operon.database import Database
from operon.errors import ValidationError
from operon.schema import NCBI_SOURCE_FILE_ROLES, Schema
from operon.secrets import resolve_secret
from operon.utils import now_iso
from operon.workflow import finish_run, new_run_id, start_run

from ._ncbi_download import (
    NCBI_DATASETS_API_FALLBACK,
    _download_batch_aiohttp,
    _download_batches_async,
    _download_ncbi_dataset_once,
    _interruptible_retry_sleep,
    download_ncbi_dataset,
    download_ncbi_datasets_parallel,
    fetch_entrez_assembly_reports,
)
from ._ncbi_model import (
    DEFAULT_INCLUDES,
    INCLUDE_TYPES,
    VERSIONED_ACCESSION_RE,
    DatasetAsset,
    ImportPlan,
    SourceBundle,
    _accession_version,
    _annotation_identity,
    _assembly_asset_role,
    _assembly_namespace,
    _canonical_accession,
    _chunks,
    _collect_accessions,
    _date_only,
    _deduplicate_reports,
    _deep_merge,
    _DownloadCancelled,
    _extract_metadata,
    _float_or_none,
    _integer_or_none,
    _lat_lon,
    _mapping,
    _merge_nonempty,
    _normalize_assembly_level,
    _normalize_reference_status,
    _normalize_sex,
    _normalize_source_database,
    _pick,
    _read_report_file,
    _read_report_handle,
    _read_report_tsv,
    _report_has_accession,
    _select_canonical_assembly_accession,
    _split_accession,
    _unique,
    _version_tuple,
)
from ._ncbi_plan import (
    _adapter_schema,
    _apply_plan,
    _asset_sha256,
    _file_satisfies_include,
    _find_archived_assembly,
    _ingest_dataset_asset,
    _plan_missing_downloads,
    _PlanBuilder,
    _preflight_assets,
    _validate_plan_rows,
)
from ._ncbi_sources import (
    _accession_from_path,
    _asset_role,
    _local_zip_entry_names,
    _open_source,
    _preserve_source,
    _validate_zip_info,
    _zip_package_diagnostic,
    discover_dataset_assets,
    load_dataset_reports,
)
from ._ncbi_storage import (
    _format_bytes,
    _no_space_error,
    _require_disk_space,
)

# Deliberate re-export surface for the CLI (operon/cli.py), the TUI
# (operon/tui/actions.py), operon/ncbi_reconcile.py and the tests.  Kept in
# the sorted order ruff's RUF022 expects.
__all__ = [
    "DEFAULT_INCLUDES",
    "INCLUDE_TYPES",
    "NCBI_DATASETS_API_FALLBACK",
    "NCBI_SOURCE_FILE_ROLES",
    "VERSIONED_ACCESSION_RE",
    "DatasetAsset",
    "ImportPlan",
    "SourceBundle",
    "_DownloadCancelled",
    "_PlanBuilder",
    "_accession_from_path",
    "_accession_version",
    "_adapter_schema",
    "_annotation_identity",
    "_apply_plan",
    "_assembly_asset_role",
    "_assembly_namespace",
    "_asset_role",
    "_asset_sha256",
    "_canonical_accession",
    "_chunks",
    "_collect_accessions",
    "_date_only",
    "_deduplicate_reports",
    "_deep_merge",
    "_download_batch_aiohttp",
    "_download_batches_async",
    "_download_ncbi_dataset_once",
    "_extract_metadata",
    "_file_satisfies_include",
    "_find_archived_assembly",
    "_float_or_none",
    "_format_bytes",
    "_ingest_dataset_asset",
    "_integer_or_none",
    "_interruptible_retry_sleep",
    "_lat_lon",
    "_local_zip_entry_names",
    "_mapping",
    "_merge_nonempty",
    "_no_space_error",
    "_normalize_assembly_level",
    "_normalize_reference_status",
    "_normalize_sex",
    "_normalize_source_database",
    "_open_source",
    "_pick",
    "_plan_missing_downloads",
    "_preflight_assets",
    "_preserve_source",
    "_read_report_file",
    "_read_report_handle",
    "_read_report_tsv",
    "_report_has_accession",
    "_require_disk_space",
    "_select_canonical_assembly_accession",
    "_split_accession",
    "_unique",
    "_validate_plan_rows",
    "_validate_zip_info",
    "_version_tuple",
    "_zip_package_diagnostic",
    "discover_dataset_assets",
    "download_ncbi_dataset",
    "download_ncbi_datasets_parallel",
    "fetch_entrez_assembly_reports",
    "load_dataset_reports",
    "run_ncbi_datasets_adapter",
]


def run_ncbi_datasets_adapter(
        db: Database,
        project: Project,
        *,
        inputs: Sequence[str | Path] = (),
        accessions: Sequence[str] = (),
        accession_file: str | Path | None = None,
        includes: Sequence[str] = DEFAULT_INCLUDES,
        archive_files: bool = True,
        standardize: bool = False,
        dry_run: bool = False,
        preserve_sources: bool = True,
        email: str | None = None,
        api_key: str | None = None,
        timeout: float = 300.0,
        batch_size: int = 10,
        download_workers: int = 3,
        max_retries: int = 4,
        retry_backoff: float = 1.0,
        resume_run_id: str | None = None,
        plan_only: bool = False,
        cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Import existing NCBI Datasets outputs and optionally download packages.

    ``cancel_event`` is the cooperative-cancellation hook used by the TUI:
    once set, in-flight downloads stop at the next chunk/batch boundary and
    the run aborts with ``ShutdownRequested``, so the run row is recorded as
    interrupted exactly as after a signal.  The CLI leaves it unset.
    """

    requested = _validate_adapter_args(
        inputs=inputs,
        accessions=accessions,
        accession_file=accession_file,
        includes=includes,
        plan_only=plan_only,
        batch_size=batch_size,
        download_workers=download_workers,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
    )
    ctx, download_groups, skipped_existing = _prepare_run(
        db,
        project,
        requested=requested,
        includes=includes,
        archive_files=archive_files,
        standardize=standardize,
        dry_run=dry_run,
        preserve_sources=preserve_sources,
        email=email,
        api_key=api_key,
        batch_size=batch_size,
        download_workers=download_workers,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
    )
    fingerprint = _request_fingerprint(
        ctx,
        inputs=inputs,
        requested=requested,
        includes=includes,
        archive_files=archive_files,
        standardize=standardize,
        download_groups=download_groups,
        skipped_existing=skipped_existing,
        download_workers=download_workers,
        max_retries=max_retries,
        plan_only=plan_only,
    )
    if fingerprint is None:
        return ctx.summary
    command_text, request_sha256, request_document = fingerprint
    _begin_run(
        ctx,
        requested=requested,
        includes=includes,
        resume_run_id=resume_run_id,
        command_text=command_text,
        request_sha256=request_sha256,
        request_document=request_document,
        skipped_existing=skipped_existing,
    )

    try:
        for raw_input in inputs:
            source = Path(raw_input).resolve()
            _process_source(ctx, source, label=str(source))
        if download_groups:
            _consume_download_batches(
                ctx,
                download_groups,
                batch_size=batch_size,
                timeout=timeout,
                download_workers=download_workers,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
                cancel_event=cancel_event,
            )
        return _finalize_run(ctx, skipped_existing)
    except KeyboardInterrupt as exc:
        _record_run_outcome(ctx, exc, interrupted=True)
        raise
    except Exception as exc:
        reported_exc = _record_run_outcome(ctx, exc, interrupted=False)
        if reported_exc is not exc:
            raise reported_exc from exc
        raise


@dataclass
class _AdapterRunContext:
    """Mutable state shared by the per-source import steps of one adapter run."""

    db: Database
    project: Project
    run_id: str
    started_at: str
    dry_run: bool
    preserve_sources: bool
    archive_files: bool
    standardize: bool
    email: str | None
    api_key: str | None
    preview_schema: Schema
    summary: dict[str, Any]
    persisted_schema: Schema | None = None
    imported_assembly_ids: set[str] = field(default_factory=set)
    observed_assembly_groups: dict[str, set[str]] = field(default_factory=dict)
    accession_group: dict[str, str] = field(default_factory=dict)
    download_failures: list[dict[str, str]] = field(default_factory=list)


def _validate_adapter_args(
        *,
        inputs: Sequence[str | Path],
        accessions: Sequence[str],
        accession_file: str | Path | None,
        includes: Sequence[str],
        plan_only: bool,
        batch_size: int,
        download_workers: int,
        max_retries: int,
        retry_backoff: float,
) -> list[str]:
    """Collect requested accessions and reject invalid argument combinations."""
    requested = _collect_accessions(accessions, accession_file)
    if not inputs and not requested:
        raise ValidationError("provide at least one --input, --accession, or --accession-file")
    if plan_only and inputs:
        raise ValidationError("--plan-only supports accession requests, not offline --input packages")
    unknown_includes = sorted(set(includes) - set(INCLUDE_TYPES))
    if unknown_includes:
        raise ValidationError(f"unknown NCBI include type(s): {unknown_includes}")
    if batch_size < 1 or batch_size > 100:
        raise ValidationError("--batch-size must be between 1 and 100")
    if download_workers < 1 or download_workers > 10:
        raise ValidationError("--download-workers must be between 1 and 10")
    if max_retries < 0 or max_retries > 10:
        raise ValidationError("--retries must be between 0 and 10")
    if retry_backoff < 0:
        raise ValidationError("--retry-backoff must be >= 0")
    return requested


def _prepare_run(
        db: Database,
        project: Project,
        *,
        requested: Sequence[str],
        includes: Sequence[str],
        archive_files: bool,
        standardize: bool,
        dry_run: bool,
        preserve_sources: bool,
        email: str | None,
        api_key: str | None,
        batch_size: int,
        download_workers: int,
        max_retries: int,
        retry_backoff: float,
) -> tuple[_AdapterRunContext, dict[tuple[str, ...], list[str]], list[str]]:
    """Build the run context, summary skeleton and missing-download plan."""
    run_id = new_run_id()
    started_at = now_iso()
    summary: dict[str, Any] = {
        "run_id": run_id,
        "dry_run": dry_run,
        "sources": [],
        "assembly_records": 0,
        "metadata_rows": {
            "organisms": 0,
            "samples": 0,
            "assemblies": 0,
            "annotations": 0,
            "accessions": 0,
        },
        "new_ids": {
            "organism": 0,
            "sample": 0,
            "assembly": 0,
            "annotation": 0,
        },
        "discovered_files": 0,
        "archived_files": [],
        "standardized_files": [],
        "download": {
            "batch_size": batch_size,
            "workers": download_workers,
            "retries": max_retries,
            "retry_backoff": retry_backoff,
        },
        "download_failures": [],
        "skipped_existing": [],
    }
    ctx = _AdapterRunContext(
        db=db,
        project=project,
        run_id=run_id,
        started_at=started_at,
        dry_run=dry_run,
        preserve_sources=preserve_sources,
        archive_files=archive_files,
        standardize=standardize,
        email=email,
        api_key=api_key,
        preview_schema=_adapter_schema(project, persist=False),
        summary=summary,
    )

    download_groups: dict[tuple[str, ...], list[str]] = {
        tuple(includes): list(requested),
    } if requested else {}
    skipped_existing: list[str] = []
    if requested and archive_files:
        download_groups, skipped_existing = _plan_missing_downloads(
            db, project, requested, includes, standardize=standardize,
        )
        summary["skipped_existing"] = skipped_existing
    summary["download_plan"] = [
        {"includes": list(signature), "accessions": list(values)}
        for signature, values in download_groups.items()
    ]
    return ctx, download_groups, skipped_existing


def _request_fingerprint(
        ctx: _AdapterRunContext,
        *,
        inputs: Sequence[str | Path],
        requested: Sequence[str],
        includes: Sequence[str],
        archive_files: bool,
        standardize: bool,
        download_groups: dict[tuple[str, ...], list[str]],
        skipped_existing: Sequence[str],
        download_workers: int,
        max_retries: int,
        plan_only: bool,
) -> tuple[str, str, dict[str, Any]] | None:
    """Hash the request; return (command, sha256, document), or None for plan-only."""
    summary = ctx.summary
    to_download_count = sum(len(values) for values in download_groups.values())
    command_text = (
        "offline import" if not requested
        else f"download {to_download_count} accession(s) "
             f"(workers={download_workers}, retries={max_retries})"
             + (f"; skipped {len(skipped_existing)} already archived" if skipped_existing else "")
    )
    request_document = {
        "inputs": [str(Path(value).resolve()) for value in inputs],
        "accessions": requested,
        "includes": list(includes),
        "archive_files": archive_files,
        "standardize": standardize,
    }
    request_sha256 = hashlib.sha256(
        json.dumps(request_document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if plan_only:
        summary["plan_only"] = True
        summary["request_sha256"] = request_sha256
        return None
    return command_text, request_sha256, request_document


def _begin_run(
        ctx: _AdapterRunContext,
        *,
        requested: Sequence[str],
        includes: Sequence[str],
        resume_run_id: str | None,
        command_text: str,
        request_sha256: str,
        request_document: dict[str, Any],
        skipped_existing: Sequence[str],
) -> None:
    """Validate a resume request and open the workflow run with its items."""
    db = ctx.db
    if resume_run_id:
        previous = db.conn.execute(
            "SELECT run_id, input_sha256 FROM workflow_runs WHERE run_id=?",
            (resume_run_id,),
        ).fetchone()
        if previous is None:
            raise ValidationError(f"resume workflow run does not exist: {resume_run_id}")
        if previous["input_sha256"] and previous["input_sha256"] != request_sha256:
            raise ValidationError(
                "--resume-run request differs from the original run; use the same inputs, "
                "accessions, include set and archival options"
            )
    if not ctx.dry_run:
        start_run(db, {
            "run_id": ctx.run_id,
            "resumes_run_id": resume_run_id,
            "step": "ncbi_datasets_import",
            "status": "running",
            "started_at": ctx.started_at,
            "tool": "NCBI Datasets adapter",
            "parameter_set": ",".join(includes),
            "command": command_text,
            "input_sha256": request_sha256,
            "execution_details": json.dumps(request_document, ensure_ascii=False, sort_keys=True),
        })
        for accession in requested:
            status = "skipped" if accession in skipped_existing else "pending"
            db.upsert_adapter_run_item(
                ctx.run_id, accession, json.dumps(list(includes)), status,
                started_at=ctx.started_at,
                finished_at=now_iso() if status == "skipped" else None,
                result_json=(
                    json.dumps({"reason": "requested roles already archived"})
                    if status == "skipped" else None
                ),
            )


def _process_source(
        ctx: _AdapterRunContext,
        source_path: Path,
        *,
        label: str,
        requested_batch: Sequence[str] = (),
        already_preserved: Path | None = None,
) -> dict[str, Any]:
    """Import one source bundle (offline input or downloaded package)."""
    db = ctx.db
    project = ctx.project
    summary = ctx.summary
    bundle = _open_source(source_path, project, False, label=label)
    try:
        bundle_reports = load_dataset_reports(
            bundle.root,
            direct_file=bundle.source if bundle.source.is_file() else None,
        )
        contact_email = ctx.email or resolve_ncbi_email()
        if not bundle_reports and requested_batch and contact_email:
            bundle_reports.extend(fetch_entrez_assembly_reports(
                requested_batch,
                email=contact_email,
                api_key=ctx.api_key or resolve_secret("ncbi.api_key"),
            ))
        bundle_assets = discover_dataset_assets(bundle.root, bundle_reports, bundle.label)
        source_summary = {
            "source": bundle.label,
            "preserved_path": (
                project_rel(project, already_preserved) if already_preserved else None
            ),
            "reports": len(bundle_reports),
            "assets": len(bundle_assets),
        }
        summary["sources"].append(source_summary)
        if not bundle_reports:
            return {}

        _track_assembly_groups(ctx, bundle_reports)

        plan = _PlanBuilder(db).build(bundle_reports, bundle_assets)
        _preflight_assets(db, plan)
        # Validate each batch's plan rows exactly once; the normalized rows
        # feed `_apply_plan` below instead of being recomputed inside it.
        normalized = _validate_plan_rows(ctx.preview_schema, plan)
        ctx.imported_assembly_ids.update(plan.assembly_ids.values())
        for table, rows in plan.tables.items():
            summary["metadata_rows"][table] += len(rows)
        for entity_type, count in plan.new_ids.items():
            summary["new_ids"][entity_type] += count
        summary["discovered_files"] += len(plan.assets)
        if ctx.dry_run:
            return {
                "assembly_ids": sorted(set(plan.assembly_ids.values())),
                "annotation_ids": sorted(set(plan.annotation_ids.values())),
                "file_ids": [],
            }

        if ctx.preserve_sources and already_preserved is None and bundle.source.is_file():
            bundle.preserved_path = _preserve_source(bundle.source, project)
            source_summary["preserved_path"] = project_rel(project, bundle.preserved_path)

        if ctx.persisted_schema is None:
            ctx.persisted_schema = _adapter_schema(project, persist=True)
        _apply_plan(
            db, project, plan, ctx.persisted_schema,
            workflow_run_id=ctx.run_id, normalized=normalized,
        )
        source_file_ids = _archive_plan_assets(ctx, plan)
        return {
            "assembly_ids": sorted(set(plan.assembly_ids.values())),
            "annotation_ids": sorted(set(plan.annotation_ids.values())),
            "file_ids": source_file_ids,
        }
    finally:
        # Critical for large accession lists: no source bundle or staging
        # directory is allowed to survive into the next batch.
        bundle.close()


def _track_assembly_groups(ctx: _AdapterRunContext, bundle_reports: Sequence[dict[str, Any]]) -> None:
    """Track report identity independently of allocated IDs."""
    # This keeps large --dry-run summaries correct even though dry runs do
    # not write one batch's ID allocations for the next batch to see.
    observed_assembly_groups = ctx.observed_assembly_groups
    accession_group = ctx.accession_group
    for report in bundle_reports:
        meta = _extract_metadata(report)
        related = set(_unique([
            meta.get("accession"),
            meta.get("current_accession"),
            meta.get("paired_accession"),
        ]))
        if not related:  # pragma: no cover
            continue
        roots = {accession_group[item] for item in related if item in accession_group}
        root = min(roots) if roots else min(related)
        members = set(related)
        for old_root in roots:
            members.update(observed_assembly_groups.pop(old_root, set()))
        observed_assembly_groups[root] = members
        for item in members:
            accession_group[item] = root


def _archive_plan_assets(ctx: _AdapterRunContext, plan: ImportPlan) -> list[str]:
    """Archive every planned asset and return the source file IDs."""
    db = ctx.db
    project = ctx.project
    summary = ctx.summary
    source_file_ids: list[str] = []
    if ctx.archive_files:
        for asset in plan.assets:
            accession = _canonical_accession(asset.accession)
            if asset.role in {"annotation_gff3", "cds_fasta", "protein_fasta"}:
                entity_type = "annotation"
                entity_id = plan.annotation_ids[accession]
            else:
                entity_type = "assembly"
                entity_id = plan.assembly_ids[accession]
            row = _ingest_dataset_asset(
                db,
                project,
                asset,
                entity_type,
                entity_id,
                run_id=ctx.run_id,
                standardize=ctx.standardize,
            )
            summary["archived_files"].append(row["file_id"])
            source_file_ids.append(row["file_id"])
            if entity_type == "assembly":
                pointer = (
                    "genome_file_id" if asset.role.startswith("genome_fasta")
                    else "report_file_id" if asset.role.startswith("assembly_report")
                    else None
                )
                if pointer:  # pragma: no branch
                    with db.transaction():
                        db.conn.execute(
                            f"UPDATE ncbi_assembly_records SET {pointer}=?, "
                            "workflow_run_id=?, updated_at=? WHERE accession=?",  # nosec B608 # fixed mappings or validated schema identifiers; values are bound
                            (row["file_id"], ctx.run_id, now_iso(), accession),
                        )
            if row.get("standardized_file_id"):
                summary["standardized_files"].append(row["standardized_file_id"])
    return source_file_ids


def _consume_download_batch(
        ctx: _AdapterRunContext,
        batch: Sequence[str],
        zip_path: Path,
        *,
        includes: Sequence[str],
) -> None:
    """Preserve and import one downloaded batch ZIP, tracking per-accession status."""
    db = ctx.db
    preserved_path: Path | None = None
    source_path = zip_path
    if not ctx.dry_run:
        for accession in batch:
            db.upsert_adapter_run_item(
                ctx.run_id, accession, json.dumps(list(includes)),
                "downloading", started_at=now_iso(),
            )
    try:
        if ctx.preserve_sources and not ctx.dry_run:
            preserved_path = _preserve_source(zip_path, ctx.project, move=True)
            source_path = preserved_path
        result = _process_source(
            ctx,
            source_path,
            label=f"download:{','.join(batch)}",
            requested_batch=batch,
            already_preserved=preserved_path,
        )
        if not ctx.dry_run:
            for accession in batch:
                db.upsert_adapter_run_item(
                    ctx.run_id, accession, json.dumps(list(includes)),
                    "completed", started_at=ctx.started_at, finished_at=now_iso(),
                    result_json=json.dumps(result, ensure_ascii=False, sort_keys=True),
                )
    except BaseException as exc:
        if not ctx.dry_run:
            status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            for accession in batch:
                db.upsert_adapter_run_item(
                    ctx.run_id, accession, json.dumps(list(includes)),
                    status, started_at=ctx.started_at, finished_at=now_iso(),
                    error=f"{type(exc).__name__}: {exc}",
                )
        raise
    finally:
        zip_path.unlink(missing_ok=True)


def _record_download_failure(
        ctx: _AdapterRunContext,
        batch: Sequence[str],
        error: BaseException,
        *,
        includes: Sequence[str],
) -> None:
    """Record one failed download batch in the summary and the run items."""
    ctx.download_failures.append({
        "accessions": ",".join(batch),
        "includes": ",".join(includes),
        "error": f"{type(error).__name__}: {error}",
    })
    ctx.summary["download_failures"] = ctx.download_failures
    if not ctx.dry_run:
        for accession in batch:
            ctx.db.upsert_adapter_run_item(
                ctx.run_id, accession, json.dumps(list(includes)),
                "failed", started_at=ctx.started_at, finished_at=now_iso(),
                error=f"{type(error).__name__}: {error}",
            )


def _consume_download_batches(
        ctx: _AdapterRunContext,
        download_groups: dict[tuple[str, ...], list[str]],
        *,
        batch_size: int,
        timeout: float,
        download_workers: int,
        max_retries: int,
        retry_backoff: float,
        cancel_event: threading.Event | None = None,
) -> None:
    """Download the planned batches concurrently and import each as it lands."""
    # Keep downloads off /tmp: it is commonly a small tmpfs.  The
    # staging directory lives on the project filesystem.  Batches are
    # downloaded concurrently and consumed as soon as each finishes.
    with tempfile.TemporaryDirectory(
            prefix=".operon-ncbi-download-", dir=str(ctx.project.root)
    ) as temp_name:
        for missing_signature, group_accessions in download_groups.items():
            batches = list(_chunks(group_accessions, batch_size))
            download_ncbi_datasets_parallel(
                batches,
                Path(temp_name),
                includes=missing_signature,
                email=ctx.email,
                api_key=ctx.api_key,
                timeout=timeout,
                max_workers=download_workers,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
                cancel_event=cancel_event,
                on_complete=lambda batch, zip_path, signature=missing_signature:
                    _consume_download_batch(ctx, batch, zip_path, includes=signature),
                on_error=lambda batch, error, signature=missing_signature:
                    _record_download_failure(ctx, batch, error, includes=signature),
            )


def _finalize_run(ctx: _AdapterRunContext, skipped_existing: Sequence[str]) -> dict[str, Any]:
    """Record the audit entry and close out the run, or raise on failures."""
    db = ctx.db
    summary = ctx.summary
    download_failures = ctx.download_failures
    if download_failures and ctx.dry_run:
        summary["assembly_records"] = len(ctx.observed_assembly_groups)
        return summary
    if not ctx.imported_assembly_ids and not skipped_existing:
        if download_failures:
            details = "\n".join(
                f"- {item['accessions']}: {item['error']}" for item in download_failures[:20]
            )
            raise ValidationError(
                "no NCBI assembly records could be imported; download batch failures:\n" + details
            )
        raise ValidationError("no NCBI assembly records found in the supplied input/download")
    summary["assembly_records"] = len(ctx.observed_assembly_groups)
    if ctx.dry_run:
        return summary
    evidence = ", ".join(
        item["preserved_path"] for item in summary["sources"] if item["preserved_path"]
    ) or None
    db.record_change(
        "adapter",
        ctx.run_id,
        None,
        None,
        json.dumps(summary, ensure_ascii=False, sort_keys=True),
        "NCBI Datasets import",
        evidence=evidence,
        actor=resolve_actor(),
        workflow_run_id=ctx.run_id,
    )
    if download_failures:
        failed_count = len(download_failures)
        details = "\n".join(
            f"- {item['accessions']}: {item['error']}" for item in download_failures[:20]
        )
        if failed_count > 20:
            details += f"\n- ... and {failed_count - 20} more failed batch(es)"
        raise ValidationError(
            f"{failed_count} NCBI download batch(es) failed while other batches were imported successfully:\n"
            + details
        )
    finish_run(
        db, ctx.project, ctx.run_id, status="completed", exit_code=0,
        execution_details=json.dumps(summary, ensure_ascii=False, sort_keys=True),
    )
    return summary

def _record_run_outcome(ctx: _AdapterRunContext, exc: Exception, *, interrupted: bool) -> Exception:
    """Best-effort run-outcome record so aborted or failed runs stay in the audit trail."""
    if interrupted:
        # SIGINT/SIGTERM (ShutdownRequested included): record the interruption
        # so an aborted run is visible in the audit trail instead of looking
        # like it never happened.
        if not ctx.dry_run:
            try:
                signum = getattr(exc, "signum", None)
                finish_run(
                    ctx.db, ctx.project, ctx.run_id, status="interrupted", exit_code=130,
                    error=(f"interrupted by signal {signum}" if signum is not None
                           else "interrupted"),
                    execution_details=json.dumps(ctx.summary, ensure_ascii=False, sort_keys=True),
                )
            except Exception:  # noqa: BLE001 - failed-run bookkeeping must not mask the original error  # pylint: disable=broad-exception-caught
                pass
        return exc
    # Translate ENOSPC, best-effort record the failure, return the error to raise.
    reported_exc: Exception = exc
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        reported_exc = _no_space_error(ctx.project.root, "NCBI Datasets import", exc)
    if not ctx.dry_run:
        try:
            finish_run(
                ctx.db, ctx.project, ctx.run_id, status="failed", exit_code=1,
                error=str(reported_exc),
                execution_details=json.dumps(ctx.summary, ensure_ascii=False, sort_keys=True),
            )
        except Exception:  # noqa: BLE001 - failed-run bookkeeping must not mask the original error  # pylint: disable=broad-exception-caught
            pass
    return reported_exc
