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
import shlex
import shutil
import stat as stat_module
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from operon.config import Project
from operon.errors import ConflictError, ExternalToolError, RemoteError, ValidationError
from operon.utils import sha256_file, sha256_path

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
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            completed = subprocess.run(
                [str(a) for a in argv], cwd=str(cwd) if cwd else None,
                stdout=out, stderr=err, timeout=timeout,
            )
        return ExecResult(exit_code=completed.returncode, details={"backend": "local"})


def _submit_slurm_job(sbatch: str, script_path: Path) -> str:
    proc = subprocess.run([sbatch, "--parsable", str(script_path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise ExternalToolError(f"sbatch submission failed: {proc.stderr.strip() or proc.stdout.strip()}")
    job_id = proc.stdout.strip().split(";")[0].strip()
    if not job_id:
        raise ExternalToolError(f"could not parse sbatch job id from {proc.stdout!r}")
    return job_id


def _squeue_job_gone(squeue: str, job_id: str) -> bool:
    proc = subprocess.run([squeue, "-h", "-j", job_id], capture_output=True, text=True)
    if proc.returncode != 0:
        # Completed jobs disappear with "Invalid job id specified".
        if "Invalid job id" in (proc.stderr or ""):
            return True
        raise ExternalToolError(f"squeue failed for job {job_id}: {proc.stderr.strip()}")
    return not proc.stdout.strip()


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
        while True:
            if _squeue_job_gone(squeue, job_id):
                break
            if deadline is not None and time.monotonic() > deadline:
                scancel = shutil.which("scancel")
                if scancel:
                    subprocess.run([scancel, job_id], capture_output=True)
                return ExecResult(exit_code=None,
                                  error=f"timeout after {timeout}s waiting for slurm job {job_id}",
                                  scheduler_job_id=job_id)
            time.sleep(max(0.1, self.slurm.poll_interval))
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
            self.host = storage.host
            self.user = self.user or storage.user
            self.port = storage.port
            self.key_file = self.key_file or storage.key_file
            self.remote_root = self.remote_root or posixpath.normpath(storage.root)
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

    def describe(self) -> str:
        return f"ssh:{self.user + '@' if self.user else ''}{self.host}"

    def cache_identity(self) -> str:
        return (
            f"{self.describe()}:{self.port}:scheduler={self.scheduler}:"
            f"root={self.remote_root}:storage={self.storage_remote}:"
            f"hostkey={self.host_key_sha256}:slurm={self.slurm!r}"
        )

    def _connect(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(self)
        from operon.remotes import connect_ssh
        return connect_ssh(self.host, user=self.user, port=self.port,
                           key_file=self.key_file, connect_timeout=self.connect_timeout,
                           known_hosts=self.known_hosts, host_key_sha256=self.host_key_sha256,
                           insecure_accept_unknown_host=self.insecure_accept_unknown_host)

    def _rewrite(self, value: Any) -> str:
        return rewrite_remote_path(str(value), self.project.root, self.remote_root)

    def prepare_database(self, path: str | Path, *, mutable_cache: bool) -> str:
        """Require a pre-provisioned remote reference or create a mutable cache dir."""
        remote = self._rewrite(Path(path))
        client = self._connect()
        try:
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
        finally:
            client.close()
        return remote

    def run(self, argv: Iterable[Any], *, cwd: str | Path | None, stdout_path: Path,
            stderr_path: Path, timeout: float | None = None, threads: int | None = None,
            run_id: str | None = None, stage_inputs: Iterable[Any] = (),
            expected_outputs: Iterable[Any] = ()) -> ExecResult:
        client = self._connect()
        try:
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
                        timeout=timeout,
                    )
                if result.exit_code == 0:
                    self._pull_outputs(client, sftp, expected_outputs)
                return result
            finally:
                sftp.close()
        finally:
            client.close()

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
            tmp = f"{remote}.operon-tmp"
            sftp.put(str(local), tmp)
            sftp.rename(tmp, remote)
            from operon.remotes import remote_sha256
            digest = remote_sha256(client, remote, sftp=sftp)
            if digest != sha256_file(local):
                raise RemoteError(f"staged input verification failed for {remote}")

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
        tmp = f"{remote}.operon-tmp"
        _remove_remote_tree(sftp, tmp)
        sftp.mkdir(tmp)
        try:
            for path in sorted(local.rglob("*"), key=lambda p: p.relative_to(local).as_posix()):
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
                    timeout: float | None) -> ExecResult:
        command = shlex.join(self._rewrite(a) for a in argv)
        remote_cwd = self._rewrite(cwd) if cwd else self.remote_root
        if remote_cwd:
            command = f"cd {shlex.quote(remote_cwd)} && {command}"
        _, stdout, _ = client.exec_command(command)
        channel = stdout.channel
        deadline = time.monotonic() + timeout if timeout else None
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            while True:
                while channel.recv_ready():
                    out.write(channel.recv(65536))
                while channel.recv_stderr_ready():
                    err.write(channel.recv_stderr(65536))
                if channel.exit_status_ready():
                    break
                if deadline is not None and time.monotonic() > deadline:
                    channel.close()
                    return ExecResult(exit_code=None, error=f"timeout after {timeout}s")
                time.sleep(0.05)
            while channel.recv_ready():
                out.write(channel.recv(65536))
            while channel.recv_stderr_ready():
                err.write(channel.recv_stderr(65536))
        return ExecResult(
            exit_code=channel.recv_exit_status(),
            details={"backend": "ssh", "scheduler": "none", "host": self.host},
        )

    # -- remote slurm ------------------------------------------------------

    def _run_via_slurm(self, client: Any, sftp: Any, argv: list[str], *,
                       cwd: str | Path | None, stdout_path: Path, stderr_path: Path,
                       timeout: float | None, threads: int | None,
                       run_id: str | None) -> ExecResult:
        from operon.remotes import sftp_makedirs
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
        with sftp.open(f"{remote_script}.operon-tmp", "w") as handle:
            handle.write(script)
        sftp.rename(f"{remote_script}.operon-tmp", remote_script)
        rc, out = self._remote_exec(client, f"sbatch --parsable {shlex.quote(remote_script)}")
        if rc != 0:
            raise RemoteError(f"remote sbatch submission failed: {out.strip()}")
        job_id = out.strip().split(";")[0].strip().splitlines()[-1]
        if not job_id:
            raise RemoteError(f"could not parse remote sbatch job id from {out!r}")
        deadline = time.monotonic() + timeout if timeout else None
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
            time.sleep(min(self.slurm.poll_interval, 5.0))
        self._sftp_get_if_exists(sftp, remote_stdout, stdout_path)
        self._sftp_get_if_exists(sftp, remote_stderr, stderr_path)
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

    def _remote_exec(self, client: Any, command: str) -> tuple[int, str]:
        _, stdout, stderr = client.exec_command(command)
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
        fd_tmp = local.parent / f".{local.name}.operon-tmp"
        sftp.get(remote, str(fd_tmp))
        os.replace(fd_tmp, local)
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
        tmp = local.parent / f".{local.name}.operon-tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
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
