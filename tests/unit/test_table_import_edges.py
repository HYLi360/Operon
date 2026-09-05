"""Boundary and failure-path tests for controlled metadata table imports."""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

import pytest

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.lifecycle import apply_lifecycle_event
from operon.schema import Schema
from operon.table_import import (
    _column_index,
    _excel_datetime,
    _xlsx_date_styles,
    apply_table_import,
    preview_table_import,
    read_table_file,
    write_table_template,
)


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db, Schema.from_file(project.schema_path)
    finally:
        db.close()


def _csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _xlsx(path: Path, workbook: str, sheet: str | None = None, **members: str) -> Path:
    rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
    <Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        if sheet is not None:
            archive.writestr("xl/worksheets/sheet1.xml", sheet)
        for name, content in members.items():
            archive.writestr(name.replace("__", "/").replace("_xml", ".xml"), content)
    return path


def test_template_and_input_validation(project_db, tmp_path):
    _project, _db, schema = project_db
    assert write_table_template(schema, "organisms", tmp_path / "template.csv").exists()
    with pytest.raises(ValidationError, match="not importable"):
        write_table_template(schema, "bad", tmp_path / "bad.csv")
    with pytest.raises(ValidationError, match="must end"):
        write_table_template(schema, "organisms", tmp_path / "bad.txt")
    with pytest.raises(ValidationError, match="does not exist"):
        read_table_file(tmp_path / "missing.csv")
    wrong = tmp_path / "input.txt"
    wrong.write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match="must end"):
        read_table_file(wrong)
    with pytest.raises(ValidationError, match="cell reference"):
        _column_index("12")


def test_xlsx_empty_invalid_shared_boolean_and_1904_dates(tmp_path):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    empty_book = f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets/></workbook>'
    assert read_table_file(_xlsx(tmp_path / "empty.xlsx", empty_book)) == []

    book = f'''<workbook xmlns="{ns}" xmlns:r="{rel}"><workbookPr date1904="true"/>
    <sheets><sheet name="data" r:id="rId1"/></sheets></workbook>'''
    sheet = f'''<worksheet xmlns="{ns}"><sheetData>
    <row><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>flag</t></is></c><c r="C1" t="inlineStr"><is><t>when</t></is></c></row>
    <row><c r="A2" t="s"><v>1</v></c><c r="B2" t="b"><v>1</v></c><c r="C2" s="1"><v>0.5</v></c></row>
    <row><c r="A3" t="inlineStr"><is><t>second</t></is></c><c r="B3" t="b"><v>0</v></c><c r="C3"/></row>
    <row><c r="A4" t="inlineStr"><is><t> </t></is></c></row>
    </sheetData></worksheet>'''
    shared = f'''<sst xmlns="{ns}"><si><t>name</t></si><si><r><t>A</t></r><r><t>B</t></r></si></sst>'''
    styles = f'''<styleSheet xmlns="{ns}"><numFmts><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/></numFmts>
    <cellXfs><xf/><xf numFmtId="164"/></cellXfs></styleSheet>'''
    path = _xlsx(tmp_path / "values.xlsx", book, sheet,
                 xl__sharedStrings_xml=shared, xl__styles_xml=styles)
    rows = read_table_file(path)
    assert rows == [
        {"name": "AB", "flag": "true", "when": "1904-01-01T12:00:00"},
        {"name": "second", "flag": "false", "when": ""},
    ]
    invalid = tmp_path / "invalid.xlsx"
    invalid.write_bytes(b"not a zip")
    with pytest.raises(ValidationError, match="cannot read XLSX"):
        read_table_file(invalid)


def test_xlsx_style_and_date_edge_cases(tmp_path):
    no_styles = tmp_path / "none.zip"
    with zipfile.ZipFile(no_styles, "w") as archive:
        assert _xlsx_date_styles(archive) == {}
    no_cell_xfs = tmp_path / "no-xfs.zip"
    with zipfile.ZipFile(no_cell_xfs, "w") as archive:
        archive.writestr("xl/styles.xml", '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')
    with zipfile.ZipFile(no_cell_xfs) as archive:
        assert _xlsx_date_styles(archive) == {}
    assert _excel_datetime("not-a-number", False, False) == "not-a-number"
    assert _excel_datetime("1", False, False) == "1899-12-31"


