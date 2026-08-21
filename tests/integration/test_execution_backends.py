"""End-to-end execution backend tests using fake scheduler binaries on PATH."""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
from pathlib import Path

from tests.helpers import PytestAssertions

import yaml

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import ingest_file
from operon.tools import run_analysis


FAKE_SBATCH = """\
#!/usr/bin/env bash
# Fake sbatch: run the script synchronously, honoring --output/--error like Slurm.
script="${!#}"
out="$(grep -m1 '^#SBATCH --output=' "$script" | cut -d= -f2-)"
err="$(grep -m1 '^#SBATCH --error=' "$script" | cut -d= -f2-)"
bash "$script" > "$out" 2> "$err"
echo "12345"
"""

FAKE_SQUEUE = """\
#!/usr/bin/env bash
# Fake squeue: the queue is always empty (every job finished).
exit 0
"""


class TestSlurmBackend(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_SLURM_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        self.bin_dir = self.root / "fakebin"
        self.bin_dir.mkdir()
        for name, content in (("sbatch", FAKE_SBATCH), ("squeue", FAKE_SQUEUE)):
            script = self.bin_dir / name
            script.write_text(content, encoding="utf-8")
            script.chmod(0o755)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{self._old_path}"
        self.addCleanup(self._restore_path)

    def _restore_path(self):
        os.environ["PATH"] = self._old_path

    def _write_fake_tool(self) -> Path:
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
                            **({"slurm": slurm} if slurm else {}),
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

    def test_analyze_via_slurm_backend(self):
        self._write_fake_tool()
        self._write_tool_config(self.root / "fakeblast.py")
        self._add_assembly()
        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm", threads=2)
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["tool_version"], "9.8.7")
        self.assertEqual(result["hit_count"], 1)
        job = self.db.conn.execute(
            "SELECT launcher, status FROM analysis_jobs WHERE analysis_name='fake_nt'"
        ).fetchone()
        self.assertEqual(job["status"], "completed")
        self.assertIn("[slurm]", job["launcher"])
        run = self.db.conn.execute(
            "SELECT threads, command FROM workflow_runs WHERE step='analysis:fake_nt'"
        ).fetchone()
        self.assertEqual(run["threads"], 2)
        # The generated batch script and exit code file are provenance artifacts.
        sbatch_scripts = list(self.project.logs_root.glob("*.sbatch"))
        self.assertTrue(sbatch_scripts)
        self.assertIn("#SBATCH --cpus-per-task=2", sbatch_scripts[0].read_text())

    def test_recipe_slurm_overrides_apply_to_actual_analysis_job(self):
        self._write_fake_tool()
        self._write_tool_config(
            self.root / "fakeblast.py", {"time": "02:03:04", "mem_gb": 23, "partition": "science"},
        )
        self._add_assembly()
        results = run_analysis(self.project, self.db, "fake_nt", backend="slurm", threads=3)
        self.assertEqual(results[0]["status"], "completed", results[0].get("error"))
        analysis_runs = self.db.conn.execute(
            "SELECT run_id FROM workflow_runs WHERE step='analysis:fake_nt'"
        ).fetchall()
        scripts = [self.project.logs_root / f"{row['run_id']}.sbatch" for row in analysis_runs]
        script = next(path for path in scripts if path.exists()).read_text(encoding="utf-8")
        self.assertIn("#SBATCH --time=02:03:04", script)
        self.assertIn("#SBATCH --mem=23G", script)
        self.assertIn("#SBATCH --partition=science", script)

    def test_run_external_cli_with_slurm_backend(self):
        rc = main([
            "--project", str(self.root), "run-external",
            "--step", "slurm-smoke", "--backend", "slurm",
            "--command", "bash -c 'echo cluster > out.txt'",
            "--expected-output", "out.txt",
        ])
        self.assertEqual(rc, 0)
        self.assertEqual((self.root / "out.txt").read_text().strip(), "cluster")

    def test_analyze_cli_backend_flag(self):
        self._write_fake_tool()
        self._write_tool_config(self.root / "fakeblast.py")
        self._add_assembly()
        rc = main(["--project", str(self.root), "analyze", "--analysis", "fake_nt", "--backend", "slurm"])
        self.assertEqual(rc, 0)

    def test_slurm_requires_sbatch(self):
        # Point PATH at an empty directory so sbatch/squeue are missing.
        os.environ["PATH"] = str(self.root / "emptybin")
        (self.root / "emptybin").mkdir()
        from operon.workflow import run_external_command
        with self.assertRaises(RuntimeError):
            run_external_command(self.db, self.project, ["true"], step="x", backend="slurm")
        row = self.db.conn.execute("SELECT status, error FROM workflow_runs WHERE step='x'").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("sbatch", row["error"])
