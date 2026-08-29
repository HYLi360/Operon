"""Regression coverage for the P0 correctness guarantees."""

from __future__ import annotations

import csv
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
from operon.schema import Schema


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

    def test_metadata_report_includes_files_and_custom_fields_without_becoming_an_import_source(self):
        db = self._db()
        self._add_assembly(db)
        source = self.root / "assembly.fa"
        source.write_text(_fasta(), encoding="utf-8")
        file_row = ingest_file(db, self.project, source, "assembly", "ASM_000001", "genome_fasta")
        self.assertTrue(qc_file(db, self.project, file_row["file_id"])["ok"])
        data_source = db.register_data_source({
            "source_type": "insdc", "provider": "NCBI",
            "database_name": "GenBank", "record_url": "https://example.invalid/GCA_000001",
        })
        db.link_data_source(data_source["source_id"], [
            ("assembly", "ASM_000001"), ("file", file_row["file_id"]),
        ])

        schema_doc = yaml.safe_load(self.project.schema_path.read_text(encoding="utf-8"))
        schema_doc["tables"]["organisms"]["fields"]["provenance_note"] = {
            "type": "string", "description": "Project-defined round-trip field",
        }
        self.project.schema_path.write_text(yaml.safe_dump(schema_doc, sort_keys=False), encoding="utf-8")
        db.ensure_metadata_columns(Schema.from_file(self.project.schema_path))
        db.conn.execute("UPDATE organisms SET provenance_note='kept exactly' WHERE organism_id='ORG_000001'")
        db.conn.commit()

        self.assertEqual(main(["--project", str(self.root), "report", "metadata"]), 0)
        organisms = (self.root / "reports" / "metadata" / "organisms.tsv").read_text(encoding="utf-8")
        files = (self.root / "reports" / "metadata" / "files.tsv").read_text(encoding="utf-8")
        sources = (self.root / "reports" / "metadata" / "data_sources.tsv").read_text(encoding="utf-8")
        source_links = (self.root / "reports" / "metadata" / "source_links.tsv").read_text(encoding="utf-8")
        self.assertIn("provenance_note", organisms)
        self.assertIn("kept exactly", organisms)
        self.assertIn(file_row["file_id"], files)
        self.assertIn(data_source["source_id"], sources)
        self.assertIn("GenBank", sources)
        self.assertIn(file_row["file_id"], source_links)
        self.assertEqual(db.query("SELECT provenance_note FROM organisms")[0]["provenance_note"], "kept exactly")

    def test_idempotent_ingest_repairs_missing_entity_file_link(self):
        db = self._db()
        self._add_assembly(db)
        source = self.root / "assembly.fa"
        source.write_text(_fasta(), encoding="utf-8")
        first = ingest_file(db, self.project, source, "assembly", "ASM_000001", "genome_fasta")
        db.conn.execute("UPDATE assemblies SET fasta_file_id=NULL WHERE assembly_id='ASM_000001'")
        db.conn.commit()

        second = ingest_file(db, self.project, source, "assembly", "ASM_000001", "genome_fasta")

        self.assertEqual(second["file_id"], first["file_id"])
        linked = db.query("SELECT fasta_file_id FROM assemblies WHERE assembly_id='ASM_000001'")[0]
        self.assertEqual(linked["fasta_file_id"], first["file_id"])

    def test_rapid_ingests_keep_distinct_workflow_rows(self):
        db = self._db()
        self._add_assembly(db)
        first = self.root / "assembly.fa"
        first.write_text(_fasta(), encoding="utf-8")
        annotation = self.root / "annotation.gff3"
        annotation.write_text("##gff-version 3\n", encoding="utf-8")
        db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
            "annotation_source": "test", "annotation_version": 1,
        })

        ingest_file(db, self.project, first, "assembly", "ASM_000001", "genome_fasta")
        ingest_file(db, self.project, annotation, "annotation", "ANN_000001", "annotation_gff3")

        rows = db.query("SELECT run_id, command FROM workflow_runs WHERE step='ingest' ORDER BY command")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["run_id"], rows[1]["run_id"])

    def test_table_import_is_atomic_on_late_database_failure(self):
        db = self._db()
        db.conn.execute(
            "CREATE TRIGGER reject_second_organism BEFORE INSERT ON organisms "
            "WHEN NEW.organism_id='ORG_000002' BEGIN SELECT RAISE(ABORT, 'synthetic late import failure'); END"
        )
        db.conn.commit()
        path = self.root / "organisms.csv"
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["organism_id", "scientific_name", "taxonomy_source"])
            writer.writeheader()
            writer.writerows([
                {"organism_id": "ORG_000001", "scientific_name": "First", "taxonomy_source": "NCBI"},
                {"organism_id": "ORG_000002", "scientific_name": "Second", "taxonomy_source": "NCBI"},
            ])

        self.assertEqual(main([
            "--project", str(self.root), "import", "table", "--table", "organisms",
            "--file", str(path), "--yes",
        ]), 1)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM organisms")[0]["n"], 0)

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

    def test_schema_2_2_adds_remote_location_and_executor_provenance(self):
        old_path = self.root / "schema-2.1.sqlite"
        conn = sqlite3.connect(old_path)
        conn.executescript(
            """
            CREATE TABLE workflow_runs (
                run_id TEXT PRIMARY KEY, parent_run_id TEXT, entity_type TEXT, entity_id TEXT,
                step TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, exit_code INTEGER, command TEXT, tool TEXT,
                tool_version TEXT, parameter_set TEXT, input_sha256 TEXT,
                output_sha256 TEXT, threads INTEGER, max_rss_mb REAL, log_file TEXT,
                stdout_file TEXT, stderr_file TEXT, error TEXT
            );
            """
        )
        conn.close()
        migrated = Database(old_path)
        self.addCleanup(migrated.close)
        columns = set(migrated.table_columns("workflow_runs"))
        self.assertTrue({"executor", "scheduler_job_id", "execution_details"}.issubset(columns))
        tables = {row["name"] for row in migrated.query(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn("file_locations", tables)

    def test_schema_2_3_adds_taxonomy_and_coverage_history(self):
        db = self._db()
        tables = {row["name"] for row in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertTrue({
            "taxonomy_snapshots", "taxonomy_nodes", "taxonomy_aliases",
            "taxonomy_reference_sets", "coverage_reports", "coverage_report_metrics",
        }.issubset(tables))

    def test_schema_2_4_adds_normalized_source_provenance(self):
        old_path = self.root / "schema-2.3.sqlite"
        sqlite3.connect(old_path).close()
        db = Database(old_path)
        self.addCleanup(db.close)
        tables = {row["name"] for row in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertTrue({"data_sources", "source_links"}.issubset(tables))
        source = db.register_data_source({
            "source_type": "non_insdc",
            "provider": "Example Institute",
            "database_name": "Genome Portal",
            "record_url": "https://example.invalid/record/1",
            "citation": "doi:10.0000/example",
            "license_name": "CC-BY-4.0",
        })
        reused = db.register_data_source({
            "source_type": "non_insdc",
            "provider": "Example Institute",
            "database_name": "Genome Portal",
            "record_url": "https://example.invalid/record/1",
            "citation": "doi:10.0000/example",
            "license_name": "CC-BY-4.0",
        })
        self.assertEqual(reused["source_id"], source["source_id"])

    def test_schema_2_5_adds_local_file_verification_cache(self):
        old_path = self.root / "schema-2.4.sqlite"
        sqlite3.connect(old_path).close()
        db = Database(old_path)
        self.addCleanup(db.close)
        columns = set(db.table_columns("local_file_verifications"))
        self.assertEqual(columns, {
            "file_id", "sha256", "size_bytes", "device", "inode",
            "mtime_ns", "ctime_ns", "verified_at",
        })
