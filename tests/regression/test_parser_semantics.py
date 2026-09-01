"""Golden behavior tests shared by the reference and production parsers."""

from __future__ import annotations

import gzip

import pytest

from operon.errors import QCError
from operon.qc_module import _parsers as cy_parsers
from operon.qc_module import parsers as py_parsers


BACKENDS = [pytest.param(py_parsers, id="python"), pytest.param(cy_parsers, id="cython")]


@pytest.mark.parametrize("backend", BACKENDS)
def test_fasta_record_boundaries_headers_and_lone_cr(tmp_path, backend):
    path = tmp_path / "records.fa"
    path.write_bytes(b">a circular\rAN\r>b\rNA\r>a circular\rTT\r")
    stats = backend.fasta_stats(path)
    assert stats["sequence_count"] == 3
    assert stats["total_length"] == 6
    assert stats["gap_count"] == 0
    assert stats["duplicate_sequence_id_count"] == 1
    assert stats["duplicate_header_count"] == 1
    assert stats["circular_sequence_count"] == 2


@pytest.mark.parametrize("backend", BACKENDS)
def test_fasta_sequence_must_be_ascii(tmp_path, backend):
    path = tmp_path / "unicode.fa"
    path.write_bytes(">x\nAéT\n".encode())
    with pytest.raises(QCError, match="non-ASCII data in FASTA sequence"):
        backend.fasta_stats(path)


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(("content", "message"), [
    (b"@r1\n", "missing sequence"),
    (b"@r1\nACGT\n", "missing plus line"),
    (b"@r1\nACGT\n+\n", "missing quality"),
    (b"@r1\nACGT\n\nIIII\n", "plus line malformed"),
    (b"@r1\nAC\n+\nII\n\n@r2\nGT\n+\nII\n", "blank line where FASTQ header was expected"),
])
def test_fastq_rejects_truncated_or_blank_records(tmp_path, backend, content, message):
    path = tmp_path / "bad.fq"
    path.write_bytes(content)
    with pytest.raises(QCError, match=message):
        backend.fastq_stats(path)


@pytest.mark.parametrize("backend", BACKENDS)
def test_fastq_defaults_to_modern_phred33_and_exposes_ambiguous_auto(tmp_path, backend):
    path = tmp_path / "high-quality.fq"
    path.write_bytes(b"@r1\rACGT\r+\rKKKK\r")
    default = backend.fastq_stats(path)
    assert default["quality_encoding"] == "sanger_phred33"
    assert default["q20_percent"] == 100.0
    assert default["q30_percent"] == 100.0
    automatic = backend.fastq_stats(path, phred_offset="auto")
    assert automatic["quality_encoding"] == "ambiguous_assumed_phred33"
    explicit_64 = backend.fastq_stats(path, phred_offset=64)
    assert explicit_64["quality_encoding"] == "illumina_phred64"
    assert explicit_64["q20_percent"] == 0.0


@pytest.mark.parametrize("backend", BACKENDS)
def test_fastq_sampling_is_exact_and_described(tmp_path, backend):
    path = tmp_path / "sample.fq"
    path.write_bytes(
        b"@r1\nAAAA\n+\nIIII\n"
        b"@r2\nAAAA\n+\nIIII\n"
        b"@r3\nCCCC\n+\nIIII\n"
    )
    stats = backend.fastq_stats(path, sample_size=2)
    assert stats["duplicate_percent"] == 50.0
    assert stats["duplicate_sampled_read_count"] == 2
    assert stats["duplicate_is_sampled"] is True
    assert stats["duplicate_sampling_strategy"] == "first_n"
    assert backend.fastq_record_count(path) == 3


@pytest.mark.parametrize("backend", BACKENDS)
def test_fastq_length_histogram_preserves_l50_for_odd_totals(tmp_path, backend):
    path = tmp_path / "lengths.fq"
    path.write_bytes(
        b"@r1\nAA\n+\nII\n"
        b"@r2\nCC\n+\nII\n"
        b"@r3\nG\n+\nI\n"
    )
    stats = backend.fastq_stats(path)
    assert stats["read_length_n50"] == 2.0
    assert stats["read_length_l50"] == 2


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("sample_size", [0, -1, True, 1.5])
def test_fastq_sample_size_must_be_a_positive_integer(tmp_path, backend, sample_size):
    path = tmp_path / "reads.fq"
    path.write_bytes(b"@r1\nA\n+\nI\n")
    with pytest.raises(ValueError, match="positive integer"):
        backend.fastq_stats(path, sample_size=sample_size)


@pytest.mark.parametrize("backend", BACKENDS)
def test_all_text_parsers_recognize_lone_cr_and_gzip(tmp_path, backend):
    fasta = tmp_path / "assembly.fa.gz"
    with gzip.open(fasta, "wb") as handle:
        handle.write(b">ctg1\rACGT\r")
    gff = tmp_path / "annotation.gff3.gz"
    with gzip.open(gff, "wb") as handle:
        handle.write(b"##gff-version 3\rctg1\tsrc\tgene\t1\t4\t.\t+\t.\tID=g1\r")
    assert backend.fasta_record_count(fasta) == 1
    assert backend.gff3_stats(gff, fasta)["gene_count"] == 1


@pytest.mark.parametrize("backend", BACKENDS)
def test_fasta_stats_crlf_golden_within_single_chunk(tmp_path, backend):
    # The file is far smaller than the default 1 MiB chunk, so every CRLF is
    # handled by the in-chunk branch of the line splitter (not the carry-over
    # path exercised by test_line_splitter_handles_crlf_across_chunks).
    path = tmp_path / "crlf.fa"
    path.write_bytes(b">ctg1 circular\r\nACGTN\r\nNN\r\n>ctg2\r\nCCGGTT\r\n")
    stats = backend.fasta_stats(path)
    assert stats == {
        "sequence_count": 2,
        "total_length": 13,
        "min_sequence_length": 6,
        "max_sequence_length": 7,
        "mean_sequence_length": 6.5,
        "median_sequence_length": 6.5,
        "contig_n50": 7.0,
        "contig_l50": 1,
        "contig_n90": 6.0,
        "contig_l90": 2,
        "gc_percent": 60.0,
        "n_percent": 300.0 / 13,
        "ambiguous_base_percent": 0.0,
        "invalid_base_count": 0,
        "gap_count": 0,
        "gap_percent": 0.0,
        "empty_sequence_count": 0,
        "duplicate_sequence_id_count": 0,
        "duplicate_header_count": 0,
        "circular_sequence_count": 1,
    }


@pytest.mark.parametrize("backend", BACKENDS)
def test_fasta_stats_dash_gaps_are_separate_from_n(tmp_path, backend):
    # `-` is an alignment gap character: it feeds gap runs and gap_percent but
    # never n_percent, ambiguous_base_percent, or invalid_base_count.
    path = tmp_path / "aligned.fa"
    path.write_bytes(b">x\nAC-GT--N\r\n")
    stats = backend.fasta_stats(path)
    assert stats["total_length"] == 8
    assert stats["n_percent"] == 12.5
    assert stats["gap_count"] == 2
    assert stats["gap_percent"] == 37.5
    assert stats["ambiguous_base_percent"] == 0.0
    assert stats["invalid_base_count"] == 0


@pytest.mark.parametrize("backend", BACKENDS)
def test_line_splitter_handles_crlf_across_chunks(backend, tmp_path):
    path = tmp_path / "lines.txt"
    path.write_bytes(b"a\r\nb\rc\n" + b"d" * 10 + b"\n")
    assert list(backend._iter_binary_lines(path, chunk_size=2)) == [b"a", b"b", b"c", b"d" * 10]
