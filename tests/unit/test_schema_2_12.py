"""Schema 2.12 migration tests: slim WITHOUT ROWID sequences registry."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from operon.database import Database
from tests.helpers import PytestAssertions

LEGACY_SEQUENCES_DDL = """
CREATE TABLE sequences (
    sequence_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL REFERENCES files(file_id),
    file_sha256 TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    seqid TEXT NOT NULL,
    length INTEGER NOT NULL,
    UNIQUE (file_id, seqid)
);
CREATE INDEX idx_sequences_seqid ON sequences(seqid);
CREATE INDEX idx_sequences_entity ON sequences(entity_type, entity_id);
"""


class TestSchema212(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "meta.sqlite"

    def _build_legacy_sequences(self) -> None:
        """Replace the current-shape sequences table with the 2.10/2.11 one."""
        conn = sqlite3.connect(str(self.path))
        conn.execute("DROP TABLE sequences")
        conn.executescript(LEGACY_SEQUENCES_DDL)
        conn.execute(
            "INSERT INTO files(file_id, entity_type, entity_id, file_role, format, "
            "compression, relative_path, source_url, size_bytes, sha256, "
            "downloaded_at, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "FIL_000001",
                "assembly",
                "ASM_000001",
                "assembly",
                "fasta",
                "none",
                "raw/a.fasta",
                None,
                12,
                "a" * 64,
                None,
                "CHECKSUM_VERIFIED",
            ),
        )
        conn.executemany(
            "INSERT INTO sequences(file_id, file_sha256, entity_type, entity_id, "
            "seqid, length) VALUES(?,?,?,?,?,?)",
            [
                ("FIL_000001", "a" * 64, "assembly", "ASM_000001", "contig_a", 100),
                ("FIL_000001", "a" * 64, "assembly", "ASM_000001", "contig_b", 200),
                ("FIL_000001", "a" * 64, "assembly", "ASM_000001", "contig_c", 300),
            ],
        )
        conn.execute(
            "DELETE FROM schema_migrations WHERE migration_id='2.12-sequences-slim'"
        )
        conn.commit()
        conn.close()

    def _open(self) -> Database:
        db = Database(self.path)
        self.addCleanup(db.close)
        return db

    def test_legacy_sequences_table_is_rebuilt_without_redundant_columns(self):
        db = self._open()
        db.close()  # first open establishes the surrounding schema
        self._build_legacy_sequences()
        migrated = self._open()
        self.assertEqual(
            migrated.table_columns("sequences"),
            ["file_id", "seqid", "length"],
        )
        rows = migrated.query(
            "SELECT file_id, seqid, length FROM sequences ORDER BY seqid"
        )
        self.assertEqual(
            [(row["file_id"], row["seqid"], row["length"]) for row in rows],
            [
                ("FIL_000001", "contig_a", 100),
                ("FIL_000001", "contig_b", 200),
                ("FIL_000001", "contig_c", 300),
            ],
        )
        indexes = {
            row["name"]
            for row in migrated.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        self.assertIn("idx_sequences_seqid", indexes)
        self.assertFalse("idx_sequences_entity" in indexes)
        self.assertFalse("sqlite_autoindex_sequences_1" in indexes)
        row = migrated.conn.execute(
            "SELECT migration_id FROM schema_migrations "
            "WHERE migration_id='2.12-sequences-slim'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_rebuilt_table_enforces_file_seqid_uniqueness(self):
        db = self._open()
        db.close()
        self._build_legacy_sequences()
        migrated = self._open()
        with self.assertRaises(sqlite3.IntegrityError):
            migrated.conn.execute(
                "INSERT INTO sequences(file_id, seqid, length) VALUES(?,?,?)",
                ("FIL_000001", "contig_a", 999),
            )

    def test_reopen_after_rebuild_is_idempotent(self):
        db = self._open()
        db.close()
        self._build_legacy_sequences()
        first = self._open()
        first.close()
        again = self._open()
        self.assertEqual(
            again.table_columns("sequences"),
            ["file_id", "seqid", "length"],
        )
        count = again.query("SELECT COUNT(*) AS n FROM sequences")[0]["n"]
        self.assertEqual(count, 3)
