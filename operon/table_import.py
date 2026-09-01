"""Controlled CSV/XLSX metadata-table templates, previews and imports."""

from __future__ import annotations

import csv
import json
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET

from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.schema import ENTITY_ID_COLUMNS, ENTITY_TABLES, Schema
from operon.utils import now_iso


IMPORTABLE_TABLES = ["organisms", "samples", "runs", "assemblies", "annotations", "accessions"]
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
BUILTIN_DATE_FORMATS = set(range(14, 23)) | set(range(27, 37)) | set(range(45, 48)) | set(range(50, 59))


def _xml(tag: str, **attrs: str) -> ET.Element:
    return ET.Element(f"{{{NS_MAIN}}}{tag}", attrs)


def _column_name(index: int) -> str:
    value = index + 1
    chars: list[str] = []
    while value:
        value, remainder = divmod(value - 1, 26)
        chars.append(chr(65 + remainder))
    return "".join(reversed(chars))


def _column_index(cell_ref: str) -> int:
    match = re.match(r"([A-Z]+)", cell_ref.upper())
    if not match:
        raise ValidationError(f"invalid XLSX cell reference {cell_ref!r}")
    value = 0
    for char in match.group(1):
        value = value * 26 + ord(char) - 64
    return value - 1


def write_table_template(schema: Schema, table: str, output: str | Path) -> Path:
    if table not in IMPORTABLE_TABLES:
        raise ValidationError(f"table {table!r} is not importable; choose from {IMPORTABLE_TABLES}")
    output = Path(output)
    suffix = output.suffix.lower()
    if suffix == ".csv":
        output.parent.mkdir(parents=True, exist_ok=True)
        with open(output, "w", encoding="utf-8-sig", newline="") as handle:
            csv.writer(handle).writerow(schema.columns(table))
        return output
    if suffix != ".xlsx":
        raise ValidationError("template output must end in .csv or .xlsx")
    _write_xlsx_template(schema, table, output)
    return output


