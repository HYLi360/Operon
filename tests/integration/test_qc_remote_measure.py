"""End-to-end remote built-in QC: qc-measure -> import-qc -> evaluate."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import ingest_file
from operon.qc import file_qc_status
from operon.rules import evaluate_entity
from operon.utils import sha256_file


class TestRemoteMeasurePipeline(PytestAssertions):
    def test_measure_import_evaluate_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                main(["--project", str(root), "init", str(root), "--project-id", "PRJ_RQC_001"]), 0)
            project = load_project(root)
            db = Database(project.db_path)
            try:
                db.insert_row("organisms", {
                    "organism_id": "ORG_000001", "scientific_name": "Testus exemplar",
                    "taxonomy_source": "NCBI",
                })
                db.insert_row("samples", {
                    "sample_id": "SMP_000001", "organism_id": "ORG_000001", "sex": "unknown",
                })
                db.insert_row("assemblies", {
                    "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
                    "assembly_level": "scaffold", "assembly_version": 1,
                })
                source = root / "genome.fa"
                source.write_text(">ctg1\n" + "ACGT" * 750 + "\n>ctg2\n" + "GGGG" * 500 + "\n",
                                  encoding="utf-8")
                row = ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")

                # The HPC side: measure the archived bytes without any project.
                archived = root / row["relative_path"]
                payload_path = root / "qc-payload.json"
                self.assertEqual(main([
                    "qc-measure", "--file", str(archived), "--format", "fasta",
                    "--role", "genome_fasta", "--sha256", sha256_file(archived),
                    "--size-bytes", str(archived.stat().st_size),
                    "--file-id", row["file_id"], "--out", str(payload_path),
                ]), 0)
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["file"]["file_id"], row["file_id"])

                # Back in the project: import the payload and decide.
                self.assertEqual(
                    main(["--project", str(root), "import-qc", "--file", str(payload_path)]), 0)
                self.assertEqual(file_qc_status(db, row["file_id"]), "QC_COMPLETE")
                self.assertEqual(db.get_entity_state("assembly", "ASM_000001"), "QC_COMPLETE")
                decision = evaluate_entity(
                    db, project, "assembly", "ASM_000001", "assembly_production_v1")
                self.assertEqual(decision["decision"], "PASS")
                runs = db.query("SELECT * FROM workflow_runs WHERE step='import-qc'")
                self.assertEqual(len(runs), 1)
                self.assertIn("operon import-qc --file", runs[0]["command"])
            finally:
                db.close()
