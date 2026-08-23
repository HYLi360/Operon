"""Manifest artifact operations: immutable archival, content identity, verification.

Path is a mutable property; identity is `file_id + sha256 + size_bytes` for
both regular files and directory trees.
Downloads/copies are atomic and idempotent (same checksum -> skip, different
checksum at target -> hard conflict).
"""

from __future__ import annotations

import os
import shutil
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from operon.config import Project, project_rel
from operon.database import Database
from operon.errors import ChecksumError, ConflictError, EntityNotFoundError, ValidationError
from operon.utils import (
    atomic_copy,
    atomic_copytree,
    is_gzip_path,
    now_iso,
    path_size_bytes,
    sha256_path,
)

ROLE_FORMATS = {
    "genome_fasta": "fasta",
    "cds_fasta": "fasta",
    "protein_fasta": "fasta",
    "annotation_gff3": "gff3",
    "reads_r1": "fastq",
    "reads_r2": "fastq",
    "reads_single": "fastq",
    "assembly_report": "txt",
    "other": "other",
}

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

GZIP_SUFFIXES = {".gz", ".gzip", ".bgz", ".bgzf"}


def canonical_filename(entity_id: str, role: str, fmt: str, compression: str) -> str:
    if fmt == "directory":
        return f"{entity_id}.{role}.dir"
    suffix = ".gz" if compression in {"gzip", "bgzip"} else ""
    return f"{entity_id}.{role}.{fmt}{suffix}"


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
        raise ValidationError(f"{path}: filename claims gzip but magic bytes are not gzip")
    return "gzip" if is_gzip_path(path) else "none"


def raw_bucket(entity_type: str) -> str:
    return ENTITY_BUCKETS.get(entity_type, "other")


def find_existing_file(db: Database, entity_type: str, entity_id: str, role: str, sha256: str) -> dict[str, Any] | None:
    row = db.conn.execute(
        "SELECT * FROM files WHERE entity_type=? AND entity_id=? AND file_role=? AND sha256=? ORDER BY file_id LIMIT 1",
        (entity_type, entity_id, role, sha256),
    ).fetchone()
    return dict(row) if row else None


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
) -> dict[str, Any]:
    """Archive one file or directory into raw/ and register it in the manifest.

    The function is idempotent:
      * target exists with same checksum -> existing manifest row is returned;
      * target exists with different checksum -> ConflictError, never overwrite;
      * otherwise copy to a temporary sibling and atomically rename.
    """
    source = Path(source)
    if not source.exists() or not (source.is_file() or source.is_dir()):
        raise ValidationError(f"source artifact does not exist: {source}")
    db.require_entity(entity_type, entity_id)

    fmt = fmt or detect_format(source, role)
    compression = compression or detect_compression(source)
    if source.is_dir() and fmt != "directory":
        raise ValidationError(f"directory input requires format=directory, got {fmt!r}: {source}")
    if source.is_file() and fmt == "directory":
        raise ValidationError(f"format=directory requires a directory source: {source}")
    if source.is_dir() and compression != "none":
        raise ValidationError("directory artifacts require compression=none")
    if compression in {"gzip", "bgzip"} and fmt not in {"fasta", "fastq", "gff3", "tsv", "txt", "json"}:
        # Not fatal for binary formats that commonly use bgzip (bam/cram are already compressed).
        pass

    source_sha = sha256_path(source)
    source_size = path_size_bytes(source)
    existing = find_existing_file(db, entity_type, entity_id, role, source_sha)
    same_role = db.conn.execute(
        "SELECT * FROM files WHERE entity_type=? AND entity_id=? AND file_role=? ORDER BY file_id LIMIT 1",
        (entity_type, entity_id, role),
    ).fetchone()
    if same_role is not None and str(same_role["sha256"]).lower() != source_sha.lower():
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
        if target.exists() and sha256_path(target) == source_sha:
            db.conn.execute(
                "UPDATE files SET status='CHECKSUM_VERIFIED', downloaded_at=COALESCE(downloaded_at, ?), source_url=COALESCE(source_url, ?) WHERE file_id=?",
                (now_iso(), source_url, existing["file_id"]),
            )
            db.conn.commit()
            db.set_entity_state(entity_type, entity_id, "CHECKSUM_VERIFIED", f"file {existing['file_id']} already archived and verified")
            _workflow_log(db, project, run_id, "ingest", entity_type, entity_id, "completed", command=f"ingest {source}", input_sha256=source_sha)
            return dict(existing)

    target_dir = project.raw_root / raw_bucket(entity_type) / entity_id
    target = target_dir / canonical_filename(entity_id, role, fmt, compression)
    if target.exists():
        target_sha = sha256_path(target)
        if target_sha == source_sha:
            row = _register_file(db, project, entity_type, entity_id, role, fmt, compression, target, source_url, source_sha, source_size)
            db.set_entity_state(entity_type, entity_id, "CHECKSUM_VERIFIED", f"file {row['file_id']} matched existing target")
            _workflow_log(db, project, run_id, "ingest", entity_type, entity_id, "completed", command=f"ingest {source}", input_sha256=source_sha)
            return row
        raise ConflictError(
            f"target {target} already exists with a different checksum "
            f"({target_sha[:12]}... != {source_sha[:12]}...); refusing to overwrite"
        )

    if move:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source, target)
        except OSError:
            # Cross-device moves use a verified copy followed by source removal.
            if source.is_dir():
                atomic_copytree(source, target)
                shutil.rmtree(source)
            else:
                atomic_copy(source, target)
                source.unlink()
    else:
        if source.is_dir():
            atomic_copytree(source, target)
        else:
            atomic_copy(source, target)
    target_sha = sha256_path(target)
    target_size = path_size_bytes(target)
    if target_sha != source_sha or target_size != source_size:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        raise ChecksumError(f"checksum mismatch while archiving {source} to {target}")
    row = _register_file(db, project, entity_type, entity_id, role, fmt, compression, target, source_url, target_sha, target_size)
    db.set_entity_state(entity_type, entity_id, "CHECKSUM_VERIFIED", f"file {row['file_id']} archived and checksum verified")
    _workflow_log(db, project, run_id, "ingest", entity_type, entity_id, "completed", command=f"ingest {source}", input_sha256=target_sha)
    return row


