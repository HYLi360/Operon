"""Regression coverage for the P0 correctness guarantees."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from tests.helpers import PytestAssertions

import yaml

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import ingest_file, standardize_file
from operon.qc_module import qc_all, qc_file
from operon.release import create_release
from operon.rules import curate_decision, evaluate_entity
from operon.schema import Schema, write_tsv


def _fasta(length: int = 2500) -> str:
    return ">ctg1\n" + ("ACGT" * ((length + 3) // 4))[:length] + "\n"


class TestCorrectnessRegressions(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_P0_001"]), 0)
        self.project = load_project(self.root)

    def _db(self) -> Database:
        db = Database(self.project.db_path)
        self.addCleanup(db.close)
        return db

    def _add_assembly(self, db: Database) -> None:
        db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Correctus testii", "taxonomy_source": "NCBI",
        })
        db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        db.insert_row("assemblies", {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_level": "contig", "assembly_version": 1,
        })

    def test_metadata_export_import_round_trip_including_files_and_custom_fields(self):
        db = self._db()
        self._add_assembly(db)
        source = self.root / "assembly.fa"
        source.write_text(_fasta(), encoding="utf-8")
        file_row = ingest_file(db, self.project, source, "assembly", "ASM_000001", "genome_fasta")
        self.assertTrue(qc_file(db, self.project, file_row["file_id"])["ok"])

        schema_doc = yaml.safe_load(self.project.schema_path.read_text(encoding="utf-8"))
        schema_doc["tables"]["organisms"]["fields"]["provenance_note"] = {
            "type": "string", "description": "Project-defined round-trip field",
        }
        self.project.schema_path.write_text(yaml.safe_dump(schema_doc, sort_keys=False), encoding="utf-8")
        db.ensure_metadata_columns(Schema.from_file(self.project.schema_path))
        db.conn.execute("UPDATE organisms SET provenance_note='kept exactly' WHERE organism_id='ORG_000001'")
        db.conn.commit()

        self.assertEqual(main(["--project", str(self.root), "export-metadata"]), 0)
        schema = Schema.from_file(self.project.schema_path)
        before = {
            table: db.export_rows(table, schema.columns(table))
            for table in ("organisms", "samples", "runs", "assemblies", "annotations", "accessions", "files")
        }
        # Mutations made after export must disappear when the TSV snapshot is
        # restored, including rows in a header-only table.
        db.insert_row("organisms", {
            "organism_id": "ORG_000002", "scientific_name": "Post export", "taxonomy_source": "NCBI",
        })
        db.insert_row("accessions", {
            "internal_type": "assembly", "internal_id": "ASM_000001",
            "namespace": "TEST", "accession": "POST_EXPORT",
        })
        self.assertEqual(main(["--project", str(self.root), "import-metadata", "--replace"]), 0)
        after = {
            table: db.export_rows(table, schema.columns(table))
            for table in ("organisms", "samples", "runs", "assemblies", "annotations", "accessions", "files")
        }
        self.assertEqual(after, before)
        self.assertEqual(after["organisms"][0]["provenance_note"], "kept exactly")

    def test_metadata_replace_is_atomic_on_late_database_failure(self):
        db = self._db()
        db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Original", "taxonomy_source": "NCBI",
        })
        db.conn.execute(
            "CREATE TRIGGER reject_sample_import BEFORE INSERT ON samples "
            "BEGIN SELECT RAISE(ABORT, 'synthetic late import failure'); END"
        )
        db.conn.commit()
        schema = Schema.from_file(self.project.schema_path)
        write_tsv(
            self.project.metadata_dir / "organisms.tsv", schema.columns("organisms"),
            [{"organism_id": "ORG_000002", "scientific_name": "Replacement", "taxonomy_source": "NCBI"}],
        )
        write_tsv(
            self.project.metadata_dir / "samples.tsv", schema.columns("samples"),
            [{"sample_id": "SMP_000002", "organism_id": "ORG_000002"}],
        )

        self.assertEqual(main(["--project", str(self.root), "import-metadata", "--replace"]), 1)
        rows = db.query("SELECT organism_id, scientific_name FROM organisms ORDER BY organism_id")
        self.assertEqual([tuple(row) for row in rows], [("ORG_000001", "Original")])
        self.assertEqual(db.query("SELECT COUNT(*) FROM samples")[0][0], 0)

    def test_raw_standardized_and_release_are_independent_copies_by_default(self):
        db = self._db()
        self._add_assembly(db)
        source = self.root / "source.fa"
        source.write_text(_fasta(), encoding="utf-8")
        file_row = ingest_file(db, self.project, source, "assembly", "ASM_000001", "genome_fasta")
        standardized = Path(standardize_file(db, self.project, file_row["file_id"])["target"])
        raw = self.root / file_row["relative_path"]
        self.assertNotEqual(source.stat().st_ino, raw.stat().st_ino)
        self.assertNotEqual(raw.stat().st_ino, standardized.stat().st_ino)

        self.assertTrue(qc_file(db, self.project, file_row["file_id"])["ok"])
        evaluate_entity(db, self.project, "assembly", "ASM_000001", "assembly_production_v1")
        release = create_release(db, self.project, "copy-default", "assembly_production_v1")
        released = Path(release["path"]) / "data" / "assembly" / "ASM_000001" / raw.name
        self.assertNotEqual(raw.stat().st_ino, released.stat().st_ino)
        self.assertNotEqual(standardized.stat().st_ino, released.stat().st_ino)

    def test_query_rejects_database_modification(self):
        db = self._db()
        db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Read only", "taxonomy_source": "NCBI",
        })
        self.assertEqual(
            main(["--project", str(self.root), "query", "UPDATE organisms SET scientific_name='mutated'"]), 2
        )
        self.assertEqual(
            main(["--project", str(self.root), "query", "PRAGMA user_version=99"]), 2
        )
        self.assertEqual(db.query("SELECT scientific_name FROM organisms")[0][0], "Read only")
        self.assertEqual(db.query("PRAGMA user_version")[0][0], 0)
        self.assertEqual(main(["--project", str(self.root), "query", "SELECT COUNT(*) AS n FROM organisms"]), 0)

    def test_qc_identity_distinguishes_files_of_the_same_entity(self):
        db = self._db()
        db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Reads", "taxonomy_source": "NCBI",
        })
        db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        db.insert_row("runs", {
            "run_id": "RUN_000001", "sample_id": "SMP_000001", "library_layout": "PAIRED",
            "library_strategy": "WGS", "library_source": "GENOMIC", "platform": "ILLUMINA",
        })
        fastq = "@r1\nACGT\n+\nIIII\n@r2\nTGCA\n+\nIIII\n"
        r1 = self.root / "r1.fastq"
        r2 = self.root / "r2.fastq"
        r1.write_text(fastq, encoding="utf-8")
        r2.write_text(fastq, encoding="utf-8")
        first = ingest_file(db, self.project, r1, "run", "RUN_000001", "reads_r1")
        second = ingest_file(db, self.project, r2, "run", "RUN_000001", "reads_r2")
        self.assertTrue(all(item["ok"] for item in qc_all(db, self.project, entity_type="run")))

        rows = db.query(
            "SELECT file_id, file_sha256, input_identity, metric_numeric FROM qc_results "
            "WHERE entity_type='run' AND entity_id='RUN_000001' AND metric_name='read_count' ORDER BY file_id"
        )
        self.assertEqual([row["file_id"] for row in rows], [first["file_id"], second["file_id"]])
        self.assertEqual(len({row["input_identity"] for row in rows}), 2)
        self.assertTrue(all(row["file_sha256"] for row in rows))

    def test_profile_snapshots_and_decision_history_are_append_only(self):
        db = self._db()
        self._add_assembly(db)
        source = self.root / "history.fa"
        source.write_text(_fasta(), encoding="utf-8")
        file_row = ingest_file(db, self.project, source, "assembly", "ASM_000001", "genome_fasta")
        self.assertTrue(qc_file(db, self.project, file_row["file_id"])["ok"])

        first = evaluate_entity(db, self.project, "assembly", "ASM_000001", "assembly_production_v1")
        second = evaluate_entity(db, self.project, "assembly", "ASM_000001", "assembly_production_v1")
        self.assertEqual(first["profile_sha256"], second["profile_sha256"])
        self.assertEqual(db.query("SELECT COUNT(*) FROM qc_profiles")[0][0], 1)
        self.assertEqual(db.query("SELECT COUNT(*) FROM decisions")[0][0], 2)

        profile_path = self.project.profiles_dir / "assembly_production_v1.yaml"
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        next(rule for rule in profile["required"] if rule["metric"] == "total_length")["value"] = 10000
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        third = evaluate_entity(db, self.project, "assembly", "ASM_000001", "assembly_production_v1")
        self.assertEqual(third["decision"], "FAIL")
        self.assertNotEqual(first["profile_sha256"], third["profile_sha256"])
        self.assertEqual(db.query("SELECT COUNT(*) FROM qc_profiles")[0][0], 2)
        self.assertEqual(db.query("SELECT COUNT(*) FROM decisions")[0][0], 3)

        curate_decision(
            db, "assembly", "ASM_000001", "assembly_production_v1", "PASS",
            reviewer="reviewer", reason="validated independently",
        )
        decisions = db.query("SELECT decision_id, curated_decision FROM decisions ORDER BY decision_id")
        self.assertIsNone(decisions[0]["curated_decision"])
        self.assertIsNone(decisions[1]["curated_decision"])
        self.assertEqual(decisions[2]["curated_decision"], "PASS")
        self.assertEqual(db.effective_decision("assembly", "ASM_000001", "assembly_production_v1"), "PASS")

    def test_v1_qc_and_decisions_migrate_without_data_loss(self):
        legacy_path = self.root / "legacy.sqlite"
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            CREATE TABLE qc_results (
                qc_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
                qc_stage TEXT NOT NULL, metric_name TEXT NOT NULL,
                metric_value TEXT NOT NULL, metric_numeric REAL, metric_unit TEXT,
                tool TEXT NOT NULL, tool_version TEXT NOT NULL,
                parameter_set TEXT NOT NULL, evaluated_at TEXT NOT NULL,
                UNIQUE(entity_type, entity_id, qc_stage, metric_name, tool, tool_version, parameter_set)
            );
            CREATE TABLE decisions (
                decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, profile TEXT NOT NULL,
                profile_version INTEGER, decision TEXT NOT NULL, curated_decision TEXT,
                reason_codes TEXT NOT NULL, observed TEXT NOT NULL, thresholds TEXT NOT NULL,
                evaluated_at TEXT NOT NULL, curated_by TEXT, curated_reason TEXT,
                curated_evidence TEXT, curated_at TEXT,
                UNIQUE(entity_type, entity_id, profile)
            );
            INSERT INTO qc_results(
                entity_type, entity_id, qc_stage, metric_name, metric_value,
                tool, tool_version, parameter_set, evaluated_at
            ) VALUES('assembly','ASM_000001','legacy','legacy_metric','1','legacy','1','p','2026-01-01');
            INSERT INTO decisions(
                entity_type, entity_id, profile, profile_version, decision,
                reason_codes, observed, thresholds, evaluated_at
            ) VALUES('assembly','ASM_000001','legacy_profile',1,'PASS','[]','{}','{}','2026-01-01');
            """
        )
        conn.commit()
        conn.close()

        migrated = Database(legacy_path)
        self.addCleanup(migrated.close)
        qc = migrated.query("SELECT input_identity, metric_name FROM qc_results")
        self.assertEqual(qc[0]["metric_name"], "legacy_metric")
        self.assertTrue(qc[0]["input_identity"].startswith("legacy:"))
        decision = migrated.query("SELECT decision FROM current_decisions")
        self.assertEqual(decision[0]["decision"], "PASS")