def _write_xlsx_template(schema: Schema, table: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = schema.columns(table)
    fields = schema.tables[table]["fields"]

    worksheet = _xml("worksheet")
    sheet_data = ET.SubElement(worksheet, f"{{{NS_MAIN}}}sheetData")
    row = ET.SubElement(sheet_data, f"{{{NS_MAIN}}}row", {"r": "1"})
    for index, column in enumerate(columns):
        cell = ET.SubElement(row, f"{{{NS_MAIN}}}c", {"r": f"{_column_name(index)}1", "t": "inlineStr"})
        inline = ET.SubElement(cell, f"{{{NS_MAIN}}}is")
        ET.SubElement(inline, f"{{{NS_MAIN}}}t").text = column

    guide = _xml("worksheet")
    guide_data = ET.SubElement(guide, f"{{{NS_MAIN}}}sheetData")
    guide_headers = ["field", "type", "required", "allowed", "description"]
    for row_no, values in enumerate(
        [guide_headers] + [
            [
                name,
                str(spec.get("type", "string")),
                "yes" if spec.get("required") else "no",
                ", ".join(str(value) for value in spec.get("allowed", [])),
                str(spec.get("description", "")),
            ]
            for name, spec in fields.items()
        ],
        start=1,
    ):
        xml_row = ET.SubElement(guide_data, f"{{{NS_MAIN}}}row", {"r": str(row_no)})
        for index, value in enumerate(values):
            cell = ET.SubElement(xml_row, f"{{{NS_MAIN}}}c", {"r": f"{_column_name(index)}{row_no}", "t": "inlineStr"})
            inline = ET.SubElement(cell, f"{{{NS_MAIN}}}is")
            ET.SubElement(inline, f"{{{NS_MAIN}}}t").text = value

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="data" sheetId="1" r:id="rId1"/><sheet name="schema" sheetId="2" r:id="rId2"/></sheets>
</workbook>"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
</Relationships>"""
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", ET.tostring(worksheet, encoding="utf-8", xml_declaration=True))
        archive.writestr("xl/worksheets/sheet2.xml", ET.tostring(guide, encoding="utf-8", xml_declaration=True))


def read_table_file(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        raise ValidationError(f"table input does not exist: {path}")
    if path.suffix.lower() == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if path.suffix.lower() == ".xlsx":
        return _read_xlsx(path)
    raise ValidationError("table input must end in .csv or .xlsx")


def _read_xlsx(path: Path) -> list[dict[str, Any]]:
    try:
        with zipfile.ZipFile(path) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            workbook_properties = workbook.find(f"{{{NS_MAIN}}}workbookPr")
            uses_1904_dates = bool(
                workbook_properties is not None
                and workbook_properties.attrib.get("date1904", "").lower() in {"1", "true"}
            )
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            rel_targets = {
                rel.attrib["Id"]: rel.attrib["Target"]
                for rel in relationships.findall(f"{{{NS_PKG_REL}}}Relationship")
            }
            first_sheet = workbook.find(f"{{{NS_MAIN}}}sheets/{{{NS_MAIN}}}sheet")
            if first_sheet is None:
                return []
            rel_id = first_sheet.attrib[f"{{{NS_REL}}}id"]
            target = rel_targets[rel_id].lstrip("/")
            sheet_path = str(PurePosixPath("xl") / target) if not target.startswith("xl/") else target
            sheet = ET.fromstring(archive.read(sheet_path))
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in root.findall(f"{{{NS_MAIN}}}si"):
                    shared.append("".join(node.text or "" for node in item.iter(f"{{{NS_MAIN}}}t")))
            date_styles = _xlsx_date_styles(archive)
            matrix: list[list[Any]] = []
            for row in sheet.findall(f".//{{{NS_MAIN}}}sheetData/{{{NS_MAIN}}}row"):
                values: dict[int, Any] = {}
                for cell in row.findall(f"{{{NS_MAIN}}}c"):
                    index = _column_index(cell.attrib.get("r", "A1"))
                    cell_type = cell.attrib.get("t")
                    if cell_type == "inlineStr":
                        value: Any = "".join(node.text or "" for node in cell.iter(f"{{{NS_MAIN}}}t"))
                    else:
                        value_node = cell.find(f"{{{NS_MAIN}}}v")
                        raw = value_node.text if value_node is not None else ""
                        if cell_type == "s" and raw != "":
                            value = shared[int(raw)]
                        elif cell_type == "b":
                            value = "true" if raw == "1" else "false"
                        elif raw != "" and int(cell.attrib.get("s", "0")) in date_styles:
                            value = _excel_datetime(raw, uses_1904_dates, date_styles[int(cell.attrib.get("s", "0"))])
                        else:
                            value = raw
                    values[index] = value
                width = max(values, default=-1) + 1
                matrix.append([values.get(index, "") for index in range(width)])
    except (IndexError, KeyError, OSError, zipfile.BadZipFile, ET.ParseError, ValueError) as exc:
        raise ValidationError(f"cannot read XLSX {path}: {exc}") from exc
    if not matrix:
        return []
    headers = [str(value).strip() for value in matrix[0]]
    rows: list[dict[str, Any]] = []
    for values in matrix[1:]:
        padded = values + [""] * (len(headers) - len(values))
        if not any(str(value).strip() for value in padded):
            continue
        rows.append({header: padded[index] for index, header in enumerate(headers) if header})
    return rows


def _xlsx_date_styles(archive: zipfile.ZipFile) -> dict[int, bool]:
    """Map cell style index to whether the date format includes a time."""
    if "xl/styles.xml" not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read("xl/styles.xml"))
    custom_formats: dict[int, str] = {}
    for item in root.findall(f"{{{NS_MAIN}}}numFmts/{{{NS_MAIN}}}numFmt"):
        custom_formats[int(item.attrib["numFmtId"])] = item.attrib.get("formatCode", "")
    styles: dict[int, bool] = {}
    cell_formats = root.find(f"{{{NS_MAIN}}}cellXfs")
    if cell_formats is None:
        return styles
    for index, cell_format in enumerate(cell_formats.findall(f"{{{NS_MAIN}}}xf")):
        num_fmt = int(cell_format.attrib.get("numFmtId", "0"))
        code = custom_formats.get(num_fmt, "")
        normalized = re.sub(r'"[^"]*"|\[[^]]*\]|\\.', "", code).lower()
        is_date = num_fmt in BUILTIN_DATE_FORMATS or bool(re.search(r"[dmy]", normalized))
        if is_date:
            styles[index] = bool(re.search(r"[hs]", normalized)) or num_fmt in {18, 19, 20, 21, 22, 45, 46, 47}
    return styles


def _excel_datetime(raw: str, uses_1904_dates: bool, includes_time: bool) -> str:
    try:
        serial = float(raw)
    except ValueError:
        return raw
    base = datetime(1904, 1, 1) if uses_1904_dates else datetime(1899, 12, 30)
    value = base + timedelta(days=serial)
    if includes_time or serial % 1:
        return value.isoformat(timespec="seconds")
    return value.date().isoformat()


def _validate_references(db: Database, table: str, rows: list[dict[str, Any]]) -> None:
    checks = {
        "samples": ("organism", "organism_id"),
        "runs": ("sample", "sample_id"),
        "assemblies": ("sample", "sample_id"),
        "annotations": ("assembly", "assembly_id"),
    }
    if table in checks:
        entity_type, field = checks[table]
        incoming_ids = {
            row[ENTITY_ID_COLUMNS[entity_type]]
            for row in rows
            if ENTITY_ID_COLUMNS[entity_type] in row
        } if ENTITY_TABLES.get(entity_type) == table else set()
        for row in rows:
            value = row.get(field)
            if value and value not in incoming_ids:
                if not db.entity_exists(entity_type, value):
                    raise ValidationError(f"{table}: {field} {value} does not exist")
                db.require_active_entity(entity_type, value)
    if table == "accessions":
        for row in rows:
            if not db.entity_exists(row["internal_type"], row["internal_id"]):
                raise ValidationError(
                    f"accessions: {row['internal_type']} {row['internal_id']} does not exist"
                )
            db.require_active_entity(row["internal_type"], row["internal_id"])


def preview_table_import(db: Database, schema: Schema, table: str, path: str | Path) -> dict[str, Any]:
    if table not in IMPORTABLE_TABLES:
        raise ValidationError(f"table {table!r} is not importable; choose from {IMPORTABLE_TABLES}")
    raw_rows = read_table_file(path)
    keys = db._primary_keys(table)
    if not keys:
        raise ValidationError(f"table {table!r} has no import key")
    existing_rows = db.export_rows(table, schema.columns(table))
    existing = {tuple(row.get(key) for key in keys): row for row in existing_rows}
    normalized: list[dict[str, Any]] = []
    supplied_columns: list[set[str]] = []
    seen_keys: set[tuple[Any, ...]] = set()
    field_specs = schema.tables[table]["fields"]
    for row_no, raw in enumerate(raw_rows, start=1):
        unknown = set(raw) - set(schema.columns(table))
        if unknown:
            raise ValidationError(
                f"{table}: row {row_no}: unknown field(s) {sorted(unknown)}; update the schema first"
            )
        normalized_key: list[Any] = []
        for key in keys:
            value, error = schema._normalize_field(key, field_specs[key], raw.get(key, ""))
            if error:
                raise ValidationError(f"{table}: row {row_no}, field {key}: {error}")
            normalized_key.append(value)
        key_tuple = tuple(normalized_key)
        if key_tuple in seen_keys:
            raise ValidationError(f"{table}: row {row_no}: duplicate import key {key_tuple}")
        seen_keys.add(key_tuple)
        current = existing.get(key_tuple)
        # Existing rows may be patched with a subset of columns. Omitted
        # columns retain their current values; an explicitly blank cell still
        # means NULL and is shown in the preview diff.
        candidate = dict(current or {})
        candidate.update(raw)
        row, _ = schema.validate_and_normalize(table, [candidate])
        normalized.append(row[0])
        supplied_columns.append(set(raw))
    _validate_references(db, table, normalized)
    items: list[dict[str, Any]] = []
    counts = {"insert": 0, "update": 0, "unchanged": 0}
    for row, supplied in zip(normalized, supplied_columns):
        key = tuple(row.get(column) for column in keys)
        current = existing.get(key)
        if current is not None:
            for entity_type, entity_table in ENTITY_TABLES.items():
                if entity_table == table:
                    entity_id = str(current[ENTITY_ID_COLUMNS[entity_type]])
                    if db.is_entity_retired(entity_type, entity_id):
                        raise ValidationError(
                            f"{entity_type} {entity_id} is retired; restore it before table import"
                        )
                    break
        if current is None:
            action = "insert"
            differences: list[str] = []
        else:
            differences = [
                column for column in schema.columns(table)
                if column not in keys and column in supplied and current.get(column) != row.get(column)
            ]
            action = "update" if differences else "unchanged"
        counts[action] += 1
        items.append({
            "key": key, "action": action, "differences": differences, "row": row,
            "current": current, "supplied_columns": sorted(supplied),
        })
    return {"table": table, "source": str(Path(path)), "columns": schema.columns(table), "items": items, **counts}


def apply_table_import(
    db: Database,
    schema: Schema,
    preview: dict[str, Any],
    *,
    on_conflict: str,
    actor: str | None = None,
) -> dict[str, int]:
    if on_conflict not in {"error", "skip", "update"}:
        raise ValidationError("on_conflict must be error, skip or update")
    if preview["update"] and on_conflict == "error":
        raise ConflictError(f"{preview['update']} existing row(s) would be changed")
    table = preview["table"]
    columns = preview["columns"]
    keys = db._primary_keys(table)
    result = {"inserted": 0, "updated": 0, "unchanged": preview["unchanged"], "skipped": 0}
    with db.transaction() as conn:
        db.ensure_metadata_columns(schema)
        for item in preview["items"]:
            action = item["action"]
            row = item["row"]
            object_id = ":".join(str(row[key]) for key in keys)
            if action == "unchanged":
                continue
            if action == "update" and on_conflict == "skip":
                result["skipped"] += 1
                continue
            if action == "insert":
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    [row.get(column) for column in columns],
                )
                db.record_change(table, object_id, None, None, json.dumps(row, ensure_ascii=False, sort_keys=True),
                                 "table import insert", evidence=preview["source"], actor=actor)
                result["inserted"] += 1
            else:
                update_columns = [column for column in item["differences"] if column not in keys]
                assignments = ", ".join(f"{column}=?" for column in update_columns)
                conn.execute(
                    f"UPDATE {table} SET {assignments} WHERE " + " AND ".join(f"{key}=?" for key in keys),
                    [row.get(column) for column in update_columns] + [row[key] for key in keys],
                )
                for column in item["differences"]:
                    db.record_change(table, object_id, column, item["current"].get(column), row.get(column),
                                     "table import update", evidence=preview["source"], actor=actor)
                result["updated"] += 1
            for entity_type, entity_table in ENTITY_TABLES.items():
                if entity_table == table:
                    entity_id = row[ENTITY_ID_COLUMNS[entity_type]]
                    conn.execute(
                        "INSERT INTO entity_state(entity_type, entity_id, state, message, updated_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(entity_type, entity_id) DO UPDATE SET state=excluded.state, message=excluded.message, updated_at=excluded.updated_at",
                        (entity_type, entity_id, "METADATA_VALIDATED", "metadata imported from table", now_iso()),
                    )
                    break
    return result
