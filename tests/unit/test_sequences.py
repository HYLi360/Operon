"""Sequences table population (local QC, import-qc) and seqid reverse lookup."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import ingest_file
from operon.qc import measure_file, qc_file
from operon.utils import sha256_file

FASTA = ">ctg1\n" + "ACGT" * 50 + "\n>ctg2\n" + "GGGG" * 25 + "\n"
FASTA_V2 = ">ctg1\n" + "ACGT" * 50 + "\n>ctg3\n" + "TTTT" * 10 + "\n"
FASTQ = "@r1\nACGT\n+\nIIII\n@r2\nTGCA\n+\nIIII\n"
GFF3 = (
    "##gff-version 3\n"
    "ctg1\ttest\tgene\t1\t120\t.\t+\t.\tID=gene1\n"
)


class TestSequences(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(
            main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_SEQ_001"]), 0)
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
        self.db.insert_row("runs", {
            "run_id": "RUN_000001", "sample_id": "SMP_000001", "library_layout": "PAIRED",
        })

    def _ingest(self, name: str, text: str, entity_type: str, entity_id: str, role: str) -> dict:
        source = self.root / name
        source.write_text(text, encoding="utf-8")
        return ingest_file(self.db, self.project, source, entity_type, entity_id, role)

    def _sequence_rows(self, file_id: str) -> list:
        return self.db.query("SELECT * FROM sequences WHERE file_id=? ORDER BY seqid", (file_id,))

    def test_fasta_qc_populates_sequences(self):
        row = self._ingest("asm.fa", FASTA, "assembly", "ASM_000001", "genome_fasta")
        result = qc_file(self.db, self.project, row["file_id"])
        self.assertTrue(result["ok"], result)
        rows = self._sequence_rows(row["file_id"])
        self.assertEqual(
            [(r["seqid"], r["length"]) for r in rows],
            [("ctg1", 200), ("ctg2", 100)],
        )
        for record in rows:
            self.assertEqual(record["entity_type"], "assembly")
            self.assertEqual(record["entity_id"], "ASM_000001")
            self.assertEqual(record["file_sha256"], row["sha256"])

    def test_repeated_qc_is_idempotent(self):
        row = self._ingest("asm.fa", FASTA, "assembly", "ASM_000001", "genome_fasta")
        self.assertTrue(qc_file(self.db, self.project, row["file_id"])["ok"])
        self.assertTrue(qc_file(self.db, self.project, row["file_id"])["ok"])
        self.assertEqual(len(self._sequence_rows(row["file_id"])), 2)

    def test_re_qc_after_content_change_replaces_rows(self):
        row = self._ingest("asm.fa", FASTA, "assembly", "ASM_000001", "genome_fasta")
        self.assertTrue(qc_file(self.db, self.project, row["file_id"])["ok"])
        archived = self.root / row["relative_path"]
        archived.write_text(FASTA_V2, encoding="utf-8")
        self.db.conn.execute(
            "UPDATE files SET sha256=?, size_bytes=? WHERE file_id=?",
            (sha256_file(archived), archived.stat().st_size, row["file_id"]),
        )
        self.db.conn.commit()
        result = qc_file(self.db, self.project, row["file_id"])
        self.assertTrue(result["ok"], result)
        rows = self._sequence_rows(row["file_id"])
        self.assertEqual(
            [(r["seqid"], r["length"]) for r in rows],
            [("ctg1", 200), ("ctg3", 40)],
        )

    def test_gff3_qc_populates_assembly_sequences(self):
        assembly = self._ingest("ann-asm.fa", FASTA, "assembly", "ASM_000001", "genome_fasta")
        self.db.conn.execute(
            "UPDATE assemblies SET fasta_file_id=? WHERE assembly_id=?",
            (assembly["file_id"], "ASM_000001"),
        )
        self.db.conn.commit()
        self.db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
            "annotation_version": 1,
        })
        gff = self._ingest("ann.gff3", GFF3, "annotation", "ANN_000001", "annotation_gff3")
        result = qc_file(self.db, self.project, gff["file_id"])
        self.assertTrue(result["ok"], result)
        rows = self._sequence_rows(assembly["file_id"])
        self.assertEqual(
            [(r["seqid"], r["length"]) for r in rows],
            [("ctg1", 200), ("ctg2", 100)],
        )
        for record in rows:
            self.assertEqual(record["entity_type"], "assembly")
            self.assertEqual(record["entity_id"], "ASM_000001")
        self.assertEqual(self._sequence_rows(gff["file_id"]), [])

    def test_qc_measure_payload_and_import_qc_populate_sequences(self):
        row = self._ingest("remote.fa", FASTA, "assembly", "ASM_000001", "genome_fasta")
        path = self.root / row["relative_path"]
        payload = measure_file(
            path, file_format="fasta", file_role="genome_fasta",
            file_id=row["file_id"],
            sha256=sha256_file(path), size_bytes=path.stat().st_size,
        )
        self.assertEqual(payload["sequences"], {"ctg1": 200, "ctg2": 100})
        payload_path = self.root / "payload.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        code = main(["--project", str(self.root), "import-qc", "--file", str(payload_path)])
        self.assertEqual(code, 0)
        rows = self._sequence_rows(row["file_id"])
        self.assertEqual(
            [(r["seqid"], r["length"]) for r in rows],
            [("ctg1", 200), ("ctg2", 100)],
        )
        for record in rows:
            self.assertEqual(record["file_sha256"], row["sha256"])
            self.assertEqual(record["entity_id"], "ASM_000001")

    def test_show_resolves_seqid_to_owning_entity_and_file(self, capsys):
        row = self._ingest("asm.fa", FASTA, "assembly", "ASM_000001", "genome_fasta")
        self.assertTrue(qc_file(self.db, self.project, row["file_id"])["ok"])
        code = main(["--project", str(self.root), "show", "ctg1"])
        self.assertEqual(code, 0)
        out = capsys.readouterr().out
        self.assertIn("sequence ctg1", out)
        self.assertIn("ASM_000001", out)
        self.assertIn(row["file_id"], out)
        self.assertIn(row["relative_path"], out)
        code = main(["--project", str(self.root), "show", "ctg2", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(capsys.readouterr().out)
        self.assertEqual(payload["match"], "sequence")
        self.assertEqual(payload["sequences"][0]["entity_id"], "ASM_000001")
        self.assertEqual(payload["sequences"][0]["file_id"], row["file_id"])
        self.assertEqual(payload["sequences"][0]["length"], 100)

    def test_non_fasta_qc_writes_no_sequence_rows(self):
        row = self._ingest("reads.fastq", FASTQ, "run", "RUN_000001", "reads_r1")
        result = qc_file(self.db, self.project, row["file_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(self._sequence_rows(row["file_id"]), [])
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM sequences")[0]["n"], 0)
