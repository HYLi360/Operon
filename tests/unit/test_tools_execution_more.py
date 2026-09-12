"""Uncovered recipe-tool and execution-backend paths.

Fills the gaps left by ``test_tools_edges``/``test_execution``: cache
identity edge cases, remote-only analysis preparation and shutdown
bookkeeping, the SSH staging/pull failure paths, Slurm accounting
fallbacks, and the environment-probe degradation contract.

Only process, filesystem-mirror and clock boundaries are faked; every
assertion is on observable behaviour (generated script text, files created
or removed, recorded provenance rows, raised errors).
"""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import signal
import stat as stat_module
import subprocess
import sys
import threading
from pathlib import Path, PurePath
from types import SimpleNamespace

import pytest
import yaml

from operon import execution, tools
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ExternalToolError, RemoteError, ValidationError
from operon.files import ingest_file
from operon.shutdown import ShutdownRequested
from operon.tools import run_analysis, run_analysis_for_file
from operon.utils import now_iso
from tests.unit.test_execution import (
    FakeSFTP,
    FakeSSHClient,
    _FakeChannel,
    _FakeStream,
    _HangingChannel,
    _HangingStream,
)
from tests.unit.test_tools_edges import _FakeExecutor, recipe, tool_spec


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def project_ns(root: Path) -> SimpleNamespace:
    """Minimal project namespace for pure helper calls."""
    return SimpleNamespace(
        root=root,
        tools_config_path=root / "tools.yaml",
        logs_root=root / "logs",
        analysis_root=root / "analysis",
    )


def file_record(file_id: str = "F1", **overrides) -> dict:
    record = {
        "file_id": file_id,
        "entity_type": "assembly",
        "entity_id": "A1",
        "file_role": "genome_fasta",
        "sha256": hashlib.sha256(file_id.encode()).hexdigest(),
    }
    record.update(overrides)
    return record


def insert_file(db: Database, file_id: str = "F1", **overrides) -> None:
    row = {
        "file_id": file_id,
        "entity_type": "assembly",
        "entity_id": "A1",
        "file_role": "genome_fasta",
        "format": "fasta",
        "compression": "none",
        "relative_path": f"{file_id}.fa",
        "size_bytes": 4,
        "sha256": hashlib.sha256(file_id.encode()).hexdigest(),
        "status": "OK",
    }
    row.update(overrides)
    db.insert_row("files", row)


def insert_job(db: Database, file_id: str = "F1", **overrides) -> dict:
    row = {
        "analysis_name": "fake_recipe",
        "entity_type": "assembly",
        "entity_id": "A1",
        "file_id": file_id,
        "tool": "faketool",
        "tool_version": "1.0",
        "parameter_set": "set",
        "parameter_sha256": "params",
        "input_sha256": "i" * 64,
        "database_identity": "db",
        "status": "completed",
        "started_at": now_iso(),
    }
    row.update(overrides)
    db.insert_row("analysis_jobs", row)
    return row


@pytest.fixture
def operon_project(tmp_path):
    """A real initialised project plus an open database."""
    root = tmp_path / "proj"
    root.mkdir()
    assert main(["--project", str(root), "init", str(root),
                 "--project-id", "PRJ_T5_001"]) == 0
    project = load_project(root)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def base_recipe(**overrides) -> dict:
    document = {
        "entity_type": "assembly",
        "file_role": "genome_fasta",
        "format": "fasta",
        "output_subdir": "fake",
        "output_suffix": ".out.tsv",
        "arguments": ["--out", "${output}"],
        "result_parser": "none",
    }
    document.update(overrides)
    return document


def write_tool_config(project, *, executable=None, recipe_document=None, tool=None) -> None:
    document = {
        "version": 1,
        "tools": {
            "faketool": {
                "executable": str(executable or sys.executable),
                "run_method": "",
                "version_args": ["-c", "print('faketool: 1.0.0')"],
                "version_pattern": r"faketool:\s*([^\s]+)",
                "recipes": {"fake_recipe": recipe_document or base_recipe()},
                **(tool or {}),
            },
        },
    }
    project.tools_config_path.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )


def ingest_assembly(project, db: Database, number: int) -> dict:
    organism_id = f"ORG_{number:06d}"
    sample_id = f"SMP_{number:06d}"
    assembly_id = f"ASM_{number:06d}"
    db.insert_row("organisms", {"organism_id": organism_id,
                                "scientific_name": f"Testus {number}"})
    db.insert_row("samples", {"sample_id": sample_id, "organism_id": organism_id})
    db.insert_row("assemblies", {"assembly_id": assembly_id, "sample_id": sample_id})
    source = project.root / f"genome_{number}.fa"
    source.write_text(f">ctg{number}\n" + "ACGT" * 200 + "\n", encoding="utf-8")
    return ingest_file(db, project, source, "assembly", assembly_id, "genome_fasta")


def _ssh_executor(project, remote_root: Path, **config):
    return execution.SSHExecutor(
        project,
        {"host": "fake.example.org", "remote_root": str(remote_root), **config},
        execution.SlurmConfig(poll_interval=0.01),
        client_factory=lambda _self: FakeSSHClient(),
    )


# ---------------------------------------------------------------------------
# execution: resource helpers
# ---------------------------------------------------------------------------


def test_child_cpu_seconds_degrades_without_resource_module(monkeypatch):
    monkeypatch.setattr(execution, "resource", None)
    assert execution._child_cpu_seconds() is None


def test_process_rss_reports_none_when_procfs_and_ps_are_unusable(monkeypatch):
    real_open = builtins.open

    def procfs_without_rss(path, *args, **kwargs):
        if str(path).startswith("/proc/"):
            return io.StringIO("Name:\tpython\nState:\tR\n")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", procfs_without_rss)
    # ps exits non-zero: nothing to report.
    monkeypatch.setattr(execution.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=1, stdout=""))
    assert execution._read_process_rss_mb(4242) is None
    # ps itself is unavailable: still no crash, no value.
    monkeypatch.setattr(execution.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(
        OSError("no ps binary")))
    assert execution._read_process_rss_mb(4242) is None


def test_sample_process_rss_stops_when_reading_is_unavailable(monkeypatch):
    monkeypatch.setattr(execution, "_read_process_rss_mb", lambda _pid: None)
    samples: list[float] = []
    execution._sample_process_rss(4242, samples, threading.Event())
    assert samples == []


