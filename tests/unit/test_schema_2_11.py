"""Schema 2.11 migration tests: per-sequence classification labels."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.database import SCHEMA_VERSION, Database


class TestSchema211(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "meta.sqlite")
        self.addCleanup(self.db.close)

    def test_schema_2_11_tables_exist(self):
        self.assertEqual(SCHEMA_VERSION, "2.11")
        self.assertEqual(
            self.db.table_columns("sequence_labels"),
            ["file_id", "seqid", "label", "profile_name",
             "profile_sha256", "details_json", "decided_at"],
        )
        indexes = {
            row["name"]
            for row in self.db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        self.assertIn("idx_sequence_labels_label", indexes)
        row = self.db.conn.execute(
            "SELECT migration_id FROM schema_migrations "
            "WHERE migration_id='2.11-sequence-labels'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_migration_backfills_dropped_2_11_objects(self):
        self.db.close()
        path = Path(self.tmp.name) / "meta.sqlite"
        conn = sqlite3.connect(str(path))
        conn.execute("DROP TABLE sequence_labels")
        conn.execute(
            "DELETE FROM schema_migrations WHERE migration_id='2.11-sequence-labels'"
        )
        conn.commit()
        conn.close()
        migrated = Database(path)
        self.assertIn("profile_sha256", migrated.table_columns("sequence_labels"))
        row = migrated.conn.execute(
            "SELECT migration_id FROM schema_migrations "
            "WHERE migration_id='2.11-sequence-labels'"
        ).fetchone()
        self.assertIsNotNone(row)
        # Reopening the migrated database is idempotent.
        migrated.close()
        again = Database(path)
        self.addCleanup(again.close)
        self.assertIn("profile_sha256", again.table_columns("sequence_labels"))
