"""Schema 2.9 migration tests: file lineage, recipe snapshots, run resources."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.database import SCHEMA_VERSION, Database


class TestSchema29(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "meta.sqlite")
        self.addCleanup(self.db.close)

    def test_schema_2_9_columns_and_tables_exist(self):
        self.assertEqual(SCHEMA_VERSION, "2.9")
        for column in ("max_rss_mb", "duration_seconds", "avg_rss_mb", "cpu_seconds"):
            self.assertIn(column, self.db.table_columns("workflow_runs"))
        self.assertIn("recipe_snapshot_id", self.db.table_columns("analysis_jobs"))
        self.assertEqual(
            self.db.table_columns("file_lineage"),
            ["lineage_id", "derived_file_id", "input_file_id", "workflow_run_id", "created_at"],
        )
        self.assertEqual(
            self.db.table_columns("recipe_snapshots"),
            ["recipe_snapshot_id", "recipe_name", "recipe_version", "recipe_sha256",
             "recipe_document", "recorded_at"],
        )
        row = self.db.conn.execute(
            "SELECT migration_id FROM schema_migrations "
            "WHERE migration_id='2.9-lineage-recipes-resources'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_record_recipe_is_content_addressed(self):
        document = {"tool": "blastn", "params": {"evalue": "1e-5"}}
        first = self.db.record_recipe("pangenome", 1, document)
        second = self.db.record_recipe("pangenome", 1, dict(reversed(list(document.items()))))
        self.assertEqual(first, second)
        rows = self.db.conn.execute("SELECT * FROM recipe_snapshots").fetchall()
        self.assertEqual(len(rows), 1)
        stored = json.loads(rows[0]["recipe_document"])
        self.assertEqual(stored, {key: document[key] for key in sorted(document)})
        self.assertTrue(rows[0]["recipe_sha256"])
        self.assertTrue(rows[0]["recorded_at"])
        # A new version or a changed document records a new snapshot.
        third = self.db.record_recipe("pangenome", 2, document)
        fourth = self.db.record_recipe("pangenome", 1, {**document, "params": {"evalue": "1e-3"}})
        self.assertNotEqual(first, third)
        self.assertNotEqual(first, fourth)
        count = self.db.conn.execute("SELECT COUNT(*) AS n FROM recipe_snapshots").fetchone()
        self.assertEqual(count["n"], 3)

    def test_migration_backfills_dropped_2_9_objects(self):
        self.db.close()
        path = Path(self.tmp.name) / "meta.sqlite"
        conn = sqlite3.connect(str(path))
        conn.execute("ALTER TABLE workflow_runs DROP COLUMN duration_seconds")
        conn.execute("ALTER TABLE workflow_runs DROP COLUMN avg_rss_mb")
        conn.execute("ALTER TABLE workflow_runs DROP COLUMN cpu_seconds")
        conn.execute("ALTER TABLE analysis_jobs DROP COLUMN recipe_snapshot_id")
        conn.execute("DROP TABLE file_lineage")
        conn.execute("DROP TABLE recipe_snapshots")
        conn.commit()
        conn.close()
        migrated = Database(path)
        self.assertIn("duration_seconds", migrated.table_columns("workflow_runs"))
        self.assertIn("avg_rss_mb", migrated.table_columns("workflow_runs"))
        self.assertIn("cpu_seconds", migrated.table_columns("workflow_runs"))
        self.assertIn("recipe_snapshot_id", migrated.table_columns("analysis_jobs"))
        self.assertIn("lineage_id", migrated.table_columns("file_lineage"))
        self.assertIn("recipe_snapshot_id", migrated.table_columns("recipe_snapshots"))
        # Reopening the migrated database is idempotent.
        migrated.close()
        again = Database(path)
        self.addCleanup(again.close)
        self.assertIn("recipe_snapshot_id", again.table_columns("analysis_jobs"))
        self.assertIn("lineage_id", again.table_columns("file_lineage"))
