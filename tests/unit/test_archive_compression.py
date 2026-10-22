#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Deterministic BGZF archive storage for plain FASTA/FASTQ ingest."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from operon import files, qc
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.utils import atomic_copy_bgzip, sha256_path

BGZF_EOF_BLOCK = (
    b"\x1f\x8b\x08\x04\x00\x00\x00\x00\x00\xff\x06\x00BC"
    b"\x02\x00\x1b\x00\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00"
)

FASTA = ">ctg1\nACGTACGTACGT\n>ctg2\nTTTTCCCCAAAA\n"
FASTQ = "@read1\nACGTACGT\n+\nIIIIIIII\n@read2\nTTTTGGGG\n+\nIIIIIIII\n"


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
    db.insert_row("runs", {"run_id": "RUN_000001", "sample_id": "SMP_000001"})
    try:
        yield project, db
    finally:
        db.close()


def _write_source(tmp_path: Path, name: str, text: str) -> Path:
    source = tmp_path / name
    source.write_text(text, encoding="utf-8")
    return source


def _assert_bgzf(path: Path, plain: bytes) -> None:
    blob = path.read_bytes()
    assert blob[:2] == b"\x1f\x8b"
    assert blob.endswith(BGZF_EOF_BLOCK)
    assert gzip.decompress(blob) == plain


def test_new_project_defaults_to_bgzip_for_plain_fasta_and_fastq(project_db, tmp_path):
    project, db = project_db
    assert project.compress_archive is True

    fasta = _write_source(tmp_path, "genome.fna", FASTA)
    fasta_row = files.ingest_file(
        db, project, fasta, "assembly", "ASM_000001", "genome_fasta"
    )
    assert fasta_row["relative_path"].endswith("ASM_000001.genome_fasta.fasta.gz")
    assert fasta_row["compression"] == "bgzip"
    archived = project.root / fasta_row["relative_path"]
    assert fasta_row["sha256"] == sha256_path(archived)
    assert int(fasta_row["size_bytes"]) == archived.stat().st_size
    _assert_bgzf(archived, FASTA.encode("utf-8"))

    fastq = _write_source(tmp_path, "reads.fastq", FASTQ)
    fastq_row = files.ingest_file(db, project, fastq, "run", "RUN_000001", "reads_r1")
    assert fastq_row["relative_path"].endswith("RUN_000001.reads_r1.fastq.gz")
    assert fastq_row["compression"] == "bgzip"
    _assert_bgzf(project.root / fastq_row["relative_path"], FASTQ.encode("utf-8"))


def test_bgzip_output_is_byte_deterministic(tmp_path):
    source = _write_source(tmp_path, "genome.fna", FASTA * 200)
    first = tmp_path / "first.gz"
    second = tmp_path / "second.gz"
    atomic_copy_bgzip(source, first)
    atomic_copy_bgzip(source, second)
    assert first.read_bytes() == second.read_bytes()


def test_reingest_of_same_source_is_idempotent(project_db, tmp_path):
    project, db = project_db
    source = _write_source(tmp_path, "genome.fna", FASTA)
    first = files.ingest_file(
        db, project, source, "assembly", "ASM_000001", "genome_fasta"
    )
    again = files.ingest_file(
        db, project, source, "assembly", "ASM_000001", "genome_fasta"
    )
    assert again["file_id"] == first["file_id"]
    assert db.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1


def test_opt_out_keeps_plain_storage(project_db, tmp_path):
    project, db = project_db
    source = _write_source(tmp_path, "genome.fna", FASTA)
    row = files.ingest_file(
        db,
        project,
        source,
        "assembly",
        "ASM_000001",
        "genome_fasta",
        compress_archive=False,
    )
    assert row["compression"] == "none"
    assert row["relative_path"].endswith("ASM_000001.genome_fasta.fasta")
    assert (project.root / row["relative_path"]).read_bytes() == FASTA.encode("utf-8")


