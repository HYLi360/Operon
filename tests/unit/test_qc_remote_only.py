"""QC guard for REMOTE_ONLY manifest files (evicted local bytes)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import ingest_file
from operon.qc import qc_all


class TestQCRemoteOnly(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(
            main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_QC_RO_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        self.db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Testus exemplar",
            "taxonomy_source": "NCBI",
        })
        self.db.insert_row("samples", {
            "sample_id": "SMP_000001", "organism_id": "ORG_000001", "sex": "unknown",
        })
        self.db.insert_row("assemblies", {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_level": "scaffold", "assembly_version": 1,
        })

    def _ingest_assembly(self, name: str = "asm.fa") -> dict:
        source = self.root / name
        source.write_text(">ctg1\n" + "A" * 200 + "\n>ctg2\n" + "C" * 100 + "\n", encoding="utf-8")
        return ingest_file(self.db, self.project, source, "assembly", "ASM_000001", "genome_fasta")

    def _make_remote_only(self, file_row: dict) -> None:
        with self.db.transaction():
            self.db.conn.execute(
                "UPDATE files SET status='REMOTE_ONLY' WHERE file_id=?", (file_row["file_id"],),
            )
        (self.root / file_row["relative_path"]).unlink()

    def _qc_run_count(self, file_id: str) -> int:
        return int(self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='qc' AND command LIKE ?",
            (f"%{file_id}%",),
        ).fetchone()["n"])

    def test_qc_all_skips_remote_only_without_side_effects(self):
        row = self._ingest_assembly()
        self._make_remote_only(row)
        entity_state_before = self.db.get_entity_state("assembly", "ASM_000001")
        qc_rows_before = self.db.conn.execute("SELECT COUNT(*) AS n FROM qc_results").fetchone()["n"]

        results = qc_all(self.db, self.project)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result["skipped"])
        self.assertFalse(result["ok"])
        self.assertIn("REMOTE_ONLY", result["error"])
        self.assertIn("operon pull", result["error"])
        self.assertIn("operon import-qc", result["error"])
        self.assertEqual(result["file_id"], row["file_id"])
        self.assertEqual(result["file_qc_state"], "QC_PENDING")
        self.assertEqual(result["entity_qc_state"], entity_state_before)
        self.assertEqual(
            [item["file_id"] for item in result["file_statuses"]], [row["file_id"]],
        )
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) AS n FROM qc_results").fetchone()["n"],
            qc_rows_before,
        )
        self.assertEqual(
            self.db.get_entity_state("assembly", "ASM_000001"), entity_state_before,
        )
        self.assertEqual(self._qc_run_count(row["file_id"]), 0)

    def test_cli_file_id_remote_only_exits_1_with_actionable_stderr(self, capsys):
        row = self._ingest_assembly()
        self._make_remote_only(row)

        code = main(["--project", str(self.root), "qc", "--file-id", row["file_id"]])

        self.assertEqual(code, 1)
        err = capsys.readouterr().err
        self.assertIn("SKIPPED", err)
        self.assertIn("pull", err)
        self.assertEqual(self._qc_run_count(row["file_id"]), 0)

    def test_cli_batch_with_skip_but_no_failure_exits_0(self, capsys):
        skipped_row = self._ingest_assembly("skipped.fa")
        self._make_remote_only(skipped_row)
        self.db.insert_row("assemblies", {
            "assembly_id": "ASM_000002", "sample_id": "SMP_000001",
            "assembly_level": "scaffold", "assembly_version": 1,
        })
        source = self.root / "local.fa"
        source.write_text(">ctg1\n" + "A" * 200 + "\n", encoding="utf-8")
        local_row = ingest_file(self.db, self.project, source, "assembly", "ASM_000002", "genome_fasta")

        code = main(["--project", str(self.root), "qc"])

        self.assertEqual(code, 0)
        captured = capsys.readouterr()
        self.assertIn(f"{skipped_row['file_id']}: SKIPPED", captured.err)
        assert f"{skipped_row['file_id']}: FAILED" not in captured.err
        self.assertIn(f"{local_row['file_id']}: QC_COMPLETE", captured.out)
        self.assertIn("skipped", captured.out)
        self.assertEqual(
            self.db.get_entity_state("assembly", "ASM_000002"), "QC_COMPLETE",
        )

    def test_missing_local_file_without_remote_only_status_still_fails(self):
        row = self._ingest_assembly()
        (self.root / row["relative_path"]).unlink()

        results = qc_all(self.db, self.project)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertFalse(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["file_qc_state"], "QC_FAILED")
        self.assertEqual(result["entity_qc_state"], "QC_FAILED")
        self.assertGreater(
            self.db.conn.execute(
                "SELECT COUNT(*) AS n FROM qc_results WHERE file_id=?", (row["file_id"],),
            ).fetchone()["n"],
            0,
        )
        self.assertEqual(self._qc_run_count(row["file_id"]), 1)

    def test_normal_local_file_batch_unaffected(self):
        row = self._ingest_assembly()

        results = qc_all(self.db, self.project)

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result["ok"])
        self.assertFalse(result["skipped"])
        self.assertEqual(result["file_qc_state"], "QC_COMPLETE")
        self.assertEqual(result["entity_qc_state"], "QC_COMPLETE")
        self.assertGreater(
            self.db.conn.execute(
                "SELECT COUNT(*) AS n FROM qc_results WHERE file_id=?", (row["file_id"],),
            ).fetchone()["n"],
            0,
        )
