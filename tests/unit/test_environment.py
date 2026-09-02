"""Execution-environment capture, fingerprinting, and schema 2.8 migration tests."""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import tempfile
from pathlib import Path

from tests.helpers import PytestAssertions

from operon import __version__
from operon.database import Database
from operon.environment import (
    PROBE_SHELL_LINES,
    environment_fingerprint,
    local_environment,
    parse_probe_output,
)


class TestEnvironmentDocuments(PytestAssertions):
    def test_local_environment_fields(self):
        env = local_environment()
        self.assertEqual(env["hostname"], socket.gethostname())
        self.assertEqual(env["os"], platform.system())
        self.assertEqual(env["os_release"], platform.release())
        self.assertEqual(env["machine"], platform.machine())
        self.assertEqual(env["python_version"], platform.python_version())
        self.assertEqual(env["operon_version"], __version__)
        self.assertTrue(isinstance(env["dockerenv"], bool))

    def test_local_environment_omits_unset_variables(self):
        env = local_environment()
        for name in ("CONDA_PREFIX", "CONDA_DEFAULT_ENV", "SINGULARITY_NAME", "APPTAINER_NAME"):
            if not os.environ.get(name):
                self.assertFalse(name.lower() in env)
        # No variable is ever stored as an empty string.
        self.assertTrue(all(value != "" for value in env.values()))

    def test_fingerprint_is_deterministic_and_order_independent(self):
        first = {"hostname": "h", "os": "Linux", "path": "/bin", "dockerenv": False}
        second = {"dockerenv": False, "path": "/bin", "os": "Linux", "hostname": "h"}
        self.assertEqual(environment_fingerprint(first), environment_fingerprint(second))
        self.assertEqual(
            environment_fingerprint(first),
            environment_fingerprint(json.loads(json.dumps(first))),
        )
        self.assertNotEqual(environment_fingerprint(first), environment_fingerprint({**first, "os": "Darwin"}))

    def test_probe_round_trip_matches_local_document(self):
        proc = subprocess.run(
            ["bash", "-c", " ; ".join(PROBE_SHELL_LINES)], capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        probed = parse_probe_output(proc.stdout)
        local = local_environment()
        for key in ("hostname", "os", "os_release", "machine", "path"):
            self.assertEqual(probed.get(key), local.get(key), key)
        # The remote probe never reports the controller's Python/operon versions.
        self.assertFalse("python_version" in probed)
        self.assertFalse("operon_version" in probed)

    def test_parse_probe_output_treats_empty_values_as_missing(self):
        parsed = parse_probe_output("hostname=node1\nconda_prefix=\npath=\n=dropped\n\n")
        self.assertEqual(parsed, {"hostname": "node1"})

    def test_parse_probe_output_dockerenv_flag(self):
        self.assertEqual(parse_probe_output("dockerenv=1\n"), {"dockerenv": True})
        self.assertEqual(parse_probe_output("dockerenv=\n"), {})


class TestEnvironmentSchema(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "meta.sqlite")
        self.addCleanup(self.db.close)

    def test_schema_2_8_columns_and_table_exist(self):
        self.assertIn("environment_id", self.db.table_columns("workflow_runs"))
        self.assertIn("environment_id", self.db.table_columns("analysis_jobs"))
        self.assertEqual(
            self.db.table_columns("execution_environments"),
            ["environment_id", "document", "created_at"],
        )
        row = self.db.conn.execute(
            "SELECT migration_id FROM schema_migrations WHERE migration_id='2.8-execution-environments'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_record_environment_is_idempotent(self):
        document = local_environment()
        first = self.db.record_environment(document)
        second = self.db.record_environment(dict(reversed(list(document.items()))))
        self.assertEqual(first, second)
        rows = self.db.conn.execute("SELECT document, created_at FROM execution_environments").fetchall()
        self.assertEqual(len(rows), 1)
        stored = json.loads(rows[0]["document"])
        self.assertEqual(stored, {k: document[k] for k in sorted(document)})
        self.assertTrue(rows[0]["created_at"])

    def test_migration_adds_columns_to_pre_2_8_database(self):
        self.db.close()
        path = Path(self.tmp.name) / "meta.sqlite"
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.execute("ALTER TABLE workflow_runs DROP COLUMN environment_id")
        conn.execute("ALTER TABLE analysis_jobs DROP COLUMN environment_id")
        conn.execute("DROP TABLE execution_environments")
        conn.commit()
        conn.close()
        migrated = Database(path)
        self.addCleanup(migrated.close)
        self.assertIn("environment_id", migrated.table_columns("workflow_runs"))
        self.assertIn("environment_id", migrated.table_columns("analysis_jobs"))
        self.assertIn("environment_id", migrated.table_columns("execution_environments"))
