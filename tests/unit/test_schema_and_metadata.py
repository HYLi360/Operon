"""Metadata schema validation and TSV/SQLite import tests."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.database import Database
from operon.schema import Schema, write_tsv


class TestSchemaAndMetadata(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_TEST_001"]), 0)

    def _write(self, table: str, columns: list[str], rows: list[dict]):
        write_tsv(self.root / "metadata" / f"{table}.tsv", columns, rows)

    def test_valid_import_and_export(self):
        self._write("organisms", ["organism_id", "scientific_name", "taxon_id", "taxonomy_source"],
                    [{"organism_id": "ORG_000001", "scientific_name": "Escherichia coli", "taxon_id": 562, "taxonomy_source": "NCBI"}])
        self._write("samples", ["sample_id", "organism_id", "biosample_accession", "sex", "country_iso", "collection_date"],
                    [{"sample_id": "SMP_000001", "organism_id": "ORG_000001", "biosample_accession": "SAMN1", "sex": "not collected", "country_iso": "US", "collection_date": "2026-01-02"}])
        self.assertEqual(main(["--project", str(self.root), "import-metadata", "--replace"]), 0)
        db = Database(self.root / "operon.sqlite")
        try:
            self.assertEqual(db.query("SELECT COUNT(*) AS n FROM organisms")[0]["n"], 1)
            self.assertEqual(db.query("SELECT COUNT(*) AS n FROM samples")[0]["n"], 1)
            self.assertEqual(db.query("SELECT sex FROM samples WHERE sample_id='SMP_000001'")[0]["sex"], "not collected")
        finally:
            db.close()
        self.assertEqual(main(["--project", str(self.root), "export-metadata"]), 0)
        exported = (self.root / "metadata" / "organisms.tsv").read_text(encoding="utf-8")
        self.assertIn("ORG_000001", exported)

    def test_schema_rejects_unknown_column(self):
        self._write("organisms", ["organism_id", "scientific_name", "made_up_column"],
                    [{"organism_id": "ORG_000001", "scientific_name": "X", "made_up_column": "bad"}])
        self.assertEqual(main(["--project", str(self.root), "import-metadata", "--replace"]), 2)

    def test_schema_rejects_bad_controlled_vocabulary(self):
        self._write("organisms", ["organism_id", "scientific_name", "taxonomy_source"],
                    [{"organism_id": "ORG_000001", "scientific_name": "X", "taxonomy_source": "wikipedia"}])
        self.assertEqual(main(["--project", str(self.root), "import-metadata", "--replace"]), 2)

    def test_schema_normalizes_case_of_controlled_values(self):
        self._write("organisms", ["organism_id", "scientific_name", "taxonomy_source"],
                    [{"organism_id": "ORG_000001", "scientific_name": "X", "taxonomy_source": "ncbi"}])
        self.assertEqual(main(["--project", str(self.root), "import-metadata", "--replace"]), 0)
        db = Database(self.root / "operon.sqlite")
        try:
            self.assertEqual(db.query("SELECT taxonomy_source FROM organisms")[0]["taxonomy_source"], "NCBI")
        finally:
            db.close()

    def test_add_entity_uses_next_stable_id(self):
        self._write("organisms", ["organism_id", "scientific_name", "taxonomy_source"],
                    [{"organism_id": "ORG_000007", "scientific_name": "X", "taxonomy_source": "NCBI"}])
        self.assertEqual(main(["--project", str(self.root), "import-metadata", "--replace"]), 0)
        self.assertEqual(main(["--project", str(self.root), "add", "organism",
                               "--field", "scientific_name=Yersinia pestis",
                               "--field", "taxonomy_source=NCBI"]), 0)
        db = Database(self.root / "operon.sqlite")
        try:
            rows = db.query("SELECT organism_id FROM organisms ORDER BY organism_id")
            self.assertEqual([r["organism_id"] for r in rows], ["ORG_000007", "ORG_000008"])
        finally:
            db.close()