def test_local_executor_probe_environment_degrades_on_failure(monkeypatch):
    def exploding_probe():
        raise OSError("cannot inspect this host")

    monkeypatch.setattr(execution, "local_environment", exploding_probe)
    assert execution.LocalExecutor().probe_environment() is None


def test_local_executor_run_without_resource_samples(tmp_path, monkeypatch):
    monkeypatch.setattr(execution, "_sample_process_rss", lambda *_a: None)
    monkeypatch.setattr(execution, "_child_cpu_seconds", lambda: None)
    result = execution.LocalExecutor().run(
        [sys.executable, "-c", "print('payload')"], cwd=tmp_path,
        stdout_path=tmp_path / "out.log", stderr_path=tmp_path / "err.log",
    )
    assert result.exit_code == 0
    assert result.resources == {}
    assert (tmp_path / "out.log").read_text(encoding="utf-8").strip() == "payload"


# ---------------------------------------------------------------------------
# execution: slurm accounting
# ---------------------------------------------------------------------------


def test_sacct_accounting_skips_blank_lines_and_applies_resources():
    accounting = execution._parse_sacct_accounting(
        "\n   \n0:0|1.5G|512M|01:02:03|00:10:00|\n0:0|64K|32K|00:00:01|00:00:01|\n"
    )
    assert accounting["exit_code"] == 0
    assert accounting["max_rss_mb"] == 1536.0
    assert accounting["avg_rss_mb"] == 512.0
    assert accounting["elapsed_seconds"] == 3723.0
    assert accounting["cpu_seconds"] == 600.0

    result = execution.ExecResult(exit_code=0)
    execution._apply_slurm_accounting(result, accounting)
    assert result.resources == {
        "max_rss_mb": 1536.0, "avg_rss_mb": 512.0, "cpu_seconds": 600.0,
    }
    assert result.details == {"slurm_elapsed_seconds": 3723.0}

    untouched = execution.ExecResult(exit_code=0)
    execution._apply_slurm_accounting(untouched, {"exit_code": 0})
    assert untouched.resources == {}
    assert untouched.details == {}


def test_read_slurm_accounting_uses_sacct_metrics_and_survives_sacct_failure(
        tmp_path, monkeypatch):
    exitcode = tmp_path / "job.exitcode"
    exitcode.write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(execution.shutil, "which",
                        lambda name: "sacct" if name == "sacct" else None)
    monkeypatch.setattr(execution.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        stdout="0:0|1G|512M|00:00:30|00:00:10|\n"))
    result = execution._read_slurm_accounting(exitcode, "77", retries=1)
    assert result.exit_code == 0
    assert result.resources["max_rss_mb"] == 1024.0
    assert result.resources["avg_rss_mb"] == 512.0
    assert result.details["slurm_elapsed_seconds"] == 30.0

    monkeypatch.setattr(execution.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(
        OSError("sacct disappeared")))
    degraded = execution._read_slurm_accounting(exitcode, "77", retries=1)
    assert degraded.exit_code == 0
    assert degraded.resources == {}
    assert degraded.details == {}


