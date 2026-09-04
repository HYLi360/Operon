"""Execution backend configuration, Slurm script rendering, and SSH plumbing tests."""

from __future__ import annotations

import os
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import RemoteError, ValidationError
from operon.execution import (
    ExecResult,
    LocalExecutor,
    SSHExecutor,
    SlurmConfig,
    SlurmExecutor,
    _parse_remote_stats,
    _parse_sacct_accounting,
    _parse_sacct_memory_mb,
    _parse_sbatch_job_id,
    _parse_slurm_time_seconds,
    get_executor,
    load_slurm_config,
    render_slurm_script,
    rewrite_remote_path,
)
from operon.shutdown import ShutdownRequested
from operon.tools import ToolSpec, detect_tool_version_record
from operon.workflow import run_external_command


class _FakeChannel:
    """Minimal paramiko channel facade over a finished local process."""

    def __init__(self, proc: subprocess.CompletedProcess):
        self._proc = proc
        self._out_pos = 0
        self._err_pos = 0

    def recv_ready(self) -> bool:
        return self._out_pos < len(self._proc.stdout)

    def recv(self, n: int) -> bytes:
        chunk = self._proc.stdout[self._out_pos:self._out_pos + n]
        self._out_pos += len(chunk)
        return chunk

    def recv_stderr_ready(self) -> bool:
        return self._err_pos < len(self._proc.stderr)

    def recv_stderr(self, n: int) -> bytes:
        chunk = self._proc.stderr[self._err_pos:self._err_pos + n]
        self._err_pos += len(chunk)
        return chunk

    def exit_status_ready(self) -> bool:
        return True

    def recv_exit_status(self) -> int:
        return self._proc.returncode

    def close(self) -> None:
        pass


class _FakeStream:
    def __init__(self, data: bytes, channel: _FakeChannel):
        self._data = data
        self.channel = channel

    def read(self) -> bytes:
        return self._data


class FakeSSHClient:
    """Runs "remote" shell commands locally and mirrors files into remote_dir."""

    def __init__(self, remote_dir: Path | None = None):
        self.remote_dir = remote_dir
        self.sftp = FakeSFTP()
        self.commands: list[str] = []
        self.close_calls = 0

    def open_sftp(self) -> "FakeSFTP":
        return self.sftp

    def exec_command(self, command: str, timeout: float | None = None):
        self.commands.append(command)
        argv = shlex.split(command)
        if argv[:4] == ["setsid", "--wait", "sh", "-c"]:
            # The production remote contract requires util-linux setsid. The
            # fake executes that Linux-side payload on the CI host, where
            # macOS has no setsid, so emulate only the session boundary.
            proc = subprocess.run(
                argv[2:], capture_output=True, timeout=timeout,
                start_new_session=True,
            )
        else:
            proc = subprocess.run(command, shell=True, capture_output=True, timeout=timeout)
        channel = _FakeChannel(proc)
        return None, _FakeStream(proc.stdout, channel), _FakeStream(proc.stderr, channel)

    def close(self) -> None:
        self.close_calls += 1


class _FakeStat:
    def __init__(self, path: Path):
        st = path.stat()
        self.st_size = st.st_size
        self.st_mode = st.st_mode


class FakeSFTP:
    """SFTP facade backed by the local filesystem (remote paths are real paths)."""

    def stat(self, path: str) -> _FakeStat:
        p = Path(path)
        if not p.exists():
            raise IOError(f"no such file: {path}")
        return _FakeStat(p)

    def lstat(self, path: str) -> _FakeStat:
        p = Path(path)
        if not p.exists() and not p.is_symlink():
            raise IOError(f"no such file: {path}")
        obj = object.__new__(_FakeStat)
        st = p.lstat()
        obj.st_size = st.st_size
        obj.st_mode = st.st_mode
        return obj

    def put(self, local: str, remote: str) -> None:
        if not Path(remote).parent.is_dir():
            raise IOError(f"no such directory: {Path(remote).parent}")
        Path(remote).write_bytes(Path(local).read_bytes())

    def get(self, remote: str, local: str) -> None:
        if not Path(local).parent.is_dir():
            raise IOError(f"no such local directory: {Path(local).parent}")
        Path(local).write_bytes(Path(remote).read_bytes())

    def rename(self, src: str, dst: str) -> None:
        if Path(dst).exists() or Path(dst).is_symlink():
            raise IOError(f"destination exists: {dst}")
        Path(src).rename(dst)

    def posix_rename(self, src: str, dst: str) -> None:
        import os
        os.replace(src, dst)

    def mkdir(self, path: str) -> None:
        Path(path).mkdir()

    def remove(self, path: str) -> None:
        Path(path).unlink()

    def rmdir(self, path: str) -> None:
        Path(path).rmdir()

    def symlink(self, target: str, path: str) -> None:
        Path(path).symlink_to(target)

    def readlink(self, path: str) -> str:
        import os
        return os.readlink(path)

    def open(self, path: str, mode: str = "r"):
        if not Path(path).parent.is_dir():
            raise IOError(f"no such directory: {Path(path).parent}")
        return open(path, mode)

    def listdir_attr(self, path: str):
        return [_FakeStatEntry(p) for p in sorted(Path(path).iterdir())]

    def close(self) -> None:
        pass


