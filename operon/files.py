#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Manifest artifact operations: immutable archival, content identity, verification.

Path is a mutable property; identity is `file_id + sha256 + size_bytes` for
both regular files and directory trees.
Downloads/copies are atomic and idempotent (same checksum -> skip; different
checksum at target -> relocate a manifest-claimed occupant to its own canonical
path, quarantine an untracked leftover, else hard conflict).
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from operon.config import Project, project_rel, resolve_actor
from operon.database import Database
from operon.errors import (
    ChecksumError,
    ConflictError,
    EntityNotFoundError,
    ValidationError,
)
from operon.utils import (
    atomic_copy,
    atomic_copy_bgzip,
    atomic_copytree,
    is_gzip_path,
    now_iso,
    path_size_bytes,
    sha256_path,
)
from operon.workflow import set_state

ROLE_FORMATS = {
    "genome_fasta": "fasta",
    "genome_fasta_genbank": "fasta",
    "genome_fasta_refseq": "fasta",
    "cds_fasta": "fasta",
    "protein_fasta": "fasta",
    "annotation_gff3": "gff3",
    "reads_r1": "fastq",
    "reads_r2": "fastq",
    "reads_single": "fastq",
    "assembly_report": "txt",
    "assembly_report_genbank": "txt",
    "assembly_report_refseq": "txt",
    "other": "other",
}

_CHECKSUM_ADVANCE_FROM = {
    None,
    "DISCOVERED",
    "METADATA_FETCHED",
    "METADATA_VALIDATED",
    "DOWNLOAD_PENDING",
    "DOWNLOADED",
    "DOWNLOAD_FAILED",
    "CHECKSUM_FAILED",
}


def _advance_checksum_state(
    db: Database, entity_type: str, entity_id: str, message: str
) -> None:
    """Advance early/failure states without demoting completed QC or decisions."""
    current = db.get_entity_state(entity_type, entity_id)
    if current in _CHECKSUM_ADVANCE_FROM:
        db.set_entity_state(entity_type, entity_id, "CHECKSUM_VERIFIED", message)


ENTITY_BUCKETS = {
    "run": "reads",
    "sample": "reads",
    "assembly": "assemblies",
    "annotation": "annotations",
    "organism": "assemblies",
}

FORMAT_EXTENSIONS = {
    "fasta": {".fasta", ".fa", ".fna", ".faa"},
    "fastq": {".fastq", ".fq"},
    "gff3": {".gff3", ".gff"},
    "bam": {".bam"},
    "cram": {".cram"},
    "tsv": {".tsv"},
    "txt": {".txt"},
    "html": {".html", ".htm"},
    "json": {".json", ".jsonl"},
}


def _local_file_fingerprint(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "size_bytes": int(stat.st_size),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "mtime_ns": int(stat.st_mtime_ns),
        "ctime_ns": int(stat.st_ctime_ns),
    }