def test_slurm_executor_timeout_and_completion_without_probe_document(tmp_path, monkeypatch):
    executor = execution.SlurmExecutor(project_ns(tmp_path),
                                       execution.SlurmConfig(poll_interval=0.01))
    out, err = tmp_path / "out.log", tmp_path / "err.log"
    monkeypatch.setattr(execution.shutil, "which",
                        lambda name: name if name in {"sbatch", "squeue"} else None)
    monkeypatch.setattr(execution, "_submit_slurm_job", lambda *_a: "42")
    monkeypatch.setattr(execution, "_scancel_slurm_job", lambda _job: None)
    monkeypatch.setattr(execution.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(execution, "_squeue_job_gone", lambda *_a: False)

    times = iter([0, 2, 2])
    monkeypatch.setattr(execution.time, "monotonic", lambda: next(times, 2))
    timed_out = executor.run(["tool"], cwd=None, stdout_path=out, stderr_path=err,
                             timeout=1, run_id="noprobe")
    assert timed_out.exit_code is None
    assert "timeout after 1s" in timed_out.error
    assert "environment" not in timed_out.details
    assert not (tmp_path / "noprobe.env").exists()

    def finish(_squeue, _job_id):
        (tmp_path / "done.exitcode").write_text("0\n", encoding="utf-8")
        return True

    monkeypatch.setattr(execution, "_squeue_job_gone", finish)
    completed = executor.run(["tool", "--flag"], cwd=None, stdout_path=out, stderr_path=err,
                             run_id="done")
    assert completed.exit_code == 0
    assert completed.scheduler_job_id == "42"
    assert completed.details["backend"] == "slurm"
    assert completed.details["script"] == str(tmp_path / "done.sbatch")
    assert "environment" not in completed.details
    script = (tmp_path / "done.sbatch").read_text(encoding="utf-8")
    assert "#SBATCH --job-name=operon_done" in script
    assert "tool --flag" in script
    assert not (tmp_path / "done.env").exists()


# ---------------------------------------------------------------------------
# execution: probe files
# ---------------------------------------------------------------------------


def test_read_probe_environment_degrades_when_unreadable_and_undeletable(tmp_path):
    directory = tmp_path / "probe-dir"
    directory.mkdir()
    assert execution._read_probe_environment(directory) is None
    # The unlink in the cleanup path must not delete or fail on the directory.
    assert directory.is_dir()


def test_read_remote_probe_environment_accepts_text_payload_and_survives_cleanup_error():
    removed: list[str] = []

    class Handle:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return "os=Linux\narch=x86_64\n"

    class SFTP:
        def open(self, _path, _mode):
            return Handle()

        def remove(self, path):
            removed.append(path)
            raise OSError("permission denied")

    environment = execution._read_remote_probe_environment(SFTP(), "/tmp/operon-probe")
    assert environment == {"os": "Linux", "arch": "x86_64"}
    assert removed == ["/tmp/operon-probe"]


def test_ssh_probe_environment_returns_none_when_the_connection_fails(tmp_path):
    class BrokenClient:
        def exec_command(self, *_args, **_kwargs):
            raise OSError("connection refused")

    ssh = execution.SSHExecutor(
        project_ns(tmp_path), {"host": "unreachable.example.org"},
        execution.SlurmConfig(), client_factory=lambda _self: BrokenClient(),
    )
    assert ssh.probe_environment() is None


def test_ssh_close_without_a_connection_is_a_noop(tmp_path):
    opened: list[int] = []

    def factory(_self):
        opened.append(1)
        return FakeSSHClient()

    ssh = execution.SSHExecutor(project_ns(tmp_path), {"host": "host.example.org"},
                                execution.SlurmConfig(), client_factory=factory)
    ssh.close()
    assert ssh._client is None
    assert opened == []


# ---------------------------------------------------------------------------
# execution: SSH staging and retrieval
# ---------------------------------------------------------------------------


def test_stage_inputs_stages_a_directory_tree(tmp_path):
    root, remote_root = tmp_path / "project", tmp_path / "remote"
    local = root / "tree"
    (local / "sub").mkdir(parents=True)
    remote_root.mkdir()
    (local / "a.txt").write_text("a", encoding="utf-8")
    (local / "sub" / "b.txt").write_text("b", encoding="utf-8")
    (local / "link").symlink_to("a.txt")
    client = FakeSSHClient()
    ssh = execution.SSHExecutor(
        project_ns(root), {"host": "host", "remote_root": str(remote_root)},
        execution.SlurmConfig(), client_factory=lambda _self: client,
    )
    ssh._stage_inputs(client, client.sftp, [local])
    assert (remote_root / "tree" / "sub" / "b.txt").read_text(encoding="utf-8") == "b"
    assert (remote_root / "tree" / "link").is_symlink()


def test_reset_outputs_rejects_the_project_root_itself(tmp_path):
    root, remote_root = tmp_path / "project", tmp_path / "remote"
    root.mkdir()
    remote_root.mkdir()
    client = FakeSSHClient()
    ssh = execution.SSHExecutor(
        project_ns(root), {"host": "host", "remote_root": str(remote_root)},
        execution.SlurmConfig(), client_factory=lambda _self: client,
    )
    with pytest.raises(ValidationError, match="escapes remote_root"):
        ssh._reset_outputs(client.sftp, [root])


def test_stage_directory_rejects_entries_that_appear_after_hashing(tmp_path, monkeypatch):
    root, remote_root = tmp_path / "project", tmp_path / "remote"
    local = root / "tree"
    local.mkdir(parents=True)
    remote_root.mkdir()
    (local / "a.txt").write_text("a", encoding="utf-8")
    client = FakeSSHClient()
    ssh = execution.SSHExecutor(
        project_ns(root), {"host": "host", "remote_root": str(remote_root)},
        execution.SlurmConfig(), client_factory=lambda _self: client,
    )
    real_entries = execution.iter_directory_entries

    def entries_with_late_fifo(path):
        pipe = Path(path) / "pipe"
        if Path(path) == local and not pipe.exists():
            os.mkfifo(pipe)  # a racing writer adds an unstorable entry
        return real_entries(path)

    monkeypatch.setattr(execution, "iter_directory_entries", entries_with_late_fifo)
    with pytest.raises(RemoteError, match="unsupported directory input entry"):
        ssh._stage_directory(client, client.sftp, local, str(remote_root / "tree"))
    assert not (remote_root / "tree").exists()
    assert not list(remote_root.glob("*.operon-tmp-*"))


def test_stage_directory_verification_and_publish_failures_clean_up(tmp_path, monkeypatch):
    root, remote_root = tmp_path / "project", tmp_path / "remote"
    local = root / "tree"
    local.mkdir(parents=True)
    remote_root.mkdir()
    (local / "a.txt").write_text("a", encoding="utf-8")
    client = FakeSSHClient()
    ssh = execution.SSHExecutor(
        project_ns(root), {"host": "host", "remote_root": str(remote_root)},
        execution.SlurmConfig(), client_factory=lambda _self: client,
    )

    monkeypatch.setattr("operon.remotes._remote_directory_identity",
                        lambda *_a, **_k: ("0" * 64, {}))
    with pytest.raises(RemoteError, match="staged directory verification failed"):
        ssh._stage_directory(client, client.sftp, local, str(remote_root / "mismatch"))
    assert not (remote_root / "mismatch").exists()

    monkeypatch.undo()
    monkeypatch.setattr("operon.remotes._publish_remote",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("publish failed")))
    with pytest.raises(OSError, match="publish failed"):
        ssh._stage_directory(client, client.sftp, local, str(remote_root / "unpublished"))
    assert not (remote_root / "unpublished").exists()
    assert not list(remote_root.glob("*.operon-tmp-*"))


def test_pull_outputs_removes_mismatched_directory_and_temporary_tree(tmp_path, monkeypatch):
    root, remote_root = tmp_path / "project", tmp_path / "remote"
    root.mkdir()
    remote_root.mkdir()
    client = FakeSSHClient()
    ssh = execution.SSHExecutor(
        project_ns(root), {"host": "host", "remote_root": str(remote_root)},
        execution.SlurmConfig(), client_factory=lambda _self: client,
    )

    remote_dir = remote_root / "outdir"
    remote_dir.mkdir()
    (remote_dir / "f.txt").write_text("remote-bytes", encoding="utf-8")
    monkeypatch.setattr(execution, "sha256_path", lambda _path: "local-digest")
    with pytest.raises(RemoteError, match="checksum mismatch"):
        ssh._pull_outputs(client, client.sftp, [root / "outdir"])
    assert not (root / "outdir").exists()

    monkeypatch.undo()
    monkeypatch.setattr(ssh, "_pull_directory_into",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("sftp broken")))
    with pytest.raises(OSError, match="sftp broken"):
        ssh._pull_directory(client.sftp, str(remote_dir), root / "other")
    assert not (root / "other").exists()
    assert not list(root.glob(".other.operon-tmp-*"))


def test_pull_directory_into_rejects_unknown_remote_entry_types(tmp_path):
    class Entry:
        filename = "pipe"
        st_mode = stat_module.S_IFIFO | 0o644

    class SFTP:
        def listdir_attr(self, _remote):
            return [Entry()]

        def lstat(self, _remote):
            return Entry()

    ssh = execution.SSHExecutor(project_ns(tmp_path), {"host": "host"},
                                execution.SlurmConfig())
    with pytest.raises(RemoteError, match="unsupported remote output entry type"):
        ssh._pull_directory_into(SFTP(), "/remote/out", tmp_path / "local")


# ---------------------------------------------------------------------------
# execution: direct SSH runs
# ---------------------------------------------------------------------------


