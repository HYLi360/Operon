"""Configuration, host-key, path, and SFTP primitive edge cases."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from operon import remotes
from operon.errors import ConfigError, ConflictError, RemoteError, ValidationError
from operon.utils import path_size_bytes, sha256_path
from tests.unit.test_execution import FakeSFTP, FakeSSHClient


def project(tmp_path: Path, configured):
    return SimpleNamespace(root=tmp_path, config={"remotes": configured})


def test_remote_config_validation_and_address(tmp_path):
    with pytest.raises(ValidationError, match="must be a mapping"):
        remotes.list_remotes(SimpleNamespace(config={"remotes": [1]}))
    cases = [
        ({}, "missing", "unknown remote"),
        ({"r": []}, "r", "must be a mapping"),
        ({"r": {"type": "http"}}, "r", "unsupported type"),
        ({"r": {"root": "/r"}}, "r", "host.*required"),
        ({"r": {"host": "h"}}, "r", "root.*required"),
        ({"r": {"host": "h", "root": "relative"}}, "r", "absolute POSIX"),
    ]
    for config, name, message in cases:
        with pytest.raises(ValidationError, match=message):
            remotes.get_remote(project(tmp_path, config), name)
    spec = remotes.get_remote(project(tmp_path, {"r": {
        "host": "host", "root": "/root", "user": "user", "port": "2222",
        "connect_timeout": "5", "insecure_accept_unknown_host": True,
    }}), "r")
    assert spec.address == "user@host:2222"
    assert spec.connect_timeout == 5


@pytest.mark.parametrize(
    "value",
    ["", " ", "/absolute", "a//b", "a/./b", "a/../b", "../escape", 1],
)
def test_relative_path_validation_rejects_unsafe_values(value):
    with pytest.raises(ValidationError):
        remotes.validate_relative_path(value)


def test_relative_and_local_paths_handle_backslashes_and_symlink_escape(tmp_path):
    assert remotes.validate_relative_path("a\\b") == "a/b"
    root = tmp_path / "project"
    root.mkdir()
    assert remotes.local_artifact_path(SimpleNamespace(root=root), "a/b") == root / "a" / "b"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValidationError, match="escapes project root"):
        remotes.local_artifact_path(SimpleNamespace(root=root), "link/file")


def test_fingerprint_normalization():
    assert remotes._normalize_host_key_fingerprint(" SHA256:abc== ") == "abc"


def test_connect_ssh_known_hosts_pinning_and_errors(monkeypatch):
    class Policy:
        pass

    class Key:
        def __init__(self, data=b"key"):
            self.data = data

        def asbytes(self):
            return self.data

        def get_name(self):
            return "ssh-ed25519"

    expected = remotes._host_key_fingerprint(Key())

    class HostKeys:
        def __init__(self):
            self.added = []

        def add(self, *args):
            self.added.append(args)

    class Client:
        def __init__(self):
            self.closed = False
            self.policy = None
            self.host_keys = HostKeys()

        def load_system_host_keys(self):
            pass

        def load_host_keys(self, path):
            if path == "bad":
                raise OSError("bad")

        def set_missing_host_key_policy(self, policy):
            self.policy = policy

        def connect(self, **kwargs):
            self.kwargs = kwargs

        def get_transport(self):
            return SimpleNamespace(get_remote_server_key=lambda: Key())

        def get_host_keys(self):
            return self.host_keys

        def close(self):
            self.closed = True

    clients = []

    def make_client():
        client = Client()
        clients.append(client)
        return client

    fake = SimpleNamespace(
        SSHClient=make_client,
        MissingHostKeyPolicy=Policy,
        AutoAddPolicy=lambda: "auto",
        RejectPolicy=lambda: "reject",
        SSHException=RuntimeError,
    )
    monkeypatch.setattr(remotes, "import_paramiko", lambda: fake)
    client = remotes.connect_ssh(
        "host", user="user", port=2222, key_file="key", host_key_sha256=f"SHA256:{expected}"
    )
    assert client.kwargs["username"] == "user" and client.kwargs["key_filename"] == "key"
    client.policy.missing_host_key(client, "host", Key())
    assert client.host_keys.added
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        client.policy.missing_host_key(client, "host", Key(b"other"))

    with pytest.raises(ConfigError, match="cannot load SSH known-hosts"):
        remotes.connect_ssh("host", known_hosts="bad")

    class BadClient(Client):
        def connect(self, **kwargs):
            raise OSError("connect")

    fake.SSHClient = BadClient
    with pytest.raises(RemoteError, match="cannot connect"):
        remotes.connect_ssh("host")


def test_sftp_makedirs_and_not_found_helpers(tmp_path):
    class SFTP:
        def __init__(self):
            self.existing = {"/"}
            self.created = []

        def stat(self, path):
            if path not in self.existing:
                raise IOError("no such file")

        def mkdir(self, path):
            self.existing.add(path)
            self.created.append(path)

    sftp = SFTP()
    remotes.sftp_makedirs(sftp, "")
    remotes.sftp_makedirs(sftp, "/")
    remotes.sftp_makedirs(sftp, "/a/b")
    assert sftp.created == ["/a", "/a/b"]
    assert remotes._sftp_not_found(FileNotFoundError(2, "missing"))
    assert remotes._sftp_not_found(IOError("No such file"))
    assert not remotes._sftp_not_found(IOError("denied"))


def test_remote_sha256_command_stream_fallback_and_errors(tmp_path):
    class Channel:
        def __init__(self, rc):
            self.rc = rc

        def recv_exit_status(self):
            return self.rc

    class Stream:
        def __init__(self, data, rc):
            self.data = data
            self.channel = Channel(rc)

        def read(self):
            return self.data

    class SFTP:
        def __init__(self, fail=False):
            self.fail = fail
            self.closed = False

        def open(self, *_a):
            if self.fail:
                raise IOError("bad")
            return open(tmp_path / "remote", "rb")

        def close(self):
            self.closed = True

    (tmp_path / "remote").write_bytes(b"content")

    class Client:
        def __init__(self, output, rc, sftp=None):
            self.output, self.rc, self.sftp = output, rc, sftp

        def exec_command(self, *_a, **_k):
            return None, Stream(self.output, self.rc), None

        def open_sftp(self):
            if self.sftp is None:
                raise IOError("no sftp")
            return self.sftp

    sftp = SFTP()
    assert remotes.remote_sha256(Client(b"bad", 1, sftp), "/x") == hashlib.sha256(b"content").hexdigest()
    assert sftp.closed
    with pytest.raises(RemoteError, match="cannot hash remote file"):
        remotes.remote_sha256(Client(b"bad", 1), "/x")
    with pytest.raises(RemoteError, match="cannot stream remote file"):
        remotes.remote_sha256(Client(b"bad", 1), "/x", sftp=SFTP(fail=True))


def test_remote_directory_identity_remove_and_publish(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "dir").mkdir()
    (root / "dir" / "file").write_text("x", encoding="utf-8")
    (root / "link").symlink_to("dir/file")
    sftp = FakeSFTP()
    digest, size = remotes._remote_directory_identity(sftp, str(root))
    assert digest and size == 1
    remotes._remove_remote_tree(sftp, str(root))
    assert not root.exists()
    remotes._remove_remote_tree(sftp, str(root))

    source = tmp_path / "source"
    source.write_text("x", encoding="utf-8")
    target = tmp_path / "target"
    remotes._publish_remote(sftp, str(source), str(target), overwrite=False)
    assert target.exists()
    replacement = tmp_path / "replacement"
    replacement.write_text("y", encoding="utf-8")
    remotes._publish_remote(sftp, str(replacement), str(target), overwrite=True)
    assert target.read_text() == "y"

    class NoPosix:
        @staticmethod
        def rename(*_a):
            pass

    with pytest.raises(RemoteError, match="POSIX rename is required"):
        remotes._publish_remote(NoPosix(), "a", "b", overwrite=True)

    class BadPosix:
        @staticmethod
        def posix_rename(*_a):
            raise IOError("bad")

    with pytest.raises(RemoteError, match="cannot atomically replace"):
        remotes._publish_remote(BadPosix(), "a", "b", overwrite=True)


def _store(tmp_path: Path):
    root = tmp_path / "remote"
    root.mkdir(exist_ok=True)
    client = FakeSSHClient()
    spec = remotes.RemoteSpec("r", "host", "", 22, "", str(root), connect_timeout=0)
    return remotes.SFTPStore(spec, client=client), client, root


def test_store_properties_paths_exists_and_matches(tmp_path):
    store, client, root = _store(tmp_path)
    assert store.client is client
    assert store.sftp is client.sftp and store.sftp is client.sftp
    assert store.remote_path("a/b") == str(root / "a" / "b")
    assert not store.exists("missing")
    file = root / "file"
    file.write_text("data", encoding="utf-8")
    assert store.exists("file")
    assert store.matches("file", sha256_path(file), path_size_bytes(file))
    assert not store.matches("file", "0" * 64, file.stat().st_size)
    directory = root / "directory"
    directory.mkdir()
    (directory / "x").write_text("x", encoding="utf-8")
    assert store.matches("directory", sha256_path(directory), path_size_bytes(directory))
    store.close()
    assert store._sftp is None


def test_store_put_get_file_and_directory_with_symlink(tmp_path):
    store, _client, root = _store(tmp_path)
    local_file = tmp_path / "local.txt"
    local_file.write_text("file", encoding="utf-8")
    store.put(local_file, "files/value.txt")
    downloaded = tmp_path / "downloaded"
    store.get("files/value.txt", downloaded)
    assert downloaded.read_text() == "file"

    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "plain").write_text("p", encoding="utf-8")
    (tree / "sub" / "nested").write_text("n", encoding="utf-8")
    (tree / "link").symlink_to("plain")
    store.put(tree, "trees/tree")
    pulled = tmp_path / "pulled"
    store.get("trees/tree", pulled)
    assert (pulled / "sub" / "nested").read_text() == "n"
    assert (pulled / "link").is_symlink()
    assert (root / "trees" / "tree").is_dir()


def test_store_get_and_directory_identity_reject_special_entries(tmp_path):
    store, _client, root = _store(tmp_path)
    special = root / "fifo"
    import os
    os.mkfifo(special)
    assert not store.matches("fifo", "0" * 64, 0)
    with pytest.raises(RemoteError, match="unsupported remote artifact type"):
        store.get("fifo", tmp_path / "out")
    directory = root / "special-tree"
    directory.mkdir()
    os.mkfifo(directory / "fifo")
    with pytest.raises(RemoteError, match="unsupported remote directory entry"):
        remotes._remote_directory_identity(store.sftp, str(directory))

    class CannotList:
        @staticmethod
        def listdir_attr(_path):
            raise IOError("denied")

    with pytest.raises(RemoteError, match="cannot list remote directory"):
        remotes._remote_directory_identity(CannotList(), "/x")


def test_store_manifest_read_write_and_validation(tmp_path):
    store, _client, root = _store(tmp_path)
    assert store.read_manifest() == {"version": 2, "files": {}}
    store.write_manifest({"files": {"x": {}}})
    document = store.read_manifest()
    assert document["version"] == 2 and document["remote_root"] == str(root)
    store.write_manifest({"files": {}})
    assert store.read_manifest()["files"] == {}

    manifest = root / remotes.REMOTE_MANIFEST_NAME
    manifest.write_text("not json", encoding="utf-8")
    with pytest.raises(RemoteError, match="invalid operon-manifest"):
        store.read_manifest()
    manifest.write_text("[]", encoding="utf-8")
    with pytest.raises(RemoteError, match="must be a mapping"):
        store.read_manifest()


def test_manifest_lock_happy_timeout_create_and_release_failures(tmp_path, monkeypatch):
    store, _client, root = _store(tmp_path)
    lock = root / remotes.REMOTE_MANIFEST_LOCK_NAME
    with store.manifest_lock():
        assert lock.is_dir()
        assert (lock / "owner.json").is_file()
    assert not lock.exists()

    lock.mkdir()
    monkeypatch.setattr(remotes.time, "monotonic", lambda: 1)
    with pytest.raises(RemoteError, match="manifest is locked"):
        with store.manifest_lock(timeout=0):
            pass
    lock.rmdir()

    class CannotCreate(FakeSFTP):
        def mkdir(self, path):
            if path.endswith(remotes.REMOTE_MANIFEST_LOCK_NAME):
                raise IOError("permission denied")
            super().mkdir(path)

        def stat(self, path):
            if path.endswith(remotes.REMOTE_MANIFEST_LOCK_NAME):
                raise IOError("permission denied")
            return super().stat(path)

    store._sftp = CannotCreate()
    with pytest.raises(RemoteError, match="cannot create manifest lock"):
        with store.manifest_lock(timeout=0):
            pass

    class CannotRelease(FakeSFTP):
        def rmdir(self, path):
            if path.endswith(remotes.REMOTE_MANIFEST_LOCK_NAME):
                raise IOError("permission denied")
            super().rmdir(path)

    store._sftp = CannotRelease()
    with pytest.raises(RemoteError, match="failed to release manifest lock"):
        with store.manifest_lock():
            pass


@pytest.mark.parametrize(
    "entry",
    [None, {}, {"file_id": "F", "sha256": "bad", "size_bytes": 1},
     {"file_id": "F", "sha256": "a" * 64, "size_bytes": "bad"},
     {"file_id": "F", "sha256": "a" * 64, "size_bytes": -1}],
)
def test_manifest_entry_identity_validation(entry):
    with pytest.raises(RemoteError):
        remotes._entry_identity(entry, label="entry")


def test_project_manifest_and_record_identity_conflicts(tmp_path):
    p = SimpleNamespace(project_id="P1")
    document = {}
    remotes._require_project_manifest(p, "r", document)
    assert document["project_id"] == "P1"
    with pytest.raises(ConflictError, match="belongs to project"):
        remotes._require_project_manifest(p, "r", {"project_id": "P2"})
    record = {"file_id": "F1", "relative_path": "x", "sha256": "a" * 64, "size_bytes": 1}
    with pytest.raises(ConflictError, match="does not match"):
        remotes._assert_entry_matches_record(
            "r", "x", {"file_id": "F2", "sha256": "a" * 64, "size_bytes": 1}, record
        )


def test_fetch_sftp_rejects_password_and_cleans_failed_download(tmp_path, monkeypatch):
    with pytest.raises(ValidationError, match="passwords in sftp"):
        remotes.fetch_url_to_temp(SimpleNamespace(), "sftp://user:secret@host/path.fa")

    temporary = tmp_path / "partial.fa"
    fd = os.open(temporary, os.O_CREAT | os.O_RDWR)
    monkeypatch.setattr(remotes.tempfile, "mkstemp", lambda **_k: (fd, str(temporary)))

    class SFTP:
        closed = False

        def get(self, *_a):
            raise OSError("connection lost")

        def close(self):
            self.closed = True

    class Client:
        closed = False
        sftp = SFTP()

        def open_sftp(self):
            return self.sftp

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(remotes, "connect_ssh", lambda *_a, **_k: client)
    with pytest.raises(OSError, match="connection lost"):
        remotes.fetch_url_to_temp(SimpleNamespace(), "sftp://host/path.fa")
    assert client.closed and client.sftp.closed
    assert not temporary.exists()


def test_fetch_remote_manifest_conflicts_and_failed_verification_cleanup(tmp_path, monkeypatch):
    configured_project = SimpleNamespace(
        root=tmp_path,
        project_id="P1",
        config={"remotes": {"r": {"host": "host", "root": "/remote"}}},
    )

    class Store:
        entry = None
        matches_result = True

        def __init__(self, _spec):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read_manifest(self):
            files = {} if self.entry is None else {"artifact": self.entry}
            return {"project_id": "P1", "files": files}

        def matches(self, *_a):
            return self.matches_result

        def get(self, _rel, destination):
            destination = Path(destination)
            if self.entry["kind"] == "directory":
                destination.mkdir()
                (destination / "part").write_text("wrong", encoding="utf-8")
            else:
                destination.write_text("wrong", encoding="utf-8")

    monkeypatch.setattr(remotes, "SFTPStore", Store)
    with pytest.raises(RemoteError, match="no manifest entry"):
        remotes.fetch_url_to_temp(configured_project, "remote://r/artifact")

    Store.entry = {"file_id": "F1", "sha256": "a" * 64, "size_bytes": 999, "kind": "file"}
    Store.matches_result = False
    with pytest.raises(ConflictError, match="content diverges"):
        remotes.fetch_url_to_temp(configured_project, "remote://r/artifact")

    Store.matches_result = True
    directory_temp = tmp_path / "partial-directory"

    def make_directory(**_kwargs):
        directory_temp.mkdir()
        return str(directory_temp)

    monkeypatch.setattr(remotes.tempfile, "mkdtemp", make_directory)
    Store.entry["kind"] = "directory"
    with pytest.raises(RemoteError, match="download verification failed"):
        remotes.fetch_url_to_temp(configured_project, "remote://r/artifact")
    assert not directory_temp.exists()

    file_temp = tmp_path / "partial-file"

    def make_file(**_kwargs):
        return os.open(file_temp, os.O_CREAT | os.O_RDWR), str(file_temp)

    monkeypatch.setattr(remotes.tempfile, "mkstemp", make_file)
    Store.entry["kind"] = "file"
    with pytest.raises(RemoteError, match="download verification failed"):
        remotes.fetch_url_to_temp(configured_project, "remote://r/artifact")
    assert not file_temp.exists()
