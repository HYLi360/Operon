"""Failure-path tests for local, Slurm, and SSH execution backends."""

from __future__ import annotations

import builtins
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from operon import execution
from operon.errors import ConflictError, ExternalToolError, RemoteError, ValidationError
from tests.unit.test_execution import FakeSFTP, FakeSSHClient


def project(tmp_path: Path, execution_config=None):
    return SimpleNamespace(
        root=tmp_path,
        logs_root=tmp_path / "logs",
        config={"execution": execution_config or {}},
    )


def test_slurm_config_merging_and_validation(tmp_path):
    p = project(tmp_path, {"slurm": {
        "partition": "p", "time": "01:00:00", "mem_gb": 2,
        "extra_sbatch": ["--qos=x"], "setup_commands": ["module load x"],
        "poll_interval": 1,
    }})
    cfg = execution.load_slurm_config(p, {"partition": "override", "mem_gb": "3", "time": ""})
    assert cfg.partition == "override" and cfg.mem_gb == 3 and cfg.time_limit == "01:00:00"
    for raw in (
        {"mem_gb": -1}, {"poll_interval": -1},
        {"extra_sbatch": ["one\ntwo"]}, {"setup_commands": ["one\rtwo"]},
    ):
        with pytest.raises(ValidationError):
            execution.load_slurm_config(project(tmp_path, {"slurm": raw}))


def test_process_group_termination_fallback_and_kill(monkeypatch):
    class Proc:
        pid = 10

        def __init__(self):
            self.polls = iter([None, None, None, 0])
            self.terminated = self.killed = self.waited = False

        def poll(self):
            return next(self.polls, 0)

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self):
            self.waited = True

    proc = Proc()
    monkeypatch.setattr(execution.os, "killpg", lambda *_a: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(execution.time, "monotonic", lambda: 1)
    monkeypatch.setattr(execution.time, "sleep", lambda *_a: None)
    execution._terminate_process_group(proc, grace=1)
    assert proc.terminated and proc.waited

    class Alive(Proc):
        def poll(self):
            return None

    alive = Alive()
    times = iter([0, 2, 2])
    monkeypatch.setattr(execution.time, "monotonic", lambda: next(times, 2))
    execution._terminate_process_group(alive, grace=1)
    assert alive.killed and alive.waited

    done = Proc()
    done.polls = iter([0])
    execution._terminate_process_group(done)
    assert not done.waited


def test_process_rss_falls_back_to_portable_ps(monkeypatch):
    real_open = builtins.open

    def no_procfs(path, *args, **kwargs):
        if str(path).startswith("/proc/"):
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", no_procfs)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="20480\n"
    ))
    assert execution._read_process_rss_mb(123) == 20.0


def test_slurm_command_helpers(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=1, stderr="denied", stdout=""
    ))
    with pytest.raises(ExternalToolError, match="submission failed"):
        execution._submit_slurm_job("sbatch", tmp_path / "x")
    with pytest.raises(ExternalToolError, match="squeue failed"):
        execution._squeue_job_gone("squeue", "1")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=1, stderr="Invalid job id specified", stdout=""
    ))
    assert execution._squeue_job_gone("squeue", "1") is True
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=0, stderr="", stdout="running"
    ))
    assert execution._squeue_job_gone("squeue", "1") is False

    monkeypatch.setattr(execution.shutil, "which", lambda _name: None)
    execution._scancel_slurm_job("1")
    monkeypatch.setattr(execution.shutil, "which", lambda _name: "scancel")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError()))
    execution._scancel_slurm_job("1")


def test_slurm_exit_code_file_sacct_and_unavailable(monkeypatch, tmp_path):
    path = tmp_path / "exit"
    path.write_text("3", encoding="utf-8")
    assert execution._read_slurm_accounting(path, "1").exit_code == 3
    path.write_text("bad", encoding="utf-8")
    monkeypatch.setattr(execution.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(execution.shutil, "which", lambda name: "sacct" if name == "sacct" else None)
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="bad\n4:0|||||\n"))
    assert execution._read_slurm_accounting(path, "1", retries=1).exit_code == 4
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(stdout="bad:x|||||\n"))
    result = execution._read_slurm_accounting(path, "1", retries=1)
    assert result.exit_code is None and "unavailable" in result.error