class _DrainingChannel:
    """Reports exit before the trailing stdout/stderr drain has run."""

    def __init__(self):
        self._stdout = [b"tail-out\n"]
        self._stderr = [b"tail-err\n"]
        self._draining = False
        self.closed = False

    def recv_ready(self):
        return self._draining and bool(self._stdout)

    def recv(self, _size):
        return self._stdout.pop(0) if self._stdout else b""

    def recv_stderr_ready(self):
        return self._draining and bool(self._stderr)

    def recv_stderr(self, _size):
        return self._stderr.pop(0) if self._stderr else b""

    def exit_status_ready(self):
        self._draining = True
        return True

    def recv_exit_status(self):
        return 0

    def close(self):
        self.closed = True


class _ScriptedClient:
    """SSH client whose payload channel is supplied by the test."""

    def __init__(self, channel, aux_returncode: int = 0):
        self.channel = channel
        self.aux_returncode = aux_returncode
        self.commands: list[str] = []

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        if command.startswith("setsid "):
            stream = _HangingStream(self.channel)
            return None, stream, stream
        proc = subprocess.CompletedProcess(command, self.aux_returncode, b"", b"")
        channel = _FakeChannel(proc)
        return None, _FakeStream(proc.stdout, channel), _FakeStream(proc.stderr, channel)


def test_rewrite_remote_path_rejects_a_relative_remote_root():
    # A relative remote_root cannot host a mapped absolute project path: the
    # mapping would silently leave the remote namespace.
    with pytest.raises(ValidationError, match="escapes remote_root"):
        execution.rewrite_remote_path("/project/file", Path("/project"), ".")


def test_run_direct_without_probe_or_remote_cwd(tmp_path):
    client = FakeSSHClient()
    ssh = execution.SSHExecutor(project_ns(tmp_path), {"host": "host"},
                                execution.SlurmConfig(),
                                client_factory=lambda _self: client)
    stdout_path, stderr_path = tmp_path / "out.log", tmp_path / "err.log"
    result = ssh._run_direct(client, ["bash", "-c", "echo payload; pwd"], cwd=None,
                             stdout_path=stdout_path, stderr_path=stderr_path,
                             timeout=None, run_id="direct")
    assert result.exit_code == 0
    payload_line, working_directory = stdout_path.read_text(encoding="utf-8").split()
    assert payload_line == "payload"
    # Without cwd and without a remote root no "cd" is injected, so the payload
    # runs in the login shell's own directory.
    assert working_directory == os.getcwd()
    assert result.details == {"backend": "ssh", "scheduler": "none", "host": "host"}


def test_run_direct_drains_bytes_buffered_after_exit(tmp_path):
    channel = _DrainingChannel()
    client = _ScriptedClient(channel)
    ssh = execution.SSHExecutor(project_ns(tmp_path), {"host": "host"},
                                execution.SlurmConfig(),
                                client_factory=lambda _self: client)
    stdout_path, stderr_path = tmp_path / "out.log", tmp_path / "err.log"
    result = ssh._run_direct(client, ["tool"], cwd=None, stdout_path=stdout_path,
                             stderr_path=stderr_path, timeout=None, run_id="drain")
    assert result.exit_code == 0
    assert stdout_path.read_text(encoding="utf-8") == "tail-out\n"
    assert stderr_path.read_text(encoding="utf-8") == "tail-err\n"


def test_run_direct_timeout_reports_when_termination_finds_no_process(tmp_path, monkeypatch):
    client = _ScriptedClient(_HangingChannel(), aux_returncode=2)
    ssh = execution.SSHExecutor(project_ns(tmp_path), {"host": "host"},
                                execution.SlurmConfig(),
                                client_factory=lambda _self: client)
    monkeypatch.setattr(execution.time, "sleep", lambda *_a: None)
    times = iter([0, 2, 2])
    monkeypatch.setattr(execution.time, "monotonic", lambda: next(times, 2))
    result = ssh._run_direct(client, ["sleep", "30"], cwd=None,
                             stdout_path=tmp_path / "out.log",
                             stderr_path=tmp_path / "err.log",
                             timeout=1, run_id="hang")
    assert result.exit_code is None
    assert "remote process may still be running" in result.error
    assert "termination command exited 2" in result.error
    assert result.details["termination_signaled"] is False
    assert result.resources == {}


def test_run_direct_interrupt_without_signal_confirmation_still_closes_channel(
        tmp_path, monkeypatch):
    channel = _HangingChannel()
    client = _ScriptedClient(channel, aux_returncode=2)
    ssh = execution.SSHExecutor(project_ns(tmp_path), {"host": "host"},
                                execution.SlurmConfig(),
                                client_factory=lambda _self: client)

    def interrupting_sleep(_seconds):
        raise ShutdownRequested(signal.SIGINT)

    monkeypatch.setattr(execution.time, "sleep", interrupting_sleep)
    with pytest.raises(ShutdownRequested):
        ssh._run_direct(client, ["sleep", "30"], cwd=None,
                        stdout_path=tmp_path / "out.log",
                        stderr_path=tmp_path / "err.log",
                        timeout=None, run_id="interrupt")
    assert channel.closed
    assert any("kill -TERM" in command for command in client.commands)


def test_read_remote_stats_degrades_when_the_exec_channel_fails(tmp_path):
    class BrokenClient:
        def exec_command(self, *_args, **_kwargs):
            raise OSError("ssh transport closed")

    ssh = execution.SSHExecutor(project_ns(tmp_path), {"host": "host"},
                                execution.SlurmConfig())
    assert ssh._read_remote_stats(BrokenClient(), "/tmp/operon.stats") == {}


# ---------------------------------------------------------------------------
# execution: remote slurm
# ---------------------------------------------------------------------------


def _remote_slurm_executor(tmp_path, monkeypatch):
    root, remote_root = tmp_path / "project", tmp_path / "remote"
    (root / "logs").mkdir(parents=True)
    remote_root.mkdir()
    ssh = execution.SSHExecutor(
        project_ns(root),
        {"host": "host", "remote_root": str(remote_root), "scheduler": "slurm"},
        execution.SlurmConfig(poll_interval=0.01),
    )
    monkeypatch.setattr(execution.time, "sleep", lambda *_a: None)
    return ssh, root


def test_remote_slurm_script_upload_failure_cleans_up(tmp_path, monkeypatch):
    ssh, root = _remote_slurm_executor(tmp_path, monkeypatch)

    class FailingSFTP(FakeSFTP):
        def open(self, _path, _mode="r"):
            raise OSError("remote disk full")

    with pytest.raises(OSError, match="remote disk full"):
        ssh._run_via_slurm(
            None, FailingSFTP(), ["tool"], cwd=root,
            stdout_path=root / "logs" / "o.log", stderr_path=root / "logs" / "e.log",
            timeout=None, threads=None, run_id="upload",
        )
    assert not list((root / "logs").glob("*.operon-tmp-*"))


