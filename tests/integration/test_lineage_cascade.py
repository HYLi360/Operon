"""End-to-end cascading workflow: export -> external workflow -> adopt -> analyze."""

from __future__ import annotations

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


class TestLineageCascade(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root)]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        self.db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Testus"})
        self.db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        self.db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
        fasta = self.root / "genome.fa"
        fasta.write_text(">ctg1\n" + "ACGT" * 600 + "\n", encoding="utf-8")
        self.genome = ingest_file(self.db, self.project, fasta, "assembly", "ASM_000001", "genome_fasta")

    def _write_fake_tool(self, name: str, version: str) -> Path:
        script = self.root / f"{name}.py"
        script.write_text(textwrap.dedent(f"""
            import sys
            args = sys.argv[1:]
            if '-version' in args:
                print('{name}: {version}')
                raise SystemExit(0)
            source = args[args.index('--in') + 1]
            out = args[args.index('--out') + 1]
            with open(source) as handle:
                data = handle.read()
            with open(out, 'w') as handle:
                handle.write('tool={name}\\n')
                handle.write('input_bytes=' + str(len(data)) + '\\n')
        """).strip(), encoding="utf-8")
        return script

    def _write_tools_yaml(self):
        document = {
            "version": 1,
            "tools": {
                "fakecount": {
                    "description": "counts input bytes (upstream recipe)",
                    "executable": str(self._write_fake_tool("fakecount", "1.0")),
                    "run_method": sys.executable,
                    "version_args": ["-version"],
                    "version_pattern": r"fakecount:\s*([^\s]+)",
                    "recipes": {
                        "count_bases": {
                            "version": 1,
                            "entity_type": "assembly",
                            "file_role": "genome_fasta",
                            "format": "fasta",
                            "output_subdir": "count_bases",
                            "output_suffix": ".counts.tsv",
                            "arguments": ["--in", "${input}", "--out", "${output}"],
                            "result_parser": "none",
                        },
                    },
                },
                "fakesummary": {
                    "description": "summarizes a derived table (downstream recipe)",
                    "executable": str(self._write_fake_tool("fakesummary", "2.0")),
                    "run_method": sys.executable,
                    "version_args": ["-version"],
                    "version_pattern": r"fakesummary:\s*([^\s]+)",
                    "recipes": {
                        "summarize_matrix": {
                            "version": 3,
                            "entity_type": "assembly",
                            "file_role": "pangenome_matrix",
                            "format": "tsv",
                            "output_subdir": "summarize_matrix",
                            "output_suffix": ".summary.tsv",
                            "arguments": ["--in", "${input}", "--out", "${output}"],
                            "result_parser": "none",
                        },
                    },
                },
            },
        }
        self.project.tools_config_path.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def test_export_adopt_analyze_cascade(self):
        self._write_tools_yaml()

        # QC + evaluate the raw assembly, then run the upstream recipe.
        self.assertEqual(
            main(["--project", str(self.root), "qc", "--file-id", self.genome["file_id"]]), 0)
        self.assertEqual(
            main(["--project", str(self.root), "evaluate", "--entity-type", "assembly",
                  "--entity-id", "ASM_000001"]), 0)
        self.assertEqual(
            main(["--project", str(self.root), "analyze", "--analysis", "count_bases"]), 0)
        upstream_job = self.db.query("SELECT * FROM analysis_jobs WHERE analysis_name='count_bases'")[0]
        self.assertEqual(upstream_job["status"], "completed")
        self.assertIsNotNone(upstream_job["recipe_snapshot_id"])

        # Export the raw inputs, then simulate an external workflow manager
        # consuming the export plus the upstream analysis output.
        export_dir = self.root / "handoff"
        self.assertEqual(main(["--project", str(self.root), "export",
                               "--output", str(export_dir), "--entity-type", "assembly"]), 0)
        self.assertTrue((export_dir / "manifest.tsv").is_file())
        workflow_out = self.root / "external_run"
        workflow_out.mkdir()
        derived = workflow_out / "matrix.tsv"
        upstream_output = self.root / upstream_job["output_relative_path"]
        derived.write_text(
            "id\tcount\n" + f"ASM_000001\t{len(upstream_output.read_text())}\n",
            encoding="utf-8",
        )

        # Adopt the external workflow's outputs back into the manifest.
        manifest = workflow_out / "adopt_manifest.tsv"
        manifest.write_text(
            "path\tentity_type\tentity_id\trole\tformat\tcompression\tderived_from\tworkflow_run_id\n"
            f"{derived}\tassembly\tASM_000001\tpangenome_matrix\ttsv\tnone\t"
            f"{self.genome['file_id']}\tWF_EXTERNAL_42\n",
            encoding="utf-8",
        )
        self.assertEqual(
            main(["--project", str(self.root), "adopt", "--from-manifest", str(manifest)]), 0)
        edges = self.db.query("SELECT * FROM file_lineage")
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["input_file_id"], self.genome["file_id"])
        self.assertEqual(edges[0]["workflow_run_id"], "WF_EXTERNAL_42")
        adopted_file_id = edges[0]["derived_file_id"]

        # The downstream recipe cascades onto the adopted artifact.
        self.assertEqual(
            main(["--project", str(self.root), "analyze", "--analysis", "summarize_matrix"]), 0)
        downstream_jobs = self.db.query(
            "SELECT * FROM analysis_jobs WHERE analysis_name='summarize_matrix'")
        self.assertEqual(len(downstream_jobs), 1)
        downstream = downstream_jobs[0]
        self.assertEqual(downstream["status"], "completed")
        self.assertEqual(downstream["file_id"], adopted_file_id)
        self.assertIsNotNone(downstream["recipe_snapshot_id"])

        snapshots = self.db.query("SELECT * FROM recipe_snapshots ORDER BY recipe_snapshot_id")
        self.assertEqual({row["recipe_name"] for row in snapshots},
                         {"count_bases", "summarize_matrix"})
        versions = {row["recipe_name"]: row["recipe_version"] for row in snapshots}
        self.assertEqual(versions, {"count_bases": 1, "summarize_matrix": 3})

        # The adopt step is auditable in workflow_runs.
        adopt_runs = self.db.query("SELECT * FROM workflow_runs WHERE step='adopt'")
        self.assertEqual(len(adopt_runs), 1)
        details = json.loads(adopt_runs[0]["execution_details"])
        self.assertEqual(details["items"][0]["file_id"], adopted_file_id)
