#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Explicit ``operon recompress``: plain FASTA/FASTQ raw archives to BGZF."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from operon import files
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import recompress_files
from operon.utils import sha256_path

PLAIN_FASTA = ">ctg1\nACGTACGTACGT\n"


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row(
        "organisms", {"organism_id": "ORG_000001", "scientific_name": "Example"}
    )
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row(
        "assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"}
    )
    db.insert_row(
        "annotations", {"annotation_id": "ANN_000001", "assembly_id": "ASM_000001"}
    )
    try:
        yield project, db
    finally:
        db.close()


def _ingest_plain(project, db, tmp_path: Path):
    source = tmp_path / "genome.fa"
    source.write_text(PLAIN_FASTA, encoding="utf-8")
    return files.ingest_file(
        db,
        project,
        source,
        "assembly",
        "ASM_000001",
        "genome_fasta",
        compress_archive=False,
    )


def _row(db, file_id: str) -> dict:
    return dict(db.query("SELECT * FROM files WHERE file_id=?", (file_id,))[0])


def test_recompress_full_flow_audit_and_content_preservation(project_db, tmp_path):
    project, db = project_db
    record = _ingest_plain(project, db, tmp_path)
    db.insert_row(
        "file_locations",
        {
            "file_id": record["file_id"],
            "location_name": "mirror",
            "location_type": "sftp",
            "uri": f"remote://mirror/{record['relative_path']}",
            "relative_path": record["relative_path"],
            "sha256": record["sha256"],
            "size_bytes": record["size_bytes"],
            "status": "AVAILABLE",
        },
    )
    old_path = project.root / record["relative_path"]

    (result,) = recompress_files(db, project)
    assert result["status"] == "recompressed"

    row = _row(db, record["file_id"])
    assert row["compression"] == "bgzip"
    assert row["relative_path"] == record["relative_path"] + ".gz"
    new_path = project.root / row["relative_path"]
    assert not old_path.exists() and new_path.is_file()
    blob = new_path.read_bytes()
    assert row["sha256"] == sha256_path(new_path)
    assert int(row["size_bytes"]) == len(blob)
    assert gzip.decompress(blob) == PLAIN_FASTA.encode("utf-8")

    audit = db.query(
        "SELECT field, old_value, new_value FROM changes "
        "WHERE object_type='files' AND object_id=? ORDER BY change_id",
        (record["file_id"],),
    )
    assert [row["field"] for row in audit] == [
        "relative_path",
        "sha256",
        "size_bytes",
        "compression",
    ]
    reasons = db.query(
        "SELECT DISTINCT reason FROM changes WHERE object_id=?", (record["file_id"],)
    )
    assert all("recompress" in row["reason"] for row in reasons)

    # The stale remote location row is gone, with its own audit trail.
    assert (
        db.query("SELECT * FROM file_locations WHERE file_id=?", (record["file_id"],))
        == []
    )
    location_audit = db.query(
        "SELECT * FROM changes WHERE object_type='file_location' AND object_id=?",
        (f"{record['file_id']}@mirror",),
    )
    assert len(location_audit) == 1
    assert location_audit[0]["new_value"] is None

    # The verification cache now covers the compressed bytes.
    matched, info = files.verify_local_file_identity(db, row, new_path, rehash=True)
    assert matched and info["verification_method"] == "full_sha256"
    matched, info = files.verify_local_file_identity(db, row, new_path)
    assert matched and info["verification_method"] == "cached_stat_fingerprint"

    # A second default run selects nothing (the row is compressed now), and an
    # explicit re-run of the file is an idempotent skip.
    assert recompress_files(db, project) == []
    (again,) = recompress_files(db, project, [record["file_id"]])
    assert again["status"] == "skipped"
    assert db.query("SELECT COUNT(*) AS n FROM files")[0]["n"] == 1


def test_recompress_is_idempotent_noop_when_already_compressed(project_db, tmp_path):
    project, db = project_db
    source = tmp_path / "genome.fa"
    source.write_text(PLAIN_FASTA, encoding="utf-8")
    record = files.ingest_file(
        db, project, source, "assembly", "ASM_000001", "genome_fasta"
    )
    assert record["compression"] == "bgzip"

    # Default selection only lists plain files: nothing to do here.
    assert recompress_files(db, project) == []
    (result,) = recompress_files(db, project, [record["file_id"]])
    assert result["status"] == "skipped"
    assert "already compressed" in result["error"]


def test_recompress_refuses_release_members(project_db, tmp_path):
    project, db = project_db
    record = _ingest_plain(project, db, tmp_path)
    db.insert_row(
        "releases",
        {
            "version": "v1",
            "created_at": "2026-09-01T00:00:00+08:00",
            "profile": "p",
            "path": "releases/v1",
            "summary": "{}",
        },
    )
    db.insert_row(
        "release_members",
        {
            "release_version": "v1",
            "file_id": record["file_id"],
            "entity_type": "assembly",
            "entity_id": "ASM_000001",
            "release_path": "data/x.fasta",
            "sha256": record["sha256"],
            "size_bytes": record["size_bytes"],
        },
    )
    (result,) = recompress_files(db, project, [record["file_id"]])
    assert result["status"] == "error"
    assert "immutable" in result["error"] and "v1" in result["error"]
    # Nothing moved.
    assert _row(db, record["file_id"])["compression"] == "none"
    assert (project.root / record["relative_path"]).is_file()