def test_remote_slurm_completion_paths_use_queue_sacct_and_local_fallbacks(
        tmp_path, monkeypatch):
    ssh, root = _remote_slurm_executor(tmp_path, monkeypatch)
    sftp = FakeSFTP()
    stdout_path, stderr_path = root / "logs" / "o.log", root / "logs" / "e.log"

    def completed(_client, command, **_kwargs):
        if command.startswith("sbatch"):
            return 0, "99"
        if command.startswith("squeue"):
            return 1, "Invalid job id specified"
        if command.startswith("cat "):
            return 0, "not-a-number"  # exit-code file unreadable while it settles
        if command.startswith("sacct"):
            return 0, "0:0|1G|512M|00:00:05|00:00:02|"
        return 0, ""

    monkeypatch.setattr(ssh, "_remote_exec", completed)
    result = ssh._run_via_slurm(None, sftp, ["tool"], cwd=root,
                                stdout_path=stdout_path, stderr_path=stderr_path,
                                timeout=None, threads=None, run_id="finished")
    assert result.exit_code == 0
    assert result.scheduler_job_id == "99"
    assert result.resources["max_rss_mb"] == 1024.0
    assert result.resources["cpu_seconds"] == 2.0
    assert result.details["slurm_elapsed_seconds"] == 5.0

    def sacct_unavailable(_client, command, **_kwargs):
        if command.startswith("sbatch"):
            return 0, "100"
        if command.startswith("squeue"):
            return 0, ""
        if command.startswith("cat "):
            return 1, ""
        raise OSError("sacct missing on the login node")

    monkeypatch.setattr(ssh, "_remote_exec", sacct_unavailable)
    missing = ssh._run_via_slurm(None, sftp, ["tool"], cwd=root,
                                 stdout_path=stdout_path, stderr_path=stderr_path,
                                 timeout=None, threads=None, run_id="noexit")
    assert missing.exit_code is None
    assert "exit code is unavailable" in missing.error
    assert missing.scheduler_job_id == "100"


def test_cancel_remote_slurm_job_reports_rejections_and_transport_failures(tmp_path, monkeypatch):
    ssh, _root = _remote_slurm_executor(tmp_path, monkeypatch)

    monkeypatch.setattr(ssh, "_remote_exec", lambda *_a, **_k: (1, "not permitted"))
    assert ssh._cancel_remote_slurm_job(None, "7") == (False, "not permitted")
    monkeypatch.setattr(ssh, "_remote_exec", lambda *_a, **_k: (2, ""))
    assert ssh._cancel_remote_slurm_job(None, "7") == (False, "scancel exited 2")
    monkeypatch.setattr(ssh, "_remote_exec",
                        lambda *_a, **_k: (_ for _ in ()).throw(RemoteError("link down")))
    signaled, error = ssh._cancel_remote_slurm_job(None, "7")
    assert signaled is False
    assert "RemoteError" in error and "link down" in error


# ---------------------------------------------------------------------------
# tools: recipe identity and parser edges
# ---------------------------------------------------------------------------


def test_version_detection_falls_back_when_pattern_does_not_match(monkeypatch):
    tools._VERSION_CACHE.clear()
    monkeypatch.setattr(tools.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        stdout="tool build 7.5\nsecond line\n", stderr=""))
    assert tools.detect_tool_version(tool_spec(version_pattern=r"nomatch=(\S+)"), {}) == "7.5"


def test_command_step_provenance_validation_and_dry_run_variants(monkeypatch):
    commands = [
        tools.RecipeCommand(["owner", "run"], ["--version"], r"owner:\s*(\S+)"),
        tools.RecipeCommand(["helper", "run"], ["--version"], r"helper:\s*(\S+)"),
    ]
    analysis = recipe(commands=commands)
    tool = tool_spec(executable="owner", run_method="")
    rendered = [["owner", "run"], ["helper", "run"]]

    with pytest.raises(ValidationError, match="rendered command count does not match"):
        tools.command_step_provenance(analysis, tool, {}, rendered[:1], "1.0", "owner: 1.0")

    details = tools.command_step_provenance(
        analysis, tool, {}, rendered, "1.0", "owner: 1.0",
        executor=_FakeExecutor("ssh"), dry_run=True,
    )
    assert details[0]["tool_version"] == "not probed (backend=ssh)"
    assert details[0]["tool_version_raw"] == ""
    assert details[1]["tool_version"] == "not probed (backend=ssh)"

    monkeypatch.setattr(tools, "_detect_version_record",
                        lambda *_a, **_k: (_ for _ in ()).throw(ExternalToolError("probe failed")))
    degraded = tools.command_step_provenance(
        analysis, tool, {}, rendered, "1.0", "owner: 1.0", dry_run=True,
    )
    assert degraded[0]["tool_version"] == "unavailable (probe failed)"
    assert degraded[0]["tool_version_raw"] == "probe failed"
    assert degraded[1]["tool_version"] == "unavailable (probe failed)"

    # A real (non-dry) run must not hide a failing chain-step probe.
    with pytest.raises(ExternalToolError, match="probe failed"):
        tools.command_step_provenance(analysis, tool, {}, rendered, "1.0", "owner: 1.0")


def test_candidate_files_prefix_recipe_and_entity_filters(tmp_path):
    db = Database(tmp_path / "meta.sqlite")
    insert_file(db, "F1", file_role="subfamily_alignment:SF01", entity_type="annotation",
                entity_id="ANN_000001")
    insert_file(db, "F2", file_role="subfamily_alignment:SF02", entity_type="annotation",
                entity_id="ANN_000002")
    insert_file(db, "F3", file_role="genome_fasta")
    insert_file(db, "F4", file_role="genome_fasta", format="genbank")

    prefixed = recipe(entity_type="", file_role="", file_role_prefix="subfamily_alignment:",
                      fmt="fasta")
    assert [row["file_id"] for row in tools.candidate_files(db, prefixed)] == ["F1", "F2"]
    assert [row["file_id"] for row in tools.candidate_files(
        db, prefixed, entity_type="annotation")] == ["F1", "F2"]
    assert tools.candidate_files(db, prefixed, entity_type="assembly") == []

    exact = recipe(file_role="genome_fasta", fmt="fasta")
    assert [row["file_id"] for row in tools.candidate_files(db, exact)] == ["F3"]
    assert [row["file_id"] for row in tools.candidate_files(db, exact, entity_id="A1")] == ["F3"]
    assert tools.candidate_files(db, exact, entity_id="MISSING") == []
    db.close()


