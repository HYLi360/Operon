"""Remote storage (SFTP mirror) tests with an in-process fake client."""

from __future__ import annotations

import tempfile
import base64
import hashlib
import json
import sys
import textwrap
from types import SimpleNamespace
from pathlib import Path

from tests.helpers import PytestAssertions

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ConflictError, RemoteError, ValidationError
from operon.files import ingest_file, verify_files
from operon.remotes import (
    REMOTE_MANIFEST_LOCK_NAME,
    SFTPStore,
    connect_ssh,
    evict_local,
    fetch_url_to_temp,
    get_remote,
    pull,
    push,
    verify_remote_record,
)
from operon.tools import run_analysis

from .test_execution import FakeSSHClient


class TestRemoteConfig(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_REM_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)

    def test_unknown_remote(self):
        with self.assertRaisesRegex(ValidationError, "unknown remote"):
            get_remote(self.project, "nowhere")

    def test_fetch_url_rejects_bad_inputs(self):
        with self.assertRaisesRegex(ValidationError, "unsupported remote source"):
            fetch_url_to_temp(self.project, "https://example.org/x.fa")
        with self.assertRaisesRegex(ValidationError, "invalid sftp URL"):
            fetch_url_to_temp(self.project, "sftp://host-only-no-path")
        with self.assertRaisesRegex(ValidationError, "invalid remote URL"):
            fetch_url_to_temp(self.project, "remote://missing-path")
        with self.assertRaisesRegex(ValidationError, "unsafe path component"):
            fetch_url_to_temp(self.project, "remote://missing/../escape.fa")

    def test_ssh_host_keys_are_rejected_by_default_and_insecure_mode_is_explicit(self, monkeypatch):
        class MissingHostKeyPolicy:
            pass

        class RejectPolicy(MissingHostKeyPolicy):
            pass

        class AutoAddPolicy(MissingHostKeyPolicy):
            pass

        class FakeClient:
            def __init__(self):
                self.policy = None
                self.connect_kwargs = None
                self.closed = False

            def load_system_host_keys(self):
                pass

            def load_host_keys(self, path):
                self.known_hosts = path

            def set_missing_host_key_policy(self, policy):
                self.policy = policy

            def connect(self, **kwargs):
                self.connect_kwargs = kwargs

            def close(self):
                self.closed = True

        created = []

        def client_factory():
            client = FakeClient()
            created.append(client)
            return client

        fake_paramiko = SimpleNamespace(
            SSHClient=client_factory,
            MissingHostKeyPolicy=MissingHostKeyPolicy,
            RejectPolicy=RejectPolicy,
            AutoAddPolicy=AutoAddPolicy,
            SSHException=RuntimeError,
        )
        monkeypatch.setattr("operon.remotes.import_paramiko", lambda: fake_paramiko)

        connect_ssh("hpc.example.org")
        self.assertTrue(isinstance(created[-1].policy, RejectPolicy))
        connect_ssh("hpc.example.org", insecure_accept_unknown_host=True)
        self.assertTrue(isinstance(created[-1].policy, AutoAddPolicy))

    def test_ssh_client_is_closed_when_authentication_fails(self, monkeypatch):
        class MissingHostKeyPolicy:
            pass

        class Client:
            closed = False

            def load_system_host_keys(self):
                pass

            def set_missing_host_key_policy(self, policy):
                pass

            def connect(self, **kwargs):
                raise RuntimeError("authentication rejected")

            def close(self):
                self.closed = True

        client = Client()
        fake_paramiko = SimpleNamespace(
            SSHClient=lambda: client,
            MissingHostKeyPolicy=MissingHostKeyPolicy,
            RejectPolicy=MissingHostKeyPolicy,
            AutoAddPolicy=MissingHostKeyPolicy,
            SSHException=RuntimeError,
        )
        monkeypatch.setattr("operon.remotes.import_paramiko", lambda: fake_paramiko)
        with self.assertRaisesRegex(RemoteError, "authentication rejected"):
            connect_ssh("hpc.example.org")
        self.assertTrue(client.closed)

    def test_ssh_pinned_fingerprint_is_verified_after_connect(self, monkeypatch):
        class MissingHostKeyPolicy:
            pass

        class Key:
            def asbytes(self):
                return b"server-public-key"

        key = Key()

        class Client:
            def load_system_host_keys(self):
                pass

            def set_missing_host_key_policy(self, policy):
                self.policy = policy

            def connect(self, **kwargs):
                pass

            def get_transport(self):
                return SimpleNamespace(get_remote_server_key=lambda: key)

            def close(self):
                self.closed = True

        fake_paramiko = SimpleNamespace(
            SSHClient=Client,
            MissingHostKeyPolicy=MissingHostKeyPolicy,
            RejectPolicy=MissingHostKeyPolicy,
            AutoAddPolicy=MissingHostKeyPolicy,
            SSHException=RuntimeError,
        )
        monkeypatch.setattr("operon.remotes.import_paramiko", lambda: fake_paramiko)
        expected = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        connect_ssh("hpc.example.org", host_key_sha256=f"SHA256:{expected}")
        with self.assertRaisesRegex(RemoteError, "fingerprint mismatch"):
            connect_ssh("hpc.example.org", host_key_sha256="SHA256:not-the-key")


