"""Execution backend configuration, Slurm script rendering, and SSH plumbing tests."""

from __future__ import annotations

import subprocess
import tempfile
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
    get_executor,
    load_slurm_config,
    render_slurm_script,
    rewrite_remote_path,
)
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

    def open_sftp(self) -> "FakeSFTP":
        return self.sftp

    def exec_command(self, command: str, timeout: float | None = None):
        self.commands.append(command)
        proc = subprocess.run(command, shell=True, capture_output=True, timeout=timeout)
        channel = _FakeChannel(proc)
        return None, _FakeStream(proc.stdout, channel), _FakeStream(proc.stderr, channel)

    def close(self) -> None:
        pass


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
        Path(remote).parent.mkdir(parents=True, exist_ok=True)
        Path(remote).write_bytes(Path(local).read_bytes())

    def get(self, remote: str, local: str) -> None:
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        Path(local).write_bytes(Path(remote).read_bytes())

    def rename(self, src: str, dst: str) -> None:
        if Path(dst).exists() or Path(dst).is_symlink():
            raise IOError(f"destination exists: {dst}")
        Path(src).rename(dst)

    def posix_rename(self, src: str, dst: str) -> None:
        import os
        os.replace(src, dst)

    def mkdir(self, path: str) -> None:
        Path(path).mkdir(exist_ok=True)

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
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        return open(path, mode)

    def listdir_attr(self, path: str):
        return [_FakeStatEntry(p) for p in sorted(Path(path).iterdir())]

    def close(self) -> None:
        pass


class _FakeStatEntry(_FakeStat):
    def __init__(self, path: Path):
        super().__init__(path)
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
            cwd="/p", stdout_path="/p/logs/WF_1.stdout.log", stderr_path="/p/logs/WF_1.stderr.log",
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
        self.assertEqual(
            rewrite_remote_path(str(root / "raw/x.fa"), root, ""), str(root / "raw/x.fa"),
        )
        self.assertEqual(rewrite_remote_path(str(root / "raw/x.fa"), root, "/"), "/raw/x.fa")
        with self.assertRaisesRegex(ValidationError, "escapes the project root"):
            rewrite_remote_path(str(root / ".." / "escape.fa"), root, "/remote/proj")

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

    def test_remote_slurm_submission_flow(self):
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
        # Fake remote: sbatch executes the script synchronously, squeue says
        # the job is gone; both are just shell commands to the fake client.
        def fake_exec(command, timeout=None):
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
                proc = subprocess.CompletedProcess(command, 0, b"4242\n", b"")
            elif command.startswith("squeue "):
                proc = subprocess.CompletedProcess(command, 0, b"", b"")
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
        # The uploaded batch script must live in and reference the mirror.
        script = remote_root / "logs" / "WF_TEST_1.sbatch"
        self.assertTrue(script.exists())
        self.assertIn(str(remote_root / "analysis" / "slurm-out.txt"), script.read_text())
