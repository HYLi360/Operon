"""SQL identifiers must not turn metadata names into executable expressions."""

import pytest
import yaml

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ValidationError
from operon.import_wizard import _commit


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