class TestPushPull(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_SYNC_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        self.remote_dir = self.root / "remote"
        self.remote_dir.mkdir()
        # Persist the remote so both the in-memory project and CLI reloads see it.
        import yaml
        config = yaml.safe_load(self.project.config_path.read_text(encoding="utf-8"))
        config["remotes"] = {
            "mirror": {"type": "sftp", "host": "fake", "root": str(self.remote_dir)},
        }
        self.project.config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        self.project.config["remotes"] = config["remotes"]
        self.db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Testus", "taxonomy_source": "NCBI"})
        self.db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        self.db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001", "assembly_level": "contig", "assembly_version": 1})
        source = self.root / "asm.fa"
        source.write_text(">ctg1\n" + "ACGT" * 250 + "\n", encoding="utf-8")
        self.file_row = ingest_file(self.db, self.project, source, "assembly", "ASM_000001", "genome_fasta")

    def _store(self) -> SFTPStore:
        return SFTPStore(get_remote(self.project, "mirror"), client=FakeSSHClient())

    def _add_other_file(self, name: str = "notes.txt") -> dict:
        source = self.root / name
        source.write_text(f"contents of {name}\n", encoding="utf-8")
        return ingest_file(
            self.db, self.project, source, "assembly", "ASM_000001", "other",
            fmt="txt", compression="none",
        )

    def test_push_is_idempotent_and_verified(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        results = push(self.db, self.project, "mirror")
        self.assertEqual([r["status"] for r in results], ["uploaded"])
        rel = self.file_row["relative_path"]
        remote_copy = self.remote_dir / rel
        self.assertTrue(remote_copy.exists())
        self.assertEqual(remote_copy.read_bytes(), (self.root / rel).read_bytes())
        manifest = self._store().read_manifest()
        entry = manifest["files"][rel]
        self.assertEqual(entry["sha256"], self.file_row["sha256"])
        self.assertEqual(entry["file_id"], self.file_row["file_id"])
        # Second push: same content on the remote -> skipped, nothing changes.
        results = push(self.db, self.project, "mirror")
        self.assertEqual([r["status"] for r in results], ["skipped"])
        rows = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='push:mirror'"
        ).fetchone()
        self.assertEqual(rows["n"], 2)

    def test_push_continues_after_item_failure_and_publishes_manifest_once(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        second = self._add_other_file()
        original_put = SFTPStore.put
        original_write_manifest = SFTPStore.write_manifest
        put_calls = 0
        manifest_writes = 0

        def fail_first_put(store, local_path, relative_path):
            nonlocal put_calls
            put_calls += 1
            if put_calls == 1:
                raise RemoteError("injected upload failure")
            return original_put(store, local_path, relative_path)

        def count_manifest_write(store, manifest):
            nonlocal manifest_writes
            manifest_writes += 1
            return original_write_manifest(store, manifest)

        monkeypatch.setattr(SFTPStore, "put", fail_first_put)
        monkeypatch.setattr(SFTPStore, "write_manifest", count_manifest_write)
        results = push(
            self.db, self.project, "mirror",
            file_ids=[self.file_row["file_id"], second["file_id"]],
        )
        self.assertEqual([result["status"] for result in results], ["error", "uploaded"])
        self.assertIn("injected upload failure", results[0]["error"])
        self.assertEqual(manifest_writes, 1)
        manifest = self._store().read_manifest()
        self.assertFalse(self.file_row["relative_path"] in manifest["files"])
        self.assertIn(second["relative_path"], manifest["files"])

    def test_manifest_lock_refuses_concurrent_writer(self):
        lock_path = self.remote_dir / REMOTE_MANIFEST_LOCK_NAME
        lock_path.mkdir()
        (lock_path / "owner.json").write_text("{}", encoding="utf-8")
        with self._store() as store:
            with self.assertRaisesRegex(RemoteError, "manifest is locked"):
                with store.manifest_lock(timeout=0):
                    pass

    def test_failed_upload_removes_unique_remote_temp(self):
        source = self.root / "broken-upload.txt"
        source.write_text("payload", encoding="utf-8")
        with self._store() as store:
            def partial_put(local, remote):
                Path(remote).write_bytes(b"partial")
                raise IOError("injected transfer interruption")

            store.sftp.put = partial_put
            with self.assertRaisesRegex(IOError, "transfer interruption"):
                store.put(source, "staging/broken-upload.txt")
        leftovers = list(self.remote_dir.rglob("*.operon-tmp-*"))
        self.assertEqual(leftovers, [])

    def test_push_refuses_conflicting_remote_bytes(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        rel = self.file_row["relative_path"]
        # An outsider modified the remote copy; the local manifest entry must win.
        (self.remote_dir / rel).write_text(">corrupted\nTTTT\n", encoding="utf-8")
        results = push(self.db, self.project, "mirror")
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("ConflictError", results[0]["error"])
        location = self.db.conn.execute(
            "SELECT status FROM file_locations WHERE file_id=? AND location_name='mirror'",
            (self.file_row["file_id"],),
        ).fetchone()
        self.assertEqual(location["status"], "CORRUPT")

    def _location_status(self, file_id: str) -> str:
        row = self.db.conn.execute(
            "SELECT status FROM file_locations WHERE file_id=? AND location_name='mirror'",
            (file_id,),
        ).fetchone()
        return row["status"] if row else ""

    def test_push_indexes_preexisting_identical_remote_bytes(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        rel = self.file_row["relative_path"]
        remote_copy = self.remote_dir / rel
        remote_copy.parent.mkdir(parents=True, exist_ok=True)
        # Bytes landed on the remote out of band; the manifest has no entry.
        remote_copy.write_bytes((self.root / rel).read_bytes())
        results = push(self.db, self.project, "mirror")
        self.assertEqual([r["status"] for r in results], ["indexed"])
        entry = self._store().read_manifest()["files"][rel]
        self.assertEqual(entry["sha256"], self.file_row["sha256"])
        self.assertEqual(self._location_status(self.file_row["file_id"]), "AVAILABLE")

    def test_push_marks_corrupt_on_divergent_remote_bytes_without_entry(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        rel = self.file_row["relative_path"]
        remote_copy = self.remote_dir / rel
        remote_copy.parent.mkdir(parents=True, exist_ok=True)
        remote_copy.write_text("foreign bytes", encoding="utf-8")
        results = push(self.db, self.project, "mirror")
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("ConflictError", results[0]["error"])
        # The divergent remote bytes are left untouched for manual resolution.
        self.assertEqual(remote_copy.read_text(encoding="utf-8"), "foreign bytes")
        self.assertEqual(self._location_status(self.file_row["file_id"]), "CORRUPT")
        self.assertFalse(rel in self._store().read_manifest()["files"])

    def test_push_refuses_local_bytes_diverging_from_manifest_identity(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        rel = self.file_row["relative_path"]
        (self.root / rel).write_text("tampered local bytes", encoding="utf-8")
        results = push(self.db, self.project, "mirror")
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("ConflictError: local content does not match manifest identity",
                      results[0]["error"])
        self.assertFalse((self.remote_dir / rel).exists())
        self.assertEqual(self._store().read_manifest()["files"], {})

    def test_push_skips_when_local_missing_but_remote_copy_verified(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        (self.root / self.file_row["relative_path"]).unlink()
        results = push(self.db, self.project, "mirror")
        self.assertEqual([r["status"] for r in results], ["skipped"])

    def test_push_errors_when_local_missing_and_no_verified_remote_copy(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        (self.root / self.file_row["relative_path"]).unlink()
        results = push(self.db, self.project, "mirror")
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("RemoteError", results[0]["error"])
        self.assertIn("no verified copy", results[0]["error"])

    def test_pull_marks_corrupt_when_remote_diverges_from_manifest_entry(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        (self.remote_dir / self.file_row["relative_path"]).write_text(
            ">corrupted\nTTTT\n", encoding="utf-8"
        )
        results = pull(self.db, self.project, "mirror")
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("ConflictError", results[0]["error"])
        self.assertEqual(self._location_status(self.file_row["file_id"]), "CORRUPT")

    def test_pull_with_file_ids_errors_per_item_without_manifest_entry(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        second = self._add_other_file()
        results = pull(
            self.db, self.project, "mirror",
            file_ids=[self.file_row["file_id"], second["file_id"]],
        )
        self.assertEqual([r["status"] for r in results], ["error", "error"])
        for result in results:
            self.assertIn("not present in the remote manifest", result["error"])
        runs = self.db.conn.execute(
            "SELECT status, error FROM workflow_runs WHERE step='pull:mirror' ORDER BY run_id"
        ).fetchall()
        self.assertEqual([run["status"] for run in runs], ["failed", "failed"])

    def test_verify_remote_record_marks_missing_without_manifest_entry(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        with self.assertRaisesRegex(RemoteError, "no manifest entry"):
            verify_remote_record(self.project, "mirror", self.file_row, db=self.db)
        self.assertEqual(self._location_status(self.file_row["file_id"]), "MISSING")

    def test_verify_remote_record_marks_corrupt_on_entry_identity_mismatch(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        rel = self.file_row["relative_path"]
        manifest_path = self.remote_dir / "operon-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][rel]["sha256"] = "b" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ConflictError, "does not match"):
            verify_remote_record(self.project, "mirror", self.file_row, db=self.db)
        self.assertEqual(self._location_status(self.file_row["file_id"]), "CORRUPT")

    def test_evict_skips_and_rewrites_placeholder_when_local_already_absent(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        evict_local(self.db, self.project, "mirror", [self.file_row["file_id"]])
        placeholder = self.root / ".operon" / "placeholders" / f"{self.file_row['file_id']}.json"
        placeholder.unlink()
        results = evict_local(self.db, self.project, "mirror", [self.file_row["file_id"]])
        self.assertEqual(results[0]["status"], "skipped")
        self.assertTrue(placeholder.exists())
        status = self.db.conn.execute(
            "SELECT status FROM files WHERE file_id=?", (self.file_row["file_id"],)
        ).fetchone()["status"]
        self.assertEqual(status, "REMOTE_ONLY")

    def test_remote_manifest_is_bound_to_one_project(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        manifest_path = self.remote_dir / "operon-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["project_id"], self.project.project_id)
        manifest["project_id"] = "PRJ_SOMEONE_ELSE"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ConflictError, "belongs to project"):
            pull(self.db, self.project, "mirror")

    def test_push_unknown_file_id(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        with self.assertRaisesRegex(ValidationError, "unknown file_id"):
            push(self.db, self.project, "mirror", file_ids=["FIL_999999"])

    def test_pull_restores_missing_file(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        rel = self.file_row["relative_path"]
        local = self.root / rel
        local.unlink()
        self.db.conn.execute(
            "UPDATE files SET status='MISSING' WHERE file_id=?", (self.file_row["file_id"],)
        )
        self.db.conn.commit()
        results = pull(self.db, self.project, "mirror")
        self.assertEqual([r["status"] for r in results], ["downloaded"])
        self.assertEqual(local.read_bytes(), (self.remote_dir / rel).read_bytes())
        status = self.db.conn.execute(
            "SELECT status FROM files WHERE file_id=?", (self.file_row["file_id"],)
        ).fetchone()
        self.assertEqual(status["status"], "CHECKSUM_VERIFIED")
        # Second pull: local copy already matches -> skipped.
        results = pull(self.db, self.project, "mirror")
        self.assertEqual([r["status"] for r in results], ["skipped"])

    def test_pull_refuses_to_overwrite_different_local_bytes(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        rel = self.file_row["relative_path"]
        local = self.root / rel
        local.unlink()
        local.write_text(">different\nGGGG\n", encoding="utf-8")
        results = pull(self.db, self.project, "mirror")
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("ConflictError", results[0]["error"])

    def test_pull_continues_after_conflict_and_audits_restored_status(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        second = self._add_other_file()
        push(
            self.db, self.project, "mirror",
            file_ids=[self.file_row["file_id"], second["file_id"]],
        )
        first_local = self.root / self.file_row["relative_path"]
        first_local.write_text("different local bytes", encoding="utf-8")
        second_local = self.root / second["relative_path"]
        second_local.unlink()
        self.db.conn.execute(
            "UPDATE files SET status='MISSING' WHERE file_id=?", (second["file_id"],)
        )
        self.db.conn.commit()

        results = pull(
            self.db, self.project, "mirror",
            file_ids=[self.file_row["file_id"], second["file_id"]],
        )
        self.assertEqual([result["status"] for result in results], ["error", "downloaded"])
        self.assertTrue(second_local.exists())
        audit = self.db.conn.execute(
            "SELECT actor, new_value FROM changes WHERE object_type='files' AND object_id=? "
            "ORDER BY change_id DESC LIMIT 1",
            (second["file_id"],),
        ).fetchone()
        self.assertEqual(audit["actor"], "operon pull")
        self.assertEqual(audit["new_value"], "CHECKSUM_VERIFIED")

    def test_fetch_remote_url_to_temp(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        rel = self.file_row["relative_path"]
        tmp_path = fetch_url_to_temp(self.project, f"remote://mirror/{rel}")
        self.addCleanup(lambda: tmp_path.unlink(missing_ok=True))
        self.assertEqual(tmp_path.read_bytes(), (self.root / rel).read_bytes())
        self.assertTrue(tmp_path.name.endswith(Path(rel).name))

    def test_fetch_sftp_url_to_temp(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        rel = self.file_row["relative_path"]
        tmp_path = fetch_url_to_temp(self.project, f"sftp://fake{self.remote_dir}/{rel}")
        self.addCleanup(lambda: tmp_path.unlink(missing_ok=True))
        self.assertEqual(tmp_path.read_bytes(), (self.root / rel).read_bytes())

    def test_same_size_corruption_is_detected_without_remote_sha256(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        rel = self.file_row["relative_path"]
        remote = self.remote_dir / rel
        original = remote.read_bytes()
        remote.write_bytes(b"X" * len(original))
        client = FakeSSHClient()
        original_exec = client.exec_command

        def no_sha256(command, timeout=None):
            return original_exec("false" if command.startswith("sha256sum ") else command, timeout)

        client.exec_command = no_sha256
        with SFTPStore(get_remote(self.project, "mirror"), client=client) as store:
            self.assertFalse(store.matches(rel, self.file_row["sha256"], self.file_row["size_bytes"]))

    def test_manifest_overwrite_requires_posix_rename(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        client = FakeSSHClient()
        client.sftp.posix_rename = None
        with SFTPStore(get_remote(self.project, "mirror"), client=client) as store:
            doc = store.read_manifest()
            with self.assertRaisesRegex(RemoteError, "POSIX rename"):
                store.write_manifest(doc)

    def test_pull_rejects_remote_manifest_path_escape(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        manifest_path = self.remote_dir / "operon-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = next(iter(manifest["files"].values()))
        manifest["files"] = {"../../escaped.fa": entry}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "unsafe path component"):
            pull(self.db, self.project, "mirror")
        self.assertFalse((self.root.parent / "escaped.fa").exists())

    def test_default_pull_rejects_entry_absent_from_local_sqlite(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        manifest_path = self.remote_dir / "operon-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = next(iter(manifest["files"].values()))
        entry["file_id"] = "FIL_999999"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        results = pull(self.db, self.project, "mirror")
        self.assertEqual(results[0]["status"], "error")
        self.assertIn("absent from the local SQLite manifest", results[0]["error"])
        failed = self.db.conn.execute(
            "SELECT status FROM workflow_runs WHERE step='pull:mirror' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(failed["status"], "failed")

    def test_directory_artifact_round_trip(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        source = self.root / "tree"
        (source / "empty").mkdir(parents=True)
        (source / "data").mkdir()
        (source / "data" / "x.txt").write_text("directory bytes", encoding="utf-8")
        (source / "link").symlink_to("data/x.txt")
        row = ingest_file(
            self.db, self.project, source, "assembly", "ASM_000001", "other",
            fmt="directory", compression="none",
        )
        results = push(self.db, self.project, "mirror", file_ids=[row["file_id"]])
        self.assertEqual(results[0]["status"], "uploaded")
        local = self.root / row["relative_path"]
        import shutil
        shutil.rmtree(local)
        results = pull(self.db, self.project, "mirror", file_ids=[row["file_id"]])
        self.assertEqual(results[0]["status"], "downloaded")
        self.assertTrue((local / "empty").is_dir())
        self.assertEqual((local / "data" / "x.txt").read_text(), "directory bytes")
        self.assertTrue((local / "link").is_symlink())

    def test_directory_symlink_to_directory_has_stable_identity(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        source = self.root / "tree-with-directory-link"
        (source / "data").mkdir(parents=True)
        (source / "data" / "x.txt").write_text("directory bytes", encoding="utf-8")
        (source / "data-link").symlink_to("data", target_is_directory=True)
        row = ingest_file(
            self.db, self.project, source, "assembly", "ASM_000001", "other",
            fmt="directory", compression="none",
        )
        results = push(self.db, self.project, "mirror", file_ids=[row["file_id"]])
        self.assertEqual(results[0]["status"], "uploaded")
        local = self.root / row["relative_path"]
        import shutil
        shutil.rmtree(local)
        results = pull(self.db, self.project, "mirror", file_ids=[row["file_id"]])
        self.assertEqual(results[0]["status"], "downloaded")
        self.assertTrue((local / "data-link").is_symlink())
        self.assertEqual((local / "data-link").readlink().as_posix(), "data")

    def test_evict_creates_remote_only_placeholder_and_pull_hydrates(self, monkeypatch, capsys):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        results = evict_local(self.db, self.project, "mirror", [self.file_row["file_id"]])
        self.assertEqual(results[0]["status"], "evicted")
        local = self.root / self.file_row["relative_path"]
        self.assertFalse(local.exists())
        pointer = self.root / ".operon" / "placeholders" / f"{self.file_row['file_id']}.json"
        self.assertTrue(pointer.exists())
        status = self.db.conn.execute(
            "SELECT status FROM files WHERE file_id=?", (self.file_row["file_id"],)
        ).fetchone()["status"]
        self.assertEqual(status, "REMOTE_ONLY")
        location = self.db.conn.execute(
            "SELECT * FROM file_locations WHERE file_id=?", (self.file_row["file_id"],)
        ).fetchone()
        self.assertEqual(location["status"], "AVAILABLE")
        self.assertEqual(main(["--project", str(self.root), "locations"]), 0)
        self.assertIn("REMOTE_ONLY", capsys.readouterr().out)
        self.assertEqual(main(["--project", str(self.root), "verify"]), 0)
        self.assertIn("REMOTE_ONLY", capsys.readouterr().out)
        pull(self.db, self.project, "mirror", [self.file_row["file_id"]])
        self.assertTrue(local.exists())
        self.assertFalse(pointer.exists())

    def test_verify_marks_deleted_remote_only_object_missing(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        evict_local(self.db, self.project, "mirror", [self.file_row["file_id"]])
        (self.remote_dir / self.file_row["relative_path"]).unlink()

        results = verify_files(self.db, self.project, [self.file_row["file_id"]])
        self.assertEqual(results[0]["status"], "MISSING")
        file_status = self.db.conn.execute(
            "SELECT status FROM files WHERE file_id=?", (self.file_row["file_id"],)
        ).fetchone()["status"]
        location_status = self.db.conn.execute(
            "SELECT status FROM file_locations WHERE file_id=? AND location_name='mirror'",
            (self.file_row["file_id"],),
        ).fetchone()["status"]
        self.assertEqual(file_status, "MISSING")
        self.assertEqual(location_status, "MISSING")
        audit = self.db.conn.execute(
            "SELECT actor, new_value FROM changes WHERE object_type='files' AND object_id=? "
            "ORDER BY change_id DESC LIMIT 1",
            (self.file_row["file_id"],),
        ).fetchone()
        self.assertEqual(audit["actor"], "operon verify")
        self.assertEqual(audit["new_value"], "MISSING")

    def test_verify_upgrades_old_metadata_before_restoring_remote_only(self, monkeypatch):
        import yaml

        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        (self.root / self.file_row["relative_path"]).unlink()
        metadata = yaml.safe_load(self.project.schema_path.read_text(encoding="utf-8"))
        metadata["schema_version"] = "1.1"
        statuses = metadata["tables"]["files"]["fields"]["status"]["allowed"]
        metadata["tables"]["files"]["fields"]["status"]["allowed"] = [
            status for status in statuses if status != "REMOTE_ONLY"
        ]
        self.project.schema_path.write_text(
            yaml.safe_dump(metadata, sort_keys=False), encoding="utf-8"
        )

        results = verify_files(self.db, self.project, [self.file_row["file_id"]])
        self.assertEqual(results[0]["status"], "REMOTE_ONLY")
        upgraded = yaml.safe_load(self.project.schema_path.read_text(encoding="utf-8"))
        self.assertEqual(str(upgraded["schema_version"]), "1.2")
        self.assertIn(
            "REMOTE_ONLY", upgraded["tables"]["files"]["fields"]["status"]["allowed"]
        )

    def test_verify_does_not_claim_success_when_remote_is_unreachable(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        (self.root / self.file_row["relative_path"]).unlink()
        before = self.db.conn.execute(
            "SELECT status FROM files WHERE file_id=?", (self.file_row["file_id"],)
        ).fetchone()["status"]
        monkeypatch.setattr(
            "operon.remotes.connect_ssh",
            lambda *a, **k: (_ for _ in ()).throw(RemoteError("network unavailable")),
        )

        results = verify_files(self.db, self.project, [self.file_row["file_id"]])
        self.assertEqual(results[0]["status"], "REMOTE_UNVERIFIED")
        self.assertIn("network unavailable", results[0]["error"])
        after = self.db.conn.execute(
            "SELECT status FROM files WHERE file_id=?", (self.file_row["file_id"],)
        ).fetchone()["status"]
        self.assertEqual(after, before)

    def test_evict_continues_after_local_conflict(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        second = self._add_other_file()
        push(
            self.db, self.project, "mirror",
            file_ids=[self.file_row["file_id"], second["file_id"]],
        )
        (self.root / self.file_row["relative_path"]).write_text(
            "different local bytes", encoding="utf-8"
        )

        results = evict_local(
            self.db, self.project, "mirror",
            [self.file_row["file_id"], second["file_id"]],
        )
        self.assertEqual([result["status"] for result in results], ["error", "evicted"])
        self.assertFalse((self.root / second["relative_path"]).exists())

    def test_ssh_analysis_consumes_verified_remote_only_input(self, monkeypatch):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        push(self.db, self.project, "mirror")
        evict_local(self.db, self.project, "mirror", [self.file_row["file_id"]])
        self.db.set_file_status(
            self.file_row["file_id"], "MISSING",
            reason="simulate a previously unavailable remote",
            actor="test",
        )
        tool_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tool_tmp.cleanup)
        script = Path(tool_tmp.name) / "remote_tool.py"
        script.write_text(textwrap.dedent("""
            import pathlib
            import sys
            args = sys.argv[1:]
            if '--version' in args:
                print('remote-tool 1.2.3')
                raise SystemExit(0)
            source = pathlib.Path(args[args.index('--input') + 1])
            output = pathlib.Path(args[args.index('--output') + 1])
            output.write_text('observed=' + source.read_text().splitlines()[0] + '\\n')
        """).strip(), encoding="utf-8")
        config = {
            "version": 1,
            "tools": {
                "remote-tool": {
                    "executable": str(script), "run_method": sys.executable,
                    "version_args": ["--version"],
                    "version_pattern": r"remote-tool\s+([^\s]+)",
                    "recipes": {
                        "remote_read": {
                            "entity_type": "assembly", "file_role": "genome_fasta",
                            "format": "fasta", "database": "",
                            "output_subdir": "remote_read", "output_suffix": ".txt",
                            "arguments": ["--input", "${input}", "--output", "${output}"],
                            "result_parser": "none",
                        },
                    },
                },
            },
        }
        import yaml
        self.project.tools_config_path.write_text(
            yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
        )
        self.project.config["execution"] = {
            "backend": "ssh",
            "ssh": {"storage_remote": "mirror", "scheduler": "none"},
            "slurm": {},
        }
        results = run_analysis(
            self.project, self.db, "remote_read", backend="ssh", threads=1,
        )
        self.assertEqual(results[0]["status"], "completed", results[0].get("error"))
        output = self.root / results[0]["output"]
        self.assertIn("observed=>ctg1", output.read_text(encoding="utf-8"))
        self.assertFalse((self.root / self.file_row["relative_path"]).exists())
        status = self.db.conn.execute(
            "SELECT status FROM files WHERE file_id=?", (self.file_row["file_id"],)
        ).fetchone()["status"]
        self.assertEqual(status, "REMOTE_ONLY")
        audit = self.db.conn.execute(
            "SELECT actor, old_value, new_value FROM changes "
            "WHERE object_type='files' AND object_id=? AND field='status' "
            "ORDER BY change_id DESC LIMIT 1",
            (self.file_row["file_id"],),
        ).fetchone()
        self.assertEqual(dict(audit), {
            "actor": "operon analyze", "old_value": "MISSING", "new_value": "REMOTE_ONLY",
        })

    def test_cli_push_and_remotes(self, monkeypatch, capsys):
        monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
        rc = main(["--project", str(self.root), "push", "--remote", "mirror"])
        self.assertEqual(rc, 0)
        rc = main(["--project", str(self.root), "remotes"])
        self.assertEqual(rc, 0)
        out = capsys.readouterr().out
        self.assertIn("mirror", out)
        self.assertIn("ok", out)
