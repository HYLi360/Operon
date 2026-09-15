"""Execution backends for external commands: local, Slurm, and SSH (HPC/cloud).

`run_external_command` delegates process execution to one of three backends:

  * ``local`` — plain ``subprocess`` on this machine (the default);
  * ``slurm`` — submit to the local Slurm cluster with ``sbatch`` and poll
    ``squeue`` until the job leaves the queue (requires a filesystem shared
    with the compute nodes);
  * ``ssh``   — run on a remote host over SSH (Paramiko), which is also how
    generic cloud VMs are reached; with ``scheduler: slurm`` the command is
    submitted to a Slurm installation on the remote host instead.

All backends preserve the same provenance contract: stdout/stderr land in the
same local log files, the exit code is captured, and expected outputs are
checked locally afterwards (which re-verifies content hashes downstream).
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import shutil
import signal
import stat as stat_module
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    import resource
except ImportError:  # pragma: no cover - the stdlib 'resource' module exists on every supported (POSIX) platform
    resource = None  # type: ignore[assignment]

from operon.config import Project
from operon.environment import PROBE_SHELL_LINES, local_environment, parse_probe_output
from operon.environment_capture import capture_local, probe_shell
from operon.errors import ConflictError, ExternalToolError, RemoteError, ValidationError
from operon.utils import iter_directory_entries, sha256_file, sha256_path

VALID_BACKENDS = ("local", "slurm", "ssh")


@dataclass
class ExecResult:
    exit_code: int | None
    error: str | None = None
    scheduler_job_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    # Resource usage measured by the backend; every key is optional and a
    # missing/None value means the metric could not be collected.
    # Known keys: max_rss_mb, avg_rss_mb, cpu_seconds.
    resources: dict[str, Any] = field(default_factory=dict)


@dataclass
class SlurmConfig:
    partition: str = ""
    time_limit: str = "24:00:00"
    mem_gb: int = 0
    extra_sbatch: list[str] = field(default_factory=list)
    setup_commands: list[str] = field(default_factory=list)
    poll_interval: float = 15.0


def load_slurm_config(project: Project, overrides: dict[str, Any] | None = None) -> SlurmConfig:
    """Merge `execution.slurm` from project.yaml with per-recipe overrides."""
    execution = project.config.get("execution", {}) or {}
    raw: dict[str, Any] = dict(execution.get("slurm", {}) or {})
    for key, value in (overrides or {}).items():
        if value is not None and value != "":
            raw[key] = value
    config = SlurmConfig(
        partition=str(raw.get("partition", "") or ""),
        time_limit=str(raw.get("time", "") or ""),
        mem_gb=int(raw.get("mem_gb", 0) or 0),
        extra_sbatch=[str(x) for x in raw.get("extra_sbatch", []) or []],
        setup_commands=[str(x) for x in raw.get("setup_commands", []) or []],
        poll_interval=float(raw.get("poll_interval", 15) or 15),
    )
    if config.mem_gb < 0 or config.poll_interval <= 0:
        raise ValidationError("execution.slurm mem_gb must be >= 0 and poll_interval must be > 0")
    for field_name, values in (
            ("extra_sbatch", config.extra_sbatch), ("setup_commands", config.setup_commands),
    ):
        if any("\n" in value or "\r" in value for value in values):
            raise ValidationError(f"execution.slurm.{field_name} entries must be single lines")
    return config


def get_executor(project: Project, backend: str | None = None,
                 slurm_overrides: dict[str, Any] | None = None) -> Any:
    """Select the execution backend (CLI flag overrides `execution.backend`)."""
    execution = project.config.get("execution", {}) or {}
    name = str(backend or execution.get("backend") or "local").strip().lower()
    if name == "local":
        return LocalExecutor()
    if name == "slurm":
        return SlurmExecutor(project, load_slurm_config(project, slurm_overrides))
    if name == "ssh":
        return SSHExecutor(project, execution.get("ssh", {}) or {},
                           load_slurm_config(project, slurm_overrides))
    raise ValidationError(f"unknown execution backend {name!r}; valid: {', '.join(VALID_BACKENDS)}")


def rewrite_remote_path(value: str, local_root: Path, remote_root: str) -> str:
    """Map a local absolute path into the remote mirror of the project root.

    Only full path-prefix matches under the project root are rewritten; every
    other argument (flags, values, foreign paths) is passed through verbatim.
    With an empty `remote_root` the project must sit on a shared filesystem
    and paths are used unchanged.
    """
    if not remote_root:
        return value
    lexical_root = local_root.absolute()
    candidate_path = Path(value)
    if not candidate_path.is_absolute():
        return value

    try:
        candidate_path.relative_to(lexical_root)
        lexically_inside = True
    except ValueError:
        lexically_inside = False
    resolved_root = lexical_root.resolve()
    resolved_candidate = candidate_path.resolve(strict=False)
    try:
        relative = resolved_candidate.relative_to(resolved_root)
    except ValueError:
        if lexically_inside:
            raise ValidationError(
                f"local path escapes the project root and cannot be mapped over SSH: {value}"
            )
        return value

    # Resolve before mapping so a symlink below the project cannot smuggle an
    # argument outside it. This also treats macOS aliases such as /var and
    # /private/var as the same root.
    root = posixpath.normpath(remote_root)
    mapped = posixpath.normpath(posixpath.join(root, relative.as_posix()))
    if mapped == root or mapped.startswith(root.rstrip("/") + "/"):
        return mapped
    raise ValidationError(f"mapped SSH path escapes remote_root {root!r}: {value}")


def _slurm_script_preamble(*, job_name: str, stdout_path: str, stderr_path: str,
                           threads: int | None, slurm: SlurmConfig,
                           extra_directives: Iterable[str] = ()) -> list[str]:
    """Shared SBATCH header and setup block for single and array scripts."""
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --output={stdout_path}",
        f"#SBATCH --error={stderr_path}",
        f"#SBATCH --cpus-per-task={max(1, int(threads or 1))}",
        *extra_directives,
    ]
    if slurm.time_limit:
        lines.append(f"#SBATCH --time={slurm.time_limit}")
    if slurm.partition:
        lines.append(f"#SBATCH --partition={slurm.partition}")
    if slurm.mem_gb:
        lines.append(f"#SBATCH --mem={slurm.mem_gb}G")
    for extra in slurm.extra_sbatch:
        lines.append(f"#SBATCH {extra}")
    lines.append("")
    lines.extend(slurm.setup_commands)
    if slurm.setup_commands:
        lines.append("")
    return lines


def render_slurm_script(*, job_name: str, command_line: str, cwd: str,
                        stdout_path: str, stderr_path: str, exitcode_path: str,
                        threads: int | None, slurm: SlurmConfig,
                        probe_path: str | None = None) -> str:
    """Render a self-contained sbatch script (pure, unit-testable)."""
    lines = _slurm_script_preamble(
        job_name=job_name, stdout_path=stdout_path, stderr_path=stderr_path,
        threads=threads, slurm=slurm,
    )
    lines.append(f"cd {shlex.quote(cwd)}")
    lines.append("rc=$?")
    if probe_path:
        # Capture the compute-side environment before the payload runs;
        # probe failures must never affect the job itself.
        # Raw package metadata can contain private-channel credentials. Restrict
        # the transient probe file without changing the payload's umask.
        lines.append("(")
        lines.append("umask 077")
        lines.append("{")
        lines.append(probe_shell(shlex.split(command_line)))
        lines.append(f"}} > {shlex.quote(probe_path)} 2>/dev/null || true")
        lines.append(")")
    lines.append("if [ $rc -eq 0 ]; then")
    lines.append(f"  {command_line}")
    lines.append("  rc=$?")
    lines.append("fi")
    lines.append(f"echo $rc > {shlex.quote(exitcode_path)}")
    lines.append("exit $rc")
    return "\n".join(lines) + "\n"


def render_slurm_array_script(*, job_name: str, manifest_path: str, cwd: str,
                              stdout_path: str, stderr_path: str,
                              threads: int | None, slurm: SlurmConfig,
                              task_count: int, concurrency: int | None = None) -> str:
    """Render a self-contained sbatch array script (pure, unit-testable).

    The payload is dispatched from the manifest: array task N executes line N
    of the tab-separated manifest (``index, run_id, stdout, stderr, exitcode,
    probe, command`` — the command is the last field and is stored verbatim,
    so it may contain any character except a newline).  Each task probes the
    compute-side environment into its own probe file (the probe shell lives in
    ``<probe>.sh`` next to it) and writes its own exit-code file.  Slurm's
    own ``--output``/``--error`` capture only wrapper noise; the payload's
    streams are redirected to the per-task files from the manifest.
    """
    array_spec = f"1-{task_count}"
    if concurrency is not None:
        array_spec += f"%{int(concurrency)}"
    lines = _slurm_script_preamble(
        job_name=job_name, stdout_path=stdout_path, stderr_path=stderr_path,
        threads=threads, slurm=slurm,
        extra_directives=[f"#SBATCH --array={array_spec}"],
    )
    lines.append(f'line="$(sed -n "${{SLURM_ARRAY_TASK_ID}}p" {shlex.quote(manifest_path)})"')
    lines.append('if [ -z "$line" ]; then')
    lines.append(f'  echo "operon array task ${{SLURM_ARRAY_TASK_ID}}: '
                 f'no manifest line in {manifest_path}" >&2')
    lines.append("  exit 1")
    lines.append("fi")
    lines.append("IFS=$'\\t' read -r task_index task_run_id task_stdout task_stderr "
                 'task_exitcode task_probe task_command <<< "$line"')
    lines.append(f"cd {shlex.quote(cwd)}")
    lines.append("rc=$?")
    lines.append("(")
    lines.append("umask 077")
    lines.append("{")
    lines.append('sh "${task_probe}.sh"')
    lines.append('} > "$task_probe" 2>/dev/null || true')
    lines.append(")")
    lines.append("if [ $rc -eq 0 ]; then")
    lines.append('  eval "$task_command" > "$task_stdout" 2> "$task_stderr"')
    lines.append("  rc=$?")
    lines.append("fi")
    lines.append('echo "$rc" > "$task_exitcode"')
    lines.append("exit $rc")
    return "\n".join(lines) + "\n"


@dataclass
class _ArrayTask:
    """One validated array member with its derived per-task log paths."""

    index: int
    run_id: str
    command: str
    stdout_path: Path
    stderr_path: Path
    exitcode_path: Path
    probe_path: Path

    @property
    def probe_script_path(self) -> Path:
        return Path(str(self.probe_path) + ".sh")


_ARRAY_BATCH_ID_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _prepare_array_tasks(tasks: Iterable[dict[str, Any]], *, cwd: str | Path | None,
                         threads: int | None,
                         default_cwd: str) -> tuple[list[_ArrayTask], str, int | None]:
    """Validate task dicts and derive per-task paths; uniform cwd/threads win.

    Tasks may carry their own ``cwd``/``threads`` copies, but every task in
    one array must agree with the batch-level values (a job array has a
    single working directory guard and one ``--cpus-per-task``).
    """
    effective_cwd = str(cwd) if cwd else None
    effective_threads = threads
    prepared: list[_ArrayTask] = []
    seen_run_ids: set[str] = set()
    for index, task in enumerate(tasks, start=1):
        run_id = str(task.get("run_id") or "").strip()
        command = str(task.get("command") or "")
        if not run_id:
            raise ValidationError(f"array task {index} is missing run_id")
        if not command.strip():
            raise ValidationError(f"array task {run_id!r} is missing command")
        if "\n" in command or "\r" in command:
            raise ValidationError(f"array task {run_id!r} command must be a single line")
        if run_id in seen_run_ids:
            raise ValidationError(f"duplicate array task run_id {run_id!r}")
        seen_run_ids.add(run_id)
        task_cwd = task.get("cwd")
        if effective_cwd is None and task_cwd:
            effective_cwd = str(task_cwd)
        if task_cwd and effective_cwd and Path(str(task_cwd)) != Path(effective_cwd):
            raise ValidationError(f"array task {run_id!r} cwd differs from the batch cwd")
        task_threads = task.get("threads")
        if effective_threads is None and task_threads is not None:
            effective_threads = int(task_threads)
        if (task_threads is not None and effective_threads is not None
                and int(task_threads) != int(effective_threads)):
            raise ValidationError(f"array task {run_id!r} threads differ from the batch threads")
        try:
            stdout_path = Path(task["stdout_path"])
            stderr_path = Path(task["stderr_path"])
        except (KeyError, TypeError) as exc:
            missing = exc.args[0] if isinstance(exc, KeyError) else "stdout_path/stderr_path"
            raise ValidationError(f"array task {run_id!r} is missing {missing}") from exc
        for value in (run_id, str(stdout_path), str(stderr_path)):
            if "\t" in value or "\n" in value:
                raise ValidationError(
                    f"array task {run_id!r} paths and run_id must not contain tabs or newlines"
                )
        logs = stdout_path.parent
        prepared.append(_ArrayTask(
            index=index, run_id=run_id, command=command,
            stdout_path=stdout_path, stderr_path=stderr_path,
            exitcode_path=logs / f"{run_id}.exitcode",
            probe_path=logs / f"{run_id}.env",
        ))
    if not prepared:
        raise ValidationError("run_array requires at least one task")
    return prepared, effective_cwd or default_cwd, effective_threads


def _validate_array_batch(batch_id: str, array_concurrency: int | None) -> None:
    if not _ARRAY_BATCH_ID_RE.fullmatch(batch_id):
        raise ValidationError(f"invalid array batch id {batch_id!r}")
    if array_concurrency is not None and int(array_concurrency) < 1:
        raise ValidationError("array_concurrency must be a positive integer")


def render_array_manifest(tasks: Iterable[_ArrayTask]) -> str:
    """Render the tab-separated array dispatch manifest (pure, unit-testable).

    Line N is array task N; the command is the last field so it survives any
    character except a newline (validated in ``_prepare_array_tasks``).
    """
    lines = []
    for task in tasks:
        lines.append("\t".join((
            str(task.index), task.run_id, str(task.stdout_path), str(task.stderr_path),
            str(task.exitcode_path), str(task.probe_path), task.command,
        )))
    return "\n".join(lines) + "\n"


def _array_probe_shell(command: str) -> str:
    """Probe shell for one array task's command; unsplittable commands degrade."""
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = []
    return probe_shell(argv)


