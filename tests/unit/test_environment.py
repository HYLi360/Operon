"""Execution-environment capture, fingerprinting, and schema 2.8 migration tests."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from tests.helpers import PytestAssertions

from operon import __version__
from operon.database import Database
from operon.environment import (
    PROBE_SHELL_LINES,
    environment_fingerprint,
    environment_summary,
    local_environment,
    parse_probe_output,
)


def _hashed_hostname(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class TestEnvironmentDocuments(PytestAssertions):
    def test_local_environment_fields(self):
        env = local_environment()
        self.assertEqual(env["hostname"], _hashed_hostname(socket.gethostname()))
        self.assertEqual(env["os"], platform.system())
        self.assertEqual(env["os_release"], platform.release())
        self.assertEqual(env["machine"], platform.machine())
        self.assertEqual(env["python_version"], platform.python_version())
        self.assertEqual(env["operon_version"], __version__)
        self.assertTrue(isinstance(env["dockerenv"], bool))

    def test_hostname_hash_is_stable_and_truncated(self):
        hostname = local_environment()["hostname"]
        self.assertTrue(hostname.startswith("sha256:"))
        self.assertEqual(len(hostname), len("sha256:") + 16)
        self.assertEqual(local_environment()["hostname"], hostname)

    def test_home_prefix_is_redacted(self):
        home = str(Path.home())
        if home == "/":
            self.skipTest("no meaningful home directory")
        prefix = str(Path(home) / "envs" / "demo")
        with mock.patch.dict(os.environ, {
            "PATH": prefix + "/bin:/usr/bin:" + home,
            "CONDA_PREFIX": prefix,
            "VIRTUAL_ENV": prefix,
            "CONDA_DEFAULT_ENV": "demo",
        }):
            env = local_environment()
        self.assertEqual(env["path"], "~/envs/demo/bin:/usr/bin:~")
        self.assertEqual(env["conda_prefix"], "~/envs/demo")
        self.assertEqual(env["virtual_env"], "~/envs/demo")
        # Path-free fields keep their raw values.
        self.assertEqual(env["conda_default_env"], "demo")

    def test_transient_home_key_drives_redaction_and_is_dropped(self):
        parsed = parse_probe_output(
            "hostname=node1\nhome=/home/u\npath=/home/u/bin:/usr/bin\n"
            "conda_prefix=/home/u/env\nvirtual_env=/home/u/venv\n"
            "conda_default_env=demo\ncontainer=docker\n"
        )
        self.assertEqual(parsed["hostname"], _hashed_hostname("node1"))
        self.assertEqual(parsed["path"], "~/bin:/usr/bin")
        self.assertEqual(parsed["conda_prefix"], "~/env")
        self.assertEqual(parsed["virtual_env"], "~/venv")
        self.assertEqual(parsed["conda_default_env"], "demo")
        self.assertEqual(parsed["container"], "docker")
        self.assertFalse("home" in parsed)

    def test_redaction_dedupes_fingerprints_across_hosts_and_homes(self):
        # Home-prefix differences vanish entirely after redaction; only the
        # (hashed) hostname still distinguishes the two documents.
        first = parse_probe_output(
            "hostname=node-a\nhome=/home/alice\nos=Linux\nconda_prefix=/home/alice/env\n")
        second = parse_probe_output(
            "hostname=node-b\nhome=/home/bob\nos=Linux\nconda_prefix=/home/bob/env\n")
        self.assertEqual({k: v for k, v in first.items() if k != "hostname"},
                         {k: v for k, v in second.items() if k != "hostname"})
        self.assertNotEqual(first["hostname"], second["hostname"])
        # Rich captures therefore share their sub-fingerprints across hosts:
        # identical setups deduplicate at the system/hardware/conda level.
        probe = (
            "capture_schema=1\nos=Linux\nos_release=6.1\nmachine=x86_64\n"
            "hostname={host}\nhome=/home/{user}\nconda_prefix=/home/{user}/env\n"
            "conda_present=0\ncapture_complete=1\n"
        )
        rich_first = parse_probe_output(probe.format(host="node-a", user="alice"))
        rich_second = parse_probe_output(probe.format(host="node-b", user="bob"))
        for key in ("system_fingerprint", "hardware_fingerprint"):
            self.assertEqual(rich_first[key], rich_second[key], key)
        self.assertEqual(rich_first["conda"], rich_second["conda"])

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
        # The transient home key is consumed by redaction, never persisted.
        self.assertFalse("home" in probed)
        self.assertTrue(probed["hostname"].startswith("sha256:"))
        home = str(Path.home())
        if home != "/":
            self.assertFalse(home in str(probed.get("path", "")))

    def test_parse_probe_output_treats_empty_values_as_missing(self):
        parsed = parse_probe_output("hostname=node1\nconda_prefix=\npath=\n=dropped\n\n")
        self.assertEqual(parsed, {"hostname": _hashed_hostname("node1")})

    def test_parse_probe_output_dockerenv_flag(self):
        self.assertEqual(parse_probe_output("dockerenv=1\n"), {"dockerenv": True})
        self.assertEqual(parse_probe_output("dockerenv=\n"), {})


class TestEnvironmentSummary(PytestAssertions):
    def test_full_document_renders_all_segments(self):
        document = {
            "system": {"os": "Linux", "os_release": "6.1.0",
                       "distribution": {"pretty_name": "Ubuntu 22.04.3 LTS"}},
            "hardware": {"cpu": ["flags : avx sse", "model name\t:  Intel(R)  Xeon(R) Gold 6230"],
                         "memory_total": "65432108 kB",
                         "nvidia_gpus": ["NVIDIA A100, 535.104.05, 8.0"]},
            "conda_default_env": "bio",
            "conda": {"status": "captured", "packages": [{"name": "a"}, {"name": "b"}]},
            "capture_status": "partial",
        }
        self.assertEqual(
            environment_summary(document),
            "Ubuntu 22.04.3 LTS; Intel(R) Xeon(R) Gold 6230; 65432108 kB; "
            "NVIDIA A100 (driver 535.104.05); conda: bio (2 packages); capture: partial",
        )

    def test_missing_fields_are_omitted(self):
        self.assertEqual(environment_summary({}), "")
        self.assertEqual(environment_summary({"capture_status": "complete"}), "")
        # Legacy documents without structured sections fall back to os fields.
        self.assertEqual(environment_summary({"os": "Linux", "os_release": "6.1"}), "Linux 6.1")
        self.assertEqual(
            environment_summary({"os": "Linux", "machine": "x86_64", "capture_status": "failed"}),
            "Linux; capture: failed",
        )
        multiple = {"hardware": {"nvidia_gpus": ["A100, 535.1", "A100, 535.1"]}}
        self.assertEqual(environment_summary(multiple), "2x A100 (driver 535.1)")


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
