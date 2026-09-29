"""Plan one file's analysis job without executing it.

Input verification, database preparation, output naming, command
rendering, version probing, cache and adopt decisions, and the ``RUNNING``
row all stage into :class:`_PlanContext`; the result is an
:class:`_AnalysisExecution` the executor consumes.  Array participation
never enters the cache fingerprint."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.errors import ExternalToolError, ValidationError
from operon.utils import now_iso, sha256_path

from ._cache import (
    _cache_environment_decision,
    _find_verified_adoptee,
    _job_columns,
    find_adoptable_job,
    find_cached_job,
)
from ._config import Recipe, ToolSpec, launcher_prefix, tool_command
from ._inputs import (
    _resolve_database_path,
    database_identity,
    parameter_fingerprint,
    render_arguments,
)
from ._probe import (
    _detect_version_record,
    command_step_provenance,
    detect_tool_version_record,
    recipe_version_probe_command,
)


@dataclass
class _AnalysisExecution:
    """One file planned for real execution: the RUNNING row already exists.

    Produced by ``plan_analysis_for_file``; executed either immediately
    (sequential per-file path) or as one task of a job array.  Array
    participation never enters the cache fingerprint, so a cached result is
    reusable regardless of how it was produced.
    """

    file_record: dict[str, Any]
    job_id: int
    run_id: str
    argv_steps: list[list[str]]
    commands: list[list[str]] | None
    command_details: list[dict[str, Any]] | None
    command_display: str
    version: str
    threads: int
    output_path: Path
    output_rel: str
    work_dir: Path | None
    stage_inputs: tuple[Any, ...]
    env_decision: dict[str, Any] | None
    runtime_parameters: dict[str, str] | None
    stdout_path: Path
    stderr_path: Path
    started: str
    backups: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class _PlanContext:
    """Per-call state for ``plan_analysis_for_file`` and its staged helpers.

    Carries the 11 plan inputs verbatim plus the pipeline intermediates each
    stage fills in order.  Constructed fresh for every file; never shared.
    """

    project: Project
    db: Database
    recipe: Recipe
    tool: ToolSpec
    config: dict[str, Any]
    file_record: dict[str, Any]
    dry_run: bool
    force: bool
    threads: int
    executor: Any
    runtime_parameters: dict[str, str] | None

    remote_only: bool = False
    input_path: Path | None = None
    actual_sha: str = ""
    input_is_remote: bool = False
    database_path: Path | None = None
    database_mode: str = ""
    db_identity: str = ""
    output_dir: Path | None = None
    output_name: str = ""
    output_path: Path | None = None
    output_rel: str = ""
    rendered_args: list[str] = field(default_factory=list)
    work_dir: Path | None = None
    rendered_commands: list[list[str]] = field(default_factory=list)
    version: str = ""
    version_raw: str = ""
    step_provenance: list[dict[str, Any]] | None = None
    parameter_sha: str = ""
    cached: dict[str, Any] | None = None
    step_commands: list[list[str]] | None = None
    command: list[str] | None = None
    command_display: str = ""
    env_decision: dict[str, Any] | None = None
    recipe_snapshot_id: int | None = None
    job_id: int = 0
    started: str = ""


def _verify_plan_input(ctx: _PlanContext) -> None:
    """Verify the input artifact, resolving a remote-only input when allowed."""
    project = ctx.project
    db = ctx.db
    recipe = ctx.recipe
    file_record = ctx.file_record
    executor = ctx.executor
    ctx.remote_only = executor.name == "ssh" and bool(
        getattr(executor, "remote_root", "")
    )
    input_rel = file_record["relative_path"]
    input_path = project.root / input_rel
    ctx.input_path = input_path
    manifest_sha = str(file_record["sha256"]).lower()
    input_is_remote = False
    if not input_path.exists():
        storage_remote = str(getattr(executor, "storage_remote", "") or "")
        if executor.name != "ssh" or not storage_remote:
            raise ExternalToolError(
                f"{file_record['file_id']}: input missing at {input_path}; hydrate it locally or "
                "configure execution.ssh.storage_remote"
            )
        if not ctx.dry_run:
            from operon.remotes import _ensure_remote_only_schema, verify_remote_record

            verify_remote_record(
                project,
                storage_remote,
                file_record,
                db=db,
                client=getattr(executor, "client", None),
            )
            _ensure_remote_only_schema(project)
            db.set_file_status(
                file_record["file_id"],
                "REMOTE_ONLY",
                reason=f"local bytes absent; remote input verified for analysis on {storage_remote}",
                actor="operon analyze",
                evidence=f"remote://{storage_remote}/{input_rel}",
            )
        input_is_remote = True
        actual_sha = manifest_sha
    else:
        _require_artifact_kind(
            input_path, recipe.input_kind, f"{file_record['file_id']} input"
        )
        actual_sha = sha256_path(input_path).lower()
        if actual_sha != manifest_sha:
            raise ExternalToolError(
                f"{file_record['file_id']}: input checksum mismatch "
                f"(manifest={manifest_sha[:12]}..., actual={actual_sha[:12]}...); raw data was modified"
            )
    ctx.input_is_remote = input_is_remote
    ctx.actual_sha = actual_sha


def _prepare_plan_database(ctx: _PlanContext) -> None:
    """Resolve the recipe database, creating a mutable cache when configured."""
    project = ctx.project
    recipe = ctx.recipe
    executor = ctx.executor
    database_path = _resolve_database_path(project, recipe)
    ctx.database_path = database_path
    database_mode = str(recipe.raw.get("database_mode", "reference") or "reference")
    ctx.database_mode = database_mode
    if (
        database_path is not None
        and database_mode == "mutable_cache"
        and not ctx.dry_run
        and not ctx.remote_only
    ):
        database_path.mkdir(parents=True, exist_ok=True)
    if (
        recipe.database
        and database_path is not None
        and not database_path.exists()
        and not ctx.remote_only
    ):
        # A pure filesystem check: a dry run reports the same missing database the
        # real run would fail on instead of planning a job that cannot execute
        # (ODR-28).
        raise ExternalToolError(
            f"{recipe.name}: reference database not found: {database_path}; edit config/tools.yaml"
        )
    if (
        ctx.remote_only
        and recipe.database
        and database_mode == "reference"
        and not recipe.raw.get("database_checksum")
    ):
        raise ValidationError(
            f"{recipe.name}: remote reference databases require database_checksum so cache identity "
            "does not depend on a missing local path"
        )
    # A reference database can be verified over the backend without executing a
    # command (a stat), so a dry run still fails on an unprovisioned remote
    # reference instead of planning around it (ODR-28); a mutable cache is
    # created by the run itself and has nothing to verify yet.
    if (
        ctx.remote_only
        and database_path is not None
        and (not ctx.dry_run or database_mode == "reference")
    ):
        executor.prepare_database(
            database_path,
            mutable_cache=database_mode == "mutable_cache",
        )
    executor_identity = (
        executor.cache_identity()
        if hasattr(executor, "cache_identity")
        else executor.describe()
    )
    ctx.db_identity = database_identity(
        project, recipe, executor_identity if ctx.remote_only else ""
    )


def _plan_output_paths(ctx: _PlanContext) -> None:
    """Derive the output directory/name/path and the manifest-relative output."""
    project = ctx.project
    recipe = ctx.recipe
    file_record = ctx.file_record
    output_dir = project.analysis_root / recipe.output_subdir / file_record["entity_id"]
    ctx.output_dir = output_dir
    output_name = _render_output_name(
        recipe,
        file_record,
        ctx.input_path,
        runtime_parameters=ctx.runtime_parameters,
    )
    ctx.output_name = output_name
    output_path = output_dir / output_name
    ctx.output_path = output_path
    if (
        ctx.tool.name == "busco"
        and any(
            arg in {"--auto-lineage", "--auto-lineage-euk", "--auto-lineage-prok"}
            for arg in recipe.arguments
        )
        and "fasta" in output_path.as_posix()
    ):
        raise ValidationError(
            f"{recipe.name}: BUSCO auto-lineage output path contains 'fasta', which SEPP "
            "rewrites to 'jplace' in the full path; set output_name: '${file_id}.busco' "
            f"and ensure parent directories also avoid that substring (rendered path: {output_path})"
        )
    ctx.output_rel = output_path.relative_to(project.root).as_posix()


def _render_plan_commands(ctx: _PlanContext) -> None:
    """Render the argument vector and, for chains, the per-step commands."""
    recipe = ctx.recipe
    rendered_args = render_arguments(
        recipe,
        input_path=ctx.input_path,
        output_path=ctx.output_path,
        database_path=ctx.database_path,
        threads=ctx.threads,
        file_record=ctx.file_record,
        runtime_parameters=ctx.runtime_parameters,
    )
    ctx.rendered_args = rendered_args
    work_dir: Path | None = None
    rendered_commands: list[list[str]] = []
    if recipe.commands:
        # Deterministic per-file scratch directory for intermediate artifacts:
        # the rendered path enters the cache fingerprint, so it must not
        # contain a random component.
        work_dir = ctx.output_dir / f"{ctx.output_name}.work"
        rendered_commands = [
            render_arguments(
                recipe,
                input_path=ctx.input_path,
                output_path=ctx.output_path,
                database_path=ctx.database_path,
                threads=ctx.threads,
                file_record=ctx.file_record,
                runtime_parameters=ctx.runtime_parameters,
                work_dir=work_dir,
                arguments=block.arguments,
            )
            for block in recipe.commands
        ]
    ctx.work_dir = work_dir
    ctx.rendered_commands = rendered_commands


def _probe_plan_versions(ctx: _PlanContext) -> None:
    """Probe the tool/command-chain version and fingerprint the parameters."""
    recipe = ctx.recipe
    tool = ctx.tool
    config = ctx.config
    rendered_commands = ctx.rendered_commands
    owner_probe = recipe_version_probe_command(recipe, tool, config, rendered_commands)
    try:
        if ctx.dry_run and ctx.executor.name != "local":
            # Do not submit cluster jobs or open SSH connections for a dry run;
            # the cache verdict below may be approximate without the version.
            version = f"not probed (backend={ctx.executor.describe()})"
            version_raw = ""
        elif owner_probe is not None:
            # The first chain command is the recipe's logical owner: its
            # declared probe, not the tool-level one, defines tool_version.
            version, version_raw = _detect_version_record(
                *owner_probe,
                executor=ctx.executor,
            )
        else:
            version, version_raw = detect_tool_version_record(
                tool, config, executor=ctx.executor
            )
    except ExternalToolError as exc:
        if not ctx.dry_run:
            raise
        version = f"unavailable ({exc})"
        version_raw = str(exc)
    ctx.version = version
    ctx.version_raw = version_raw
    step_provenance = (
        command_step_provenance(
            recipe,
            tool,
            config,
            rendered_commands,
            version,
            version_raw,
            executor=ctx.executor,
            dry_run=ctx.dry_run,
        )
        if rendered_commands
        else None
    )
    ctx.step_provenance = step_provenance
    ctx.parameter_sha = parameter_fingerprint(
        recipe,
        ctx.rendered_args,
        ctx.threads,
        version,
        runtime_parameters=ctx.runtime_parameters,
        commands=rendered_commands or None,
        command_versions=(
            [[step["executable"], step["tool_version"]] for step in step_provenance]
            if step_provenance
            else None
        ),
    )


def _lookup_cached_job(ctx: _PlanContext) -> None:
    """Look up the cache fingerprint and judge a hit against the environment policy."""
    db = ctx.db
    recipe = ctx.recipe
    tool = ctx.tool
    config = ctx.config
    file_record = ctx.file_record
    rendered_commands = ctx.rendered_commands
    cached = find_cached_job(
        db,
        recipe.name,
        file_record["file_id"],
        ctx.parameter_sha,
        ctx.actual_sha,
        ctx.db_identity,
    )
    ctx.cached = cached
    if rendered_commands:
        step_commands = [
            [*launcher_prefix(tool, config), *block] for block in rendered_commands
        ]
        ctx.step_commands = step_commands
        ctx.command_display = " && ".join(" ".join(step) for step in step_commands)
    else:
        command = [*tool_command(tool, config), *ctx.rendered_args]
        ctx.command = command
        ctx.command_display = " ".join(command)

    env_decision: dict[str, Any] | None = None
    if cached is not None and not ctx.force and not ctx.dry_run:
        env_decision = _cache_environment_decision(
            db,
            recipe,
            ctx.executor,
            cached,
            ctx.step_commands[0] if ctx.step_commands else ctx.command,
            ctx.project.root,
        )
    ctx.env_decision = env_decision


def _plan_dry_run_result(ctx: _PlanContext) -> dict[str, Any]:
    """Dry-run verdict: cached/adoptable/planned, with no side effects."""
    db = ctx.db
    recipe = ctx.recipe
    file_record = ctx.file_record
    cached = ctx.cached
    adoptee = (
        None
        if ctx.runtime_parameters
        else find_adoptable_job(db, recipe.name, file_record["file_id"])
    )
    adoptable = (
        cached is None
        and not ctx.force
        and adoptee is not None
        and adoptee["input_sha256"] == ctx.actual_sha
    )
    if cached is not None and not ctx.force:
        status = "cached"
    elif adoptable:
        status = "adoptable"
    else:
        status = "planned"
    return {
        "file_id": file_record["file_id"],
        "entity_type": file_record["entity_type"],
        "entity_id": file_record["entity_id"],
        "analysis": recipe.name,
        "cached": cached is not None,
        "tool_version": ctx.version,
        "command": ctx.command_display,
        "adoptable": adoptable,
        "status": status,
        "output": ctx.output_rel,
        "dry_run": True,
    }


def _reuse_cached_job(ctx: _PlanContext) -> dict[str, Any] | None:
    """Return the cache-hit result dict, or supersede the stale row and return None."""
    db = ctx.db
    recipe = ctx.recipe
    file_record = ctx.file_record
    cached = ctx.cached
    output_path = ctx.output_path
    env_decision = ctx.env_decision
    if cached is not None and not ctx.force:
        strict_miss = env_decision is not None and not env_decision["reuse"]
        if (
            not strict_miss
            and output_path.exists()
            and sha256_path(output_path) == cached["output_sha256"]
        ):
            if env_decision is not None and env_decision["details"]:
                _record_cache_reuse_note(ctx)
                if env_decision["warning"]:
                    print(f"{file_record['file_id']}: {env_decision['warning']}")
            return {
                "file_id": file_record["file_id"],
                "entity_type": file_record["entity_type"],
                "entity_id": file_record["entity_id"],
                "analysis": recipe.name,
                "cached": True,
                "job_id": cached["job_id"],
                "tool_version": cached["tool_version"],
                "command": ctx.command_display,
                "output": ctx.output_rel,
                "status": "cached",
            }
        if strict_miss:
            print(
                f"{file_record['file_id']}: execution environment changed and "
                f"{recipe.name} sets environment_policy=strict; recomputing"
            )
        # Cached row exists but is not reusable (output deleted/modified, or a
        # strict environment mismatch): re-run and record a new job.
        with db.transaction() as conn:
            conn.execute(
                "UPDATE analysis_jobs SET status='superseded' WHERE job_id=?",
                (cached["job_id"],),
            )
        cached = None
        ctx.cached = None

    if cached is not None and ctx.force:
        # Force re-run keeps the historical row but removes it from the completed cache.
        with db.transaction() as conn:
            conn.execute(
                "UPDATE analysis_jobs SET status='superseded' WHERE job_id=?",
                (cached["job_id"],),
            )
        cached = None
        ctx.cached = None
    return None


def _record_cache_reuse_note(ctx: _PlanContext) -> None:
    """Persist the environment comparison behind a cache reuse as a run record."""
    from operon.workflow import log_run

    db = ctx.db
    recipe = ctx.recipe
    file_record = ctx.file_record
    cached = ctx.cached
    decision = ctx.env_decision
    details = dict(decision["details"])
    details["cache_reuse"] = True
    record: dict[str, Any] = {
        "entity_type": file_record["entity_type"],
        "entity_id": file_record["entity_id"],
        "step": f"analysis:{recipe.name}",
        "status": "completed",
        "command": ctx.command_display,
        "tool": ctx.tool.name,
        "tool_version": cached["tool_version"],
        "parameter_set": f"{recipe.name}:{cached['tool_version']}",
        "threads": ctx.threads,
        "executor": ctx.executor.describe(),
        "execution_details": json.dumps(details, ensure_ascii=False, sort_keys=True),
    }
    environment = decision.get("environment")
    if environment:
        with db.transaction():
            record["environment_id"] = db.record_environment(environment)
    log_run(db, ctx.project, record)


def _adopt_verified_output(ctx: _PlanContext) -> dict[str, Any] | None:
    """Adopt a verified existing output as a completed job, or return None."""
    db = ctx.db
    project = ctx.project
    recipe = ctx.recipe
    tool = ctx.tool
    file_record = ctx.file_record
    rendered_args = ctx.rendered_args
    if ctx.cached is None and not ctx.force and not ctx.runtime_parameters:
        # Resume tier 2: adopt a verified existing output instead of
        # recomputing when the exact cache fingerprint changed (version
        # upgrade, recipe rename) but the input content is unchanged.
        verified = _find_verified_adoptee(
            db, project, recipe.name, file_record["file_id"], ctx.actual_sha
        )
        if verified is not None:
            adoptee, _adoptee_output = verified
            finished = now_iso()
            columns = _job_columns()
            adopted_job = {
                "analysis_name": recipe.name,
                "entity_type": file_record["entity_type"],
                "entity_id": file_record["entity_id"],
                "file_id": file_record["file_id"],
                "tool": tool.name,
                "tool_version": ctx.version,
                "tool_version_raw": ctx.version_raw,
                "launcher": tool.run_method
                if ctx.executor.name == "local"
                else f"{tool.run_method} [{ctx.executor.describe()}]",
                "command": ctx.command_display,
                "parameter_set": json.dumps(
                    {
                        "arguments": rendered_args,
                        "threads": ctx.threads,
                        "runtime_parameters": ctx.runtime_parameters or {},
                    },
                    ensure_ascii=False,
                ),
                "parameter_sha256": ctx.parameter_sha,
                "input_sha256": ctx.actual_sha,
                "database_identity": ctx.db_identity,
                "status": "completed",
                "output_relative_path": adoptee["output_relative_path"],
                "output_sha256": adoptee["output_sha256"],
                "stdout_file": adoptee["stdout_file"],
                "stderr_file": adoptee["stderr_file"],
                "started_at": finished,
                "finished_at": finished,
                "workflow_run_id": adoptee["workflow_run_id"],
                "environment_id": adoptee["environment_id"],
                # Adopted results inherit the original job's recipe snapshot:
                # they were produced by that configuration, not today's.
                "recipe_snapshot_id": adoptee["recipe_snapshot_id"],
            }
            with db.transaction() as conn:
                cursor = conn.execute(
                    f"INSERT INTO analysis_jobs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",  # nosec B608 # fixed job columns; values are bound
                    [adopted_job.get(c) for c in columns],
                )
                adopted_job_id = int(cursor.lastrowid)
                conn.execute(
                    "INSERT INTO changes(object_type, object_id, field, old_value, new_value, "
                    "reason, evidence, actor, changed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "analysis_job",
                        str(adopted_job_id),
                        "status",
                        None,
                        "completed",
                        f"adopted verified output from job {adoptee['job_id']} "
                        "after cache fingerprint change",
                        f"output_sha256={adoptee['output_sha256']}",
                        "operon analyze",
                        finished,
                    ),
                )
            print(
                f"{file_record['file_id']}: adopting verified output from job "
                f"{adoptee['job_id']} for {recipe.name} (cache fingerprint changed)"
            )
            return {
                "file_id": file_record["file_id"],
                "entity_type": file_record["entity_type"],
                "entity_id": file_record["entity_id"],
                "analysis": recipe.name,
                "cached": True,
                "adopted": True,
                "job_id": adopted_job_id,
                "tool_version": ctx.version,
                "command": ctx.command_display,
                "output": adoptee["output_relative_path"],
                "status": "adopted",
            }
    return None


def _insert_running_job(ctx: _PlanContext) -> None:
    """Remove a stale output and insert the RUNNING analysis_jobs row."""
    db = ctx.db
    recipe = ctx.recipe
    tool = ctx.tool
    file_record = ctx.file_record
    rendered_args = ctx.rendered_args
    rendered_commands = ctx.rendered_commands
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    # Force/uncached runs must produce a fresh output; an old file from a
    # superseded job must not satisfy expected-output validation.
    _remove_output_artifact(ctx.project, ctx.output_path)
    started = now_iso()
    job = {
        "analysis_name": recipe.name,
        "entity_type": file_record["entity_type"],
        "entity_id": file_record["entity_id"],
        "file_id": file_record["file_id"],
        "tool": tool.name,
        "tool_version": ctx.version,
        "tool_version_raw": ctx.version_raw,
        "launcher": tool.run_method
        if ctx.executor.name == "local"
        else f"{tool.run_method} [{ctx.executor.describe()}]",
        "command": ctx.command_display,
        "parameter_set": json.dumps(
            {
                "arguments": rendered_args,
                "threads": ctx.threads,
                "runtime_parameters": ctx.runtime_parameters or {},
                **({"commands": rendered_commands} if rendered_commands else {}),
            },
            ensure_ascii=False,
        ),
        "parameter_sha256": ctx.parameter_sha,
        "input_sha256": ctx.actual_sha,
        "database_identity": ctx.db_identity,
        "status": "RUNNING",
        "started_at": started,
        "recipe_snapshot_id": ctx.recipe_snapshot_id,
    }
    columns = _job_columns()
    with db.transaction() as conn:
        cursor = conn.execute(
            f"INSERT INTO analysis_jobs ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",  # nosec B608 # fixed job columns; values are bound
            [job.get(c) for c in columns],
        )
        ctx.job_id = int(cursor.lastrowid)
    ctx.started = started


def _build_analysis_execution(ctx: _PlanContext) -> _AnalysisExecution:
    """Assemble the executable plan for the RUNNING row just inserted."""
    from operon.workflow import new_run_id

    run_id = new_run_id()
    logs = ctx.project.logs_root
    return _AnalysisExecution(
        file_record=ctx.file_record,
        job_id=ctx.job_id,
        run_id=run_id,
        argv_steps=[
            [str(a) for a in step] for step in (ctx.step_commands or [ctx.command])
        ],
        commands=ctx.step_commands,
        command_details=ctx.step_provenance,
        command_display=ctx.command_display,
        version=ctx.version,
        threads=ctx.threads,
        output_path=ctx.output_path,
        output_rel=ctx.output_rel,
        work_dir=ctx.work_dir,
        stage_inputs=(ctx.input_path,)
        if ctx.executor.name == "ssh" and not ctx.input_is_remote
        else (),
        env_decision=ctx.env_decision,
        runtime_parameters=ctx.runtime_parameters,
        stdout_path=logs / f"{run_id}.stdout.log",
        stderr_path=logs / f"{run_id}.stderr.log",
        started=ctx.started,
    )


def plan_analysis_for_file(
    project: Project,
    db: Database,
    recipe: Recipe,
    tool: ToolSpec,
    config: dict[str, Any],
    file_record: dict[str, Any],
    dry_run: bool = False,
    force: bool = False,
    threads: int = 4,
    executor: Any = None,
    runtime_parameters: dict[str, str] | None = None,
) -> dict[str, Any] | _AnalysisExecution:
    """Apply every per-file decision short of execution.

    Returns a plain result dict for dry-run/cache-hit/adopted files, or an
    ``_AnalysisExecution`` once the RUNNING analysis_jobs row exists.  The
    sequential path (``run_analysis_for_file``) executes the plan immediately;
    the array path collects plans across files and submits them in one batch.
    """
    ctx = _PlanContext(
        project=project,
        db=db,
        recipe=recipe,
        tool=tool,
        config=config,
        file_record=file_record,
        dry_run=dry_run,
        force=force,
        threads=threads,
        executor=executor,
        runtime_parameters=runtime_parameters,
    )
    _verify_plan_input(ctx)
    _prepare_plan_database(ctx)
    _plan_output_paths(ctx)
    _render_plan_commands(ctx)
    _probe_plan_versions(ctx)
    _lookup_cached_job(ctx)
    if dry_run:
        return _plan_dry_run_result(ctx)
    # Snapshot the recipe together with its tool spec; any edit to either
    # produces a new content-addressed snapshot, keeping jobs traceable to
    # the exact configuration that produced them.
    ctx.recipe_snapshot_id = db.record_recipe(
        recipe.name, recipe.version, {"recipe": recipe.raw, "tool": tool.raw}
    )
    reuse = _reuse_cached_job(ctx)
    if reuse is not None:
        return reuse
    adopted = _adopt_verified_output(ctx)
    if adopted is not None:
        return adopted
    _insert_running_job(ctx)
    return _build_analysis_execution(ctx)


def _require_artifact_kind(path: Path, kind: str, label: str) -> None:
    if kind == "file" and not path.is_file():
        raise ExternalToolError(f"{label} must be a regular file: {path}")
    if kind == "directory" and not path.is_dir():
        raise ExternalToolError(f"{label} must be a directory: {path}")


def _render_output_name(
    recipe: Recipe,
    file_record: dict[str, Any],
    input_path: Path,
    runtime_parameters: dict[str, str] | None = None,
) -> str:
    if not recipe.output_name_template:
        role = recipe.file_role or str(file_record["file_role"])
        return f"{file_record['file_id']}.{role}{recipe.output_suffix}"
    context = {
        "file_id": str(file_record["file_id"]),
        "file_role": str(file_record["file_role"]),
        "entity_type": str(file_record["entity_type"]),
        "entity_id": str(file_record["entity_id"]),
        "input_name": input_path.name,
        "input_stem": input_path.stem,
    }
    context.update(runtime_parameters or {})
    output_name = recipe.output_name_template
    for key, replacement in context.items():
        output_name = output_name.replace("${" + key + "}", replacement)
    unresolved = re.findall(r"\$\{[^}]+\}", output_name)
    if unresolved:
        raise ValidationError(
            f"{recipe.name}: unsupported placeholder(s) in output_name: {', '.join(unresolved)}"
        )
    if (
        not output_name
        or output_name in {".", ".."}
        or Path(output_name).name != output_name
    ):
        raise ValidationError(
            f"{recipe.name}: output_name must render to one safe path component, got {output_name!r}"
        )
    return output_name


def _remove_output_artifact(project: Project, output_path: Path) -> None:
    """Remove only the exact computed analysis artifact before a fresh run."""
    if not output_path.exists() and not output_path.is_symlink():
        return
    analysis_root = project.analysis_root.resolve()
    resolved = output_path.resolve(strict=False)
    if resolved == analysis_root or not resolved.is_relative_to(analysis_root):
        raise ExternalToolError(
            f"refusing to remove output outside analysis root: {output_path}"
        )
    if output_path.is_dir() and not output_path.is_symlink():
        shutil.rmtree(output_path)
    else:
        output_path.unlink(missing_ok=True)
