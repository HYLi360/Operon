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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from operon.config import Project
from operon.errors import ConflictError, ExternalToolError, RemoteError, ValidationError
from operon.utils import iter_directory_entries, sha256_file, sha256_path

VALID_BACKENDS = ("local", "slurm", "ssh")


@dataclass
class ExecResult:
    exit_code: int | None
    error: str | None = None
    scheduler_job_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


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
    local_root = local_root.resolve()
    local = str(local_root)
    if value == local:
        return posixpath.normpath(remote_root)
    prefix = local + "/"
    if value.startswith(prefix):
        candidate = Path(value).resolve(strict=False)
        if not candidate.is_relative_to(local_root):
            raise ValidationError(
                f"local path escapes the project root and cannot be mapped over SSH: {value}"
            )
        relative = candidate.relative_to(local_root).as_posix()
        root = posixpath.normpath(remote_root)
        mapped = posixpath.normpath(posixpath.join(root, relative))
        if mapped == root or mapped.startswith(root.rstrip("/") + "/"):
            return mapped
        raise ValidationError(f"mapped SSH path escapes remote_root {root!r}: {value}")
    return value


def render_slurm_script(*, job_name: str, command_line: str, cwd: str,
                        stdout_path: str, stderr_path: str, exitcode_path: str,
                        threads: int | None, slurm: SlurmConfig) -> str:
    """Render a self-contained sbatch script (pure, unit-testable)."""
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --output={stdout_path}",
        f"#SBATCH --error={stderr_path}",
        f"#SBATCH --cpus-per-task={max(1, int(threads or 1))}",
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
    lines.append(f"cd {shlex.quote(cwd)}")
    lines.append("rc=$?")
    lines.append("if [ $rc -eq 0 ]; then")
    lines.append(f"  {command_line}")
    lines.append("  rc=$?")
    lines.append("fi")
    lines.append(f"echo $rc > {shlex.quote(exitcode_path)}")
    lines.append("exit $rc")
    return "\n".join(lines) + "\n"


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


class LocalExecutor:
    name = "local"

    def describe(self) -> str:
        return "local"

    def cache_identity(self) -> str:
        return "local"

    def run(self, argv: Iterable[Any], *, cwd: str | Path | None, stdout_path: Path,
            stderr_path: Path, timeout: float | None = None, threads: int | None = None,
            run_id: str | None = None, stage_inputs: Iterable[Any] = (),
            expected_outputs: Iterable[Any] = ()) -> ExecResult:
        command = [str(a) for a in argv]
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            # A dedicated process group lets shutdown (or timeout) terminate
            # the tool and any children it spawned, without orphaning them.
            process = subprocess.Popen(
                command, cwd=str(cwd) if cwd else None,
                stdout=out, stderr=err, start_new_session=True,
            )
            try:
                process.wait(timeout=timeout)
            except BaseException:
                # TimeoutExpired, ShutdownRequested/KeyboardInterrupt or any
                # other abort: kill the whole group, then re-raise unchanged.
                _terminate_process_group(process)
                raise
        return ExecResult(exit_code=process.returncode, details={"backend": "local"})


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


