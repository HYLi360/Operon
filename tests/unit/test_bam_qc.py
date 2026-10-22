#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""BAM/CRAM built-in QC measurement (pysam-backed ``alignment_basic`` stage)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from operon import __version__
from operon.config import load_project
from operon.database import Database
from operon.errors import QCError
from operon.files import ingest_file
from operon.qc import bam as bam_module
from operon.qc import file_qc_status, measure_file, qc_file
from operon.qc.bam import bam_stats
from operon.qc.measure import _bam_metric_specs
from operon.utils import sha256_file
from tests.helpers import PytestAssertions, write_test_bam


def _identity(path: Path) -> dict:
    return {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}


class TestBamStats(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.path = write_test_bam(self.root / "reads.bam")
        self.stats = bam_stats(self.path, file_format="bam")

    def test_flagstat_level_counts(self):
        self.assertEqual(self.stats["alignment_count"], 12)
        self.assertEqual(self.stats["qc_failed_count"], 1)
        self.assertEqual(self.stats["primary_count"], 10)
        self.assertEqual(self.stats["secondary_count"], 1)
        self.assertEqual(self.stats["supplementary_count"], 1)
        self.assertEqual(self.stats["mapped_count"], 10)
        self.assertAlmostEqual(self.stats["mapped_percent"], 10 / 12 * 100)
        self.assertEqual(self.stats["primary_mapped_count"], 8)
        self.assertAlmostEqual(self.stats["primary_mapped_percent"], 8 / 12 * 100)
        self.assertEqual(self.stats["unmapped_count"], 2)
        self.assertEqual(self.stats["duplicate_count"], 1)
        self.assertAlmostEqual(self.stats["duplicate_percent"], 1 / 12 * 100)
        self.assertEqual(self.stats["paired_count"], 7)
        self.assertEqual(self.stats["read1_count"], 4)
        self.assertEqual(self.stats["read2_count"], 3)
        self.assertEqual(self.stats["proper_pair_count"], 4)
        self.assertAlmostEqual(self.stats["proper_pair_percent"], 4 / 7 * 100)
        self.assertEqual(self.stats["both_mates_mapped_count"], 6)
        self.assertEqual(self.stats["singleton_count"], 1)
        self.assertAlmostEqual(self.stats["singleton_percent"], 1 / 7 * 100)
        self.assertEqual(self.stats["mate_different_reference_count"], 2)
        self.assertEqual(self.stats["mate_different_reference_mapq5_count"], 1)

    def test_stats_level_metrics(self):
        self.assertEqual(self.stats["read_length_min"], 40)
        self.assertEqual(self.stats["read_length_max"], 50)
        self.assertAlmostEqual(self.stats["read_length_mean"], 590 / 12)
        self.assertAlmostEqual(self.stats["gc_percent"], 175 / 450 * 100)
        self.assertEqual(self.stats["mapq_ge30_count"], 5)
        self.assertAlmostEqual(self.stats["mapq_ge30_percent"], 5 / 8 * 100)
        self.assertAlmostEqual(self.stats["error_rate"], 7 / 450)
        self.assertEqual(self.stats["nm_tag_present_count"], 9)
        self.assertEqual(self.stats["insert_size_count"], 2)
        self.assertAlmostEqual(self.stats["insert_size_mean"], 200.0)
        self.assertAlmostEqual(self.stats["insert_size_std"], 100.0)
        self.assertAlmostEqual(self.stats["insert_size_median"], 200.0)
        self.assertEqual(self.stats["insert_size_min"], 100)
        self.assertEqual(self.stats["insert_size_max"], 300)

    def test_coverage_metrics(self):
        self.assertEqual(self.stats["reference_count"], 3)
        self.assertEqual(self.stats["references_with_mapped_count"], 2)
        self.assertAlmostEqual(self.stats["mean_depth"], 450 / 1700)
        self.assertEqual(self.stats["covered_bases"], 440)
        self.assertAlmostEqual(self.stats["coverage_percent"], 440 / 1700 * 100)
        self.assertEqual(self.stats["depth_ge10_percent"], 0.0)
        self.assertEqual(self.stats["depth_ge30_percent"], 0.0)
        self.assertEqual(self.stats["coverage_breadth_skipped"], 0)

    def test_coverage_memory_guard(self, monkeypatch):
        monkeypatch.setattr(bam_module, "COVERAGE_MAX_REFERENCE_BASES", 10)
        stats = bam_stats(self.path, file_format="bam")
        # mean_depth stays exact; breadth/depth-threshold metrics are dropped.
        self.assertAlmostEqual(stats["mean_depth"], 450 / 1700)
        self.assertIsNone(stats["covered_bases"])
        self.assertIsNone(stats["coverage_percent"])
        self.assertIsNone(stats["depth_ge10_percent"])
        self.assertIsNone(stats["depth_ge30_percent"])
        self.assertEqual(stats["coverage_breadth_skipped"], 1)
        specs = {name: value for _s, name, value, _u in _bam_metric_specs(stats)}
        self.assertNotIn("covered_bases", specs)
        self.assertEqual(specs["coverage_breadth_skipped"], 1)

    def test_empty_bam(self):
        import pysam

        path = self.root / "empty.bam"
        header = {"HD": {"VN": "1.6"}, "SQ": [{"SN": "chr1", "LN": 100}]}
        with pysam.AlignmentFile(str(path), "wb", header=header):
            pass
        stats = bam_stats(path, file_format="bam")
        self.assertEqual(stats["alignment_count"], 0)
        self.assertEqual(stats["mapped_percent"], 0.0)
        self.assertEqual(stats["covered_bases"], 0)
        self.assertEqual(stats["coverage_percent"], 0.0)
        self.assertEqual(stats["insert_size_count"], 0)
        self.assertEqual(stats["insert_size_median"], 0.0)
        self.assertIsNone(stats["error_rate"])
        names = {name for _s, name, value, _u in _bam_metric_specs(stats)}
        self.assertNotIn("error_rate", names)

    def test_corrupt_bam_raises_qc_error(self):
        path = self.root / "broken.bam"
        path.write_bytes(b"this is not a BAM file")
        with self.assertRaises(QCError):
            bam_stats(path, file_format="bam")

    def test_unknown_format_raises_value_error(self):
        with self.assertRaises(ValueError):
            bam_stats(self.path, file_format="sam")

    def test_embedded_reference_cram(self):
        import pysam

        reference = self.root / "ref.fa"
        reference.write_text(
            ">chr1\n"
            + "ACGT" * 250
            + "\n>chr2\n"
            + "ACGT" * 125
            + "\n>chr3\n"
            + "ACGT" * 50
            + "\n",
            encoding="utf-8",
        )
        pysam.faidx(str(reference))
        path = self.root / "reads.cram"
        with (
            pysam.AlignmentFile(str(self.path), "rb") as source,
            pysam.AlignmentFile(
                str(path),
                "wc",
                reference_filename=str(reference),
                header=source.header,
                format_options=[b"embed_ref=1"],
            ) as out,
        ):
            for read in source:
                out.write(read)
        stats = bam_stats(path, file_format="cram")
        self.assertEqual(stats["alignment_count"], 12)
        self.assertEqual(stats["mapped_count"], 10)
        self.assertEqual(stats["covered_bases"], 440)

    def test_corrupt_cram_raises_qc_error(self):
        path = self.root / "broken.cram"
        path.write_bytes(b"CRAM\x03garbage")
        with self.assertRaises(QCError):
            bam_stats(path, file_format="cram")


class TestBamQcInProject(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        from operon.cli import main

        self.assertEqual(
            main(
                [
                    "--project",
                    str(self.root),
                    "init",
                    str(self.root),
                    "--project-id",
                    "PRJ_BAM_001",
                ]
            ),
            0,
        )
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        self.db.insert_row(
            "organisms",
            {
                "organism_id": "ORG_000001",
                "scientific_name": "Testus exemplar",
                "taxonomy_source": "NCBI",
            },
        )
        self.db.insert_row(
            "samples",
            {
                "sample_id": "SMP_000001",
                "organism_id": "ORG_000001",
                "sex": "unknown",
            },
        )
        self.db.insert_row(
            "assemblies",
            {
                "assembly_id": "ASM_000001",
                "sample_id": "SMP_000001",
                "assembly_level": "scaffold",
                "assembly_version": 1,
            },
        )

    def _ingest_bam(self, name: str = "reads.bam") -> dict:
        source = write_test_bam(self.root / name)
        return ingest_file(
            self.db, self.project, source, "assembly", "ASM_000001", "alignment_bam"
        )

    def _metrics(self, file_id: str) -> dict:
        rows = self.db.query(
            "SELECT qc_stage, metric_name, metric_numeric FROM qc_results "
            "WHERE file_id=?",
            (file_id,),
        )
        return {
            (row["qc_stage"], row["metric_name"]): row["metric_numeric"] for row in rows
        }

    def test_qc_file_measures_alignment_stage(self):
        row = self._ingest_bam()
        result = qc_file(self.db, self.project, row["file_id"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["file_qc_state"], "QC_COMPLETE")
        self.assertEqual(file_qc_status(self.db, row["file_id"]), "QC_COMPLETE")
        self.assertEqual(
            self.db.get_entity_state("assembly", "ASM_000001"), "QC_COMPLETE"
        )
        metrics = self._metrics(row["file_id"])
        self.assertEqual(metrics[("file_integrity", "parseable")], 1.0)
        self.assertEqual(metrics[("alignment_basic", "alignment_count")], 12.0)
        self.assertEqual(metrics[("alignment_basic", "mapped_count")], 10.0)
        self.assertAlmostEqual(
            metrics[("alignment_basic", "mapped_percent")], 10 / 12 * 100
        )
        self.assertEqual(metrics[("alignment_basic", "covered_bases")], 440.0)
        units = {
            row["metric_name"]: row["metric_unit"]
            for row in self.db.query(
                "SELECT metric_name, metric_unit FROM qc_results WHERE file_id=?",
                (row["file_id"],),
            )
        }
        self.assertEqual(units["mapped_percent"], "percent")
        self.assertEqual(units["covered_bases"], "bp")
        self.assertEqual(units["insert_size_mean"], "bp")
        run = self.db.query(
            "SELECT execution_details FROM workflow_runs "
            "WHERE entity_type='assembly' AND entity_id='ASM_000001' AND step='qc'",
        )[-1]
        details = json.loads(run["execution_details"])
        self.assertEqual(details["parser_backend"], "pysam")
        self.assertIn("bam_stats", details["stages_seconds"])

    def test_measure_file_matches_qc_file(self):
        row = self._ingest_bam()
        path = self.root / row["relative_path"]
        payload = measure_file(
            path,
            file_format="bam",
            file_role="alignment_bam",
            file_id=row["file_id"],
            **_identity(path),
        )
        self.assertEqual(payload["parser_backend"], "pysam")
        self.assertEqual(payload["tool_version"], __version__)
        stages = {m["qc_stage"] for m in payload["metrics"]}
        self.assertEqual(stages, {"file_integrity", "alignment_basic"})
        names = {m["metric_name"] for m in payload["metrics"]}
        self.assertIn("parseable", names)
        self.assertIn("error_rate", names)
        payload_tuples = {
            (m["qc_stage"], m["metric_name"], m["metric_value"], m["metric_numeric"])
            for m in payload["metrics"]
        }
        result = qc_file(self.db, self.project, row["file_id"])
        self.assertTrue(result["ok"], result)
        local_tuples = {
            (r["qc_stage"], r["metric_name"], r["metric_value"], r["metric_numeric"])
            for r in self.db.query(
                "SELECT qc_stage, metric_name, metric_value, metric_numeric "
                "FROM qc_results WHERE file_id=?",
                (row["file_id"],),
            )
        }
        self.assertEqual(payload_tuples, local_tuples)

    def test_corrupt_bam_marks_qc_failed(self):
        source = self.root / "corrupt.bam"
        source.write_bytes(b"BAM\x01but not really")
        row = ingest_file(
            self.db, self.project, source, "assembly", "ASM_000001", "alignment_bam"
        )
        result = qc_file(self.db, self.project, row["file_id"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["file_qc_state"], "QC_FAILED")
        metrics = self._metrics(row["file_id"])
        self.assertEqual(metrics[("file_integrity", "parseable")], 0.0)
        self.assertNotIn(("alignment_basic", "mapped_count"), metrics)