def test_slurm_executor_requires_commands_timeout_and_interrupt(tmp_path, monkeypatch):
    executor = execution.SlurmExecutor(project(tmp_path), execution.SlurmConfig(poll_interval=0.01))
    out, err = tmp_path / "o", tmp_path / "e"
    monkeypatch.setattr(execution.shutil, "which", lambda name: None)
    with pytest.raises(ExternalToolError, match="requires 'sbatch'"):
        executor.run(["x"], cwd=None, stdout_path=out, stderr_path=err)
    monkeypatch.setattr(execution.shutil, "which", lambda name: "sbatch" if name == "sbatch" else None)
    with pytest.raises(ExternalToolError, match="requires 'squeue'"):
        executor.run(["x"], cwd=None, stdout_path=out, stderr_path=err)

    monkeypatch.setattr(execution.shutil, "which", lambda name: name)
    def submit_with_probe(*_args):
        (tmp_path / "r.env").write_text("hostname=compute-01\nos=Linux\n", encoding="utf-8")
        return "42"

    monkeypatch.setattr(execution, "_submit_slurm_job", submit_with_probe)
    monkeypatch.setattr(execution, "_squeue_job_gone", lambda *_a: False)
    monkeypatch.setattr(execution, "_scancel_slurm_job", lambda job: None)
    times = iter([0, 2, 2])
    monkeypatch.setattr(execution.time, "monotonic", lambda: next(times, 2))
    monkeypatch.setattr(execution.time, "sleep", lambda *_a: None)
    result = executor.run(["x"], cwd=None, stdout_path=out, stderr_path=err, timeout=1, run_id="r")
    assert result.exit_code is None and result.scheduler_job_id == "42"
    assert result.details["backend"] == "slurm"
    assert result.details["environment"]["hostname"] == "compute-01"
    assert not (tmp_path / "r.env").exists()

    monkeypatch.setattr(execution, "_squeue_job_gone", lambda *_a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        executor.run(["x"], cwd=None, stdout_path=out, stderr_path=err, run_id="r2")


def test_ssh_storage_remote_validation_and_inheritance(tmp_path, monkeypatch):
    p = project(tmp_path)
    storage = SimpleNamespace(
        host="storage", user="user", port=2222, root="/remote", key_file="key",
        known_hosts="known", host_key_sha256="sha", insecure_accept_unknown_host=True,
    )
    monkeypatch.setattr("operon.remotes.get_remote", lambda *_a: storage)
    with pytest.raises(ValidationError, match="host .* differs"):
        execution.SSHExecutor(p, {"storage_remote": "r", "host": "other"}, execution.SlurmConfig())
    with pytest.raises(ValidationError, match="user differs"):
        execution.SSHExecutor(p, {"storage_remote": "r", "user": "other"}, execution.SlurmConfig())
    with pytest.raises(ValidationError, match="port differs"):
        execution.SSHExecutor(p, {"storage_remote": "r", "port": 23}, execution.SlurmConfig())
    ssh = execution.SSHExecutor(p, {"storage_remote": "r"}, execution.SlurmConfig())
    assert ssh.host == "storage" and ssh.port == 2222 and ssh.remote_root == "/remote"
    assert ssh.describe() == "ssh:user@storage"
    assert "hostkey=sha" in ssh.cache_identity()


def test_ssh_connection_close_prepare_database_and_termination(tmp_path, monkeypatch):
    p = project(tmp_path)

    class SFTP:
        def __init__(self):
            self.closed = False

        def stat(self, path):
            if "missing" in path:
                raise IOError()
            return SimpleNamespace(st_size=1)

        def close(self):
            self.closed = True

    class Client:
        def __init__(self):
            self.sftp = SFTP()
            self.closed = False

        def open_sftp(self):
            return self.sftp

        def close(self):
            self.closed = True

    client = Client()
    ssh = execution.SSHExecutor(
        p, {"host": "host", "remote_root": str(tmp_path)}, execution.SlurmConfig(),
        client_factory=lambda _self: client,
    )
    assert ssh.client is client and ssh.client is client
    db = tmp_path / "db"
    db.write_text("x", encoding="utf-8")
    assert ssh.prepare_database(db, mutable_cache=False).endswith("/db")
    assert ssh.prepare_database(db, mutable_cache=False).endswith("/db")
    missing = tmp_path / "missing"
    with pytest.raises(RemoteError, match="not provisioned"):
        ssh.prepare_database(missing, mutable_cache=False)
    monkeypatch.setattr("operon.remotes.sftp_makedirs", lambda *_a: None)
    assert ssh.prepare_database(missing, mutable_cache=True).endswith("/missing")

    monkeypatch.setattr(ssh, "_remote_exec", lambda *_a, **_k: (0, ""))
    assert ssh._terminate_remote_process(client, "/tmp/pid") == (True, "")
    monkeypatch.setattr(ssh, "_remote_exec", lambda *_a, **_k: (2, "gone"))
    assert ssh._terminate_remote_process(client, "/tmp/pid") == (False, "gone")
    monkeypatch.setattr(ssh, "_remote_exec", lambda *_a, **_k: (_ for _ in ()).throw(OSError("bad")))
    signaled, error = ssh._terminate_remote_process(client, "/tmp/pid")
    assert signaled is False and "OSError" in error
    ssh.close()
    assert client.closed and ssh._client is None


def test_sftp_get_if_exists_and_cleanup(tmp_path):
    ssh = execution.SSHExecutor(project(tmp_path), {"host": "host"}, execution.SlurmConfig())

    class SFTP:
        def stat(self, path):
            if path == "missing":
                raise IOError()
            return SimpleNamespace()

        def get(self, remote, local):
            Path(local).write_text(remote, encoding="utf-8")

    sftp = SFTP()
    local = tmp_path / "nested" / "file"
    assert ssh._sftp_get_if_exists(sftp, "remote", local) is True
    assert local.read_text() == "remote"
    assert ssh._sftp_get_if_exists(sftp, "missing", tmp_path / "x") is False

    class Failed(SFTP):
        def get(self, remote, local):
            raise OSError("bad")

    with pytest.raises(OSError):
        ssh._sftp_get_if_exists(Failed(), "remote", tmp_path / "failed")


def test_stage_file_missing_idempotent_conflict_and_verification(tmp_path, monkeypatch):
    root = tmp_path / "project"
    remote_root = tmp_path / "remote"
    root.mkdir()
    remote_root.mkdir()
    p = project(root)
    client = FakeSSHClient()
    ssh = execution.SSHExecutor(
        p, {"host": "host", "remote_root": str(remote_root)}, execution.SlurmConfig(),
        client_factory=lambda _self: client,
    )
    with pytest.raises(RemoteError, match="missing local input"):
        ssh._stage_inputs(client, client.sftp, [root / "missing"])

    local = root / "input.txt"
    local.write_text("same", encoding="utf-8")
    remote = remote_root / "input.txt"
    remote.write_text("same", encoding="utf-8")
    ssh._stage_inputs(client, client.sftp, [local])
    remote.write_text("different", encoding="utf-8")
    with pytest.raises(ConflictError, match="different bytes"):
        ssh._stage_inputs(client, client.sftp, [local])

    remote.unlink()
    monkeypatch.setattr("operon.remotes.remote_sha256", lambda *_a, **_k: "0" * 64)
    with pytest.raises(RemoteError, match="verification failed"):
        ssh._stage_inputs(client, client.sftp, [local])
    assert not list(remote_root.glob("*.operon-tmp-*"))


def test_stage_directory_identity_conflict_and_tree_entries(tmp_path):
    root = tmp_path / "project"
    remote_root = tmp_path / "remote"
    local = root / "tree"
    (local / "sub").mkdir(parents=True)
    remote_root.mkdir()
    (local / "a.txt").write_text("a", encoding="utf-8")
    (local / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (local / "link").symlink_to("a.txt")
    client = FakeSSHClient()
    ssh = execution.SSHExecutor(
        project(root), {"host": "host", "remote_root": str(remote_root)},
        execution.SlurmConfig(), client_factory=lambda _self: client,
    )
    remote = str(remote_root / "tree")
    ssh._stage_directory(client, client.sftp, local, remote)
    assert (remote_root / "tree" / "sub" / "b.txt").read_text() == "b"
    assert (remote_root / "tree" / "link").is_symlink()
    ssh._stage_directory(client, client.sftp, local, remote)
    (local / "a.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ConflictError, match="different content"):
        ssh._stage_directory(client, client.sftp, local, remote)


def test_reset_and_pull_outputs_for_files_directories_and_conflicts(tmp_path, monkeypatch):
    root = tmp_path / "project"
    remote_root = tmp_path / "remote"
    root.mkdir()
    remote_root.mkdir()
    client = FakeSSHClient()
    ssh = execution.SSHExecutor(
        project(root), {"host": "host", "remote_root": str(remote_root)},
        execution.SlurmConfig(), client_factory=lambda _self: client,
    )
    with pytest.raises(ValidationError, match="under the project root"):
        ssh._reset_outputs(client.sftp, [tmp_path / "outside"])

    missing = root / "missing.txt"
    ssh._pull_outputs(client, client.sftp, [missing])
    assert not missing.exists()

    remote_file = remote_root / "result.txt"
    remote_file.write_text("value", encoding="utf-8")
    local_file = root / "result.txt"
    ssh._pull_outputs(client, client.sftp, [local_file])
    assert local_file.read_text() == "value"
    ssh._pull_outputs(client, client.sftp, [local_file])
    local_file.write_text("changed", encoding="utf-8")
    with pytest.raises(ConflictError, match="different content"):
        ssh._pull_outputs(client, client.sftp, [local_file])

    local_file.unlink()
    local_file.mkdir()
    with pytest.raises(ConflictError, match="different artifact types"):
        ssh._pull_outputs(client, client.sftp, [local_file])
    local_file.rmdir()

    remote_dir = remote_root / "directory"
    (remote_dir / "sub").mkdir(parents=True)
    (remote_dir / "plain").write_text("p", encoding="utf-8")
    (remote_dir / "sub" / "nested").write_text("n", encoding="utf-8")
    (remote_dir / "link").symlink_to("plain")
    local_dir = root / "directory"
    ssh._pull_outputs(client, client.sftp, [local_dir])
    assert (local_dir / "sub" / "nested").read_text() == "n"
    assert (local_dir / "link").is_symlink()
    ssh._pull_outputs(client, client.sftp, [local_dir])

    monkeypatch.setattr(execution, "sha256_file", lambda path: "local" if str(path).startswith(str(root)) else "remote")
    remote_bad = remote_root / "bad.txt"
    remote_bad.write_text("bad", encoding="utf-8")
    with pytest.raises(RemoteError, match="checksum mismatch"):
        ssh._pull_outputs(client, client.sftp, [root / "bad.txt"])
    assert not (root / "bad.txt").exists()


def test_remote_slurm_submission_queue_and_exitcode_failures(tmp_path, monkeypatch):
    root = tmp_path / "project"
    remote_root = tmp_path / "remote"
    (root / "logs").mkdir(parents=True)
    remote_root.mkdir()
    ssh = execution.SSHExecutor(
        project(root), {"host": "host", "remote_root": str(remote_root), "scheduler": "slurm"},
        execution.SlurmConfig(poll_interval=0.01),
    )
    sftp = FakeSFTP()
    out = root / "logs" / "o"
    err = root / "logs" / "e"
    monkeypatch.setattr(execution.time, "sleep", lambda *_a: None)

    monkeypatch.setattr(ssh, "_remote_exec", lambda *_a, **_k: (1, "denied"))
    with pytest.raises(RemoteError, match="submission failed"):
        ssh._run_via_slurm(None, sftp, ["true"], cwd=root, stdout_path=out,
                           stderr_path=err, timeout=None, threads=None, run_id="a")

    responses = iter([(0, "not-a-job" )])
    monkeypatch.setattr(ssh, "_remote_exec", lambda *_a, **_k: next(responses))
    with pytest.raises(RemoteError, match="could not parse"):
        ssh._run_via_slurm(None, sftp, ["true"], cwd=root, stdout_path=out,
                           stderr_path=err, timeout=None, threads=None, run_id="b")

    responses = iter([(0, "42"), (1, "queue broken")])
    monkeypatch.setattr(ssh, "_remote_exec", lambda *_a, **_k: next(responses))
    with pytest.raises(RemoteError, match="squeue failed"):
        ssh._run_via_slurm(None, sftp, ["true"], cwd=root, stdout_path=out,
                           stderr_path=err, timeout=None, threads=None, run_id="c")

    calls = []
    times = iter([0, 2, 2])
    monkeypatch.setattr(execution.time, "monotonic", lambda: next(times, 2))
    def timeout_exec(_client, command, **_kwargs):
        calls.append(command)
        if command.startswith("sbatch"):
            probe = remote_root / "logs" / "d.env"
            probe.write_text("hostname=compute-01\nos=Linux\n", encoding="utf-8")
            return 0, "42"
        if command.startswith("squeue"):
            return 0, "42"
        return 0, ""
    monkeypatch.setattr(ssh, "_remote_exec", timeout_exec)
    result = ssh._run_via_slurm(None, sftp, ["true"], cwd=root, stdout_path=out,
                                stderr_path=err, timeout=1, threads=None, run_id="d")
    assert result.exit_code is None and any(command.startswith("scancel") for command in calls)
    assert result.details["backend"] == "ssh"
    assert result.details["scheduler"] == "slurm"
    assert result.details["cancellation_requested"] is True
    assert result.details["environment"]["hostname"] == "compute-01"
    assert not (remote_root / "logs" / "d.env").exists()

    times = iter([0, 2, 2])
    monkeypatch.setattr(execution.time, "monotonic", lambda: next(times, 2))

    def failed_cancel(_client, command, **_kwargs):
        if command.startswith("sbatch"):
            return 0, "43"
        if command.startswith("squeue"):
            return 0, "43"
        if command.startswith("scancel"):
            return 1, "permission denied"
        return 0, ""

    monkeypatch.setattr(ssh, "_remote_exec", failed_cancel)
    result = ssh._run_via_slurm(None, sftp, ["true"], cwd=root, stdout_path=out,
                                stderr_path=err, timeout=1, threads=None, run_id="e")
    assert result.details["cancellation_requested"] is False
    assert result.details["cancellation_error"] == "permission denied"
    assert "job may still be running" in result.error


def _remote_slurm_executor(tmp_path, monkeypatch):
    root = tmp_path / "project"
    remote_root = tmp_path / "remote"
    (root / "logs").mkdir(parents=True)
    remote_root.mkdir()
    ssh = execution.SSHExecutor(
        project(root), {"host": "host", "remote_root": str(remote_root), "scheduler": "slurm"},
        execution.SlurmConfig(poll_interval=0.01),
    )
    monkeypatch.setattr(execution.time, "sleep", lambda *_a: None)
    return ssh, root


def test_remote_slurm_exitcode_falls_back_to_sacct(tmp_path, monkeypatch):
    ssh, root = _remote_slurm_executor(tmp_path, monkeypatch)

    def fake_exec(_client, command, **_kwargs):
        if command.startswith("sbatch"):
            return 0, "42"
        if command.startswith("squeue"):
            return 0, ""  # job already left the queue
        if command.startswith("cat "):
            return 1, ""  # the exitcode file never becomes visible
        if command.startswith("sacct"):
            return 0, "bad\n4:0|||||\n"
        return 0, ""

    monkeypatch.setattr(ssh, "_remote_exec", fake_exec)
    result = ssh._run_via_slurm(None, FakeSFTP(), ["true"], cwd=root,
                                stdout_path=root / "logs" / "o", stderr_path=root / "logs" / "e",
                                timeout=None, threads=None, run_id="sacct")
    assert result.exit_code == 4 and result.scheduler_job_id == "42"


def test_remote_slurm_exitcode_unavailable_is_terminal_failure(tmp_path, monkeypatch):
    ssh, root = _remote_slurm_executor(tmp_path, monkeypatch)

    def fake_exec(_client, command, **_kwargs):
        if command.startswith("sbatch"):
            return 0, "42"
        if command.startswith("squeue"):
            return 0, ""
        if command.startswith("cat "):
            return 1, ""
        if command.startswith("sacct"):
            return 0, "bad:x|||||\n"
        return 0, ""

    monkeypatch.setattr(ssh, "_remote_exec", fake_exec)
    result = ssh._run_via_slurm(None, FakeSFTP(), ["true"], cwd=root,
                                stdout_path=root / "logs" / "o", stderr_path=root / "logs" / "e",
                                timeout=None, threads=None, run_id="gone")
    assert result.exit_code is None and "exit code is unavailable" in result.error
    assert result.scheduler_job_id == "42"