class _FakeStatEntry(_FakeStat):
    def __init__(self, path: Path):
        obj = path.lstat()
        self.st_size = obj.st_size
        self.st_mode = obj.st_mode
        self.filename = path.name


class TestExecutionConfig(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_EXEC_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)

    def test_default_backend_is_local(self):
        executor = get_executor(self.project)
        self.assertTrue(isinstance(executor, LocalExecutor))
        self.assertEqual(executor.describe(), "local")

    def test_backend_selection_from_config(self):
        self.project.config.setdefault("execution", {})["backend"] = "slurm"
        executor = get_executor(self.project)
        self.assertTrue(isinstance(executor, SlurmExecutor))
        with self.assertRaises(ValidationError):
            get_executor(self.project, "kubernetes")

    def test_ssh_backend_requires_host(self):
        with self.assertRaisesRegex(ValidationError, "ssh.host"):
            get_executor(self.project, "ssh")

    def test_ssh_backend_rejects_unknown_scheduler(self):
        cfg = {"host": "hpc.example.org", "scheduler": "pbs"}
        with self.assertRaisesRegex(ValidationError, "scheduler"):
            SSHExecutor(self.project, cfg, SlurmConfig())

    def test_ssh_backend_requires_absolute_remote_root(self):
        cfg = {"host": "hpc.example.org", "remote_root": "relative/project"}
        with self.assertRaisesRegex(ValidationError, "absolute POSIX"):
            SSHExecutor(self.project, cfg, SlurmConfig())

    def test_ssh_backend_rejects_storage_and_execution_root_mismatch(self):
        self.project.config["remotes"] = {
            "mirror": {
                "type": "sftp", "host": "hpc.example.org", "root": "/data/project",
            },
        }
        cfg = {
            "storage_remote": "mirror", "remote_root": "/scratch/different-project",
        }
        with self.assertRaisesRegex(ValidationError, "differs from storage remote"):
            SSHExecutor(self.project, cfg, SlurmConfig())

    def test_sbatch_parser_tolerates_warnings_and_cluster_suffix(self):
        self.assertEqual(
            _parse_sbatch_job_id("warning: using default account\n4242;cluster-a\n"), "4242"
        )
        self.assertEqual(_parse_sbatch_job_id("4242_7\n"), "4242_7")
        with self.assertRaisesRegex(Exception, "could not parse"):
            _parse_sbatch_job_id("warning only\n")

    def test_local_backend_does_not_load_paramiko(self, monkeypatch):
        monkeypatch.setattr(
            "operon.remotes.import_paramiko",
            lambda: (_ for _ in ()).throw(AssertionError("Paramiko should not be loaded")),
        )
        stdout = self.root / "logs" / "local-no-paramiko.stdout.log"
        stderr = self.root / "logs" / "local-no-paramiko.stderr.log"
        stdout.parent.mkdir(exist_ok=True)
        result = LocalExecutor().run(
            ["true"], cwd=self.root, stdout_path=stdout, stderr_path=stderr,
        )
        self.assertEqual(result.exit_code, 0)

    def test_slurm_config_merge_with_recipe_overrides(self):
        self.project.config["execution"] = {
            "backend": "slurm",
            "slurm": {"partition": "short", "time": "01:00:00", "mem_gb": 8,
                      "extra_sbatch": ["--gres=gpu:1"], "setup_commands": ["module load blast"]},
        }
        slurm = load_slurm_config(self.project, {"time": "48:00:00", "mem_gb": 32})
        self.assertEqual(slurm.partition, "short")
        self.assertEqual(slurm.time_limit, "48:00:00")
        self.assertEqual(slurm.mem_gb, 32)
        self.assertEqual(slurm.extra_sbatch, ["--gres=gpu:1"])
        self.assertEqual(slurm.setup_commands, ["module load blast"])

    def test_render_slurm_script(self):
        slurm = SlurmConfig(partition="long", time_limit="12:00:00", mem_gb=16,
                            extra_sbatch=["--gres=gpu:1"], setup_commands=["module load blast/2.15"])
        script = render_slurm_script(
            job_name="operon_WF_1", command_line="blastn -query /p/in.fa -out /p/out.tsv",
            cwd="/work dir", stdout_path="/p/logs/WF_1.stdout.log", stderr_path="/p/logs/WF_1.stderr.log",
            exitcode_path="/p/logs/WF_1.exitcode", threads=8, slurm=slurm,
        )
        self.assertIn("#SBATCH --job-name=operon_WF_1", script)
        self.assertIn("#SBATCH --output=/p/logs/WF_1.stdout.log", script)
        self.assertIn("#SBATCH --error=/p/logs/WF_1.stderr.log", script)
        self.assertIn("#SBATCH --cpus-per-task=8", script)
        self.assertIn("#SBATCH --time=12:00:00", script)
        self.assertIn("#SBATCH --partition=long", script)
        self.assertIn("#SBATCH --mem=16G", script)
        self.assertIn("#SBATCH --gres=gpu:1", script)
        self.assertIn("module load blast/2.15", script)
        self.assertIn("cd '/work dir'", script)
        self.assertIn("blastn -query /p/in.fa -out /p/out.tsv", script)
        self.assertIn("echo $rc > /p/logs/WF_1.exitcode", script)
        self.assertTrue(script.endswith("exit $rc\n"))

    def test_render_slurm_script_minimal(self):
        script = render_slurm_script(
            job_name="j", command_line="echo hi", cwd="/p",
            stdout_path="/o", stderr_path="/e", exitcode_path="/x",
            threads=None, slurm=SlurmConfig(partition="", time_limit="", mem_gb=0),
        )
        self.assertIn("#SBATCH --cpus-per-task=1", script)
        self.assertFalse("--time=" in script)
        self.assertFalse("--partition=" in script)
        self.assertFalse("--mem=" in script)
        self.assertFalse("PROBE" in script or "hostname" in script)

    def test_render_slurm_script_with_probe(self):
        from operon.environment import PROBE_SHELL_LINES
        script = render_slurm_script(
            job_name="j", command_line="echo hi", cwd="/work dir",
            stdout_path="/o", stderr_path="/e", exitcode_path="/x",
            threads=None, slurm=SlurmConfig(partition="", time_limit="", mem_gb=0),
            probe_path="/p/logs/WF_1.env",
        )
        for probe_line in PROBE_SHELL_LINES:
            self.assertIn(probe_line, script)
        self.assertIn("} > /p/logs/WF_1.env 2>/dev/null || true", script)
        # The probe block sits after `cd` and before the payload command.
        self.assertTrue(script.index("cd '/work dir'") < script.index("hostname"))
        self.assertTrue(script.index("hostname") < script.index("echo hi"))

    def test_local_executor_probe_environment(self):
        env = LocalExecutor().probe_environment()
        self.assertIsNotNone(env)
        self.assertEqual(env["hostname"], socket.gethostname())
        self.assertIn("os", env)
        self.assertIn("python_version", env)

    def test_run_external_command_records_environment(self):
        record = run_external_command(self.db, self.project, ["true"], step="test:environment")
        self.assertIsNotNone(record.get("environment_id"))
        row = self.db.conn.execute(
            "SELECT environment_id FROM workflow_runs WHERE run_id=?", (record["run_id"],),
        ).fetchone()
        self.assertEqual(row["environment_id"], record["environment_id"])
        env_row = self.db.conn.execute(
            "SELECT document FROM execution_environments WHERE environment_id=?",
            (record["environment_id"],),
        ).fetchone()
        self.assertIsNotNone(env_row)
        import json as _json
        document = _json.loads(env_row["document"])
        self.assertEqual(document["hostname"], socket.gethostname())
        # A second run in the same environment reuses the row.
        second = run_external_command(self.db, self.project, ["true"], step="test:environment2")
        self.assertEqual(second["environment_id"], record["environment_id"])
        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM execution_environments").fetchone()
        self.assertEqual(count["n"], 1)

    def test_rewrite_remote_path(self):
        root = self.root
        self.assertEqual(
            rewrite_remote_path(str(root / "raw/x.fa"), root, "/remote/proj"),
            "/remote/proj/raw/x.fa",
        )
        self.assertEqual(rewrite_remote_path(str(root), root, "/remote/proj"), "/remote/proj")
        self.assertEqual(
            rewrite_remote_path("/etc/other", root, "/remote/proj"), "/etc/other",
        )
        self.assertEqual(rewrite_remote_path("--flag", root, "/remote/proj"), "--flag")
        self.assertEqual(
            rewrite_remote_path(str(root / "raw/x.fa"), root, ""), str(root / "raw/x.fa"),
        )
        self.assertEqual(rewrite_remote_path(str(root / "raw/x.fa"), root, "/"), "/raw/x.fa")
        with self.assertRaisesRegex(ValidationError, "escapes the project root"):
            rewrite_remote_path(str(root / ".." / "escape.fa"), root, "/remote/proj")
        link = root / "link"
        link.symlink_to(root.parent)
        with self.assertRaisesRegex(ValidationError, "escapes the project root"):
            rewrite_remote_path(str(link / "x"), root, "/remote/proj")

    def test_rewrite_remote_path_accepts_resolved_root_alias(self):
        real_root = self.root / "real"
        real_root.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(real_root, target_is_directory=True)
        self.assertEqual(
            rewrite_remote_path(str(alias / "raw/x.fa"), alias, "/remote/proj"),
            "/remote/proj/raw/x.fa",
        )

    def test_run_external_command_local_default_unchanged(self):
        output = self.root / "out.txt"
        record = run_external_command(
            self.db, self.project, ["bash", "-c", f"echo hello > {output}"],
            step="test:local", expected_outputs=[output], cwd=self.root,
        )
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["executor"], "local")
        self.assertTrue(output.read_text().strip() == "hello")

    def test_run_external_command_records_threads(self):
        run_external_command(
            self.db, self.project, ["true"], step="test:threads", threads=7,
        )
        row = self.db.conn.execute(
            "SELECT threads FROM workflow_runs WHERE step='test:threads'"
        ).fetchone()
        self.assertEqual(row["threads"], 7)

    def test_run_external_command_records_executor_provenance(self):
        record = run_external_command(self.db, self.project, ["true"], step="test:provenance")
        row = self.db.conn.execute(
            "SELECT executor, scheduler_job_id, execution_details FROM workflow_runs "
            "WHERE run_id=?", (record["run_id"],),
        ).fetchone()
        self.assertEqual(row["executor"], "local")
        self.assertIsNone(row["scheduler_job_id"])
        self.assertIn('"backend": "local"', row["execution_details"])

    def test_local_executor_collects_resources(self):
        out_log = self.root / "logs" / "res.stdout.log"
        err_log = self.root / "logs" / "res.stderr.log"
        result = LocalExecutor().run(
            [sys.executable, "-c",
             "x = bytearray(20 << 20)\nimport time\ntime.sleep(1.2)"],
            cwd=self.root, stdout_path=out_log, stderr_path=err_log,
        )
        self.assertEqual(result.exit_code, 0)
        # The 20 MiB allocation must show up in the sampled RSS.
        self.assertGreater(result.resources["max_rss_mb"], 10)
        self.assertGreater(result.resources["avg_rss_mb"], 0)
        self.assertLessEqual(result.resources["avg_rss_mb"], result.resources["max_rss_mb"])
        self.assertGreaterEqual(result.resources["cpu_seconds"], 0)

    def test_local_executor_resources_degrade_silently(self):
        out_log = self.root / "logs" / "deg.stdout.log"
        err_log = self.root / "logs" / "deg.stderr.log"
        # A failing command still returns resources without raising.
        result = LocalExecutor().run(
            ["false"], cwd=self.root, stdout_path=out_log, stderr_path=err_log,
        )
        self.assertEqual(result.exit_code, 1)
        self.assertGreaterEqual(result.resources.get("cpu_seconds", 0), 0)

    def test_run_external_command_records_resource_usage(self):
        record = run_external_command(
            self.db, self.project,
            [sys.executable, "-c",
             "x = bytearray(20 << 20)\nimport time\ntime.sleep(0.8)"],
            step="test:resources",
        )
        row = self.db.conn.execute(
            "SELECT duration_seconds, max_rss_mb, avg_rss_mb, cpu_seconds "
            "FROM workflow_runs WHERE run_id=?", (record["run_id"],),
        ).fetchone()
        self.assertIsNotNone(row["duration_seconds"])
        self.assertGreaterEqual(row["duration_seconds"], 0.7)
        self.assertTrue(row["duration_seconds"] < 30)
        self.assertGreater(row["max_rss_mb"], 10)
        self.assertGreater(row["avg_rss_mb"], 0)
        self.assertGreaterEqual(row["cpu_seconds"], 0)

    def test_run_external_command_failed_run_has_null_resources(self):
        with self.assertRaisesRegex(RuntimeError, "test:failed-resources"):
            run_external_command(self.db, self.project, ["false"], step="test:failed-resources")
        row = self.db.conn.execute(
            "SELECT duration_seconds, max_rss_mb FROM workflow_runs WHERE step='test:failed-resources'"
        ).fetchone()
        self.assertIsNotNone(row["duration_seconds"])

    def test_version_cache_is_scoped_to_executor_identity(self):
        import operon.tools as tools_module
        tools_module._VERSION_CACHE.clear()
        tool = ToolSpec(
            name="fake", executable="fake", run_method="", version_args=["--version"],
            version_pattern=r"fake\s+([^\s]+)", description="", recipes={}, raw={},
        )

        class VersionExecutor:
            name = "ssh"

            def __init__(self, host, version):
                self.host = host
                self.version = version

            def describe(self):
                return f"ssh:{self.host}"

            def cache_identity(self):
                return self.describe()

            def run(self, argv, *, stdout_path, stderr_path, **kwargs):
                stdout_path.write_text(f"fake {self.version}\n", encoding="utf-8")
                stderr_path.write_text("", encoding="utf-8")
                return ExecResult(0)

        first = detect_tool_version_record(tool, {}, executor=VersionExecutor("one", "1.0"))[0]
        second = detect_tool_version_record(tool, {}, executor=VersionExecutor("two", "2.0"))[0]
        self.assertEqual(first, "1.0")
        self.assertEqual(second, "2.0")