def _terminate_process_group(process: subprocess.Popen, grace: float = 3.0) -> None:
    """SIGTERM the child's process group, then SIGKILL if it survives.

    Children are started with ``start_new_session=True``, so the process
    group id equals the child pid and the whole tree (including
    grandchildren) is signaled.
    """
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        process.terminate()
    deadline = time.monotonic() + grace
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
    process.wait()


def _read_process_rss_mb(pid: int) -> float | None:
    """Read one process's RSS on Linux or another POSIX host, if available."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0  # kB -> MB
    except (OSError, ValueError, IndexError):
        pass

    # macOS has no procfs. Its BSD ps and the procps implementation used on
    # Linux both report this field in KiB.
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.split()[0]) / 1024.0
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return None


def _sample_process_rss(pid: int, samples_mb: list[float], stop: threading.Event) -> None:
    """Poll process RSS until the process exits or ``stop`` is requested.

    Sampling is best-effort diagnostics: unavailable procfs/ps data or a
    vanished process ends the sampler silently.
    """
    while not stop.is_set():
        rss_mb = _read_process_rss_mb(pid)
        if rss_mb is None:
            return
        samples_mb.append(rss_mb)
        stop.wait(0.5)


def _child_cpu_seconds() -> float | None:
    """Cumulative user+system CPU seconds of waited-for children, if available."""
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def _parse_remote_stats(text: str) -> dict[str, Any]:
    """Parse the remote sampler's "<max_kb> <sum_kb> <count>" stats line."""
    parts = text.split()
    if len(parts) != 3:
        return {}
    try:
        max_kb, sum_kb, count = float(parts[0]), float(parts[1]), int(parts[2])
    except ValueError:
        return {}
    if count <= 0:
        return {}
    return {
        "max_rss_mb": round(max_kb / 1024.0, 3),
        "avg_rss_mb": round(sum_kb / count / 1024.0, 3),
    }


class LocalExecutor:
    name = "local"

    def describe(self) -> str:
        return "local"

    def cache_identity(self) -> str:
        return "local"

    def probe_environment(self) -> dict[str, Any] | None:
        """Collect the local execution environment document."""
        try:
            return local_environment()
        except Exception:
            return None

    def run(self, argv: Iterable[Any], *, cwd: str | Path | None, stdout_path: Path,
            stderr_path: Path, timeout: float | None = None, threads: int | None = None,
            run_id: str | None = None, stage_inputs: Iterable[Any] = (),
            expected_outputs: Iterable[Any] = ()) -> ExecResult:
        command = [str(a) for a in argv]
        environment = capture_local(command, cwd)
        samples_mb: list[float] = []
        stop = threading.Event()
        cpu_before = _child_cpu_seconds()
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            # A dedicated process group lets shutdown (or timeout) terminate
            # the tool and any children it spawned, without orphaning them.
            process = subprocess.Popen(
                command, cwd=str(cwd) if cwd else None,
                stdout=out, stderr=err, start_new_session=True,
            )
            sampler = threading.Thread(
                target=_sample_process_rss, args=(process.pid, samples_mb, stop),
                daemon=True,
            )
            sampler.start()
            try:
                process.wait(timeout=timeout)
            except BaseException:
                # TimeoutExpired, ShutdownRequested/KeyboardInterrupt or any
                # other abort: kill the whole group, then re-raise unchanged.
                _terminate_process_group(process)
                raise
            finally:
                stop.set()
                sampler.join()
        resources: dict[str, Any] = {}
        if samples_mb:
            resources["max_rss_mb"] = round(max(samples_mb), 3)
            resources["avg_rss_mb"] = round(sum(samples_mb) / len(samples_mb), 3)
        cpu_after = _child_cpu_seconds()
        if cpu_before is not None and cpu_after is not None:
            resources["cpu_seconds"] = round(max(0.0, cpu_after - cpu_before), 3)
        return ExecResult(exit_code=process.returncode,
                          details={"backend": "local", "environment": environment},
                          resources=resources)


