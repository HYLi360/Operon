"""Built-in QC stages and rule engine tests."""

from __future__ import annotations

import gzip
import tempfile
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import ingest_file
from operon.qc_module import qc_all
from operon.rules import evaluate_entity


def _fasta_text(seqs):
    out = []
    for name, seq in seqs:
        out.append(f">{name}")
        for i in range(0, len(seq), 70):
            out.append(seq[i:i + 70])
    return "\n".join(out) + "\n"


class TestQCAndRules(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_QC_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)

    def _add_organism_sample_assembly(self):
        self.db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Testus exemplar", "taxonomy_source": "NCBI"})
        self.db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001", "sex": "unknown"})
        self.db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001", "assembly_level": "scaffold", "assembly_version": 1})

    def test_assembly_structural_qc_and_profile(self):
        self._add_organism_sample_assembly()
        source = self.root / "asm.fa"
        source.write_text(_fasta_text([("ctg1", "A" * 3000), ("ctg2", "C" * 2000)]), encoding="utf-8")
        row = ingest_file(self.db, self.project, source, "assembly", "ASM_000001", "genome_fasta")
        result = qc_all(self.db, self.project, entity_type="assembly")[0]
        self.assertTrue(result["ok"], result)
        metrics = self.db.latest_metrics("assembly", "ASM_000001")
        self.assertEqual(metrics["total_length"], 5000.0)
        self.assertEqual(metrics["contig_n50"], 3000.0)
        decision = evaluate_entity(self.db, self.project, "assembly", "ASM_000001", "assembly_production_v1")
        self.assertEqual(decision["decision"], "PASS")

    def test_duplicate_sequence_ids_are_measured_not_hard_coded(self):
        self._add_organism_sample_assembly()
        source = self.root / "dup.fa"
        source.write_text(_fasta_text([("same", "ACGT" * 100), ("same", "TGCA" * 100)]), encoding="utf-8")
        ingest_file(self.db, self.project, source, "assembly", "ASM_000001", "genome_fasta")
        qc_all(self.db, self.project, entity_type="assembly")
        metrics = self.db.latest_metrics("assembly", "ASM_000001")
        self.assertEqual(metrics["duplicate_sequence_id_count"], 1.0)

    def test_gzip_fasta_is_detected_and_parsed(self):
        self._add_organism_sample_assembly()
        source = self.root / "asm.fna.gz"
        with gzip.open(source, "wt", encoding="utf-8") as handle:
            handle.write(_fasta_text([("ctg1", "GATTACA" * 400)]))
        row = ingest_file(self.db, self.project, source, "assembly", "ASM_000001", "genome_fasta")
        self.assertEqual(row["format"], "fasta")
        self.assertEqual(row["compression"], "gzip")
        result = qc_all(self.db, self.project, entity_type="assembly")[0]
        self.assertTrue(result["ok"], result)

    def test_annotation_qc_finds_broken_cds_and_parent(self):
        self.db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Testus", "taxonomy_source": "NCBI"})
        self.db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        self.db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001", "assembly_level": "scaffold", "assembly_version": 1})
        self.db.insert_row("annotations", {"annotation_id": "ANN_000001", "assembly_id": "ASM_000001", "annotation_version": 1})
        fasta = self.root / "asm.fa"
        fasta.write_text(_fasta_text([("ctg1", "A" * 2000)]), encoding="utf-8")
        ingest_file(self.db, self.project, fasta, "assembly", "ASM_000001", "genome_fasta")
        gff = self.root / "bad.gff3"
        gff.write_text(
            "##gff-version 3\n"
            "ctg1\ttest\tgene\t101\t800\t.\t+\t.\tID=gene1\n"
            "ctg1\ttest\tmRNA\t101\t800\t.\t+\t.\tID=mrna1;Parent=gene1\n"
            "ctg1\ttest\tCDS\t101\t701\t.\t+\t0\tID=cds1;Parent=mrna1\n"
            "ctg1\ttest\tmRNA\t900\t1000\t.\t-\t.\tID=orphan;Parent=ghost\n",
            encoding="utf-8",
        )
        ingest_file(self.db, self.project, gff, "annotation", "ANN_000001", "annotation_gff3")
        qc_all(self.db, self.project, entity_type="annotation")
        metrics = self.db.latest_metrics("annotation", "ANN_000001")
        self.assertEqual(metrics["cds_length_multiple3_percent"], 0.0)
        self.assertEqual(metrics["missing_parent_count"], 1.0)
        decision = evaluate_entity(self.db, self.project, "annotation", "ANN_000001", "annotation_release_v1")
        self.assertEqual(decision["decision"], "FAIL")
        self.assertIn("CDS_NOT_MULTIPLE_OF_3", decision["reason_codes"])

