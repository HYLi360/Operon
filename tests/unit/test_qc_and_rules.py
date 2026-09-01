"""Built-in QC stages and rule engine tests."""

from __future__ import annotations

import gzip
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.config import load_project
from operon.database import Database
import operon.files as files_module
from operon.files import ingest_file
import operon.qc_module as qc_module
from operon.qc_module import PARSER_BACKEND, qc_all
from operon.rules import evaluate_entity
from operon.utils import now_iso


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

    def _add_annotation_inputs(self):
        self._add_organism_sample_assembly()
        self.db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
            "annotation_version": 1,
        })
        assembly = self.root / "annotation-assembly.fa"
        assembly.write_text(">ctg1\n" + "A" * 120 + "\n>ctg2\n" + "C" * 80 + "\n", encoding="utf-8")
        assembly_row = ingest_file(
            self.db, self.project, assembly, "assembly", "ASM_000001", "genome_fasta",
        )
        gff = self.root / "annotation.gff3"
        gff.write_text(
            "##gff-version 3\n"
            "ctg1\ttest\tgene\t1\t120\t.\t+\t.\tID=gene1\n"
            "ctg1\ttest\tmRNA\t1\t120\t.\t+\t.\tID=mrna1;Parent=gene1\n"
            "ctg1\ttest\tCDS\t1\t120\t.\t+\t0\tID=cds1;Parent=mrna1\n",
            encoding="utf-8",
        )
        gff_row = ingest_file(
            self.db, self.project, gff, "annotation", "ANN_000001", "annotation_gff3",
        )
        protein = self.root / "annotation-proteins.faa"
        protein.write_text(">p1\nMAAAAAAAAA*\n", encoding="utf-8")
        protein_row = ingest_file(
            self.db, self.project, protein, "annotation", "ANN_000001", "protein_fasta",
        )
        return assembly_row, gff_row, protein_row

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

    def test_cython_backend_and_full_fasta_header_metrics_are_persisted(self):
        self.assertEqual(PARSER_BACKEND, "cython")
        self._add_organism_sample_assembly()
        source = self.root / "headers.fa"
        source.write_text(
            ">same circular chromosome\nACGT\n"
            ">same circular chromosome\nTGCA\n",
            encoding="utf-8",
        )
        ingest_file(self.db, self.project, source, "assembly", "ASM_000001", "genome_fasta")
        result = qc_all(self.db, self.project, entity_type="assembly")[0]
        self.assertTrue(result["ok"], result)
        metrics = self.db.latest_metrics("assembly", "ASM_000001")
        self.assertEqual(metrics["duplicate_sequence_id_count"], 1.0)
        self.assertEqual(metrics["duplicate_header_count"], 1.0)
        self.assertEqual(metrics["circular_sequence_count"], 2.0)

    def test_qc_reuses_verified_immutable_file_fingerprint_and_rehash_bypasses_it(self):
        self._add_organism_sample_assembly()
        source = self.root / "cached.fa"
        source.write_text(_fasta_text([("ctg1", "ACGT" * 100)]), encoding="utf-8")
        row = ingest_file(
            self.db, self.project, source, "assembly", "ASM_000001", "genome_fasta",
        )

        with patch("operon.files.sha256_path", wraps=files_module.sha256_path) as hasher:
            cached = qc_all(self.db, self.project, file_id=row["file_id"])[0]
            self.assertTrue(cached["ok"], cached)
            self.assertEqual(hasher.call_count, 0)

            rehashed = qc_all(
                self.db, self.project, file_id=row["file_id"], force_checksum=True,
            )[0]
            self.assertTrue(rehashed["ok"], rehashed)
            self.assertEqual(hasher.call_count, 1)

        records = [
            json.loads(line)
            for line in (self.project.logs_root / "workflow.jsonl").read_text(encoding="utf-8").splitlines()
            if '"step": "qc"' in line and row["file_id"] in line
        ]
        self.assertEqual(records[-2]["checksum_verification_method"], "cached_stat_fingerprint")
        self.assertEqual(records[-1]["checksum_verification_method"], "full_sha256")
        self.assertFalse(records[-2]["qc_timing"]["integrity"]["rehash_requested"])
        self.assertTrue(records[-1]["qc_timing"]["integrity"]["rehash_requested"])

    def test_changed_same_size_file_invalidates_qc_verification_cache(self):
        self._add_organism_sample_assembly()
        source = self.root / "changed.fa"
        source.write_text(">ctg1\nAAAA\n", encoding="utf-8")
        row = ingest_file(
            self.db, self.project, source, "assembly", "ASM_000001", "genome_fasta",
        )
        archived = self.project.root / row["relative_path"]
        archived.write_text(">ctg1\nTTTT\n", encoding="utf-8")

        with patch("operon.files.sha256_path", wraps=files_module.sha256_path) as hasher:
            result = qc_all(self.db, self.project, file_id=row["file_id"])[0]
        self.assertFalse(result["ok"])
        self.assertEqual(hasher.call_count, 1)
        cached = self.db.conn.execute(
            "SELECT 1 FROM local_file_verifications WHERE file_id=?", (row["file_id"],),
        ).fetchone()
        self.assertIsNone(cached)

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

    def _add_other_format_file(self):
        self._add_organism_sample_assembly()
        source = self.root / "notes.dat"
        source.write_text("not a sequence format\n", encoding="utf-8")
        return ingest_file(self.db, self.project, source, "assembly", "ASM_000001", "other")

    def test_unparsed_format_records_integrity_without_parseable(self):
        row = self._add_other_format_file()
        self.assertEqual(row["format"], "other")
        result = qc_module.qc_file(self.db, self.project, row["file_id"])
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["error"])
        names = {
            item["metric_name"]
            for item in self.db.conn.execute(
                "SELECT metric_name FROM qc_results WHERE file_id=?", (row["file_id"],),
            ).fetchall()
        }
        self.assertTrue({"file_exists", "size_bytes", "sha256_match"}.issubset(names))
        self.assertFalse("parseable" in names)
        self.assertEqual(self.db.get_entity_state("assembly", "ASM_000001"), "QC_COMPLETE")

    def test_unparsed_format_is_not_evaluated_by_integrity_profile(self):
        row = self._add_other_format_file()
        result = qc_module.qc_file(self.db, self.project, row["file_id"])
        self.assertTrue(result["ok"], result)
        decision = evaluate_entity(self.db, self.project, "assembly", "ASM_000001", "file_integrity_v1")
        self.assertEqual(decision["decision"], "NOT_EVALUATED")
        self.assertIn("MISSING_METRIC:parseable", decision["reason_codes"])

    def test_paired_fastq_count_is_cached_within_qc_all(self):
        self.db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Reads", "taxonomy_source": "NCBI",
        })
        self.db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        self.db.insert_row("runs", {
            "run_id": "RUN_000001", "sample_id": "SMP_000001", "library_layout": "PAIRED",
        })
        fastq = "@r1\nACGT\n+\nIIII\n@r2\nTGCA\n+\nIIII\n"
        for role in ("reads_r1", "reads_r2"):
            source = self.root / f"{role}.fastq"
            source.write_text(fastq, encoding="utf-8")
            ingest_file(self.db, self.project, source, "run", "RUN_000001", role)
        with patch("operon.qc_module.fastq_record_count", wraps=qc_module.fastq_record_count) as counter:
            results = qc_all(self.db, self.project, entity_type="run")
        self.assertTrue(all(item["ok"] for item in results), results)
        self.assertEqual(counter.call_count, 1)
        metrics = self.db.latest_metrics("run", "RUN_000001")
        self.assertEqual(metrics["paired_read_count_match"], 1.0)

    def test_annotation_fasta_lengths_are_cached_across_qc_runs(self):
        assembly_row, gff_row, protein_row = self._add_annotation_inputs()
        with patch("operon.qc_module.fasta_lengths", wraps=qc_module.fasta_lengths) as scanner:
            first = qc_all(self.db, self.project, file_id=gff_row["file_id"])[0]
            second = qc_all(self.db, self.project, file_id=gff_row["file_id"])[0]
            self.assertTrue(first["ok"], first)
            self.assertTrue(second["ok"], second)
            self.assertEqual(scanner.call_count, 1)

            cache_files = list((self.project.qc_root / "cache" / "fasta_lengths").glob("*.tsv"))
            self.assertEqual(len(cache_files), 1)
            cache_lines = cache_files[0].read_text(encoding="utf-8").splitlines()
            seqid, length = cache_lines[1].rsplit("\t", 1)
            cache_lines[1] = f"{seqid}\t{int(length) + 1}"
            cache_files[0].write_text("\n".join(cache_lines) + "\n", encoding="utf-8")
            rebuilt = qc_all(self.db, self.project, file_id=gff_row["file_id"])[0]
            self.assertTrue(rebuilt["ok"], rebuilt)
            self.assertEqual(scanner.call_count, 2)

        records = [
            json.loads(line)
            for line in (self.project.logs_root / "workflow.jsonl").read_text(encoding="utf-8").splitlines()
            if '"step": "qc"' in line and gff_row["file_id"] in line
        ]
        self.assertEqual(len(records), 3)
        cache_statuses = [
            next(
                item["length_cache"]["status"]
                for item in record["qc_timing"]["related_inputs"]
                if item["kind"] == "assembly_fasta"
            )
            for record in records
        ]
        self.assertEqual(cache_statuses, ["built", "hit", "built"])
        self.assertIn("assembly_fasta_lengths", records[0]["stage_timings_seconds"])
        self.assertFalse("assembly_fasta_lengths" in records[1]["stage_timings_seconds"])
        self.assertIn("assembly_fasta_length_cache_lookup", records[1]["stage_timings_seconds"])

        annotation_rows = self.db.conn.execute(
            "SELECT DISTINCT input_identity FROM qc_results "
            "WHERE file_id=? AND qc_stage='annotation_basic'",
            (gff_row["file_id"],),
        ).fetchall()
        self.assertEqual(len(annotation_rows), 1)
        self.assertTrue(annotation_rows[0]["input_identity"].startswith("input-set:v1:"))
        integrity_rows = self.db.conn.execute(
            "SELECT DISTINCT input_identity FROM qc_results "
            "WHERE file_id=? AND qc_stage='file_integrity'",
            (gff_row["file_id"],),
        ).fetchall()
        self.assertEqual(
            {row["input_identity"] for row in integrity_rows},
            {f"file:{gff_row['file_id']}:{gff_row['sha256']}"},
        )
        self.assertEqual(
            {item["file_id"] for item in records[-1]["qc_timing"]["related_inputs"]},
            {assembly_row["file_id"], protein_row["file_id"]},
        )

    def test_annotation_rehash_covers_primary_and_related_inputs(self):
        _assembly_row, gff_row, _protein_row = self._add_annotation_inputs()
        qc_all(self.db, self.project, file_id=gff_row["file_id"])
        with patch("operon.files.sha256_path", wraps=files_module.sha256_path) as hasher:
            result = qc_all(
                self.db, self.project, file_id=gff_row["file_id"], force_checksum=True,
            )[0]
        self.assertTrue(result["ok"], result)
        self.assertEqual(hasher.call_count, 3)
        records = [
            json.loads(line)
            for line in (self.project.logs_root / "workflow.jsonl").read_text(encoding="utf-8").splitlines()
            if '"step": "qc"' in line and gff_row["file_id"] in line
        ]
        timing = records[-1]["qc_timing"]
        self.assertTrue(timing["integrity"]["rehash_requested"])
        self.assertEqual(
            {item["integrity"]["verification_method"] for item in timing["related_inputs"]},
            {"full_sha256"},
        )

    def test_annotation_qc_rejects_changed_related_protein(self):
        _assembly_row, gff_row, protein_row = self._add_annotation_inputs()
        protein_path = self.project.root / protein_row["relative_path"]
        original = protein_path.read_text(encoding="utf-8")
        protein_path.write_text(original.replace("A", "G"), encoding="utf-8")
        result = qc_all(self.db, self.project, file_id=gff_row["file_id"])[0]
        self.assertFalse(result["ok"])
        self.assertIn("related protein_fasta", result["error"])
        record = next(
            json.loads(line)
            for line in reversed(
                (self.project.logs_root / "workflow.jsonl").read_text(encoding="utf-8").splitlines()
            )
            if gff_row["file_id"] in line and '"step": "qc"' in line
        )
        protein_input = next(
            item for item in record["qc_timing"]["related_inputs"]
            if item["kind"] == "protein_fasta"
        )
        self.assertEqual(protein_input["integrity"]["verification_method"], "full_sha256")

    def test_annotation_qc_rejects_changed_related_assembly_before_cache_use(self):
        assembly_row, gff_row, _protein_row = self._add_annotation_inputs()
        assembly_path = self.project.root / assembly_row["relative_path"]
        with patch("operon.qc_module.fasta_lengths", wraps=qc_module.fasta_lengths) as scanner:
            first = qc_all(self.db, self.project, file_id=gff_row["file_id"])[0]
            self.assertTrue(first["ok"], first)
            original = assembly_path.read_text(encoding="utf-8")
            assembly_path.write_text(original.replace("A", "G", 1), encoding="utf-8")
            changed = qc_all(self.db, self.project, file_id=gff_row["file_id"])[0]
        self.assertFalse(changed["ok"])
        self.assertIn("related assembly_fasta", changed["error"])
        self.assertEqual(scanner.call_count, 1)

        record = next(
            json.loads(line)
            for line in reversed(
                (self.project.logs_root / "workflow.jsonl").read_text(encoding="utf-8").splitlines()
            )
            if gff_row["file_id"] in line and '"step": "qc"' in line
        )
        assembly_input = next(
            item for item in record["qc_timing"]["related_inputs"]
            if item["kind"] == "assembly_fasta"
        )
        self.assertEqual(assembly_input["integrity"]["verification_method"], "full_sha256")
        assert "length_cache" not in assembly_input

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
        gff_row = ingest_file(self.db, self.project, gff, "annotation", "ANN_000001", "annotation_gff3")
        protein = self.root / "proteins.faa"
        protein.write_text(">p1\nMPEPTIDE*\n", encoding="utf-8")
        protein_row = ingest_file(
            self.db, self.project, protein, "annotation", "ANN_000001", "protein_fasta",
        )
        qc_all(self.db, self.project, entity_type="annotation")
        metrics = self.db.latest_metrics("annotation", "ANN_000001")
        self.assertEqual(metrics["cds_length_multiple3_percent"], 0.0)
        self.assertEqual(metrics["missing_parent_count"], 1.0)
        decision = evaluate_entity(self.db, self.project, "annotation", "ANN_000001", "annotation_release_v1")
        self.assertEqual(decision["decision"], "FAIL")
        self.assertIn("CDS_NOT_MULTIPLE_OF_3", decision["reason_codes"])

        parseable = self.db.conn.execute(
            "SELECT metric_unit, parameter_set FROM qc_results "
            "WHERE file_id=? AND qc_stage='annotation_basic' AND metric_name='parseable'",
            (gff_row["file_id"],),
        ).fetchone()
        self.assertIsNotNone(parseable)
        self.assertIsNone(parseable["metric_unit"])
        self.assertEqual(parseable["parameter_set"], "builtin_v2")

        records = [
            json.loads(line)
            for line in (self.project.logs_root / "workflow.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        record = next(
            item for item in records
            if item.get("step") == "qc" and item.get("file_id") == gff_row["file_id"]
        )
        self.assertEqual(record["parser_backend"], "cython")
        self.assertEqual(record["file_role"], "annotation_gff3")
        self.assertEqual(record["input_size_bytes"], gff_row["size_bytes"])
        self.assertGreaterEqual(record["duration_seconds"], 0.0)
        timing = record["qc_timing"]
        self.assertEqual(timing["schema_version"], 1)
        self.assertEqual(timing["clock"], "perf_counter")
        self.assertEqual(timing["input"]["file_id"], gff_row["file_id"])
        self.assertEqual(
            {item["kind"] for item in timing["related_inputs"]},
            {"assembly_fasta", "protein_fasta"},
        )
        self.assertIn(
            protein_row["file_id"],
            {item["file_id"] for item in timing["related_inputs"]},
        )
        expected_stages = {
            "state_qc_running", "file_integrity", "annotation_manifest_lookup",
            "assembly_fasta_integrity", "assembly_fasta_length_cache_lookup",
            "assembly_fasta_lengths", "assembly_fasta_length_cache_write",
            "assembly_fasta_length_map_prepare", "gff3_scan", "gff3_finalize",
            "protein_manifest_lookup",
            "protein_fasta_integrity", "protein_stats", "qc_results_write",
            "state_qc_complete", "unattributed",
        }
        self.assertTrue(expected_stages.issubset(timing["stages_seconds"]))
        self.assertTrue(all(value >= 0.0 for value in timing["stages_seconds"].values()))
        self.assertEqual(record["stage_timings_seconds"], timing["stages_seconds"])
        assembly_input = next(
            item for item in timing["related_inputs"] if item["kind"] == "assembly_fasta"
        )
        self.assertEqual(assembly_input["length_cache"]["status"], "built")
        self.assertEqual(
            assembly_input["integrity"]["verification_method"],
            "cached_stat_fingerprint",
        )

        annotation_identity = self.db.conn.execute(
            "SELECT input_identity FROM qc_results WHERE file_id=? "
            "AND qc_stage='annotation_basic' AND metric_name='parseable'",
            (gff_row["file_id"],),
        ).fetchone()
        self.assertTrue(annotation_identity["input_identity"].startswith("input-set:v1:"))

        db_record = self.db.conn.execute(
            "SELECT execution_details FROM workflow_runs "
            "WHERE step='qc' AND entity_id=? AND command=? ORDER BY rowid DESC LIMIT 1",
            ("ANN_000001", f"operon qc --file-id {gff_row['file_id']}"),
        ).fetchone()
        self.assertIsNotNone(db_record)
        self.assertEqual(json.loads(db_record["execution_details"]), timing)

    def _insert_busco_metrics(self, stage, lineage, complete, fragmented=1.0, duplicated=1.0):
        for name, value, numeric in [
            ("busco_lineage_dataset", lineage, None),
            ("busco_complete_percent", str(complete), complete),
            ("busco_fragmented_percent", str(fragmented), fragmented),
            ("busco_duplicated_percent", str(duplicated), duplicated),
        ]:
            self.db.insert_qc_result({
                "entity_type": "annotation", "entity_id": "ANN_000001",
                "input_identity": "file:FIL_000001:test", "qc_stage": stage,
                "metric_name": name, "metric_value": value, "metric_numeric": numeric,
                "metric_unit": "percent" if name.endswith("_percent") else None,
                "tool": "busco", "tool_version": "6.1.0",
                "parameter_set": stage, "evaluated_at": now_iso(),
            })

    def test_busco_value_by_uses_declared_qc_stage(self):
        self.db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Testus",
            "taxonomy_source": "NCBI",
        })
        self.db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        self.db.insert_row("assemblies", {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_level": "scaffold", "assembly_version": 1,
        })
        self.db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
            "annotation_version": 1,
        })
        self._insert_busco_metrics(
            "analysis:busco_autolineage", "fabales_odb12.2", 74.0,
        )
        # A newer fixed-lineage result must not silently replace the source
        # selected by the profile.
        self._insert_busco_metrics(
            "analysis:busco_lineage:lineage_dataset=brassicales_odb12.2",
            "brassicales_odb12.2", 99.0,
        )
        decision = evaluate_entity(
            self.db, self.project, "annotation", "ANN_000001",
            "annotation_busco_viridiplantae_odb12_v1",
        )
        self.assertEqual(decision["decision"], "FAIL")
        self.assertIn("BUSCO_COMPLETENESS_FAIL", decision["reason_codes"])
        snapshot = json.loads(decision["observed"])
        self.assertEqual(
            snapshot["_rule_sources"]["analysis:busco_autolineage"]
            ["busco_complete_percent"],
            74.0,
        )

        self._insert_busco_metrics(
            "analysis:busco_autolineage", "fabales_odb12.2", 80.0,
        )
        decision = evaluate_entity(
            self.db, self.project, "annotation", "ANN_000001",
            "annotation_busco_viridiplantae_odb12_v1",
        )
        self.assertEqual(decision["decision"], "PASS_WITH_WARNINGS")
        self.assertIn("BUSCO_COMPLETENESS_WARNING", decision["reason_codes"])

    def test_busco_value_by_unknown_lineage_warns(self):
        self._insert_busco_metrics(
            "analysis:busco_autolineage", "new_lineage_odb12.2", 99.0,
        )
        decision = evaluate_entity(
            self.db, self.project, "annotation", "ANN_000001",
            "annotation_busco_viridiplantae_odb12_v1",
        )
        self.assertEqual(decision["decision"], "PASS_WITH_WARNINGS")
        self.assertIn("BUSCO_LINEAGE_UNCONFIGURED", decision["reason_codes"])