def test_directory_fingerprint_marks_unreadable_entries(tmp_path, monkeypatch):
    directory = tmp_path / "database"
    directory.mkdir()
    (directory / "readable").write_text("x", encoding="utf-8")
    (directory / "locked").write_text("y", encoding="utf-8")
    readable = tools._directory_fingerprint(directory)

    real_stat = Path.stat

    def failing_stat(self, *args, **kwargs):
        if self.name == "locked":
            raise OSError("permission denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_stat)
    unreadable = tools._directory_fingerprint(directory)
    assert unreadable != readable
    assert len(unreadable) == 64
    assert tools._directory_fingerprint(directory) == unreadable


def test_database_identity_treats_special_paths_as_unreadable(tmp_path):
    os.mkfifo(tmp_path / "database.fifo")
    project = project_ns(tmp_path)
    tools._DATABASE_IDENTITY_CACHE.clear()
    identity = tools.database_identity(project, recipe(database="database.fifo"))
    assert identity == tools.database_identity(project, recipe(database="database.fifo"))
    assert identity != tools.database_identity(project, recipe(database="missing"))


def test_cached_environment_document_missing_corrupt_and_legacy_rows(tmp_path):
    db = Database(tmp_path / "meta.sqlite")
    assert tools._cached_environment_document(db, None) is None
    assert tools._cached_environment_document(db, "") is None
    assert tools._cached_environment_document(db, "no-such-environment") is None
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO execution_environments(environment_id, document, created_at) "
            "VALUES (?, ?, ?)", ("corrupt", "{not-json", now_iso()))
        conn.execute(
            "INSERT INTO execution_environments(environment_id, document, created_at) "
            "VALUES (?, ?, ?)", ("legacy", json.dumps(["linux", "x86_64"]), now_iso()))
    assert tools._cached_environment_document(db, "corrupt") is None
    assert tools._cached_environment_document(db, "legacy") is None
    recorded = db.record_environment({"os": "Linux", "arch": "x86_64"})
    assert tools._cached_environment_document(db, recorded) == {"os": "Linux", "arch": "x86_64"}
    db.close()


def test_current_environment_document_prefers_the_executor_probe(tmp_path):
    executor = _FakeExecutor("ssh", {"os": "Linux"})
    assert tools._current_environment_document(executor, ["tool"], tmp_path) == {"os": "Linux"}

    class NoProbe:
        name = "ssh"

    assert tools._current_environment_document(NoProbe(), ["tool"], tmp_path) is None

    class EmptyProbe(NoProbe):
        def probe_environment(self):
            return {}

    assert tools._current_environment_document(EmptyProbe(), ["tool"], tmp_path) is None


def test_find_verified_adoptee_requires_a_present_recorded_output(tmp_path):
    project = project_ns(tmp_path)
    db = Database(tmp_path / "meta.sqlite")
    insert_file(db, "F1")
    insert_job(db, "F1", parameter_sha256="p1", input_sha256="i" * 64,
               output_relative_path=None, output_sha256=None)
    assert tools._find_verified_adoptee(db, project, "fake_recipe", "F1", "i" * 64) is None

    insert_job(db, "F1", parameter_sha256="p2", input_sha256="i" * 64,
               output_relative_path="analysis/out.tsv", output_sha256="0" * 64)
    assert tools._find_verified_adoptee(db, project, "fake_recipe", "F1", "i" * 64) is None

    output = tmp_path / "analysis" / "out.tsv"
    output.parent.mkdir(parents=True)
    output.write_text("real output", encoding="utf-8")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    insert_job(db, "F1", parameter_sha256="p3", input_sha256="i" * 64,
               output_relative_path="analysis/out.tsv", output_sha256=digest)
    adoptee, path = tools._find_verified_adoptee(db, project, "fake_recipe", "F1", "i" * 64)
    assert path == output and adoptee["parameter_sha256"] == "p3"

    output.write_text("tampered", encoding="utf-8")
    assert tools._find_verified_adoptee(db, project, "fake_recipe", "F1", "i" * 64) is None
    assert tools._find_verified_adoptee(db, project, "fake_recipe", "F1", "other-sha") is None
    db.close()


def test_require_artifact_kind_rejects_the_wrong_artifact_type(tmp_path):
    directory = tmp_path / "dir"
    directory.mkdir()
    regular = tmp_path / "file"
    regular.write_text("x", encoding="utf-8")
    tools._require_artifact_kind(regular, "file", "input")
    tools._require_artifact_kind(directory, "directory", "output")
    with pytest.raises(ExternalToolError, match="input must be a regular file"):
        tools._require_artifact_kind(directory, "file", "input")
    with pytest.raises(ExternalToolError, match="output must be a directory"):
        tools._require_artifact_kind(regular, "directory", "output")


def test_render_output_name_rejects_unsupported_placeholders(tmp_path):
    with pytest.raises(ValidationError, match="unsupported placeholder"):
        tools._render_output_name(
            recipe(output_name_template="${file_id}.${unknown}.tsv"),
            file_record(), tmp_path / "in.fna",
        )


def test_parse_and_store_results_records_metrics_without_evalue(tmp_path):
    db = Database(tmp_path / "meta.sqlite")
    insert_file(db, "F1")
    insert_job(db, "F1")
    hits_path = tmp_path / "hits.tsv"
    hits_path.write_text("q1\ts1\t50\nq1\ts2\t40\nq2\ts1\t30\n", encoding="utf-8")
    analysis = recipe(result_parser="blast_tabular", raw={
        "result_columns": ["qseqid", "sseqid", "score"], "hit_metric_columns": ["score"],
    })
    counts = tools.parse_and_store_results(
        db, project_ns(tmp_path), analysis, tool_spec(), "1.0",
        file_record("F1"), 1, hits_path, "output-sha",
    )
    # hit pairs, distinct queries, queries with a rank-1 hit, metrics, alignments
    assert counts == (3, 2, 2, 3, 3)
    stored = [row["metric_name"] for row in db.query(
        "SELECT metric_name FROM analysis_results WHERE job_id=1")]
    assert sorted(stored) == ["hit_count", "query_count", "query_with_hit_count"]
    db.close()


