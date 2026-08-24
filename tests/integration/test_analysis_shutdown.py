"""Graceful shutdown of `operon analyze`: bookkeeping, partial outputs, resume."""

from __future__ import annotations

import signal
import sys
import tempfile
import textwrap
from pathlib import Path

from tests.helpers import PytestAssertions

import yaml

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.execution import LocalExecutor
from operon.files import ingest_file
from operon.shutdown import ShutdownRequested


class TestAnalysisShutdown(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_ANSH_001"]), 0)
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
                            "database": "",
                            "output_subdir": "fake_nt",
                            "output_suffix": ".out.tsv",
                            "arguments": ["-query", "${input}", "-out", "${output}", "-num_threads", "${threads}"],
                            "result_parser": "blast_tabular",
                            "result_columns": ["qseqid", "sseqid", "pident", "length", "evalue", "bitscore"],
                        }
                    },
                }
            },
        }
        self.project.tools_config_path.write_text(yaml.safe_dump(tool_config, sort_keys=False), encoding="utf-8")

    def _add_assembly(self):
        self.db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Testus", "taxonomy_source": "NCBI"})
        self.db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        self.db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001", "assembly_level": "contig", "assembly_version": 1})
        fasta = self.root / "asm.fa"
        fasta.write_text(">ctg1\n" + "ACGT" * 600 + "\n", encoding="utf-8")
        return ingest_file(self.db, self.project, fasta, "assembly", "ASM_000001", "genome_fasta")

    def _interrupting_run(self, file_id: str):
        """Fake executor.run: leave a partial output, then raise the signal."""
        def fake_run(_executor, argv, *, cwd, stdout_path, stderr_path, timeout=None,
                     threads=None, run_id=None, stage_inputs=(), expected_outputs=()):
            Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
            Path(stdout_path).write_text("partial stdout\n", encoding="utf-8")
            for path in expected_outputs:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text("partial output\n", encoding="utf-8")
            raise ShutdownRequested(signal.SIGINT)
        return fake_run

    def _output_path(self, file_id: str) -> Path:
        return (self.project.analysis_root / "fake_nt" / "ASM_000001"
                / f"{file_id}.genome_fasta.out.tsv")

    def test_interrupt_finalizes_job_removes_partial_and_resumes(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        file_row = self._add_assembly()
        output = self._output_path(file_row["file_id"])

        monkeypatch.setattr(LocalExecutor, "run", self._interrupting_run(file_row["file_id"]))
        rc = main(["--project", str(self.root), "analyze", "--analysis", "fake_nt"])
        self.assertEqual(rc, 130)

        jobs = self.db.query("SELECT * FROM analysis_jobs")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "interrupted")
        self.assertIsNotNone(jobs[0]["finished_at"])
        # The partial output artifact is removed; the log files stay for diagnosis.
        self.assertFalse(output.exists())
        self.assertTrue(list(self.project.logs_root.glob("*.stdout.log")))

        # Resume: the interrupted job is not cached, the re-run completes.
        monkeypatch.undo()
        rc = main(["--project", str(self.root), "analyze", "--analysis", "fake_nt"])
        self.assertEqual(rc, 0)
        jobs = self.db.query("SELECT * FROM analysis_jobs ORDER BY job_id")
        self.assertEqual([j["status"] for j in jobs], ["interrupted", "completed"])
        self.assertEqual(output.read_text(), "q1\ts1\t99.0\t100\t1e-10\t500\n")

    def test_keep_partial_retains_interrupted_output(self, monkeypatch):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        file_row = self._add_assembly()
        output = self._output_path(file_row["file_id"])

        monkeypatch.setattr(LocalExecutor, "run", self._interrupting_run(file_row["file_id"]))
        rc = main(["--project", str(self.root), "analyze", "--analysis", "fake_nt", "--keep-partial"])
        self.assertEqual(rc, 130)
        jobs = self.db.query("SELECT * FROM analysis_jobs")
        self.assertEqual(jobs[0]["status"], "interrupted")
        self.assertEqual(output.read_text(), "partial output\n")

    def test_stale_running_jobs_are_swept_on_startup(self):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        file_row = self._add_assembly()
        # Simulate a job row orphaned by a SIGKILLed process.
        self.db.conn.execute(
            "INSERT INTO analysis_jobs (analysis_name, entity_type, entity_id, file_id, tool, "
            "tool_version, parameter_set, parameter_sha256, input_sha256, database_identity, "
            "status, started_at) VALUES ('fake_nt', 'assembly', 'ASM_000001', ?, 'fakeblast', "
            "'9.8.7', '{}', 'deadbeef', 'cafebabe', 'test', 'RUNNING', '2026-01-01T00:00:00Z')",
            (file_row["file_id"],),
        )
        self.db.conn.commit()

        rc = main(["--project", str(self.root), "analyze", "--analysis", "fake_nt"])
        self.assertEqual(rc, 0)
        jobs = self.db.query("SELECT status FROM analysis_jobs ORDER BY job_id")
        self.assertEqual([j["status"] for j in jobs], ["interrupted", "completed"])
