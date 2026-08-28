"""Metadata schema validation and controlled CSV/XLSX table-import tests."""

from __future__ import annotations

import csv
import tempfile
import zipfile
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.database import Database
from operon.table_import import read_table_file


class TestSchemaAndMetadata(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_TEST_001"]), 0)

    def _write(self, table: str, columns: list[str], rows: list[dict]) -> Path:
        path = self.root / f"{table}.csv"
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _import(self, table: str, path: Path, *extra: str) -> int:
        return main([
            "--project", str(self.root), "import", "table", "--table", table,
            "--file", str(path), "--yes", *extra,
        ])

    def test_valid_import_and_export(self):
        organisms = self._write("organisms", ["organism_id", "scientific_name", "taxon_id", "taxonomy_source"],
                                [{"organism_id": "ORG_000001", "scientific_name": "Escherichia coli", "taxon_id": 562, "taxonomy_source": "NCBI"}])
        samples = self._write("samples", ["sample_id", "organism_id", "biosample_accession", "sex", "country_iso", "collection_date"],
                              [{"sample_id": "SMP_000001", "organism_id": "ORG_000001", "biosample_accession": "SAMN1", "sex": "not collected", "country_iso": "US", "collection_date": "2026-01-02"}])
        self.assertEqual(self._import("organisms", organisms), 0)
        self.assertEqual(self._import("samples", samples), 0)
        db = Database(self.root / "operon.sqlite")
        try:
            self.assertEqual(db.query("SELECT COUNT(*) AS n FROM organisms")[0]["n"], 1)
            self.assertEqual(db.query("SELECT COUNT(*) AS n FROM samples")[0]["n"], 1)
            self.assertEqual(db.query("SELECT sex FROM samples WHERE sample_id='SMP_000001'")[0]["sex"], "not collected")
        finally:
            db.close()
        self.assertEqual(main(["--project", str(self.root), "report", "metadata"]), 0)
        exported = (self.root / "reports" / "metadata" / "organisms.tsv").read_text(encoding="utf-8")
        self.assertIn("ORG_000001", exported)

    def test_schema_rejects_unknown_column(self):
        path = self._write("organisms", ["organism_id", "scientific_name", "made_up_column"],
                           [{"organism_id": "ORG_000001", "scientific_name": "X", "made_up_column": "bad"}])
        self.assertEqual(self._import("organisms", path), 2)

    def test_schema_rejects_bad_controlled_vocabulary(self):
        path = self._write("organisms", ["organism_id", "scientific_name", "taxonomy_source"],
                           [{"organism_id": "ORG_000001", "scientific_name": "X", "taxonomy_source": "wikipedia"}])
        self.assertEqual(self._import("organisms", path), 2)

    def test_schema_normalizes_case_of_controlled_values(self):
        path = self._write("organisms", ["organism_id", "scientific_name", "taxonomy_source"],
                           [{"organism_id": "ORG_000001", "scientific_name": "X", "taxonomy_source": "ncbi"}])
        self.assertEqual(self._import("organisms", path), 0)
        db = Database(self.root / "operon.sqlite")
        try:
            self.assertEqual(db.query("SELECT taxonomy_source FROM organisms")[0]["taxonomy_source"], "NCBI")
        finally:
            db.close()

    def test_add_entity_uses_next_stable_id(self):
        path = self._write("organisms", ["organism_id", "scientific_name", "taxonomy_source"],
                           [{"organism_id": "ORG_000007", "scientific_name": "X", "taxonomy_source": "NCBI"}])
        self.assertEqual(self._import("organisms", path), 0)
        self.assertEqual(main(["--project", str(self.root), "add", "organism",
                               "--field", "scientific_name=Yersinia pestis",
                               "--field", "taxonomy_source=NCBI"]), 0)
        db = Database(self.root / "operon.sqlite")
        try:
            rows = db.query("SELECT organism_id FROM organisms ORDER BY organism_id")
            self.assertEqual([r["organism_id"] for r in rows], ["ORG_000007", "ORG_000008"])
        finally:
            db.close()

    def test_xlsx_template_contains_data_and_schema_sheets(self):
        path = self.root / "organisms.xlsx"
        self.assertEqual(main([
            "--project", str(self.root), "import", "table", "--table", "organisms",
            "--template", str(path),
        ]), 0)
        self.assertTrue(path.exists())
        self.assertEqual(read_table_file(path), [])

    def test_existing_xlsx_reads_inline_text_and_excel_dates(self):
        path = self.root / "samples.xlsx"
        self.assertEqual(main([
            "--project", str(self.root), "import", "table", "--table", "samples",
            "--template", str(path),
        ]), 0)
        with zipfile.ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members["xl/worksheets/sheet1.xml"] = b"""<?xml version='1.0' encoding='utf-8'?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>sample_id</t></is></c><c r="B1" t="inlineStr"><is><t>organism_id</t></is></c><c r="C1" t="inlineStr"><is><t>collection_date</t></is></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><t>SMP_000001</t></is></c><c r="B2" t="inlineStr"><is><t>ORG_000001</t></is></c><c r="C2" s="1"><v>46023</v></c></row>
</sheetData></worksheet>"""
        members["xl/styles.xml"] = b"""<?xml version='1.0' encoding='utf-8'?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>"""
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        rows = read_table_file(path)
        self.assertEqual(rows[0]["sample_id"], "SMP_000001")
        self.assertEqual(rows[0]["collection_date"], "2026-01-01")

    def test_existing_row_requires_explicit_conflict_policy(self):
        first = self._write("organisms", ["organism_id", "scientific_name", "taxonomy_source"],
                            [{"organism_id": "ORG_000001", "scientific_name": "Before", "taxonomy_source": "NCBI"}])
        self.assertEqual(self._import("organisms", first), 0)
        second = self._write("organisms-update", ["organism_id", "scientific_name"],
                             [{"organism_id": "ORG_000001", "scientific_name": "After"}])
        self.assertEqual(self._import("organisms", second), 2)
        self.assertEqual(self._import("organisms", second, "--on-conflict", "update"), 0)
        db = Database(self.root / "operon.sqlite")
        try:
            row = db.query("SELECT scientific_name, taxonomy_source FROM organisms")[0]
            self.assertEqual(row["scientific_name"], "After")
            self.assertEqual(row["taxonomy_source"], "NCBI")
            self.assertEqual(db.query("SELECT COUNT(*) AS n FROM changes WHERE field='scientific_name'")[0]["n"], 1)
        finally:
            db.close()