def test_recompress_skips_remote_only_and_missing_local_bytes(project_db, tmp_path):
    project, db = project_db
    record = _ingest_plain(project, db, tmp_path)
    (project.root / record["relative_path"]).unlink()
    db.conn.execute(
        "UPDATE files SET status='REMOTE_ONLY' WHERE file_id=?", (record["file_id"],)
    )
    db.conn.commit()

    (result,) = recompress_files(db, project)
    assert result["status"] == "skipped"
    assert "local bytes absent" in result["error"]
    assert _row(db, record["file_id"])["compression"] == "none"


def test_recompress_skips_retired_entities(project_db, tmp_path):
    from operon.lifecycle import apply_lifecycle_event

    project, db = project_db
    record = _ingest_plain(project, db, tmp_path)
    apply_lifecycle_event(
        db,
        "assembly",
        "ASM_000001",
        action="RETIRE",
        reason="mistake",
        reason_code="accidental_import",
        actor="tester",
    )
    (result,) = recompress_files(db, project)
    assert result["status"] == "skipped"
    assert "retired" in result["error"]
    assert _row(db, record["file_id"])["compression"] == "none"


def test_recompress_rebuilds_the_standardized_target(project_db, tmp_path):
    project, db = project_db
    record = _ingest_plain(project, db, tmp_path)
    files.standardize_file(db, project, record["file_id"])
    old_standardized = (
        project.standardized_root
        / "assemblies"
        / "ASM_000001"
        / Path(record["relative_path"]).name
    )
    assert old_standardized.is_file()

    (result,) = recompress_files(db, project)
    assert result["status"] == "recompressed"
    row = _row(db, record["file_id"])
    assert row["status"] == "STANDARDIZED"
    assert not old_standardized.exists()
    new_standardized = (
        project.standardized_root
        / "assemblies"
        / "ASM_000001"
        / Path(row["relative_path"]).name
    )
    assert new_standardized.is_file()
    assert gzip.decompress(new_standardized.read_bytes()) == PLAIN_FASTA.encode("utf-8")
    assert db.get_entity_state("assembly", "ASM_000001") == "STANDARDIZED"


def test_recompress_dry_run_writes_nothing(project_db, tmp_path):
    project, db = project_db
    record = _ingest_plain(project, db, tmp_path)
    (result,) = recompress_files(db, project, dry_run=True)
    assert result["status"] == "would_recompress"
    assert result["relative_path"] == record["relative_path"] + ".gz"
    assert _row(db, record["file_id"])["compression"] == "none"
    assert (project.root / record["relative_path"]).is_file()
    assert not (project.root / (record["relative_path"] + ".gz")).exists()
    assert db.query("SELECT COUNT(*) AS n FROM changes")[0]["n"] == 0


def test_recompress_unknown_file_id_is_an_error(project_db):
    _project, db = project_db
    (result,) = recompress_files(db, _project, ["FIL_999999"])
    assert result["status"] == "error"
    assert "unknown file_id" in result["error"]


def test_recompress_refuses_checksum_mismatch(project_db, tmp_path):
    project, db = project_db
    record = _ingest_plain(project, db, tmp_path)
    path = project.root / record["relative_path"]
    path.write_text(PLAIN_FASTA + ">ctg2\nTTTT\n", encoding="utf-8")
    (result,) = recompress_files(db, project, [record["file_id"]])
    assert result["status"] == "error"
    assert "do not match the manifest" in result["error"]
    assert _row(db, record["file_id"])["compression"] == "none"


def test_recompress_cli(project_db, tmp_path, capsys):
    project, db = project_db
    record = _ingest_plain(project, db, tmp_path)
    db.close()

    # Scope is mandatory.
    assert main(["--project", str(project.root), "recompress"]) == 2
    capsys.readouterr()
    # A dry run reports and changes nothing.
    assert (
        main(["--project", str(project.root), "recompress", "--all", "--dry-run"]) == 0
    )
    out = capsys.readouterr().out
    assert "would_recompress" in out
    assert (project.root / record["relative_path"]).is_file()
    # The real run converts the archive.
    assert main(["--project", str(project.root), "recompress", "--all"]) == 0
    out = capsys.readouterr().out
    assert "recompressed" in out
    check = Database(project.db_path)
    try:
        row = _row(check, record["file_id"])
        assert row["compression"] == "bgzip"
        assert (project.root / row["relative_path"]).is_file()
    finally:
        check.close()
    # An unknown --file-id fails the command.
    assert (
        main(["--project", str(project.root), "recompress", "--file-id", "FIL_999999"])
        == 1
    )
    capsys.readouterr()
