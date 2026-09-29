"""Execute a planned analysis job and finalize its bookkeeping.

Runs the rendered command steps, collects provenance and environment
details, parses results, and records the terminal state (complete,
failed or interrupted) including partial-output removal."""

from __future__ import annotations

from typing import Any

from operon.config import Project
from operon.database import Database
from operon.shutdown import ShutdownRequested, cleanup_completed
from operon.utils import now_iso, sha256_path

from ._config import Recipe, ToolSpec
from ._plan import (
    _AnalysisExecution,
    _remove_output_artifact,
    _require_artifact_kind,
    plan_analysis_for_file,
)
from ._results import parse_and_store_results


def _env_policy_extra_details(plan: _AnalysisExecution) -> dict[str, Any] | None:
    env_decision = plan.env_decision
    if (
        env_decision is not None
        and not env_decision["reuse"]
        and env_decision["details"]
    ):
        return {"environment_policy_check": env_decision["details"]}
    return None


def _analysis_error_result(
    file_record: dict[str, Any], recipe: Recipe, exc: BaseException
) -> dict[str, Any]:
    return {
        "file_id": file_record["file_id"],
        "entity_type": file_record["entity_type"],
        "entity_id": file_record["entity_id"],
        "analysis": recipe.name,
        "cached": False,
        "status": "error",
        "error": f"{type(exc).__name__}: {exc}",
    }


def _execute_analysis_plan(
    project: Project,
    db: Database,
    recipe: Recipe,
    tool: ToolSpec,
    executor: Any,
    plan: _AnalysisExecution,
) -> dict[str, Any]:
    """Execute one planned analysis file through the per-file executor path."""
    from operon.workflow import run_external_command

    if plan.work_dir is not None:
        # Drop stale intermediates from an earlier failed/interrupted run.
        _remove_output_artifact(project, plan.work_dir)
        plan.work_dir.mkdir(parents=True, exist_ok=True)
        prepare = getattr(executor, "prepare_database", None)
        if (
            executor.name == "ssh"
            and getattr(executor, "remote_root", "")
            and prepare is not None
        ):
            prepare(plan.work_dir, mutable_cache=True)
    return run_external_command(
        db,
        project,
        plan.argv_steps[0],
        step=f"analysis:{recipe.name}",
        entity_type=plan.file_record["entity_type"],
        entity_id=plan.file_record["entity_id"],
        parameter_set=f"{recipe.name}:{plan.version}",
        expected_outputs=[plan.output_path],
        cwd=project.root,
        tool=tool.name,
        tool_version=plan.version,
        threads=plan.threads,
        stage_inputs=plan.stage_inputs,
        executor=executor,
        run_id=plan.run_id,
        commands=plan.commands,
        command_details=plan.command_details,
        extra_details=_env_policy_extra_details(plan),
    )


def _finalize_analysis_execution(
    project: Project,
    db: Database,
    recipe: Recipe,
    tool: ToolSpec,
    plan: _AnalysisExecution,
    run_record: dict[str, Any],
    keep_partial: bool = False,
) -> dict[str, Any]:
    """Post-run success path: validate the output, parse results, complete the job."""
    file_record = plan.file_record
    _require_artifact_kind(
        plan.output_path, recipe.output_kind, f"{recipe.name} output"
    )
    output_sha = sha256_path(plan.output_path)
    hit_count, query_count, query_with_hit_count, metric_count, alignment_count = (
        parse_and_store_results(
            db,
            project,
            recipe,
            tool,
            plan.version,
            file_record,
            plan.job_id,
            plan.output_path,
            output_sha,
            runtime_parameters=plan.runtime_parameters,
        )
    )
    if plan.work_dir is not None and not keep_partial:
        _remove_output_artifact(project, plan.work_dir)
    finished = now_iso()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE analysis_jobs SET status='completed', output_relative_path=?, output_sha256=?, "
            "stdout_file=?, stderr_file=?, finished_at=?, workflow_run_id=?, environment_id=? "
            "WHERE job_id=?",
            (
                plan.output_rel,
                output_sha,
                run_record.get("stdout_file"),
                run_record.get("stderr_file"),
                finished,
                run_record.get("run_id"),
                run_record.get("environment_id"),
                plan.job_id,
            ),
        )
    return {
        "file_id": file_record["file_id"],
        "entity_type": file_record["entity_type"],
        "entity_id": file_record["entity_id"],
        "analysis": recipe.name,
        "cached": False,
        "job_id": plan.job_id,
        "tool_version": plan.version,
        "command": plan.command_display,
        "output": plan.output_rel,
        "status": "completed",
        "hit_count": hit_count,
        "query_count": query_count,
        "query_with_hit_count": query_with_hit_count,
        "metric_count": metric_count,
        "alignment_count": alignment_count,
    }


