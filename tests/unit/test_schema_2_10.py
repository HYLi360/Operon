"""Schema 2.10 migration tests: per-sequence lengths and analysis alignments."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.database import Database


class TestSchema210(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "meta.sqlite")
        self.addCleanup(self.db.close)

    def test_schema_2_10_tables_exist(self):
        self.assertEqual(
            self.db.table_columns("sequences"),
            ["sequence_row_id", "file_id", "file_sha256", "entity_type",
             "entity_id", "seqid", "length"],
        )
        self.assertEqual(
            self.db.table_columns("analysis_alignments"),
            ["alignment_id", "job_id", "entity_type", "entity_id", "file_id",
             "analysis_name", "query_id", "subject_id", "hit_rank",
             "query_start", "query_end", "subject_start", "subject_end",
             "evalue", "bitscore", "percent_identity", "extra_json"],
        )
        indexes = {
            row["name"]
            for row in self.db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        for name in (
                "idx_sequences_seqid",
                "idx_sequences_entity",
                "idx_analysis_alignments_query",
                "idx_analysis_alignments_subject",
                "idx_analysis_alignments_job",
        ):
            self.assertIn(name, indexes)
        row = self.db.conn.execute(
            "SELECT migration_id FROM schema_migrations "
            "WHERE migration_id='2.10-sequences-alignments'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_migration_backfills_dropped_2_10_objects(self):
        self.db.close()
        path = Path(self.tmp.name) / "meta.sqlite"
        conn = sqlite3.connect(str(path))
        conn.execute("DROP TABLE sequences")
        conn.execute("DROP TABLE analysis_alignments")
        conn.execute(
            "DELETE FROM schema_migrations WHERE migration_id='2.10-sequences-alignments'"
        )
        conn.commit()
        conn.close()
        migrated = Database(path)
        self.assertIn("sequence_row_id", migrated.table_columns("sequences"))
        self.assertIn("alignment_id", migrated.table_columns("analysis_alignments"))
        row = migrated.conn.execute(
            "SELECT migration_id FROM schema_migrations "
            "WHERE migration_id='2.10-sequences-alignments'"
        ).fetchone()
        self.assertIsNotNone(row)
        # Reopening the migrated database is idempotent.
        migrated.close()
        again = Database(path)
        self.addCleanup(again.close)
        self.assertIn("sequence_row_id", again.table_columns("sequences"))
        self.assertIn("alignment_id", again.table_columns("analysis_alignments"))
