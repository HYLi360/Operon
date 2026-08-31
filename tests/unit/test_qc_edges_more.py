"""Cache corruption, related-input, and pairing branches for built-in QC."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from operon import qc_module as qc
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import QCError


def test_metric_none_text_bool_and_write_skips_none():
    assert qc.metric("x", "y", "s", "none", None) is None
    assert qc.metric("x", "y", "s", "bool", True)["metric_numeric"] == 1.0
    assert qc.metric("x", "y", "s", "text", "v")["metric_numeric"] is None

    class DB:
        rows = None

        def insert_many_qc(self, rows):
            self.rows = rows

    db = DB()
    record = {"file_id": "F", "sha256": "a" * 64, "size_bytes": 1}
    item = qc.metric("annotation", "A", "annotation_basic", "m", 1)
    qc._write(db, [None, item], record, [{
        "kind": "assembly", "file_id": "AF", "sha256": "b" * 64, "size_bytes": 1,
    }])
    assert len(db.rows) == 1 and db.rows[0]["input_identity"].startswith("input-set:v1:")


def test_related_input_descriptor_and_failed_verification(tmp_path, monkeypatch):
    record = {
        "file_id": "F", "sha256": "a" * 64, "size_bytes": 1,
        "relative_path": "missing", "file_role": "genome_fasta",
        "format": "fasta", "compression": "none",
    }
    related = []
    monkeypatch.setattr(qc, "verify_local_file_identity", lambda *_a, **_k: (
        False, {"size_bytes": 0, "verification_method": "missing"}
    ))
    with pytest.raises(QCError, match="related assembly"):
        qc._verify_related_input(
            SimpleNamespace(), SimpleNamespace(root=tmp_path), record,
            kind="assembly", stage="verify", timings={}, related_inputs=related, rehash=True,
        )
    assert related[0]["integrity"]["verification_method"] == "missing"


@pytest.mark.parametrize("seqid", ["", "a\tb", "a\nb", "a\rb"])
def test_fasta_cache_row_rejects_bad_ids(seqid):
    with pytest.raises(QCError, match="invalid FASTA sequence identifier"):
        qc._fasta_length_cache_row(seqid, 1)
    with pytest.raises(QCError, match="negative"):
        qc._fasta_length_cache_row("ok", -1)


def _cache_header(record, *, count=1, digest="bad"):
    return {
        "cache_format": qc.FASTA_LENGTH_CACHE_FORMAT,
        "file_id": record["file_id"], "sha256": record["sha256"],
        "size_bytes": record["size_bytes"], "sequence_count": count,
        "lengths_sha256": digest,
    }


@pytest.mark.parametrize(
    "body",
    [
        "not-json\n",
        json.dumps({"cache_format": "wrong"}) + "\n",
        None,
        "negative",
        "count",
        "digest",
    ],
)
def test_load_fasta_cache_discards_corruption(tmp_path, body):
    record = {"file_id": "F", "sha256": "a" * 64, "size_bytes": 1}
    path = tmp_path / "cache.tsv"
    if body is None:
        header = _cache_header(record)
        text = json.dumps(header) + "\nbad-row\n"
    elif body == "negative":
        text = json.dumps(_cache_header(record)) + "\nseq\t-1\n"
    elif body == "count":
        row = qc._fasta_length_cache_row("seq", 1)
        digest = hashlib.sha256(row.encode()).hexdigest()
        text = json.dumps(_cache_header(record, count=2, digest=digest)) + "\n" + row
    elif body == "digest":
        text = json.dumps(_cache_header(record, count=1, digest="bad")) + "\nseq\t1\n"
    else:
        text = body
    path.write_text(text, encoding="utf-8")
    assert qc._load_fasta_length_cache(path, record) is None
    assert not path.exists()


def test_fasta_cache_roundtrip_write_failure_and_cached_statuses(tmp_path, monkeypatch):
    record = {"file_id": "F", "sha256": "a" * 64, "size_bytes": 1}
    path = tmp_path / "cache.tsv"
    qc._write_fasta_length_cache(path, record, {"a": 1})
    assert qc._load_fasta_length_cache(path, record) == {"a": 1}

    monkeypatch.setattr(qc.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        qc._write_fasta_length_cache(tmp_path / "failed.tsv", record, {"a": 1})
    assert not list(tmp_path.glob(".failed.tsv.*"))

    project = SimpleNamespace(root=tmp_path, qc_root=tmp_path / "qc")
    fasta = tmp_path / "assembly.fa"
    fasta.write_text(">a\nA\n", encoding="utf-8")
    monkeypatch.setattr(qc, "fasta_lengths", lambda _path: {"a": 1})
    monkeypatch.setattr(qc, "_write_fasta_length_cache", lambda *_a: (_ for _ in ()).throw(OSError("disk")))
    lengths, info = qc._cached_fasta_lengths(project, record, fasta, {})
    assert lengths == {"a": 1} and info["status"] == "write_failed" and "error" in info


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


def test_pairing_metric_all_early_returns_cache_and_mismatch(tmp_path, monkeypatch):
    project = SimpleNamespace(root=tmp_path)
    base = {
        "file_id": "R1", "sha256": "a", "entity_type": "run", "entity_id": "RUN_1",
        "file_role": "other",
    }
    assert qc._pairing_metric(SimpleNamespace(), project, base, 1) is None

    class DB:
        def __init__(self, row):
            self.conn = self
            self.row = row

        def execute(self, *_a, **_k):
            return _Result(self.row)

    record = {**base, "file_role": "reads_r1"}
    assert qc._pairing_metric(DB(None), project, record, 1) is None
    sibling = {"file_id": "R2", "sha256": "b", "relative_path": "r2.fastq"}
    assert qc._pairing_metric(DB(sibling), project, record, 1) is None
    (tmp_path / "r2.fastq").write_text("@r\nA\n+\n!\n", encoding="utf-8")
    cache = {("R2", "b"): 2}
    metric = qc._pairing_metric(DB(sibling), project, record, 1, read_count_cache=cache)
    assert metric["metric_numeric"] == 0
    cache.clear()
    monkeypatch.setattr(qc, "fastq_record_count", lambda _path: 1)
    metric = qc._pairing_metric(DB(sibling), project, record, 1, read_count_cache=cache)
    assert metric["metric_numeric"] == 1 and cache[("R2", "b")] == 1


def test_qc_missing_file_and_qc_all_filters(tmp_path, monkeypatch):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        with pytest.raises(FileNotFoundError, match="not found in manifest"):
            qc.qc_file(db, project, "FIL_999999")
        calls = []
        monkeypatch.setattr(qc, "qc_file", lambda *_a, **kwargs: calls.append(kwargs) or {"ok": True})
        assert qc.qc_all(
            db, project, entity_type="assembly", entity_id="ASM_1", file_id="FIL_1",
            force_checksum=True,
        ) == []
    finally:
        db.close()