class TestSSHExecutorWithFakeClient(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_SSH_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)

    def _executor(self, remote_root: str = "", scheduler: str = "none",
                  client: FakeSSHClient | None = None) -> SSHExecutor:
        cfg = {"host": "fake.example.org", "user": "tester", "remote_root": remote_root,
               "scheduler": scheduler}
        return SSHExecutor(self.project, cfg, SlurmConfig(poll_interval=0.05),
                           client_factory=lambda _self: client or FakeSSHClient())

    def test_direct_execution_shared_filesystem(self):
        client = FakeSSHClient()
        executor = self._executor(client=client)
        out_log = self.root / "logs" / "t.stdout.log"
        err_log = self.root / "logs" / "t.stderr.log"
        output = self.root / "result.txt"
        result = executor.run(
            ["bash", "-c", f"echo remote-run > {output}"],
            cwd=self.root, stdout_path=out_log, stderr_path=err_log,
            expected_outputs=[output],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(output.read_text().strip(), "remote-run")
        self.assertTrue(any("remote-run" in c for c in client.commands))

    def test_direct_execution_exit_code_and_stderr(self):
        executor = self._executor()
        out_log = self.root / "logs" / "f.stdout.log"
        err_log = self.root / "logs" / "f.stderr.log"
        result = executor.run(
            ["bash", "-c", "echo oops >&2; exit 3"],
            cwd=self.root, stdout_path=out_log, stderr_path=err_log,
        )
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(err_log.read_text().strip(), "oops")

    def test_direct_execution_collects_remote_rss_stats(self):
        client = FakeSSHClient()
        executor = self._executor(client=client)
        out_log = self.root / "logs" / "stats.stdout.log"
        err_log = self.root / "logs" / "stats.stderr.log"
        result = executor.run(
            [sys.executable, "-c",
             "x = bytearray(20 << 20)\nimport time\ntime.sleep(2.5)"],
            cwd=self.root, stdout_path=out_log, stderr_path=err_log,
        )
        self.assertEqual(result.exit_code, 0)
        # The fake client executes the payload locally, so the remote sampler
        # really ran and its stats file was read back over "SSH".
        self.assertGreater(result.resources["max_rss_mb"], 10)
        self.assertGreater(result.resources["avg_rss_mb"], 0)
        self.assertLessEqual(result.resources["avg_rss_mb"], result.resources["max_rss_mb"])
        self.assertFalse("cpu_seconds" in result.resources)  # unavailable in direct mode
        # Both the pidfile and the stats file are cleaned up afterwards.
        stats_commands = [c for c in client.commands if ".stats" in c]
        self.assertTrue(stats_commands)
        self.assertTrue(any(c.startswith("cat ") and "rm -f" in c for c in stats_commands))

    def test_direct_timeout_attempts_remote_process_group_termination(self):
        class HangingChannel:
            closed = False

            def recv_ready(self):
                return False

            def recv_stderr_ready(self):
                return False

            def exit_status_ready(self):
                return False

            def close(self):
                self.closed = True

        class HangingStream:
            def __init__(self, channel):
                self.channel = channel

        client = FakeSSHClient()
        hanging = HangingChannel()

        def fake_exec(command, timeout=None):
            client.commands.append(command)
            if command.startswith("setsid "):
                stream = HangingStream(hanging)
                return None, stream, stream
            proc = subprocess.CompletedProcess(command, 0, b"", b"")
            channel = _FakeChannel(proc)
            return None, _FakeStream(proc.stdout, channel), _FakeStream(proc.stderr, channel)

        client.exec_command = fake_exec
        result = self._executor(client=client).run(
            ["sleep", "30"], cwd=self.root,
            stdout_path=self.root / "logs" / "timeout.stdout.log",
            stderr_path=self.root / "logs" / "timeout.stderr.log",
            timeout=0.001, run_id="WF_TIMEOUT",
        )
        self.assertIsNone(result.exit_code)
        self.assertIn("termination signals were sent", result.error)
        self.assertTrue(hanging.closed)
        self.assertTrue(any("kill -TERM" in command for command in client.commands))
        self.assertEqual(result.details["termination_signaled"], True)
        self.assertTrue(any(
            command.startswith("cat ") and ".stats" in command and "rm -f" in command
            for command in client.commands
        ))
        # setsid must wait for the payload so its exit status is propagated
        # even when setsid itself is already a process group leader.
        setsid_commands = [c for c in client.commands if c.startswith("setsid ")]
        self.assertTrue(setsid_commands)
        self.assertTrue(all(c.startswith("setsid --wait ") for c in setsid_commands))

    def test_executor_reuses_one_ssh_connection_until_closed(self):
        client = FakeSSHClient()
        connections = 0

        def factory(_executor):
            nonlocal connections
            connections += 1
            return client

        cfg = {"host": "fake.example.org", "scheduler": "none"}
        executor = SSHExecutor(
            self.project, cfg, SlurmConfig(poll_interval=0.05), client_factory=factory,
        )
        for suffix in ("one", "two"):
            result = executor.run(
                ["true"], cwd=self.root,
                stdout_path=self.root / "logs" / f"{suffix}.stdout.log",
                stderr_path=self.root / "logs" / f"{suffix}.stderr.log",
            )
            self.assertEqual(result.exit_code, 0)
        self.assertEqual(connections, 1)
        self.assertEqual(client.close_calls, 0)
        executor.close()
        self.assertEqual(client.close_calls, 1)

    def test_remote_slurm_skips_login_node_environment_probe(self):
        client = FakeSSHClient()
        executor = self._executor(scheduler="slurm", client=client)

        self.assertIsNone(executor.probe_environment())
        self.assertEqual(client.commands, [])

    def test_stage_and_pull_with_remote_root(self):
        remote_root = self.root / "remote-mirror"
        remote_root.mkdir()
        client = FakeSSHClient()
        executor = self._executor(remote_root=str(remote_root), client=client)
        staged = self.root / "inputs" / "data.txt"
        staged.parent.mkdir()
        staged.write_text("staged-bytes", encoding="utf-8")
        out_log = self.root / "logs" / "s.stdout.log"
        err_log = self.root / "logs" / "s.stderr.log"
        output = self.root / "analysis" / "out.txt"
        # Path arguments are rewritten into the remote mirror; the command runs
        # "remotely" (locally in the fake), and the output is pulled back.
        result = executor.run(
            ["cp", str(staged), str(output)],
            cwd=self.root, stdout_path=out_log, stderr_path=err_log,
            stage_inputs=[staged], expected_outputs=[output],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual((remote_root / "inputs" / "data.txt").read_text(), "staged-bytes")
        self.assertEqual(output.read_text(), "staged-bytes")
        # The command the fake received must reference rewritten mirror paths.
        self.assertTrue(any(str(remote_root / "analysis" / "out.txt") in c for c in client.commands))

    def test_declared_workflow_input_is_staged_with_remote_root(self):
        remote_root = self.root / "remote-declared-input"
        remote_root.mkdir()
        client = FakeSSHClient()
        executor = self._executor(remote_root=str(remote_root), client=client)
        staged = self.root / "inputs" / "declared.txt"
        staged.parent.mkdir()
        staged.write_text("declared-input", encoding="utf-8")
        output = self.root / "analysis" / "declared-output.txt"

        record = run_external_command(
            self.db, self.project, ["cp", str(staged), str(output)],
            step="test:declared-ssh-input", cwd=self.root,
            inputs=[Path("inputs/declared.txt")], expected_outputs=[output],
            executor=executor,
        )

        self.assertEqual(record["status"], "completed")
        self.assertEqual(
            (remote_root / "inputs" / "declared.txt").read_text(), "declared-input",
        )
        self.assertEqual(output.read_text(), "declared-input")

    def test_staged_input_outside_project_is_rejected(self):
        remote_root = self.root / "remote-contained-input"
        remote_root.mkdir()
        outside = self.root.parent / f"{self.root.name}-outside.txt"
        outside.write_text("outside", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        executor = self._executor(remote_root=str(remote_root), client=FakeSSHClient())

        with self.assertRaisesRegex(ValidationError, "must stay under the project root"):
            executor.run(
                ["true"], cwd=self.root,
                stdout_path=self.root / "logs" / "outside.stdout.log",
                stderr_path=self.root / "logs" / "outside.stderr.log",
                stage_inputs=[outside],
            )

    def test_stale_remote_output_is_removed_before_execution(self):
        remote_root = self.root / "remote-stale"
        stale = remote_root / "analysis" / "out.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text("old result", encoding="utf-8")
        client = FakeSSHClient()
        executor = self._executor(remote_root=str(remote_root), client=client)
        local_output = self.root / "analysis" / "out.txt"
        result = executor.run(
            ["true"], cwd=self.root,
            stdout_path=self.root / "logs" / "stale.stdout.log",
            stderr_path=self.root / "logs" / "stale.stderr.log",
            expected_outputs=[local_output],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(stale.exists())
        self.assertFalse(local_output.exists())

    def test_remote_database_contract_requires_reference_and_creates_mutable_cache(self):
        remote_root = self.root / "remote-database"
        remote_root.mkdir()
        executor = self._executor(remote_root=str(remote_root), client=FakeSSHClient())
        database = self.root / "resources" / "reference-db"
        with self.assertRaisesRegex(RemoteError, "not provisioned"):
            executor.prepare_database(database, mutable_cache=False)
        remote = executor.prepare_database(database, mutable_cache=True)
        self.assertEqual(remote, str(remote_root / "resources" / "reference-db"))
        self.assertTrue(Path(remote).is_dir())

    def test_remote_slurm_submission_flow(self, monkeypatch):
        remote_root = self.root / "remote-slurm"
        remote_root.mkdir()
        client = FakeSSHClient()
        executor = self._executor(remote_root=str(remote_root), scheduler="slurm", client=client)
        staged = self.root / "inputs" / "reads.txt"
        staged.parent.mkdir(exist_ok=True)
        staged.write_text("via-slurm", encoding="utf-8")
        out_log = self.root / "logs" / "r.stdout.log"
        err_log = self.root / "logs" / "r.stderr.log"
        output = self.root / "analysis" / "slurm-out.txt"
        stale_exitcode = remote_root / "logs" / "WF_TEST_1.exitcode"
        stale_exitcode.parent.mkdir(parents=True, exist_ok=True)
        stale_exitcode.write_text("99\n", encoding="utf-8")
        executor.slurm.poll_interval = 7.25
        sleep_calls: list[float] = []
        monkeypatch.setattr("operon.execution.time.sleep", sleep_calls.append)
        queue_checks = 0
        exitcode_checks = 0
        # Fake remote: sbatch executes the script synchronously, squeue says
        # the job is gone; both are just shell commands to the fake client.
        def fake_exec(command, timeout=None):
            nonlocal queue_checks, exitcode_checks
            client.commands.append(command)
            if command.startswith("sbatch "):
                script = command.split()[-1]
                # Honor the #SBATCH --output/--error redirections like Slurm would.
                out = err = None
                for line in Path(script).read_text().splitlines():
                    if line.startswith("#SBATCH --output="):
                        out = line.split("=", 1)[1]
                    if line.startswith("#SBATCH --error="):
                        err = line.split("=", 1)[1]
                with open(out, "wb") as o, open(err, "wb") as e:
                    subprocess.run(["bash", script], stdout=o, stderr=e)
                proc = subprocess.CompletedProcess(
                    command, 0, b"warning: default account selected\n4242;cluster-a\n", b""
                )
            elif command.startswith("squeue "):
                queue_checks += 1
                output = b"4242\n" if queue_checks == 1 else b""
                proc = subprocess.CompletedProcess(command, 0, output, b"")
            elif command.startswith("cat ") and command.endswith(".exitcode"):
                exitcode_checks += 1
                if exitcode_checks < 3:
                    proc = subprocess.CompletedProcess(command, 1, b"", b"not visible yet")
                else:
                    proc = subprocess.CompletedProcess(
                        command, 0, stale_exitcode.read_bytes(), b""
                    )
            else:
                proc = subprocess.run(command, shell=True, capture_output=True, timeout=timeout)
            channel = _FakeChannel(proc)
            return None, _FakeStream(proc.stdout, channel), _FakeStream(proc.stderr, channel)
        client.exec_command = fake_exec
        result = executor.run(
            ["cp", str(staged), str(output)],
            cwd=self.root, stdout_path=out_log, stderr_path=err_log,
            stage_inputs=[staged], expected_outputs=[output], run_id="WF_TEST_1",
        )
        self.assertEqual(result.exit_code, 0, result.error)
        self.assertEqual(output.read_text(), "via-slurm")
        self.assertTrue(any(c.startswith("sbatch ") for c in client.commands))
        self.assertTrue(any(c.startswith("squeue ") for c in client.commands))
        self.assertEqual([seconds for seconds in sleep_calls if seconds >= 1], [7.25, 1, 1])
        self.assertEqual(exitcode_checks, 3)
        self.assertEqual(result.details["environment"]["hostname"], socket.gethostname())
        # The uploaded batch script must live in and reference the mirror.
        script = remote_root / "logs" / "WF_TEST_1.sbatch"
        self.assertTrue(script.exists())
        self.assertIn(str(remote_root / "analysis" / "slurm-out.txt"), script.read_text())
        self.assertIn(str(remote_root / "logs" / "WF_TEST_1.env"), script.read_text())
        self.assertFalse((remote_root / "logs" / "WF_TEST_1.env").exists())


FAKE_SBATCH_OK = """\
#!/usr/bin/env bash
echo "12345"
"""

FAKE_SQUEUE_BUSY = """\
#!/usr/bin/env bash
# Job always present in the queue.
echo "12345"
"""

FAKE_SCANCEL = """\
#!/usr/bin/env bash
echo "$@" >> "$SCANCEL_LOG"
"""


class TestShutdownCleanup(PytestAssertions):
    """Interrupt (KeyboardInterrupt/ShutdownRequested) cleanup per backend."""

    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_SHUT_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)

    def _wait_until_dead(self, pid: int) -> bool:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False

    def test_local_interrupt_kills_whole_process_group(self, monkeypatch):
        pidfile = self.root / "grandchild.pid"
        out_log = self.root / "logs" / "int.stdout.log"
        err_log = self.root / "logs" / "int.stderr.log"
        executor = LocalExecutor()
        real_wait = subprocess.Popen.wait
        interrupted = {"done": False}

        def interrupting_wait(process, timeout=None):
            if not interrupted["done"]:
                interrupted["done"] = True
                # Let the child actually spawn its grandchild first.
                deadline = time.monotonic() + 5
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                raise ShutdownRequested(signal.SIGINT)
            return real_wait(process, timeout)

        monkeypatch.setattr(subprocess.Popen, "wait", interrupting_wait)
        with self.assertRaises(ShutdownRequested):
            executor.run(
                ["bash", "-c", f"sleep 60 & echo $! > {pidfile}; wait"],
                cwd=self.root, stdout_path=out_log, stderr_path=err_log,
            )
        grandchild = int(pidfile.read_text().strip())
        # SIGTERM went to the whole group: the grandchild sleep must be gone.
        self.assertTrue(self._wait_until_dead(grandchild))

    def test_slurm_interrupt_cancels_cluster_job(self, monkeypatch):
        bin_dir = self.root / "fakebin"
        bin_dir.mkdir()
        scancel_log = self.root / "scancel.log"
        for name, content in (("sbatch", FAKE_SBATCH_OK), ("squeue", FAKE_SQUEUE_BUSY),
                              ("scancel", FAKE_SCANCEL)):
            script = bin_dir / name
            script.write_text(content, encoding="utf-8")
            script.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old_path))
        os.environ["SCANCEL_LOG"] = str(scancel_log)
        self.addCleanup(lambda: os.environ.pop("SCANCEL_LOG", None))

        executor = SlurmExecutor(self.project, SlurmConfig(poll_interval=0.05))

        def interrupting_sleep(_seconds):
            raise ShutdownRequested(signal.SIGTERM)

        monkeypatch.setattr("operon.execution.time.sleep", interrupting_sleep)
        with self.assertRaises(ShutdownRequested):
            executor.run(
                ["sleep", "30"], cwd=self.root,
                stdout_path=self.root / "logs" / "s.stdout.log",
                stderr_path=self.root / "logs" / "s.stderr.log",
                run_id="WF_SLURM_INT",
            )
        self.assertEqual(scancel_log.read_text().strip(), "12345")


class _HangingChannel:
    def __init__(self):
        self.closed = False

    def recv_ready(self):
        return False

    def recv_stderr_ready(self):
        return False

    def exit_status_ready(self):
        return False

    def close(self):
        self.closed = True


class _HangingStream:
    def __init__(self, channel):
        self.channel = channel


class TestSSHShutdownCleanup(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_SSHI_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)

    def _executor(self, scheduler: str, client: FakeSSHClient) -> SSHExecutor:
        cfg = {"host": "fake.example.org", "user": "tester", "scheduler": scheduler}
        return SSHExecutor(self.project, cfg, SlurmConfig(poll_interval=0.05),
                           client_factory=lambda _self: client)

    def test_direct_interrupt_terminates_remote_process_group(self, monkeypatch):
        client = FakeSSHClient()
        hanging = _HangingChannel()

        def fake_exec(command, timeout=None):
            client.commands.append(command)
            if command.startswith("setsid "):
                stream = _HangingStream(hanging)
                return None, stream, stream
            proc = subprocess.CompletedProcess(command, 0, b"", b"")
            channel = _FakeChannel(proc)
            return None, _FakeStream(proc.stdout, channel), _FakeStream(proc.stderr, channel)

        client.exec_command = fake_exec

        def interrupting_sleep(_seconds):
            raise ShutdownRequested(signal.SIGINT)

        monkeypatch.setattr("operon.execution.time.sleep", interrupting_sleep)
        with self.assertRaises(ShutdownRequested):
            self._executor("none", client).run(
                ["sleep", "30"], cwd=self.root,
                stdout_path=self.root / "logs" / "i.stdout.log",
                stderr_path=self.root / "logs" / "i.stderr.log",
                run_id="WF_SSH_INT",
            )
        # The setsid payload would survive connection teardown, so the
        # shutdown path must actively kill the remote process group.
        self.assertTrue(any("kill -TERM" in command for command in client.commands))
        self.assertTrue(hanging.closed)

    def test_remote_slurm_interrupt_cancels_job(self, monkeypatch):
        client = FakeSSHClient()

        def fake_exec(command, timeout=None):
            client.commands.append(command)
            if command.startswith("sbatch "):
                proc = subprocess.CompletedProcess(command, 0, b"4242\n", b"")
            elif command.startswith("squeue "):
                proc = subprocess.CompletedProcess(command, 0, b"4242\n", b"")
            else:
                proc = subprocess.CompletedProcess(command, 0, b"", b"")
            channel = _FakeChannel(proc)
            return None, _FakeStream(proc.stdout, channel), _FakeStream(proc.stderr, channel)

        client.exec_command = fake_exec

        def interrupting_sleep(_seconds):
            raise ShutdownRequested(signal.SIGTERM)

        monkeypatch.setattr("operon.execution.time.sleep", interrupting_sleep)
        with self.assertRaises(ShutdownRequested):
            self._executor("slurm", client).run(
                ["sleep", "30"], cwd=self.root,
                stdout_path=self.root / "logs" / "rs.stdout.log",
                stderr_path=self.root / "logs" / "rs.stderr.log",
                run_id="WF_RSLURM_INT",
            )
        self.assertTrue(any(c.startswith("scancel 4242") for c in client.commands))


class TestResourceParsing(PytestAssertions):
    """Pure parsers for sacct accounting output and remote sampler stats."""

    def test_parse_sacct_memory_suffixes(self):
        self.assertEqual(_parse_sacct_memory_mb("256M"), 256.0)
        self.assertAlmostEqual(_parse_sacct_memory_mb("2048K"), 2.0)
        self.assertEqual(_parse_sacct_memory_mb("1.5G"), 1536.0)
        self.assertEqual(_parse_sacct_memory_mb("2T"), 2.0 * 1024 * 1024)
        # A bare number is raw bytes (Slurm prints small exact values so).
        self.assertAlmostEqual(_parse_sacct_memory_mb("1048576"), 1.0)
        for bad in ("", "Unknown", "abc", "12X"):
            self.assertIsNone(_parse_sacct_memory_mb(bad))

    def test_parse_slurm_time_formats(self):
        self.assertEqual(_parse_slurm_time_seconds("01:05"), 65.0)
        self.assertEqual(_parse_slurm_time_seconds("02:03:04"), 7384.0)
        self.assertEqual(_parse_slurm_time_seconds("1-02:03:04"), 93784.0)
        self.assertAlmostEqual(_parse_slurm_time_seconds("00:00:30.5"), 30.5)
        for bad in ("", "Unknown", "1:2:3:4", "abc"):
            self.assertIsNone(_parse_slurm_time_seconds(bad))

    def test_parse_sacct_accounting_multiple_steps(self):
        text = (
            # Main job row first; memory fields are empty on it.
            "0:0|||00:02:10|00:01:40|\n"
            "0:0|256M|200M|00:02:10|00:01:38|\n"
            "0:0|512K|400K|00:02:10|00:00:02|\n"
            "0:0|1G|700M|00:02:10|00:01:30|\n"
        )
        accounting = _parse_sacct_accounting(text)
        self.assertEqual(accounting["exit_code"], 0)
        self.assertEqual(accounting["max_rss_mb"], 1024.0)
        self.assertEqual(accounting["avg_rss_mb"], 700.0)
        self.assertEqual(accounting["elapsed_seconds"], 130.0)
        self.assertEqual(accounting["cpu_seconds"], 100.0)

    def test_parse_sacct_accounting_degrades_gracefully(self):
        self.assertEqual(_parse_sacct_accounting(""), {})
        self.assertEqual(_parse_sacct_accounting("garbage\nlines\n"), {})
        # Non-zero exit code from the first parseable token; no memory data.
        accounting = _parse_sacct_accounting("1:0|||00:00:05|00:00:01|\n")
        self.assertEqual(accounting["exit_code"], 1)
        self.assertFalse("max_rss_mb" in accounting)
        self.assertEqual(accounting["elapsed_seconds"], 5.0)

    def test_parse_remote_stats(self):
        parsed = _parse_remote_stats("20480 30720 3\n")
        self.assertEqual(parsed["max_rss_mb"], 20.0)
        self.assertEqual(parsed["avg_rss_mb"], 10.0)
        for bad in ("", "garbage", "1 2", "1 2 0", "a b c"):
            self.assertEqual(_parse_remote_stats(bad), {})

    def test_exec_result_resources_default_empty(self):
        self.assertEqual(ExecResult(exit_code=0).resources, {})
