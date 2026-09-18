"""Slurm array submission in `operon analyze` (two-phase run_analysis wiring)."""

from __future__ import annotations

import shlex
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

import pytest
import yaml

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import RemoteError
from operon.execution import ExecResult, SlurmConfig, SlurmExecutor
from operon.files import ingest_file
from operon.shutdown import ShutdownRequested
from operon.tools import run_analysis
from tests.helpers import PytestAssertions


class _FakeSFTPChannel:
    """Recording stand-in for a paramiko SFTP client."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSSHClientChannel:
    def __init__(self):
        self.sftp = _FakeSFTPChannel()

    def open_sftp(self):
        return self.sftp


class _FakeRemoteSlurmExecutor:
    """Duck-typed remote-Slurm executor recording the transfer guarantee calls.

    Executes payloads locally, but keeps the SSH surface (staging, output
    backup/pull/restore) as recorded no-ops so the array wiring in tools.py
    can be verified without a cluster.
    """

    name = "ssh"
    remote_root = "/remote/proj"
    storage_remote = ""
    scheduler = "slurm"

    def __init__(self, project, *, array: bool = True, concurrency: int | None = None):
        self.project = project
        self.slurm = SlurmConfig(array=array, array_concurrency=concurrency)
        self.client = _FakeSSHClientChannel()
        self.transfer_log: list[tuple] = []
        self.array_calls: list[dict] = []
        self.fail_staging_for: set[str] = set()
        self.run_array_impl = None
        self.closed = False

    def describe(self):
        return "ssh tester@fake [slurm]"

    def cache_identity(self):
        return "ssh:fake:scheduler=slurm"

    def probe_environment(self):
        return None

    def close(self):
        self.closed = True

    def _connect(self):
        return self.client

    def _stage_inputs(self, client, sftp, stage_inputs):
        for item in stage_inputs:
            if str(item) in self.fail_staging_for:
                raise RemoteError(f"cannot stage {item}")
            assert Path(item).exists()
            self.transfer_log.append(("stage", str(item)))

    def _reset_outputs(self, sftp, expected_outputs):
        self.transfer_log.append(("reset", [str(p) for p in expected_outputs]))
        return [(str(p), f"{p}.bak") for p in expected_outputs]

    def _pull_outputs(self, client, sftp, expected_outputs):
        for path in expected_outputs:
            assert Path(path).exists(), f"remote output never produced: {path}"
        self.transfer_log.append(("pull", [str(p) for p in expected_outputs]))

    def _drop_output_backups(self, sftp, backups):
        self.transfer_log.append(("drop", list(backups)))

    def _restore_output_backups(self, sftp, backups):
        self.transfer_log.append(("restore", list(backups)))

    def _execute(self, argv, cwd, stdout_path, stderr_path):
        Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
        with open(stdout_path, "w") as out, open(stderr_path, "w") as err:
            return subprocess.run([str(a) for a in argv], stdout=out, stderr=err, cwd=cwd)

    def run(self, argv, *, cwd, stdout_path, stderr_path, timeout=None, threads=None,
            run_id=None, stage_inputs=(), expected_outputs=()):
        proc = self._execute(argv, cwd, stdout_path, stderr_path)
        return ExecResult(exit_code=proc.returncode, scheduler_job_id=f"job-{run_id}")

    def run_array(self, tasks, *, cwd=None, threads=None, timeout=None, batch_id=None,
                  array_concurrency=None):
        self.array_calls.append({
            "run_ids": [task["run_id"] for task in tasks],
            "array_concurrency": array_concurrency,
        })
        if self.run_array_impl is not None:
            return self.run_array_impl(tasks, cwd=cwd)
        results = []
        for index, task in enumerate(tasks, start=1):
            proc = self._execute(shlex.split(task["command"]), cwd,
                                 task["stdout_path"], task["stderr_path"])
            exitcode = Path(task["stdout_path"]).parent / f"{task['run_id']}.exitcode"
            exitcode.write_text(f"{proc.returncode}\n", encoding="utf-8")
            results.append(ExecResult(exit_code=proc.returncode,
                                      scheduler_job_id=f"9000_{index}"))
        return results


class TestAnalysisArray(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_ARR_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        self.array_calls: list[dict] = []
        self.run_calls: list[dict] = []

    def _write_fake_blast(self) -> Path:
        script = self.root / "fakeblast.py"
        script.write_text(textwrap.dedent("""
            import sys
            args = sys.argv[1:]
            if '-version' in args:
                print('fakeblast: 9.8.7')
                raise SystemExit(0)
            out = args[args.index('-out') + 1]
            with open(out, 'w') as handle:
                handle.write('q1\\ts1\\t99.0\\t100\\t1e-10\\t500\\n')
        """).strip(), encoding="utf-8")
        return script

    def _write_tool_config(self, executable: Path, slurm: dict | None = None):
        recipe = {
            "entity_type": "assembly",
            "file_role": "genome_fasta",
            "format": "fasta",
            "output_subdir": "fake_nt",
            "output_suffix": ".out.tsv",
            "arguments": ["-query", "${input}", "-out", "${output}", "-num_threads", "${threads}"],
            "result_parser": "blast_tabular",
            "result_columns": ["qseqid", "sseqid", "pident", "length", "evalue", "bitscore"],
        }
        if slurm is not None:
            recipe["slurm"] = slurm
        tool_config = {
            "version": 1,
            "tools": {
                "fakeblast": {
                    "executable": str(executable),
                    "run_method": sys.executable,
                    "version_args": ["-version"],
                    "version_pattern": r"fakeblast:\s*([^\s]+)",
                    "recipes": {"fake_nt": recipe},
                }
            },
        }
        self.project.tools_config_path.write_text(yaml.safe_dump(tool_config, sort_keys=False), encoding="utf-8")

    def _add_assembly(self, number: int):
        self.db.insert_row("organisms", {"organism_id": f"ORG_{number:06d}", "scientific_name": "Testus", "taxonomy_source": "NCBI"})
        self.db.insert_row("samples", {"sample_id": f"SMP_{number:06d}", "organism_id": f"ORG_{number:06d}"})
        self.db.insert_row("assemblies", {"assembly_id": f"ASM_{number:06d}", "sample_id": f"SMP_{number:06d}", "assembly_level": "contig", "assembly_version": 1})
        fasta = self.root / f"asm{number}.fa"
        fasta.write_text(f">ctg{number}\n" + "ACGT" * 600 + "\n", encoding="utf-8")
        return ingest_file(self.db, self.project, fasta, "assembly", f"ASM_{number:06d}", "genome_fasta")

    def _output_path(self, file_row) -> Path:
        return (self.project.analysis_root / "fake_nt" / file_row["entity_id"]
                / f"{file_row['file_id']}.genome_fasta.out.tsv")

    # -- fake Slurm backends: execute the payload locally -----------------

    def _fake_run(self, monkeypatch):
        def fake_run(_executor, argv, *, cwd, stdout_path, stderr_path, timeout=None,
                     threads=None, run_id=None, stage_inputs=(), expected_outputs=()):
            self.run_calls.append({"argv": [str(a) for a in argv], "run_id": run_id})
            Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
            with open(stdout_path, "w") as out, open(stderr_path, "w") as err:
                proc = subprocess.run([str(a) for a in argv], stdout=out, stderr=err, cwd=cwd)
            return ExecResult(exit_code=proc.returncode, scheduler_job_id=f"job-{run_id}")
        monkeypatch.setattr(SlurmExecutor, "run", fake_run)

    def _fake_run_array(self, monkeypatch):
        def fake_run_array(_executor, tasks, *, cwd=None, threads=None, timeout=None,
                           batch_id=None, array_concurrency=None):
            self.array_calls.append({
                "run_ids": [task["run_id"] for task in tasks],
                "array_concurrency": array_concurrency,
                "threads": threads,
            })
            results = []
            for index, task in enumerate(tasks, start=1):
                exitcode_path = Path(task["stdout_path"]).parent / f"{task['run_id']}.exitcode"
                with open(task["stdout_path"], "w") as out, open(task["stderr_path"], "w") as err:
                    proc = subprocess.run(shlex.split(task["command"]), stdout=out, stderr=err, cwd=cwd)
                exitcode_path.write_text(f"{proc.returncode}\n", encoding="utf-8")
                results.append(ExecResult(exit_code=proc.returncode,
                                          scheduler_job_id=f"7000_{index}"))
            return results
        monkeypatch.setattr(SlurmExecutor, "run_array", fake_run_array)

    def _jobs(self):
        return self.db.query("SELECT * FROM analysis_jobs ORDER BY job_id")

    def _runs(self):
        return self.db.query(
            "SELECT * FROM workflow_runs WHERE step='analysis:fake_nt' ORDER BY rowid")

    # -- tests -------------------------------------------------------------

    def test_array_submits_all_candidates_as_one_batch(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py",
                                slurm={"array": True, "array_concurrency": 3})
        rows = [self._add_assembly(n) for n in (1, 2, 3)]
        self._fake_run(monkeypatch)
        self._fake_run_array(monkeypatch)

        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm")

        self.assertEqual([r["status"] for r in results], ["completed"] * 3)
        # One array submission carried every candidate; the per-file path only
        # ever ran the version probe (run_id=None), never an analysis command.
        self.assertEqual(len(self.array_calls), 1)
        self.assertEqual(len(self.array_calls[0]["run_ids"]), 3)
        self.assertEqual(self.array_calls[0]["array_concurrency"], 3)
        self.assertFalse(any(call["run_id"] is not None for call in self.run_calls))
        jobs = self._jobs()
        self.assertEqual([j["status"] for j in jobs], ["completed"] * 3)
        # Each file keeps its own workflow run, with the per-task array job id.
        runs = self._runs()
        self.assertEqual(len(runs), 3)
        self.assertEqual([r["scheduler_job_id"] for r in runs], ["7000_1", "7000_2", "7000_3"])
        self.assertEqual({r["status"] for r in runs}, {"completed"})
        self.assertEqual({j["workflow_run_id"] for j in jobs}, {r["run_id"] for r in runs})
        for row in rows:
            self.assertEqual(self._output_path(row).read_text(), "q1\ts1\t99.0\t100\t1e-10\t500\n")

    def test_array_mixes_cache_hits_and_submissions(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        first = self._add_assembly(1)
        self._fake_run(monkeypatch)
        self._fake_run_array(monkeypatch)

        # One candidate: per-file fallback computes it (no array).
        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm")
        self.assertEqual([r["status"] for r in results], ["completed"])
        self.assertEqual(len(self.array_calls), 0)

        # Two more candidates: the cached file is skipped, the rest form one array.
        rows = [first, self._add_assembly(2), self._add_assembly(3)]
        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm")
        self.assertEqual([r["status"] for r in results],
                         ["cached", "completed", "completed"])
        self.assertEqual(len(self.array_calls), 1)
        self.assertEqual(len(self.array_calls[0]["run_ids"]), 2)
        jobs = self._jobs()
        self.assertEqual([j["status"] for j in jobs], ["completed"] * 3)
        self.assertEqual([j["file_id"] for j in jobs], [r["file_id"] for r in rows])

        # Array-produced results stay reusable when the recipe no longer uses
        # arrays: array participation is not part of the cache fingerprint.
        self._write_tool_config(self.root / "fakeblast.py")
        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm")
        self.assertEqual([r["status"] for r in results], ["cached"] * 3)
        self.assertEqual(len(self._jobs()), 3)

    def test_array_disabled_by_default_uses_per_file_path(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        self._add_assembly(1)
        self._add_assembly(2)
        self._fake_run(monkeypatch)
        self._fake_run_array(monkeypatch)

        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm")

        self.assertEqual([r["status"] for r in results], ["completed"] * 2)
        self.assertEqual(len(self.array_calls), 0)
        analysis_runs = [c for c in self.run_calls if c["run_id"] is not None]
        self.assertEqual(len(analysis_runs), 2)
        self.assertEqual([j["status"] for j in self._jobs()], ["completed"] * 2)

    def test_array_single_candidate_falls_back_to_per_file(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        self._add_assembly(1)
        self._fake_run(monkeypatch)
        self._fake_run_array(monkeypatch)

        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm")

        self.assertEqual([r["status"] for r in results], ["completed"])
        self.assertEqual(len(self.array_calls), 0)
        analysis_runs = [c for c in self.run_calls if c["run_id"] is not None]
        self.assertEqual(len(analysis_runs), 1)

    def test_executor_without_run_array_falls_back_to_per_file(self):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        self._add_assembly(1)
        self._add_assembly(2)

        # The local executor offers no run_array; the recipe opt-in is ignored.
        results = run_analysis(self.project, self.db, "fake_nt", backend="local")

        self.assertEqual([r["status"] for r in results], ["completed"] * 2)
        self.assertEqual(len(self.array_calls), 0)
        self.assertEqual([j["status"] for j in self._jobs()], ["completed"] * 2)

    def test_array_task_failure_marks_only_that_file_failed(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        rows = [self._add_assembly(n) for n in (1, 2, 3)]
        self._fake_run(monkeypatch)

        def positional_run_array(_executor, tasks, *, cwd=None, threads=None, timeout=None,
                                 batch_id=None, array_concurrency=None):
            self.array_calls.append({"run_ids": [t["run_id"] for t in tasks]})
            results = []
            for index, task in enumerate(tasks, start=1):
                exitcode_path = Path(task["stdout_path"]).parent / f"{task['run_id']}.exitcode"
                if index == 2:
                    Path(task["stderr_path"]).write_text("boom\n", encoding="utf-8")
                    exitcode_path.write_text("3\n", encoding="utf-8")
                    results.append(ExecResult(exit_code=3, scheduler_job_id=f"7000_{index}"))
                    continue
                with open(task["stdout_path"], "w") as out, open(task["stderr_path"], "w") as err:
                    proc = subprocess.run(shlex.split(task["command"]), stdout=out, stderr=err, cwd=cwd)
                exitcode_path.write_text(f"{proc.returncode}\n", encoding="utf-8")
                results.append(ExecResult(exit_code=proc.returncode,
                                          scheduler_job_id=f"7000_{index}"))
            return results

        monkeypatch.setattr(SlurmExecutor, "run_array", positional_run_array)

        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm")

        self.assertEqual([r["status"] for r in results],
                         ["completed", "error", "completed"])
        jobs = self._jobs()
        self.assertEqual([j["status"] for j in jobs], ["completed", "failed", "completed"])
        self.assertIn("exit code 3", jobs[1]["error"])
        runs = self._runs()
        self.assertEqual([r["status"] for r in runs], ["completed", "failed", "completed"])
        self.assertEqual(runs[1]["exit_code"], 3)
        # The failed file's output was never created; the others were parsed.
        self.assertFalse(self._output_path(rows[1]).exists())
        hits = self.db.query("SELECT DISTINCT file_id FROM analysis_hits")
        self.assertEqual({h["file_id"] for h in hits},
                         {rows[0]["file_id"], rows[2]["file_id"]})

    def test_array_interrupt_completes_finished_tasks_and_interrupts_the_rest(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        rows = [self._add_assembly(n) for n in (1, 2)]
        self._fake_run(monkeypatch)

        def interrupting_run_array(_executor, tasks, *, cwd=None, threads=None, timeout=None,
                                   batch_id=None, array_concurrency=None):
            # Task 1 finishes before the array is cancelled: its exit-code
            # file and output exist.  Task 2 never writes an exit code.
            task = tasks[0]
            with open(task["stdout_path"], "w") as out, open(task["stderr_path"], "w") as err:
                proc = subprocess.run(shlex.split(task["command"]), stdout=out, stderr=err, cwd=cwd)
            exitcode_path = Path(task["stdout_path"]).parent / f"{task['run_id']}.exitcode"
            exitcode_path.write_text(f"{proc.returncode}\n", encoding="utf-8")
            raise ShutdownRequested(signal.SIGINT)

        monkeypatch.setattr(SlurmExecutor, "run_array", interrupting_run_array)

        with pytest.raises(ShutdownRequested):
            run_analysis(self.project, self.db, "fake_nt", backend="slurm")

        jobs = self._jobs()
        self.assertEqual([j["status"] for j in jobs], ["completed", "interrupted"])
        self.assertIsNotNone(jobs[1]["finished_at"])
        runs = self._runs()
        # Only the finished task got a workflow_runs row — the in-flight task
        # of the sequential interrupt path gets none either.
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")
        self.assertEqual(jobs[0]["workflow_run_id"], runs[0]["run_id"])
        self.assertTrue(self._output_path(rows[0]).exists())
        self.assertFalse(self._output_path(rows[1]).exists())

        # Resume: the interrupted file is recomputed; the completed one caches.
        self._fake_run_array(monkeypatch)
        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm")
        self.assertEqual([r["status"] for r in results], ["cached", "completed"])
        self.assertEqual(len(self.array_calls), 0)  # single candidate: per-file
        self.assertEqual([j["status"] for j in self._jobs()],
                         ["completed", "interrupted", "completed"])

    def test_array_task_without_output_is_failed_not_completed(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        self._add_assembly(1)
        self._add_assembly(2)
        self._fake_run(monkeypatch)

        def lying_run_array(_executor, tasks, *, cwd=None, threads=None, timeout=None,
                            batch_id=None, array_concurrency=None):
            results = []
            for index, task in enumerate(tasks, start=1):
                if index == 1:
                    # Exit code 0 but the expected output was never produced.
                    Path(task["stdout_path"]).write_text("done\n", encoding="utf-8")
                    Path(task["stderr_path"]).write_text("", encoding="utf-8")
                    results.append(ExecResult(exit_code=0, scheduler_job_id=f"7000_{index}"))
                    continue
                with open(task["stdout_path"], "w") as out, open(task["stderr_path"], "w") as err:
                    proc = subprocess.run(shlex.split(task["command"]), stdout=out, stderr=err, cwd=cwd)
                results.append(ExecResult(exit_code=proc.returncode,
                                          scheduler_job_id=f"7000_{index}"))
            return results

        monkeypatch.setattr(SlurmExecutor, "run_array", lying_run_array)
        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm")

        self.assertEqual([r["status"] for r in results], ["error", "completed"])
        jobs = self._jobs()
        self.assertEqual([j["status"] for j in jobs], ["failed", "completed"])
        self.assertIn("expected output missing or empty", jobs[0]["error"])
        self.assertEqual([r["status"] for r in self._runs()], ["failed", "completed"])

    @pytest.mark.bug("ODR-0018")
    def test_array_progress_callback_cancellation_aborts_the_batch(self, monkeypatch):
        """A plain-Exception cancellation raised by the phase-3 progress
        callback (the TUI's AnalysisCancelled) must propagate with the
        completed tasks kept — never be swallowed as per-file failures."""
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        self._add_assembly(1)
        self._add_assembly(2)
        self._add_assembly(3)
        self._fake_run(monkeypatch)
        self._fake_run_array(monkeypatch)

        class Cancelled(Exception):
            pass

        phases: list[tuple[str, str]] = []

        def cancelling(index, total, file_id, phase):
            phases.append((file_id, phase))
            if phase == "completed":
                raise Cancelled()

        with pytest.raises(Cancelled):
            run_analysis(self.project, self.db, "fake_nt", backend="slurm",
                         progress_callback=cancelling)

        # The first task was collected before the cancellation; the remaining
        # tasks had already written their exit-code files, so they finalize
        # as completed instead of being misreported as failed.
        self.assertEqual([j["status"] for j in self._jobs()],
                         ["completed", "completed", "completed"])
        self.assertEqual([r["status"] for r in self._runs()],
                         ["completed", "completed", "completed"])

    @pytest.mark.bug("ODR-0018")
    def test_array_progress_callback_shutdown_interrupts_remaining_tasks(self, monkeypatch):
        """A KeyboardInterrupt subclass from the phase-3 callback keeps the
        interrupt path: uncollected tasks are interrupted, not failed."""
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        self._add_assembly(1)
        self._add_assembly(2)
        self._fake_run(monkeypatch)
        self._fake_run_array(monkeypatch)

        def cancelling(index, total, file_id, phase):
            if phase == "completed":
                raise ShutdownRequested(signal.SIGINT)

        with pytest.raises(ShutdownRequested):
            run_analysis(self.project, self.db, "fake_nt", backend="slurm",
                         progress_callback=cancelling)

        self.assertEqual([j["status"] for j in self._jobs()],
                         ["completed", "interrupted"])

    def test_array_forwards_cancel_event_to_supporting_executors(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        self._add_assembly(1)
        self._add_assembly(2)
        self._fake_run(monkeypatch)
        received: list = []

        def aware_run_array(_executor, tasks, *, cwd=None, threads=None, timeout=None,
                            batch_id=None, array_concurrency=None, cancel_event=None):
            received.append(cancel_event)
            results = []
            for index, task in enumerate(tasks, start=1):
                exitcode_path = Path(task["stdout_path"]).parent / f"{task['run_id']}.exitcode"
                with open(task["stdout_path"], "w") as out, open(task["stderr_path"], "w") as err:
                    proc = subprocess.run(shlex.split(task["command"]), stdout=out, stderr=err,
                                          cwd=cwd, check=False)
                exitcode_path.write_text(f"{proc.returncode}\n", encoding="utf-8")
                results.append(ExecResult(exit_code=proc.returncode,
                                          scheduler_job_id=f"7000_{index}"))
            return results

        monkeypatch.setattr(SlurmExecutor, "run_array", aware_run_array)
        cancel_event = threading.Event()
        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm",
                               cancel_event=cancel_event)

        self.assertEqual([r["status"] for r in results], ["completed", "completed"])
        self.assertEqual(received, [cancel_event])

    def test_array_collection_stops_at_task_boundary_on_cancel_event(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        self._add_assembly(1)
        self._add_assembly(2)
        self._add_assembly(3)
        self._fake_run(monkeypatch)
        # _fake_run_array has no cancel_event parameter, so the event is not
        # forwarded; the phase-3 collection boundary still honors it.
        self._fake_run_array(monkeypatch)

        cancel_event = threading.Event()

        def cancelling(index, total, file_id, phase):
            if phase == "completed":
                cancel_event.set()

        with pytest.raises(ShutdownRequested):
            run_analysis(self.project, self.db, "fake_nt", backend="slurm",
                         progress_callback=cancelling, cancel_event=cancel_event)

        # Task 1 was collected; the set event stops collection, so the
        # remaining plans are interrupted exactly as after a signal.
        self.assertEqual([j["status"] for j in self._jobs()],
                         ["completed", "interrupted", "interrupted"])

    def test_two_phase_planning_stops_on_cancel_event(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        self._add_assembly(1)
        self._add_assembly(2)
        self._fake_run(monkeypatch)
        self._fake_run_array(monkeypatch)

        cancel_event = threading.Event()

        def cancelling(index, total, file_id, phase):
            if phase == "start":
                cancel_event.set()

        with pytest.raises(ShutdownRequested):
            run_analysis(self.project, self.db, "fake_nt", backend="slurm",
                         progress_callback=cancelling, cancel_event=cancel_event)

        # File 1 was planned (RUNNING) before the event landed; file 2 was
        # never planned.  The stale RUNNING row is swept on the next run.
        self.assertEqual([j["status"] for j in self._jobs()], ["RUNNING"])
        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm")
        self.assertEqual([r["status"] for r in results], ["completed", "completed"])
        self.assertEqual([j["status"] for j in self._jobs()],
                         ["interrupted", "completed", "completed"])

    def test_two_phase_sequential_fallback_stops_on_cancel_event(self, monkeypatch):
        # A commands chain never joins an array; the two-phase path executes
        # it through the sequential fallback loop, which honors the event.
        self._write_fake_blast()
        recipe = {
            "entity_type": "assembly",
            "file_role": "genome_fasta",
            "format": "fasta",
            "output_subdir": "fake_nt",
            "output_suffix": ".out.tsv",
            "commands": [
                {"arguments": [str(self.root / "fakeblast.py"),
                               "-query", "${input}", "-out", "${output}"]},
                {"arguments": [str(self.root / "fakeblast.py"), "-version"]},
            ],
            "result_parser": "blast_tabular",
            "result_columns": ["qseqid", "sseqid", "pident", "length", "evalue", "bitscore"],
            "slurm": {"array": True},
        }
        tool_config = {
            "version": 1,
            "tools": {
                "fakeblast": {
                    "executable": str(self.root / "fakeblast.py"),
                    "run_method": sys.executable,
                    "version_args": ["-version"],
                    "version_pattern": r"fakeblast:\s*([^\s]+)",
                    "recipes": {"fake_nt": recipe},
                }
            },
        }
        self.project.tools_config_path.write_text(yaml.safe_dump(tool_config, sort_keys=False), encoding="utf-8")
        self._add_assembly(1)
        self._add_assembly(2)
        self._add_assembly(3)
        self._fake_run(monkeypatch)

        cancel_event = threading.Event()

        def cancelling(index, total, file_id, phase):
            if phase == "completed":
                cancel_event.set()

        with pytest.raises(ShutdownRequested):
            run_analysis(self.project, self.db, "fake_nt", backend="slurm",
                         progress_callback=cancelling, cancel_event=cancel_event)

        # File 1 completed through the fallback; the other planned rows stay
        # RUNNING until the next run's stale sweep.
        self.assertEqual([j["status"] for j in self._jobs()],
                         ["completed", "RUNNING", "RUNNING"])

    def test_array_planning_error_and_fallback_failure_are_per_file(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        missing = self._add_assembly(1)
        failing = self._add_assembly(2)
        (self.root / missing["relative_path"]).unlink()
        self._fake_run(monkeypatch)
        self._fake_run_array(monkeypatch)
        # The one computable file fails under the per-file fallback (fewer
        # than two array-eligible tasks remain after the planning error);
        # version probes (run_id=None) still succeed.
        probe_run = SlurmExecutor.run

        def failing_analysis_run(executor, argv, *, run_id=None, **kwargs):
            if run_id is None:
                return probe_run(executor, argv, run_id=run_id, **kwargs)
            return ExecResult(exit_code=7, error="exit code 7", scheduler_job_id=f"job-{run_id}")

        monkeypatch.setattr(SlurmExecutor, "run", failing_analysis_run)

        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm")

        self.assertEqual([r["status"] for r in results], ["error", "error"])
        self.assertIn("input missing", results[0]["error"])
        self.assertIn("exit code 7", results[1]["error"])
        jobs = self._jobs()
        # The planning failure never inserted a row; the executed file failed.
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["file_id"], failing["file_id"])
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertEqual(len(self.array_calls), 0)

    def test_array_recipe_dry_run_never_submits(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py", slurm={"array": True})
        self._add_assembly(1)
        self._add_assembly(2)
        self._fake_run(monkeypatch)
        self._fake_run_array(monkeypatch)

        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm", dry_run=True)

        self.assertEqual([r["status"] for r in results], ["planned", "planned"])
        self.assertEqual(len(self.array_calls), 0)
        self.assertFalse(any(call["run_id"] is not None for call in self.run_calls))
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"], 0)


class TestAnalysisArrayRemote(PytestAssertions):
    """Array wiring of the remote-Slurm transfer guarantees (staging/backups)."""

    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_ARRR_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)

    def _write_fake_blast(self) -> Path:
        script = self.root / "fakeblast.py"
        script.write_text(textwrap.dedent("""
            import sys
            args = sys.argv[1:]
            if '-version' in args:
                print('fakeblast: 9.8.7')
                raise SystemExit(0)
            out = args[args.index('-out') + 1]
            with open(out, 'w') as handle:
                handle.write('q1\\ts1\\t99.0\\t100\\t1e-10\\t500\\n')
        """).strip(), encoding="utf-8")
        return script

    def _write_tool_config(self, executable: Path):
        tool_config = {
            "version": 1,
            "tools": {
                "fakeblast": {
                    "executable": str(executable),
                    "run_method": sys.executable,
                    "version_args": ["-version"],
                    "version_pattern": r"fakeblast:\s*([^\s]+)",
                    "recipes": {
                        "fake_nt": {
                            "entity_type": "assembly",
                            "file_role": "genome_fasta",
                            "format": "fasta",
                            "output_subdir": "fake_nt",
                            "output_suffix": ".out.tsv",
                            "arguments": ["-query", "${input}", "-out", "${output}"],
                            "result_parser": "blast_tabular",
                            "result_columns": ["qseqid", "sseqid", "pident", "length", "evalue", "bitscore"],
                            "slurm": {"array": True, "array_concurrency": 2},
                        }
                    },
                }
            },
        }
        self.project.tools_config_path.write_text(yaml.safe_dump(tool_config, sort_keys=False), encoding="utf-8")

    def _add_assembly(self, number: int):
        self.db.insert_row("organisms", {"organism_id": f"ORG_{number:06d}", "scientific_name": "Testus", "taxonomy_source": "NCBI"})
        self.db.insert_row("samples", {"sample_id": f"SMP_{number:06d}", "organism_id": f"ORG_{number:06d}"})
        self.db.insert_row("assemblies", {"assembly_id": f"ASM_{number:06d}", "sample_id": f"SMP_{number:06d}", "assembly_level": "contig", "assembly_version": 1})
        fasta = self.root / f"asm{number}.fa"
        fasta.write_text(f">ctg{number}\n" + "ACGT" * 600 + "\n", encoding="utf-8")
        return ingest_file(self.db, self.project, fasta, "assembly", f"ASM_{number:06d}", "genome_fasta")

    def _output_path(self, file_row) -> Path:
        return (self.project.analysis_root / "fake_nt" / file_row["entity_id"]
                / f"{file_row['file_id']}.genome_fasta.out.tsv")

    def _executor(self, monkeypatch, **kwargs) -> _FakeRemoteSlurmExecutor:
        executor = _FakeRemoteSlurmExecutor(self.project, **kwargs)
        monkeypatch.setattr("operon.execution.get_executor", lambda *a, **k: executor)
        return executor

    def _jobs(self):
        return self.db.query("SELECT * FROM analysis_jobs ORDER BY job_id")

    def _runs(self):
        return self.db.query(
            "SELECT * FROM workflow_runs WHERE step='analysis:fake_nt' ORDER BY rowid")

    def _ops(self, executor, name):
        return [entry for entry in executor.transfer_log if entry[0] == name]

    def test_remote_array_stages_backs_up_pulls_and_drops(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        rows = [self._add_assembly(n) for n in (1, 2)]
        executor = self._executor(monkeypatch, concurrency=2)

        progress: list[tuple] = []
        results = run_analysis(self.project, self.db, "fake_nt", backend="ssh",
                               progress_callback=lambda *a: progress.append(a))

        self.assertEqual([r["status"] for r in results], ["completed"] * 2)
        self.assertEqual(len(executor.array_calls), 1)
        self.assertEqual(executor.array_calls[0]["array_concurrency"], 2)
        # Every input was staged and every remote output backed up BEFORE the
        # batch was submitted; after success each output was pulled and its
        # backup dropped; nothing was restored.
        self.assertEqual(len(self._ops(executor, "stage")), 2)
        self.assertEqual(len(self._ops(executor, "reset")), 2)
        self.assertEqual(len(self._ops(executor, "pull")), 2)
        self.assertEqual(len(self._ops(executor, "drop")), 2)
        self.assertEqual(self._ops(executor, "restore"), [])
        # Submission happens strictly after staging and backup.
        op_order = [op for op, _ in executor.transfer_log]
        self.assertEqual(op_order[:4], ["stage", "reset", "stage", "reset"])
        self.assertEqual([r["scheduler_job_id"] for r in self._runs()], ["9000_1", "9000_2"])
        self.assertTrue(executor.client.sftp.closed)
        self.assertTrue(executor.closed)
        # Progress: start + final status per file, in file order.
        self.assertEqual([p[2:] for p in progress if p[3] == "start"],
                         [(r["file_id"], "start") for r in rows])
        completed = [p for p in progress if p[3] == "completed"]
        self.assertEqual(len(completed), 2)

    def test_remote_array_failed_task_restores_its_backup(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        rows = [self._add_assembly(n) for n in (1, 2)]
        executor = self._executor(monkeypatch)

        def failing_impl(tasks, *, cwd):
            results = []
            for index, task in enumerate(tasks, start=1):
                exitcode = Path(task["stdout_path"]).parent / f"{task['run_id']}.exitcode"
                if index == 1:
                    Path(task["stderr_path"]).write_text("boom\n", encoding="utf-8")
                    exitcode.write_text("5\n", encoding="utf-8")
                    results.append(ExecResult(exit_code=5, scheduler_job_id=f"9000_{index}"))
                    continue
                proc = executor._execute(shlex.split(task["command"]), cwd,
                                         task["stdout_path"], task["stderr_path"])
                exitcode.write_text(f"{proc.returncode}\n", encoding="utf-8")
                results.append(ExecResult(exit_code=proc.returncode,
                                          scheduler_job_id=f"9000_{index}"))
            return results

        executor.run_array_impl = failing_impl
        results = run_analysis(self.project, self.db, "fake_nt", backend="ssh")

        self.assertEqual([r["status"] for r in results], ["error", "completed"])
        # The failed task's backup was restored, the successful one's dropped.
        failed_output = str(self._output_path(rows[0]))
        restored = self._ops(executor, "restore")
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0][1][0][0], failed_output)
        dropped = self._ops(executor, "drop")
        self.assertEqual(len(dropped), 1)
        self.assertNotEqual(dropped[0][1][0][0], failed_output)
        self.assertEqual([j["status"] for j in self._jobs()], ["failed", "completed"])

    def test_remote_array_interrupt_restores_unfinished_and_completes_finished(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        rows = [self._add_assembly(n) for n in (1, 2)]
        executor = self._executor(monkeypatch)

        def interrupting_impl(tasks, *, cwd):
            task = tasks[0]
            proc = executor._execute(shlex.split(task["command"]), cwd,
                                     task["stdout_path"], task["stderr_path"])
            exitcode = Path(task["stdout_path"]).parent / f"{task['run_id']}.exitcode"
            exitcode.write_text(f"{proc.returncode}\n", encoding="utf-8")
            raise ShutdownRequested(signal.SIGINT)

        executor.run_array_impl = interrupting_impl
        with pytest.raises(ShutdownRequested):
            run_analysis(self.project, self.db, "fake_nt", backend="ssh")

        self.assertEqual([j["status"] for j in self._jobs()], ["completed", "interrupted"])
        # Finished task: output pulled, backup dropped.  Unfinished task: backup restored.
        self.assertEqual(len(self._ops(executor, "pull")), 1)
        self.assertEqual(len(self._ops(executor, "drop")), 1)
        restored = self._ops(executor, "restore")
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0][1][0][0], str(self._output_path(rows[1])))
        self.assertFalse(self._output_path(rows[1]).exists())

    def test_remote_array_submission_failure_fails_every_planned_file(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        self._add_assembly(1)
        self._add_assembly(2)
        executor = self._executor(monkeypatch)

        def broken_impl(tasks, *, cwd):
            raise RemoteError("remote sbatch submission failed: denied")

        executor.run_array_impl = broken_impl
        results = run_analysis(self.project, self.db, "fake_nt", backend="ssh")

        self.assertEqual([r["status"] for r in results], ["error", "error"])
        self.assertIn("submission failed", results[0]["error"])
        self.assertEqual([j["status"] for j in self._jobs()], ["failed", "failed"])
        # No task ran, so every backed-up remote output was restored.
        self.assertEqual(len(self._ops(executor, "restore")), 2)
        self.assertEqual(self._ops(executor, "pull"), [])

    def test_remote_array_staging_failure_excludes_only_that_file(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        rows = [self._add_assembly(n) for n in (1, 2)]
        executor = self._executor(monkeypatch)
        # stage_inputs are built from the resolved project.root (config.py
        # resolves it); on macOS self.root keeps the /var symlink spelling.
        executor.fail_staging_for = {str(self.project.root / rows[0]["relative_path"])}

        results = run_analysis(self.project, self.db, "fake_nt", backend="ssh")

        self.assertEqual([r["status"] for r in results], ["error", "completed"])
        self.assertIn("cannot stage", results[0]["error"])
        self.assertEqual([j["status"] for j in self._jobs()], ["failed", "completed"])
        # The surviving task was still submitted as an array of one.
        self.assertEqual(len(executor.array_calls), 1)
        self.assertEqual(len(executor.array_calls[0]["run_ids"]), 1)

    def test_remote_array_interrupt_with_mixed_task_outcomes(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        rows = [self._add_assembly(n) for n in (1, 2, 3)]
        executor = self._executor(monkeypatch)

        def mixed_impl(tasks, *, cwd):
            # Task 1 succeeded, task 2 ran to a non-zero exit, task 3 never
            # wrote an exit code before the array was cancelled.
            first, second = tasks[0], tasks[1]
            proc = executor._execute(shlex.split(first["command"]), cwd,
                                     first["stdout_path"], first["stderr_path"])
            for task, code in ((first, proc.returncode), (second, 3)):
                exitcode = Path(task["stdout_path"]).parent / f"{task['run_id']}.exitcode"
                exitcode.write_text(f"{code}\n", encoding="utf-8")
            raise ShutdownRequested(signal.SIGINT)

        executor.run_array_impl = mixed_impl
        with pytest.raises(ShutdownRequested):
            run_analysis(self.project, self.db, "fake_nt", backend="ssh")

        self.assertEqual([j["status"] for j in self._jobs()],
                         ["completed", "failed", "interrupted"])
        runs = self._runs()
        self.assertEqual([r["status"] for r in runs], ["completed", "failed"])
        # Finished tasks: pulled; the failed one restored afterwards; the
        # unfinished one restored without any pull.
        self.assertEqual(len(self._ops(executor, "pull")), 1)
        restored_targets = {entry[1][0][0] for entry in self._ops(executor, "restore")}
        self.assertEqual(restored_targets, {str(self._output_path(rows[1])),
                                            str(self._output_path(rows[2]))})

    @pytest.mark.bug("ODR-0018")
    def test_remote_array_staging_callback_cancellation_aborts_the_batch(self, monkeypatch):
        """The same swallow existed at the staging-error callback: a
        cancellation raised there must propagate with the never-submitted
        plans interrupted, not failed."""
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        rows = [self._add_assembly(n) for n in (1, 2)]
        executor = self._executor(monkeypatch)
        executor.fail_staging_for = {str(self.project.root / rows[0]["relative_path"])}

        class Cancelled(Exception):
            pass

        def cancelling(index, total, file_id, phase):
            if phase == "error":
                raise Cancelled()

        with pytest.raises(Cancelled):
            run_analysis(self.project, self.db, "fake_nt", backend="ssh",
                         progress_callback=cancelling)

        self.assertEqual([j["status"] for j in self._jobs()], ["failed", "interrupted"])

    def test_remote_array_pull_failure_restores_backup_and_fails(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        rows = [self._add_assembly(n) for n in (1, 2)]
        executor = self._executor(monkeypatch)

        original_pull = executor._pull_outputs

        def flaky_pull(client, sftp, expected_outputs):
            if Path(expected_outputs[0]).name.startswith(str(rows[0]["file_id"])):
                raise RemoteError("sftp connection dropped mid-pull")
            return original_pull(client, sftp, expected_outputs)

        executor._pull_outputs = flaky_pull
        results = run_analysis(self.project, self.db, "fake_nt", backend="ssh")

        self.assertEqual([r["status"] for r in results], ["error", "completed"])  # TODO(Incompatible with Darwin): left = ['completed', 'completed'], right = ['error', 'completed']
        self.assertIn("mid-pull", results[0]["error"])
        self.assertEqual([j["status"] for j in self._jobs()], ["failed", "completed"])
        restored_targets = {entry[1][0][0] for entry in self._ops(executor, "restore")}
        self.assertEqual(restored_targets, {str(self._output_path(rows[0]))})
        self.assertEqual(len(self._ops(executor, "drop")), 1)