def test_blast_parser_skips_empty_metric_values(tmp_path):
    blast = tmp_path / "blast.tsv"
    blast.write_text("q1\ts1\t\tnote\nq1\ts2\t12.5\tother\n", encoding="utf-8")
    analysis = recipe(result_parser="blast_tabular", raw={
        "result_columns": ["qseqid", "sseqid", "score", "note"],
        "hit_metric_columns": ["score"],
    })
    hits, alignments = tools.parse_hits(blast, analysis)
    assert [(hit["subject_id"], hit["metric_value"]) for hit in hits] == [("s2", "12.5")]
    assert [alignment["subject_id"] for alignment in alignments] == ["s1", "s2"]


def test_hmmer_swap_defaults_to_hmmscan_for_unrecognized_headers(tmp_path):
    tblout = tmp_path / "hmmer.tblout"
    tblout.write_text(
        "# a header that names no program\n"
        "# another comment line\n"
        "PF00001.28 x query1 x 1e-5 20\n",
        encoding="utf-8",
    )
    hits, _alignments = tools.parse_hits(tblout, recipe(result_parser="hmmer_tblout"))
    # Without a recognizable program the hmmscan orientation is kept:
    # field 3 stays the query and field 1 the profile target.
    assert (hits[0]["query_id"], hits[0]["subject_id"]) == ("query1", "PF00001.28")

    # Header-only output (an empty or truncated result) keeps the same default.
    headers_only = tmp_path / "headers-only.tblout"
    headers_only.write_text("# comment one\n# comment two\n", encoding="utf-8")
    assert tools.parse_hits(headers_only, recipe(result_parser="hmmer_tblout")) == ([], [])


def test_hmmer_domtblout_parser_degrades_on_unparsable_numbers(tmp_path):
    domtblout = tmp_path / "hmmer.domtblout"
    domtblout.write_text(
        "PF00001.28 - 144 query1 - 350 1.2e-30 105.5 0.0 1 2 3.4e-33 bad worse 0.0 "
        "1 120 10 130 10 132 0.95 kinase domain\n",
        encoding="utf-8",
    )
    hits, alignments = tools.parse_hits(domtblout, recipe(result_parser="hmmer_domtblout"))
    assert alignments[0]["evalue"] is None
    assert alignments[0]["bitscore"] is None
    assert [hit["metric_numeric"] for hit in hits] == [None, None]
    assert [hit["metric_value"] for hit in hits] == ["bad", "worse"]


def test_rpsbproc_parser_accepts_output_without_enddata(tmp_path):
    output = tmp_path / "rpsbproc.out"
    output.write_text(
        "DATA\n"
        "SESSION\t1\tblastp\t2.16.0+\tcdd/Cdd\tBLOSUM62\t0.001\n"
        "QUERY\tQuery_1\tPeptide\t10\tdefinition one\n"
        "DOMAINS\n"
        "1\tQuery_1\tSpecific\t381460\t5\t40\t1e-5\t50\tcd1\tname\t-\t-\n"
        "ENDDOMAINS\n"
        "ENDQUERY\tQuery_1\n"
        "ENDSESSION\t1\n",
        encoding="utf-8",
    )
    hits, alignments = tools.parse_hits(output, recipe(result_parser="rpsbproc_tabular"))
    assert len(alignments) == 1
    assert alignments[0]["query_id"] == "definition one"
    assert [hit["metric_name"] for hit in hits] == ["evalue", "bitscore"]


# ---------------------------------------------------------------------------
# tools: analysis orchestration
# ---------------------------------------------------------------------------


def test_run_analysis_reports_missing_candidates_and_honours_limit(operon_project, capsys):
    project, db = operon_project
    tools._VERSION_CACHE.clear()
    write_tool_config(project)
    assert run_analysis(project, db, "fake_recipe") == []
    message = capsys.readouterr().out
    assert "no candidate files for fake_recipe" in message
    assert "file_role=genome_fasta" in message
    assert "format=fasta" in message

    write_tool_config(project, recipe_document=base_recipe(
        file_role="", file_role_prefix="subfamily_alignment:"))
    assert run_analysis(project, db, "fake_recipe") == []
    assert "file_role_prefix=subfamily_alignment:" in capsys.readouterr().out

    write_tool_config(project)
    ingest_assembly(project, db, 1)
    ingest_assembly(project, db, 2)
    results = run_analysis(project, db, "fake_recipe", dry_run=True, limit=1)
    assert len(results) == 1
    assert results[0]["status"] == "planned"
    assert db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"] == 0


def test_run_analysis_dry_run_reports_unavailable_version_and_fails_without_it(
        operon_project, capsys):
    project, db = operon_project
    ingest_assembly(project, db, 1)
    write_tool_config(project, executable="/nonexistent/operon-t5-tool")
    tools._VERSION_CACHE.clear()

    planned = run_analysis(project, db, "fake_recipe", dry_run=True)
    assert planned[0]["status"] == "planned"
    assert planned[0]["tool_version"].startswith("unavailable (")
    assert "cannot launch" in planned[0]["tool_version"]

    failed = run_analysis(project, db, "fake_recipe")
    assert failed[0]["status"] == "error"
    assert "cannot launch" in failed[0]["error"]
    # Version detection fails before any job row is opened.
    assert db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"] == 0


def test_run_analysis_for_file_owns_a_local_executor_for_dry_runs(operon_project):
    project, db = operon_project
    write_tool_config(project)
    tools._VERSION_CACHE.clear()
    record = ingest_assembly(project, db, 1)
    analysis = tools.get_recipe(project, "fake_recipe")
    tool = tools.get_tool(project, "faketool")
    result = run_analysis_for_file(
        project, db, analysis, tool, tools.load_tools_config(project), record,
        dry_run=True, backend="local",
    )
    assert result["status"] == "planned"
    assert result["cached"] is False
    assert result["output"].endswith(".out.tsv")
    assert result["tool_version"] == "1.0.0"


def test_run_analysis_for_file_owns_and_closes_a_remote_executor(operon_project):
    project, db = operon_project
    write_tool_config(project)
    tools._VERSION_CACHE.clear()
    record = ingest_assembly(project, db, 1)
    document = yaml.safe_load(project.config_path.read_text(encoding="utf-8"))
    document["execution"] = {"backend": "ssh", "ssh": {"host": "hpc.example.org"}}
    project.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    project = load_project(project.root)
    analysis = tools.get_recipe(project, "fake_recipe")
    tool = tools.get_tool(project, "faketool")

    result = run_analysis_for_file(
        project, db, analysis, tool, tools.load_tools_config(project), record,
        dry_run=True,
    )
    # The owned SSH executor is created without a connection for the dry run and
    # closed again afterwards.
    assert result["status"] == "planned"
    assert result["tool_version"].startswith("not probed (backend=ssh:")