def _interrupt_analysis_execution(
    project: Project,
    db: Database,
    plan: _AnalysisExecution,
    exc: BaseException,
    keep_partial: bool = False,
) -> None:
    """Graceful shutdown: finalize the job row, drop the partial output (unless
    --keep-partial).  Partial stdout/stderr logs are kept for diagnosis."""
    signum = getattr(exc, "signum", None)
    error = f"interrupted by signal {signum}" if signum is not None else "interrupted"
    with db.transaction() as conn:
        conn.execute(
            "UPDATE analysis_jobs SET status='interrupted', finished_at=?, error=? WHERE job_id=?",
            (now_iso(), error, plan.job_id),
        )
    if not keep_partial:
        _remove_output_artifact(project, plan.output_path)
        if plan.work_dir is not None:
            _remove_output_artifact(project, plan.work_dir)


def _fail_analysis_execution(
    project: Project,
    db: Database,
    plan: _AnalysisExecution,
    exc: BaseException,
    keep_partial: bool = False,
) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE analysis_jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
            (now_iso(), f"{type(exc).__name__}: {exc}", plan.job_id),
        )
    if plan.work_dir is not None and not keep_partial:
        _remove_output_artifact(project, plan.work_dir)


def run_analysis_for_file(
    project: Project,
    db: Database,
    recipe: Recipe,
    tool: ToolSpec,
    config: dict[str, Any],
    file_record: dict[str, Any],
    dry_run: bool = False,
    force: bool = False,
    threads: int = 4,
    backend: str | None = None,
    executor: Any = None,
    keep_partial: bool = False,
    runtime_parameters: dict[str, str] | None = None,
) -> dict[str, Any]:
    if executor is None:
        from operon.execution import get_executor

        recipe_slurm = recipe.raw.get("slurm")
        owned_executor = get_executor(
            project,
            backend,
            slurm_overrides=recipe_slurm if isinstance(recipe_slurm, dict) else None,
        )
        try:
            return run_analysis_for_file(
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
                executor=owned_executor,
                keep_partial=keep_partial,
                runtime_parameters=runtime_parameters,
            )
        finally:
            close = getattr(owned_executor, "close", None)
            if close is not None:
                close()
    plan = plan_analysis_for_file(
        project,
        db,
        recipe,
        tool,
        config,
        file_record,
        dry_run=dry_run,
        force=force,
        threads=threads,
        executor=executor,
        runtime_parameters=runtime_parameters,
    )
    if not isinstance(plan, _AnalysisExecution):
        return plan
    try:
        run_record = _execute_analysis_plan(project, db, recipe, tool, executor, plan)
        return _finalize_analysis_execution(
            project, db, recipe, tool, plan, run_record, keep_partial=keep_partial
        )
    except ShutdownRequested as exc:
        _interrupt_analysis_execution(project, db, plan, exc, keep_partial=keep_partial)
        cleanup_completed()
        raise
    except Exception as exc:
        _fail_analysis_execution(project, db, plan, exc, keep_partial=keep_partial)
        raise