def _parse_sbatch_job_id(output: str) -> str:
    """Parse the final --parsable job-id line, tolerating preceding warnings."""
    for line in reversed(output.splitlines()):
        token = line.strip().split(";", 1)[0].strip()
        if re.fullmatch(r"[0-9]+(?:_[0-9]+)?", token):
            return token
    raise ExternalToolError(f"could not parse sbatch job id from {output!r}")


def _submit_slurm_job(sbatch: str, script_path: Path) -> str:
    proc = subprocess.run([sbatch, "--parsable", str(script_path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise ExternalToolError(f"sbatch submission failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return _parse_sbatch_job_id(proc.stdout)


def _squeue_job_gone(squeue: str, job_id: str) -> bool:
    proc = subprocess.run([squeue, "-h", "-j", job_id], capture_output=True, text=True)
    if proc.returncode != 0:
        # Completed jobs disappear with "Invalid job id specified".
        if "Invalid job id" in (proc.stderr or ""):
            return True
        raise ExternalToolError(f"squeue failed for job {job_id}: {proc.stderr.strip()}")
    return not proc.stdout.strip()


def _scancel_slurm_job(job_id: str) -> None:
    """Best-effort scancel; never raises (used on timeout and shutdown)."""
    scancel = shutil.which("scancel")
    if not scancel:
        return
    try:
        subprocess.run([scancel, job_id], capture_output=True)
    except OSError:
        pass


def _parse_sacct_memory_mb(value: str) -> float | None:
    """Parse a sacct memory field (e.g. 123K, 256M, 1.5G, 2T) into MB.

    Suffixed values follow Slurm's K/M/G/T conventions; a bare number is raw
    bytes (Slurm prints small exact values without a suffix).
    """
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGTkmgt]?)", value.strip())
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2).upper()
    if suffix == "K":
        return number / 1024.0
    if suffix == "M":
        return number
    if suffix == "G":
        return number * 1024.0
    if suffix == "T":
        return number * 1024.0 * 1024.0
    return number / (1024.0 * 1024.0)


_SLURM_TIME_RE = re.compile(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)")


def _parse_slurm_time_seconds(value: str) -> float | None:
    """Parse a Slurm ``[[dd-]hh:]mm:ss`` time field into seconds."""
    match = _SLURM_TIME_RE.fullmatch(value.strip())
    if not match:
        return None
    days, hours, minutes, seconds = match.groups()
    total = float(seconds) + int(minutes) * 60
    if hours is not None:
        total += int(hours) * 3600
    if days is not None:
        total += int(days) * 86400
    return total


def _parse_sacct_accounting(text: str) -> dict[str, Any]:
    """Parse ``sacct -n -p -o ExitCode,MaxRSS,AveRSS,Elapsed,TotalCPU`` output.

    The main job row comes first, followed by its step rows (.batch, .extern,
    ...).  The exit code is the first parseable "N:M" token (identical on
    every row): a nonzero signal component M with a zero exit component N is
    folded into ``128 + M`` and recorded as ``exit_signal``, so a signalled
    job is never read as a success.  Memory metrics take the maximum across
    all rows, and elapsed / total CPU come from the main job row only.
    """
    accounting: dict[str, Any] = {}
    max_rss: float | None = None
    ave_rss: float | None = None
    is_main_row = True
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = line.split("|")
        if fields and fields[-1] == "":
            fields.pop()  # -p terminates every line with a trailing '|'
        if len(fields) < 5:
            continue
        exit_token, max_rss_raw, ave_rss_raw, elapsed_raw, total_cpu_raw = fields[:5]
        if "exit_code" not in accounting and ":" in exit_token:
            try:
                code, term_signal = (int(part) for part in exit_token.split(":", 1))
            except ValueError:
                pass
            else:
                # sacct reports "exit:signal"; a signal kill with a zero exit
                # component (e.g. an OOM kill's 0:9) is a failure, so fold it
                # into a shell-style 128+signal exit code.
                accounting["exit_code"] = 128 + term_signal if term_signal and code == 0 else code
                if term_signal:
                    accounting["exit_signal"] = term_signal
        rss = _parse_sacct_memory_mb(max_rss_raw)
        if rss is not None:
            max_rss = rss if max_rss is None else max(max_rss, rss)
        rss = _parse_sacct_memory_mb(ave_rss_raw)
        if rss is not None:
            ave_rss = rss if ave_rss is None else max(ave_rss, rss)
        if is_main_row:
            is_main_row = False
            elapsed = _parse_slurm_time_seconds(elapsed_raw)
            if elapsed is not None:
                accounting["elapsed_seconds"] = elapsed
            total_cpu = _parse_slurm_time_seconds(total_cpu_raw)
            if total_cpu is not None:
                accounting["cpu_seconds"] = total_cpu
    if max_rss is not None:
        accounting["max_rss_mb"] = round(max_rss, 3)
    if ave_rss is not None:
        accounting["avg_rss_mb"] = round(ave_rss, 3)
    return accounting


def _apply_slurm_accounting(result: ExecResult, accounting: dict[str, Any]) -> None:
    """Copy parsed sacct metrics into the result's resources and details."""
    for key in ("max_rss_mb", "avg_rss_mb", "cpu_seconds"):
        if key in accounting:
            result.resources[key] = accounting[key]
    if "elapsed_seconds" in accounting:
        result.details["slurm_elapsed_seconds"] = accounting["elapsed_seconds"]
    if "exit_signal" in accounting:
        result.details["slurm_exit_signal"] = accounting["exit_signal"]


# Slurm accounting for a killed job can take several seconds to settle, so
# the exit-code-file retries must comfortably outlast that lag before the
# sacct fallback is consulted.
_SLURM_EXIT_CODE_RETRIES = 10
_SLURM_EXIT_CODE_RETRY_SECONDS = 2.0


def _read_slurm_accounting(exitcode_path: Path, job_id: str,
                           retries: int = _SLURM_EXIT_CODE_RETRIES) -> ExecResult:
    """Resolve a finished job's exit code and collect its sacct accounting.

    The exit-code file written by the batch script remains the primary exit
    code source; sacct supplies the fallback exit code and the resource
    metrics.  Accounting failures degrade to empty resources and never fail
    the run itself.
    """
    result: ExecResult | None = None
    for _ in range(retries):
        try:
            result = ExecResult(exit_code=int(exitcode_path.read_text().strip()))
            break
        except (OSError, ValueError):
            time.sleep(_SLURM_EXIT_CODE_RETRY_SECONDS)  # wait for shared-filesystem metadata to settle
    sacct = shutil.which("sacct")
    if sacct:
        try:
            proc = subprocess.run(
                [sacct, "-n", "-p", "-o", "ExitCode,MaxRSS,AveRSS,Elapsed,TotalCPU",
                 "-j", job_id],
                capture_output=True, text=True,
            )
            accounting = _parse_sacct_accounting(proc.stdout)
        except Exception:
            accounting = {}
        if result is None:
            exit_code = accounting.get("exit_code")
            if exit_code is not None:
                result = ExecResult(exit_code=exit_code)
        if result is not None:
            _apply_slurm_accounting(result, accounting)
    if result is None:
        result = ExecResult(exit_code=None,
                            error=f"slurm job {job_id} finished but its exit code is unavailable")
    return result


class SlurmExecutor:
    """Submit one job per command and block until it leaves the queue."""

    name = "slurm"

    def __init__(self, project: Project, slurm: SlurmConfig):
        self.project = project
        self.slurm = slurm

    def describe(self) -> str:
        return "slurm"

    def cache_identity(self) -> str:
        return "slurm:" + repr(self.slurm)

    def probe_environment(self) -> dict[str, Any] | None:
        """The compute-side environment is only probed inside the job itself."""
        return None

    def run(self, argv: Iterable[Any], *, cwd: str | Path | None, stdout_path: Path,
            stderr_path: Path, timeout: float | None = None, threads: int | None = None,
            run_id: str | None = None, stage_inputs: Iterable[Any] = (),
            expected_outputs: Iterable[Any] = ()) -> ExecResult:
        sbatch = shutil.which("sbatch")
        if not sbatch:
            raise ExternalToolError("slurm backend requires 'sbatch' in PATH")
        squeue = shutil.which("squeue")
        if not squeue:
            raise ExternalToolError("slurm backend requires 'squeue' in PATH")
        label = run_id or f"job_{int(time.time() * 1000)}"
        logs = Path(stdout_path).parent
        script_path = logs / f"{label}.sbatch"
        exitcode_path = logs / f"{label}.exitcode"
        probe_path = logs / f"{label}.env"
        details = {"backend": "slurm", "script": str(script_path)}
        script = render_slurm_script(
            job_name=f"operon_{label}",
            command_line=shlex.join(str(a) for a in argv),
            cwd=str(cwd) if cwd else str(self.project.root),
            stdout_path=str(stdout_path), stderr_path=str(stderr_path),
            exitcode_path=str(exitcode_path), threads=threads, slurm=self.slurm,
            probe_path=str(probe_path),
        )
        script_path.write_text(script, encoding="utf-8")
        exitcode_path.unlink(missing_ok=True)
        probe_path.unlink(missing_ok=True)
        job_id = _submit_slurm_job(sbatch, script_path)
        deadline = time.monotonic() + timeout if timeout else None
        try:
            while True:
                if _squeue_job_gone(squeue, job_id):
                    break
                if deadline is not None and time.monotonic() > deadline:
                    _scancel_slurm_job(job_id)
                    environment = _read_probe_environment(probe_path)
                    if environment:
                        details["environment"] = environment
                    return ExecResult(exit_code=None,
                                      error=f"timeout after {timeout}s waiting for slurm job {job_id}",
                                      scheduler_job_id=job_id, details=details)
                time.sleep(max(0.1, self.slurm.poll_interval))
        except KeyboardInterrupt:
            # Shutdown must not abandon a queued/running cluster job.
            _scancel_slurm_job(job_id)
            raise
        result = _read_slurm_accounting(exitcode_path, job_id)
        result.scheduler_job_id = job_id
        result.details = {**details, **result.details}
        environment = _read_probe_environment(probe_path)
        if environment:
            result.details["environment"] = environment
        return result

    def run_array(self, tasks: Iterable[dict[str, Any]], *, cwd: str | Path | None = None,
                  threads: int | None = None, timeout: float | None = None,
                  batch_id: str | None = None,
                  array_concurrency: int | None = None) -> list[ExecResult]:
        """Submit all tasks as one Slurm job array and block until it drains.

        Each task is a dict with ``run_id``, ``command`` (a rendered single
        shell line), ``stdout_path``/``stderr_path`` and optionally ``cwd`` /
        ``threads`` copies that must agree across the batch.  Returns one
        ExecResult per task, in task order, with ``scheduler_job_id`` set to
        ``<array_id>_<task_index>``.

        Interrupt contract: on KeyboardInterrupt the whole array is cancelled
        once and the exception propagates; the caller distinguishes finished
        tasks by which per-task exit-code files (``<run_id>.exitcode`` next
        to each task's stdout log) exist.  On timeout the array is cancelled
        and per-task results are still returned: tasks that left an exit-code
        file keep their exit code, the rest get ``exit_code=None`` with a
        timeout error.
        """
        sbatch = shutil.which("sbatch")
        if not sbatch:
            raise ExternalToolError("slurm backend requires 'sbatch' in PATH")
        squeue = shutil.which("squeue")
        if not squeue:
            raise ExternalToolError("slurm backend requires 'squeue' in PATH")
        batch_id = batch_id or f"array_{uuid.uuid4().hex[:12]}"
        _validate_array_batch(batch_id, array_concurrency)
        prepared, effective_cwd, effective_threads = _prepare_array_tasks(
            tasks, cwd=cwd, threads=threads, default_cwd=str(self.project.root),
        )
        logs = prepared[0].stdout_path.parent
        manifest_path = logs / f"{batch_id}.array-manifest.tsv"
        script_path = logs / f"{batch_id}.sbatch"
        details = {"backend": "slurm", "script": str(script_path),
                   "manifest": str(manifest_path)}
        for task in prepared:
            task.probe_script_path.write_text(
                _array_probe_shell(task.command) + "\n", encoding="utf-8")
            task.exitcode_path.unlink(missing_ok=True)
            task.probe_path.unlink(missing_ok=True)
        manifest_path.write_text(render_array_manifest(prepared), encoding="utf-8")
        script = render_slurm_array_script(
            job_name=f"operon_{batch_id}", manifest_path=str(manifest_path),
            cwd=effective_cwd,
            stdout_path=str(logs / f"{batch_id}.%A_%a.out"),
            stderr_path=str(logs / f"{batch_id}.%A_%a.err"),
            threads=effective_threads, slurm=self.slurm,
            task_count=len(prepared), concurrency=array_concurrency,
        )
        script_path.write_text(script, encoding="utf-8")
        job_id = _submit_slurm_job(sbatch, script_path)
        deadline = time.monotonic() + timeout if timeout else None
        try:
            while True:
                if _squeue_job_gone(squeue, job_id):
                    break
                if deadline is not None and time.monotonic() > deadline:
                    _scancel_slurm_job(job_id)
                    return self._array_timeout_results(prepared, job_id, timeout, details)
                time.sleep(max(0.1, self.slurm.poll_interval))
        except KeyboardInterrupt:
            # Shutdown must not abandon queued/running cluster tasks.
            _scancel_slurm_job(job_id)
            raise
        results = []
        for task in prepared:
            task_job_id = f"{job_id}_{task.index}"
            result = _read_slurm_accounting(task.exitcode_path, task_job_id)
            result.scheduler_job_id = task_job_id
            result.details = {"array_job_id": job_id, **details, **result.details}
            environment = _read_probe_environment(task.probe_path)
            if environment:
                result.details["environment"] = environment
            results.append(result)
        return results

    def _array_timeout_results(self, prepared: list[_ArrayTask], job_id: str,
                               timeout: float | None,
                               details: dict[str, Any]) -> list[ExecResult]:
        """Per-task results after a cancelled array: finished tasks keep theirs."""
        results = []
        for task in prepared:
            task_job_id = f"{job_id}_{task.index}"
            result: ExecResult | None = None
            try:
                result = ExecResult(exit_code=int(task.exitcode_path.read_text().strip()))
            except (OSError, ValueError):
                pass
            if result is None:
                result = ExecResult(
                    exit_code=None,
                    error=f"timeout after {timeout}s waiting for slurm job array {job_id}",
                )
            result.scheduler_job_id = task_job_id
            result.details = {"array_job_id": job_id, **details, **result.details}
            environment = _read_probe_environment(task.probe_path)
            if environment:
                result.details["environment"] = environment
            results.append(result)
        return results


def _read_probe_environment(probe_path: Path) -> dict[str, Any] | None:
    """Read and parse a job-side environment probe file; best-effort."""
    try:
        environment = parse_probe_output(probe_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        try:
            probe_path.unlink(missing_ok=True)
        except OSError:
            pass
    return environment or None


def _read_remote_probe_environment(sftp: Any, probe_path: str) -> dict[str, Any] | None:
    """Read and remove a compute-side probe through SFTP; best-effort."""
    try:
        with sftp.open(probe_path, "rb") as handle:
            payload = handle.read()
        if isinstance(payload, bytes):
            text = payload.decode("utf-8", "replace")
        else:
            text = str(payload)
        environment = parse_probe_output(text)
    except (OSError, ValueError, TypeError):
        return None
    finally:
        try:
            sftp.remove(probe_path)
        except OSError:
            pass
    return environment or None


class SSHExecutor:
    """Run commands on a remote host over SSH, optionally through remote Slurm."""

    name = "ssh"

    def __init__(self, project: Project, ssh_config: dict[str, Any], slurm: SlurmConfig,
                 client_factory: Any = None):
        cfg = dict(ssh_config or {})
        self.project = project
        self.host = str(cfg.get("host", "") or "").strip()
        self.user = str(cfg.get("user", "") or "").strip()
        self.port = int(cfg.get("port", 22) or 22)
        self.key_file = str(cfg.get("key_file", "") or "").strip()
        self.remote_root = str(cfg.get("remote_root", "") or "").strip()
        if self.remote_root:
            if not self.remote_root.startswith("/"):
                raise ValidationError("execution.ssh.remote_root must be an absolute POSIX path")
            self.remote_root = posixpath.normpath(self.remote_root)
        self.scheduler = str(cfg.get("scheduler", "none") or "none").strip().lower()
        if self.scheduler not in {"none", "slurm"}:
            raise ValidationError(
                f"execution.ssh.scheduler must be 'none' or 'slurm', got {self.scheduler!r}"
            )
        if self.scheduler != "slurm":
            # Only the remote-Slurm path supports job arrays; keep duck-typing
            # capability detection (`getattr(executor, "run_array", None)`)
            # false for direct SSH execution.
            self.run_array = None
        self.connect_timeout = float(cfg.get("connect_timeout", 30) or 30)
        self.storage_remote = str(cfg.get("storage_remote", "") or "").strip()
        self.known_hosts = str(cfg.get("known_hosts", "") or "").strip()
        self.host_key_sha256 = str(cfg.get("host_key_sha256", "") or "").strip()
        self.insecure_accept_unknown_host = bool(cfg.get("insecure_accept_unknown_host", False))
        if self.storage_remote:
            from operon.remotes import get_remote
            storage = get_remote(project, self.storage_remote)
            if self.host and self.host != storage.host:
                raise ValidationError(
                    f"execution.ssh.host {self.host!r} differs from storage remote "
                    f"{self.storage_remote!r} host {storage.host!r}"
                )
            if self.user and storage.user and self.user != storage.user:
                raise ValidationError("execution.ssh.user differs from storage remote user")
            if self.port != storage.port and cfg.get("port") not in (None, "", 22):
                raise ValidationError("execution.ssh.port differs from storage remote port")
            storage_root = posixpath.normpath(storage.root)
            if self.remote_root and self.remote_root != storage_root:
                raise ValidationError(
                    f"execution.ssh.remote_root {self.remote_root!r} differs from storage remote "
                    f"{self.storage_remote!r} root {storage_root!r}"
                )
            self.host = storage.host
            self.user = self.user or storage.user
            self.port = storage.port
            self.key_file = self.key_file or storage.key_file
            self.remote_root = self.remote_root or storage_root
            self.known_hosts = self.known_hosts or storage.known_hosts
            self.host_key_sha256 = self.host_key_sha256 or storage.host_key_sha256
            self.insecure_accept_unknown_host = (
                    self.insecure_accept_unknown_host or storage.insecure_accept_unknown_host
            )
        if not self.host:
            raise ValidationError(
                "execution backend 'ssh' requires execution.ssh.host or storage_remote in project.yaml"
            )
        self.slurm = slurm
        self._client_factory = client_factory
        self._client: Any = None
        self._prepared_databases: set[tuple[str, bool]] = set()

    def describe(self) -> str:
        return f"ssh:{self.user + '@' if self.user else ''}{self.host}"

    def cache_identity(self) -> str:
        return (
            f"{self.describe()}:{self.port}:scheduler={self.scheduler}:"
            f"root={self.remote_root}:storage={self.storage_remote}:"
            f"hostkey={self.host_key_sha256}:slurm={self.slurm!r}"
        )

    def probe_environment(self) -> dict[str, Any] | None:
        """Probe the remote host over SSH; any failure degrades to None."""
        if self.scheduler == "slurm":
            # The login node is not the execution environment. Remote Slurm
            # captures the compute-side document inside the submitted job.
            return None
        try:
            client = self._connect()
            _, stdout, _ = client.exec_command(
                " ; ".join(PROBE_SHELL_LINES), timeout=self.connect_timeout,
            )
            environment = parse_probe_output(stdout.read().decode("utf-8", "replace"))
            return environment or None
        except Exception:
            return None

    def _connect(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory(self)
            else:
                from operon.remotes import connect_ssh
                self._client = connect_ssh(
                    self.host, user=self.user, port=self.port,
                    key_file=self.key_file, connect_timeout=self.connect_timeout,
                    known_hosts=self.known_hosts, host_key_sha256=self.host_key_sha256,
                    insecure_accept_unknown_host=self.insecure_accept_unknown_host,
                )
        return self._client

    @property
    def client(self) -> Any:
        """Return the executor's reusable, lazily-created SSH connection."""
        return self._connect()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _rewrite(self, value: Any) -> str:
        return rewrite_remote_path(str(value), self.project.root, self.remote_root)

    def prepare_database(self, path: str | Path, *, mutable_cache: bool) -> str:
        """Require a pre-provisioned remote reference or create a mutable cache dir."""
        remote = self._rewrite(Path(path))
        cache_key = (remote, bool(mutable_cache))
        if cache_key in self._prepared_databases:
            return remote
        client = self._connect()
        sftp = client.open_sftp()
        try:
            if mutable_cache:
                from operon.remotes import sftp_makedirs
                sftp_makedirs(sftp, remote)
            else:
                try:
                    sftp.stat(remote)
                except IOError as exc:
                    raise RemoteError(
                        f"remote reference database is not provisioned at {remote}"
                    ) from exc
        finally:
            sftp.close()
        self._prepared_databases.add(cache_key)
        return remote

    def run(self, argv: Iterable[Any], *, cwd: str | Path | None, stdout_path: Path,
            stderr_path: Path, timeout: float | None = None, threads: int | None = None,
            run_id: str | None = None, stage_inputs: Iterable[Any] = (),
            expected_outputs: Iterable[Any] = ()) -> ExecResult:
        client = self._connect()
        sftp = client.open_sftp()
        try:
            self._stage_inputs(client, sftp, stage_inputs)
            backups = self._reset_outputs(sftp, expected_outputs)
            try:
                if self.scheduler == "slurm":
                    result = self._run_via_slurm(
                        client, sftp, [str(a) for a in argv], cwd=cwd,
                        stdout_path=Path(stdout_path), stderr_path=Path(stderr_path),
                        timeout=timeout, threads=threads, run_id=run_id,
                    )
                else:
                    command = [str(a) for a in argv]
                    remote_probe = f"/tmp/operon-env-{uuid.uuid4().hex}"
                    try:
                        result = self._run_direct(
                            client, command, cwd=cwd,
                            stdout_path=Path(stdout_path), stderr_path=Path(stderr_path),
                            timeout=timeout, run_id=run_id, probe_path=remote_probe,
                        )
                    finally:
                        environment = _read_remote_probe_environment(sftp, remote_probe)
                    result.details["environment"] = environment or {
                        "capture_schema": 1, "capture_status": "failed",
                    }
                if result.exit_code == 0:
                    self._pull_outputs(client, sftp, expected_outputs)
            except BaseException:
                self._restore_output_backups(sftp, backups)
                raise
            if result.exit_code == 0:
                self._drop_output_backups(sftp, backups)
            else:
                self._restore_output_backups(sftp, backups)
            return result
        finally:
            sftp.close()

    # -- staging -----------------------------------------------------------

    def _stage_inputs(self, client: Any, sftp: Any, stage_inputs: Iterable[Any]) -> None:
        from operon.remotes import sftp_makedirs
        for item in stage_inputs:
            local = Path(item)
            if not local.exists():
                raise RemoteError(f"cannot stage missing local input: {local}")
            if self.remote_root and not local.resolve().is_relative_to(
                    self.project.root.resolve()):
                raise ValidationError(
                    f"SSH staged input must stay under the project root: {local}"
                )
            remote = self._rewrite(local)
            if local.is_dir():
                self._stage_directory(client, sftp, local, remote)
                continue
            sftp_makedirs(sftp, posixpath.dirname(remote))
            if self._remote_file_matches(client, sftp, remote, local):
                continue
            try:
                sftp.stat(remote)
            except IOError:
                pass
            else:
                raise ConflictError(
                    f"remote input already exists with different bytes; refusing to overwrite: {remote}"
                )
            from operon.remotes import _remove_remote_tree, remote_sha256
            tmp = f"{remote}.operon-tmp-{uuid.uuid4().hex}"
            try:
                sftp.put(str(local), tmp)
                sftp.rename(tmp, remote)
                digest = remote_sha256(client, remote, sftp=sftp)
                if digest != sha256_file(local):
                    raise RemoteError(f"staged input verification failed for {remote}")
            except BaseException:
                _remove_remote_tree(sftp, tmp)
                raise

    def _reset_outputs(self, sftp: Any, expected_outputs: Iterable[Any]) -> list[tuple[str, str]]:
        """Back up existing remote outputs instead of deleting them.

        Returns ``(remote, backup)`` pairs; the caller drops the backups once
        the new outputs are pulled and verified, or restores them after a
        failure or interrupt.
        """
        from operon.remotes import _sftp_not_found, sftp_makedirs
        backups: list[tuple[str, str]] = []
        try:
            for item in expected_outputs:
                local = Path(item).resolve(strict=False)
                if self.remote_root and not local.is_relative_to(self.project.root.resolve()):
                    raise ValidationError(
                        f"SSH expected output must stay under the project root: {local}"
                    )
                remote = self._rewrite(local)
                if self.remote_root:
                    root = posixpath.normpath(self.remote_root)
                    normalized = posixpath.normpath(remote)
                    if not normalized.startswith(root.rstrip("/") + "/"):
                        raise ValidationError(f"SSH output escapes remote_root: {remote}")
                    backup = f"{normalized}.operon-prev-{uuid.uuid4().hex}"
                    try:
                        sftp.rename(normalized, backup)
                    except IOError as exc:
                        if not _sftp_not_found(exc):
                            raise
                    else:
                        backups.append((normalized, backup))
                sftp_makedirs(sftp, posixpath.dirname(remote))
        except Exception:
            self._restore_output_backups(sftp, backups)
            raise
        return backups

    def _drop_output_backups(self, sftp: Any, backups: list[tuple[str, str]]) -> None:
        """Remove previous-output backups; the new outputs are already verified."""
        from operon.remotes import _remove_remote_tree
        for _, backup in backups:
            try:
                _remove_remote_tree(sftp, backup)
            except Exception:
                pass

    def _restore_output_backups(self, sftp: Any, backups: list[tuple[str, str]]) -> None:
        """Best-effort restore of previous remote outputs after a failed run."""
        from operon.remotes import _remove_remote_tree
        for remote, backup in reversed(backups):
            try:
                _remove_remote_tree(sftp, remote)
                sftp.rename(backup, remote)
            except Exception:
                pass

    def _stage_directory(self, client: Any, sftp: Any, local: Path, remote: str) -> None:
        """Stage an immutable directory artifact with a strict tree identity."""
        from operon.remotes import (
            _publish_remote, _remote_directory_identity, _remove_remote_tree, sftp_makedirs,
        )
        digest = sha256_path(local).lower()
        try:
            stat = sftp.stat(remote)
        except IOError:
            stat = None
        if stat is not None:
            actual, _ = _remote_directory_identity(sftp, remote)
            if actual == digest:
                return
            raise ConflictError(
                f"remote directory input already exists with different content: {remote}"
            )
        sftp_makedirs(sftp, posixpath.dirname(remote))
        tmp = f"{remote}.operon-tmp-{uuid.uuid4().hex}"
        sftp.mkdir(tmp)
        try:
            for path in iter_directory_entries(local):
                rel = posixpath.join(tmp, path.relative_to(local).as_posix())
                if path.is_symlink():
                    sftp_makedirs(sftp, posixpath.dirname(rel))
                    sftp.symlink(os.readlink(path), rel)
                elif path.is_dir():
                    sftp.mkdir(rel)
                elif path.is_file():
                    sftp_makedirs(sftp, posixpath.dirname(rel))
                    sftp.put(str(path), rel)
                else:
                    raise RemoteError(f"unsupported directory input entry: {path}")
            actual, _ = _remote_directory_identity(sftp, tmp)
            if actual != digest:
                raise RemoteError(f"staged directory verification failed for {remote}")
            _publish_remote(sftp, tmp, remote, overwrite=False)
        except BaseException:
            _remove_remote_tree(sftp, tmp)
            raise

    def _remote_file_matches(self, client: Any, sftp: Any, remote: str, local: Path) -> bool:
        from operon.remotes import remote_sha256
        try:
            stat = sftp.stat(remote)
        except IOError:
            return False
        if int(stat.st_size) != local.stat().st_size:
            return False
        digest = remote_sha256(client, remote, sftp=sftp)
        return digest == sha256_file(local)

    # -- direct execution --------------------------------------------------

    def _run_direct(self, client: Any, argv: list[str], *, cwd: str | Path | None,
                    stdout_path: Path, stderr_path: Path,
                    timeout: float | None, run_id: str | None,
                    probe_path: str | None = None) -> ExecResult:
        command = shlex.join(self._rewrite(a) for a in argv)
        if probe_path:
            probe = probe_shell([self._rewrite(a) for a in argv])
            command = f"(umask 077; {{ {probe}; }} > {shlex.quote(probe_path)} 2>/dev/null) || true; {command}"
        remote_cwd = self._rewrite(cwd) if cwd else self.remote_root
        if remote_cwd:
            command = f"cd {shlex.quote(remote_cwd)} && {{ {command}; }}"
        label = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id or uuid.uuid4().hex)
        pidfile = f"/tmp/operon-{label}-{uuid.uuid4().hex}.pid"
        statsfile = f"{pidfile}.stats"
        # Background RSS sampler: once per second record the combined RSS
        # (kB) of the payload shell and its children as a "<max> <sum>
        # <count>" stats line.  Hosts without ps/awk simply leave no usable
        # stats file; sampling must never affect the payload itself.
        sampler = (
            "( max=0; sum=0; count=0; "
            "while kill -0 $$ 2>/dev/null; do "
            "rss=$(ps -o rss= -g $$ 2>/dev/null | "
            "awk 'NF{s+=$1; n++} END{if (n) print s}'); "
            "if [ -n \"$rss\" ]; then "
            "[ \"$rss\" -gt \"$max\" ] 2>/dev/null && max=$rss; "
            "sum=$((sum + rss)); count=$((count + 1)); "
            f"printf '%s %s %s\\n' \"$max\" \"$sum\" \"$count\" > {shlex.quote(statsfile)}; "
            "fi; "
            "sleep 1; "
            "done ) & sampler_pid=$!; "
        )
        payload = (
            f"umask 077; echo $$ > {shlex.quote(pidfile)}; "
            f"{sampler}"
            "trap 'kill \"$sampler_pid\" 2>/dev/null || true; "
            "wait \"$sampler_pid\" 2>/dev/null || true; "
            f"rm -f {shlex.quote(pidfile)}' EXIT; {command}"
        )
        # --wait makes setsid propagate the payload's exit status even when
        # setsid itself is already a process group leader and has to fork
        # (util-linux setsid exits 0 immediately in that case otherwise).
        wrapped_command = f"setsid --wait sh -c {shlex.quote(payload)}"
        _, stdout, _ = client.exec_command(wrapped_command)
        channel = stdout.channel
        deadline = time.monotonic() + timeout if timeout else None
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            try:
                while True:
                    while channel.recv_ready():
                        out.write(channel.recv(65536))
                    while channel.recv_stderr_ready():
                        err.write(channel.recv_stderr(65536))
                    if channel.exit_status_ready():
                        break
                    if deadline is not None and time.monotonic() > deadline:
                        signaled, termination_error = self._terminate_remote_process(client, pidfile)
                        channel.close()
                        resources = self._read_remote_stats(client, statsfile) if signaled else {}
                        if signaled:
                            error = (
                                f"timeout after {timeout}s; termination signals were sent to the "
                                "remote process group"
                            )
                        else:
                            error = (
                                f"timeout after {timeout}s; remote process may still be running: "
                                f"{termination_error}"
                            )
                        return ExecResult(
                            exit_code=None, error=error,
                            details={
                                "backend": "ssh", "scheduler": "none", "host": self.host,
                                "pidfile": pidfile, "termination_signaled": signaled,
                            },
                            resources=resources,
                        )
                    time.sleep(0.05)
            except KeyboardInterrupt:
                # The remote payload runs under setsid and would survive
                # connection teardown, so terminate its process group first.
                try:
                    signaled, _ = self._terminate_remote_process(client, pidfile)
                    if signaled:
                        self._read_remote_stats(client, statsfile)
                except Exception:
                    pass
                channel.close()
                raise
            while channel.recv_ready():
                out.write(channel.recv(65536))
            while channel.recv_stderr_ready():
                err.write(channel.recv_stderr(65536))
        return ExecResult(
            exit_code=channel.recv_exit_status(),
            details={"backend": "ssh", "scheduler": "none", "host": self.host},
            resources=self._read_remote_stats(client, statsfile),
        )

    def _read_remote_stats(self, client: Any, statsfile: str) -> dict[str, Any]:
        """Read back and remove the remote sampler stats file; best-effort."""
        try:
            _, out = self._remote_exec(
                client,
                f"cat {shlex.quote(statsfile)} 2>/dev/null; rm -f {shlex.quote(statsfile)}",
            )
        except Exception:
            return {}
        return _parse_remote_stats(out)

    def _terminate_remote_process(self, client: Any, pidfile: str) -> tuple[bool, str]:
        command = (
            f"if test -s {shlex.quote(pidfile)}; then "
            f"pid=$(cat {shlex.quote(pidfile)}); "
            "kill -TERM -- -\"$pid\" 2>/dev/null || kill -TERM \"$pid\" 2>/dev/null || true; "
            "sleep 1; "
            "if kill -0 \"$pid\" 2>/dev/null; then "
            "kill -KILL -- -\"$pid\" 2>/dev/null || kill -KILL \"$pid\" 2>/dev/null || true; "
            "fi; "
            f"rm -f {shlex.quote(pidfile)}; exit 0; "
            "else exit 2; fi"
        )
        try:
            rc, output = self._remote_exec(client, command, timeout=max(5.0, self.connect_timeout))
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if rc == 0:
            return True, ""
        return False, output.strip() or f"termination command exited {rc}"

    # -- remote slurm ------------------------------------------------------

    def _sftp_write_file(self, sftp: Any, remote: str, content: str) -> None:
        """Write a remote text file atomically (temporary name + rename)."""
        from operon.remotes import _remove_remote_tree, sftp_makedirs
        sftp_makedirs(sftp, posixpath.dirname(remote))
        _remove_remote_tree(sftp, remote)
        tmp = f"{remote}.operon-tmp-{uuid.uuid4().hex}"
        try:
            with sftp.open(tmp, "w") as handle:
                handle.write(content)
            sftp.rename(tmp, remote)
        except BaseException:
            _remove_remote_tree(sftp, tmp)
            raise

    def _run_via_slurm(self, client: Any, sftp: Any, argv: list[str], *,
                       cwd: str | Path | None, stdout_path: Path, stderr_path: Path,
                       timeout: float | None, threads: int | None,
                       run_id: str | None) -> ExecResult:
        from operon.remotes import _remove_remote_tree, sftp_makedirs
        label = run_id or f"job_{int(time.time() * 1000)}"
        remote_stdout = self._rewrite(stdout_path)
        remote_stderr = self._rewrite(stderr_path)
        remote_dir = posixpath.dirname(remote_stdout)
        remote_script = posixpath.join(remote_dir, f"{label}.sbatch")
        remote_exitcode = posixpath.join(remote_dir, f"{label}.exitcode")
        remote_probe = posixpath.join(remote_dir, f"{label}.env")
        script = render_slurm_script(
            job_name=f"operon_{label}",
            command_line=shlex.join(self._rewrite(a) for a in argv),
            cwd=self._rewrite(cwd) if cwd else (self.remote_root or "."),
            stdout_path=remote_stdout, stderr_path=remote_stderr,
            exitcode_path=remote_exitcode, threads=threads, slurm=self.slurm,
            probe_path=remote_probe,
        )
        sftp_makedirs(sftp, remote_dir)
        _remove_remote_tree(sftp, remote_exitcode)
        _remove_remote_tree(sftp, remote_probe)
        self._sftp_write_file(sftp, remote_script, script)
        rc, out = self._remote_exec(client, f"sbatch --parsable {shlex.quote(remote_script)}")
        if rc != 0:
            raise RemoteError(f"remote sbatch submission failed: {out.strip()}")
        try:
            job_id = _parse_sbatch_job_id(out)
        except ExternalToolError as exc:
            raise RemoteError(str(exc)) from exc
        details = {"backend": "ssh", "scheduler": "slurm", "host": self.host,
                   "script": remote_script}
        deadline = time.monotonic() + timeout if timeout else None
        try:
            while True:
                rc, out = self._remote_exec(client, f"squeue -h -j {shlex.quote(job_id)}")
                if rc != 0:
                    if "Invalid job id" in out:
                        break
                    raise RemoteError(f"remote squeue failed for job {job_id}: {out.strip()}")
                if not out.strip():
                    break
                if deadline is not None and time.monotonic() > deadline:
                    cancelled, cancellation_error = self._cancel_remote_slurm_job(client, job_id)
                    details["cancellation_requested"] = cancelled
                    if cancellation_error:
                        details["cancellation_error"] = cancellation_error
                    self._sftp_get_if_exists(sftp, remote_stdout, stdout_path)
                    self._sftp_get_if_exists(sftp, remote_stderr, stderr_path)
                    environment = _read_remote_probe_environment(sftp, remote_probe)
                    if environment:
                        details["environment"] = environment
                    error = f"timeout after {timeout}s waiting for remote slurm job {job_id}"
                    if not cancelled:
                        error += (
                            "; cancellation request failed and the job may still be running: "
                            f"{cancellation_error}"
                        )
                    return ExecResult(exit_code=None,
                                      error=error, scheduler_job_id=job_id, details=details)
                time.sleep(max(0.1, self.slurm.poll_interval))
        except KeyboardInterrupt:
            # Shutdown must not abandon the queued/running remote cluster job.
            self._cancel_remote_slurm_job(client, job_id)
            raise
        self._sftp_get_if_exists(sftp, remote_stdout, stdout_path)
        self._sftp_get_if_exists(sftp, remote_stderr, stderr_path)
        environment = _read_remote_probe_environment(sftp, remote_probe)
        if environment:
            details["environment"] = environment
        result = self._read_remote_slurm_accounting(
            client, remote_exitcode=remote_exitcode, job_id=job_id)
        result.details = {**details, **result.details}
        return result

    def _read_remote_slurm_accounting(self, client: Any, *, remote_exitcode: str,
                                      job_id: str) -> ExecResult:
        """Resolve a finished remote job's exit code and its sacct accounting.

        The job-side exit-code file remains the primary exit code source;
        sacct supplies the fallback exit code and the resource metrics, and
        accounting failures degrade to empty resources and never fail the run.
        """
        exit_code: int | None = None
        for _ in range(_SLURM_EXIT_CODE_RETRIES):
            rc, out = self._remote_exec(client, f"cat {shlex.quote(remote_exitcode)}")
            if rc == 0:
                try:
                    exit_code = int(out.strip())
                    break
                except ValueError:
                    pass
            time.sleep(_SLURM_EXIT_CODE_RETRY_SECONDS)
        # sacct supplies the fallback exit code and the resource metrics;
        # accounting failures degrade to empty resources and never fail the
        # run itself.
        try:
            rc, out = self._remote_exec(
                client,
                f"sacct -n -p -o ExitCode,MaxRSS,AveRSS,Elapsed,TotalCPU -j {shlex.quote(job_id)}",
            )
            accounting = _parse_sacct_accounting(out) if rc == 0 else {}
        except Exception:
            accounting = {}
        if exit_code is None:
            exit_code = accounting.get("exit_code")
        if exit_code is not None:
            result = ExecResult(exit_code=exit_code, scheduler_job_id=job_id)
            _apply_slurm_accounting(result, accounting)
            return result
        return ExecResult(
            exit_code=None,
            error=f"remote slurm job {job_id} finished but its exit code is unavailable",
            scheduler_job_id=job_id,
        )

    def run_array(self, tasks: Iterable[dict[str, Any]], *, cwd: str | Path | None = None,
                  threads: int | None = None, timeout: float | None = None,
                  batch_id: str | None = None,
                  array_concurrency: int | None = None) -> list[ExecResult]:
        """Submit all tasks as one job array on the remote Slurm cluster.

        Task dicts follow the same contract as ``SlurmExecutor.run_array``;
        commands must be shlex-quoted command lines (as produced by
        ``shlex.join``) so path arguments can be rewritten into the remote
        mirror — anything needing top-level shell operators must be wrapped
        in ``bash -c``.  Inputs/outputs are not staged or backed up here;
        that stays with the caller.  The interrupt/timeout contract matches
        the local backend, with one addition: before an interrupt propagates,
        any per-task exit-code files that already exist remotely are pulled
        back, so the caller can apply the same which-exitcode-files-exist
        bookkeeping against local paths.
        """
        client = self._connect()
        sftp = client.open_sftp()
        try:
            return self._run_array_via_slurm(
                client, sftp, tasks, cwd=cwd, threads=threads, timeout=timeout,
                batch_id=batch_id, array_concurrency=array_concurrency,
            )
        finally:
            sftp.close()

    def _rewrite_array_task(self, task: _ArrayTask) -> _ArrayTask:
        """Map one task's paths and command arguments into the remote mirror."""
        try:
            argv = shlex.split(task.command)
        except ValueError as exc:
            raise ValidationError(
                f"array task {task.run_id!r} command must be a shlex-quoted command "
                "line for remote path rewriting"
            ) from exc
        stdout = self._rewrite(task.stdout_path)
        remote_dir = posixpath.dirname(stdout)
        return _ArrayTask(
            index=task.index, run_id=task.run_id,
            command=shlex.join(self._rewrite(a) for a in argv),
            stdout_path=Path(stdout), stderr_path=Path(self._rewrite(task.stderr_path)),
            exitcode_path=Path(posixpath.join(remote_dir, f"{task.run_id}.exitcode")),
            probe_path=Path(posixpath.join(remote_dir, f"{task.run_id}.env")),
        )

    def _run_array_via_slurm(self, client: Any, sftp: Any, tasks: Iterable[dict[str, Any]],
                             *, cwd: str | Path | None, threads: int | None,
                             timeout: float | None, batch_id: str | None,
                             array_concurrency: int | None) -> list[ExecResult]:
        from operon.remotes import _remove_remote_tree, sftp_makedirs
        batch_id = batch_id or f"array_{uuid.uuid4().hex[:12]}"
        _validate_array_batch(batch_id, array_concurrency)
        prepared, effective_cwd, effective_threads = _prepare_array_tasks(
            tasks, cwd=cwd, threads=threads, default_cwd=str(self.project.root),
        )
        remote_tasks = [self._rewrite_array_task(task) for task in prepared]
        remote_dir = posixpath.dirname(str(remote_tasks[0].stdout_path))
        remote_manifest = posixpath.join(remote_dir, f"{batch_id}.array-manifest.tsv")
        remote_script = posixpath.join(remote_dir, f"{batch_id}.sbatch")
        details = {"backend": "ssh", "scheduler": "slurm", "host": self.host,
                   "script": remote_script, "manifest": remote_manifest}
        for task in remote_tasks:
            sftp_makedirs(sftp, posixpath.dirname(str(task.stdout_path)))
            _remove_remote_tree(sftp, str(task.exitcode_path))
            _remove_remote_tree(sftp, str(task.probe_path))
            self._sftp_write_file(sftp, str(task.probe_script_path),
                                  _array_probe_shell(task.command) + "\n")
        self._sftp_write_file(sftp, remote_manifest, render_array_manifest(remote_tasks))
        script = render_slurm_array_script(
            job_name=f"operon_{batch_id}", manifest_path=remote_manifest,
            cwd=self._rewrite(effective_cwd),
            stdout_path=posixpath.join(remote_dir, f"{batch_id}.%A_%a.out"),
            stderr_path=posixpath.join(remote_dir, f"{batch_id}.%A_%a.err"),
            threads=effective_threads, slurm=self.slurm,
            task_count=len(remote_tasks), concurrency=array_concurrency,
        )
        self._sftp_write_file(sftp, remote_script, script)
        rc, out = self._remote_exec(client, f"sbatch --parsable {shlex.quote(remote_script)}")
        if rc != 0:
            raise RemoteError(f"remote sbatch submission failed: {out.strip()}")
        try:
            job_id = _parse_sbatch_job_id(out)
        except ExternalToolError as exc:
            raise RemoteError(str(exc)) from exc
        deadline = time.monotonic() + timeout if timeout else None
        try:
            while True:
                rc, out = self._remote_exec(client, f"squeue -h -j {shlex.quote(job_id)}")
                if rc != 0:
                    if "Invalid job id" in out:
                        break
                    raise RemoteError(f"remote squeue failed for job {job_id}: {out.strip()}")
                if not out.strip():
                    break
                if deadline is not None and time.monotonic() > deadline:
                    cancelled, cancellation_error = self._cancel_remote_slurm_job(client, job_id)
                    details["cancellation_requested"] = cancelled
                    if cancellation_error:
                        details["cancellation_error"] = cancellation_error
                    return self._remote_array_timeout_results(
                        client, sftp, prepared, remote_tasks, job_id, timeout, details)
                time.sleep(max(0.1, self.slurm.poll_interval))
        except KeyboardInterrupt:
            # Shutdown must not abandon the queued/running remote array tasks.
            self._cancel_remote_slurm_job(client, job_id)
            for local_task, remote_task in zip(prepared, remote_tasks):
                try:
                    self._sftp_get_if_exists(sftp, str(remote_task.exitcode_path),
                                             local_task.exitcode_path)
                except Exception:
                    pass
            raise
        results = []
        for local_task, remote_task in zip(prepared, remote_tasks):
            task_job_id = f"{job_id}_{remote_task.index}"
            self._sftp_get_if_exists(sftp, str(remote_task.stdout_path),
                                     local_task.stdout_path)
            self._sftp_get_if_exists(sftp, str(remote_task.stderr_path),
                                     local_task.stderr_path)
            task_details = {"array_job_id": job_id, **details}
            environment = _read_remote_probe_environment(sftp, str(remote_task.probe_path))
            if environment:
                task_details["environment"] = environment
            result = self._read_remote_slurm_accounting(
                client, remote_exitcode=str(remote_task.exitcode_path), job_id=task_job_id)
            result.details = {**task_details, **result.details}
            results.append(result)
        return results

    def _remote_array_timeout_results(self, client: Any, sftp: Any,
                                      local_tasks: list[_ArrayTask],
                                      remote_tasks: list[_ArrayTask], job_id: str,
                                      timeout: float | None,
                                      details: dict[str, Any]) -> list[ExecResult]:
        """Per-task results after a cancelled remote array; finished tasks keep theirs."""
        results = []
        for local_task, remote_task in zip(local_tasks, remote_tasks):
            task_job_id = f"{job_id}_{remote_task.index}"
            self._sftp_get_if_exists(sftp, str(remote_task.stdout_path),
                                     local_task.stdout_path)
            self._sftp_get_if_exists(sftp, str(remote_task.stderr_path),
                                     local_task.stderr_path)
            exit_code: int | None = None
            try:
                rc, out = self._remote_exec(
                    client, f"cat {shlex.quote(str(remote_task.exitcode_path))}")
                if rc == 0:
                    exit_code = int(out.strip())
            except Exception:
                exit_code = None
            if exit_code is not None:
                result = ExecResult(exit_code=exit_code)
            else:
                result = ExecResult(
                    exit_code=None,
                    error=f"timeout after {timeout}s waiting for remote slurm "
                          f"job array {job_id}",
                )
            result.scheduler_job_id = task_job_id
            task_details = {"array_job_id": job_id, **details}
            environment = _read_remote_probe_environment(sftp, str(remote_task.probe_path))
            if environment:
                task_details["environment"] = environment
            result.details = {**task_details, **result.details}
            results.append(result)
        return results

    def _cancel_remote_slurm_job(self, client: Any, job_id: str) -> tuple[bool, str]:
        """Request cancellation and report whether Slurm accepted the command."""
        try:
            rc, output = self._remote_exec(client, f"scancel {shlex.quote(job_id)}")
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if rc != 0:
            return False, output.strip() or f"scancel exited {rc}"
        return True, ""

    def _remote_exec(self, client: Any, command: str,
                     timeout: float | None = None) -> tuple[int, str]:
        _, stdout, stderr = client.exec_command(
            command, timeout=timeout if timeout is not None else self.connect_timeout,
        )
        output = stdout.read().decode("utf-8", "replace")
        error = stderr.read().decode("utf-8", "replace")
        combined = output + (("\n" if output and error else "") + error if error else "")
        return stdout.channel.recv_exit_status(), combined

    def _sftp_get_if_exists(self, sftp: Any, remote: str, local: Path) -> bool:
        try:
            sftp.stat(remote)
        except IOError:
            return False
        local.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{local.name}.operon-tmp-", dir=local.parent)
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            sftp.get(remote, str(tmp))
            os.replace(tmp, local)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return True

    # -- output retrieval --------------------------------------------------

    def _pull_outputs(self, client: Any, sftp: Any, expected_outputs: Iterable[Any]) -> None:
        from operon.remotes import _remote_directory_identity, remote_sha256
        for item in expected_outputs:
            local = Path(item)
            remote = self._rewrite(local)
            try:
                stat = sftp.stat(remote)
            except IOError:
                continue  # the caller's expected-output check reports it
            if local.exists():
                if stat_module.S_ISDIR(stat.st_mode) and local.is_dir():
                    remote_digest, _ = _remote_directory_identity(sftp, remote)
                    local_digest = sha256_path(local)
                elif stat_module.S_ISREG(stat.st_mode) and local.is_file():
                    remote_digest = remote_sha256(client, remote, sftp=sftp)
                    local_digest = sha256_file(local)
                else:
                    raise ConflictError(
                        f"local and remote expected outputs have different artifact types: {local}"
                    )
                if local_digest != remote_digest:
                    raise ConflictError(
                        f"local expected output already exists with different content: {local}"
                    )
                continue
            if stat_module.S_ISDIR(stat.st_mode):
                self._pull_directory(sftp, remote, local)
            else:
                self._sftp_get_if_exists(sftp, remote, local)
            if stat_module.S_ISDIR(stat.st_mode):
                remote_digest, _ = _remote_directory_identity(sftp, remote)
                local_digest = sha256_path(local)
            else:
                remote_digest = remote_sha256(client, remote, sftp=sftp)
                local_digest = sha256_file(local)
            if local_digest != remote_digest:
                if local.is_dir() and not local.is_symlink():
                    shutil.rmtree(local, ignore_errors=True)
                else:
                    local.unlink(missing_ok=True)
                raise RemoteError(f"retrieved output checksum mismatch: {local}")

    def _pull_directory(self, sftp: Any, remote: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f".{local.name}.operon-tmp-", dir=local.parent))
        try:
            self._pull_directory_into(sftp, remote, tmp)
            os.replace(tmp, local)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    def _pull_directory_into(self, sftp: Any, remote: str, local: Path) -> None:
        for entry in sftp.listdir_attr(remote):
            remote_child = posixpath.join(remote, entry.filename)
            local_child = local / entry.filename
            lstat = getattr(sftp, "lstat", None)
            mode = (lstat(remote_child) if lstat else entry).st_mode
            if stat_module.S_ISLNK(mode):
                os.symlink(sftp.readlink(remote_child), local_child)
            elif stat_module.S_ISDIR(mode):
                local_child.mkdir(exist_ok=True)
                self._pull_directory_into(sftp, remote_child, local_child)
            elif stat_module.S_ISREG(mode):
                sftp.get(remote_child, str(local_child))
            else:
                raise RemoteError(f"unsupported remote output entry type: {remote_child}")
