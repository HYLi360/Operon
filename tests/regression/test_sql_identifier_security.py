"""SQL identifiers must not turn metadata names into executable expressions."""

import pytest
import yaml

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ValidationError
from operon.import_wizard import _commit
from operon.schema import Schema
from operon.table_import import apply_table_import, preview_table_import


@pytest.fixture
def project_db(tmp_path):
    assert main(['init', str(tmp_path), '--project-id', 'PRJ_SQL']) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def test_wizard_rejects_schema_column_injection_atomically(project_db):
    project, db = project_db
    document = yaml.safe_load(project.schema_path.read_text())
    fields = document['tables']['organisms']['fields']
    spec = fields.pop('taxonomy_version')
    # Previously this evaluated upper(?) inside SQL instead of storing the value.
    malicious = 'taxonomy_version) VALUES (' + ', '.join(['?'] * len(fields) + ['upper(?)']) + ') --'
    fields[malicious] = spec
    project.schema_path.write_text(yaml.safe_dump(document, sort_keys=False))
    draft = {
        'source': {'source_type': 'insdc', 'database_name': 'NCBI', 'provider': 'NCBI'},
        'organism': {'action': 'create', 'id': 'ORG_000001', 'row': {
            'organism_id': 'ORG_000001', 'scientific_name': 'Probe', malicious: 'injected',
        }},
        'files': [],
    }
    with pytest.raises(ValidationError, match='unsafe SQL identifier'):
        _commit(db, project, draft)
    assert db.query('SELECT * FROM organisms') == []
    assert db.query('SELECT * FROM data_sources') == []
    assert db.query('SELECT * FROM changes') == []
    assert db.query('SELECT status FROM workflow_runs')[0][0] == 'failed'


@pytest.mark.parametrize('method', ['insert_row', 'upsert_rows'])
def test_column_cannot_replace_insert_values(project_db, method):
    _, db = project_db
    row = {'organism_id': 'ORG_000001',
           'scientific_name) VALUES (?, upper(?)) --': 'injected'}
    with pytest.raises(ValidationError, match='unsafe SQL identifier'):
        if method == 'insert_row':
            db.insert_row('organisms', row)
        else:
            db.upsert_rows('organisms', list(row), [row])
    assert db.query('SELECT * FROM organisms') == []


@pytest.mark.parametrize('method', ['table_columns', 'export_rows', 'export_active_rows', 'insert_row', 'upsert_rows'])
def test_table_argument_is_not_sql(project_db, method):
    _, db = project_db
    table = 'organisms (organism_id, scientific_name) VALUES (?, upper(?)) --'
    row = {'organism_id': 'ORG_000001', 'scientific_name': 'injected'}
    with pytest.raises(ValidationError, match='unsafe SQL identifier'):
        if method == 'insert_row':
            db.insert_row(table, row)
        elif method == 'upsert_rows':
            db.upsert_rows(table, list(row), [row])
        else:
            getattr(db, method)(table)
    assert db.query('SELECT * FROM organisms') == []


def test_table_import_rejects_tampered_update_column(project_db, tmp_path):
    project, db = project_db
    db.insert_row('organisms', {'organism_id': 'ORG_000001', 'scientific_name': 'Original'})
    source = tmp_path / 'import.csv'
    source.write_text('organism_id,scientific_name\nORG_000001,Changed\n')
    schema = Schema.from_file(project.schema_path)
    preview = preview_table_import(db, schema, 'organisms', source)
    malicious = 'scientific_name=upper(?) WHERE organism_id=? --'
    preview['items'][0]['differences'] = [malicious]
    preview['items'][0]['row'][malicious] = 'injected'
    with pytest.raises(ValidationError, match='unsafe SQL identifier'):
        apply_table_import(db, schema, preview, on_conflict='update')
    assert db.query('SELECT scientific_name FROM organisms')[0][0] == 'Original'
    assert db.query('SELECT * FROM changes') == []


def test_table_import_rechecks_allowed_table(project_db):
    project, db = project_db
    with pytest.raises(ValidationError, match='not importable'):
        apply_table_import(db, Schema.from_file(project.schema_path),
                           {'table': 'changes', 'update': 0}, on_conflict='update')


def test_custom_keyword_column_and_sql_like_values_round_trip(project_db, tmp_path):
    project, db = project_db
    schema = Schema.from_file(project.schema_path)
    schema.tables['organisms']['fields']['select'] = {'type': 'string'}
    db.ensure_metadata_columns(schema)
    value = "O'Brien'); DROP TABLE organisms; --"
    row = {'organism_id': 'ORG_000001', 'scientific_name': value, 'select': value}
    db.insert_row('organisms', row)
    row['scientific_name'] = 'Updated'
    db.upsert_rows('organisms', list(row), [row])
    for export in (db.export_rows, db.export_active_rows):
        assert export('organisms', list(row)) == [row]
    source = tmp_path / 'keyword.csv'
    source.write_text('organism_id,select\nORG_000001,keyword update\n')
    preview = preview_table_import(db, schema, 'organisms', source)
    apply_table_import(db, schema, preview, on_conflict='update')
    assert db.export_rows('organisms', ['select']) == [{'select': 'keyword update'}]
