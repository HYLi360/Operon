"""Immutable archive and verification edge cases."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from operon import files
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ChecksumError, ConflictError, EntityNotFoundError, ValidationError
from operon.utils import sha256_file, sha256_path


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Example"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
    db.insert_row("annotations", {"annotation_id": "ANN_000001", "assembly_id": "ASM_000001"})
    try:
        yield project, db
    finally:
        db.close()


def test_format_compression_filename_and_bucket_detection(tmp_path):
    directory = tmp_path / "directory"
    directory.mkdir()
    assert files.detect_format(directory) == "directory"
    assert files.detect_compression(directory) == "none"
    assert files.canonical_filename("A", "r", "directory", "none") == "A.r.dir"
    assert files.canonical_filename("A", "r", "fasta", "bgzip") == "A.r.fasta.gz"
    assert files.detect_format(tmp_path / "x.unknown", "genome_fasta") == "fasta"
    assert files.detect_format(tmp_path / "x.unknown") == "other"
    assert files.raw_bucket("unknown") == "other"
    fake = tmp_path / "fake.gz"
    fake.write_text("not gzip", encoding="utf-8")
    with pytest.raises(ValidationError, match="claims gzip"):
        files.detect_compression(fake)
    real = tmp_path / "real.data"
    with gzip.open(real, "wt") as handle:
        handle.write("x")
    assert files.detect_compression(real) == "gzip"


def test_ingest_rejects_artifact_shape_and_role_conflicts(project_db, tmp_path):
    project, db = project_db
    missing = tmp_path / "missing"
    with pytest.raises(ValidationError, match="source artifact does not exist"):
        files.ingest_file(db, project, missing, "assembly", "ASM_000001", "genome_fasta")
    directory = tmp_path / "dir"
    directory.mkdir()
    with pytest.raises(ValidationError, match="directory input requires"):
        files.ingest_file(
            db, project, directory, "assembly", "ASM_000001", "genome_fasta", fmt="fasta"
        )
    source = tmp_path / "x.fna"
    source.write_text(">x\nA\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="requires a directory source"):
        files.ingest_file(
            db, project, source, "assembly", "ASM_000001", "genome_fasta", fmt="directory"
        )
    with pytest.raises(ValidationError, match="compression=none"):
        files.ingest_file(
            db, project, directory, "assembly", "ASM_000001", "genome_fasta",
            fmt="directory", compression="gzip",
        )
    first = files.ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    changed = tmp_path / "changed.fna"
    changed.write_text(">x\nC\n", encoding="utf-8")
    with pytest.raises(ConflictError, match="already has"):
        files.ingest_file(db, project, changed, "assembly", "ASM_000001", "genome_fasta")
    assert files.find_existing_file(
        db, "assembly", "ASM_000001", "genome_fasta", first["sha256"]
    )["file_id"] == first["file_id"]


def test_ingest_move_falls_back_to_copy(project_db, tmp_path, monkeypatch):
    project, db = project_db
    source = tmp_path / "annotation.gff3"
    source.write_text("##gff-version 3\n", encoding="utf-8")
    real_replace = files.os.replace
    move_sources = {source}

    def cross_device_for_sources(src, target):
        if Path(src) in move_sources:
            raise OSError("cross-device")
        return real_replace(src, target)

    monkeypatch.setattr(files.os, "replace", cross_device_for_sources)
    row = files.ingest_file(
        db, project, source, "annotation", "ANN_000001", "annotation_gff3", move=True
    )
    assert not source.exists()
    assert (project.root / row["relative_path"]).is_file()

    directory = tmp_path / "artifact-dir"
    directory.mkdir()
    (directory / "result.txt").write_text("x", encoding="utf-8")
    move_sources.add(directory)
    row = files.ingest_file(
        db, project, directory, "annotation", "ANN_000001", "analysis_output",
        fmt="directory", compression="none", move=True,
    )
    assert not directory.exists() and (project.root / row["relative_path"]).is_dir()


def test_local_verification_cache_missing_size_cached_and_changed(project_db, tmp_path, monkeypatch):
    project, db = project_db
    source = tmp_path / "x.fna"
    source.write_text(">x\nA\n", encoding="utf-8")
    row = files.ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    path = project.root / row["relative_path"]
    matched, info = files.verify_local_file_identity(db, row, path)
    assert matched and info["verification_method"] == "cached_stat_fingerprint"
    matched, info = files.verify_local_file_identity(db, row, path, rehash=True)
    assert matched and info["verification_method"] == "full_sha256"

    missing = tmp_path / "missing"
    matched, info = files.verify_local_file_identity(db, row, missing)
    assert not matched and info["verification_method"] == "missing"
    wrong_size = dict(row, size_bytes=row["size_bytes"] + 1)
    matched, info = files.verify_local_file_identity(db, wrong_size, path)
    assert not matched and info["verification_method"] == "size_mismatch"

    real_fingerprint = files._local_file_fingerprint
    fingerprints = iter([
        {"size_bytes": row["size_bytes"], "device": 1, "inode": 1, "mtime_ns": 1, "ctime_ns": 1},
        {"size_bytes": row["size_bytes"], "device": 1, "inode": 1, "mtime_ns": 2, "ctime_ns": 1},
    ])
    monkeypatch.setattr(files, "_local_file_fingerprint", lambda _p: next(fingerprints))
    matched, info = files.verify_local_file_identity(db, row, path, rehash=True)
    assert not matched and info["verification_method"] == "changed_during_sha256"
    monkeypatch.setattr(files, "_local_file_fingerprint", real_fingerprint)

    monkeypatch.setattr(files, "sha256_path", lambda _p: (_ for _ in ()).throw(OSError("read")))
    matched, info = files.verify_local_file_identity(db, row, path, rehash=True)
    assert not matched and info["verification_method"] == "sha256_error"


def test_verification_cache_ignores_nonfiles_and_wrong_size(project_db, tmp_path, monkeypatch):
    _project, db = project_db
    record = {"file_id": "F", "sha256": "x", "size_bytes": 1}
    files.remember_local_file_verification(db, record, tmp_path / "missing")
    source = tmp_path / "source"
    source.write_text("xx", encoding="utf-8")
    files.remember_local_file_verification(db, record, source)
    assert db.query("SELECT * FROM local_file_verifications WHERE file_id='F'") == []
    monkeypatch.setattr(Path, "is_file", lambda _self: (_ for _ in ()).throw(OSError("stat")))
    files.remember_local_file_verification(db, record, source)


def test_standardize_missing_remote_tampered_links_and_idempotency(project_db, tmp_path, monkeypatch):
    project, db = project_db
    with pytest.raises(EntityNotFoundError):
        files.standardize_file(db, project, "FIL_MISSING")
    source = tmp_path / "x.fna"
    source.write_text(">x\nA\n", encoding="utf-8")
    row = files.ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    archived = project.root / row["relative_path"]
    archived.unlink()
    db.conn.execute("UPDATE files SET status='REMOTE_ONLY' WHERE file_id=?", (row["file_id"],))
    db.conn.commit()
    with pytest.raises(ChecksumError, match="remote-only"):
        files.standardize_file(db, project, row["file_id"])
    archived.write_text("tampered", encoding="utf-8")
    with pytest.raises(ChecksumError, match="does not match manifest"):
        files.standardize_file(db, project, row["file_id"])
    archived.write_text(">x\nA\n", encoding="utf-8")
    result = files.standardize_file(db, project, row["file_id"], link_kind="symlink")
    assert Path(result["target"]).is_symlink()
    assert files.standardize_file(db, project, row["file_id"])["action"] == "skipped"


def test_standardize_hardlink_fallback_directory_and_batch_error(project_db, tmp_path, monkeypatch):
    project, db = project_db
    source = tmp_path / "gff.gff3"
    source.write_text("##gff-version 3\n", encoding="utf-8")
    row = files.ingest_file(db, project, source, "annotation", "ANN_000001", "annotation_gff3")
    monkeypatch.setattr(files.os, "link", lambda *_a: (_ for _ in ()).throw(OSError("no hardlink")))
    assert files.standardize_file(db, project, row["file_id"], link_kind="hardlink")["action"] == "hardlink"

    directory = tmp_path / "results"
    directory.mkdir()
    (directory / "x").write_text("x", encoding="utf-8")
    directory_row = files.ingest_file(
        db, project, directory, "annotation", "ANN_000001", "analysis_output",
        fmt="directory", compression="none",
    )
    assert Path(files.standardize_file(
        db, project, directory_row["file_id"], link_kind="hardlink"
    )["target"]).is_dir()
    (project.root / row["relative_path"]).unlink()
    results = files.standardize_all(db, project)
    assert any("error" in item for item in results)


def test_local_verification_stat_error(project_db, tmp_path, monkeypatch):
    _project, db = project_db
    path = tmp_path / "x"
    path.write_text("x", encoding="utf-8")
    record = {"file_id": "F", "sha256": sha256_path(path), "size_bytes": 1}
    monkeypatch.setattr(files, "_local_file_fingerprint", lambda _path: (_ for _ in ()).throw(OSError("stat")))
    matched, info = files.verify_local_file_identity(db, record, path, rehash=True)
    assert not matched and info["verification_method"] == "stat_error"


def test_ingest_existing_unregistered_target_and_directory_conflict(project_db, tmp_path):
    project, db = project_db
    source = tmp_path / "source.fna"
    source.write_text(">x\nA\n", encoding="utf-8")
    target = project.raw_root / "assemblies" / "ASM_000001" / "ASM_000001.genome_fasta.fasta"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    row = files.ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    assert row["relative_path"].endswith("ASM_000001.genome_fasta.fasta")

    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "new").write_text("new", encoding="utf-8")
    occupied = project.raw_root / "annotations" / "ANN_000001" / "ANN_000001.analysis_output.dir"
    occupied.mkdir(parents=True)
    (occupied / "old").write_text("old", encoding="utf-8")
    with pytest.raises(ConflictError, match="different checksum"):
        files.ingest_file(
            db, project, directory, "annotation", "ANN_000001", "analysis_output",
            fmt="directory", compression="none",
        )


def test_ingest_checksum_mismatch_removes_file_and_directory(project_db, tmp_path, monkeypatch):
    project, db = project_db
    real_sha = files.sha256_path
    source = tmp_path / "file.txt"
    source.write_text("x", encoding="utf-8")
    def mismatching_file(path):
        return "0" * 64 if str(path).startswith(str(project.raw_root)) else real_sha(path)
    monkeypatch.setattr(files, "sha256_path", mismatching_file)
    with pytest.raises(ChecksumError, match="checksum mismatch while archiving"):
        files.ingest_file(db, project, source, "annotation", "ANN_000001", "other", fmt="txt")
    assert not list((project.raw_root / "annotations" / "ANN_000001").glob("*.txt"))

    directory = tmp_path / "dir"
    directory.mkdir()
    (directory / "x").write_text("x", encoding="utf-8")
    with pytest.raises(ChecksumError, match="checksum mismatch while archiving"):
        files.ingest_file(
            db, project, directory, "annotation", "ANN_000001", "analysis_output",
            fmt="directory", compression="none",
        )
    assert not (project.raw_root / "annotations" / "ANN_000001" / "ANN_000001.analysis_output.dir").exists()


def test_resolve_occupied_target_claimant_conflicts_relocation_and_orphan(project_db, tmp_path):
    project, db = project_db
    target = project.raw_root / "assemblies" / "ASM_000001" / "occupied.fa"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")
    digest = sha256_path(target)
    db.insert_row("files", {
        "file_id": "FIL_000001", "entity_type": "assembly", "entity_id": "ASM_000001",
        "file_role": "genome_fasta_refseq", "format": "fasta", "compression": "none",
        "relative_path": files.project_rel(project, target), "size_bytes": 3,
        "sha256": "0" * 64, "status": "CHECKSUM_VERIFIED",
    })
    with pytest.raises(ConflictError, match="do not match manifest"):
        files._resolve_occupied_target(db, project, target, digest)
    db.conn.execute("UPDATE files SET sha256=? WHERE file_id='FIL_000001'", (digest,))
    canonical = target.parent / files.canonical_filename(
        "ASM_000001", "genome_fasta_refseq", "fasta", "none"
    )
    canonical.write_text("different", encoding="utf-8")
    with pytest.raises(ConflictError, match="destination exists"):
        files._resolve_occupied_target(db, project, target, digest)
    canonical.unlink()
    files._resolve_occupied_target(db, project, target, digest)
    assert canonical.read_text() == "old"

    orphan = target.parent / "orphan.fa"
    orphan.write_text("orphan", encoding="utf-8")
    orphan_sha = sha256_path(orphan)
    files._resolve_occupied_target(db, project, orphan, orphan_sha)
    assert not orphan.exists() and list(orphan.parent.glob("orphan.fa.orphan-*"))


def test_standardize_missing_nonremote_target_conflict_and_postcopy_cleanup(project_db, tmp_path, monkeypatch):
    project, db = project_db
    source = tmp_path / "x.gff3"
    source.write_text("##gff-version 3\n", encoding="utf-8")
    row = files.ingest_file(db, project, source, "annotation", "ANN_000001", "annotation_gff3")
    archived = project.root / row["relative_path"]
    archived.unlink()
    with pytest.raises(ChecksumError, match="source missing"):
        files.standardize_file(db, project, row["file_id"])
    archived.write_text("##gff-version 3\n", encoding="utf-8")
    target = project.standardized_root / "annotations" / "ANN_000001" / archived.name
    target.parent.mkdir(parents=True)
    target.write_text("different", encoding="utf-8")
    with pytest.raises(ConflictError, match="different content"):
        files.standardize_file(db, project, row["file_id"])
    target.unlink()

    real_sha = files.sha256_path
    monkeypatch.setattr(files, "sha256_path", lambda path: "bad" if str(path).startswith(str(project.standardized_root)) else real_sha(path))
    with pytest.raises(ChecksumError, match="standardized target checksum mismatch"):
        files.standardize_file(db, project, row["file_id"])
    assert not target.exists()