def test_project_config_opt_out_keeps_plain_storage(project_db, tmp_path):
    project, db = project_db
    config_path = project.config_path
    text = config_path.read_text(encoding="utf-8")
    assert "compress_archive: true" in text
    config_path.write_text(
        text.replace("compress_archive: true", "compress_archive: false"),
        encoding="utf-8",
    )
    project = load_project(project.root)
    assert project.compress_archive is False
    source = _write_source(tmp_path, "genome.fna", FASTA)
    row = files.ingest_file(
        db, project, source, "assembly", "ASM_000001", "genome_fasta"
    )
    assert row["compression"] == "none"
    assert not row["relative_path"].endswith(".gz")


def test_legacy_project_without_the_key_keeps_plain_storage(project_db, tmp_path):
    project, db = project_db
    config_path = project.config_path
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace("  compress_archive: true\n", ""), encoding="utf-8"
    )
    project = load_project(project.root)
    assert project.compress_archive is False
    source = _write_source(tmp_path, "genome.fna", FASTA)
    row = files.ingest_file(
        db, project, source, "assembly", "ASM_000001", "genome_fasta"
    )
    assert row["compression"] == "none"


@pytest.mark.parametrize(
    ("name", "role", "canonical_suffix"),
    [
        ("ann.gff3", "annotation_gff3", "ANN_000001.annotation_gff3.gff3.gz"),
        ("ann.gtf", "annotation_gff3", "ANN_000001.annotation_gff3.gtf.gz"),
    ],
)
def test_plain_annotations_are_stored_as_bgzip_by_default(
    project_db, tmp_path, name, role, canonical_suffix
):
    project, db = project_db
    source = _write_source(tmp_path, name, "##gff-version 3\n")
    row = files.ingest_file(db, project, source, "annotation", "ANN_000001", role)
    assert row["compression"] == "bgzip"
    assert row["relative_path"].endswith(canonical_suffix)


def test_already_gzipped_annotation_is_stored_as_is(project_db, tmp_path):
    project, db = project_db
    source = tmp_path / "ann.gff3.gz"
    with gzip.open(source, "wt", encoding="utf-8") as handle:
        handle.write("##gff-version 3\n")
    original = source.read_bytes()
    row = files.ingest_file(
        db, project, source, "annotation", "ANN_000001", "annotation_gff3"
    )
    assert row["compression"] == "gzip"
    assert (project.root / row["relative_path"]).read_bytes() == original


def test_explicit_compression_argument_pins_the_storage_form(project_db, tmp_path):
    project, db = project_db
    source = _write_source(tmp_path, "genome.fna", FASTA)
    row = files.ingest_file(
        db,
        project,
        source,
        "assembly",
        "ASM_000001",
        "genome_fasta",
        compression="none",
    )
    assert row["compression"] == "none"
    assert not row["relative_path"].endswith(".gz")


def test_already_gzipped_source_is_stored_as_is(project_db, tmp_path):
    project, db = project_db
    source = tmp_path / "genome.fna.gz"
    with gzip.open(source, "wt", encoding="utf-8") as handle:
        handle.write(FASTA)
    original = source.read_bytes()
    row = files.ingest_file(
        db, project, source, "assembly", "ASM_000001", "genome_fasta"
    )
    assert row["compression"] == "gzip"
    assert (project.root / row["relative_path"]).read_bytes() == original


def test_qc_runs_on_the_bgzip_archive_and_caches_lengths(project_db, tmp_path):
    project, db = project_db
    source = _write_source(tmp_path, "genome.fna", FASTA)
    row = files.ingest_file(
        db, project, source, "assembly", "ASM_000001", "genome_fasta"
    )
    result = qc.qc_file(db, project, row["file_id"])
    assert result["ok"], result["error"]
    metrics = {
        (m["qc_stage"], m["metric_name"]): m["metric_value"]
        for m in db.query(
            "SELECT qc_stage, metric_name, metric_value FROM qc_results WHERE file_id=?",
            (row["file_id"],),
        )
    }
    assert metrics[("file_integrity", "sha256_match")] in ("1", "True", "true")
    assert metrics[("file_integrity", "parseable")] in ("1", "True", "true")
    assert float(metrics[("assembly_basic", "total_length")]) == 24
    cache_dir = project.index_root / "fasta_lengths"
    caches = list(cache_dir.glob("*"))
    assert caches, "the fasta_lengths cache must be rebuilt for the new identity"
    assert "ctg1\t12" in caches[0].read_text(encoding="utf-8")


