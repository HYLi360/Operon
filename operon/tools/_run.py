"""Top-level ``run_analysis`` orchestration.

The two-phase batch loop (plan every file, then execute), the cooperative
cancel/abort contract, and the Slurm job-array path with its interrupt
finalization."""

from __future__ import annotations

import inspect
import shlex
import threading
import time
from collections.abc import Callable
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.errors import ExternalToolError
from operon.shutdown import ShutdownRequested, cleanup_completed, graceful_shutdown
from operon.utils import now_iso

from ._cache import _raise_if_cancelled, _sweep_stale_running_jobs
from ._config import Recipe, ToolSpec, get_recipe, get_tool, load_tools_config
from ._execute import (
    _analysis_error_result,
    _env_policy_extra_details,
    _execute_analysis_plan,
    _fail_analysis_execution,
    _finalize_analysis_execution,
    _interrupt_analysis_execution,
    run_analysis_for_file,
)
from ._inputs import candidate_files, resolve_runtime_parameters
from ._plan import _AnalysisExecution, plan_analysis_for_file


def run_analysis(
    project: Project,
    db: Database,
    analysis_name: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    limit: int | None = None,
    threads: int | None = None,
    backend: str | None = None,
    keep_partial: bool = False,
    runtime_parameters: dict[str, str] | None = None,
    progress_callback: Callable[[int, int, str, str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Execute one configured analysis over all matching manifest files.

    ``progress_callback``, when given, is invoked per file as
    ``progress_callback(index, total, file_id, phase)`` with a 1-based
    ``index``; ``phase`` is ``"start"`` before the file's job begins and the
    result status (``completed``/``cached``/``error``/...) after it ends.

    ``cancel_event``, when given, is checked at every file/planning/collection
    boundary and — for executors that accept it — while a job array is still
    waiting on the scheduler; once set, the batch aborts with
    ``ShutdownRequested`` and the same interrupt bookkeeping as a signal.
    """
    recipe = get_recipe(project, analysis_name)
    resolved_parameters = resolve_runtime_parameters(recipe, runtime_parameters)
    tool = get_tool(project, recipe.tool_name)
    config = load_tools_config(project)
    threads = int(
        threads or project.config.get("resources", {}).get("default_threads", 4) or 4
    )
    files = candidate_files(db, recipe, entity_type=entity_type, entity_id=entity_id)
    if limit is not None:
        files = files[: max(0, int(limit))]
    if not files:
        role_selector = (
            f"file_role_prefix={recipe.file_role_prefix}"
            if recipe.file_role_prefix
            else f"file_role={recipe.file_role}"
        )
        print(
            f"no candidate files for {analysis_name} "
            f"(entity_type={recipe.entity_type or 'any'}, {role_selector}, format={recipe.fmt})"
        )
        return []

    from operon.execution import get_executor

    recipe_slurm = recipe.raw.get("slurm")
    executor = get_executor(
        project,
        backend,
        slurm_overrides=recipe_slurm if isinstance(recipe_slurm, dict) else None,
    )
    results: list[dict[str, Any]] = []
    try:
        with graceful_shutdown():
            if not dry_run:
                _sweep_stale_running_jobs(db, analysis_name)
            total = len(files)
            slurm_config = getattr(executor, "slurm", None)
            array_requested = (
                not dry_run
                and slurm_config is not None
                and bool(getattr(slurm_config, "array", False))
                and getattr(executor, "run_array", None) is not None
            )
            if array_requested:
                return _run_analysis_two_phase(
                    project,
                    db,
                    recipe,
                    tool,
                    config,
                    files,
                    executor,
                    force=force,
                    threads=threads,
                    keep_partial=keep_partial,
                    runtime_parameters=resolved_parameters,
                    array_concurrency=slurm_config.array_concurrency,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                )
            for index, file_record in enumerate(files, start=1):
                _raise_if_cancelled(cancel_event)
                if progress_callback is not None:
                    progress_callback(index, total, file_record["file_id"], "start")
                try:
                    result = run_analysis_for_file(
                        project,
                        db,
                        recipe,
                        tool,
                        config,
                        file_record,
                        dry_run=dry_run,
                        force=force,
                        threads=threads,
                        backend=backend,
                        executor=executor,
                        keep_partial=keep_partial,
                        runtime_parameters=resolved_parameters,
                    )
                except ShutdownRequested:
                    # Bookkeeping already finalized in run_analysis_for_file;
                    # stop the batch here and let the CLI report exit 130.
                    raise
                except Exception as exc:  # noqa: BLE001 - per-file analysis failures become error results; interrupts re-raise  # pylint: disable=broad-exception-caught
                    result = _analysis_error_result(file_record, recipe, exc)
                results.append(result)
                if progress_callback is not None:
                    progress_callback(
                        index,
                        total,
                        file_record["file_id"],
                        str(result.get("status", "done")),
                    )
    finally:
        close = getattr(executor, "close", None)
        if close is not None:
            close()
    return results


def _run_analysis_two_phase(
    project: Project,
    db: Database,
    recipe: Recipe,
    tool: ToolSpec,
    config: dict[str, Any],
    files: list[dict[str, Any]],
    executor: Any,
    *,
    force: bool,
    threads: int,
    keep_partial: bool,
    runtime_parameters: dict[str, str] | None,
    array_concurrency: int | None,
    progress_callback: Callable[[int, int, str, str], None] | None,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Array-enabled analyze: plan per file, submit once, collect per task.

    Phase 1 applies the exact per-file decisions of the sequential path
    (input verification, cache reuse, adopt, force, version probing) and only
    collects the files that need real computation.  Phase 2 submits them as
    one job array; phase 3 replays the sequential post-run bookkeeping per
    task, so every file keeps its own independent analysis_jobs and
    workflow_runs rows (scheduler_job_id is the per-task
    ``<array_id>_<task_index>``).
    """
    total = len(files)
    results: list[dict[str, Any] | None] = [None] * total
    pending: list[tuple[int, _AnalysisExecution]] = []
    for index, file_record in enumerate(files, start=1):
        _raise_if_cancelled(cancel_event)
        if progress_callback is not None:
            progress_callback(index, total, file_record["file_id"], "start")
        try:
            plan = plan_analysis_for_file(
                project,
                db,
                recipe,
                tool,
                config,
                file_record,
                dry_run=False,
                force=force,
                threads=threads,
                executor=executor,
                runtime_parameters=runtime_parameters,
            )
        except ShutdownRequested:
            raise
        except Exception as exc:
            results[index - 1] = _analysis_error_result(file_record, recipe, exc)
            if progress_callback is not None:
                progress_callback(index, total, file_record["file_id"], "error")
            continue
        if isinstance(plan, _AnalysisExecution):
            pending.append((index, plan))
        else:
            results[index - 1] = plan
            if progress_callback is not None:
                progress_callback(
                    index,
                    total,
                    file_record["file_id"],
                    str(plan.get("status", "done")),
                )
    # A job array dispatches one shell line per task; multi-step command
    # chains keep the sequential per-file path.  Below two array-eligible
    # tasks the array buys nothing, so they fall back to per-file submission
    # as well.
    array_plans = [(i, p) for i, p in pending if len(p.argv_steps) == 1]
    sequential = pending
    if len(array_plans) >= 2:
        _execute_analysis_array(
            project,
            db,
            recipe,
            tool,
            executor,
            array_plans,
            results,
            threads=threads,
            array_concurrency=array_concurrency,
            keep_partial=keep_partial,
            progress_callback=progress_callback,
            total=total,
            cancel_event=cancel_event,
        )
        sequential = [(i, p) for i, p in pending if len(p.argv_steps) != 1]
    for index, plan in sequential:
        _raise_if_cancelled(cancel_event)
        try:
            run_record = _execute_analysis_plan(
                project, db, recipe, tool, executor, plan
            )
            outcome = _finalize_analysis_execution(
                project, db, recipe, tool, plan, run_record, keep_partial=keep_partial
            )
        except ShutdownRequested as exc:
            _interrupt_analysis_execution(
                project, db, plan, exc, keep_partial=keep_partial
            )
            cleanup_completed()
            raise
        except Exception as exc:
            _fail_analysis_execution(project, db, plan, exc, keep_partial=keep_partial)
            outcome = _analysis_error_result(plan.file_record, recipe, exc)
        results[index - 1] = outcome
        if progress_callback is not None:
            progress_callback(
                index,
                total,
                plan.file_record["file_id"],
                str(outcome.get("status", "done")),
            )
    return [result for result in results if result is not None]


class _CallbackAbortedBatch(Exception):
    """Internal sentinel: a progress-callback exception already ran the abort
    bookkeeping, so the batch failure handlers must re-raise it untouched."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(f"{type(original).__name__}: {original}")
        self.original = original


def _execute_analysis_array(
    project: Project,
    db: Database,
    recipe: Recipe,
    tool: ToolSpec,
    executor: Any,
    indexed_plans: list[tuple[int, _AnalysisExecution]],
    results: list[dict[str, Any] | None],
    *,
    threads: int,
    array_concurrency: int | None,
    keep_partial: bool,
    progress_callback: Callable[[int, int, str, str], None] | None,
    total: int,
    cancel_event: threading.Event | None = None,
) -> None:
    """Submit planned files as one job array and collect each task's result.

    ``run_array`` performs neither the input staging nor the remote output
    backup that ``SSHExecutor.run`` does, so both are applied here per task:
    inputs are staged and existing remote outputs are moved aside before
    submission; a completed task's output is pulled and its backup dropped, a
    failed/interrupted task's backup is restored — the same guarantee the
    per-file path gives.  (The local Slurm backend needs neither:
    ``SlurmExecutor.run`` itself performs no staging or backup — outputs live
    on the shared filesystem and a previous local output was already removed
    during planning.)
    """
    from operon.workflow import record_execution_result

    logs = project.logs_root
    logs.mkdir(parents=True, exist_ok=True)
    ssh_remote = executor.name == "ssh" and bool(getattr(executor, "remote_root", ""))
    client = None
    sftp = None
    finalized: set[int] = set()
    started = now_iso()
    started_monotonic = time.monotonic()
    batch: list[tuple[int, _AnalysisExecution]] = []

    def report_progress(index: int, file_id: str, phase: str) -> None:
        """Invoke the progress callback; its exceptions abort the batch.

        The callback is how cooperative callers (e.g. the TUI) cancel, so its
        exceptions must reach the caller and never feed the per-file or
        whole-batch failure paths.  A KeyboardInterrupt subclass keeps the
        outer interrupt path; any other exception finalizes the tasks that
        already wrote their exit-code file, marks the remaining plans
        interrupted, and re-raises.
        """
        if progress_callback is None:
            return
        try:
            progress_callback(index, total, file_id, phase)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _finalize_interrupted_array(
                project,
                db,
                recipe,
                tool,
                executor,
                batch,
                results,
                finalized,
                sftp=sftp,
                client=client,
                keep_partial=keep_partial,
                started=started,
            )
            for _, pending_plan in indexed_plans:
                if pending_plan.job_id in finalized:
                    continue
                if sftp is not None and pending_plan.backups:
                    executor._restore_output_backups(sftp, pending_plan.backups)
                    pending_plan.backups = []
                _interrupt_analysis_execution(
                    project, db, pending_plan, exc, keep_partial=keep_partial
                )
            cleanup_completed()
            raise _CallbackAbortedBatch(exc) from exc

    try:
        if ssh_remote:
            client = executor._connect()
            sftp = client.open_sftp()
            for index, plan in indexed_plans:
                try:
                    executor._stage_inputs(client, sftp, plan.stage_inputs)
                    plan.backups = executor._reset_outputs(sftp, [plan.output_path])
                except Exception as exc:
                    _fail_analysis_execution(
                        project, db, plan, exc, keep_partial=keep_partial
                    )
                    finalized.add(plan.job_id)
                    results[index - 1] = _analysis_error_result(
                        plan.file_record, recipe, exc
                    )
                    report_progress(index, plan.file_record["file_id"], "error")
                else:
                    batch.append((index, plan))
        else:
            batch = list(indexed_plans)
        if not batch:
            return
        tasks = [
            {
                "run_id": plan.run_id,
                "command": shlex.join(plan.argv_steps[0]),
                "stdout_path": plan.stdout_path,
                "stderr_path": plan.stderr_path,
            }
            for _, plan in batch
        ]
        _raise_if_cancelled(cancel_event)
        # Only forward the event to executors whose run_array knows the
        # keyword; duck-typed executors without it simply never cancel
        # mid-array and the collection-loop boundary still applies.
        run_array_kwargs: dict[str, Any] = {}
        if "cancel_event" in inspect.signature(executor.run_array).parameters:
            run_array_kwargs["cancel_event"] = cancel_event
        try:
            exec_results = executor.run_array(
                tasks,
                cwd=project.root,
                threads=threads,
                array_concurrency=array_concurrency,
                **run_array_kwargs,
            )
        except KeyboardInterrupt:
            _finalize_interrupted_array(
                project,
                db,
                recipe,
                tool,
                executor,
                batch,
                results,
                finalized,
                sftp=sftp,
                client=client,
                keep_partial=keep_partial,
                started=started,
            )
            cleanup_completed()
            raise
        if len(exec_results) != len(batch):
            raise ExternalToolError(
                f"run_array returned {len(exec_results)} results for {len(batch)} tasks"
            )
        duration = round(time.monotonic() - started_monotonic, 3)
        for (index, plan), result in zip(batch, exec_results):
            _raise_if_cancelled(cancel_event)
            try:
                if sftp is not None and plan.backups:
                    if result.exit_code == 0 and not result.error:
                        executor._pull_outputs(client, sftp, [plan.output_path])
                        executor._drop_output_backups(sftp, plan.backups)
                    else:
                        executor._restore_output_backups(sftp, plan.backups)
                    plan.backups = []
                run_record = record_execution_result(
                    db,
                    project,
                    result,
                    run_id=plan.run_id,
                    argv=plan.argv_steps[0],
                    step=f"analysis:{recipe.name}",
                    entity_type=plan.file_record["entity_type"],
                    entity_id=plan.file_record["entity_id"],
                    parameter_set=f"{recipe.name}:{plan.version}",
                    expected_outputs=[plan.output_path],
                    cwd=project.root,
                    tool=tool.name,
                    tool_version=plan.version,
                    threads=threads,
                    executor_name=executor.describe(),
                    started_at=started,
                    duration_seconds=duration,
                    stdout_file=plan.stdout_path,
                    stderr_file=plan.stderr_path,
                    extra_details=_env_policy_extra_details(plan),
                )
                if run_record["status"] != "completed":
                    raise RuntimeError(
                        f"analysis:{recipe.name} failed: "
                        f"{run_record.get('error') or 'unknown error'}"
                    )
                outcome = _finalize_analysis_execution(
                    project,
                    db,
                    recipe,
                    tool,
                    plan,
                    run_record,
                    keep_partial=keep_partial,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if sftp is not None and plan.backups:
                    executor._restore_output_backups(sftp, plan.backups)
                    plan.backups = []
                _fail_analysis_execution(
                    project, db, plan, exc, keep_partial=keep_partial
                )
                outcome = _analysis_error_result(plan.file_record, recipe, exc)
            finalized.add(plan.job_id)
            results[index - 1] = outcome
            report_progress(
                index, plan.file_record["file_id"], str(outcome.get("status", "done"))
            )
    except KeyboardInterrupt as exc:
        # A shutdown during staging or collection: every plan that was never
        # finalized is the in-flight file of the sequential path.
        for _, plan in indexed_plans:
            if plan.job_id in finalized:
                continue
            if sftp is not None and plan.backups:
                executor._restore_output_backups(sftp, plan.backups)
                plan.backups = []
            _interrupt_analysis_execution(
                project, db, plan, exc, keep_partial=keep_partial
            )
        cleanup_completed()
        raise
    except _CallbackAbortedBatch as aborted:
        # The progress callback aborted the batch; report_progress already
        # finalized/interrupted every plan — re-raise its original exception.
        raise aborted.original
    except Exception as exc:
        # Whole-batch failure (connect, staging setup, submission): every
        # planned file fails with the same error, as sequential submission
        # would produce per file.
        for index, plan in indexed_plans:
            if plan.job_id in finalized:
                continue
            if sftp is not None and plan.backups:
                executor._restore_output_backups(sftp, plan.backups)
                plan.backups = []
            _fail_analysis_execution(project, db, plan, exc, keep_partial=keep_partial)
            results[index - 1] = _analysis_error_result(plan.file_record, recipe, exc)
            if progress_callback is not None:
                progress_callback(index, total, plan.file_record["file_id"], "error")
    finally:
        if sftp is not None:
            sftp.close()


def _finalize_interrupted_array(
    project: Project,
    db: Database,
    recipe: Recipe,
    tool: ToolSpec,
    executor: Any,
    batch: list[tuple[int, _AnalysisExecution]],
    results: list[dict[str, Any] | None],
    finalized: set[int],
    *,
    sftp: Any,
    client: Any,
    keep_partial: bool,
    started: str,
) -> None:
    """Bookkeep a cancelled array: finished tasks complete, the rest interrupt.

    The executor cancels the whole array on interrupt and re-raises; which
    tasks finished is visible from the per-task ``<run_id>.exitcode`` files
    (the remote backend pulls them before propagating).  A task with an exit
    code is finalized exactly like a sequential per-file run — completed or
    failed, with its own workflow_runs row; a task without one is the
    in-flight file of the sequential interrupt path (marked interrupted by
    the caller, partial output removed, no workflow_runs row).
    """
    from operon.execution import ExecResult
    from operon.workflow import record_execution_result

    for index, plan in batch:
        if plan.job_id in finalized:
            continue
        exitcode_path = plan.stdout_path.parent / f"{plan.run_id}.exitcode"
        try:
            exit_code = int(exitcode_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        try:
            result = ExecResult(
                exit_code=exit_code,
                error=None if exit_code == 0 else f"exit code {exit_code}",
            )
            if sftp is not None and plan.backups:
                if exit_code == 0:
                    executor._pull_outputs(client, sftp, [plan.output_path])
                    executor._drop_output_backups(sftp, plan.backups)
                else:
                    executor._restore_output_backups(sftp, plan.backups)
                plan.backups = []
            run_record = record_execution_result(
                db,
                project,
                result,
                run_id=plan.run_id,
                argv=plan.argv_steps[0],
                step=f"analysis:{recipe.name}",
                entity_type=plan.file_record["entity_type"],
                entity_id=plan.file_record["entity_id"],
                parameter_set=f"{recipe.name}:{plan.version}",
                expected_outputs=[plan.output_path],
                cwd=project.root,
                tool=tool.name,
                tool_version=plan.version,
                threads=plan.threads,
                executor_name=executor.describe(),
                started_at=started,
                stdout_file=plan.stdout_path,
                stderr_file=plan.stderr_path,
                extra_details=_env_policy_extra_details(plan),
            )
            if run_record["status"] != "completed":
                raise RuntimeError(
                    f"analysis:{recipe.name} failed: "
                    f"{run_record.get('error') or 'unknown error'}"
                )
            outcome = _finalize_analysis_execution(
                project, db, recipe, tool, plan, run_record, keep_partial=keep_partial
            )
        except Exception as finalize_exc:  # noqa: BLE001 - a per-task finalize failure marks the task failed  # pylint: disable=broad-exception-caught
            # A task whose exit code exists but whose result cannot be
            # finalized (lost output, parse error) is failed, not completed.
            if sftp is not None and plan.backups:
                executor._restore_output_backups(sftp, plan.backups)
                plan.backups = []
            _fail_analysis_execution(
                project, db, plan, finalize_exc, keep_partial=keep_partial
            )
            outcome = _analysis_error_result(plan.file_record, recipe, finalize_exc)
        finalized.add(plan.job_id)
        results[index - 1] = outcome
