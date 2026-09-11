"""extract-domains and select-sequences: alignment-driven FASTA subsets."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import ingest_file
from operon.utils import sha256_file


class _SequenceToolProject(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(
            main(["--project", str(self.root), "init", str(self.root),
                  "--project-id", "PRJ_SEQ_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        self.db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Testus",
            "taxonomy_source": "NCBI",
        })
        self.db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        self.db.insert_row("assemblies", {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_level": "contig", "assembly_version": 1,
        })
        self.db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
            "annotation_source": "test", "annotation_version": 1,
        })
        self.proteins = {
            "p1": "M" + "A" * 49,
            "p2": "C" * 40,
            "p3": "G" * 20,
            "p4": "D" * 60,
            "p5": "H" * 30,
        }
        fasta = self.root / "proteins.faa"
        lines: list[str] = []
        for seqid, sequence in self.proteins.items():
            lines.append(f">{seqid} description of {seqid}" if seqid == "p2" else f">{seqid}")
            lines.append(sequence)
        fasta.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.file_row = ingest_file(
            self.db, self.project, fasta, "annotation", "ANN_000001", "protein_fasta")
        self.file_id = self.file_row["file_id"]
        for seqid, sequence in self.proteins.items():
            self.db.insert_row("sequences", {
                "file_id": self.file_id, "file_sha256": self.file_row["sha256"],
                "entity_type": "annotation", "entity_id": "ANN_000001",
                "seqid": seqid, "length": len(sequence),
            })
        self.cdd_job = self._add_job("cdd")
        self._add_alignment(self.cdd_job, "cdd", "p1", "bhlh_family", 1, 10, 40, 1e-10,
                            {"hit_type": "Specific", "short_name": "bHLH"})
        self._add_alignment(self.cdd_job, "cdd", "p1", "other_dom", 2, 5, 20, 1e-3,
                            {"hit_type": "NonSpecific", "short_name": "other"})
        self._add_alignment(self.cdd_job, "cdd", "p2 description of p2", "bhlh_family",
                            1, 2, 39, 1e-8, {"hit_type": "Specific", "short_name": "bHLH"})
        self._add_alignment(self.cdd_job, "cdd", "p3", "bhlh_family", 1, 3, 12, 1e-6,
                            {"hit_type": "Specific", "short_name": "bHLH"})
        self._add_alignment(self.cdd_job, "cdd", "p4", "CD99999", 1, 5, 55, 1e-20,
                            {"hit_type": "Specific", "short_name": "bHLH_MYC"})
        failed_job = self._add_job("cdd", status="failed")
        self._add_alignment(failed_job, "cdd", "p5", "must_not_count", 1, 1, 30, 1e-50)

    def _add_job(self, analysis: str, status: str = "completed") -> int:
        self.db.insert_row("analysis_jobs", {
            "analysis_name": analysis, "entity_type": "annotation",
            "entity_id": "ANN_000001", "file_id": self.file_id,
            "tool": "faketool", "tool_version": "1.0",
            "parameter_set": "default", "parameter_sha256": "p" * 64,
            "input_sha256": self.file_row["sha256"], "database_identity": "db",
            "status": status, "started_at": "2026-01-01T00:00:00+00:00",
        })
        return self.db.query("SELECT max(job_id) AS j FROM analysis_jobs")[0]["j"]

    def _add_alignment(self, job_id: int, analysis: str, query_id: str, subject_id: str,
                       rank: int, qstart: int, qend: int, evalue: float,
                       extra: dict | None = None) -> None:
        self.db.insert_row("analysis_alignments", {
            "job_id": job_id, "entity_type": "annotation", "entity_id": "ANN_000001",
            "file_id": self.file_id, "analysis_name": analysis, "query_id": query_id,
            "subject_id": subject_id, "hit_rank": rank, "query_start": qstart,
            "query_end": qend, "evalue": evalue,
            "extra_json": json.dumps(extra) if extra else None,
        })

    @staticmethod
    def _read_fasta(path: Path) -> list[tuple[str, str]]:
        records: list[tuple[str, str]] = []
        header: str | None = None
        parts: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(">"):
                if header is not None:
                    records.append((header, "".join(parts)))
                header, parts = line[1:], []
            else:
                parts.append(line)
        if header is not None:
            records.append((header, "".join(parts)))
        return records

    @staticmethod
    def _read_manifest(path: Path) -> list[dict[str, str]]:
        lines = path.read_text(encoding="utf-8").splitlines()
        header = lines[0].split("\t")
        return [dict(zip(header, line.split("\t"))) for line in lines[1:]]

    def _run_extract(self, *extra: str) -> int:
        return main(["--project", str(self.root), "extract-domains",
                     "--file-id", self.file_id, *extra])

    def _run_select(self, *extra: str) -> int:
        return main(["--project", str(self.root), "select-sequences",
                     "--file-id", self.file_id, *extra])


class TestExtractDomains(_SequenceToolProject):
    def test_best_only_flanks_and_truncates_at_boundaries(self):
        out = self.root / "domains.faa"
        manifest = self.root / "domains.tsv"
        self.assertEqual(self._run_extract(
            "--analysis", "cdd", "--out", str(out), "--manifest", str(manifest)), 0)
        self.assertEqual(self._read_fasta(out), [
            ("p1", "A" * 41),   # 10-40 flanked to 5-45
            ("p2", "C" * 40),   # 2-39 flanked, clamped to 1-40
            ("p4", "D" * 60),   # 5-55 flanked, clamped to 1-60
        ])
        rows = {(r["seqid"], r["region_start"]): r for r in self._read_manifest(manifest)}
        self.assertEqual(len(rows), 5)
        extracted = rows[("p1", "10")]
        self.assertEqual(extracted["extracted_start"], "5")
        self.assertEqual(extracted["extracted_end"], "45")
        self.assertEqual(extracted["length"], "41")
        self.assertEqual(extracted["analysis_name"], "cdd")
        self.assertEqual(extracted["excluded_reason"], "")
        self.assertEqual(rows[("p1", "5")]["excluded_reason"], "below_min_length")
        self.assertEqual(rows[("p3", "3")]["excluded_reason"], "below_min_length")
        self.assertEqual(rows[("p2", "2")]["length"], "40")
        self.assertEqual(rows[("p4", "5")]["extracted_start"], "1")
        self.assertEqual(rows[("p4", "5")]["extracted_end"], "60")

        runs = self.db.query("SELECT * FROM workflow_runs WHERE step='extract-domains'")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "completed")
        self.assertTrue(runs[0]["command"].startswith("operon extract-domains --file-id"))
        self.assertIn("--analysis cdd", runs[0]["command"])
        self.assertEqual(runs[0]["input_sha256"], self.file_row["sha256"])
        self.assertEqual(runs[0]["output_sha256"], sha256_file(out))
        self.assertEqual(runs[0]["entity_type"], "annotation")
        self.assertEqual(runs[0]["entity_id"], "ANN_000001")

    def test_all_regions_emits_one_record_per_region(self):
        out = self.root / "domains.faa"
        self.assertEqual(self._run_extract(
            "--analysis", "cdd", "--all-regions", "--min-length", "1",
            "--out", str(out)), 0)
        self.assertEqual([h for h, _s in self._read_fasta(out)], [
            "p1|region:1-25",
            "p1|region:5-45",
            "p2|region:1-40",
            "p3|region:1-17",
            "p4|region:1-60",
        ])

    def test_subject_like_matches_subject_id_and_short_name(self):
        out = self.root / "domains.faa"
        self.assertEqual(self._run_extract(
            "--analysis", "cdd", "--subject-like", "bhlh%", "--out", str(out)), 0)
        # p1/p2/p3 match via subject_id, p4 only via the short_name in extra_json.
        self.assertEqual([h for h, _s in self._read_fasta(out)], ["p1", "p2", "p4"])

    def test_evalue_max_filters_alignments(self):
        out = self.root / "domains.faa"
        self.assertEqual(self._run_extract(
            "--analysis", "cdd", "--evalue-max", "1e-9", "--out", str(out)), 0)
        self.assertEqual([h for h, _s in self._read_fasta(out)], ["p1", "p4"])

    def test_failed_jobs_are_ignored(self):
        out = self.root / "domains.faa"
        self.assertEqual(self._run_extract(
            "--analysis", "cdd", "--min-length", "1", "--out", str(out)), 0)
        # p5 only has an alignment on the failed job.
        assert "p5" not in [h for h, _s in self._read_fasta(out)]

    def test_regions_tsv_path(self):
        regions = self.root / "regions.tsv"
        regions.write_text(
            "seqid\tstart\tend\tsubject\tevalue\n"
            "p1\t10\t40\tdomA\t1e-7\n"
            "p2\t1\t5\ttiny\t0.5\n"
            "p9\t1\t10\tghost\t1e-3\n",
            encoding="utf-8",
        )
        out = self.root / "domains.faa"
        manifest = self.root / "domains.tsv"
        self.assertEqual(self._run_extract(
            "--regions-tsv", str(regions), "--min-length", "1",
            "--out", str(out), "--manifest", str(manifest)), 0)
        self.assertEqual(self._read_fasta(out), [
            ("p1", "A" * 41),
            ("p2", "C" * 10),
        ])
        rows = {r["seqid"]: r for r in self._read_manifest(manifest)}
        self.assertEqual(rows["p1"]["analysis_name"], "")
        self.assertEqual(rows["p1"]["subject_id"], "domA")
        self.assertEqual(rows["p2"]["extracted_end"], "10")
        self.assertEqual(rows["p9"]["excluded_reason"], "seqid_not_in_fasta")
        self.assertEqual(rows["p9"]["length"], "")

    def test_analysis_and_regions_tsv_are_mutually_exclusive(self):
        out = self.root / "domains.faa"
        with self.assertRaises(SystemExit):
            self._run_extract("--analysis", "cdd", "--regions-tsv", "x.tsv", "--out", str(out))
        with self.assertRaises(SystemExit):
            self._run_extract("--out", str(out))

    def test_remote_only_file_gives_actionable_error(self, capsys):
        self.db.conn.execute(
            "UPDATE files SET status='REMOTE_ONLY' WHERE file_id=?", (self.file_id,))
        self.db.conn.commit()
        out = self.root / "domains.faa"
        self.assertEqual(self._run_extract("--analysis", "cdd", "--out", str(out)), 2)
        self.assertIn("operon pull", capsys.readouterr().err)
        self.assertFalse(out.exists())


class TestSelectSequences(_SequenceToolProject):
    def test_require_hit_selects_matching_sequences(self):
        out = self.root / "subset.faa"
        manifest = self.root / "selection.tsv"
        self.assertEqual(self._run_select(
            "--analysis", "cdd", "--out", str(out), "--manifest", str(manifest)), 0)
        self.assertEqual([h for h, _s in self._read_fasta(out)], ["p1", "p2", "p3", "p4"])
        rows = {r["seqid"]: r for r in self._read_manifest(manifest)}
        self.assertEqual(len(rows), 5)
        p1 = rows["p1"]
        self.assertEqual(p1["selected"], "1")
        self.assertEqual(p1["matched_analysis"], "cdd")
        self.assertEqual(p1["best_subject"], "bhlh_family")
        self.assertEqual(p1["hit_count"], "2")
        self.assertEqual(float(p1["best_evalue"]), 1e-10)
        p5 = rows["p5"]
        self.assertEqual(p5["selected"], "0")
        self.assertEqual(p5["hit_count"], "0")
        self.assertEqual(p5["matched_analysis"], "")

        runs = self.db.query("SELECT * FROM workflow_runs WHERE step='select-sequences'")
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["command"].startswith("operon select-sequences --file-id"))
        self.assertEqual(runs[0]["input_sha256"], self.file_row["sha256"])
        self.assertEqual(runs[0]["output_sha256"], sha256_file(out))

    def test_multiple_analyses_are_or_ed(self):
        pfam_job = self._add_job("pfam")
        self._add_alignment(pfam_job, "pfam", "p5", "PF00001", 1, 2, 25, 1e-4,
                            {"hit_type": "Specific"})
        self._add_alignment(pfam_job, "pfam", "p3", "PF00002", 1, 1, 15, 1e-30)
        out = self.root / "subset.faa"
        manifest = self.root / "selection.tsv"
        self.assertEqual(self._run_select(
            "--analysis", "cdd", "--analysis", "pfam",
            "--out", str(out), "--manifest", str(manifest)), 0)
        self.assertEqual([h for h, _s in self._read_fasta(out)],
                         ["p1", "p2", "p3", "p4", "p5"])
        rows = {r["seqid"]: r for r in self._read_manifest(manifest)}
        p3 = rows["p3"]
        self.assertEqual(p3["matched_analysis"], "cdd,pfam")
        self.assertEqual(p3["hit_count"], "2")
        self.assertEqual(p3["best_subject"], "PF00002")
        self.assertEqual(float(p3["best_evalue"]), 1e-30)
        self.assertEqual(rows["p5"]["matched_analysis"], "pfam")
        self.assertEqual(rows["p5"]["hit_count"], "1")

    def test_require_no_hit_outputs_the_complement(self):
        out = self.root / "subset.faa"
        self.assertEqual(self._run_select(
            "--analysis", "cdd", "--subject-like", "bhlh_family", "--require-no-hit",
            "--out", str(out)), 0)
        # p1/p2/p3 match subject_id 'bhlh_family'; p4 (CD99999) and p5 do not.
        self.assertEqual([h for h, _s in self._read_fasta(out)], ["p4", "p5"])

    def test_require_no_hit_falls_back_to_fasta_universe(self):
        self.db.conn.execute("DELETE FROM sequences WHERE file_id=?", (self.file_id,))
        self.db.conn.commit()
        out = self.root / "subset.faa"
        self.assertEqual(self._run_select(
            "--analysis", "cdd", "--require-no-hit", "--out", str(out)), 0)
        self.assertEqual([h for h, _s in self._read_fasta(out)], ["p5"])

    def test_hit_type_filter(self):
        out = self.root / "subset.faa"
        manifest = self.root / "selection.tsv"
        self.assertEqual(self._run_select(
            "--analysis", "cdd", "--hit-type", "NonSpecific",
            "--out", str(out), "--manifest", str(manifest)), 0)
        self.assertEqual([h for h, _s in self._read_fasta(out)], ["p1"])
        rows = {r["seqid"]: r for r in self._read_manifest(manifest)}
        self.assertEqual(rows["p1"]["hit_count"], "1")
        self.assertEqual(rows["p1"]["best_subject"], "other_dom")

    def test_min_span_filter(self):
        out = self.root / "subset.faa"
        self.assertEqual(self._run_select(
            "--analysis", "cdd", "--min-span", "35", "--out", str(out)), 0)
        self.assertEqual([h for h, _s in self._read_fasta(out)], ["p2", "p4"])

    def test_evalue_max_filter(self):
        out = self.root / "subset.faa"
        self.assertEqual(self._run_select(
            "--analysis", "cdd", "--evalue-max", "1e-8", "--out", str(out)), 0)
        self.assertEqual([h for h, _s in self._read_fasta(out)], ["p1", "p2", "p4"])

    def test_entity_type_filter_restricts_counted_alignments(self):
        out = self.root / "subset.faa"
        self.assertEqual(self._run_select(
            "--analysis", "cdd", "--entity-type", "assembly", "--out", str(out)), 0)
        self.assertEqual(self._read_fasta(out), [])
        self.assertEqual(out.read_text(encoding="utf-8"), "")

    def test_no_hit_criteria_is_rejected(self, capsys):
        out = self.root / "subset.faa"
        self.assertEqual(self._run_select("--out", str(out)), 2)
        self.assertIn("no hit criteria", capsys.readouterr().err)
        self.assertFalse(out.exists())

    def test_remote_only_file_gives_actionable_error(self, capsys):
        self.db.conn.execute(
            "UPDATE files SET status='REMOTE_ONLY' WHERE file_id=?", (self.file_id,))
        self.db.conn.commit()
        out = self.root / "subset.faa"
        self.assertEqual(self._run_select("--analysis", "cdd", "--out", str(out)), 2)
        self.assertIn("operon pull", capsys.readouterr().err)
        self.assertFalse(out.exists())
