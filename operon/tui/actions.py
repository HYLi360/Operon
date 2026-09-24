"""Write actions for the Operon TUI (phase 2).

The TUI never reimplements business logic: every function here calls the
same core functions the CLI uses, so audit rows, provenance, and semantics
are identical to the equivalent ``operon`` command.  Each public function
opens its own short-lived *writable* ``Database`` connection, does the work,
closes it, and returns plain dicts.  Writable connections are only ever
opened inside this module — the UI layer never holds one.

``lifecycle_preview`` is the single exception: it is a read-only plan
preview, so it uses a read-only connection like :mod:`operon.tui.data`.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.utils import atomic_write_text


@contextmanager
def _open_writable(project: Project) -> Iterator[Database]:
    db = Database(project.db_path)
    try:
        yield db
    finally:
        db.close()


def evaluate(
        project: Project,
        entity_type: str | None = None,
        entity_id: str | None = None,
        profile: str | None = None,
) -> list[dict[str, Any]]:
    """Evaluate decisions like ``operon evaluate``; returns summary rows."""
    from operon.rules import evaluate_all, evaluate_entity

    with _open_writable(project) as db:
        if entity_id:
            if not entity_type:
                raise ValidationError("--entity-type is required when --entity-id is given")
            rows = [evaluate_entity(db, project, entity_type, entity_id, profile)]
        else:
            rows = evaluate_all(db, project, profile, entity_type)
    return [
        {
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "profile": row["profile"],
            "decision": row["decision"],
            "reason_codes": json.loads(row.get("reason_codes") or "[]"),
        }
        for row in rows
    ]


def curate(
        project: Project,
        entity_type: str,
        entity_id: str,
        profile: str,
        decision: str,
        reviewer: str,
        reason: str,
        evidence: str | None = None,
) -> None:
    """Record a curated decision like ``operon curate`` (audited override)."""
    from operon.rules import curate_decision

    with _open_writable(project) as db:
        curate_decision(db, entity_type, entity_id, profile, decision,
                        reviewer=reviewer, reason=reason, evidence=evidence)


def lifecycle_preview(project: Project, identifier: str, action: str) -> dict[str, Any]:
    """Return the read-only impact plan for a RETIRE/RESTORE operation."""
    from operon.lifecycle import lifecycle_plan

    db = Database(project.db_path, read_only=True)
    try:
        return lifecycle_plan(db, identifier, action=action)
    finally:
        db.close()


def lifecycle_apply(
        project: Project,
        identifier: str,
        action: str,
        reason: str,
        actor: str,
        reason_code: str | None = None,
        evidence: str | None = None,
) -> dict[str, Any]:
    """Apply a RETIRE/RESTORE exactly like ``operon retire|restore --apply``."""
    from operon.lifecycle import apply_lifecycle_event, lifecycle_plan
    from operon.utils import now_iso
    from operon.workflow import flush_run_log, log_run, new_run_id

    action = action.upper()
    if action not in {"RETIRE", "RESTORE"}:
        raise ValidationError(f"unsupported lifecycle action {action!r}")
    actor = (actor or os.environ.get("USER") or "").strip()
    if not actor:
        raise ValidationError("--actor is required when USER is not set")
    with _open_writable(project) as db:
        plan = lifecycle_plan(db, identifier, action=action)
        if not plan["will_change"]:
            if plan["blocker"]:
                raise ValidationError(plan["blocker"])
            return {"applied": False, "action": action, "target": plan["target"], "plan": plan}
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
                reason=reason,
                reason_code=reason_code,
                evidence=evidence,
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
                    "command": f"operon {action.lower()} {identifier}",
                    "tool": "operon",
                    "parameter_set": json.dumps({
                        "reason_code": reason_code if reason_code is not None else "manual_restore",
                        "reason": reason,
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
        return {
            "applied": True,
            "action": action,
            "target": target,
            "event": result["event"],
            "effectively_retired": db.is_entity_retired(target["entity_type"], target["entity_id"]),
        }


def ingest(
        project: Project,
        source: str,
        entity_type: str,
        entity_id: str,
        role: str,
        fmt: str | None = None,
        compression: str | None = None,
        source_url: str | None = None,
        move: bool = False,
) -> dict[str, Any]:
    """Archive one file like ``operon ingest``; ConflictError propagates."""
    from operon.files import ingest_file

    temp_path: Path | None = None
    if source.startswith(("sftp://", "remote://")):
        from operon.remotes import fetch_url_to_temp
        original_url = source
        temp_path = fetch_url_to_temp(project, source)
        source = str(temp_path)
        source_url = source_url or original_url
    try:
        with _open_writable(project) as db:
            return ingest_file(
                db, project, source, entity_type, entity_id, role,
                fmt=fmt, compression=compression, source_url=source_url, move=move,
            )
    finally:
        if temp_path is not None:
            if temp_path.is_dir() and not temp_path.is_symlink():
                shutil.rmtree(temp_path, ignore_errors=True)
            else:
                temp_path.unlink(missing_ok=True)


def verify(project: Project, file_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Verify files like ``operon verify`` (writes statuses/audit rows)."""
    from operon.files import verify_files

    with _open_writable(project) as db:
        return verify_files(db, project, file_ids)


def run_qc(
        project: Project,
        entity_type: str | None = None,
        entity_id: str | None = None,
        file_id: str | None = None,
        progress: Callable[[int, int, dict[str, Any]], None] | None = None,
        *,
        sample_size: int = 1000000,
        phred_offset: int | str = 33,
        rehash: bool = False,
) -> list[dict[str, Any]]:
    """Run built-in QC like ``operon qc``.

    ``progress`` is invoked after each file with ``(done, total, result)``;
    raising from it aborts the batch between files (results for files
    already processed are kept).
    """
    from operon.qc import qc_all

    if sample_size <= 0:
        raise ValidationError("sample size must be a positive integer")
    if str(phred_offset) not in {"33", "64", "auto"}:
        raise ValidationError("phred offset must be 33, 64 or auto")
    with _open_writable(project) as db:
        return qc_all(
            db, project, entity_type=entity_type, entity_id=entity_id,
            file_id=file_id, progress_callback=progress,
            sample_size=sample_size, phred_offset=phred_offset, force_checksum=rehash,
        )