def _same_local_fingerprint(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(
        int(left[name]) == int(right[name])
        for name in (
            "size_bytes",
            "device",
            "inode",
            "mtime_ns",
            "ctime_ns",
        )
    )


def remember_local_file_verification(
    db: Database, record: dict[str, Any], path: Path, *, verified_at: str | None = None
) -> None:
    """Cache a completed full-file checksum against a strong stat fingerprint."""
    try:
        if not path.is_file():
            return
        fingerprint = _local_file_fingerprint(path)
    except OSError:
        return
    if fingerprint["size_bytes"] != int(record["size_bytes"]):
        return
    with db.transaction():
        db.conn.execute(
            "INSERT INTO local_file_verifications "
            "(file_id, sha256, size_bytes, device, inode, mtime_ns, ctime_ns, verified_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET sha256=excluded.sha256, "
            "size_bytes=excluded.size_bytes, device=excluded.device, inode=excluded.inode, "
            "mtime_ns=excluded.mtime_ns, ctime_ns=excluded.ctime_ns, "
            "verified_at=excluded.verified_at",
            (
                record["file_id"],
                record["sha256"],
                fingerprint["size_bytes"],
                fingerprint["device"],
                fingerprint["inode"],
                fingerprint["mtime_ns"],
                fingerprint["ctime_ns"],
                verified_at or now_iso(),
            ),
        )


def clear_local_file_verification(db: Database, file_id: str) -> None:
    with db.transaction():
        db.conn.execute(
            "DELETE FROM local_file_verifications WHERE file_id=?", (file_id,)
        )


def verify_local_file_identity(
    db: Database, record: dict[str, Any], path: Path, *, rehash: bool = False
) -> tuple[bool, dict[str, Any]]:
    """Verify a local artifact, reusing a full-hash result only while its stat identity is unchanged.

    The cache is derived data.  Any size/device/inode/mtime/ctime change forces a full SHA-256,
    while ``rehash=True`` bypasses it unconditionally.

    Directory artifacts (``format == "directory"``) are verified with the same
    deterministic tree hash used at ingest; the stat-fingerprint cache does not
    apply to them because a directory's own mtime changes on any member touch.
    """
    expect_directory = str(record.get("format")) == "directory"
    try:
        # A file<->directory type flip is reported as missing: the recorded
        # bytes, whatever they were, are no longer at this path.
        exists = path.is_dir() if expect_directory else path.is_file()
    except OSError:
        exists = False
    info: dict[str, Any] = {
        "exists": exists,
        "sha256_match": False,
        "size_bytes": 0,
        "verification_method": "missing",
        "verification_cached_at": None,
    }
    if not info["exists"]:
        clear_local_file_verification(db, str(record["file_id"]))
        return False, info

    if expect_directory:
        return _verify_directory_identity(db, record, path, info)

    try:
        before = _local_file_fingerprint(path)
    except OSError as exc:
        info.update(
            verification_method="stat_error", error=f"{type(exc).__name__}: {exc}"
        )
        clear_local_file_verification(db, str(record["file_id"]))
        return False, info
    info["size_bytes"] = before["size_bytes"]
    if before["size_bytes"] != int(record["size_bytes"]):
        info["verification_method"] = "size_mismatch"
        clear_local_file_verification(db, str(record["file_id"]))
        return False, info

    if not rehash:
        cached = db.conn.execute(
            "SELECT * FROM local_file_verifications WHERE file_id=?",
            (record["file_id"],),
        ).fetchone()
        if cached is not None:
            cached_record = dict(cached)
            if str(cached_record["sha256"]).lower() == str(
                record["sha256"]
            ).lower() and _same_local_fingerprint(cached_record, before):
                info.update(
                    sha256_match=True,
                    verification_method="cached_stat_fingerprint",
                    verification_cached_at=cached_record["verified_at"],
                )
                return True, info

    try:
        current_sha = sha256_path(path)
        after = _local_file_fingerprint(path)
    except OSError as exc:
        info.update(
            verification_method="sha256_error", error=f"{type(exc).__name__}: {exc}"
        )
        clear_local_file_verification(db, str(record["file_id"]))
        return False, info
    if not _same_local_fingerprint(before, after):
        info.update(
            size_bytes=after["size_bytes"],
            verification_method="changed_during_sha256",
        )
        clear_local_file_verification(db, str(record["file_id"]))
        return False, info

    matched = current_sha.lower() == str(record["sha256"]).lower()
    info.update(
        sha256_match=matched,
        verification_method="full_sha256",
        current_sha256=current_sha,
    )
    if matched:
        remember_local_file_verification(db, record, path)
    else:
        clear_local_file_verification(db, str(record["file_id"]))
    return matched, info


def _verify_directory_identity(
    db: Database, record: dict[str, Any], path: Path, info: dict[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Verify a directory artifact against its manifest identity.

    Always a full tree hash: no stat fingerprint is trustworthy for a
    directory (its own size/mtime say nothing about members), and the
    verification cache is never consulted nor written for directories.
    """
    try:
        current_size = path_size_bytes(path)
    except OSError as exc:
        info.update(
            verification_method="stat_error", error=f"{type(exc).__name__}: {exc}"
        )
        return False, info
    info["size_bytes"] = current_size
    if current_size != int(record["size_bytes"]):
        info["verification_method"] = "size_mismatch"
        return False, info
    try:
        current_sha = sha256_path(path)
    except OSError as exc:
        info.update(
            verification_method="sha256_error", error=f"{type(exc).__name__}: {exc}"
        )
        return False, info
    matched = current_sha.lower() == str(record["sha256"]).lower()
    info.update(
        sha256_match=matched,
        verification_method="full_sha256",
        current_sha256=current_sha,
    )
    return matched, info


GZIP_SUFFIXES = {".gz", ".gzip", ".bgz", ".bgzf"}


def canonical_filename(entity_id: str, role: str, fmt: str, compression: str) -> str:
    for name, value in (("entity_id", entity_id), ("role", role), ("format", fmt)):
        if (
            not value
            or value in {".", ".."}
            or any(
                char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in value
            )
        ):
            raise ValidationError(f"invalid archive {name}: {value!r}")
    if fmt == "directory":
        return f"{entity_id}.{role}.dir"
    suffix = ".gz" if compression in {"gzip", "bgzip"} else ""
    return f"{entity_id}.{role}.{fmt}{suffix}"


def _require_project_path(project: Project, target: Path) -> None:
    """Reject lexical and symlink escapes before accessing an archive target."""
    try:
        target.resolve().relative_to(project.root.resolve())
    except (ValueError, RuntimeError) as exc:
        raise ValidationError(
            f"archive target must stay inside the project root: {target}"
        ) from exc


def archive_target(
    project: Project,
    entity_type: str,
    entity_id: str,
    role: str,
    fmt: str,
    compression: str,
    archive_root: str | Path | None = None,
) -> Path:
    """Build and validate the destination shared by ingestion and batch planning."""
    filename = canonical_filename(entity_id, role, fmt, compression)
    root = (
        Path(archive_root)
        if archive_root is not None
        else project.raw_root / raw_bucket(entity_type)
    )
    target = root / entity_id / filename
    _require_project_path(project, target)
    return target


def detect_format(path: str | Path, role: str | None = None) -> str:
    if Path(path).is_dir():
        return "directory"
    name = Path(path).name.lower()
    # A .gz/.bgz suffix is compression metadata, not format metadata.
    for suffix in sorted(GZIP_SUFFIXES, key=len, reverse=True):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    for fmt, extensions in FORMAT_EXTENSIONS.items():
        if any(name.endswith(ext) for ext in sorted(extensions, key=len, reverse=True)):
            return fmt
    if role in ROLE_FORMATS:
        return ROLE_FORMATS[role]
    return "other"


def detect_compression(path: str | Path) -> str:
    if Path(path).is_dir():
        return "none"
    name = Path(path).name.lower()
    if any(name.endswith(suffix) for suffix in GZIP_SUFFIXES):
        if is_gzip_path(path):
            return "gzip"
        raise ValidationError(
            f"{path}: filename claims gzip but magic bytes are not gzip"
        )
    return "gzip" if is_gzip_path(path) else "none"


def raw_bucket(entity_type: str) -> str:
    return ENTITY_BUCKETS.get(entity_type, "other")


def find_existing_file(
    db: Database, entity_type: str, entity_id: str, role: str, sha256: str
) -> dict[str, Any] | None:
    row = db.conn.execute(
        "SELECT * FROM files WHERE entity_type=? AND entity_id=? AND file_role=? AND sha256=? ORDER BY file_id LIMIT 1",
        (entity_type, entity_id, role, sha256),
    ).fetchone()
    return dict(row) if row else None


def stored_content_sha256(project: Project, record: dict[str, Any]) -> str | None:
    """SHA-256 of a manifest row's content, decompressing stored gzip/BGZF bytes.

    Compression is a storage property, not an identity of its own: a preflight
    that holds the plain source bytes must still recognize a row ingest archived
    as BGZF.  Returns ``None`` when the local bytes are absent (a remote-only
    row cannot prove a content match).
    """
    path = project.root / str(record["relative_path"])
    if not path.is_file():
        return None
    if str(record["compression"]) in {"gzip", "bgzip"}:
        digest = hashlib.sha256()
        with gzip.open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    return sha256_path(path)


def ingest_file(
    db: Database,
    project: Project,
    source: str | Path,
    entity_type: str,
    entity_id: str,
    role: str,
    fmt: str | None = None,
    compression: str | None = None,
    source_url: str | None = None,
    move: bool = False,
    run_id: str | None = None,
    actor: str | None = None,
    provenance_buffer: list[dict[str, Any]] | None = None,
    archive_root: str | Path | None = None,
    compress_archive: bool | None = None,
) -> dict[str, Any]:
    """Archive one file or directory into raw/ and register it in the manifest.

    ``archive_root`` overrides the archive location (default
    ``raw/<bucket>/<entity_id>/``); adopters of derived artifacts pass a
    derived area such as ``analysis/adopted/`` while reusing the exact same
    idempotency and conflict semantics.

    ``compress_archive`` selects the storage transform for plain FASTA/FASTQ
    sources: ``None`` follows the project's ``storage.compress_archive``
    setting.  When enabled, an uncompressed FASTA/FASTQ *file* is first
    rewritten as deterministic BGZF into a temporary sibling of the archive
    target and the manifest row records the compressed bytes
    (``compression="bgzip"``, ``.gz`` canonical name); sources already
    gzip/bgzip-compressed, directories, and other formats are stored as-is,
    and an explicit ``compression`` argument always pins the storage form
    (derived-artifact paths such as fanout/adopt use it to opt out).

    The function is idempotent:
      * target exists with same checksum -> existing manifest row is returned;
      * target exists with different checksum -> the occupant is relocated to
        its own canonical path when another manifest row claims those exact
        bytes (e.g. a file left behind by a role rename), or quarantined
        aside when no manifest row claims it (interrupted-run leftover);
        anything else raises ConflictError, never overwrite;
      * otherwise copy to a temporary sibling and atomically rename.
    """
    source = Path(source)
    if not source.exists() or not (source.is_file() or source.is_dir()):
        raise ValidationError(f"source artifact does not exist: {source}")
    db.require_active_entity(entity_type, entity_id)

    fmt = fmt or detect_format(source, role)
    compression_pinned = compression is not None
    compression = compression or detect_compression(source)
    target = archive_target(
        project, entity_type, entity_id, role, fmt, compression, archive_root
    )
    if source.is_dir() and fmt != "directory":
        raise ValidationError(
            f"directory input requires format=directory, got {fmt!r}: {source}"
        )
    if source.is_file() and fmt == "directory":
        raise ValidationError(f"format=directory requires a directory source: {source}")
    if source.is_dir() and compression != "none":
        raise ValidationError("directory artifacts require compression=none")
    if compression in {"gzip", "bgzip"} and fmt not in {
        "fasta",
        "fastq",
        "gff3",
        "tsv",
        "txt",
        "json",
    }:
        # Not fatal for binary formats that commonly use bgzip (bam/cram are already compressed).
        pass

    if compress_archive is None:
        compress_archive = project.compress_archive
    staged_compression: Path | None = None
    effective_source = source
    try:
        if (
            compress_archive
            and not compression_pinned
            and compression == "none"
            and fmt in {"fasta", "fastq"}
            and source.is_file()
        ):
            # Plain sequence bytes are archived as deterministic BGZF: the
            # manifest identity binds the compressed bytes actually stored.
            target = archive_target(
                project, entity_type, entity_id, role, fmt, "bgzip", archive_root
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, staged = tempfile.mkstemp(
                prefix=f".{target.name}.bgzip-", dir=str(target.parent)
            )
            os.close(fd)
            staged_compression = Path(staged)
            atomic_copy_bgzip(source, staged_compression)
            effective_source = staged_compression
            compression = "bgzip"

        source_sha = sha256_path(effective_source)
        source_size = path_size_bytes(effective_source)
        existing = find_existing_file(db, entity_type, entity_id, role, source_sha)
        same_role = db.conn.execute(
            "SELECT * FROM files WHERE entity_type=? AND entity_id=? AND file_role=? ORDER BY file_id LIMIT 1",
            (entity_type, entity_id, role),
        ).fetchone()
        if (
            same_role is not None
            and str(same_role["sha256"]).lower() != source_sha.lower()
        ):
            # raw/ is immutable: a different byte stream for the same entity+role is
            # a new version (new entity ID) or an explicit audited replacement, not
            # a silent overwrite of the archive.
            raise ConflictError(
                f"{entity_type} {entity_id} already has {same_role['file_id']} for role {role!r} "
                f"with sha256 {same_role['sha256'][:12]}...; source has {source_sha[:12]}... "
                "Create a new entity/version for the new bytes."
            )
        if existing is not None:
            target = project.root / existing["relative_path"]
            _require_project_path(project, target)
            if target.exists() and sha256_path(target) == source_sha:
                with db.transaction():
                    if str(existing["status"]) != "STANDARDIZED":
                        # Audited, and a no-op when already CHECKSUM_VERIFIED;
                        # a standardized file keeps its status: re-verifying the
                        # same raw bytes must not demote it.
                        db.set_file_status(
                            existing["file_id"],
                            "CHECKSUM_VERIFIED",
                            reason="re-ingested identical bytes; checksum re-verified",
                            actor=actor or "operon ingest",
                        )
                    db.conn.execute(
                        "UPDATE files SET downloaded_at=COALESCE(downloaded_at, ?), source_url=COALESCE(source_url, ?) WHERE file_id=?",
                        (now_iso(), source_url, existing["file_id"]),
                    )
                    # Idempotency must also repair a missing denormalized entity
                    # link.  Older TSV round-trips could clear these columns while
                    # leaving the immutable file manifest row intact.
                    _link_file_to_entity(
                        db, entity_type, entity_id, role, existing["file_id"]
                    )
                existing_record = dict(existing)
                remember_local_file_verification(db, existing_record, target)
                _advance_checksum_state(
                    db,
                    entity_type,
                    entity_id,
                    f"file {existing['file_id']} already archived and verified",
                )
                _workflow_log(
                    db,
                    project,
                    run_id,
                    "ingest",
                    entity_type,
                    entity_id,
                    "completed",
                    provenance_buffer=provenance_buffer,
                    command=f"ingest {source}",
                    input_sha256=source_sha,
                )
                return existing_record

        target = archive_target(
            project, entity_type, entity_id, role, fmt, compression, archive_root
        )
        if target.exists():
            target_sha = sha256_path(target)
            if target_sha == source_sha:
                row = _register_file(
                    db,
                    project,
                    entity_type,
                    entity_id,
                    role,
                    fmt,
                    compression,
                    target,
                    source_url,
                    source_sha,
                    source_size,
                )
                remember_local_file_verification(db, row, target)
                _advance_checksum_state(
                    db,
                    entity_type,
                    entity_id,
                    f"file {row['file_id']} matched existing target",
                )
                _workflow_log(
                    db,
                    project,
                    run_id,
                    "ingest",
                    entity_type,
                    entity_id,
                    "completed",
                    provenance_buffer=provenance_buffer,
                    command=f"ingest {source}",
                    input_sha256=source_sha,
                )
                return row
            if not target.is_dir():
                _resolve_occupied_target(db, project, target, target_sha)
            else:
                raise ConflictError(
                    f"target {target} already exists with a different checksum "
                    f"({target_sha[:12]}... != {source_sha[:12]}...); refusing to overwrite"
                )

        # A move is deliberately implemented as copy -> verify -> register ->
        # remove-source.  This is slightly more work on one filesystem than
        # ``os.replace``, but it leaves the source recoverable if copying,
        # verification, or the manifest transaction fails (including cross-device
        # moves, where rename is unavailable).
        if effective_source.is_dir():
            atomic_copytree(effective_source, target)
        else:
            atomic_copy(effective_source, target)
        target_sha = sha256_path(target)
        target_size = path_size_bytes(target)
        if target_sha != source_sha or target_size != source_size:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            raise ChecksumError(
                f"checksum mismatch while archiving {source} to {target}"
            )
        row = _register_file(
            db,
            project,
            entity_type,
            entity_id,
            role,
            fmt,
            compression,
            target,
            source_url,
            target_sha,
            target_size,
        )
        remember_local_file_verification(db, row, target)
        _advance_checksum_state(
            db,
            entity_type,
            entity_id,
            f"file {row['file_id']} archived and checksum verified",
        )
        _workflow_log(
            db,
            project,
            run_id,
            "ingest",
            entity_type,
            entity_id,
            "completed",
            provenance_buffer=provenance_buffer,
            command=f"ingest {source}",
            input_sha256=target_sha,
        )
        if move:
            if source.is_dir() and not source.is_symlink():
                shutil.rmtree(source)
            else:
                source.unlink()
        return row
    finally:
        if staged_compression is not None:
            staged_compression.unlink(missing_ok=True)


def _resolve_occupied_target(
    db: Database, project: Project, target: Path, target_sha: str
) -> None:
    """Free an occupied canonical path without violating raw/ immutability.

    Two occupants are recoverable:
      * a file still claimed by another manifest row whose role was renamed
        after archiving (e.g. NCBI source-specific reconciliation) — relocate
        it to its own canonical path and update the row, audited;
      * an untracked leftover from an interrupted run (no manifest row claims
        the path) — quarantine it aside, bytes preserved, audited.

    Anything else (a claimant whose recorded checksum does not match the
    occupying bytes) still raises ConflictError.
    """
    rel = project_rel(project, target)
    claimant = db.conn.execute(
        "SELECT * FROM files WHERE relative_path=? LIMIT 1", (rel,)
    ).fetchone()
    if claimant is not None:
        if str(claimant["sha256"]).lower() != target_sha.lower():
            raise ConflictError(
                f"target {target} already exists with a different checksum and its "
                f"bytes do not match manifest row {claimant['file_id']} either; refusing to overwrite"
            )
        # The occupant is accounted for under another role; move it to its own
        # canonical path so this role can take the name.
        new_name = canonical_filename(
            str(claimant["entity_id"]),
            str(claimant["file_role"]),
            str(claimant["format"]),
            str(claimant["compression"]),
        )
        new_path = target.parent / new_name
        if new_path == target:
            raise ConflictError(
                f"manifest row {claimant['file_id']} claims {target} under role "
                f"{claimant['file_role']!r} whose canonical path is the same; refusing to overwrite"
            )
        if new_path.exists() and sha256_path(new_path).lower() != target_sha.lower():
            raise ConflictError(
                f"cannot relocate {target} to {new_path}: destination exists with "
                "different bytes; refusing to overwrite"
            )
        os.replace(target, new_path)
        new_rel = project_rel(project, new_path)
        with db.transaction():
            db.conn.execute(
                "UPDATE files SET relative_path=? WHERE file_id=?",
                (new_rel, claimant["file_id"]),
            )
        db.record_change(
            "files",
            claimant["file_id"],
            "relative_path",
            rel,
            new_rel,
            "relocate file left at a stale canonical path after a role rename",
            actor=resolve_actor(),
        )
        return
    quarantine = target.with_name(f"{target.name}.orphan-{target_sha[:12]}")
    os.replace(target, quarantine)
    db.record_change(
        "raw_file",
        rel,
        "quarantined",
        None,
        project_rel(project, quarantine),
        "untracked leftover (interrupted run) moved aside to archive new content",
        evidence=target_sha,
        actor=resolve_actor(),
    )


def _register_file(
    db: Database,
    project: Project,
    entity_type: str,
    entity_id: str,
    role: str,
    fmt: str,
    compression: str,
    target: Path,
    source_url: str | None,
    sha: str,
    size: int,
) -> dict[str, Any]:
    row = db.conn.execute(
        "SELECT * FROM files WHERE relative_path=? AND sha256=? AND size_bytes=? LIMIT 1",
        (project_rel(project, target), sha, size),
    ).fetchone()
    if row:
        return dict(row)
    file_id = db.next_id("file")
    rel = project_rel(project, target)
    record = {
        "file_id": file_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "file_role": role,
        "format": fmt,
        "compression": compression,
        "relative_path": rel,
        "source_url": source_url,
        "size_bytes": size,
        "sha256": sha,
        "downloaded_at": now_iso(),
        "status": "CHECKSUM_VERIFIED",
    }
    columns = list(record.keys())
    with db.transaction():
        db.conn.execute(
            f"INSERT INTO files ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",  # nosec B608 # fixed record keys or role mapping; values are bound
            [record[c] for c in columns],
        )
        _link_file_to_entity(db, entity_type, entity_id, role, file_id)
    record["file_id"] = file_id
    return record


def _link_file_to_entity(
    db: Database, entity_type: str, entity_id: str, role: str, file_id: str
) -> None:
    if entity_type == "assembly" and role == "genome_fasta":
        db.conn.execute(
            "UPDATE assemblies SET fasta_file_id=? WHERE assembly_id=?",
            (file_id, entity_id),
        )
    elif entity_type == "annotation":
        field = {
            "annotation_gff3": "gff_file_id",
            "cds_fasta": "cds_file_id",
            "protein_fasta": "protein_file_id",
        }.get(role)
        if field:
            db.conn.execute(
                f"UPDATE annotations SET {field}=? WHERE annotation_id=?",
                (file_id, entity_id),
            )  # nosec B608 # fixed record keys or role mapping; values are bound


def verify_files(
    db: Database, project: Project, file_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """Verify local bytes or live-check at least one recorded remote copy."""
    if file_ids:
        placeholders = ", ".join("?" for _ in file_ids)
        rows = db.conn.execute(
            f"SELECT * FROM files WHERE file_id IN ({placeholders})", file_ids
        ).fetchall()  # nosec B608 # fixed record keys or role mapping; values are bound
    else:
        rows = db.conn.execute("SELECT * FROM files").fetchall()
    results: list[dict[str, Any]] = []
    from operon.remotes import (
        SFTPStore,
        _ensure_remote_only_schema,
        get_remote,
        placeholder_path,
        verify_remote_record,
    )

    stores: dict[str, SFTPStore] = {}
    manifests: dict[str, dict[str, Any]] = {}
    connection_errors: dict[str, str] = {}
    with ExitStack() as stack:
        for row in rows:
            record = dict(row)
            path = project.root / record["relative_path"]
            result = {
                "file_id": record["file_id"],
                "relative_path": record["relative_path"],
                "status": "",
                "recorded_sha256": record["sha256"],
                "current_sha256": None,
                "error": None,
                "remote": "",
            }
            if not path.exists():
                clear_local_file_verification(db, record["file_id"])
                locations = db.conn.execute(
                    "SELECT location_name FROM file_locations WHERE file_id=? AND status='AVAILABLE' "
                    "ORDER BY verified_at DESC",
                    (record["file_id"],),
                ).fetchall()
                remote_errors: list[str] = []
                remote_verified = ""
                unavailable = False
                for location in locations:
                    name = str(location["location_name"])
                    if name in connection_errors:
                        unavailable = True
                        remote_errors.append(f"{name}: {connection_errors[name]}")
                        continue
                    opening_store = name not in stores
                    try:
                        if name not in stores:
                            stores[name] = stack.enter_context(
                                SFTPStore(get_remote(project, name))
                            )
                        if name not in manifests:
                            manifests[name] = stores[name].read_manifest()
                        verify_remote_record(
                            project,
                            name,
                            record,
                            db=db,
                            store=stores[name],
                            manifest=manifests[name],
                        )
                    except Exception as exc:  # noqa: BLE001 - one broken remote location must not stop the batch  # pylint: disable=broad-exception-caught
                        if opening_store and name not in stores:
                            connection_errors[name] = f"{type(exc).__name__}: {exc}"
                        location_status = db.conn.execute(
                            "SELECT status FROM file_locations WHERE file_id=? AND location_name=?",
                            (record["file_id"], name),
                        ).fetchone()
                        if (
                            location_status is None
                            or location_status["status"] == "AVAILABLE"
                        ):
                            unavailable = True
                        remote_errors.append(f"{name}: {type(exc).__name__}: {exc}")
                        continue
                    remote_verified = name
                    break

                if remote_verified:
                    _ensure_remote_only_schema(project)
                    result.update(status="REMOTE_ONLY", remote=remote_verified)
                    db.set_file_status(
                        record["file_id"],
                        "REMOTE_ONLY",
                        reason=f"local bytes absent; live remote copy verified on {remote_verified}",
                        actor="operon verify",
                        evidence=f"remote://{remote_verified}/{record['relative_path']}",
                    )
                elif unavailable:
                    result.update(
                        status="REMOTE_UNVERIFIED",
                        error="; ".join(remote_errors)
                        or "recorded remote copy could not be verified",
                    )
                else:
                    result.update(
                        status="MISSING",
                        error=(
                            "local bytes absent and no verified remote copy remains"
                            + (f": {'; '.join(remote_errors)}" if remote_errors else "")
                        ),
                    )
                    db.set_file_status(
                        record["file_id"],
                        "MISSING",
                        reason="local bytes absent and live remote verification found no usable copy",
                        actor="operon verify",
                    )
            else:
                current = sha256_path(path)
                result["current_sha256"] = current
                if current.lower() == str(record["sha256"]).lower():
                    result["status"] = "CHECKSUM_VERIFIED"
                    remember_local_file_verification(db, record, path)
                    if record.get("status") != "STANDARDIZED":
                        db.set_file_status(
                            record["file_id"],
                            "CHECKSUM_VERIFIED",
                            reason="local artifact checksum verified",
                            actor="operon verify",
                        )
                    placeholder_path(project, record["file_id"]).unlink(missing_ok=True)
                else:
                    result.update(
                        status="CHECKSUM_FAILED", error="checksum differs from manifest"
                    )
                    clear_local_file_verification(db, record["file_id"])
                    db.set_file_status(
                        record["file_id"],
                        "CHECKSUM_FAILED",
                        reason="local artifact checksum differs from manifest",
                        actor="operon verify",
                        evidence=f"recorded={record['sha256']}; actual={current}",
                    )
            results.append(result)
    return results


RECOMPRESS_FORMATS = ("fasta", "fastq")


def recompress_files(
    db: Database,
    project: Project,
    file_ids: list[str] | None = None,
    *,
    dry_run: bool = False,
    actor: str | None = None,
) -> list[dict[str, Any]]:
    """Re-archive plain FASTA/FASTQ raw files as deterministic BGZF, in place.

    A one-shot storage migration for projects created before the
    ``storage.compress_archive`` default: each eligible manifest row
    (``compression='none'``, FASTA/FASTQ, local bytes present, active entity)
    is fully re-hashed, rewritten as BGZF next to the archive target, the
    decompressed bytes are re-hashed to prove content preservation, and the
    row's ``relative_path``/``sha256``/``size_bytes``/``compression`` are
    updated with one ``changes`` audit row per field.  The file's
    ``file_locations`` rows are deleted (audited): the stale remote bytes
    become orphans the next ``push`` supersedes.

    Release members are refused (releases are immutable); already-compressed
    rows are no-ops.  ``dry_run=True`` only reports what would happen.

    Derived data keyed by the old ``sha256`` (``qc_results``,
    ``analysis_jobs``, the ``fasta_lengths`` cache) is naturally invalidated
    and rebuilt lazily; ``sequences`` (keyed by ``file_id``) is unaffected.
    """
    resolved_actor = resolve_actor(actor) or "operon recompress"
    if file_ids:
        placeholders = ", ".join("?" for _ in file_ids)
        rows = [
            dict(row)
            for row in db.conn.execute(
                f"SELECT * FROM files WHERE file_id IN ({placeholders}) ORDER BY file_id",  # nosec B608 # values are bound
                file_ids,
            ).fetchall()
        ]
        found = {row["file_id"] for row in rows}
        missing = [file_id for file_id in file_ids if file_id not in found]
    else:
        rows = [
            dict(row)
            for row in db.conn.execute(
                "SELECT * FROM files WHERE compression='none' "
                "AND format IN ('fasta','fastq') ORDER BY file_id"
            ).fetchall()
        ]
        missing = []

    results: list[dict[str, Any]] = [
        {
            "file_id": file_id,
            "relative_path": None,
            "status": "error",
            "error": f"unknown file_id: {file_id}",
        }
        for file_id in missing
    ]
    for record in rows:
        results.append(_recompress_file(db, project, record, resolved_actor, dry_run))
    return results


def _recompress_file(
    db: Database, project: Project, record: dict[str, Any], actor: str, dry_run: bool
) -> dict[str, Any]:
    file_id = str(record["file_id"])
    rel = str(record["relative_path"])
    result: dict[str, Any] = {
        "file_id": file_id,
        "relative_path": rel,
        "status": "",
        "error": None,
    }
    try:
        if str(record["compression"]) != "none":
            result.update(status="skipped", error="already compressed")
            return result
        if str(record["format"]) not in RECOMPRESS_FORMATS:
            result.update(
                status="skipped",
                error=f"format {record['format']!r} is not eligible for recompress",
            )
            return result
        if db.is_entity_retired(str(record["entity_type"]), str(record["entity_id"])):
            result.update(status="skipped", error="entity is retired")
            return result
        releases = [
            str(row["release_version"])
            for row in db.conn.execute(
                "SELECT release_version FROM release_members WHERE file_id=? ORDER BY release_version",
                (file_id,),
            ).fetchall()
        ]
        if releases:
            raise ConflictError(
                f"{file_id} is a member of release(s) {', '.join(releases)}; "
                "releases are immutable"
            )
        path = project.root / rel
        if str(record.get("status")) == "REMOTE_ONLY" or not path.is_file():
            result.update(
                status="skipped",
                error="local bytes absent; pull the file before recompressing",
            )
            return result

        new_rel = rel + ".gz"
        target = project.root / new_rel
        _require_project_path(project, target)
        if dry_run:
            result.update(status="would_recompress", relative_path=new_rel)
            return result

        if sha256_path(path) != str(record["sha256"]) or path.stat().st_size != int(
            record["size_bytes"]
        ):
            raise ChecksumError(
                f"{file_id}: local bytes do not match the manifest; refusing to recompress"
            )
        staged = target.with_name(f".{target.name}.recompress-{uuid.uuid4().hex[:12]}")
        try:
            atomic_copy_bgzip(path, staged)
            compressed_sha = sha256_path(staged)
            # Content-preservation proof: the decompressed bytes must hash to
            # the recorded plain-content checksum.
            digest = hashlib.sha256()
            with gzip.open(staged, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            if digest.hexdigest() != str(record["sha256"]):
                raise ChecksumError(
                    f"{file_id}: decompressed content does not match the manifest"
                )
            if target.exists():
                if sha256_path(target) == compressed_sha:
                    # Bytes already in place (interrupted earlier attempt).
                    staged.unlink(missing_ok=True)
                else:
                    raise ConflictError(
                        f"recompress target {target} exists with different bytes; "
                        "refusing to overwrite"
                    )
            else:
                os.replace(staged, target)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        path.unlink()

        new_size = target.stat().st_size
        with db.transaction():
            db.conn.execute(
                "UPDATE files SET relative_path=?, sha256=?, size_bytes=?, compression='bgzip' "
                "WHERE file_id=?",
                (new_rel, compressed_sha, new_size, file_id),
            )
        for field, old_value, new_value in (
            ("relative_path", rel, new_rel),
            ("sha256", record["sha256"], compressed_sha),
            ("size_bytes", record["size_bytes"], new_size),
            ("compression", record["compression"], "bgzip"),
        ):
            db.record_change(
                "files",
                file_id,
                field,
                old_value,
                new_value,
                "recompress: plain sequence archive rewritten as deterministic BGZF",
                actor=actor,
            )
        updated = dict(
            record,
            relative_path=new_rel,
            sha256=compressed_sha,
            size_bytes=new_size,
            compression="bgzip",
        )
        clear_local_file_verification(db, file_id)
        remember_local_file_verification(db, updated, target)

        # The recorded remote copies point at the old bytes; the next push
        # supersedes them (uploads the new path, prunes the old object).
        locations = db.conn.execute(
            "SELECT location_name, uri FROM file_locations WHERE file_id=?",
            (file_id,),
        ).fetchall()
        if locations:
            with db.transaction():
                db.conn.execute(
                    "DELETE FROM file_locations WHERE file_id=?", (file_id,)
                )
            for location in locations:
                db.record_change(
                    "file_location",
                    f"{file_id}@{location['location_name']}",
                    "location",
                    location["uri"],
                    None,
                    "recompress invalidated the stale remote copy; the next push supersedes it",
                    actor=actor,
                )

        if str(record.get("status")) == "STANDARDIZED":
            # The standardized view carries the raw file's name; rebuild it
            # under the new .gz name and drop the old target.
            old_standardized = (
                project.standardized_root
                / raw_bucket(str(record["entity_type"]))
                / str(record["entity_id"])
                / Path(rel).name
            )
            if old_standardized.exists() or old_standardized.is_symlink():
                if old_standardized.is_dir() and not old_standardized.is_symlink():
                    shutil.rmtree(old_standardized)
                else:
                    old_standardized.unlink()
            standardize_file(db, project, file_id)
        result.update(
            status="recompressed",
            relative_path=new_rel,
            sha256=compressed_sha,
            size_bytes=new_size,
        )
    except Exception as exc:  # noqa: BLE001 - one failed file must not stop the batch  # pylint: disable=broad-exception-caught
        result.update(status="error", error=f"{type(exc).__name__}: {exc}")
    return result


def standardize_file(
    db: Database, project: Project, file_id: str, link_kind: str = "copy"
) -> dict[str, Any]:
    """Create the standardized/ view for a verified file.

    raw/ remains immutable. standardized/ uses independent copies by default;
    explicit symlink/hardlink modes remain available for compatibility.
    """
    row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row:
        raise EntityNotFoundError(f"file {file_id} does not exist")
    record = dict(row)
    db.require_active_entity(record["entity_type"], record["entity_id"])
    source = project.root / record["relative_path"]
    if not source.exists():
        if record.get("status") == "REMOTE_ONLY":
            raise ChecksumError(
                f"{record['file_id']}: artifact is remote-only; run `operon pull --remote NAME "
                f"--file-id {record['file_id']}` before standardizing"
            )
        raise ChecksumError(f"{record['file_id']}: source missing: {source}")
    if sha256_path(source) != record["sha256"]:
        clear_local_file_verification(db, file_id)
        db.set_entity_state(
            record["entity_type"],
            record["entity_id"],
            "CHECKSUM_FAILED",
            f"{file_id} changed after manifest registration",
        )
        raise ChecksumError(f"{file_id}: source checksum does not match manifest")
    remember_local_file_verification(db, record, source)

    bucket = raw_bucket(record["entity_type"])
    target = (
        project.standardized_root
        / bucket
        / record["entity_id"]
        / Path(record["relative_path"]).name
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if sha256_path(target) != record["sha256"]:
            raise ConflictError(
                f"{target} exists with different content; refusing to overwrite"
            )
        db.set_file_status(
            file_id,
            "STANDARDIZED",
            reason="standardized target already present and verified",
            actor="operon standardize",
        )
        if (
            db.get_entity_state(record["entity_type"], record["entity_id"])
            == "CHECKSUM_VERIFIED"
        ):
            # Repair the crash window between target creation and the state
            # commit; entities that already moved on are left alone.
            set_state(
                db,
                record["entity_type"],
                record["entity_id"],
                "STANDARDIZED",
                f"file {file_id} already staged in standardized/",
                actor="operon standardize",
            )
        return {"file_id": file_id, "target": str(target), "action": "skipped"}

    created_target = False
    temporary_link: Path | None = None
    try:
        if link_kind == "symlink":
            # A symlink is published with the same temp-name + replace pattern
            # as regular files, so an interrupted operation cannot expose a
            # half-created standardized target.
            temporary_link = target.with_name(
                f".{target.name}.operon-{uuid.uuid4().hex}"
            )
            os.symlink(source, temporary_link)
            os.replace(temporary_link, target)
        elif link_kind == "hardlink":
            if source.is_dir():
                atomic_copytree(source, target)
            else:
                temporary_link = target.with_name(
                    f".{target.name}.operon-{uuid.uuid4().hex}"
                )
                try:
                    os.link(source, temporary_link)
                    os.replace(temporary_link, target)
                except OSError:
                    if temporary_link.exists() or temporary_link.is_symlink():
                        temporary_link.unlink(missing_ok=True)
                    temporary_link = None
                    atomic_copy(source, target)
        else:
            if source.is_dir():
                atomic_copytree(source, target)
            else:
                atomic_copy(source, target)
        created_target = True
        if sha256_path(target) != record["sha256"]:
            raise ChecksumError(f"{file_id}: standardized target checksum mismatch")
        # File status and entity state are committed together, both through
        # the audited paths, and the entity transition is checked by the state
        # machine.  If either database write fails, the newly published target
        # is removed below so a retry starts from the same filesystem state.
        with db.transaction():
            db.set_file_status(
                file_id,
                "STANDARDIZED",
                reason=f"staged in standardized/ ({link_kind})",
                actor="operon standardize",
            )
            set_state(
                db,
                record["entity_type"],
                record["entity_id"],
                "STANDARDIZED",
                f"file {file_id} staged in standardized/",
                actor="operon standardize",
            )
        return {"file_id": file_id, "target": str(target), "action": link_kind}
    except BaseException:
        if temporary_link is not None and (
            temporary_link.exists() or temporary_link.is_symlink()
        ):
            temporary_link.unlink(missing_ok=True)
        if created_target or target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        raise


def standardize_all(
    db: Database, project: Project, link_kind: str = "copy"
) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT file_id FROM files "
        "WHERE status IN ('CHECKSUM_VERIFIED','STANDARDIZED') "
        "AND NOT EXISTS (SELECT 1 FROM effective_retired_entities r "
        "WHERE r.entity_type=files.entity_type AND r.entity_id=files.entity_id) "
        "ORDER BY file_id"
    ).fetchall()
    results = []
    for row in rows:
        try:
            results.append(
                standardize_file(db, project, row["file_id"], link_kind=link_kind)
            )
        except Exception as exc:  # noqa: BLE001 - one broken file should not stop the batch  # pylint: disable=broad-exception-caught
            results.append({"file_id": row["file_id"], "error": str(exc)})
    return results


def _workflow_log(
    db: Database,
    project: Project,
    run_id: str | None,
    step: str,
    entity_type: str,
    entity_id: str,
    status: str,
    provenance_buffer: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> None:
    from operon.workflow import log_run, new_run_id

    log_run(
        db,
        project,
        {
            "run_id": new_run_id(),
            "parent_run_id": run_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "step": step,
            "status": status,
            "started_at": now_iso(),
            "finished_at": now_iso(),
            **extra,
        },
        jsonl_buffer=provenance_buffer,
    )