def test_move_with_compression_removes_only_the_source(project_db, tmp_path):
    project, db = project_db
    source = _write_source(tmp_path, "genome.fna", FASTA)
    row = files.ingest_file(
        db,
        project,
        source,
        "assembly",
        "ASM_000001",
        "genome_fasta",
        move=True,
    )
    assert not source.exists()
    archived = project.root / row["relative_path"]
    assert row["compression"] == "bgzip"
    _assert_bgzf(archived, FASTA.encode("utf-8"))
    # The staging sibling of the archive target is always removed.
    assert list(archived.parent.glob(".*.bgzip-*")) == []


def test_failed_ingest_removes_the_compression_staging_file(project_db, tmp_path):
    project, db = project_db
    source = _write_source(tmp_path, "genome.fna", FASTA)
    files.ingest_file(db, project, source, "assembly", "ASM_000001", "genome_fasta")
    changed = _write_source(tmp_path, "changed.fna", FASTA + ">ctg3\nGGGG\n")
    with pytest.raises(files.ConflictError):
        files.ingest_file(
            db, project, changed, "assembly", "ASM_000001", "genome_fasta"
        )
    entity_dir = project.raw_root / "assemblies" / "ASM_000001"
    assert list(entity_dir.glob(".*.bgzip-*")) == []


def test_stored_content_sha256_matches_content_regardless_of_storage_form(
    project_db, tmp_path
):
    project, db = project_db
    plain = _write_source(tmp_path, "plain.fna", FASTA)
    plain_row = files.ingest_file(
        db,
        project,
        plain,
        "assembly",
        "ASM_000001",
        "genome_fasta",
        compress_archive=False,
    )
    assert files.stored_content_sha256(project, plain_row) == sha256_path(plain)

    db.insert_row(
        "assemblies", {"assembly_id": "ASM_000002", "sample_id": "SMP_000001"}
    )
    compressed = _write_source(tmp_path, "compressed.fna", FASTA)
    compressed_row = files.ingest_file(
        db, project, compressed, "assembly", "ASM_000002", "genome_fasta"
    )
    assert compressed_row["compression"] == "bgzip"
    # The stored bytes differ; the content checksum does not.
    assert compressed_row["sha256"] != plain_row["sha256"]
    assert files.stored_content_sha256(
        project, compressed_row
    ) == files.stored_content_sha256(project, plain_row)

    # A row whose local bytes are absent cannot prove a content match.
    assert (
        files.stored_content_sha256(
            project, dict(compressed_row, relative_path="raw/gone.fasta.gz")
        )
        is None
    )


@pytest.mark.bug("ODR-84")
def test_bam_intrinsic_compression_stays_none_and_preflight_compares_stored_bytes(
    project_db, tmp_path
):
    project, db = project_db
    payload = b"BAM\x01binary-payload"
    plain = tmp_path / "payload.bin"
    plain.write_bytes(payload)
    source = tmp_path / "reads.bam"
    atomic_copy_bgzip(plain, source)

    row = files.ingest_file(
        db, project, source, "assembly", "ASM_000001", "alignment_bam"
    )
    assert row["format"] == "bam"
    assert row["compression"] == "none"
    assert row["relative_path"].endswith("ASM_000001.alignment_bam.bam")
    assert not row["relative_path"].endswith(".gz")
    assert files.stored_content_sha256(project, row) == sha256_path(source)

    # A legacy row recorded before the correction says gzip and points at a
    # BGZF-named path; the preflight must compare its stored bytes, not the
    # decompressed BAM stream the ingest path can never reproduce.
    legacy_path = tmp_path / "legacy.bam.gz"
    legacy_path.write_bytes(source.read_bytes())
    legacy = dict(
        row,
        compression="gzip",
        relative_path=str(legacy_path.relative_to(project.root)),
    )
    assert files.stored_content_sha256(project, legacy) == sha256_path(legacy_path)
    assert files.stored_content_sha256(project, legacy) != sha256_path(plain)
