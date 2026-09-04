"""End-to-end demo pipeline, file identity and release tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ConflictError
from operon.files import ingest_file
from operon.release import create_release
from operon.utils import sha256_file


class TestPipelineAndRelease(PytestAssertions):
    def test_demo_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["--project", str(root), "init-demo", str(root), "--project-id", "PRJ_E2E_001"]), 0)
            project = load_project(root)
            db = Database(project.db_path)
            try:
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM files")[0]["n"], 9)
                decisions = {f"{r['entity_type']}:{r['entity_id']}": r for r in db.query("SELECT * FROM decisions")}
                self.assertEqual(decisions["assembly:ASM_000002"]["decision"], "FAIL")
                self.assertEqual(decisions["annotation:ANN_000003"]["decision"], "FAIL")
                self.assertEqual(decisions["run:RUN_000001"]["decision"], "PASS")
                manifest = db.query("SELECT * FROM release_members")
                self.assertEqual(len(manifest), 2)
                release_dir = root / "releases" / "2026.08.demo"
                self.assertTrue((release_dir / "provenance.json").exists())
                checksums = (release_dir / "checksums.sha256").read_text(encoding="utf-8")
                for line in checksums.strip().splitlines():
                    digest, rel = line.split("  ", 1)
                    self.assertEqual(sha256_file(release_dir / rel), digest)
                release_readme = (release_dir / "README.md").read_text(encoding="utf-8")
                self.assertIn("sha256sum -c checksums.sha256", release_readme)
                self.assertIn("shasum -a 256 -c checksums.sha256", release_readme)
            finally:
                db.close()

    def test_idempotent_ingest_and_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["--project", str(root), "init", str(root)]), 0)
            project = load_project(root)
            db = Database(project.db_path)
            try:
                db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X", "taxonomy_source": "NCBI"})
                db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
                db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001", "assembly_level": "contig", "assembly_version": 1})
                source = root / "genome.fa"
                source.write_text(">ctg1\nACGTACGT\n", encoding="utf-8")
                first = ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
                second = ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
                self.assertEqual(first["file_id"], second["file_id"])
                # Same target path but different bytes must never be silently overwritten.
                source.write_text(">ctg1\nTTTTTTTT\n", encoding="utf-8")
                target = project.root / first["relative_path"]
                target.unlink()
                with self.assertRaises(ConflictError):
                    ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
            finally:
                db.close()

    def test_run_external_records_provenance(self):
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["--project", str(root), "init", str(root)]), 0)
            expected = root / "external.out"
            command1 = sys.executable + " -c 'print(42)'"
            self.assertEqual(main(["--project", str(root), "run-external", "--step", "selftest",
                                   "--command", command1, "--expected-output", str(expected)]), 1)
            command2 = sys.executable + f" -c 'import pathlib; pathlib.Path(\"{expected}\").write_text(\"ok\")'"
            self.assertEqual(main(["--project", str(root), "run-external", "--step", "selftest_ok",
                                   "--command", command2]), 0)
            db = Database(root / "operon.sqlite")
            try:
                rows = db.query("SELECT * FROM workflow_runs WHERE step LIKE 'selftest%' ORDER BY run_id")
                self.assertGreaterEqual(len(rows), 2)
            finally:
                db.close()

    def test_export_end_to_end(self):
        import csv
        import json as json_module
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["--project", str(root), "init", str(root)]), 0)
            project = load_project(root)
            db = Database(project.db_path)
            try:
                db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X", "taxonomy_source": "NCBI"})
                db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
                db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001", "assembly_level": "contig", "assembly_version": 1})
                source = root / "genome.fa"
                source.write_text(">ctg1\n" + "A" * 2000 + "\n", encoding="utf-8")
                row = ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
            finally:
                db.close()
            self.assertEqual(main(["--project", str(root), "qc", "--file-id", row["file_id"]]), 0)
            self.assertEqual(main([
                "--project", str(root), "evaluate", "--entity-type", "assembly",
                "--entity-id", "ASM_000001", "--profile", "assembly_production_v1",
            ]), 0)
            out = root / "exported"
            self.assertEqual(main([
                "--project", str(root), "export", "--output", str(out),
                "--decision", "pass", "--profile", "assembly_production_v1",
            ]), 0)
            with open(out / "manifest.tsv", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["file_id"], row["file_id"])
            exported = out / rows[0]["export_relative_path"]
            self.assertEqual(exported.read_bytes(), source.read_bytes())
            self.assertEqual(sha256_file(exported), rows[0]["sha256"])
            provenance = json_module.loads((out / "provenance.json").read_text(encoding="utf-8"))
            self.assertEqual(provenance["selection"]["decision"], "pass")
            self.assertEqual(provenance["file_count"], 1)
            db = Database(project.db_path)
            try:
                runs = db.query("SELECT * FROM workflow_runs WHERE step='export'")
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0]["output_sha256"], provenance["manifest_sha256"])
            finally:
                db.close()

    def test_verify_detects_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(main(["--project", str(root), "init", str(root)]), 0)
            project = load_project(root)
            db = Database(project.db_path)
            try:
                db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X", "taxonomy_source": "NCBI"})
                db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
                db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001", "assembly_level": "contig", "assembly_version": 1})
                source = root / "genome.fa"
                source.write_text(">ctg1\nACGT\n", encoding="utf-8")
                row = ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
                (project.root / row["relative_path"]).write_text(">ctg1\nTGCA\n", encoding="utf-8")
                self.assertEqual(main(["--project", str(root), "verify"]), 1)
            finally:
                db.close()