def _read_slurm_exit_code(exitcode_path: Path, job_id: str, retries: int = 5) -> ExecResult:
    for _ in range(retries):
        try:
            return ExecResult(exit_code=int(exitcode_path.read_text().strip()))
        except (OSError, ValueError):
            time.sleep(1)  # wait for shared-filesystem metadata to settle
    sacct = shutil.which("sacct")
    if sacct:
        proc = subprocess.run([sacct, "-n", "-o", "ExitCode", "-j", job_id],
                              capture_output=True, text=True)
        for line in proc.stdout.splitlines():
            token = line.strip()
            if ":" in token:
                try:
                    return ExecResult(exit_code=int(token.split(":")[0]))
                except ValueError:
                    continue
    return ExecResult(exit_code=None,
                      error=f"slurm job {job_id} finished but its exit code is unavailable")


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
        script = render_slurm_script(
            job_name=f"operon_{label}",
            command_line=shlex.join(str(a) for a in argv),
            cwd=str(cwd) if cwd else str(self.project.root),
            stdout_path=str(stdout_path), stderr_path=str(stderr_path),
            exitcode_path=str(exitcode_path), threads=threads, slurm=self.slurm,
        )
        script_path.write_text(script, encoding="utf-8")
        exitcode_path.unlink(missing_ok=True)
        job_id = _submit_slurm_job(sbatch, script_path)
        deadline = time.monotonic() + timeout if timeout else None
        try:
            while True:
                if _squeue_job_gone(squeue, job_id):
                    break
                if deadline is not None and time.monotonic() > deadline:
                    _scancel_slurm_job(job_id)
                    return ExecResult(exit_code=None,
                                      error=f"timeout after {timeout}s waiting for slurm job {job_id}",
                                      scheduler_job_id=job_id)
                time.sleep(max(0.1, self.slurm.poll_interval))
        except KeyboardInterrupt:
            # Shutdown must not abandon a queued/running cluster job.
            _scancel_slurm_job(job_id)
            raise
        result = _read_slurm_exit_code(exitcode_path, job_id)
        result.scheduler_job_id = job_id
        result.details = {"backend": "slurm", "script": str(script_path)}
        return result


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
            self._reset_outputs(sftp, expected_outputs)
            if self.scheduler == "slurm":
                result = self._run_via_slurm(
                    client, sftp, [str(a) for a in argv], cwd=cwd,
                    stdout_path=Path(stdout_path), stderr_path=Path(stderr_path),
                    timeout=timeout, threads=threads, run_id=run_id,
                )
            else:
                result = self._run_direct(
                    client, [str(a) for a in argv], cwd=cwd,
                    stdout_path=Path(stdout_path), stderr_path=Path(stderr_path),
                    timeout=timeout, run_id=run_id,
                )
            if result.exit_code == 0:
                self._pull_outputs(client, sftp, expected_outputs)
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

    def _reset_outputs(self, sftp: Any, expected_outputs: Iterable[Any]) -> None:
        from operon.remotes import _remove_remote_tree, sftp_makedirs
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
                _remove_remote_tree(sftp, normalized)
            sftp_makedirs(sftp, posixpath.dirname(remote))

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
                    timeout: float | None, run_id: str | None) -> ExecResult:
        command = shlex.join(self._rewrite(a) for a in argv)
        remote_cwd = self._rewrite(cwd) if cwd else self.remote_root
        if remote_cwd:
            command = f"cd {shlex.quote(remote_cwd)} && {command}"
        label = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id or uuid.uuid4().hex)
        pidfile = f"/tmp/operon-{label}-{uuid.uuid4().hex}.pid"
        payload = (
            f"umask 077; echo $$ > {shlex.quote(pidfile)}; "
            f"trap 'rm -f {shlex.quote(pidfile)}' EXIT; {command}"
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
                        )
                    time.sleep(0.05)
            except KeyboardInterrupt:
                # The remote payload runs under setsid and would survive
                # connection teardown, so terminate its process group first.
                try:
                    self._terminate_remote_process(client, pidfile)
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
        )

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
        script = render_slurm_script(
            job_name=f"operon_{label}",
            command_line=shlex.join(self._rewrite(a) for a in argv),
            cwd=self._rewrite(cwd) if cwd else (self.remote_root or "."),
            stdout_path=remote_stdout, stderr_path=remote_stderr,
            exitcode_path=remote_exitcode, threads=threads, slurm=self.slurm,
        )
        sftp_makedirs(sftp, remote_dir)
        _remove_remote_tree(sftp, remote_exitcode)
        _remove_remote_tree(sftp, remote_script)
        script_tmp = f"{remote_script}.operon-tmp-{uuid.uuid4().hex}"
        try:
            with sftp.open(script_tmp, "w") as handle:
                handle.write(script)
            sftp.rename(script_tmp, remote_script)
        except BaseException:
            _remove_remote_tree(sftp, script_tmp)
            raise
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
                    self._remote_exec(client, f"scancel {shlex.quote(job_id)}")
                    return ExecResult(exit_code=None,
                                      error=f"timeout after {timeout}s waiting for remote slurm job {job_id}",
                                      scheduler_job_id=job_id)
                time.sleep(max(0.1, self.slurm.poll_interval))
        except KeyboardInterrupt:
            # Shutdown must not abandon the queued/running remote cluster job.
            try:
                self._remote_exec(client, f"scancel {shlex.quote(job_id)}")
            except Exception:
                pass
            raise
        self._sftp_get_if_exists(sftp, remote_stdout, stdout_path)
        self._sftp_get_if_exists(sftp, remote_stderr, stderr_path)
        for _ in range(5):
            rc, out = self._remote_exec(client, f"cat {shlex.quote(remote_exitcode)}")
            if rc == 0:
                try:
                    return ExecResult(
                        exit_code=int(out.strip()), scheduler_job_id=job_id,
                        details={"backend": "ssh", "scheduler": "slurm", "host": self.host,
                                 "script": remote_script},
                    )
                except ValueError:
                    pass
            time.sleep(1)
        rc, out = self._remote_exec(client, f"sacct -n -o ExitCode -j {shlex.quote(job_id)}")
        for line in out.splitlines():
            token = line.strip()
            if ":" in token:
                try:
                    return ExecResult(
                        exit_code=int(token.split(":")[0]), scheduler_job_id=job_id,
                        details={"backend": "ssh", "scheduler": "slurm", "host": self.host,
                                 "script": remote_script},
                    )
                except ValueError:
                    continue
        return ExecResult(exit_code=None,
                          error=f"remote slurm job {job_id} finished but its exit code is unavailable",
                          scheduler_job_id=job_id)

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