def test_preview_rejects_invalid_tables_keys_rows_and_references(project_db, tmp_path, monkeypatch):
    _project, db, schema = project_db
    base = _csv(tmp_path / "rows.csv", ["organism_id", "scientific_name"],
                [{"organism_id": "ORG_000001", "scientific_name": "X"}])
    with pytest.raises(ValidationError, match="not importable"):
        preview_table_import(db, schema, "bad", base)
    monkeypatch.setattr(db, "_primary_keys", lambda _table: [])
    with pytest.raises(ValidationError, match="no import key"):
        preview_table_import(db, schema, "organisms", base)
    monkeypatch.undo()

    unknown = _csv(tmp_path / "unknown.csv", ["organism_id", "unknown"],
                   [{"organism_id": "ORG_000001", "unknown": "x"}])
    with pytest.raises(ValidationError, match="unknown field"):
        preview_table_import(db, schema, "organisms", unknown)
    duplicate = _csv(tmp_path / "duplicate.csv", ["organism_id", "scientific_name"], [
        {"organism_id": "ORG_000001", "scientific_name": "X"},
        {"organism_id": "ORG_000001", "scientific_name": "Y"},
    ])
    with pytest.raises(ValidationError, match="duplicate import key"):
        preview_table_import(db, schema, "organisms", duplicate)
    invalid_key = _csv(tmp_path / "invalid-key.csv", ["organism_id", "scientific_name"],
                       [{"organism_id": "wrong", "scientific_name": "X"}])
    with pytest.raises(ValidationError, match="field organism_id"):
        preview_table_import(db, schema, "organisms", invalid_key)

    sample = _csv(tmp_path / "sample.csv", ["sample_id", "organism_id"],
                  [{"sample_id": "SMP_000001", "organism_id": "ORG_999999"}])
    with pytest.raises(ValidationError, match="does not exist"):
        preview_table_import(db, schema, "samples", sample)
    accession = _csv(tmp_path / "accession.csv", ["accession", "namespace", "internal_type", "internal_id"],
                     [{"accession": "X1", "namespace": "NCBI", "internal_type": "sample", "internal_id": "SMP_999999"}])
    with pytest.raises(ValidationError, match="does not exist"):
        preview_table_import(db, schema, "accessions", accession)


def test_apply_conflict_policies_and_state_updates(project_db, tmp_path):
    _project, db, schema = project_db
    source = _csv(tmp_path / "organisms.csv", ["organism_id", "scientific_name"],
                  [{"organism_id": "ORG_000001", "scientific_name": "Before"}])
    preview = preview_table_import(db, schema, "organisms", source)
    assert apply_table_import(db, schema, preview, on_conflict="update")["inserted"] == 1
    assert db.get_entity_state("organism", "ORG_000001") == "METADATA_VALIDATED"
    with pytest.raises(ValidationError, match="on_conflict"):
        apply_table_import(db, schema, preview, on_conflict="bad")

    update = _csv(tmp_path / "update.csv", ["organism_id", "scientific_name"],
                  [{"organism_id": "ORG_000001", "scientific_name": "After"}])
    changed = preview_table_import(db, schema, "organisms", update)
    with pytest.raises(ConflictError, match="would be changed"):
        apply_table_import(db, schema, changed, on_conflict="error")
    skipped = apply_table_import(db, schema, changed, on_conflict="skip")
    assert skipped["skipped"] == 1
    updated = apply_table_import(db, schema, changed, on_conflict="update", actor="tester")
    assert updated["updated"] == 1
    audit = db.query(
        "SELECT field, old_value, new_value, reason, actor FROM changes "
        "WHERE object_type='organisms' AND object_id='ORG_000001' "
        "AND reason='table import update'"
    )
    assert [(row["field"], row["old_value"], row["new_value"], row["actor"]) for row in audit] == [
        ("scientific_name", "Before", "After", "tester")
    ]


def test_preview_rejects_updates_to_retired_entities(project_db, tmp_path):
    _project, db, schema = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Before"})
    apply_lifecycle_event(
        db, "organism", "ORG_000001", action="RETIRE",
        reason_code="accidental_import", reason="mistake", actor="tester",
    )
    source = _csv(tmp_path / "update.csv", ["organism_id", "scientific_name"],
                  [{"organism_id": "ORG_000001", "scientific_name": "After"}])
    with pytest.raises(ValidationError, match="is retired; restore it before table import"):
        preview_table_import(db, schema, "organisms", source)


def test_apply_skips_unchanged_rows_without_new_audit_rows(project_db, tmp_path):
    _project, db, schema = project_db
    source = _csv(tmp_path / "organisms.csv", ["organism_id", "scientific_name"],
                  [{"organism_id": "ORG_000001", "scientific_name": "Before"}])
    preview = preview_table_import(db, schema, "organisms", source)
    apply_table_import(db, schema, preview, on_conflict="update")

    again = preview_table_import(db, schema, "organisms", source)
    assert [item["action"] for item in again["items"]] == ["unchanged"]
    audited = db.query("SELECT COUNT(*) AS n FROM changes")[0]["n"]
    result = apply_table_import(db, schema, again, on_conflict="update")
    assert result == {"inserted": 0, "updated": 0, "unchanged": 1, "skipped": 0}
    assert db.query("SELECT COUNT(*) AS n FROM changes")[0]["n"] == audited


def test_metadata_update_preserves_advanced_entity_state(project_db, tmp_path):
    project, db, schema = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Before"})
    db.set_entity_state("organism", "ORG_000001", "QC_COMPLETE", "qc complete")
    source = _csv(tmp_path / "update-advanced.csv", ["organism_id", "scientific_name"], [
        {"organism_id": "ORG_000001", "scientific_name": "After"},
    ])
    preview = preview_table_import(db, schema, "organisms", source)
    result = apply_table_import(db, schema, preview, on_conflict="update", actor="tester")
    assert result["updated"] == 1
    assert db.get_entity_state("organism", "ORG_000001") == "QC_COMPLETE"
