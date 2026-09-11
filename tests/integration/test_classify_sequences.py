"""End-to-end classify-sequences: ingest, QC, analysis hits, CLI classification.

The fixture profile mirrors the legacy bHLH prepare-stage tier rules
(Specific + complete + span>=40 -> A; span>=30 and incomplete!=NC -> B;
rescue i_evalue<=1e-5 + span>=30 -> R; no core hit -> U; otherwise C) as a
parity reference: every threshold lives in the YAML, not in the engine.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import ingest_file

BHLH_PROFILE = {
    "kind": "sequence_classification",
    "version": 1,
    "description": "bHLH-shaped tier classification fixture (test parity reference)",
    "applies_to": {"entity_type": "annotation", "file_role": "protein_fasta"},
    "sources": {
        "core": {
            "analysis": "rpsbproc_cdd",
            # Core hit: CDD cl00081 or a bhlh/bhlh_* short name ...
            "filter": [
                {"any": [
                    {"field": "subject_id", "operator": "==", "value": "cl00081"},
                    {"field": "short_name", "operator": "like", "value": "bhlh"},
                    {"field": "short_name", "operator": "like", "value": "bhlh_%"},
                ]},
                # ... but a bhlh-myc_n short name never counts as a core hit.
                {"not": {"field": "short_name", "operator": "==", "value": "bhlh-myc_n"}},
            ],
            "best_by": [
                {"field": "hit_type", "rank": {"Specific": 0, "Motif": 1, "Partial": 2}},
                {"field": "incomplete", "rank": {"-": 0, "NC": 1}},
                {"field": "evalue", "direction": "asc"},
                {"field": "bitscore", "direction": "desc"},
                {"field": "span", "direction": "desc"},
            ],
        },
        "rescue": {
            "analysis": "hmmsearch_pf00010",
            "filter": [{"field": "subject_id", "operator": "==", "value": "PF00010"}],
            "best_by": [{"field": "evalue", "direction": "asc"}],
        },
    },
    "rules": [
        {"label": "A", "source": "core", "when": [
            {"field": "hit_type", "operator": "==", "value": "Specific"},
            {"field": "incomplete", "operator": "==", "value": "-"},
            {"field": "span", "operator": ">=", "value": 40},
        ]},
        {"label": "B", "source": "core", "when": [
            {"field": "span", "operator": ">=", "value": 30},
            {"field": "incomplete", "operator": "!=", "value": "NC"},
        ]},
        {"label": "R", "source": "rescue", "when": [
            {"field": "i_evalue", "operator": "<=", "value": 1e-5},
            {"field": "span", "operator": ">=", "value": 30},
        ]},
        {"label": "U", "source": "core", "absent": True},
        {"label": "C", "default": True},
    ],
}


class TestClassifySequencesCLI(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root),
                               "--project-id", "PRJ_BHLH_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        (self.project.profiles_dir / "bhlh.yaml").write_text(
            yaml.safe_dump(BHLH_PROFILE, sort_keys=False), encoding="utf-8")
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
        fasta = self.root / "proteins.faa"
        fasta.write_text(
            "".join(f">b{index}\n{'M' * 60}A\n" for index in range(1, 6)),
            encoding="utf-8")
        self.file_row = ingest_file(
            self.db, self.project, fasta, "annotation", "ANN_000001", "protein_fasta")
        self.assertEqual(main([
            "--project", str(self.root), "qc",
            "--entity-type", "annotation", "--entity-id", "ANN_000001"]), 0)

    def _add_hits(self, analysis, alignments):
        sequence_number = self.db.query("SELECT COUNT(*) AS n FROM analysis_jobs")[0]["n"]
        self.db.insert_row("analysis_jobs", {
            "analysis_name": analysis, "entity_type": "annotation",
            "entity_id": "ANN_000001", "file_id": self.file_row["file_id"],
            "tool": "fake", "tool_version": "1.0", "parameter_set": "{}",
            "parameter_sha256": f"params-{sequence_number}",
            "input_sha256": self.file_row["sha256"],
            "database_identity": "db1", "status": "completed",
            "started_at": "2026-09-11T00:00:00+00:00"})
        job_id = self.db.query("SELECT MAX(job_id) AS j FROM analysis_jobs")[0]["j"]
        for row in alignments:
            self.db.insert_row("analysis_alignments", {
                "job_id": job_id, "entity_type": "annotation",
                "entity_id": "ANN_000001", "file_id": self.file_row["file_id"],
                "analysis_name": analysis, "hit_rank": 1, **row})
        return job_id

    def _labels(self):
        return {
            row["seqid"]: row["label"]
            for row in self.db.query(
                "SELECT seqid, label FROM sequence_labels WHERE profile_name='bhlh'")
        }

    def test_bhlh_tiers_end_to_end(self, capsys=None):
        self._add_hits("rpsbproc_cdd", [
            # b1: Specific, complete, span 45 -> A
            {"query_id": "b1 protein description", "subject_id": "cl00081",
             "query_start": 10, "query_end": 54, "evalue": 1e-12, "bitscore": 90.0,
             "extra_json": json.dumps({
                 "hit_type": "Specific", "incomplete": "-", "short_name": "bhlh_1"})},
            # b2: Specific, complete, span 35 -> B
            {"query_id": "b2", "subject_id": "cl00081",
             "query_start": 1, "query_end": 35, "evalue": 1e-8, "bitscore": 60.0,
             "extra_json": json.dumps({
                 "hit_type": "Specific", "incomplete": "-", "short_name": "bhlh"})},
            # b5: Specific but span 25 -> neither A nor B -> C
            {"query_id": "b5", "subject_id": "cl00081",
             "query_start": 1, "query_end": 25, "evalue": 1e-7, "bitscore": 50.0,
             "extra_json": json.dumps({
                 "hit_type": "Specific", "incomplete": "-", "short_name": "bhlh_9"})},
            # b4: only an excluded bhlh-myc_n row -> no core hit -> U
            {"query_id": "b4", "subject_id": "cd06214",
             "query_start": 1, "query_end": 55, "evalue": 1e-20, "bitscore": 99.0,
             "extra_json": json.dumps({
                 "hit_type": "Specific", "incomplete": "-", "short_name": "bhlh-myc_n"})},
        ])
        self._add_hits("hmmsearch_pf00010", [
            # b3: no core hit, rescue PF00010 i_evalue 1e-8 span 40 -> R
            {"query_id": "b3", "subject_id": "PF00010",
             "query_start": 3, "query_end": 42, "evalue": 1e-8, "bitscore": 55.0,
             "extra_json": json.dumps({"i_evalue": 1e-8})},
        ])

        self.assertEqual(main([
            "--project", str(self.root), "classify-sequences", "--profile", "bhlh"]), 0)
        self.assertEqual(self._labels(), {
            "b1": "A", "b2": "B", "b3": "R", "b4": "U", "b5": "C",
        })
        details = {
            row["seqid"]: json.loads(row["details_json"])
            for row in self.db.query("SELECT seqid, details_json FROM sequence_labels")
        }
        self.assertEqual(details["b1"]["source"], "core")
        self.assertEqual(details["b1"]["observed"]["span"], 45)
        self.assertEqual(details["b3"]["source"], "rescue")
        self.assertTrue(details["b4"]["absent"])
        self.assertTrue(details["b5"]["default"])
        snapshots = self.db.query(
            "SELECT * FROM qc_profiles WHERE profile_name='bhlh'")
        self.assertEqual(len(snapshots), 1)
        changes = self.db.query(
            "SELECT COUNT(*) AS n FROM changes WHERE object_type='sequence_label'")
        self.assertEqual(changes[0]["n"], 5)

        # Idempotent rerun: no label writes, no new audit rows.
        self.assertEqual(main([
            "--project", str(self.root), "classify-sequences", "--profile", "bhlh"]), 0)
        self.assertEqual(self._labels(), {
            "b1": "A", "b2": "B", "b3": "R", "b4": "U", "b5": "C",
        })
        changes = self.db.query(
            "SELECT COUNT(*) AS n FROM changes WHERE object_type='sequence_label'")
        self.assertEqual(changes[0]["n"], 5)
        runs = self.db.query(
            "SELECT status FROM workflow_runs WHERE step='classify-sequences' "
            "ORDER BY started_at")
        self.assertEqual([row["status"] for row in runs], ["completed", "completed"])

    def test_unknown_profile_fails(self):
        self.assertEqual(main([
            "--project", str(self.root), "classify-sequences", "--profile", "missing"]), 2)

    def test_wrong_kind_fails(self):
        self.assertEqual(main([
            "--project", str(self.root), "classify-sequences",
            "--profile", "assembly_production_v1"]), 2)
