"""Transport failures on a remote mirror must never look like data loss.

The regression these tests pin down: an interrupted ``operon evict`` dropped
its SSH connection, and every later file was then reported as "diverges from
its manifest" and persisted as a ``MISSING`` remote location — although the
remote still held all of them.  ``verify`` only live-checks locations recorded
as ``AVAILABLE``, so those false marks turned a network hiccup into a
declared file loss.  Only a positive "no such file" answer may mean absent;
every other failure is an unknown and must abort the run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import RemoteUnavailableError
from operon.files import ingest_file, verify_files
from operon.remotes import SFTPStore, evict_local, get_remote, push, remote_sha256
from operon.utils import path_size_bytes, sha256_path

from .test_execution import FakeSSHClient, FakeSFTP


@pytest.fixture
def mirror(tmp_path: Path, monkeypatch):
    """A project with three ingested assemblies and a local directory as remote."""
    monkeypatch.setattr("operon.remotes.connect_ssh", lambda *a, **k: FakeSSHClient())
    root = tmp_path / "project"
    assert main(["--project", str(root), "init", str(root), "--project-id", "PRJ_TRANSPORT"]) == 0
    project = load_project(root)
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    config = yaml.safe_load(project.config_path.read_text(encoding="utf-8"))
    config["remotes"] = {"hpc": {"type": "sftp", "host": "fake", "root": str(remote_dir)}}
    project.config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    project.config["remotes"] = config["remotes"]
    db = Database(project.db_path)
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Testus", "taxonomy_source": "NCBI"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    records = []
    for index in (1, 2, 3):
        assembly_id = f"ASM_{index:06d}"
        db.insert_row("assemblies", {
            "assembly_id": assembly_id, "sample_id": "SMP_000001",
            "assembly_level": "contig", "assembly_version": 1,
        })
        source = tmp_path / f"{assembly_id}.fa"
        source.write_text(f">ctg{index}\n" + "ACGT" * (100 * index) + "\n", encoding="utf-8")
        records.append(ingest_file(db, project, source, "assembly", assembly_id, "genome_fasta"))
    try:
        yield project, db, remote_dir, records
    finally:
        db.close()


def _break_connection(monkeypatch, needle: str, message: str = "Socket is closed") -> None:
    """Make every lstat/stat on a path containing ``needle`` look like a dead link."""
    real_lstat = FakeSFTP.lstat
    real_stat = FakeSFTP.stat

    def flaky_lstat(self, path: str):
        if needle in str(path):
            raise OSError(message)
        return real_lstat(self, path)

    def flaky_stat(self, path: str):
        if needle in str(path):
            raise OSError(message)
        return real_stat(self, path)

    monkeypatch.setattr(FakeSFTP, "lstat", flaky_lstat)
    monkeypatch.setattr(FakeSFTP, "stat", flaky_stat)


# --------------------------------------------------------------------------- #
# store primitives: absence vs. unknown
# --------------------------------------------------------------------------- #

def test_exists_reports_absence_but_raises_on_a_dead_connection(tmp_path, monkeypatch):
    root = tmp_path / "remote"
    root.mkdir()
    store = SFTPStore(get_remote(_spec_project(tmp_path, root), "r"), client=FakeSSHClient())
    assert store.exists("missing") is False
    (root / "file").write_text("data", encoding="utf-8")
    assert store.exists("file") is True
    monkeypatch.setattr(store.sftp, "stat", lambda path: (_ for _ in ()).throw(OSError("Socket is closed")))
    with pytest.raises(RemoteUnavailableError, match="cannot determine whether file exists"):
        store.exists("file")
    store.close()


def test_matches_reports_divergence_but_raises_on_a_dead_connection(tmp_path, monkeypatch):
    root = tmp_path / "remote"
    root.mkdir()
    store = SFTPStore(get_remote(_spec_project(tmp_path, root), "r"), client=FakeSSHClient())
    local = root / "file"
    local.write_text("data", encoding="utf-8")
    assert store.matches("file", sha256_path(local), path_size_bytes(local)) is True
    assert store.matches("file", "0" * 64, path_size_bytes(local)) is False
    assert store.matches("missing", sha256_path(local), path_size_bytes(local)) is False
    monkeypatch.setattr(store.sftp, "lstat", lambda path: (_ for _ in ()).throw(OSError("Socket is closed")))
    with pytest.raises(RemoteUnavailableError, match="cannot stat file"):
        store.matches("file", sha256_path(local), path_size_bytes(local))
    store.close()


def test_remote_sha256_turns_a_dead_channel_into_an_unavailable_error():
    class DeadChannel:
        @staticmethod
        def recv_exit_status():
            return 1

    class DeadClient:
        @staticmethod
        def exec_command(*_args, **_kwargs):
            raise OSError("Socket is closed")

        @staticmethod
        def open_sftp():
            return DeadSFTP()

    class DeadSFTP:
        @staticmethod
        def open(_path, _mode="r"):
            raise OSError("Socket is closed")

    with pytest.raises(RemoteUnavailableError, match="cannot stream remote file"):
        remote_sha256(DeadClient(), "/remote/file", sftp=DeadSFTP())


def _spec_project(tmp_path: Path, root: Path):
    """Minimal project object carrying one remote spec for store-level tests."""
    project_dir = tmp_path / "spec-project"
    if not project_dir.exists():
        project_dir.mkdir()
        (project_dir / "project.yaml").write_text(
            yaml.safe_dump({
                "project": {"id": "PRJ_SPEC", "name": "spec"},
                "remotes": {"r": {"type": "sftp", "host": "fake", "root": str(root)}},
            }),
            encoding="utf-8",
        )
    return load_project(project_dir)


# --------------------------------------------------------------------------- #
# evict: abort on transport failure, never record a false MISSING
# --------------------------------------------------------------------------- #

def test_evict_aborts_on_a_dead_connection_without_touching_the_database(mirror, monkeypatch):
    project, db, _remote, records = mirror
    assert all(result["status"] == "uploaded" for result in push(db, project, "hpc"))
    _break_connection(monkeypatch, "ASM_000002")

    results = evict_local(db, project, "hpc")

    statuses = [result["status"] for result in results]
    assert statuses == ["evicted", "error"], statuses
    assert "RemoteUnavailableError" in results[-1]["error"]
    assert "aborted" in results[-1]["error"]
    # The failing file was not marked missing, still has its bytes, and keeps
    # its local status; the file after it was never attempted.
    failed = records[1]
    row = db.conn.execute("SELECT status FROM files WHERE file_id=?", (failed["file_id"],)).fetchone()
    assert row["status"] == "CHECKSUM_VERIFIED"
    assert not db.conn.execute(
        "SELECT 1 FROM file_locations WHERE file_id=? AND status='MISSING'", (failed["file_id"],)
    ).fetchone()
    assert (project.root / failed["relative_path"]).exists()
    untouched = records[2]
    assert db.conn.execute("SELECT status FROM files WHERE file_id=?", (untouched["file_id"],)).fetchone()["status"] == "CHECKSUM_VERIFIED"
    assert (project.root / untouched["relative_path"]).exists()


def test_evict_resumes_after_the_connection_recovers(mirror, monkeypatch):
    project, db, _remote, records = mirror
    assert all(result["status"] == "uploaded" for result in push(db, project, "hpc"))
    with monkeypatch.context() as broken:
        _break_connection(broken, "ASM_000002")
        first = evict_local(db, project, "hpc")
    assert [r["status"] for r in first][-1] == "error"

    # Re-running the same command with a healthy connection finishes the work:
    # already-evicted files are skipped and the rest are evicted in order.
    second = evict_local(db, project, "hpc")
    assert [r["status"] for r in second] == ["skipped", "evicted", "evicted"]
    for record in records:
        row = db.conn.execute("SELECT status FROM files WHERE file_id=?", (record["file_id"],)).fetchone()
        assert row["status"] == "REMOTE_ONLY"
        assert not (project.root / record["relative_path"]).exists()
        location = db.conn.execute(
            "SELECT status FROM file_locations WHERE file_id=? AND location_name='hpc'", (record["file_id"],)
        ).fetchone()
        assert location["status"] == "AVAILABLE"


def test_evict_keeps_the_manifest_truthful_when_removal_fails(mirror, monkeypatch):
    project, db, _remote, records = mirror
    assert all(result["status"] == "uploaded" for result in push(db, project, "hpc"))
    target = records[1]
    local = project.root / target["relative_path"]
    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self == local:
            raise PermissionError("locked by another process")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    results = evict_local(db, project, "hpc", file_ids=[target["file_id"]])
    assert results[0]["status"] == "error"
    assert "locked by another process" in results[0]["error"]
    # Bytes are still present, so the record must not claim REMOTE_ONLY, and the
    # informational placeholder is withdrawn.
    assert local.exists()
    row = db.conn.execute("SELECT status FROM files WHERE file_id=?", (target["file_id"],)).fetchone()
    assert row["status"] == "CHECKSUM_VERIFIED"
    assert not (project.root / ".operon" / "placeholders" / f"{target['file_id']}.json").exists()


def test_verify_reports_unverified_instead_of_missing_when_the_connection_dies(mirror, monkeypatch):
    project, db, _remote, records = mirror
    assert all(result["status"] == "uploaded" for result in push(db, project, "hpc"))
    assert [r["status"] for r in evict_local(db, project, "hpc")] == ["evicted", "evicted", "evicted"]
    target = records[0]
    _break_connection(monkeypatch, "ASM_000001")

    results = verify_files(db, project, [target["file_id"]])

    assert [r["status"] for r in results] == ["REMOTE_UNVERIFIED"]
    row = db.conn.execute("SELECT status FROM files WHERE file_id=?", (target["file_id"],)).fetchone()
    assert row["status"] == "REMOTE_ONLY"
    location = db.conn.execute(
        "SELECT status FROM file_locations WHERE file_id=? AND location_name='hpc'", (target["file_id"],)
    ).fetchone()
    assert location["status"] == "AVAILABLE"


def test_failed_open_closes_the_sqlite_connection(tmp_path):
    """A failed schema setup must not leave a handle for the garbage collector."""
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    project.db_path.write_bytes(b"not a sqlite database")
    for sidecar in ("-wal", "-shm"):
        Path(f"{project.db_path}{sidecar}").unlink(missing_ok=True)
    with pytest.raises(sqlite3.DatabaseError):
        Database(project.db_path)
