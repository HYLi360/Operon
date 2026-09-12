"""Fault-injection and identity tests for SFTP mirrors, frozen taxonomy
snapshots, the TimeTree query cache, and NCBI reconciliation.

Every remote/HTTP interaction is faked at its I/O boundary: SFTP is the
filesystem-backed ``FakeSFTP`` from ``test_execution`` (plus small
failure-injecting subclasses), TimeTree sessions are the URL-routed fakes from
``test_timetree``, and no test sleeps or touches the network.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from operon import ncbi_reconcile, remotes, taxonomy
from operon.adapters.timetree import TimeTreeClient
from operon.cli import main
from operon.config import load_project, project_rel
from operon.database import Database
from operon.errors import ConflictError, ConfigError, RemoteError, ValidationError
from operon.files import ingest_file
from operon.remotes import (
    REMOTE_MANIFEST_LOCK_NAME,
    REMOTE_MANIFEST_NAME,
    SFTPStore,
    check_remote,
    evict_local,
    get_remote,
    pull,
    push,
    remote_sha256,
    verify_remote_record,
)
from tests.unit.test_execution import FakeSFTP, FakeSSHClient
from tests.unit.test_timetree import (
    BASE as TIMETREE_BASE,
    SUMMARY_3702_9606,
    make_client,
    make_session,
)


# --------------------------------------------------------------------------
# shared fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def project_db(tmp_path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def _configure_remote(project, root: Path, name: str = "mirror") -> None:
    """Persist one SFTP remote so both the live project and CLI reloads see it."""
    config = yaml.safe_load(project.config_path.read_text(encoding="utf-8"))
    config.setdefault("remotes", {})[name] = {
        "type": "sftp", "host": "fake", "root": str(root),
    }
    project.config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    project.config["remotes"] = config["remotes"]


@pytest.fixture
def remote_env(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    assert main([
        "--project", str(root), "init", str(root), "--project-id", "PRJ_T6_001",
    ]) == 0
    project = load_project(root)
    db = Database(project.db_path)
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    _configure_remote(project, remote_dir)
    monkeypatch.setattr(remotes, "connect_ssh", lambda *a, **k: FakeSSHClient())
    db.insert_row("organisms", {
        "organism_id": "ORG_000001", "scientific_name": "Testus", "taxonomy_source": "NCBI",
    })
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("assemblies", {
        "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
        "assembly_level": "contig", "assembly_version": 1,
    })
    source = root / "asm.fa"
    source.write_text(">ctg1\n" + "ACGT" * 250 + "\n", encoding="utf-8")
    record = ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    try:
        yield SimpleNamespace(
            project=project, db=db, root=root, remote_dir=remote_dir, record=record,
        )
    finally:
        db.close()


def _store(project, name: str = "mirror") -> SFTPStore:
    return SFTPStore(get_remote(project, name), client=FakeSSHClient())


def _add_directory_artifact(env, directory_name: str = "tree") -> dict:
    source = env.root / directory_name
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "x.txt").write_text("directory bytes", encoding="utf-8")
    return ingest_file(
        env.db, env.project, source, "assembly", "ASM_000001", "other",
        fmt="directory", compression="none",
    )


class _UnreadableMemberSFTP(FakeSFTP):
    """Fails a single SFTP read, like a dropped transfer mid-directory."""

    def open(self, path: str, mode: str = "r"):
        if str(path).endswith("member.bin"):
            raise IOError("transfer interrupted")
        return super().open(path, mode)


class _DeniedManifestSFTP(FakeSFTP):
    def open(self, path: str, mode: str = "r"):
        if Path(path).name == REMOTE_MANIFEST_NAME:
            raise IOError("permission denied")
        return super().open(path, mode)


class _DeniedLstatSFTP(FakeSFTP):
    def lstat(self, path: str):
        raise IOError("permission denied")


class _OwnerWriteFailsSFTP(FakeSFTP):
    """The lock owner marker cannot be written, so it is also missing on release."""

    def open(self, path: str, mode: str = "r"):
        if Path(path).name == "owner.json":
            raise IOError("no space left on device")
        return super().open(path, mode)


class _TruncatingPutSFTP(FakeSFTP):
    """Reports a successful upload while landing zero bytes."""

    def put(self, local: str, remote: str) -> None:
        Path(remote).write_bytes(b"")


class _TruncatingGetSFTP(FakeSFTP):
    """Reports a successful download while landing truncated bytes."""

    def get(self, remote: str, local: str) -> None:
        Path(local).write_bytes(b"partial")


# --------------------------------------------------------------------------
# remotes: configuration, connection, and path guards
# --------------------------------------------------------------------------

def test_import_paramiko_reports_a_missing_dependency(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "paramiko", None)
    with pytest.raises(ConfigError, match="missing the required Paramiko package"):
        remotes.import_paramiko()


def test_import_paramiko_returns_the_imported_module(monkeypatch):
    import sys
    import types

    fake_paramiko = types.ModuleType("paramiko")
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)
    assert remotes.import_paramiko() is fake_paramiko


def test_remote_path_rejects_an_empty_spec_root():
    spec = remotes.RemoteSpec("r", "host", "", 22, "", "")
    with SFTPStore(spec, client=FakeSSHClient()) as store:
        with pytest.raises(ValidationError, match="escapes configured root"):
            store.remote_path("member.fa")


# --------------------------------------------------------------------------
# remotes: hashing, remote trees, and store primitives
# --------------------------------------------------------------------------

def test_remote_sha256_falls_back_to_streaming_on_a_non_digest_reply(remote_env):
    env = remote_env
    rel = env.record["relative_path"]
    remote = env.remote_dir / rel
    remote.parent.mkdir(parents=True, exist_ok=True)
    remote.write_bytes((env.root / rel).read_bytes())
    client = FakeSSHClient()
    original_exec = client.exec_command

    def non_digest_reply(command, timeout=None):
        if command.startswith("sha256sum "):
            return original_exec("echo not-a-digest", timeout)
        return original_exec(command, timeout)

    client.exec_command = non_digest_reply
    with SFTPStore(get_remote(env.project, "mirror"), client=client) as store:
        digest = remote_sha256(
            client, store.remote_path(rel), sftp=store.sftp,
        )
    assert digest == env.record["sha256"]
    assert digest == hashlib.sha256(remote.read_bytes()).hexdigest()


def test_remote_directory_identity_reports_an_unreadable_member(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "member.bin").write_bytes(b"payload")
    with pytest.raises(RemoteError, match="cannot hash remote directory member"):
        remotes._remote_directory_identity(_UnreadableMemberSFTP(), str(tree))


def test_remove_remote_tree_reraises_non_missing_stat_errors(tmp_path):
    with pytest.raises(IOError, match="permission denied"):
        remotes._remove_remote_tree(_DeniedLstatSFTP(), str(tmp_path / "whatever"))


def test_read_manifest_reports_an_unreadable_manifest(remote_env):
    env = remote_env
    store = SFTPStore(get_remote(env.project, "mirror"), client=FakeSSHClient())
    store._sftp = _DeniedManifestSFTP()
    with pytest.raises(RemoteError, match="cannot read operon-manifest.json"):
        store.read_manifest()


def test_store_close_without_opening_sftp_keeps_the_caller_client(remote_env):
    env = remote_env
    client = FakeSSHClient()
    store = SFTPStore(get_remote(env.project, "mirror"), client=client)
    store.close()
    assert client.close_calls == 0
    # The caller-owned client is still usable after the store releases.
    assert store.sftp is client.sftp


def test_put_rejects_an_unsupported_local_directory_entry(remote_env):
    env = remote_env
    tree = env.root / "weird-tree"
    tree.mkdir()
    (tree / "a.txt").write_text("x", encoding="utf-8")
    os.mkfifo(tree / "pipe")
    with _store(env.project) as store:
        with pytest.raises(RemoteError, match="unsupported local directory entry"):
            store.put(tree, "trees/weird")
    # The partially uploaded tree is rolled back, leaving no temp artifacts.
    assert not (env.remote_dir / "trees" / "weird").exists()
    assert list(env.remote_dir.rglob("*.operon-tmp-*")) == []


def test_get_rejects_an_unsupported_remote_directory_entry(remote_env):
    env = remote_env
    tree = env.remote_dir / "special-tree"
    tree.mkdir()
    (tree / "zz-later.txt").write_text("x", encoding="utf-8")
    os.mkfifo(tree / "pipe")
    with _store(env.project) as store:
        with pytest.raises(RemoteError, match="unsupported remote artifact type"):
            store.get("special-tree", env.root / "downloaded")
    # The unsupported entry sorts first, so nothing was copied into the target.
    assert list((env.root / "downloaded").iterdir()) == []


# --------------------------------------------------------------------------
# remotes: manifest lock lifecycle
# --------------------------------------------------------------------------

def test_manifest_lock_waits_for_a_concurrent_writer_to_release(remote_env, monkeypatch):
    env = remote_env
    store = _store(env.project)
    lock = env.remote_dir / REMOTE_MANIFEST_LOCK_NAME
    lock.mkdir()
    (lock / "owner.json").write_text("{}", encoding="utf-8")
    sleeps: list[float] = []

    def competing_writer_releases(seconds: float) -> None:
        sleeps.append(seconds)
        shutil.rmtree(lock)

    monkeypatch.setattr(remotes.time, "sleep", competing_writer_releases)
    with store.manifest_lock(timeout=5):
        assert lock.is_dir()
        owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
        assert owner["token"] and owner["created_at"]
    assert sleeps == [0.2]
    assert not lock.exists()


def test_manifest_lock_is_released_when_the_owner_marker_cannot_be_written(remote_env):
    env = remote_env
    store = _store(env.project)
    store._sftp = _OwnerWriteFailsSFTP()
    lock = env.remote_dir / REMOTE_MANIFEST_LOCK_NAME
    with pytest.raises(IOError, match="no space left on device"):
        with store.manifest_lock():
            pass
    # The directory lock is released even though the owner marker never existed.
    assert not lock.exists()


# --------------------------------------------------------------------------
# remotes: push/pull failure handling and audit rows
# --------------------------------------------------------------------------

def test_push_reports_an_upload_that_lands_wrong_bytes(remote_env, monkeypatch):
    env = remote_env
    client = FakeSSHClient()
    client.sftp = _TruncatingPutSFTP()
    monkeypatch.setattr(remotes, "connect_ssh", lambda *a, **k: client)

    results = push(env.db, env.project, "mirror")
    assert results[0]["status"] == "error"
    assert "upload verification failed" in results[0]["error"]
    # The unverified upload is never claimed by the manifest or residency cache.
    rel = env.record["relative_path"]
    assert _store(env.project).read_manifest()["files"] == {}
    assert (env.remote_dir / rel).read_bytes() == b""
    assert env.db.conn.execute("SELECT COUNT(*) AS n FROM file_locations").fetchone()["n"] == 0
    run = env.db.conn.execute(
        "SELECT status, error FROM workflow_runs WHERE step='push:mirror'"
    ).fetchone()
    assert run["status"] == "failed" and "upload verification failed" in run["error"]


def test_push_reports_manifest_publication_failure(remote_env, monkeypatch):
    env = remote_env
    notes = env.root / "notes.txt"
    notes.write_text("notes\n", encoding="utf-8")
    other = ingest_file(
        env.db, env.project, notes, "assembly", "ASM_000001", "other",
        fmt="txt", compression="none",
    )
    # One artifact fails its own identity check; the other uploads cleanly and
    # then loses the single manifest batch that would have published it.
    (env.root / env.record["relative_path"]).write_text("tampered local bytes", encoding="utf-8")

    def refuse_manifest(self, doc):
        raise RemoteError("remote rejected the manifest")

    monkeypatch.setattr(SFTPStore, "write_manifest", refuse_manifest)
    results = push(
        env.db, env.project, "mirror", [env.record["file_id"], other["file_id"]],
    )
    assert [result["status"] for result in results] == ["error", "error"]
    assert "ConflictError" in results[0]["error"]
    assert "manifest publication failed" in results[1]["error"]
    assert (env.remote_dir / other["relative_path"]).read_bytes() == notes.read_bytes()
    assert not (env.remote_dir / env.record["relative_path"]).exists()
    assert not (env.remote_dir / REMOTE_MANIFEST_NAME).exists()
    assert env.db.conn.execute("SELECT COUNT(*) AS n FROM file_locations").fetchone()["n"] == 0
    runs = env.db.conn.execute(
        "SELECT error FROM workflow_runs WHERE step='push:mirror' AND status='failed'"
    ).fetchall()
    assert len(runs) == 2
    assert any("ConflictError" in row["error"] for row in runs)
    assert any("manifest publication failed" in row["error"] for row in runs)


def test_pull_removes_the_partial_file_when_verification_fails(remote_env, monkeypatch):
    env = remote_env
    push(env.db, env.project, "mirror")
    local = env.root / env.record["relative_path"]
    local.unlink()
    client = FakeSSHClient()
    client.sftp = _TruncatingGetSFTP()
    monkeypatch.setattr(remotes, "connect_ssh", lambda *a, **k: client)

    results = pull(env.db, env.project, "mirror", [env.record["file_id"]])
    assert results[0]["status"] == "error"
    assert "download verification failed" in results[0]["error"]
    assert not local.exists()
    leftovers = [p.name for p in local.parent.iterdir() if p.name.startswith(f".{local.name}.")]
    assert leftovers == []
    run = env.db.conn.execute(
        "SELECT status FROM workflow_runs WHERE step='pull:mirror'"
    ).fetchone()
    assert run["status"] == "failed"


def test_pull_removes_the_partial_directory_when_verification_fails(remote_env, monkeypatch):
    env = remote_env
    row = _add_directory_artifact(env)
    push(env.db, env.project, "mirror", [row["file_id"]])
    local = env.root / row["relative_path"]
    shutil.rmtree(local)
    client = FakeSSHClient()
    client.sftp = _TruncatingGetSFTP()
    monkeypatch.setattr(remotes, "connect_ssh", lambda *a, **k: client)

    results = pull(env.db, env.project, "mirror", [row["file_id"]])
    assert results[0]["status"] == "error"
    assert "download verification failed" in results[0]["error"]
    assert not local.exists()
    leftovers = [p.name for p in local.parent.iterdir() if p.name.startswith(f".{local.name}.")]
    assert leftovers == []


def test_pull_leaves_a_standardized_status_untouched(remote_env):
    env = remote_env
    push(env.db, env.project, "mirror")
    local = env.root / env.record["relative_path"]
    local.unlink()
    env.db.set_file_status(
        env.record["file_id"], "STANDARDIZED", reason="test setup", actor="test",
    )

    results = pull(env.db, env.project, "mirror", [env.record["file_id"]])
    assert results[0]["status"] == "downloaded"
    assert local.read_bytes() == (env.remote_dir / env.record["relative_path"]).read_bytes()
    status = env.db.conn.execute(
        "SELECT status FROM files WHERE file_id=?", (env.record["file_id"],)
    ).fetchone()["status"]
    assert status == "STANDARDIZED"
    pull_audits = env.db.conn.execute(
        "SELECT COUNT(*) AS n FROM changes WHERE object_id=? AND actor='operon pull'",
        (env.record["file_id"],),
    ).fetchone()["n"]
    assert pull_audits == 0
    location = env.db.conn.execute(
        "SELECT status, uri FROM file_locations WHERE file_id=? AND location_name='mirror'",
        (env.record["file_id"],),
    ).fetchone()
    assert location["status"] == "AVAILABLE"
    assert location["uri"] == f"remote://mirror/{env.record['relative_path']}"


# --------------------------------------------------------------------------
# remotes: verification without a database, eviction, and connectivity
# --------------------------------------------------------------------------

def test_verify_remote_record_without_a_database_writes_no_location_rows(remote_env):
    env = remote_env
    rel = env.record["relative_path"]
    local = env.root / rel
    remote = env.remote_dir / rel
    remote.parent.mkdir(parents=True, exist_ok=True)
    remote.write_bytes(local.read_bytes())
    store = _store(env.project)
    store.write_manifest({"files": {rel: {
        "file_id": env.record["file_id"], "relative_path": rel,
        "sha256": env.record["sha256"], "size_bytes": env.record["size_bytes"],
        "kind": "file",
    }}})

    assert verify_remote_record(
        env.project, "mirror", env.record, db=None, store=store,
    ) == store.remote_path(rel)

    divergent_entry = {
        "file_id": env.record["file_id"], "relative_path": rel,
        "sha256": "b" * 64, "size_bytes": env.record["size_bytes"],
    }
    with pytest.raises(ConflictError, match="does not match local manifest"):
        verify_remote_record(
            env.project, "mirror", env.record, db=None, store=store,
            manifest={"project_id": env.project.project_id, "files": {rel: divergent_entry}},
        )

    remote.write_bytes(b"X" * local.stat().st_size)
    with pytest.raises(ConflictError, match="diverges from its manifest"):
        verify_remote_record(env.project, "mirror", env.record, db=None, store=store)

    with pytest.raises(RemoteError, match="no manifest entry"):
        verify_remote_record(
            env.project, "mirror", env.record, db=None, store=store,
            manifest={"files": {}},
        )

    assert env.db.conn.execute("SELECT COUNT(*) AS n FROM file_locations").fetchone()["n"] == 0


def test_evict_removes_a_directory_artifact_and_points_at_the_remote(remote_env):
    env = remote_env
    row = _add_directory_artifact(env)
    push(env.db, env.project, "mirror", [row["file_id"]])

    results = evict_local(env.db, env.project, "mirror", [row["file_id"]])
    assert results[0]["status"] == "evicted"
    assert not (env.root / row["relative_path"]).exists()
    placeholder = env.root / ".operon" / "placeholders" / f"{row['file_id']}.json"
    payload = json.loads(placeholder.read_text(encoding="utf-8"))
    assert payload["file_id"] == row["file_id"]
    assert payload["remote"] == "mirror"
    assert payload["sha256"] == row["sha256"]
    status = env.db.conn.execute(
        "SELECT status FROM files WHERE file_id=?", (row["file_id"],)
    ).fetchone()["status"]
    assert status == "REMOTE_ONLY"


def test_evict_reports_a_failed_local_removal_and_withdraws_the_pointer(remote_env, monkeypatch):
    env = remote_env
    row = _add_directory_artifact(env)
    push(env.db, env.project, "mirror", [row["file_id"]])
    local = env.root / row["relative_path"]
    placeholder = env.root / ".operon" / "placeholders" / f"{row['file_id']}.json"

    def refuse_rmtree(path, *args, **kwargs):
        raise OSError("device busy")

    monkeypatch.setattr(remotes.shutil, "rmtree", refuse_rmtree)
    results = evict_local(env.db, env.project, "mirror", [row["file_id"]])
    assert results[0]["status"] == "error"
    assert "device busy" in results[0]["error"]
    assert local.is_dir()
    assert not placeholder.exists()
    status = env.db.conn.execute(
        "SELECT status FROM files WHERE file_id=?", (row["file_id"],)
    ).fetchone()["status"]
    assert status == "CHECKSUM_VERIFIED"
    run = env.db.conn.execute(
        "SELECT status FROM workflow_runs WHERE step='evict:mirror'"
    ).fetchone()
    assert run["status"] == "failed"


def test_evict_reports_a_metadata_schema_it_cannot_upgrade(remote_env):
    env = remote_env
    push(env.db, env.project, "mirror")
    env.project.schema_path.write_text(
        yaml.safe_dump({"schema_version": "1.2", "tables": {"files": {"fields": {}}}}),
        encoding="utf-8",
    )

    results = evict_local(env.db, env.project, "mirror", [env.record["file_id"]])
    assert results[0]["status"] == "error"
    assert "cannot upgrade project files status schema" in results[0]["error"]
    assert (env.root / env.record["relative_path"]).exists()
    placeholder = env.root / ".operon" / "placeholders" / f"{env.record['file_id']}.json"
    assert not placeholder.exists()
    location = env.db.conn.execute(
        "SELECT status FROM file_locations WHERE file_id=? AND location_name='mirror'",
        (env.record["file_id"],),
    ).fetchone()
    assert location["status"] == "AVAILABLE"


def test_mark_remote_location_rejects_an_unknown_file_id(remote_env):
    with pytest.raises(ValidationError, match="unknown file_id FIL_999999"):
        remotes._mark_remote_location(remote_env.db, "mirror", "FIL_999999", "CORRUPT")


def test_check_remote_reports_an_unreachable_root_and_cli_exits_one(remote_env, capsys):
    env = remote_env
    _configure_remote(env.project, env.root.parent / "missing-mirror", name="broken")

    result = check_remote(env.project, "broken")
    assert result["status"] == "error"
    assert result["files"] == ""
    assert result["error"].startswith("OSError") and "missing-mirror" in result["error"]

    assert main(["--project", str(env.root), "remotes"]) == 1
    out = capsys.readouterr().out
    assert "broken" in out and "error" in out


# --------------------------------------------------------------------------
# taxonomy: archive members, scalars, and taxdump import edges
# --------------------------------------------------------------------------

def _tar(path: Path, files: dict[str, str]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, text in files.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return path


def _jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8",
    )
    return path


_TAXONOMY_RECORDS = [
    {"taxId": 1, "rank": "no rank", "taxName": "root"},
    {"taxId": 10, "parents": [1], "rank": "family", "taxName": "Fam"},
    {"taxId": 11, "parents": [10], "rank": "genus", "taxName": "Gen"},
]


def test_archive_members_are_read_from_zip_and_tar(tmp_path):
    package = tmp_path / "dump.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("nested/nodes.dmp", "1 | 1 | no rank |\n")
    with taxonomy._archive_text_member(package, "nodes.dmp") as handle:
        assert handle.read().startswith("1")

    dump = _tar(tmp_path / "dump.tar.gz", {"taxonomy_report.jsonl": '{"taxId": 1}\n'})
    with taxonomy._taxonomy_text(dump) as handle:
        assert "taxId" in handle.read()

    missing = _tar(tmp_path / "missing.tar.gz", {"README": "x"})
    with pytest.raises(ValidationError, match="archive member nodes.dmp not found"):
        with taxonomy._archive_text_member(missing, "nodes.dmp"):
            pass


def test_next_snapshot_id_ignores_unrelated_identifiers(project_db):
    _project, db = project_db
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute(
        "INSERT INTO taxonomy_snapshots(taxonomy_snapshot_id, source, taxonomy_version, "
        "source_file_id, source_sha256, source_size_bytes, node_count, status, imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("LEGACY_DUMP", "NCBI", "v", "F", "sha", 1, 1, "READY", "now"),
    )
    assert taxonomy._next_snapshot_id(db) == "TAX_000001"


def test_taxids_accepts_a_scalar_value():
    assert taxonomy._taxids(5) == [5]
    assert taxonomy._taxids(["bad", 5]) == [5]


def test_taxdump_import_requires_both_nodes_and_names(project_db, tmp_path):
    _project, db = project_db
    package = tmp_path / "nodes-only.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("nodes.dmp", "1 | 1 | no rank |\n")
    with pytest.raises(ValidationError, match="requires nodes.dmp and names.dmp"):
        taxonomy._import_taxdump(db, "TAX_000001", package)


def test_taxdump_with_synonym_only_names_reports_a_missing_scientific_name(project_db, tmp_path):
    project, db = project_db
    package = _tar(tmp_path / "synonyms.tar.gz", {
        "nodes.dmp": "1 | 1 | no rank |\n",
        "names.dmp": "1 | all | | synonym |\n",
    })
    with pytest.raises(ValidationError, match="has no scientific name for TaxID 1"):
        taxonomy.import_ncbi_taxonomy(db, project, package, "synonyms-1")
    assert db.query("SELECT COUNT(*) AS n FROM taxonomy_snapshots")[0]["n"] == 0


def test_taxdump_with_empty_alias_files_imports_without_aliases(project_db, tmp_path):
    project, db = project_db
    package = _tar(tmp_path / "no-aliases.tar.gz", {
        "nodes.dmp": "1 | 1 | no rank |\n2 | 1 | genus |\n",
        "names.dmp": (
            "1 | root | | scientific name |\n2 | Genus | | scientific name |\n"
        ),
        "merged.dmp": "",
        "delnodes.dmp": "",
    })
    result = taxonomy.import_ncbi_taxonomy(db, project, package, "no-aliases-1")
    assert result["node_count"] == 2
    assert db.query(
        "SELECT COUNT(*) AS n FROM taxonomy_aliases WHERE taxonomy_snapshot_id=?",
        (result["taxonomy_snapshot_id"],),
    )[0]["n"] == 0


# --------------------------------------------------------------------------
# taxonomy: snapshot import identity and failures
# --------------------------------------------------------------------------

def test_import_rejects_a_directory_source(project_db, tmp_path):
    project, db = project_db
    with pytest.raises(ValidationError, match="must be a file"):
        taxonomy.import_ncbi_taxonomy(db, project, tmp_path, "dir-1")


def test_import_rejects_changed_bytes_for_an_existing_version(project_db, tmp_path):
    project, db = project_db
    source = _jsonl(tmp_path / "taxonomy.jsonl", _TAXONOMY_RECORDS)
    taxonomy.import_ncbi_taxonomy(db, project, source, "dup-1")
    source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(ConflictError, match="already refers to different bytes"):
        taxonomy.import_ncbi_taxonomy(db, project, source, "dup-1")
    assert db.query("SELECT COUNT(*) AS n FROM taxonomy_snapshots")[0]["n"] == 1


def test_import_of_identical_bytes_under_a_new_version_reuses_the_preserved_copy(
        project_db, tmp_path):
    project, db = project_db
    source = _jsonl(tmp_path / "taxonomy.jsonl", _TAXONOMY_RECORDS)
    first = taxonomy.import_ncbi_taxonomy(db, project, source, "v1")
    second = taxonomy.import_ncbi_taxonomy(db, project, source, "v2")

    assert second["reused"] is False
    assert second["taxonomy_snapshot_id"] != first["taxonomy_snapshot_id"]
    assert second["path"] == first["path"]
    assert second["node_count"] == first["node_count"] == 3
    archived = list((project.raw_root / "metadata" / "ncbi_taxonomy").iterdir())
    assert [path.name for path in archived] == [Path(first["path"]).name]


def test_import_rejects_an_empty_taxonomy_report(project_db, tmp_path):
    project, db = project_db
    source = tmp_path / "empty.jsonl"
    source.write_text("", encoding="utf-8")

    with pytest.raises(ValidationError, match="contains no records"):
        taxonomy.import_ncbi_taxonomy(db, project, source, "empty-1")
    assert db.query("SELECT COUNT(*) AS n FROM taxonomy_nodes")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM taxonomy_snapshots")[0]["n"] == 0
    run = db.query(
        "SELECT status, error FROM workflow_runs WHERE step='taxonomy_import'"
    )[0]
    assert run["status"] == "failed" and "contains no records" in run["error"]


def test_import_flushes_an_exact_batch_boundary_without_leftovers(project_db, tmp_path):
    project, db = project_db
    records = [{"taxId": 1, "rank": "no rank", "taxName": "root"}]
    records += [
        {"taxId": taxid, "parents": [1], "rank": "genus", "taxName": f"Genus {taxid}"}
        for taxid in range(2, 5001)
    ]
    assert len(records) == 5000
    source = _jsonl(tmp_path / "boundary.jsonl", records)

    result = taxonomy.import_ncbi_taxonomy(db, project, source, "boundary-1")
    assert result["node_count"] == 5000
    snapshot_id = result["taxonomy_snapshot_id"]
    assert db.query(
        "SELECT COUNT(*) AS n FROM taxonomy_nodes WHERE taxonomy_snapshot_id=?",
        (snapshot_id,),
    )[0]["n"] == 5000
    assert db.query(
        "SELECT scientific_name FROM taxonomy_nodes WHERE taxonomy_snapshot_id=? AND taxid=5000",
        (snapshot_id,),
    )[0]["scientific_name"] == "Genus 5000"


def test_import_failure_survives_broken_run_logging(project_db, tmp_path, monkeypatch):
    project, db = project_db
    source = _jsonl(tmp_path / "orphan.jsonl", [
        {"taxId": 1, "parentTaxId": 1, "rank": "no rank", "taxName": "root"},
        {"taxId": 2, "parentTaxId": 99, "rank": "genus", "taxName": "orphan"},
    ])

    def broken_log_run(*_args, **_kwargs):
        raise RuntimeError("audit database unavailable")

    monkeypatch.setattr(taxonomy, "log_run", broken_log_run)
    with pytest.raises(ValidationError, match="refers to missing parent"):
        taxonomy.import_ncbi_taxonomy(db, project, source, "logfail-1")
    assert db.query("SELECT COUNT(*) AS n FROM taxonomy_snapshots")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM files WHERE entity_type='taxonomy_snapshot'")[0]["n"] == 0


# --------------------------------------------------------------------------
# taxonomy: reference-set compilation, provenance, and listings
# --------------------------------------------------------------------------

def _write_profile(project, name: str, **overrides) -> dict:
    profile = {
        "kind": "taxonomy_coverage",
        "version": 1,
        "name": name,
        "taxonomy": {"source": "NCBI"},
        "scope": {"root_taxids": [1]},
        "targets": {"ranks": ["family", "genus"]},
        "thresholds": {
            "family": {"min_coverage_percent": 50},
            "genus": {"min_coverage_percent": 60},
        },
    }
    profile.update(overrides)
    (project.profiles_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(profile, sort_keys=False), encoding="utf-8",
    )
    return profile


def _compiled_reference_set(project, db, tmp_path, version: str = "comp-1"):
    source = _jsonl(tmp_path / "taxonomy.jsonl", _TAXONOMY_RECORDS)
    taxonomy.import_ncbi_taxonomy(db, project, source, version)
    _write_profile(project, "plants")
    return taxonomy.compile_reference_set(db, project, "plants", version)


def test_compile_reference_set_freezes_bytes_and_reuses_them(project_db, tmp_path):
    project, db = project_db
    first = _compiled_reference_set(project, db, tmp_path)

    assert first["reused"] is False
    assert (first["family_count"], first["genus_count"]) == (1, 1)
    tsv = Path(first["path"])
    lines = tsv.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "rank\ttaxid\tscientific_name"
    assert [line.split("\t") for line in lines[1:]] == [
        ["family", "10", "Fam"],
        ["genus", "11", "Gen"],
    ]
    assert first["tsv_size_bytes"] == tsv.stat().st_size
    assert first["tsv_sha256"] == hashlib.sha256(tsv.read_bytes()).hexdigest()

    sidecar = Path(first["provenance_path"])
    provenance = json.loads(sidecar.read_text(encoding="utf-8"))
    assert provenance["reference_set_id"] == "plants@comp-1"
    assert provenance["profile_sha256"] == first["profile_sha256"]
    assert provenance["taxonomy_source_sha256"]
    assert provenance["root_taxids"] == [1]
    assert provenance["target_ranks"] == ["family", "genus"]
    assert provenance["row_counts"] == {"family": 1, "genus": 1}
    assert provenance["compiler"] == "operon.taxonomy"
    assert provenance["compiler_version"]

    second = taxonomy.compile_reference_set(db, project, "plants", "comp-1")
    assert second["reused"] is True
    assert second["tsv_sha256"] == first["tsv_sha256"]
    assert db.query("SELECT COUNT(*) AS n FROM taxonomy_reference_sets")[0]["n"] == 1
    assert db.query(
        "SELECT COUNT(*) AS n FROM changes WHERE object_type='taxonomy_reference_set'"
    )[0]["n"] == 1
    runs = db.query("SELECT status FROM workflow_runs WHERE step='taxonomy_compile'")
    assert [row["status"] for row in runs] == ["completed"]

    snapshots = taxonomy.list_taxonomy_snapshots(db)
    assert [row["taxonomy_version"] for row in snapshots] == ["comp-1"]
    assert snapshots[0]["status"] == "READY" and snapshots[0]["node_count"] == 3
    reference_sets = taxonomy.list_reference_sets(db)
    assert [row["reference_set_id"] for row in reference_sets] == ["plants@comp-1"]
    assert reference_sets[0]["family_count"] == 1
    assert reference_sets[0]["tsv_sha256"] == first["tsv_sha256"]


def test_compile_reference_set_rejects_tampered_provenance_and_bytes(project_db, tmp_path):
    project, db = project_db
    first = _compiled_reference_set(project, db, tmp_path)
    tsv = Path(first["path"])
    sidecar = Path(first["provenance_path"])
    original = sidecar.read_text(encoding="utf-8")
    failures = 0

    sidecar.unlink()
    with pytest.raises(ConflictError, match="provenance is missing"):
        taxonomy.compile_reference_set(db, project, "plants", "comp-1")
    failures += 1

    sidecar.write_text("not json", encoding="utf-8")
    with pytest.raises(ConflictError, match="provenance is invalid"):
        taxonomy.compile_reference_set(db, project, "plants", "comp-1")
    failures += 1

    sidecar.write_text(
        original.replace(first["tsv_sha256"], "0" * 64), encoding="utf-8",
    )
    with pytest.raises(ConflictError, match="does not match frozen identity.*tsv_sha256"):
        taxonomy.compile_reference_set(db, project, "plants", "comp-1")
    failures += 1

    sidecar.write_text(original, encoding="utf-8")
    tsv_payload = tsv.read_text(encoding="utf-8")
    tsv.unlink()
    with pytest.raises(ConflictError, match="different profile, taxonomy or bytes"):
        taxonomy.compile_reference_set(db, project, "plants", "comp-1")
    failures += 1

    tsv.write_text(tsv_payload, encoding="utf-8")
    _write_profile(project, "plants", version=2)
    with pytest.raises(ConflictError, match="different profile, taxonomy or bytes"):
        taxonomy.compile_reference_set(db, project, "plants", "comp-1")
    failures += 1

    failed = db.query(
        "SELECT COUNT(*) AS n FROM workflow_runs "
        "WHERE step='taxonomy_compile' AND status='failed'"
    )[0]["n"]
    assert failed == failures


def test_compile_reference_set_requires_a_ready_snapshot(project_db):
    project, db = project_db
    _write_profile(project, "plants")

    with pytest.raises(ValidationError, match="ready NCBI taxonomy snapshot 'missing' not found"):
        taxonomy.compile_reference_set(db, project, "plants", "missing")
    run = db.query(
        "SELECT status, error FROM workflow_runs WHERE step='taxonomy_compile'"
    )[0]
    assert run["status"] == "failed" and "not found" in run["error"]
    assert db.query("SELECT COUNT(*) AS n FROM taxonomy_reference_sets")[0]["n"] == 0


def test_compile_reference_set_rebuilds_a_lost_index_from_frozen_bytes(project_db, tmp_path):
    project, db = project_db
    first = _compiled_reference_set(project, db, tmp_path)
    tsv = Path(first["path"])
    frozen = tsv.read_bytes()
    db.conn.execute("DELETE FROM taxonomy_reference_sets")
    db.conn.commit()

    rebuilt = taxonomy.compile_reference_set(db, project, "plants", "comp-1")
    assert rebuilt["reused"] is False
    assert rebuilt["tsv_sha256"] == first["tsv_sha256"]
    assert tsv.read_bytes() == frozen
    assert db.query("SELECT COUNT(*) AS n FROM taxonomy_reference_sets")[0]["n"] == 1
    assert db.query(
        "SELECT relative_path FROM taxonomy_reference_sets"
    )[0]["relative_path"] == project_rel(project, tsv)


def test_compile_reference_set_applies_name_exclusions(project_db, tmp_path):
    project, db = project_db
    records = _TAXONOMY_RECORDS + [
        {"taxId": 12, "parents": [10], "rank": "genus", "taxName": "Unclassified genus"},
    ]
    source = _jsonl(tmp_path / "taxonomy.jsonl", records)
    taxonomy.import_ncbi_taxonomy(db, project, source, "excl-1")
    _write_profile(project, "plants", filters={"exclude_name_patterns": ["Unclassified"]})

    result = taxonomy.compile_reference_set(db, project, "plants", "excl-1")
    rows = [line.split("\t") for line in Path(result["path"]).read_text().splitlines()[1:]]
    assert [row[1] for row in rows] == ["10", "11"]
    assert result["genus_count"] == 1


def test_compile_reference_set_rejects_an_empty_rank(project_db, tmp_path):
    project, db = project_db
    source = _jsonl(tmp_path / "taxonomy.jsonl", _TAXONOMY_RECORDS + [
        {"taxId": 12, "parents": [10], "rank": "genus", "taxName": "Other genus"},
    ])
    taxonomy.import_ncbi_taxonomy(db, project, source, "empty-rank-1")
    _write_profile(project, "plants", filters={"exclude_name_patterns": ["Gen", "genus"]})

    with pytest.raises(ValidationError, match=r"empty for rank\(s\): genus"):
        taxonomy.compile_reference_set(db, project, "plants", "empty-rank-1")
    assert not (project.taxonomy_reference_sets_dir / "plants@empty-rank-1.tsv").exists()


# --------------------------------------------------------------------------
# timetree: cache-less operation and response-shape edges
# --------------------------------------------------------------------------

def test_client_without_a_cache_directory_always_uses_the_network(tmp_path):
    pairwise_url = f"{TIMETREE_BASE}/pairwise/3702/9606/summaryjson"
    timeline_url = f"{TIMETREE_BASE}/timeline/3702"
    session = make_session({
        pairwise_url: json.dumps(SUMMARY_3702_9606),
        timeline_url: "node,node_name,adjusted_age\n1,cellular organisms,4200\n",
    })
    client = TimeTreeClient(session=session, delay=0)
    assert client.cache_dir is None

    first = client.pairwise(3702, 9606)
    second = client.pairwise(3702, 9606)
    assert first["from_cache"] is False and second["from_cache"] is False
    assert first["cache_file"] is None and second["cache_file"] is None
    assert first["queried_at"]

    rows = client.timeline(3702)
    assert rows[0]["_cache_file"] == ""
    assert rows[0]["_queried_at"]
    assert session.calls == [pairwise_url, pairwise_url, timeline_url]


def test_pairwise_skips_non_numeric_age_candidates(tmp_path):
    body = {
        "precomputed_age": "unknown",
        "median_time": "1496.0",
        "ci_low": 1350.0,
        "ci_high": 1650.0,
        "all_total": 42,
    }
    client, _ = make_client(tmp_path, {
        f"{TIMETREE_BASE}/pairwise/1/2/summaryjson": json.dumps(body),
    })
    result = client.pairwise(1, 2)
    assert result["age_median"] == 1496.0
    assert result["ci_low"] == 1350.0 and result["ci_high"] == 1650.0
    assert result["study_count"] == 42


def test_resolve_taxon_skips_zero_and_duplicate_ids(tmp_path):
    payload = [
        {"taxon_id": 0, "scientific_name": "Zero"},
        {"taxon_id": 3702, "scientific_name": "Arabidopsis thaliana", "rank": "species"},
        {"taxon_id": 3702, "scientific_name": "Duplicate"},
    ]
    client, _ = make_client(tmp_path, {
        f"{TIMETREE_BASE}/taxon/Apis": json.dumps(payload),
    })
    assert client.resolve_taxon("Apis") == [
        {"taxon_id": 3702, "scientific_name": "Arabidopsis thaliana", "rank": "species"},
    ]


def test_timeline_rejects_a_non_positive_id(tmp_path):
    client, session = make_client(tmp_path, {})
    with pytest.raises(ValidationError, match="positive NCBI ID"):
        client.timeline(0)
    assert session.calls == []


def test_build_calibrations_requires_two_taxa(tmp_path):
    client, session = make_client(tmp_path, {})
    with pytest.raises(ValidationError, match="at least two taxa"):
        client.build_calibrations([("Arabidopsis thaliana", 3702)])
    assert session.calls == []


# --------------------------------------------------------------------------
# ncbi reconciliation: plan edges
# --------------------------------------------------------------------------

def _seed_taxonomy(db) -> None:
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "O"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})


def _seed_assembly(db, assembly_id: str = "ASM_000001", **overrides) -> None:
    row = {
        "assembly_id": assembly_id, "sample_id": "SMP_000001",
        "assembly_level": "contig", "assembly_version": 1,
        "assembly_accession": None, "source_database": None, "fasta_file_id": None,
    }
    row.update(overrides)
    db.insert_row("assemblies", row)


def _file(file_id, entity_type, entity_id, role, rel, sha, source_url="", size=1):
    return {
        "file_id": file_id, "entity_type": entity_type, "entity_id": entity_id,
        "file_role": role, "format": "fasta", "compression": "none",
        "relative_path": rel, "source_url": source_url, "size_bytes": size,
        "sha256": sha, "status": "CHECKSUM_VERIFIED",
    }


def _seed_accessions(db, assembly_id: str, rows) -> None:
    for accession, namespace, primary in rows:
        db.insert_row("accessions", {
            "internal_type": "assembly", "internal_id": assembly_id,
            "namespace": namespace, "accession": accession, "is_primary": primary,
        })


def _plan_with(repairs=None) -> dict:
    return {
        "warnings": [], "annotation_supersessions": [], "assembly_updates": [],
        "file_role_updates": [], "file_path_repairs": repairs or [],
        "accession_primary_updates": [], "state_restorations": [], "summary": {},
    }


def test_plan_supersedes_duplicates_sharing_two_identical_roles(project_db):
    _project, db = project_db
    _seed_taxonomy(db)
    _seed_assembly(db)
    for annotation_id in ("ANN_000001", "ANN_000002"):
        db.insert_row("annotations", {
            "annotation_id": annotation_id, "assembly_id": "ASM_000001",
            "annotation_source": "NCBI", "annotation_version": 1,
        })
    for file_id, annotation_id, role, sha in (
        ("FIL_000001", "ANN_000001", "annotation_gff3", "a" * 64),
        ("FIL_000002", "ANN_000002", "annotation_gff3", "a" * 64),
        ("FIL_000003", "ANN_000001", "protein_fasta", "b" * 64),
        ("FIL_000004", "ANN_000002", "protein_fasta", "b" * 64),
    ):
        db.insert_row("files", _file(
            file_id, "annotation", annotation_id, role, f"raw/{file_id}", sha,
        ))

    plan = ncbi_reconcile.plan_ncbi_reconciliation(db)
    assert plan["warnings"] == []
    assert [(item["annotation_id"], item["superseded_by"])
            for item in plan["annotation_supersessions"]] == [("ANN_000002", "ANN_000001")]
    assert plan["annotation_supersessions"][0]["identity"] == {
        "assembly_id": "ASM_000001", "provider": "ncbi", "version": 1, "date": "",
    }


def test_plan_ignores_annotations_already_recorded_as_superseded(project_db):
    _project, db = project_db
    _seed_taxonomy(db)
    _seed_assembly(db)
    for annotation_id in ("ANN_000001", "ANN_000002"):
        db.insert_row("annotations", {
            "annotation_id": annotation_id, "assembly_id": "ASM_000001",
            "annotation_source": "NCBI", "annotation_version": 1,
        })
        db.insert_row("files", _file(
            f"FIL_00000{annotation_id[-1]}", "annotation", annotation_id,
            "annotation_gff3", f"raw/{annotation_id}", "a" * 64,
        ))
    assert db.supersede_entity(
        "annotation", "ANN_000002", "annotation", "ANN_000001",
        reason="earlier repair", workflow_run_id="RUN_000001",
    ) is True

    plan = ncbi_reconcile.plan_ncbi_reconciliation(db)
    assert plan["annotation_supersessions"] == []
    assert plan["warnings"] == []
    assert plan["summary"]["annotation_supersessions"] == 0


def test_plan_leaves_aligned_and_single_namespace_assemblies_alone(project_db):
    _project, db = project_db
    _seed_taxonomy(db)
    _seed_assembly(
        db, "ASM_000010", assembly_accession="GCF_000000010.1",
        source_database="RefSeq", fasta_file_id=None,
    )
    _seed_accessions(db, "ASM_000010", (
        ("GCF_000000010.1", "NCBI_RefSeq_Assembly", 1),
        ("GCA_000000010.1", "NCBI_GenBank_Assembly", 0),
        ("GCF_000000010.1", "NCBI_Assembly", 1),
        ("GCA_000000010.1", "NCBI_Assembly", 0),
    ))
    db.insert_row("files", _file(
        "FIL_000010", "assembly", "ASM_000010", "genome_fasta", "raw/aligned.fa", "c" * 64,
    ))
    # A renamed role that already sits at its canonical path needs no repair;
    # the misnamed sibling still does.
    from operon.files import canonical_filename

    canonical = canonical_filename("ASM_000010", "genome_fasta_genbank", "fasta", "none")
    db.insert_row("files", _file(
        "FIL_000011", "assembly", "ASM_000010", "genome_fasta_genbank",
        f"raw/{canonical}", "d" * 64,
    ))
    db.insert_row("files", _file(
        "FIL_000012", "assembly", "ASM_000010", "assembly_report_genbank",
        "raw/wrong-report.txt", "e" * 64,
    ))
    # A GCA-only assembly has no paired namespaces to reconcile.
    _seed_assembly(db, "ASM_000020", assembly_accession="GCA_000000020.1",
                   source_database="GenBank")
    _seed_accessions(db, "ASM_000020", (
        ("GCA_000000020.1", "NCBI_GenBank_Assembly", 1),
    ))

    plan = ncbi_reconcile.plan_ncbi_reconciliation(db)
    assert plan["assembly_updates"] == []
    assert plan["file_role_updates"] == []
    assert plan["accession_primary_updates"] == []
    assert [item["file_id"] for item in plan["file_path_repairs"]] == ["FIL_000012"]
    assert plan["file_path_repairs"][0]["new_relative_path"] == (
        "raw/" + canonical_filename("ASM_000010", "assembly_report_genbank", "fasta", "none")
    )
    assert plan["warnings"] == []


def test_plan_renames_source_specific_roles_and_paths(project_db):
    _project, db = project_db
    _seed_taxonomy(db)
    _seed_assembly(
        db, "ASM_000030", assembly_accession="GCA_000000030.1",
        source_database="GenBank", fasta_file_id="FIL_000031",
    )
    _seed_accessions(db, "ASM_000030", (
        ("GCF_000000030.1", "NCBI_RefSeq_Assembly", 0),
        ("GCA_000000030.1", "NCBI_GenBank_Assembly", 1),
    ))
    db.insert_row("files", _file(
        "FIL_000030", "assembly", "ASM_000030", "genome_fasta", "raw/refseq.fna", "f" * 64,
        "https://example.org/GCF_000000030.1/genome.fna",
    ))
    db.insert_row("files", _file(
        "FIL_000031", "assembly", "ASM_000030", "genome_fasta", "raw/plain.fna", "0" * 64,
        "https://example.org/GCA_000000030.1/genome.fna",
    ))

    plan = ncbi_reconcile.plan_ncbi_reconciliation(db)
    assert plan["assembly_updates"] == [{
        "assembly_id": "ASM_000030",
        "old_accession": "GCA_000000030.1",
        "new_accession": "GCF_000000030.1",
        "old_source_database": "GenBank",
        "new_source_database": "RefSeq",
    }]
    assert plan["file_role_updates"] == [{
        "file_id": "FIL_000031", "assembly_id": "ASM_000030",
        "old_role": "genome_fasta", "new_role": "genome_fasta_genbank",
        "source_accession": "GCA_000000030.1",
        "old_relative_path": "raw/plain.fna",
        "new_relative_path": "raw/ASM_000030.genome_fasta_genbank.fasta",
        "clear_fasta_link": True,
    }]
    assert plan["warnings"] == []


def test_plan_restores_only_annotations_stuck_in_early_states(project_db):
    _project, db = project_db
    _seed_taxonomy(db)
    _seed_assembly(db)
    for annotation_id, state, version in (
        ("ANN_000040", "QC_COMPLETE", 1), ("ANN_000041", "DOWNLOADED", 2),
    ):
        db.insert_row("annotations", {
            "annotation_id": annotation_id, "assembly_id": "ASM_000001",
            "annotation_source": "NCBI", "annotation_version": version,
        })
        db.insert_qc_result({
            "entity_type": "annotation", "entity_id": annotation_id, "qc_stage": "s",
            "metric_name": "m", "metric_value": "1", "metric_numeric": 1,
            "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
        })
        db.set_entity_state("annotation", annotation_id, state, "recorded")

    plan = ncbi_reconcile.plan_ncbi_reconciliation(db)
    assert plan["state_restorations"] == [{
        "annotation_id": "ANN_000041", "old_state": "DOWNLOADED", "new_state": "QC_COMPLETE",
    }]


# --------------------------------------------------------------------------
# ncbi reconciliation: apply
# --------------------------------------------------------------------------

def _seed_apply_scenario(db, project) -> None:
    """One database exercising every repair kind at once."""
    _seed_taxonomy(db)
    _seed_assembly(
        db, "ASM_000001", assembly_accession="GCA_000000001.1",
        source_database="GenBank", fasta_file_id="FIL_000020",
    )
    _seed_accessions(db, "ASM_000001", (
        ("GCF_000000001.1", "NCBI_RefSeq_Assembly", 0),
        ("GCA_000000001.1", "NCBI_GenBank_Assembly", 1),
        ("GCF_000000001.1", "NCBI_Assembly", 0),
        ("GCA_000000001.1", "NCBI_Assembly", 1),
    ))
    for annotation_id in ("ANN_000001", "ANN_000002"):
        db.insert_row("annotations", {
            "annotation_id": annotation_id, "assembly_id": "ASM_000001",
            "annotation_source": "NCBI", "annotation_version": 1,
        })
    db.insert_row("annotations", {
        "annotation_id": "ANN_000005", "assembly_id": "ASM_000001",
        "annotation_source": "NCBI", "annotation_version": 2,
    })
    # A second duplicate group with no artifacts at all.
    for annotation_id in ("ANN_000006", "ANN_000007"):
        db.insert_row("annotations", {
            "annotation_id": annotation_id, "assembly_id": "ASM_000001",
            "annotation_source": "NCBI", "annotation_version": 3,
        })
    # Identical duplicate annotation artifacts: ANN_000002 is superseded.  The
    # first file carries a historical RefSeq URL, which makes GCF canonical for
    # the paired GCA/GCF accessions below.
    for file_id, annotation_id, role, sha, url in (
        ("FIL_000001", "ANN_000001", "annotation_gff3", "a" * 64,
         "https://example.org/GCF_000000001.1/annotation.gff3"),
        ("FIL_000002", "ANN_000002", "annotation_gff3", "a" * 64, ""),
        ("FIL_000003", "ANN_000001", "protein_fasta", "b" * 64, ""),
        ("FIL_000004", "ANN_000002", "protein_fasta", "b" * 64, ""),
    ):
        db.insert_row("files", _file(
            file_id, "annotation", annotation_id, role, f"raw/{file_id}", sha, url,
        ))
    db.insert_row("files", _file(
        "FIL_000020", "assembly", "ASM_000001", "genome_fasta", "raw/old.fna",
        "e" * 64, "https://example.org/GCA_000000001.1/genome.fna",
    ))
    # A GenBank report is renamed without clearing the assembly FASTA pointer.
    db.insert_row("files", _file(
        "FIL_000021", "assembly", "ASM_000001", "assembly_report", "raw/report.txt",
        "1" * 64, "https://example.org/GCA_000000001.1/report.txt",
    ))
    db.insert_qc_result({
        "entity_type": "annotation", "entity_id": "ANN_000005", "qc_stage": "s",
        "metric_name": "m", "metric_value": "1", "metric_numeric": 1,
        "tool": "t", "tool_version": "1", "parameter_set": "p", "evaluated_at": "now",
    })
    db.set_entity_state("annotation", "ANN_000005", "DOWNLOADED", "downgraded by re-import")
    for name, payload in (("old.fna", "genome bytes"), ("report.txt", "report bytes")):
        artifact = project.root / "raw" / name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(payload, encoding="utf-8")


def test_apply_reconciliation_persists_every_repair_with_audit_rows(project_db):
    project, db = project_db
    _seed_apply_scenario(db, project)

    result = ncbi_reconcile.apply_ncbi_reconciliation(db, project, actor="tester")
    assert result["skipped_path_moves"] == []
    assert result["plan_sha256"]
    assert result["summary"]["annotation_supersessions"] == 2
    assert result["summary"]["assembly_updates"] == 1
    assert result["summary"]["file_role_updates"] == 2
    assert result["summary"]["file_path_repairs"] == 0
    assert result["summary"]["accession_primary_updates"] == 2
    assert result["summary"]["state_restorations"] == 1

    supersessions = db.query(
        "SELECT object_id, superseded_by_id, workflow_run_id FROM entity_supersessions "
        "WHERE object_type='annotation' ORDER BY object_id"
    )
    assert [(row["object_id"], row["superseded_by_id"]) for row in supersessions] == [
        ("ANN_000002", "ANN_000001"), ("ANN_000007", "ANN_000006"),
    ]
    assert {row["workflow_run_id"] for row in supersessions} == {result["run_id"]}

    assembly = db.query(
        "SELECT assembly_accession, source_database, fasta_file_id FROM assemblies "
        "WHERE assembly_id='ASM_000001'"
    )[0]
    assert tuple(assembly) == ("GCF_000000001.1", "RefSeq", None)

    renamed = db.query(
        "SELECT file_role, relative_path FROM files WHERE file_id='FIL_000020'"
    )[0]
    assert tuple(renamed) == (
        "genome_fasta_genbank", "raw/ASM_000001.genome_fasta_genbank.fasta",
    )
    assert (project.root / renamed["relative_path"]).read_text(encoding="utf-8") == "genome bytes"
    assert not (project.root / "raw" / "old.fna").exists()
    report = db.query(
        "SELECT file_role, relative_path FROM files WHERE file_id='FIL_000021'"
    )[0]
    assert tuple(report) == (
        "assembly_report_genbank", "raw/ASM_000001.assembly_report_genbank.fasta",
    )
    assert (project.root / report["relative_path"]).read_text(encoding="utf-8") == "report bytes"

    primaries = {
        row["accession"]: row["is_primary"]
        for row in db.query(
            "SELECT accession, is_primary FROM accessions WHERE namespace='NCBI_Assembly'"
        )
    }
    assert primaries == {"GCF_000000001.1": 1, "GCA_000000001.1": 0}

    state = db.query(
        "SELECT state, message FROM entity_state "
        "WHERE entity_type='annotation' AND entity_id='ANN_000005'"
    )[0]
    assert state["state"] == "QC_COMPLETE"
    assert "restored from existing QC evidence" in state["message"]

    run = db.query(
        "SELECT status, exit_code, output_sha256 FROM workflow_runs WHERE run_id=?",
        (result["run_id"],),
    )[0]
    assert tuple(run) == ("completed", 0, result["plan_sha256"])
    assert db.query(
        "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='ncbi_datasets_reconcile'"
    )[0]["n"] == 1

    changes = {
        (row["object_type"], row["object_id"], row["field"])
        for row in db.query(
            "SELECT object_type, object_id, field FROM changes WHERE workflow_run_id=?",
            (result["run_id"],),
        )
    }
    assert ("annotation", "ANN_000002", "superseded_by") in changes
    assert ("annotation", "ANN_000007", "superseded_by") in changes
    assert ("assemblies", "ASM_000001", "assembly_accession") in changes
    assert ("assemblies", "ASM_000001", "source_database") in changes
    assert ("files", "FIL_000020", "file_role") in changes
    assert ("files", "FIL_000020", "relative_path") in changes
    assert ("files", "FIL_000021", "file_role") in changes
    assert ("files", "FIL_000021", "relative_path") in changes
    assert ("accessions", "NCBI_Assembly:GCA_000000001.1", "is_primary") in changes
    assert ("accessions", "NCBI_Assembly:GCF_000000001.1", "is_primary") in changes
    assert ("entity_state", "annotation:ANN_000005", "state") in changes
    assert ("adapter_repair", result["run_id"], None) in changes
    repair = db.query(
        "SELECT new_value FROM changes WHERE object_type='adapter_repair' AND object_id=?",
        (result["run_id"],),
    )[0]
    assert json.loads(repair["new_value"]) == result["summary"]

    # A second apply is a no-op: every repair is idempotent.
    second = ncbi_reconcile.apply_ncbi_reconciliation(db, project, actor="tester")
    assert second["run_id"] != result["run_id"]
    assert set(second["summary"].values()) == {0}
    assert db.query(
        "SELECT COUNT(*) AS n FROM changes WHERE workflow_run_id=?",
        (second["run_id"],),
    )[0]["n"] == 1
    assert db.supersede_entity(
        "annotation", "ANN_000002", "annotation", "ANN_000001", reason="duplicate",
    ) is False


def test_apply_reconciliation_tolerates_equal_path_moves(project_db, monkeypatch):
    project, db = project_db
    (project.root / "raw").mkdir(exist_ok=True)
    (project.root / "raw" / "same.fa").write_text("same", encoding="utf-8")
    (project.root / "raw" / "other.fa").write_text("other", encoding="utf-8")
    db.insert_row("files", _file(
        "FIL_000001", "organism", "ORG_000001", "other", "raw/same.fa", "a" * 64,
    ))
    db.insert_row("files", _file(
        "FIL_000002", "organism", "ORG_000001", "other", "raw/other.fa", "b" * 64,
    ))
    monkeypatch.setattr(ncbi_reconcile, "plan_ncbi_reconciliation", lambda _db: _plan_with([
        {"file_id": "FIL_000001", "old_relative_path": "raw/same.fa",
         "new_relative_path": "raw/same.fa"},
        {"file_id": "FIL_000002", "old_relative_path": "raw/other.fa",
         "new_relative_path": "raw/moved.fa"},
    ]))

    result = ncbi_reconcile.apply_ncbi_reconciliation(db, project, actor="tester")
    assert result["skipped_path_moves"] == []
    assert (project.root / "raw" / "same.fa").read_text(encoding="utf-8") == "same"
    assert (project.root / "raw" / "moved.fa").read_text(encoding="utf-8") == "other"
    assert db.query(
        "SELECT relative_path FROM files WHERE file_id='FIL_000002'"
    )[0]["relative_path"] == "raw/moved.fa"
    assert db.query(
        "SELECT status FROM workflow_runs WHERE run_id=?", (result["run_id"],)
    )[0]["status"] == "completed"


def test_apply_reconciliation_is_idempotent_for_a_replayed_supersession(project_db, monkeypatch):
    project, db = project_db
    identity = {"assembly_id": "ASM_000001", "provider": "ncbi", "version": 1, "date": ""}
    assert db.supersede_entity(
        "annotation", "ANN_000002", "annotation", "ANN_000001",
        reason="recorded by an earlier run",
    ) is True
    plan = _plan_with()
    plan["annotation_supersessions"] = [
        {"annotation_id": "ANN_000002", "superseded_by": "ANN_000001", "identity": identity},
        {"annotation_id": "ANN_000003", "superseded_by": "ANN_000001", "identity": identity},
    ]
    monkeypatch.setattr(ncbi_reconcile, "plan_ncbi_reconciliation", lambda _db: plan)

    result = ncbi_reconcile.apply_ncbi_reconciliation(db, project, actor="tester")
    recorded = db.query(
        "SELECT object_id FROM changes WHERE workflow_run_id=? AND field='superseded_by' "
        "ORDER BY object_id",
        (result["run_id"],),
    )
    # The replayed entry writes no duplicate audit row; the new one is recorded.
    assert [row["object_id"] for row in recorded] == ["ANN_000003"]
    assert db.query(
        "SELECT COUNT(*) AS n FROM changes WHERE object_id='ANN_000002' "
        "AND field='superseded_by'"
    )[0]["n"] == 0
    assert db.query(
        "SELECT reason FROM entity_supersessions WHERE object_id='ANN_000002'"
    )[0]["reason"] == "recorded by an earlier run"
    assert db.query("SELECT COUNT(*) AS n FROM entity_supersessions")[0]["n"] == 2
    assert db.query(
        "SELECT status FROM workflow_runs WHERE run_id=?", (result["run_id"],)
    )[0]["status"] == "completed"


def test_apply_reconciliation_skips_redundant_accession_primary_updates(project_db, monkeypatch):
    project, db = project_db
    _seed_accessions(db, "ASM_000009", (
        ("GCF_000000009.1", "NCBI_Assembly", 1),
    ))
    plan = _plan_with()
    plan["accession_primary_updates"] = [
        # Already at the desired primacy.
        {"namespace": "NCBI_Assembly", "accession": "GCF_000000009.1", "is_primary": 1},
        # No matching accession row at all.
        {"namespace": "NCBI_Assembly", "accession": "GCA_000000009.1", "is_primary": 0},
    ]
    monkeypatch.setattr(ncbi_reconcile, "plan_ncbi_reconciliation", lambda _db: plan)

    result = ncbi_reconcile.apply_ncbi_reconciliation(db, project, actor="tester")
    assert db.query(
        "SELECT is_primary FROM accessions WHERE accession='GCF_000000009.1'"
    )[0]["is_primary"] == 1
    assert db.query(
        "SELECT COUNT(*) AS n FROM accessions WHERE accession='GCA_000000009.1'"
    )[0]["n"] == 0
    assert db.query(
        "SELECT COUNT(*) AS n FROM changes WHERE workflow_run_id=? AND object_type='accessions'",
        (result["run_id"],),
    )[0]["n"] == 0
    assert db.query(
        "SELECT status FROM workflow_runs WHERE run_id=?", (result["run_id"],)
    )[0]["status"] == "completed"


def test_apply_reconciliation_blocks_on_alternate_role_conflicts(project_db, monkeypatch):
    project, db = project_db
    plan = _plan_with()
    plan["warnings"] = [
        {"kind": "alternate_role_conflict", "file_id": "FIL_000001",
         "existing_file_id": "FIL_000002", "role": "genome_fasta_genbank"},
    ]
    monkeypatch.setattr(ncbi_reconcile, "plan_ncbi_reconciliation", lambda _db: plan)

    with pytest.raises(ConflictError, match="alternate-role byte conflicts"):
        ncbi_reconcile.apply_ncbi_reconciliation(db, project, actor="tester")
    assert db.query(
        "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='ncbi_datasets_reconcile'"
    )[0]["n"] == 0