def _register_file(db: Database, project: Project, entity_type: str, entity_id: str, role: str,
                   fmt: str, compression: str, target: Path, source_url: str | None,
                   sha: str, size: int) -> dict[str, Any]:
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
            f"INSERT INTO files ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [record[c] for c in columns],
        )
        _link_file_to_entity(db, entity_type, entity_id, role, file_id)
    record["file_id"] = file_id
    return record


def _link_file_to_entity(db: Database, entity_type: str, entity_id: str, role: str, file_id: str) -> None:
    if entity_type == "assembly" and role == "genome_fasta":
        db.conn.execute("UPDATE assemblies SET fasta_file_id=? WHERE assembly_id=?", (file_id, entity_id))
    elif entity_type == "annotation":
        field = {
            "annotation_gff3": "gff_file_id",
            "cds_fasta": "cds_file_id",
            "protein_fasta": "protein_file_id",
        }.get(role)
        if field:
            db.conn.execute(f"UPDATE annotations SET {field}=? WHERE annotation_id=?", (file_id, entity_id))


def verify_files(db: Database, project: Project, file_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Verify local bytes or live-check at least one recorded remote copy."""
    if file_ids:
        placeholders = ", ".join("?" for _ in file_ids)
        rows = db.conn.execute(f"SELECT * FROM files WHERE file_id IN ({placeholders})", file_ids).fetchall()
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
                "file_id": record["file_id"], "relative_path": record["relative_path"],
                "status": "", "recorded_sha256": record["sha256"],
                "current_sha256": None, "error": None, "remote": "",
            }
            if not path.exists():
                locations = db.conn.execute(
                    "SELECT location_name FROM file_locations WHERE file_id=? AND status='AVAILABLE' "
                    "ORDER BY verified_at DESC", (record["file_id"],),
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
                            stores[name] = stack.enter_context(SFTPStore(get_remote(project, name)))
                        if name not in manifests:
                            manifests[name] = stores[name].read_manifest()
                        verify_remote_record(
                            project, name, record, db=db, store=stores[name],
                            manifest=manifests[name],
                        )
                    except Exception as exc:
                        if opening_store and name not in stores:
                            connection_errors[name] = f"{type(exc).__name__}: {exc}"
                        location_status = db.conn.execute(
                            "SELECT status FROM file_locations WHERE file_id=? AND location_name=?",
                            (record["file_id"], name),
                        ).fetchone()
                        if location_status is None or location_status["status"] == "AVAILABLE":
                            unavailable = True
                        remote_errors.append(f"{name}: {type(exc).__name__}: {exc}")
                        continue
                    remote_verified = name
                    break

                if remote_verified:
                    _ensure_remote_only_schema(project)
                    result.update(status="REMOTE_ONLY", remote=remote_verified)
                    db.set_file_status(
                        record["file_id"], "REMOTE_ONLY",
                        reason=f"local bytes absent; live remote copy verified on {remote_verified}",
                        actor="operon verify",
                        evidence=f"remote://{remote_verified}/{record['relative_path']}",
                    )
                elif unavailable:
                    result.update(
                        status="REMOTE_UNVERIFIED",
                        error="; ".join(remote_errors) or "recorded remote copy could not be verified",
                    )
                else:
                    result.update(
                        status="MISSING",
                        error=("local bytes absent and no verified remote copy remains"
                               + (f": {'; '.join(remote_errors)}" if remote_errors else "")),
                    )
                    db.set_file_status(
                        record["file_id"], "MISSING",
                        reason="local bytes absent and live remote verification found no usable copy",
                        actor="operon verify",
                    )
            else:
                current = sha256_path(path)
                result["current_sha256"] = current
                if current.lower() == str(record["sha256"]).lower():
                    result["status"] = "CHECKSUM_VERIFIED"
                    if record.get("status") != "STANDARDIZED":
                        db.set_file_status(
                            record["file_id"], "CHECKSUM_VERIFIED",
                            reason="local artifact checksum verified",
                            actor="operon verify",
                        )
                    placeholder_path(project, record["file_id"]).unlink(missing_ok=True)
                else:
                    result.update(status="CHECKSUM_FAILED", error="checksum differs from manifest")
                    db.set_file_status(
                        record["file_id"], "CHECKSUM_FAILED",
                        reason="local artifact checksum differs from manifest",
                        actor="operon verify",
                        evidence=f"recorded={record['sha256']}; actual={current}",
                    )
            results.append(result)
    return results


def standardize_file(db: Database, project: Project, file_id: str, link_kind: str = "copy") -> dict[str, Any]:
    """Create the standardized/ view for a verified file.

    raw/ remains immutable. standardized/ uses independent copies by default;
    explicit symlink/hardlink modes remain available for compatibility.
    """
    row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row:
        raise EntityNotFoundError(f"file {file_id} does not exist")
    record = dict(row)
    source = project.root / record["relative_path"]
    if not source.exists():
        if record.get("status") == "REMOTE_ONLY":
            raise ChecksumError(
                f"{record['file_id']}: artifact is remote-only; run `operon pull --remote NAME "
                f"--file-id {record['file_id']}` before standardizing"
            )
        raise ChecksumError(f"{record['file_id']}: source missing: {source}")
    if sha256_path(source) != record["sha256"]:
        db.set_entity_state(record["entity_type"], record["entity_id"], "CHECKSUM_FAILED", f"{file_id} changed after manifest registration")
        raise ChecksumError(f"{file_id}: source checksum does not match manifest")

    bucket = raw_bucket(record["entity_type"])
    target = project.standardized_root / bucket / record["entity_id"] / Path(record["relative_path"]).name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if sha256_path(target) != record["sha256"]:
            raise ConflictError(f"{target} exists with different content; refusing to overwrite")
        db.conn.execute("UPDATE files SET status='STANDARDIZED' WHERE file_id=?", (file_id,))
        db.conn.commit()
        return {"file_id": file_id, "target": str(target), "action": "skipped"}

    if link_kind == "symlink":
        os.symlink(source, target)
    elif link_kind == "hardlink":
        if source.is_dir():
            atomic_copytree(source, target)
        else:
            try:
                os.link(source, target)
            except OSError:
                atomic_copy(source, target)
    else:
        if source.is_dir():
            atomic_copytree(source, target)
        else:
            atomic_copy(source, target)
    if sha256_path(target) != record["sha256"]:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        raise ChecksumError(f"{file_id}: standardized target checksum mismatch")
    db.conn.execute("UPDATE files SET status='STANDARDIZED' WHERE file_id=?", (file_id,))
    db.conn.commit()
    db.set_entity_state(record["entity_type"], record["entity_id"], "STANDARDIZED", f"file {file_id} staged in standardized/")
    return {"file_id": file_id, "target": str(target), "action": link_kind}


def standardize_all(db: Database, project: Project, link_kind: str = "copy") -> list[dict[str, Any]]:
    rows = db.conn.execute("SELECT file_id FROM files WHERE status IN ('CHECKSUM_VERIFIED','STANDARDIZED') ORDER BY file_id").fetchall()
    results = []
    for row in rows:
        try:
            results.append(standardize_file(db, project, row["file_id"], link_kind=link_kind))
        except Exception as exc:  # one broken file should not stop the batch
            results.append({"file_id": row["file_id"], "error": str(exc)})
    return results


def _workflow_log(db: Database, project: Project, run_id: str | None, step: str,
                  entity_type: str, entity_id: str, status: str, **extra: Any) -> None:
    from operon.workflow import log_run
    log_run(db, project, {
        "run_id": run_id or f"WF_{now_iso().replace('-', '').replace(':', '').replace('T', '_')}",
        "entity_type": entity_type,
        "entity_id": entity_id,
        "step": step,
        "status": status,
        "started_at": now_iso(),
        "finished_at": now_iso(),
        **extra,
    })