def test_run_analysis_for_file_remote_only_dry_run_skips_verification(
        operon_project, tmp_path, monkeypatch):
    project, db = operon_project
    record = ingest_assembly(project, db, 1)
    local_status = db.query("SELECT status FROM files WHERE file_id=?",
                            (record["file_id"],))[0]["status"]
    (project.root / record["relative_path"]).unlink()  # only the remote copy remains
    storage = SimpleNamespace(
        host="cluster.example.org", user="hpcuser", port=2222, root="/shared/operon",
        key_file="/keys/id", known_hosts="/keys/known", host_key_sha256="sha256:abc",
        insecure_accept_unknown_host=False,
    )
    monkeypatch.setattr("operon.remotes.get_remote", lambda *_a, **_k: storage)
    executor = execution.SSHExecutor(
        project, {"storage_remote": "store"}, execution.SlurmConfig(),
    )
    analysis = recipe(tool_name="faketool", output_subdir="fake", output_suffix=".out.tsv",
                      arguments=["--out", "${output}"])
    result = run_analysis_for_file(
        project, db, analysis, tool_spec(name="faketool", executable="faketool"),
        {}, record, dry_run=True, executor=executor,
    )
    assert result["status"] == "planned"
    assert result["tool_version"] == "not probed (backend=ssh:hpcuser@cluster.example.org)"
    assert result["command"].startswith("faketool --out ")
    assert result["command"].endswith(".out.tsv")
    # A dry run must not promote the file to REMOTE_ONLY nor touch the mirror.
    status = db.query("SELECT status FROM files WHERE file_id=?", (record["file_id"],))[0]
    assert status["status"] == local_status != "REMOTE_ONLY"
    assert not (project.root / record["relative_path"]).exists()


def test_run_analysis_for_file_remote_reference_database_requires_checksum(
        operon_project, tmp_path):
    project, db = operon_project
    record = ingest_assembly(project, db, 1)
    remote_root = tmp_path / "mirror"
    remote_root.mkdir()
    executor = _ssh_executor(project, remote_root)
    analysis = recipe(tool_name="faketool", database="refdb",
                      output_subdir="fake", output_suffix=".out.tsv")
    with pytest.raises(ValidationError, match="require database_checksum"):
        run_analysis_for_file(
            project, db, analysis, tool_spec(name="faketool"), {}, record,
            executor=executor,
        )
    assert db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"] == 0


def test_run_analysis_for_file_ssh_provisions_remote_paths_and_finalizes_shutdown(
        operon_project, tmp_path, monkeypatch):
    project, db = operon_project
    record = ingest_assembly(project, db, 1)
    remote_root = tmp_path / "mirror"
    remote_root.mkdir()
    executor = _ssh_executor(project, remote_root)
    monkeypatch.setattr(tools, "_detect_version_record",
                        lambda *_a, **_k: ("9.9", "faketool: 9.9"))

    def interrupted(*_args, **_kwargs):
        raise ShutdownRequested(signal.SIGINT)

    monkeypatch.setattr(executor, "run", interrupted)
    analysis = recipe(
        tool_name="faketool", database="refdb", database_version="v1",
        output_subdir="chain", output_suffix=".out",
        arguments=[],
        commands=[
            tools.RecipeCommand(["faketool", "--out", "${output}"],
                                ["--version"], r"faketool:\s*(\S+)"),
            tools.RecipeCommand(["helper", "${work_dir}/tmp.bin"]),
        ],
        raw={"database_mode": "mutable_cache"},
    )
    tool = tool_spec(name="faketool", executable="faketool", run_method="")

    with pytest.raises(ShutdownRequested) as caught:
        run_analysis_for_file(project, db, analysis, tool, {}, record,
                              executor=executor, threads=2)
    assert caught.value.signum == signal.SIGINT

    job = db.query("SELECT * FROM analysis_jobs ORDER BY job_id DESC LIMIT 1")[0]
    assert job["status"] == "interrupted"
    assert "interrupted by signal" in job["error"]
    # An interrupted backend run never reaches its completion record.
    assert db.query(
        "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='analysis:fake_recipe'"
    )[0]["n"] == 0

    # The mutable-cache database and the per-file work directory were provisioned
    # in the remote mirror; the partial local work directory was removed.
    assert (remote_root / "refdb").is_dir()
    work_dir = (project.analysis_root / "chain" / record["entity_id"]
                / f"{record['file_id']}.genome_fasta.out.work")
    assert not work_dir.exists()
    assert (remote_root / PurePath(work_dir).relative_to(project.root)).is_dir()
    assert not (project.analysis_root / "chain" / record["entity_id"]
                / f"{record['file_id']}.genome_fasta.out").exists()


def test_run_analysis_for_file_failure_keeps_partial_work_directory(
        operon_project, tmp_path, monkeypatch):
    project, db = operon_project
    record = ingest_assembly(project, db, 1)
    remote_root = tmp_path / "mirror"
    remote_root.mkdir()
    executor = _ssh_executor(project, remote_root)
    monkeypatch.setattr(tools, "_detect_version_record",
                        lambda *_a, **_k: ("9.9", "faketool: 9.9"))

    def exploding(*_args, **_kwargs):
        raise ExternalToolError("backend exploded")

    monkeypatch.setattr(executor, "run", exploding)
    analysis = recipe(
        tool_name="faketool", database="refdb", database_version="v1",
        output_subdir="chain", output_suffix=".out", arguments=[],
        commands=[tools.RecipeCommand(["faketool", "--out", "${output}"],
                                      ["--version"], r"faketool:\s*(\S+)")],
        raw={"database_mode": "mutable_cache"},
    )
    tool = tool_spec(name="faketool", executable="faketool", run_method="")

    with pytest.raises(RuntimeError, match="backend exploded"):
        run_analysis_for_file(project, db, analysis, tool, {}, record,
                              executor=executor, keep_partial=True)
    job = db.query("SELECT * FROM analysis_jobs ORDER BY job_id DESC LIMIT 1")[0]
    assert job["status"] == "failed"
    assert "backend exploded" in job["error"]
    work_dir = (project.analysis_root / "chain" / record["entity_id"]
                / f"{record['file_id']}.genome_fasta.out.work")
    assert work_dir.is_dir()