class AnalysisCancelled(Exception):
    """Raised when the analyze worker is cancelled by the user.

    The core reports cooperative cancellation with ``ShutdownRequested`` —
    the same exception a SIGINT produces — once ``cancel_event`` is set, so
    the full interrupt bookkeeping runs: unfinished tasks get ``interrupted``
    job rows, partial outputs follow ``keep_partial``, and a still-queued
    Slurm job or array is cancelled with one ``scancel``.  This wrapper is the
    TUI's own signal: the analyze modal reports it as a cancelled run (files
    already completed keep their results) instead of a failure.
    """


def _validate_backend_choice(backend: str | None) -> None:
    """Reject anything the CLI's ``--backend`` choices would not accept."""
    from operon.execution import VALID_BACKENDS

    if backend is not None and str(backend) not in VALID_BACKENDS:
        raise ValidationError(
            f"unknown execution backend {backend!r}; valid: {', '.join(VALID_BACKENDS)}"
        )


def preflight_backend(
        project: Project,
        backend: str | None = None,
        *,
        recipe_name: str | None = None,
) -> dict[str, Any]:
    """Resolve and validate an execution backend exactly like ``operon analyze``.

    ``backend`` is the CLI's ``--backend`` value (``None`` selects the
    project's ``execution.backend`` from ``project.yaml``); ``recipe_name``
    applies the recipe's ``slurm:`` overrides the way the core does before a
    run starts.  Returns ``{"backend": <name>, "description": <describe()>}``
    and raises ``ValidationError`` for an unknown backend, an incomplete
    ``execution.ssh`` block, or a local Slurm backend whose ``sbatch`` /
    ``squeue`` are not on PATH — the failures the analyze modal shows inline
    instead of starting a worker that would fail per file.
    """
    from operon.execution import get_executor
    from operon.tools import get_recipe

    overrides: dict[str, Any] | None = None
    if recipe_name:
        recipe_slurm = get_recipe(project, recipe_name).raw.get("slurm")
        if isinstance(recipe_slurm, dict):
            overrides = recipe_slurm
    executor = get_executor(project, backend, slurm_overrides=overrides)
    try:
        name = str(getattr(executor, "name", "") or "")
        # The SSH backend runs the scheduler commands on the remote host,
        # which only a live run can probe; the local Slurm backend shells out.
        if name == "slurm":
            for binary in ("sbatch", "squeue"):
                if not shutil.which(binary):
                    raise ValidationError(f"slurm backend requires {binary!r} in PATH")
        return {"backend": name, "description": executor.describe()}
    finally:
        close = getattr(executor, "close", None)
        if close is not None:
            close()


