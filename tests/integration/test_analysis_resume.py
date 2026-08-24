"""Two-tier resume: exact cache hits and verified-output adoption."""

from __future__ import annotations

import hashlib
import json
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
from operon.tools import database_identity, get_recipe, run_analysis


class TestAnalysisResume(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_RSM_001"]), 0)
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

    def _write_tool_config(self, executable: Path, extra_args: list[str] | None = None):
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
                            "arguments": ["-query", "${input}", "-out", "${output}"]
                            + list(extra_args or []),
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

    def test_database_identity_matches_pre_backend_digest(self):
        # Regression: adding the execution-location key must not change the
        # digest for local runs, or every pre-upgrade cache row is invalidated.
        self._write_tool_config(self.root / "fakeblast.py")
        recipe = get_recipe(self.project, "fake_nt")
        identity = database_identity(self.project, recipe)
        legacy_canonical = {
            "path": "",
            "digest": "none",
            "database_version": recipe.database_version,
            "database_mode": "reference",
        }
        legacy = hashlib.sha256(
            json.dumps(legacy_canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self.assertEqual(identity, legacy)
        # A non-empty location still distinguishes staged remote databases.
        self.assertNotEqual(identity, database_identity(self.project, recipe, "ssh:host"))

    def test_verified_output_is_adopted_after_fingerprint_change(self):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        self._add_assembly()
        results = run_analysis(self.project, self.db, "fake_nt")
        self.assertEqual(results[0]["status"], "completed", results[0].get("error"))

        # Recipe edit changes the parameter fingerprint; input and the
        # on-disk output are untouched, so the result must be adopted.
        self._write_tool_config(self.root / "fakeblast.py", extra_args=["--soft-mask"])
        results = run_analysis(self.project, self.db, "fake_nt")
        self.assertEqual(results[0]["status"], "adopted")
        self.assertTrue(results[0]["cached"])

        jobs = self.db.query("SELECT * FROM analysis_jobs ORDER BY job_id")
        self.assertEqual(len(jobs), 2)
        self.assertEqual([j["status"] for j in jobs], ["completed", "completed"])
        # The adopted row links back to the original run and output.
        self.assertEqual(jobs[1]["workflow_run_id"], jobs[0]["workflow_run_id"])
        self.assertEqual(jobs[1]["output_relative_path"], jobs[0]["output_relative_path"])
        self.assertEqual(jobs[1]["output_sha256"], jobs[0]["output_sha256"])
        self.assertNotEqual(jobs[1]["parameter_sha256"], jobs[0]["parameter_sha256"])
        audit = self.db.query("SELECT * FROM changes WHERE object_type='analysis_job'")
        self.assertEqual(len(audit), 1)
        self.assertIn("adopted verified output from job 1", audit[0]["reason"])

        # The adopted row is a normal exact cache hit from now on.
        results = run_analysis(self.project, self.db, "fake_nt")
        self.assertEqual(results[0]["status"], "cached")
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"], 2)

    def test_modified_output_is_recomputed_not_adopted(self):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        file_row = self._add_assembly()
        results = run_analysis(self.project, self.db, "fake_nt")
        self.assertEqual(results[0]["status"], "completed", results[0].get("error"))

        output = (self.project.analysis_root / "fake_nt" / "ASM_000001"
                  / f"{file_row['file_id']}.genome_fasta.out.tsv")
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("tampered\n")

        self._write_tool_config(self.root / "fakeblast.py", extra_args=["--soft-mask"])
        results = run_analysis(self.project, self.db, "fake_nt")
        self.assertEqual(results[0]["status"], "completed", results[0].get("error"))
        self.assertEqual(output.read_text(), "q1\ts1\t99.0\t100\t1e-10\t500\n")
        jobs = self.db.query("SELECT status FROM analysis_jobs ORDER BY job_id")
        self.assertEqual([j["status"] for j in jobs], ["completed", "completed"])
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM changes")[0]["n"], 0)

    def test_dry_run_reports_adoptable_candidates(self):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        self._add_assembly()
        run_analysis(self.project, self.db, "fake_nt")
        self._write_tool_config(self.root / "fakeblast.py", extra_args=["--soft-mask"])
        results = run_analysis(self.project, self.db, "fake_nt", dry_run=True)
        self.assertFalse(results[0]["cached"])
        self.assertTrue(results[0]["adoptable"])
        self.assertEqual(results[0]["status"], "adoptable")
        self.assertTrue(results[0]["output"].endswith(".out.tsv"))

    def test_dry_run_reports_planned_and_cached_status(self):
        self._write_fake_blast()
        self._write_tool_config(self.root / "fakeblast.py")
        self._add_assembly()
        results = run_analysis(self.project, self.db, "fake_nt", dry_run=True)
        self.assertEqual(results[0]["status"], "planned")
        self.assertTrue(results[0]["output"].endswith(".out.tsv"))
        self.assertFalse(results[0]["cached"])
        self.assertFalse(results[0]["adoptable"])

        run_analysis(self.project, self.db, "fake_nt")
        results = run_analysis(self.project, self.db, "fake_nt", dry_run=True)
        self.assertEqual(results[0]["status"], "cached")
        # --force supersedes the cache, so the dry run plans a re-run.
        results = run_analysis(self.project, self.db, "fake_nt", dry_run=True, force=True)
        self.assertEqual(results[0]["status"], "planned")
