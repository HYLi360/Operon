"""Remote storage mirrors over SFTP (Paramiko), plus remote URL fetching.

A configured remote keeps a byte-for-byte mirror of manifest files under its
own root, keyed by the project-relative path.  File identity is still
`file_id + sha256 + size_bytes`: every transfer is checksum-verified,
idempotent, and never silently overwrites conflicting bytes.

The mirror inventory lives on the remote itself in `operon-manifest.json`.
The local SQLite `file_locations` table is a rebuildable residency cache used
by the local control plane; file identity remains in the `files` table.

Paramiko is an optional dependency (`pip install 'operon[remote]'`) and is
imported lazily; core archiving works without it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import shlex
import shutil
import stat as stat_module
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from operon.config import Project
from operon.database import Database
from operon.errors import ConfigError, ConflictError, RemoteError, ValidationError
from operon.utils import (
    SHA256_RE,
    atomic_write_text,
    iter_directory_entries,
    now_iso,
    path_size_bytes,
    sha256_path,
)

REMOTE_MANIFEST_NAME = "operon-manifest.json"
REMOTE_MANIFEST_LOCK_NAME = ".operon-manifest.lock"


def import_paramiko() -> Any:
    """Import Paramiko lazily so it stays an optional dependency."""
    try:
        import paramiko
        return paramiko
    except ImportError as exc:
        raise ConfigError(
            "remote storage / SSH execution requires Paramiko; "
            "install it with `pip install 'operon[remote]'` or `pip install paramiko`"
        ) from exc


@dataclass
class RemoteSpec:
    name: str
    host: str
    user: str
    port: int
    key_file: str
    root: str
    connect_timeout: float = 30.0
    known_hosts: str = ""
    host_key_sha256: str = ""
    insecure_accept_unknown_host: bool = False

    @property
    def address(self) -> str:
        return f"{self.user + '@' if self.user else ''}{self.host}:{self.port}"


def list_remotes(project: Project) -> dict[str, dict[str, Any]]:
    remotes = project.config.get("remotes", {}) or {}
    if not isinstance(remotes, dict):
        raise ValidationError("'remotes' in project.yaml must be a mapping")
    return {str(name): raw for name, raw in remotes.items()}


def get_remote(project: Project, name: str) -> RemoteSpec:
    remotes = list_remotes(project)
    if name not in remotes:
        available = ", ".join(sorted(remotes)) or "(none)"
        raise ValidationError(f"unknown remote {name!r} in project.yaml; available: {available}")
    raw = remotes[name]
    if not isinstance(raw, dict):
        raise ValidationError(f"remote {name!r} in project.yaml must be a mapping")
    rtype = str(raw.get("type", "sftp") or "sftp")
    if rtype != "sftp":
        raise ValidationError(f"remote {name!r}: unsupported type {rtype!r} (only 'sftp')")
    host = str(raw.get("host", "") or "").strip()
    root = str(raw.get("root", "") or "").strip()
    if not host:
        raise ValidationError(f"remote {name!r}: 'host' is required")
    if not root:
        raise ValidationError(f"remote {name!r}: 'root' (remote mirror directory) is required")
    if not root.startswith("/"):
        raise ValidationError(f"remote {name!r}: 'root' must be an absolute POSIX path")
    return RemoteSpec(
        name=name,
        host=host,
        user=str(raw.get("user", "") or "").strip(),
        port=int(raw.get("port", 22) or 22),
        key_file=str(raw.get("key_file", "") or "").strip(),
        root=root,
        connect_timeout=float(raw.get("connect_timeout", 30) or 30),
        known_hosts=str(raw.get("known_hosts", "") or "").strip(),
        host_key_sha256=str(raw.get("host_key_sha256", "") or "").strip(),
        insecure_accept_unknown_host=bool(raw.get("insecure_accept_unknown_host", False)),
    )


def _normalize_host_key_fingerprint(value: str) -> str:
    value = value.strip()
    if value.upper().startswith("SHA256:"):
        value = value[7:]
    return value.rstrip("=")


def _host_key_fingerprint(key: Any) -> str:
    return base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")


def connect_ssh(host: str, user: str = "", port: int = 22, key_file: str = "",
                connect_timeout: float = 30.0, known_hosts: str = "",
                host_key_sha256: str = "", insecure_accept_unknown_host: bool = False) -> Any:
    """Open a host-key-verified SSH connection with key/agent authentication."""
    paramiko = import_paramiko()
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if known_hosts:
        try:
            client.load_host_keys(os.path.expanduser(known_hosts))
        except OSError as exc:
            client.close()
            raise ConfigError(f"cannot load SSH known-hosts file {known_hosts!r}: {exc}") from exc
    expected_fingerprint = _normalize_host_key_fingerprint(host_key_sha256)
    if expected_fingerprint:
        class PinnedHostKeyPolicy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, ssh_client: Any, hostname: str, key: Any) -> None:
                actual = _host_key_fingerprint(key)
                if actual != expected_fingerprint:
                    raise paramiko.SSHException(
                        f"host key fingerprint mismatch for {hostname}: "
                        f"expected SHA256:{expected_fingerprint}, got SHA256:{actual}"
                    )
                ssh_client.get_host_keys().add(hostname, key.get_name(), key)

        client.set_missing_host_key_policy(PinnedHostKeyPolicy())
    elif insecure_accept_unknown_host:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    kwargs: dict[str, Any] = {
        "hostname": host,
        "port": int(port),
        "timeout": float(connect_timeout),
        "allow_agent": True,
        "look_for_keys": True,
    }
    if user:
        kwargs["username"] = user
    if key_file:
        kwargs["key_filename"] = os.path.expanduser(key_file)
    address = f"{user + '@' if user else ''}{host}:{port}"
    try:
        client.connect(**kwargs)
        if expected_fingerprint:
            transport = client.get_transport()
            key = transport.get_remote_server_key() if transport is not None else None
            actual = _host_key_fingerprint(key) if key is not None else ""
            if actual != expected_fingerprint:
                client.close()
                raise RemoteError(
                    f"host key fingerprint mismatch for {address}: expected "
                    f"SHA256:{expected_fingerprint}, got SHA256:{actual or '(unavailable)'}"
                )
    except Exception as exc:
        client.close()
        if isinstance(exc, RemoteError):
            raise
        raise RemoteError(f"cannot connect to {address}: {type(exc).__name__}: {exc}") from exc
    return client


def sftp_makedirs(sftp: Any, remote_dir: str) -> None:
    """`mkdir -p` equivalent for SFTP (remote paths are POSIX)."""
    if not remote_dir or remote_dir == "/":
        return
    missing: list[str] = []
    path = remote_dir
    while path and path != "/":
        try:
            sftp.stat(path)
            break
        except IOError:
            missing.append(path)
            path = posixpath.dirname(path)
    for directory in reversed(missing):
        sftp.mkdir(directory)


def validate_relative_path(value: str, *, label: str = "remote relative path") -> str:
    """Return a normalized safe POSIX relative path or raise ValidationError."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty relative path")
    value = value.replace("\\", "/")
    if value.startswith("/"):
        raise ValidationError(f"{label} must not be absolute: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError(f"{label} contains an unsafe path component: {value!r}")
    normalized = posixpath.normpath(value)
    if normalized == ".." or normalized.startswith("../"):
        raise ValidationError(f"{label} escapes its configured root: {value!r}")
    return normalized


def local_artifact_path(project: Project, relative_path: str) -> Path:
    """Resolve a manifest path while proving it stays inside the project."""
    relative_path = validate_relative_path(relative_path, label="manifest relative_path")
    root = project.root.resolve()
    target = (root / relative_path).resolve(strict=False)
    if not target.is_relative_to(root):
        raise ValidationError(f"manifest relative_path escapes project root: {relative_path!r}")
    return target


def remote_sha256(client: Any, remote_path: str, timeout: float = 600.0,
                  sftp: Any = None) -> str:
    """Return an exact remote file SHA-256, falling back to streamed SFTP."""
    _, stdout, _ = client.exec_command(f"sha256sum -- {shlex.quote(remote_path)}", timeout=timeout)
    output = stdout.read().decode("utf-8", "replace")
    if stdout.channel.recv_exit_status() == 0:
        token = output.split()[0] if output.split() else ""
        if SHA256_RE.match(token):
            return token.lower()
    if sftp is None:
        try:
            sftp = client.open_sftp()
            owns_sftp = True
        except Exception as exc:
            raise RemoteError(f"cannot hash remote file {remote_path}: {exc}") from exc
    else:
        owns_sftp = False
    digest = hashlib.sha256()
    try:
        with sftp.open(remote_path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except Exception as exc:
        raise RemoteError(f"cannot stream remote file for SHA-256 {remote_path}: {exc}") from exc
    finally:
        if owns_sftp:
            sftp.close()
    return digest.hexdigest()


def _sftp_not_found(exc: BaseException) -> bool:
    return getattr(exc, "errno", None) == 2 or "no such file" in str(exc).lower()


def _sftp_lstat(sftp: Any, path: str) -> Any:
    lstat = getattr(sftp, "lstat", None)
    return lstat(path) if lstat else sftp.stat(path)


def _remote_directory_identity(sftp: Any, root: str) -> tuple[str, int]:
    """Compute the same deterministic tree identity as utils.sha256_directory."""
    entries: list[tuple[str, str, Any]] = []

    def walk(remote_dir: str, relative_dir: str = "") -> None:
        try:
            children = sorted(sftp.listdir_attr(remote_dir), key=lambda item: item.filename)
        except Exception as exc:
            raise RemoteError(f"cannot list remote directory {remote_dir}: {exc}") from exc
        for child in children:
            relative = posixpath.join(relative_dir, child.filename) if relative_dir else child.filename
            remote_child = posixpath.join(remote_dir, child.filename)
            mode = _sftp_lstat(sftp, remote_child).st_mode
            if stat_module.S_ISLNK(mode):
                entries.append((relative, "L", sftp.readlink(remote_child)))
            elif stat_module.S_ISDIR(mode):
                entries.append((relative, "D", None))
                walk(remote_child, relative)
            elif stat_module.S_ISREG(mode):
                entries.append((relative, "F", int(child.st_size)))
            else:
                raise RemoteError(f"unsupported remote directory entry type: {remote_child}")

    walk(root)
    digest = hashlib.sha256()
    total_size = 0
    for relative_text, kind, value in sorted(entries, key=lambda item: item[0]):
        relative = relative_text.encode("utf-8", errors="surrogateescape")
        if kind == "L":
            target = str(value).encode("utf-8", errors="surrogateescape")
            digest.update(b"L\0" + str(len(relative)).encode("ascii") + b":" + relative)
            digest.update(b"\0" + str(len(target)).encode("ascii") + b":" + target + b"\0")
        elif kind == "D":
            digest.update(b"D\0" + str(len(relative)).encode("ascii") + b":" + relative + b"\0")
        else:
            size = int(value)
            total_size += size
            digest.update(b"F\0" + str(len(relative)).encode("ascii") + b":" + relative)
            digest.update(b"\0" + str(size).encode("ascii") + b"\0")
            remote_file = posixpath.join(root, relative_text)
            try:
                with sftp.open(remote_file, "rb") as handle:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
            except Exception as exc:
                raise RemoteError(f"cannot hash remote directory member {remote_file}: {exc}") from exc
            digest.update(b"\0")
    return digest.hexdigest(), total_size


def _remove_remote_tree(sftp: Any, remote: str) -> None:
    try:
        mode = _sftp_lstat(sftp, remote).st_mode
    except IOError as exc:
        if _sftp_not_found(exc):
            return
        raise
    if stat_module.S_ISDIR(mode):
        for entry in sftp.listdir_attr(remote):
            _remove_remote_tree(sftp, posixpath.join(remote, entry.filename))
        sftp.rmdir(remote)
    else:
        sftp.remove(remote)


def _publish_remote(sftp: Any, tmp: str, target: str, *, overwrite: bool) -> None:
    """Publish a fully-written temp artifact without assuming rename overwrites."""
    if not overwrite:
        sftp.rename(tmp, target)
        return
    posix_rename = getattr(sftp, "posix_rename", None)
    if posix_rename is None:
        raise RemoteError(
            f"SFTP server/client cannot atomically replace {target}; POSIX rename is required"
        )
    try:
        posix_rename(tmp, target)
    except Exception as exc:
        raise RemoteError(
            f"SFTP server cannot atomically replace {target} with POSIX rename: {exc}"
        ) from exc


class SFTPStore:
    """One configured remote mirror, addressed by project-relative paths."""

    def __init__(self, spec: RemoteSpec, client: Any = None):
        self.spec = spec
        self._client = client
        self._sftp: Any = None
        self._owns_client = client is None

    def __enter__(self) -> "SFTPStore":
        _ = self.sftp
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._sftp is not None:
            self._sftp.close()
            self._sftp = None
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = connect_ssh(
                self.spec.host, user=self.spec.user, port=self.spec.port,
                key_file=self.spec.key_file, connect_timeout=self.spec.connect_timeout,
                known_hosts=self.spec.known_hosts,
                host_key_sha256=self.spec.host_key_sha256,
                insecure_accept_unknown_host=self.spec.insecure_accept_unknown_host,
            )
        return self._client

    @property
    def sftp(self) -> Any:
        if self._sftp is None:
            self._sftp = self.client.open_sftp()
        return self._sftp

    def remote_path(self, rel: str) -> str:
        rel = validate_relative_path(rel)
        root = posixpath.normpath(self.spec.root)
        joined = posixpath.normpath(posixpath.join(root, rel))
        if joined == root or not joined.startswith(root.rstrip("/") + "/"):
            raise ValidationError(f"remote path escapes configured root {root!r}: {rel!r}")
        return joined

    def exists(self, rel: str) -> bool:
        try:
            self.sftp.stat(self.remote_path(rel))
            return True
        except IOError:
            return False

    def matches(self, rel: str, sha256: str, size_bytes: int) -> bool:
        """Whether the remote object has exactly the expected content."""
        try:
            remote = self.remote_path(rel)
            stat = _sftp_lstat(self.sftp, remote)
        except IOError:
            return False
        if stat_module.S_ISDIR(stat.st_mode):
            digest, actual_size = _remote_directory_identity(self.sftp, remote)
        elif stat_module.S_ISREG(stat.st_mode):
            actual_size = int(stat.st_size)
            digest = remote_sha256(self.client, remote, sftp=self.sftp)
        else:
            return False
        return actual_size == int(size_bytes) and digest == str(sha256).lower()

    def put(self, local: Path, rel: str) -> None:
        remote = self.remote_path(rel)
        sftp_makedirs(self.sftp, posixpath.dirname(remote))
        tmp = f"{remote}.operon-tmp-{uuid.uuid4().hex}"
        try:
            if local.is_dir():
                self.sftp.mkdir(tmp)
                for path in iter_directory_entries(local):
                    remote_member = posixpath.join(tmp, path.relative_to(local).as_posix())
                    if path.is_symlink():
                        sftp_makedirs(self.sftp, posixpath.dirname(remote_member))
                        self.sftp.symlink(os.readlink(path), remote_member)
                    elif path.is_dir():
                        self.sftp.mkdir(remote_member)
                    elif path.is_file():
                        sftp_makedirs(self.sftp, posixpath.dirname(remote_member))
                        self.sftp.put(str(path), remote_member)
                    else:
                        raise RemoteError(f"unsupported local directory entry: {path}")
            else:
                self.sftp.put(str(local), tmp)
            _publish_remote(self.sftp, tmp, remote, overwrite=False)
        except BaseException:
            _remove_remote_tree(self.sftp, tmp)
            raise

    def get(self, rel: str, local_tmp: Path) -> None:
        remote = self.remote_path(rel)
        mode = _sftp_lstat(self.sftp, remote).st_mode
        if stat_module.S_ISDIR(mode):
            local_tmp.mkdir(parents=True, exist_ok=False)
            self._get_directory(remote, local_tmp)
        elif stat_module.S_ISREG(mode):
            self.sftp.get(remote, str(local_tmp))
        else:
            raise RemoteError(f"unsupported remote artifact type: {remote}")

    def _get_directory(self, remote: str, local: Path) -> None:
        for entry in sorted(self.sftp.listdir_attr(remote), key=lambda item: item.filename):
            remote_child = posixpath.join(remote, entry.filename)
            local_child = local / entry.filename
            mode = _sftp_lstat(self.sftp, remote_child).st_mode
            if stat_module.S_ISLNK(mode):
                os.symlink(self.sftp.readlink(remote_child), local_child)
            elif stat_module.S_ISDIR(mode):
                local_child.mkdir()
                self._get_directory(remote_child, local_child)
            elif stat_module.S_ISREG(mode):
                self.sftp.get(remote_child, str(local_child))
            else:
                raise RemoteError(f"unsupported remote artifact type: {remote_child}")

    def read_manifest(self) -> dict[str, Any]:
        try:
            with self.sftp.open(self.remote_path(REMOTE_MANIFEST_NAME), "rb") as handle:
                doc = json.loads(handle.read().decode("utf-8"))
        except IOError as exc:
            if _sftp_not_found(exc):
                return {"version": 2, "files": {}}
            raise RemoteError(
                f"remote {self.spec.name!r}: cannot read {REMOTE_MANIFEST_NAME}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RemoteError(
                f"remote {self.spec.name!r}: invalid {REMOTE_MANIFEST_NAME}: {exc}"
            ) from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("files"), dict):
            raise RemoteError(
                f"remote {self.spec.name!r}: {REMOTE_MANIFEST_NAME} must be a mapping with a 'files' object"
            )
        return doc

    def write_manifest(self, doc: dict[str, Any]) -> None:
        doc = dict(doc)
        doc["version"] = 2
        doc["remote_root"] = self.spec.root
        doc["updated_at"] = now_iso()
        payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        remote = self.remote_path(REMOTE_MANIFEST_NAME)
        sftp_makedirs(self.sftp, posixpath.dirname(remote))
        tmp = f"{remote}.operon-tmp-{uuid.uuid4().hex}"
        try:
            with self.sftp.open(tmp, "wb") as handle:
                handle.write(payload)
            _publish_remote(self.sftp, tmp, remote, overwrite=self.exists(REMOTE_MANIFEST_NAME))
        except BaseException:
            _remove_remote_tree(self.sftp, tmp)
            raise

    @contextmanager
    def manifest_lock(self, timeout: float | None = None):
        """Serialize manifest writers with an atomic remote-directory lock.

        A crashed writer deliberately leaves a lock behind instead of guessing
        that it is safe to steal. The error includes the exact path so an
        operator can inspect and remove a stale lock after proving no writer is
        active.
        """
        wait_timeout = self.spec.connect_timeout if timeout is None else max(0.0, float(timeout))
        sftp_makedirs(self.sftp, self.spec.root)
        lock_path = self.remote_path(REMOTE_MANIFEST_LOCK_NAME)
        owner_path = posixpath.join(lock_path, "owner.json")
        deadline = time.monotonic() + wait_timeout
        while True:
            try:
                self.sftp.mkdir(lock_path)
                break
            except IOError as exc:
                try:
                    self.sftp.stat(lock_path)
                except IOError:
                    raise RemoteError(
                        f"remote {self.spec.name!r}: cannot create manifest lock {lock_path}: {exc}"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise RemoteError(
                        f"remote {self.spec.name!r}: manifest is locked at {lock_path}; "
                        "if a previous writer crashed, inspect the owner and remove the lock "
                        "only after confirming no push is active"
                    ) from exc
                time.sleep(0.2)
        try:
            owner = json.dumps(
                {"token": uuid.uuid4().hex, "created_at": now_iso()}, sort_keys=True,
            ).encode("utf-8")
            with self.sftp.open(owner_path, "wb") as handle:
                handle.write(owner)
            yield
        finally:
            try:
                self.sftp.remove(owner_path)
            except IOError:
                pass
            try:
                self.sftp.rmdir(lock_path)
            except IOError as exc:
                raise RemoteError(
                    f"remote {self.spec.name!r}: failed to release manifest lock {lock_path}: {exc}"
                ) from exc


def _select_files(db: Database, file_ids: list[str] | None) -> list[dict[str, Any]]:
    if file_ids:
        placeholders = ", ".join("?" for _ in file_ids)
        rows = db.conn.execute(
            f"SELECT * FROM files WHERE file_id IN ({placeholders}) ORDER BY file_id", file_ids
        ).fetchall()
        found = {row["file_id"] for row in rows}
        missing = [file_id for file_id in file_ids if file_id not in found]
        if missing:
            raise ValidationError(f"unknown file_id(s): {', '.join(missing)}")
    else:
        rows = db.conn.execute("SELECT * FROM files ORDER BY file_id").fetchall()
    return [dict(row) for row in rows]


def _sync_log(db: Database, project: Project, step: str, record: dict[str, Any],
              status: str, error: str | None = None) -> None:
    from operon.workflow import log_run
    log_run(db, project, {
        "entity_type": record.get("entity_type"),
        "entity_id": record.get("entity_id"),
        "step": step,
        "status": "failed" if status == "error" else "completed",
        "started_at": now_iso(),
        "finished_at": now_iso(),
        "command": f"{step} {record.get('relative_path', '')}".strip(),
        "input_sha256": record.get("sha256"),
        "executor": "sftp",
        "execution_details": json.dumps({"result": status}, sort_keys=True),
        "error": error,
    })


def _require_project_manifest(project: Project, name: str, doc: dict[str, Any]) -> None:
    remote_project_id = str(doc.get("project_id", "") or "")
    if remote_project_id and remote_project_id != project.project_id:
        raise ConflictError(
            f"remote {name!r} belongs to project {remote_project_id!r}, "
            f"not {project.project_id!r}"
        )
    doc["project_id"] = project.project_id


def _record_remote_location(db: Database, name: str, record: dict[str, Any],
                            relative_path: str, *, status: str = "AVAILABLE") -> None:
    db.conn.execute(
        "INSERT INTO file_locations(file_id, location_name, location_type, uri, relative_path, "
        "sha256, size_bytes, status, verified_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(file_id, location_name) DO UPDATE SET "
        "location_type=excluded.location_type, uri=excluded.uri, "
        "relative_path=excluded.relative_path, sha256=excluded.sha256, "
        "size_bytes=excluded.size_bytes, status=excluded.status, verified_at=excluded.verified_at",
        (
            record["file_id"], name, "sftp", f"remote://{name}/{relative_path}", relative_path,
            str(record["sha256"]).lower(), int(record["size_bytes"]), status, now_iso(),
        ),
    )
    db.conn.commit()


def _mark_remote_location(db: Database, name: str, file_id: str, status: str) -> None:
    row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if row is None:
        raise ValidationError(f"cannot mark remote location for unknown file_id {file_id}")
    record = dict(row)
    rel = validate_relative_path(record["relative_path"], label="manifest relative_path")
    _record_remote_location(db, name, record, rel, status=status)


def _entry_identity(entry: dict[str, Any], *, label: str) -> tuple[str, str, int]:
    if not isinstance(entry, dict):
        raise RemoteError(f"{label} must be an object")
    file_id = str(entry.get("file_id", "") or "")
    sha = str(entry.get("sha256", "") or "").lower()
    try:
        size = int(entry.get("size_bytes", -1))
    except (TypeError, ValueError) as exc:
        raise RemoteError(f"{label} has invalid size_bytes") from exc
    if not file_id or not SHA256_RE.match(sha) or size < 0:
        raise RemoteError(f"{label} has invalid file_id/sha256/size_bytes identity")
    return file_id, sha, size


def _assert_entry_matches_record(name: str, rel: str, entry: dict[str, Any],
                                 record: dict[str, Any]) -> None:
    entry_file_id, entry_sha, entry_size = _entry_identity(
        entry, label=f"remote {name!r} manifest entry {rel!r}"
    )
    if (
            entry_file_id != str(record["file_id"])
            or entry_sha != str(record["sha256"]).lower()
            or entry_size != int(record["size_bytes"])
            or rel != str(record["relative_path"])
    ):
        raise ConflictError(
            f"remote {name!r} identity for {rel} does not match local manifest "
            f"record {record['file_id']}"
        )


def push(db: Database, project: Project, name: str,
         file_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Upload files as one locked manifest batch, continuing per-item errors."""
    spec = get_remote(project, name)
    records = _select_files(db, file_ids)
    results: list[dict[str, Any]] = []
    records_by_id = {record["file_id"]: record for record in records}
    with SFTPStore(spec) as store:
        with store.manifest_lock():
            doc = store.read_manifest()
            _require_project_manifest(project, name, doc)
            entries = doc.setdefault("files", {})
            if not isinstance(entries, dict):
                raise RemoteError(f"remote {name!r} manifest 'files' must be an object")
            manifest_changed = False
            for record in records:
                rel = validate_relative_path(record["relative_path"], label="manifest relative_path")
                sha = str(record["sha256"]).lower()
                size = int(record["size_bytes"])
                local = local_artifact_path(project, rel)
                result = {
                    "file_id": record["file_id"], "relative_path": rel,
                    "status": "", "error": None,
                }
                try:
                    entry = entries.get(rel)
                    if entry is not None:
                        _assert_entry_matches_record(name, rel, entry, record)
                    if not local.exists():
                        if entry is not None and store.matches(rel, sha, size):
                            result["status"] = "skipped"
                        else:
                            raise RemoteError(
                                f"local artifact is absent and remote {name!r} has no verified copy: {rel}"
                            )
                    elif path_size_bytes(local) != size or sha256_path(local).lower() != sha:
                        raise ConflictError(f"local content does not match manifest identity: {rel}")
                    elif entry is not None:
                        if store.matches(rel, sha, size):
                            result["status"] = "skipped"
                        else:
                            _mark_remote_location(db, name, record["file_id"], "CORRUPT")
                            raise ConflictError(
                                f"remote {name!r} content at {rel} diverges from its manifest entry; "
                                "refusing to overwrite — resolve the remote copy manually"
                            )
                    else:
                        remote_exists = store.exists(rel)
                        if remote_exists:
                            if not store.matches(rel, sha, size):
                                _mark_remote_location(db, name, record["file_id"], "CORRUPT")
                                raise ConflictError(
                                    f"remote {name!r} already holds different bytes at {rel}; "
                                    "refusing to overwrite — resolve the remote copy manually"
                                )
                            result["status"] = "indexed"
                        else:
                            store.put(local, rel)
                            if not store.matches(rel, sha, size):
                                raise RemoteError(
                                    f"upload verification failed for {rel} on remote {name!r}"
                                )
                            result["status"] = "uploaded"
                        entries[rel] = {
                            "file_id": record["file_id"], "relative_path": rel,
                            "sha256": sha, "size_bytes": size,
                            "kind": "directory" if local.is_dir() else "file",
                            "format": record.get("format"), "synced_at": now_iso(),
                        }
                        manifest_changed = True
                except Exception as exc:
                    result.update(status="error", error=f"{type(exc).__name__}: {exc}")
                results.append(result)

            if manifest_changed:
                try:
                    store.write_manifest(doc)
                except Exception as exc:
                    error = f"{type(exc).__name__}: manifest publication failed: {exc}"
                    for result in results:
                        if result["status"] in {"uploaded", "indexed"}:
                            result.update(status="error", error=error)

    for result in results:
        record = records_by_id[result["file_id"]]
        if result["status"] != "error":
            _record_remote_location(db, name, record, result["relative_path"])
        _sync_log(
            db, project, f"push:{name}", record, result["status"],
            result.get("error"),
        )
    return results


def pull(db: Database, project: Project, name: str,
         file_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Restore manifest files from a configured remote (idempotent, verified)."""
    from operon.files import remember_local_file_verification

    spec = get_remote(project, name)
    records = _select_files(db, file_ids) if file_ids else []
    results: list[dict[str, Any]] = []
    with SFTPStore(spec) as store:
        doc = store.read_manifest()
        _require_project_manifest(project, name, doc)
        entries = doc.get("files", {})
        if not isinstance(entries, dict):
            raise RemoteError(f"remote {name!r} manifest 'files' must be an object")
        if file_ids:
            items = [(record["relative_path"], record) for record in records]
        else:
            items = []
            for rel in sorted(entries):
                safe_rel = validate_relative_path(rel, label="remote manifest relative path")
                entry_file_id, _, _ = _entry_identity(
                    entries[rel], label=f"remote {name!r} manifest entry {rel!r}"
                )
                row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (entry_file_id,)).fetchone()
                items.append((safe_rel, dict(row) if row else None))
        for rel, record in items:
            rel = validate_relative_path(rel, label="remote manifest relative path")
            entry = entries.get(rel)
            result = {
                "file_id": (record or {}).get("file_id", ""),
                "relative_path": rel, "status": "", "error": None,
            }
            if entry is None:
                result.update(status="error", error="not present in the remote manifest")
                results.append(result)
                _sync_log(db, project, f"pull:{name}", record or {"relative_path": rel}, "error", result["error"])
                continue
            try:
                entry_file_id, sha, size = _entry_identity(
                    entry, label=f"remote {name!r} manifest entry {rel!r}"
                )
                if record is None:
                    raise ConflictError(
                        f"remote entry {entry_file_id} at {rel} is absent from the local SQLite manifest"
                    )
                result["file_id"] = record["file_id"]
                _assert_entry_matches_record(name, rel, entry, record)
                if not store.matches(rel, sha, size):
                    _mark_remote_location(db, name, record["file_id"], "CORRUPT")
                    raise ConflictError(
                        f"remote {name!r} content at {rel} diverges from its manifest entry"
                    )
                local = local_artifact_path(project, rel)
                if local.exists():
                    if path_size_bytes(local) == size and sha256_path(local).lower() == sha:
                        result["status"] = "skipped"
                    else:
                        raise ConflictError(
                            f"local artifact {local} exists with different bytes; refusing to overwrite"
                        )
                else:
                    local.parent.mkdir(parents=True, exist_ok=True)
                    is_directory = str(entry.get("kind", "file")) == "directory"
                    if is_directory:
                        tmp = Path(tempfile.mkdtemp(prefix=f".{local.name}.", dir=str(local.parent)))
                        tmp.rmdir()
                    else:
                        fd, tmp_name = tempfile.mkstemp(prefix=f".{local.name}.", dir=str(local.parent))
                        os.close(fd)
                        tmp = Path(tmp_name)
                    try:
                        store.get(rel, tmp)
                        if path_size_bytes(tmp) != size or sha256_path(tmp).lower() != sha:
                            raise RemoteError(f"download verification failed for {rel} from remote {name!r}")
                        os.replace(tmp, local)
                    except BaseException:
                        if tmp.is_dir() and not tmp.is_symlink():
                            shutil.rmtree(tmp, ignore_errors=True)
                        else:
                            tmp.unlink(missing_ok=True)
                        raise
                    result["status"] = "downloaded"
                if record.get("status") != "STANDARDIZED":
                    db.set_file_status(
                        record["file_id"], "CHECKSUM_VERIFIED",
                        reason=f"local bytes restored and verified from remote {name}",
                        actor="operon pull",
                        evidence=f"remote://{name}/{rel}",
                    )
                placeholder_path(project, record["file_id"]).unlink(missing_ok=True)
                remember_local_file_verification(db, record, local)
                _record_remote_location(db, name, record, rel)
            except Exception as exc:
                result.update(status="error", error=f"{type(exc).__name__}: {exc}")
                results.append(result)
                _sync_log(db, project, f"pull:{name}", record or {"relative_path": rel}, "error", result["error"])
                continue
            results.append(result)
            _sync_log(db, project, f"pull:{name}", record, result["status"])
    return results


def placeholder_path(project: Project, file_id: str) -> Path:
    """Return the sidecar pointer path for a deliberately remote-only artifact."""
    return project.root / ".operon" / "placeholders" / f"{file_id}.json"


def _write_placeholder(project: Project, name: str, record: dict[str, Any]) -> Path:
    target = placeholder_path(project, record["file_id"])
    payload = {
        "schema": "operon-remote-placeholder-1",
        "project_id": project.project_id,
        "file_id": record["file_id"],
        "relative_path": record["relative_path"],
        "sha256": str(record["sha256"]).lower(),
        "size_bytes": int(record["size_bytes"]),
        "remote": name,
        "uri": f"remote://{name}/{record['relative_path']}",
        "created_at": now_iso(),
    }
    atomic_write_text(target, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return target


def _ensure_remote_only_schema(project: Project) -> None:
    """Add the REMOTE_ONLY enum value to an existing project's files schema."""
    import yaml
    try:
        document = yaml.safe_load(project.schema_path.read_text(encoding="utf-8")) or {}
        status = document["tables"]["files"]["fields"]["status"]
        allowed = list(status.get("allowed", []))
    except (OSError, KeyError, TypeError) as exc:
        raise ValidationError(f"cannot upgrade project files status schema: {exc}") from exc
    changed = False
    if "REMOTE_ONLY" not in allowed:
        allowed.append("REMOTE_ONLY")
        status["allowed"] = allowed
        changed = True
    if str(document.get("schema_version", "")) in {"", "1.0", "1.1"}:
        document["schema_version"] = "1.2"
        changed = True
    if changed:
        atomic_write_text(
            project.schema_path,
            "# Operon metadata schema (YAML). Extended for remote-resident artifacts.\n"
            + yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        )


def verify_remote_record(project: Project, name: str, record: dict[str, Any],
                         db: Database | None = None, *, store: SFTPStore | None = None,
                         manifest: dict[str, Any] | None = None, client: Any = None) -> str:
    """Verify a local manifest record against a configured remote and return its path."""
    if store is None:
        spec = get_remote(project, name)
        with SFTPStore(spec, client=client) as opened:
            return verify_remote_record(
                project, name, record, db=db, store=opened, manifest=manifest,
            )
    rel = validate_relative_path(record["relative_path"], label="manifest relative_path")
    doc = manifest if manifest is not None else store.read_manifest()
    _require_project_manifest(project, name, doc)
    entry = doc.get("files", {}).get(rel)
    if entry is None:
        if db is not None:
            _mark_remote_location(db, name, record["file_id"], "MISSING")
        raise RemoteError(f"remote {name!r} has no manifest entry for {record['file_id']} at {rel}")
    try:
        _assert_entry_matches_record(name, rel, entry, record)
    except Exception:
        if db is not None:
            _mark_remote_location(db, name, record["file_id"], "CORRUPT")
        raise
    if not store.matches(rel, str(record["sha256"]), int(record["size_bytes"])):
        if db is not None:
            status = "CORRUPT" if store.exists(rel) else "MISSING"
            _mark_remote_location(db, name, record["file_id"], status)
        raise ConflictError(f"remote {name!r} artifact diverges from its manifest: {rel}")
    if db is not None:
        _record_remote_location(db, name, record, rel)
    return store.remote_path(rel)


def evict_local(db: Database, project: Project, name: str,
                file_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Remove verified local bytes only after proving an exact remote copy exists.

    The logical path remains in ``files`` and a small pointer is written under
    ``.operon/placeholders``. The pointer is informational; SQLite remains the
    source of truth and the remote identity is stored in ``file_locations``.
    """
    from operon.files import clear_local_file_verification

    records = _select_files(db, file_ids)
    spec = get_remote(project, name)
    results: list[dict[str, Any]] = []
    with SFTPStore(spec) as store:
        doc = store.read_manifest()
        _require_project_manifest(project, name, doc)
        for record in records:
            rel = validate_relative_path(record["relative_path"], label="manifest relative_path")
            result = {"file_id": record["file_id"], "relative_path": rel, "status": "", "error": None}
            try:
                verify_remote_record(
                    project, name, record, db=db, store=store, manifest=doc,
                )
                local = local_artifact_path(project, rel)
                if local.exists():
                    if (
                            path_size_bytes(local) != int(record["size_bytes"])
                            or sha256_path(local).lower() != str(record["sha256"]).lower()
                    ):
                        raise ConflictError(f"local artifact does not match manifest; refusing to evict: {rel}")
                    _ensure_remote_only_schema(project)
                    _write_placeholder(project, name, record)
                    try:
                        if local.is_dir() and not local.is_symlink():
                            shutil.rmtree(local)
                        else:
                            local.unlink()
                    except BaseException:
                        placeholder_path(project, record["file_id"]).unlink(missing_ok=True)
                        raise
                    result["status"] = "evicted"
                else:
                    result["status"] = "skipped"
                    _ensure_remote_only_schema(project)
                    _write_placeholder(project, name, record)
                _record_remote_location(db, name, record, rel)
                clear_local_file_verification(db, record["file_id"])
                db.set_file_status(
                    record["file_id"], "REMOTE_ONLY",
                    reason=f"local bytes evicted after verification on remote {name}",
                    actor="operon evict",
                    evidence=f"remote://{name}/{rel}",
                )
            except Exception as exc:
                result.update(status="error", error=f"{type(exc).__name__}: {exc}")
                results.append(result)
                _sync_log(db, project, f"evict:{name}", record, "error", result["error"])
                continue
            results.append(result)
            _sync_log(db, project, f"evict:{name}", record, result["status"])
    return results


def check_remote(project: Project, name: str) -> dict[str, Any]:
    """Connectivity check used by `operon remotes`."""
    spec = get_remote(project, name)
    result: dict[str, Any] = {"name": name, "type": "sftp", "address": spec.address, "root": spec.root, "status": "ok",
                              "error": ""}
    try:
        with SFTPStore(spec) as store:
            store.sftp.stat(spec.root)
            doc = store.read_manifest()
            _require_project_manifest(project, name, doc)
            result["files"] = len(doc.get("files", {}))
    except Exception as exc:
        result.update(status="error", error=f"{type(exc).__name__}: {exc}", files="")
    return result


def fetch_url_to_temp(project: Project, url: str) -> Path:
    """Download an `sftp://` or `remote://` URL to a local temporary file.

    The remote basename is kept as the temp-file suffix so format/compression
    detection in `ingest_file` behaves the same as for local sources.
    """
    basename: str
    if url.startswith("sftp://"):
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.path or parsed.path == "/":
            raise ValidationError(f"invalid sftp URL (expect sftp://[user@]host[:port]/path): {url!r}")
        if parsed.password:
            raise ValidationError("passwords in sftp:// URLs are not supported; use SSH keys")
        client = connect_ssh(
            parsed.hostname,
            user=parsed.username or "",
            port=parsed.port or 22,
        )
        remote_path = unquote(parsed.path)
        basename = posixpath.basename(remote_path)
        fd, tmp_name = tempfile.mkstemp(prefix="operon-fetch-", suffix=f"-{basename}")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            sftp = client.open_sftp()
            try:
                sftp.get(remote_path, str(tmp))
            finally:
                sftp.close()
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            client.close()
        return tmp
    if url.startswith("remote://"):
        rest = url[len("remote://"):]
        name, _, rel = rest.partition("/")
        if not name or not rel:
            raise ValidationError(f"invalid remote URL (expect remote://<name>/<path>): {url!r}")
        rel = validate_relative_path(unquote(rel), label="remote URL path")
        spec = get_remote(project, name)
        basename = posixpath.basename(rel)
        try:
            with SFTPStore(spec) as store:
                doc = store.read_manifest()
                _require_project_manifest(project, name, doc)
                entry = doc.get("files", {}).get(rel)
                if entry is None:
                    raise RemoteError(f"remote {name!r} has no manifest entry for {rel}")
                _, sha, size = _entry_identity(entry, label=f"remote {name!r} manifest entry {rel!r}")
                if not store.matches(rel, sha, size):
                    raise ConflictError(f"remote {name!r} content diverges from its manifest: {rel}")
                if str(entry.get("kind", "file")) == "directory":
                    tmp = Path(tempfile.mkdtemp(prefix="operon-fetch-", suffix=f"-{basename}"))
                    tmp.rmdir()
                else:
                    fd, tmp_name = tempfile.mkstemp(prefix="operon-fetch-", suffix=f"-{basename}")
                    os.close(fd)
                    tmp = Path(tmp_name)
                store.get(rel, tmp)
                if path_size_bytes(tmp) != size or sha256_path(tmp).lower() != sha:
                    raise RemoteError(f"download verification failed for remote://{name}/{rel}")
        except BaseException:
            if "tmp" in locals():
                if tmp.is_dir() and not tmp.is_symlink():
                    shutil.rmtree(tmp, ignore_errors=True)
                else:
                    tmp.unlink(missing_ok=True)
            raise
        return tmp
    raise ValidationError(
        f"unsupported remote source {url!r}; expected sftp://[user@]host[:port]/path "
        "or remote://<name>/<path>"
    )
