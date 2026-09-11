"""End-to-end fanout -> analyze: per-unit FASTAs selected by file_role_prefix."""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
from pathlib import Path

import yaml

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import ingest_file


class TestFanoutAnalyze(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root),
                               "--project-id", "PRJ_FANOUT_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        self.db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Testus",
            "taxonomy_source": "NCBI"})
        self.db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        self.db.insert_row("assemblies", {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_level": "contig", "assembly_version": 1})
        self.db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
            "annotation_source": "test", "annotation_version": 1})
        proteins = self.root / "proteins.faa"
        proteins.write_text(
            "".join(f">b{index}\n{'M' * 50}A\n" for index in range(1, 7)),
            encoding="utf-8")
        self.source = ingest_file(
            self.db, self.project, proteins, "annotation", "ANN_000001", "protein_fasta")
        # QC populates the sequences registry the fanout resolves against.
        self.assertEqual(main([
            "--project", str(self.root), "qc",
            "--entity-type", "annotation", "--entity-id", "ANN_000001"]), 0)
        assignments = self.root / "subfamilies.tsv"
        assignments.write_text(
            "unit\tseqid\nSF01\tb1\nSF01\tb2\nSF02\tb3\nSF02\tb4\nSF02\tb5\n",
            encoding="utf-8")
        self.assignments = ingest_file(
            self.db, self.project, assignments, "annotation", "ANN_000001",
            "subfamily_assignments", fmt="tsv", compression="none")

    def _write_fake_tool(self):
        script = self.root / "faketree.py"
        script.write_text(textwrap.dedent("""
            import sys
            args = sys.argv[1:]
            if '-version' in args:
                print('faketree: 2.0.1')
                raise SystemExit(0)
            out = args[args.index('-out') + 1]
            query = args[args.index('-query') + 1]
            with open(out, 'w') as handle:
                handle.write('tree for ' + query + '\\n')
        """).strip(), encoding="utf-8")
        tool_config = {
            "version": 1,
            "conda": {"bin": "conda", "run_args": ["run", "--no-capture-output"]},
            "tools": {
                "faketree": {
                    "description": "fake tree tool for fanout tests",
                    "executable": str(script),
                    "run_method": sys.executable,
                    "version_args": ["-version"],
                    "version_pattern": r"faketree:\s*([^\s]+)",
                    "recipes": {
                        "subfamily_tree": {
                            "description": "one job per fanned-out subfamily FASTA",
                            "entity_type": "annotation",
                            "file_role_prefix": "subfamily_alignment:",
                            "format": "fasta",
                            "output_subdir": "subfamily_tree",
                            "output_suffix": ".tree",
                            "arguments": ["-query", "${input}", "-out", "${output}"],
                            "result_parser": "none",
                        }
                    },
                }
            },
        }
        self.project.tools_config_path.write_text(
            yaml.safe_dump(tool_config, sort_keys=False), encoding="utf-8")

    def test_fanout_then_analyze_selects_exactly_the_unit_files(self):
        self.assertEqual(main([
            "--project", str(self.root), "fanout",
            "--assignments-file", self.assignments["file_id"],
            "--source-file", self.source["file_id"],
            "--entity-type", "annotation", "--entity-id", "ANN_000001",
            "--role-prefix", "subfamily_alignment",
        ]), 0)

        unit_rows = self.db.query(
            "SELECT * FROM files WHERE file_role LIKE 'subfamily_alignment:%' "
            "ORDER BY file_role")
        self.assertEqual(
            [row["file_role"] for row in unit_rows],
            ["subfamily_alignment:SF01", "subfamily_alignment:SF02"])
        for row in unit_rows:
            self.assertTrue(row["relative_path"].startswith("analysis/derived/ANN_000001/"))
            edges = self.db.query(
                "SELECT input_file_id, workflow_run_id FROM file_lineage "
                "WHERE derived_file_id=?", (row["file_id"],))
            self.assertEqual(
                {edge["input_file_id"] for edge in edges},
                {self.source["file_id"], self.assignments["file_id"]})
            self.assertTrue(edges[0]["workflow_run_id"])
        runs = self.db.query("SELECT * FROM workflow_runs WHERE step='fanout'")
        self.assertEqual(len(runs), 1)
        details = json.loads(runs[0]["execution_details"])
        self.assertEqual(
            {unit["unit"]: unit["sequences"] for unit in details["units"]},
            {"SF01": 2, "SF02": 3})

        self._write_fake_tool()
        self.assertEqual(main([
            "--project", str(self.root), "analyze", "--analysis", "subfamily_tree"]), 0)
        jobs = self.db.query("SELECT * FROM analysis_jobs ORDER BY job_id")
        self.assertEqual(len(jobs), 2)
        self.assertEqual(
            sorted(job["file_id"] for job in jobs),
            sorted(row["file_id"] for row in unit_rows))
        self.assertTrue(all(job["status"] == "completed" for job in jobs))
        for job in jobs:
            self.assertTrue(
                (self.root / job["output_relative_path"]).is_file(),
                f"missing analysis output for {job['file_id']}")
