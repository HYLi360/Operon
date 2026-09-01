"""Shared utility functions."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
GZIP_MAGIC = b"\x1f\x8b"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Return the hex SHA-256 of a file, streaming so large files are safe."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def iter_directory_entries(path: str | Path) -> Iterator[Path]:
    """Yield a directory tree in stable path order without following symlinks.

    ``Path.rglob`` changed its directory-symlink traversal behavior across
    supported Python versions. Artifact identity must not depend on the Python
    interpreter, so walk with ``os.scandir(..., follow_symlinks=False)`` and
    sort the complete relative-path inventory explicitly.
    """
    root = Path(path)
    if not root.is_dir():
        raise NotADirectoryError(root)
    entries: list[Path] = []

    def walk(directory: Path) -> None:
        with os.scandir(directory) as children:
            for child in children:
                item = Path(child.path)
                entries.append(item)
                if child.is_dir(follow_symlinks=False):
                    walk(item)

    walk(root)
    yield from sorted(entries, key=lambda entry: entry.relative_to(root).as_posix())


def sha256_directory(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Return a deterministic content hash for a directory tree.

    Relative paths, empty directories, regular-file sizes and bytes, and
    symlink targets all participate in the digest.  Filesystem mtimes and
    ownership do not, so copying an unchanged tree preserves its identity.
    """
    root = Path(path)
    if not root.is_dir():
        raise NotADirectoryError(root)
    digest = hashlib.sha256()
    for entry in iter_directory_entries(root):
        relative = entry.relative_to(root).as_posix().encode("utf-8", errors="surrogateescape")
        if entry.is_symlink():
            target = os.readlink(entry).encode("utf-8", errors="surrogateescape")
            digest.update(b"L\0" + str(len(relative)).encode("ascii") + b":" + relative)
            digest.update(b"\0" + str(len(target)).encode("ascii") + b":" + target + b"\0")
        elif entry.is_dir():
            digest.update(b"D\0" + str(len(relative)).encode("ascii") + b":" + relative + b"\0")
        elif entry.is_file():
            size = entry.stat().st_size
            digest.update(b"F\0" + str(len(relative)).encode("ascii") + b":" + relative)
            digest.update(b"\0" + str(size).encode("ascii") + b"\0")
            with open(entry, "rb") as handle:
                while True:
                    chunk = handle.read(chunk_size)
                    if not chunk:
                        break
                    digest.update(chunk)
            digest.update(b"\0")
        else:
            raise OSError(f"unsupported directory entry type: {entry}")
    return digest.hexdigest()


def sha256_path(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash either a regular file or a directory artifact."""
    artifact = Path(path)
    if artifact.is_dir():
        return sha256_directory(artifact, chunk_size=chunk_size)
    if artifact.is_file():
        return sha256_file(artifact, chunk_size=chunk_size)
    raise FileNotFoundError(f"artifact does not exist or has unsupported type: {artifact}")


def path_size_bytes(path: str | Path) -> int:
    """Return file size or the sum of regular-file bytes in a directory."""
    artifact = Path(path)
    if artifact.is_dir():
        return sum(
            entry.stat().st_size
            for entry in iter_directory_entries(artifact)
            if entry.is_file() and not entry.is_symlink()
        )
    if artifact.is_file():
        return artifact.stat().st_size
    raise FileNotFoundError(f"artifact does not exist or has unsupported type: {artifact}")


def path_is_nonempty(path: str | Path) -> bool:
    """Return whether a file has bytes or a directory has at least one entry."""
    artifact = Path(path)
    if artifact.is_dir():
        return next(artifact.iterdir(), None) is not None
    if artifact.is_file():
        return artifact.stat().st_size > 0
    return False


def is_gzip_path(path: str | Path) -> bool:
    with open(path, "rb") as handle:
        return handle.read(2) == GZIP_MAGIC


def open_maybe_gzip(path: str | Path, mode: str = "rt", encoding: str = "utf-8"):
    """Open a plain or gzip-compressed text file transparently."""
    if mode not in {"rt", "r"}:
        raise ValueError("open_maybe_gzip supports text reading only")
    if is_gzip_path(path):
        return gzip.open(path, mode, encoding=encoding, newline=None)
    return open(path, mode, encoding=encoding, newline=None)


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_copy(source: str | Path, target: str | Path) -> None:
    """Copy a file atomically: write a temp file, fsync, then rename."""
    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
            shutil.copyfileobj(src, out, length=1024 * 1024)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_copytree(source: str | Path, target: str | Path) -> None:
    """Copy a directory tree to a temporary sibling, then rename it in place."""
    source = Path(source)
    target = Path(target)
    if not source.is_dir():
        raise NotADirectoryError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=str(target.parent)))
    try:
        shutil.rmtree(tmp)
        shutil.copytree(source, tmp, symlinks=True)
        os.replace(tmp, target)
    except BaseException:
        if tmp.exists() or tmp.is_symlink():
            shutil.rmtree(tmp, ignore_errors=True)
        raise


def format_table(headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    """Render a lightweight aligned text table (no external dependency)."""
    headers = list(headers)
    rows = [list(rows_i) for rows_i in rows]
    strings: list[list[str]] = []
    for row in rows:
        strings.append(["" if value is None else str(value) for value in row])
    if not strings:
        strings = [[h for h in headers]]
    widths = [len(str(h)) for h in headers]
    for row in strings:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(value))

    def fmt(row: list[str]) -> str:
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(row))

    lines = [fmt([str(h) for h in headers]), "  ".join("-" * w for w in widths)]
    lines.extend(fmt(row) for row in strings)
    return "\n".join(lines)


def parse_key_values(items: list[str]) -> dict[str, str]:
    """Parse repeated key=value CLI arguments."""
    result: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"expected key=value, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip().strip("-")
        if not key:
            raise ValueError(f"empty field name in {item!r}")
        result[key] = value
    return result


def chunked(iterable: Iterable[Any], size: int) -> Iterator[list[Any]]:
    chunk: list[Any] = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    if n % 2:
        return float(ordered[n // 2])
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def pct(numerator: int | float, denominator: int | float) -> float:
    return (float(numerator) / float(denominator) * 100.0) if denominator else 0.0