def run_analysis(
        project: Project,
        analysis: str,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int | None = None,
        threads: int | None = None,
        dry_run: bool = False,
        force: bool = False,
        keep_partial: bool = False,
        parameters: dict[str, str] | None = None,
        backend: str | None = None,
        progress: Callable[[int, int, str, str], None] | None = None,
        cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one analysis recipe like ``operon analyze``.

    ``backend`` mirrors ``--backend`` (``None`` = the project default), and
    ``progress`` is forwarded as the core ``progress_callback``
    ``(index, total, file_id, phase)``; raising from it aborts the batch
    between files (results for files already processed are kept).
    ``cancel_event`` is forwarded to the core: once set, the batch aborts at
    the next file/planning/collection boundary, a still-queued scheduler job
    or array is cancelled, and the interrupt bookkeeping runs exactly as
    after a signal — reported here as :class:`AnalysisCancelled`.
    The core prints directly (cache warnings, "no candidate files"); that
    output is captured into the returned ``messages`` so it never corrupts
    the screen.
    """
    from operon.shutdown import ShutdownRequested
    from operon.tools import run_analysis as _run_analysis

    if limit is not None and int(limit) <= 0:
        raise ValidationError("limit must be a positive integer")
    if threads is not None and int(threads) <= 0:
        raise ValidationError("threads must be a positive integer")
    _validate_backend_choice(backend)
    buffer = io.StringIO()
    try:
        with _open_writable(project) as db, contextlib.redirect_stdout(buffer):
            results = _run_analysis(
                project, db, analysis,
                entity_type=entity_type, entity_id=entity_id,
                limit=limit, threads=threads, dry_run=dry_run, force=force,
                backend=backend,
                keep_partial=keep_partial, runtime_parameters=parameters,
                progress_callback=progress, cancel_event=cancel_event,
            )
    except ShutdownRequested:
        # A cooperative cancel reaches the core as the signal-style interrupt;
        # any other source (a real SIGINT in a main-thread caller) keeps its
        # own exception type.
        if cancel_event is not None and cancel_event.is_set():
            raise AnalysisCancelled() from None
        raise
    errors = sum(1 for result in results if result.get("status") in {"error", "failed"})
    return {
        "analysis": analysis,
        "results": results,
        "messages": buffer.getvalue(),
        "total": len(results),
        "succeeded": len(results) - errors,
        "errors": errors,
        "dry_run": dry_run,
    }


def run_external(
        project: Project,
        step: str,
        command_line: str,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        parameter_set: str | None = None,
        tool: str | None = None,
        inputs: Iterable[str] = (),
        expected_outputs: Iterable[str] = (),
        threads: int | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        backend: str | None = None,
) -> dict[str, Any]:
    """Run one external command like ``operon run-external``.

    ``command_line`` is split with the same ``shlex`` rules as the CLI's
    ``--command`` (shell quoting, no pipes or redirections).  Returns the
    recorded run's summary — the fields the CLI prints plus the log paths and
    the captured core output in ``messages``.  A command that *ran but failed*
    is a recorded outcome, not an error: the core writes the ``workflow_runs``
    row (with exit code, error and logs) before raising, so the failure comes
    back as ``status == "failed"`` with the run id intact.  Form-level
    problems — an empty command line, an unknown backend, a declared input
    that does not exist, a retired entity — raise ``ValidationError`` exactly
    like the CLI and the core.
    """
    import shlex

    from operon.workflow import new_run_id, run_external_command

    _validate_backend_choice(backend)
    argv = shlex.split(command_line)
    if not argv:
        raise ValidationError("--command must not be empty")
    if threads is not None and int(threads) <= 0:
        raise ValidationError("threads must be a positive integer")
    if timeout is not None and float(timeout) <= 0:
        raise ValidationError("timeout must be a positive number")

    buffer = io.StringIO()
    tool_version: str | None = None
    extra_details: dict[str, Any] | None = None
    with contextlib.redirect_stdout(buffer):
        if tool:
            # Same provenance contract as the CLI: an unconfigured tool name is
            # recorded without a version, and a failed detection only warns.
            from operon.tools import (
                detect_tool_version_record,
                get_tool,
                load_tools_config,
            )
            try:
                tool_spec = get_tool(project, tool)
            except ValidationError:
                tool_spec = None
            if tool_spec is not None:
                try:
                    config = load_tools_config(project)
                    tool_version, raw_output = detect_tool_version_record(tool_spec, config)
                    extra_details = {"tool_version_raw": raw_output}
                except Exception as exc:  # noqa: BLE001 - detection never blocks the run  # pylint: disable=broad-exception-caught
                    print(f"warning: version detection for {tool!r} failed: {exc}")
        run_id = new_run_id()
        print(f"run {run_id}: logs {project.logs_root / (run_id + '.stdout.log')} / "
              f"{project.logs_root / (run_id + '.stderr.log')}; "
              f"watch: operon workflow show {run_id} --follow")
        record: dict[str, Any]
        try:
            with _open_writable(project) as db:
                record = run_external_command(
                    db, project, argv, step=step,
                    entity_type=entity_type, entity_id=entity_id,
                    parameter_set=parameter_set,
                    expected_outputs=list(expected_outputs),
                    cwd=cwd, timeout=timeout, tool=tool, tool_version=tool_version,
                    backend=backend, threads=threads, inputs=list(inputs),
                    extra_details=extra_details, run_id=run_id,
                )
        except RuntimeError as exc:
            # The core recorded the failed run before raising; keep the summary
            # so the UI can open the full record.
            record = {
                "run_id": run_id, "step": step, "status": "failed",
                "exit_code": None, "error": str(exc),
                "stdout_file": str(project.logs_root / f"{run_id}.stdout.log"),
                "stderr_file": str(project.logs_root / f"{run_id}.stderr.log"),
            }
    return {
        "run_id": record.get("run_id", run_id),
        "step": record.get("step", step),
        "status": record.get("status"),
        "exit_code": record.get("exit_code"),
        "finished_at": record.get("finished_at"),
        "error": record.get("error"),
        "stdout_file": record.get("stdout_file"),
        "stderr_file": record.get("stderr_file"),
        "messages": buffer.getvalue(),
    }


# ---------------------------------------------------------------------------
# Config screen: structured editing of QC profiles and tools.yaml recipes.
#
# Every save is a new version: the version field is bumped and a
# content-addressed snapshot is recorded with exactly the same canonical
# document the CLI records (rules.py for profiles, tools.run_analysis for
# recipes), so a TUI save and a later CLI evaluation of identical content map
# to the same snapshot.
# ---------------------------------------------------------------------------

PROFILE_OPERATORS = (">=", "<=", ">", "<", "==", "!=", "between", "in", "not_in", "exists")
ENTITY_TYPE_NAMES = ("organism", "sample", "run", "assembly", "annotation")
# The TUI's profile editor saves three kinds; each kind has its own form,
# dispatched by the document's own kind (see operon/tui/screens/config*.py).
CLASSIFICATION_KIND = "sequence_classification"
COVERAGE_KIND = "taxonomy_coverage"
PROFILE_KINDS = ("qc", CLASSIFICATION_KIND, COVERAGE_KIND)


def write_analysis_report(
        project: Project,
        *,
        out: str,
        fmt: str = "text",
        analysis: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        query_id: str | None = None,
        subject_id: str | None = None,
        evalue_max: float | None = None,
        limit: int = 20,
        include_retired: bool = False,
) -> dict[str, Any]:
    """Write alignment hits to a path exactly like ``report analysis --hits --out``.

    The rows come from the CLI's own read-only query (``data.analysis_hits``)
    and are rendered by ``operon.reports.render_report_rows`` — the same
    renderer the CLI uses — so the file matches a CLI export with the same
    filters and limit byte for byte.  Like the CLI report, this writes no
    ``changes`` or ``workflow_runs`` rows: it is a browsing export.
    """
    from operon.reports import render_report_rows
    from operon.tui import data

    if not out.strip():
        raise ValidationError("an output path is required")
    if fmt not in {"text", "tsv", "json"}:
        raise ValidationError(f"unknown report format: {fmt}")
    rows = data.analysis_hits(
        project,
        analysis=analysis,
        entity_type=entity_type,
        entity_id=entity_id,
        query_id=query_id,
        subject_id=subject_id,
        evalue_max=evalue_max,
        limit=limit,
        include_retired=include_retired,
    )
    target = Path(out)
    text = render_report_rows(rows, fmt, headers=data.ANALYSIS_HIT_COLUMNS)
    if not text:
        text = "(no analysis results)\n"
    atomic_write_text(target, text)
    return {"path": str(target), "rows": len(rows), "format": fmt}


def run_classify(project: Project, profile_name: str) -> dict[str, Any]:
    """Run ``operon classify-sequences --profile`` like the CLI does.

    One transaction, the same run row and the same idempotency contract: an
    unchanged profile over unchanged inputs refreshes ``profile_sha256`` and
    reports ``labels_written``/``labels_removed`` as 0.
    """
    from operon.classify import classify_sequences

    _validate_config_name("profile", profile_name)
    command = shlex.join(["operon", "classify-sequences", "--profile", profile_name])
    with _open_writable(project) as db:
        return classify_sequences(db, project, profile_name=profile_name, command=command)


def extract_domains(
        project: Project,
        *,
        file_id: str,
        out: str,
        analysis: str | None = None,
        regions_tsv: str | None = None,
        flank: int = 5,
        min_length: int = 30,
        best_only: bool = True,
        subject_like: str | None = None,
        evalue_max: float | None = None,
        manifest: str | None = None,
) -> dict[str, Any]:
    """Run ``operon extract-domains``; the output FASTA stays unregistered.

    ``analysis``/``regions_tsv`` are mutually exclusive and one is required,
    and ``subject_like``/``evalue_max`` only apply to the analysis form — the
    same rules the core enforces, checked here so the modal reports them inline
    before any work starts.
    """
    from operon.sequence_tools import extract_domains as core_extract

    if bool(analysis) == bool(regions_tsv):
        raise ValidationError("exactly one of --analysis or --regions-tsv is required")
    if regions_tsv and (subject_like or evalue_max is not None):
        raise ValidationError("--subject-like/--evalue-max only apply to --analysis regions")
    if not file_id.strip():
        raise ValidationError("--file-id is required")
    if not out.strip():
        raise ValidationError("--out is required")
    parts = ["operon", "extract-domains", "--file-id", file_id]
    if analysis:
        parts += ["--analysis", analysis]
    else:
        parts += ["--regions-tsv", str(regions_tsv)]
    parts += ["--flank", str(flank), "--min-length", str(min_length)]
    parts.append("--all-regions" if not best_only else "--best-only")
    if subject_like:
        parts += ["--subject-like", subject_like]
    if evalue_max is not None:
        parts += ["--evalue-max", str(evalue_max)]
    parts += ["--out", out]
    if manifest:
        parts += ["--manifest", manifest]
    with _open_writable(project) as db:
        return core_extract(
            db, project,
            file_id=file_id, out=out, command=shlex.join(parts),
            analysis=analysis, regions_tsv=regions_tsv,
            flank=flank, min_length=min_length, best_only=best_only,
            subject_like=subject_like, evalue_max=evalue_max, manifest=manifest,
        )


def select_sequences(
        project: Project,
        *,
        file_id: str,
        out: str,
        analyses: Iterable[str] = (),
        subject_like: str | None = None,
        evalue_max: float | None = None,
        min_span: int | None = None,
        hit_type: str | None = None,
        require_hit: bool = True,
        entity_type: str | None = None,
        entity_id: str | None = None,
        manifest: str | None = None,
) -> dict[str, Any]:
    """Run ``operon select-sequences``; the output FASTA stays unregistered."""
    from operon.sequence_tools import select_sequences as core_select

    analyses = [name for name in analyses if str(name).strip()]
    if not file_id.strip():
        raise ValidationError("--file-id is required")
    if not out.strip():
        raise ValidationError("--out is required")
    if not analyses and subject_like is None and evalue_max is None \
            and min_span is None and hit_type is None:
        raise ValidationError(
            "no hit criteria given; pass at least one of --analysis, --subject-like, "
            "--evalue-max, --min-span or --hit-type"
        )
    parts = ["operon", "select-sequences", "--file-id", file_id]
    for name in analyses:
        parts += ["--analysis", name]
    if subject_like:
        parts += ["--subject-like", subject_like]
    if evalue_max is not None:
        parts += ["--evalue-max", str(evalue_max)]
    if min_span is not None:
        parts += ["--min-span", str(min_span)]
    if hit_type:
        parts += ["--hit-type", hit_type]
    parts.append("--require-hit" if require_hit else "--require-no-hit")
    if entity_type:
        parts += ["--entity-type", entity_type]
    if entity_id:
        parts += ["--entity-id", entity_id]
    parts += ["--out", out]
    if manifest:
        parts += ["--manifest", manifest]
    with _open_writable(project) as db:
        return core_select(
            db, project,
            file_id=file_id, out=out, command=shlex.join(parts),
            analyses=analyses, subject_like=subject_like, evalue_max=evalue_max,
            min_span=min_span, hit_type=hit_type, require_hit=require_hit,
            entity_type=entity_type, entity_id=entity_id, manifest=manifest,
        )


def adopt(
        project: Project,
        *,
        items: list[dict[str, Any]] | None = None,
        manifest: str | None = None,
        actor: str | None = None,
) -> dict[str, Any]:
    """Run ``operon adopt`` for one item or a manifest, like the CLI.

    The result reports how many items were newly registered versus reused from
    an identical existing registration (the core's idempotency contract); a
    conflict (same entity+role, different bytes) raises and nothing is written.
    """
    from operon.files import find_existing_file
    from operon.lineage import adopt_files, load_adopt_manifest
    from operon.utils import sha256_file

    if manifest:
        items = load_adopt_manifest(manifest)
    items = list(items or [])
    if not items:
        raise ValidationError("adopt needs one item or a manifest")
    reused_flags: list[bool] = []
    for item in items:
        missing = [key for key in ("path", "entity_type", "entity_id", "role")
                   if not str(item.get(key) or "").strip()]
        if missing:
            raise ValidationError(f"single-file adopt requires {', '.join(missing)}")
        if not item.get("derived_from"):
            raise ValidationError("single-file adopt requires at least one --derived-from FILE_ID")
        path = Path(str(item["path"]))
        sha = sha256_file(path) if path.exists() and path.is_file() else None
        with _open_writable(project) as db:
            existing = find_existing_file(
                db, str(item["entity_type"]), str(item["entity_id"]), str(item["role"]),
                sha or "",
            ) if sha else None
        reused_flags.append(existing is not None)
    resolved_actor = (actor or os.environ.get("USER") or "adopt").strip()
    with _open_writable(project) as db:
        results = adopt_files(db, project, items=items, actor=resolved_actor)
    for reused, result in zip(reused_flags, results):
        result["reused"] = reused
    return {
        "registered": len(results),
        "reused": sum(reused_flags),
        "file_ids": [result["file_id"] for result in results],
        "items": results,
    }


def fanout(
        project: Project,
        *,
        assignments_file_id: str,
        source_file_ids: Iterable[str],
        entity_type: str,
        entity_id: str,
        role_prefix: str,
        unit_column: str = "unit",
        seqid_column: str = "seqid",
        parent_run_id: str | None = None,
        actor: str | None = None,
        dry_run: bool = False,
) -> dict[str, Any]:
    """Run ``operon fanout`` (or its ``--dry-run`` preflight) like the CLI."""
    from operon.fanout import fanout_units

    source_file_ids = [file_id for file_id in source_file_ids if str(file_id).strip()]
    missing = [name for name, value in (
        ("--assignments-file", assignments_file_id), ("--entity-type", entity_type),
        ("--entity-id", entity_id), ("--role-prefix", role_prefix),
    ) if not str(value or "").strip()]
    if missing:
        raise ValidationError(f"fanout requires {', '.join(missing)}")
    if not source_file_ids:
        raise ValidationError("fanout requires at least one --source-file FILE_ID")
    parts = [
        "operon", "fanout", "--assignments-file", assignments_file_id,
        "--entity-type", entity_type, "--entity-id", entity_id,
        "--role-prefix", role_prefix,
        "--unit-column", unit_column, "--seqid-column", seqid_column,
    ]
    for file_id in source_file_ids:
        parts += ["--source-file", file_id]
    if parent_run_id:
        parts += ["--parent-run-id", parent_run_id]
    if dry_run:
        parts.append("--dry-run")
    resolved_actor = (actor or os.environ.get("USER") or "fanout").strip()
    with _open_writable(project) as db:
        return fanout_units(
            db, project,
            assignments_file_id=assignments_file_id,
            source_file_ids=source_file_ids,
            entity_type=entity_type, entity_id=entity_id,
            role_prefix=role_prefix,
            unit_column=unit_column, seqid_column=seqid_column,
            parent_run_id=parent_run_id, actor=resolved_actor,
            dry_run=dry_run, command=shlex.join(parts),
        )


def _validate_config_name(kind: str, name: str) -> None:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValidationError(f"invalid {kind} name {name!r}")


def coerce_scalar(text: str) -> Any:
    """Return an int/float when ``text`` looks numeric, else the string itself."""
    text = text.strip()
    if not text:
        return ""
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _coerce_rule_values(rule: dict[str, Any]) -> None:
    for key in ("value", "min", "max"):
        if isinstance(rule.get(key), str):
            rule[key] = coerce_scalar(rule[key])
    if isinstance(rule.get("values"), list):
        rule["values"] = [coerce_scalar(v) if isinstance(v, str) else v for v in rule["values"]]


def _coerce_classification_values(document: dict[str, Any]) -> None:
    """Coerce numeric-looking condition operands like :func:`_coerce_rule_values`.

    The classification grammar compares numerically with a string fallback, so
    the form's text inputs are coerced here before validation; ``any``/``not``
    groups are walked recursively.
    """
    def coerce_condition(condition: Any) -> None:
        if not isinstance(condition, dict):
            return
        if "any" in condition:
            for sub in condition["any"] or []:
                coerce_condition(sub)
            return
        if "not" in condition:
            coerce_condition(condition["not"])
            return
        _coerce_rule_values(condition)

    sources = document.get("sources")
    if isinstance(sources, dict):
        for source in sources.values():
            if isinstance(source, dict):
                for condition in source.get("filter") or []:
                    coerce_condition(condition)
    for rule in document.get("rules") or []:
        if isinstance(rule, dict):
            for condition in rule.get("when") or []:
                coerce_condition(condition)


def _validate_classification_document(name: str, document: dict[str, Any]) -> None:
    """Validate a ``kind: sequence_classification`` document with the core rules."""
    from operon.classify import validate_classification_profile

    if "version" not in document:
        raise ValidationError(f"profile {name!r}: 'version' is required")
    validate_classification_profile(document, name)


def _validate_coverage_document(name: str, document: dict[str, Any]) -> None:
    """Validate a ``kind: taxonomy_coverage`` document with the core rules."""
    from operon.taxonomy import _validate_coverage_profile

    if "version" not in document:
        raise ValidationError(f"profile {name!r}: 'version' is required")
    _validate_coverage_profile(name, document)


def _validate_profile_document(name: str, document: dict[str, Any], *,
                               kind: str = "qc") -> None:
    if not isinstance(document, dict):
        raise ValidationError(f"profile {name!r}: document must be a mapping")
    if str(document.get("kind", "")) != kind:
        raise ValidationError(
            f"profile {name!r}: only kind {kind!r} profiles can be saved from the TUI; "
            f"got {document.get('kind')!r}"
        )
    if kind == CLASSIFICATION_KIND:
        _validate_classification_document(name, document)
        return
    if kind == COVERAGE_KIND:
        _validate_coverage_document(name, document)
        return
    if "version" not in document:
        raise ValidationError(f"profile {name!r}: 'version' is required")
    applies_to = document.get("applies_to", [])
    if not isinstance(applies_to, list):
        raise ValidationError(f"profile {name!r}: 'applies_to' must be a list")
    unknown = sorted(set(map(str, applies_to)) - set(ENTITY_TYPE_NAMES))
    if unknown:
        raise ValidationError(
            f"profile {name!r}: unknown entity types in applies_to: {', '.join(unknown)}"
        )
    for section in ("required", "warnings"):
        rules = document.get(section, [])
        if not isinstance(rules, list):
            raise ValidationError(f"profile {name!r}: '{section}' must be a list of rules")
        for index, rule in enumerate(rules, start=1):
            if not isinstance(rule, dict):
                raise ValidationError(f"profile {name!r}: {section} rule {index} must be a mapping")
            label = f"{section} rule {index}"
            if not str(rule.get("metric", "")).strip():
                raise ValidationError(f"profile {name!r}: {label}: metric is required")
            if not str(rule.get("code", "")).strip():
                raise ValidationError(f"profile {name!r}: {label}: code is required")
            operator = rule.get("operator")
            if operator not in PROFILE_OPERATORS:
                raise ValidationError(
                    f"profile {name!r}: {label} ({rule.get('metric')}): "
                    f"unknown operator {operator!r}"
                )


@contextmanager
def _saved_config(path: Path, text: str, previous_text: str | None,
                  label: str) -> Iterator[None]:
    """Publish configuration atomically and restore it if validation or storage fails."""
    try:
        atomic_write_text(path, text)
        yield
    except BaseException as exc:
        if previous_text is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_text(path, previous_text)
        if isinstance(exc, Exception):
            raise ValidationError(f"{label}: save rolled back: {exc}") from exc
        raise


def save_profile(project: Project, name: str, document: dict[str, Any], *,
                 known_version: int = 0, kind: str = "qc") -> dict[str, Any]:
    """Validate and save a profile of ``kind`` as a new version.

    The composed document is validated (``qc`` rules, the core's
    :func:`operon.classify.validate_classification_profile` for
    ``sequence_classification``, or the core's coverage-profile validator for
    ``taxonomy_coverage``), written to ``config/profiles/<name>.yaml``
    with the same header style as :func:`operon.profiles.write_default_profiles`,
    round-trip verified through :func:`operon.profiles.load_profile`, and
    recorded as a content-addressed snapshot with the exact canonical document
    the core records when it consumes the profile (``qc_profiles`` serves both
    kinds).  On any failure the previous file content is restored.  Saving
    unchanged content is a no-op: the version is not bumped and no snapshot is
    recorded.

    ``known_version`` preserves the editor's last observed file version if
    the file disappears before its first snapshot is recorded.
    """
    from operon.profiles import load_profile
    from operon.tui.data import config_version_floor
    from operon.utils import now_iso

    _validate_config_name("profile", name)
    if kind not in PROFILE_KINDS:
        raise ValidationError(f"profile {name!r}: unknown kind {kind!r}")
    if not isinstance(document, dict) or str(document.get("kind", kind)) != kind:
        raise ValidationError(
            f"profile {name!r}: only kind {kind!r} profiles can be saved from the TUI; "
            f"got {document.get('kind')!r}"
        )
    document = {str(key): value for key, value in document.items()}
    path = project.profiles_dir / f"{name}.yaml"
    previous_text = path.read_bytes().decode("utf-8") if path.exists() else None
    existing: dict[str, Any] | None = None
    if previous_text is not None:
        parsed = yaml.safe_load(previous_text)
        if not isinstance(parsed, dict):
            raise ValidationError(f"profile {name!r}: existing file is not a YAML mapping")
        existing = parsed
        if str(existing.get("kind")) != kind:
            raise ValidationError(
                f"profile {name!r}: on-disk kind is {existing.get('kind')!r}; refusing to edit"
            )
        old_version = int(existing.get("version", 1))
        comparable = {key: value for key, value in document.items() if key != "version"}
        existing_content = {key: value for key, value in existing.items() if key != "version"}
        if comparable == existing_content:
            return {
                "name": name, "version": old_version, "sha256": None,
                "snapshot_id": None, "unchanged": True,
            }
    document["version"] = config_version_floor(
        project, "profile", name,
        max(known_version, int(existing.get("version", 1)) if existing else 0),
    ) + 1
    if kind == CLASSIFICATION_KIND:
        _coerce_classification_values(document)
    elif kind == COVERAGE_KIND:
        pass  # the coverage form composes typed values; nothing to coerce
    else:
        for section in ("required", "warnings"):
            for rule in document.get(section, []) or []:
                if isinstance(rule, dict):
                    _coerce_rule_values(rule)
    _validate_profile_document(name, document, kind=kind)

    text = (
        f"# Operon {kind} profile {name} "
        "(versioned; review and rename before changing a frozen definition)\n"
        + yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    )
    with _saved_config(path, text, previous_text, f"profile {name!r}"):
        loaded = load_profile(project.profiles_dir, name, expected_kind=kind)
        profile_document = json.dumps(loaded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        sha256 = hashlib.sha256(profile_document.encode("utf-8")).hexdigest()
        version = int(loaded.get("version", 1))
        with _open_writable(project) as db:
            with db.transaction():
                snapshot_id = db.record_profile(name, version, sha256, profile_document, now_iso())
    return {
        "name": name, "version": version, "sha256": sha256,
        "snapshot_id": snapshot_id, "unchanged": False,
    }


def save_classification_profile(project: Project, name: str, document: dict[str, Any], *,
                                known_version: int = 0) -> dict[str, Any]:
    """Save a ``kind: sequence_classification`` profile (see :func:`save_profile`)."""
    return save_profile(project, name, document, known_version=known_version,
                        kind=CLASSIFICATION_KIND)


def save_coverage_profile(project: Project, name: str, document: dict[str, Any], *,
                          known_version: int = 0) -> dict[str, Any]:
    """Save a ``kind: taxonomy_coverage`` profile (see :func:`save_profile`)."""
    return save_profile(project, name, document, known_version=known_version,
                        kind=COVERAGE_KIND)


def save_recipe(
        project: Project,
        tool_name: str,
        recipe_name: str,
        recipe_doc: dict[str, Any],
        *,
        known_version: int = 0,
) -> dict[str, Any]:
    """Validate and save one recipe inside ``config/tools.yaml`` as a new version.

    Only ``tools[tool_name]["recipes"][recipe_name]`` is replaced; the rest of
    the configuration is kept as parsed.  The recipe ``version`` is bumped,
    the whole file is written back with ``yaml.safe_dump(sort_keys=False)``,
    and the result is round-trip verified through
    :func:`operon.tools.load_tools_config` + ``get_recipe``; on any failure
    the previous file content is restored.  The snapshot is recorded with the
    same ``{"recipe": ..., "tool": ...}`` document shape
    :func:`operon.tools.run_analysis` uses.

    NOTE: saving from the TUI normalizes tools.yaml formatting and drops
    hand-written comments; every saved version is preserved verbatim in
    ``recipe_snapshots`` (see ``operon recipes history/show``).

    ``known_version`` is the editor's version floor; current file and
    snapshot versions are checked again at save time.
    """
    from operon.tools import get_recipe, get_tool, load_tools_config
    from operon.tui.data import config_version_floor

    _validate_config_name("recipe", recipe_name)
    if not isinstance(recipe_doc, dict):
        raise ValidationError(f"recipe {recipe_name!r}: document must be a mapping")
    path = project.tools_config_path
    previous_text = path.read_bytes().decode("utf-8") if path.exists() else None
    config = load_tools_config(project)
    tools = config.get("tools")
    if not isinstance(tools, dict) or tool_name not in tools:
        raise ValidationError(f"unknown tool {tool_name!r} in {path}")
    tool_raw = tools[tool_name]
    if not isinstance(tool_raw, dict):
        raise ValidationError(f"tool {tool_name!r} in tools.yaml must be a mapping")
    recipes = tool_raw.setdefault("recipes", {})
    if not isinstance(recipes, dict):
        raise ValidationError(f"tool {tool_name!r}: recipes must be a mapping")
    existing = recipes.get(recipe_name)
    if existing is not None and not isinstance(existing, dict):
        raise ValidationError(f"recipe {recipe_name!r} in tools.yaml must be a mapping")

    document = dict(recipe_doc)
    if existing is not None:
        old_version = int(existing.get("version", 1))
        comparable = {key: value for key, value in document.items() if key != "version"}
        existing_content = {key: value for key, value in existing.items() if key != "version"}
        if comparable == existing_content:
            return {
                "name": recipe_name, "tool": tool_name, "version": old_version,
                "snapshot_id": None, "unchanged": True,
            }
    document["version"] = config_version_floor(
        project, "recipe", recipe_name,
        max(known_version, int(existing.get("version", 1)) if existing else 0),
    ) + 1
    recipes[recipe_name] = document

    text = (
        "# Operon external tools configuration (YAML)\n"
        "# Saved via the Operon TUI: formatting is normalized and hand-written\n"
        "# comments are dropped; every version is kept in recipe_snapshots.\n"
        + yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    )
    with _saved_config(path, text, previous_text, f"recipe {recipe_name!r}"):
        load_tools_config(project)
        recipe = get_recipe(project, recipe_name)
        tool = get_tool(project, tool_name)
        with _open_writable(project) as db, db.transaction():
            snapshot_id = db.record_recipe(
                recipe.name, recipe.version, {"recipe": recipe.raw, "tool": tool.raw}
            )
    return {
        "name": recipe.name, "tool": tool_name, "version": recipe.version,
        "snapshot_id": snapshot_id, "unchanged": False,
    }


def check_tools(
        project: Project,
        timeout: float = 60.0,
        on_result: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Probe every configured tool's version, like ``operon tools-check``.

    Runs external commands; call from a worker thread, never the UI thread.
    One broken tool never breaks the batch: its row gets ``ok=False`` and the
    error message.  ``on_result`` (when given) is invoked with each row as it
    completes, for live UI updates.
    """
    from operon.tools import detect_tool_version, get_tool, load_tools_config

    config = load_tools_config(project)
    results: list[dict[str, Any]] = []
    for tool_name, raw in config.get("tools", {}).items():
        if not isinstance(raw, dict):
            continue
        entry: dict[str, Any] = {
            "name": str(tool_name),
            "executable": str(raw.get("executable", tool_name)),
            "run_method": "",
            "version": None,
            "ok": False,
            "error": None,
        }
        try:
            tool = get_tool(project, str(tool_name))
            entry["executable"] = tool.executable
            entry["run_method"] = tool.run_method
            entry["version"] = detect_tool_version(tool, config, timeout=timeout)
            entry["ok"] = True
        except Exception as exc:  # noqa: BLE001 - one bad tool must not break the batch  # pylint: disable=broad-exception-caught
            entry["error"] = str(exc)
        results.append(entry)
        if on_result is not None:
            on_result(entry)
    return results


# ---------------------------------------------------------------------------
# Phase 3: import wizard, release/export builders, coverage reports.
# ---------------------------------------------------------------------------


def import_dataset(project: Project, draft: dict[str, Any]) -> dict[str, Any]:
    """Commit an import-wizard draft like ``operon import dataset``.

    The draft is built by the TUI wizard with the same shape the questionary
    wizard produces; the actual commit is the shared single-transaction
    :func:`operon.import_wizard._commit` (data-source registration, entity
    rows, state, audit, file ingest with staged-file rollback, run logging).
    """
    from operon.import_wizard import _commit

    with _open_writable(project) as db:
        return _commit(db, project, draft)


class NcbiDatasetsCancelled(Exception):
    """Raised when the NCBI Datasets import worker is cancelled by the user.

    The core reports cooperative cancellation with ``ShutdownRequested`` — the
    same exception a SIGINT produces — once ``cancel_event`` is set, so the run
    row is recorded as ``interrupted`` and stays resumable with
    ``--resume-run``.  This wrapper is the TUI's own signal: the modal reports
    it as a cancelled run instead of a failure.
    """


def ncbi_datasets(
        project: Project,
        *,
        inputs: Iterable[str] = (),
        accessions: Iterable[str] = (),
        accession_file: str | None = None,
        include: Iterable[str] | None = None,
        archive_files: bool = True,
        standardize: bool = False,
        dry_run: bool = False,
        preserve_sources: bool = True,
        email: str | None = None,
        api_key: str | None = None,
        timeout: float = 300.0,
        batch_size: int = 10,
        download_workers: int = 3,
        retries: int = 4,
        retry_backoff: float = 1.0,
        resume_run_id: str | None = None,
        plan_only: bool = False,
        cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run the NCBI Datasets adapter like ``operon ncbi-datasets``.

    Same value domain and validation messages as the CLI/core, checked here
    before the core call so a form-level problem writes nothing.  ``include``
    of ``None`` means the CLI default (all supported types); empty-string
    ``email``/``api_key`` count as not provided (the core falls back to
    ``NCBI_EMAIL``/``NCBI_API_KEY``).  The core's stdout (cache notes, plans)
    is captured into the returned ``messages``.  ``cancel_event`` is forwarded
    to the downloader: once set, the run aborts with the interrupt bookkeeping
    of a signal and is reported here as :class:`NcbiDatasetsCancelled`.
    """
    from operon.adapters.ncbi_datasets import (
        DEFAULT_INCLUDES,
        INCLUDE_TYPES,
        VERSIONED_ACCESSION_RE,
        run_ncbi_datasets_adapter,
    )
    from operon.shutdown import ShutdownRequested

    inputs = [str(value).strip() for value in inputs if str(value).strip()]
    accessions = [str(value).strip() for value in accessions if str(value).strip()]
    if not inputs and not accessions and not (accession_file or "").strip():
        raise ValidationError("provide at least one --input, --accession, or --accession-file")
    if plan_only and inputs:
        raise ValidationError("--plan-only supports accession requests, not offline --input packages")
    for accession in accessions:
        if not VERSIONED_ACCESSION_RE.fullmatch(accession.upper()):
            raise ValidationError(f"invalid NCBI assembly accession: {accession!r}")
    includes = [str(value).strip() for value in include if str(value).strip()] \
        if include is not None else None
    unknown_includes = sorted(set(includes or ()) - set(INCLUDE_TYPES))
    if unknown_includes:
        raise ValidationError(f"unknown NCBI include type(s): {unknown_includes}")
    batch_size = int(batch_size)
    if batch_size < 1 or batch_size > 100:
        raise ValidationError("--batch-size must be between 1 and 100")
    download_workers = int(download_workers)
    if download_workers < 1 or download_workers > 10:
        raise ValidationError("--download-workers must be between 1 and 10")
    retries = int(retries)
    if retries < 0 or retries > 10:
        raise ValidationError("--retries must be between 0 and 10")
    retry_backoff = float(retry_backoff)
    if retry_backoff < 0:
        raise ValidationError("--retry-backoff must be >= 0")
    timeout = float(timeout)
    if timeout <= 0:
        raise ValidationError("timeout must be a positive number")

    buffer = io.StringIO()
    try:
        with _open_writable(project) as db, contextlib.redirect_stdout(buffer):
            summary = run_ncbi_datasets_adapter(
                db,
                project,
                inputs=inputs,
                accessions=accessions,
                accession_file=(accession_file or "").strip() or None,
                includes=includes or DEFAULT_INCLUDES,
                archive_files=archive_files,
                standardize=standardize,
                dry_run=dry_run,
                preserve_sources=preserve_sources,
                email=(email or "").strip() or None,
                api_key=(api_key or "").strip() or None,
                timeout=timeout,
                batch_size=batch_size,
                download_workers=download_workers,
                max_retries=retries,
                retry_backoff=retry_backoff,
                resume_run_id=(resume_run_id or "").strip() or None,
                plan_only=plan_only,
                cancel_event=cancel_event,
            )
    except ShutdownRequested:
        # A cooperative cancel reaches the core as the signal-style interrupt;
        # any other source (a real SIGINT in a main-thread caller) keeps its
        # own exception type.
        if cancel_event is not None and cancel_event.is_set():
            raise NcbiDatasetsCancelled() from None
        raise
    return {**summary, "messages": buffer.getvalue()}


def reserve_entity_ids(project: Project) -> dict[str, str]:
    """Reserve one fresh internal ID per entity type for the import wizard.

    Reservations that end up unused (the user reuses an existing entity or
    cancels) simply become gaps, which :meth:`Database.next_id` explicitly
    allows.
    """
    with _open_writable(project) as db:
        return {entity_type: db.next_id(entity_type) for entity_type in ENTITY_TYPE_NAMES}


def create_release(
        project: Project,
        version: str,
        profile: str,
        copy_files: bool = False,
        link_kind: str = "copy",
) -> dict[str, Any]:
    """Create an immutable release like ``operon release``; FileExistsError propagates."""
    from operon.release import create_release as _create_release

    with _open_writable(project) as db:
        return _create_release(
            db, project, version, profile, copy_files=copy_files, link_kind=link_kind,
        )


def export(
        project: Project,
        output_dir: str,
        *,
        entity_type: str | None = None,
        entity_ids: list[str] | None = None,
        file_ids: list[str] | None = None,
        file_role: str | None = None,
        fmt: str | None = None,
        state: str | None = None,
        decision: str | None = None,
        profile: str | None = None,
        link_kind: str = "copy",
        include_qc: bool = True,
) -> dict[str, Any]:
    """Materialize a selective export like ``operon export``; FileExistsError propagates."""
    from operon.export import export_files

    with _open_writable(project) as db:
        return export_files(
            db, project,
            output_dir=output_dir,
            entity_type=entity_type,
            entity_ids=entity_ids or (),
            file_ids=file_ids or (),
            file_role=file_role,
            fmt=fmt,
            state=state,
            decision=decision,
            profile=profile,
            link_kind=link_kind,
            include_qc=include_qc,
        )


def run_coverage(
        project: Project,
        reference_set_id: str,
        release_version: str | None = None,
) -> dict[str, Any]:
    """Generate a taxonomy coverage report like ``operon report coverage``.

    A report below its thresholds is not an exception: the result dict carries
    ``decision="FAIL"`` and ``exit_code=1`` (the CLI maps that to its exit
    status), so callers render it as a warning result, not a crash.
    """
    from operon.coverage import report_coverage

    with _open_writable(project) as db:
        return report_coverage(db, project, reference_set_id, release_version=release_version)


def import_taxonomy(
        project: Project,
        source: str,
        taxonomy_version: str,
) -> dict[str, Any]:
    """Import an NCBI taxonomy package like ``operon taxonomy import``.

    The core archives the content-addressed source, imports nodes/aliases in
    one transaction, and records the same audit and run rows as the CLI;
    identical version and bytes reuse the existing snapshot (``reused``).
    """
    from operon.taxonomy import import_ncbi_taxonomy

    with _open_writable(project) as db:
        return import_ncbi_taxonomy(db, project, source, taxonomy_version)


def compile_reference_set(
        project: Project,
        profile_name: str,
        taxonomy_version: str,
) -> dict[str, Any]:
    """Compile a coverage denominator like ``operon taxonomy compile``.

    The core freezes taxonomy/reference_sets/<profile>@<version>.tsv plus its
    provenance sidecar in one transaction with the same audit/run rows as the
    CLI; failed attempts are recorded as ``failed`` workflow runs, and
    identical profile/snapshot/bytes reuse the existing reference set.
    """
    from operon.taxonomy import compile_reference_set as _compile

    with _open_writable(project) as db:
        return _compile(db, project, profile_name, taxonomy_version)
