"""Parity regressions: the Cython parsers must match the pure-Python ones.

`operon.qc_module._parsers` is the required production backend, while
`operon.qc_module.parsers` is its behavioral reference. Both backends must
produce byte-identical metric dicts and identical QCError messages, because QC
metrics feed the versioned rule profiles.
"""

from __future__ import annotations

import gzip

import pytest

from operon.errors import QCError
from operon.qc_module import parsers as py_parsers

from operon.qc_module import _parsers as cy_parsers

FASTA_TEXT = (
    ">ctg1 circular\nACGTNacgtn--RY\nNNN\n"
    ">ctg2\nCCGGTT\n"
    ">ctg1 duplicate\nAAAA\n"
    ">empty\n"
)

FASTQ_TEXT = (
    "@r1\nACGTN\n+\nIIIII\n"
    "@r2\nacgt\n+r2\nIIII\n"
    "@r1\nACGT\n+\n!!!!\n"
)

GFF3_TEXT = (
    "##gff-version 3\n# comment\n"
    "ctg1\tsrc\tgene\t1\t14\t.\t+\t.\tID=g1\n"
    "ctg1\tsrc\tmRNA\t1\t14\t.\t+\t.\tID=m1;Parent=g1\n"
    "ctg1\tsrc\tCDS\t1\t9\t.\t+\t0\tID=c1;Parent=m1,missing\n"
    "ctg1\tsrc\tCDS\t10\t14\t.\t+\t.\tID=c2;Parent=m1\n"
    "ctgX\tsrc\tgene\t1\t5\t.\t+\t.\tParent=none\n"
    "bad\tline\n"
)

PROTEIN_TEXT = ">p1\nMKT*X\n>p1\nMMA*\n>p2\nXXX\n>p3\n\n"


@pytest.fixture
def fasta_file(tmp_path):
    path = tmp_path / "assembly.fa"
    path.write_text(FASTA_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def fastq_file(tmp_path):
    path = tmp_path / "reads.fq"
    path.write_text(FASTQ_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def gff3_file(tmp_path):
    path = tmp_path / "annotation.gff3"
    path.write_text(GFF3_TEXT, encoding="utf-8")
    return path


@pytest.fixture
def protein_file(tmp_path):
    path = tmp_path / "proteins.fa"
    path.write_text(PROTEIN_TEXT, encoding="utf-8")
    return path


def test_fasta_stats_parity(fasta_file):
    assert py_parsers.fasta_stats(fasta_file) == cy_parsers.fasta_stats(fasta_file)


def test_fastq_stats_parity(fastq_file):
    for sample_size in (1, 2, 1000000):
        assert py_parsers.fastq_stats(fastq_file, sample_size=sample_size) == \
            cy_parsers.fastq_stats(fastq_file, sample_size=sample_size)


def test_gff3_stats_parity(gff3_file, fasta_file):
    assert py_parsers.gff3_stats(gff3_file, fasta_file) == cy_parsers.gff3_stats(gff3_file, fasta_file)
    assert py_parsers.gff3_stats(gff3_file) == cy_parsers.gff3_stats(gff3_file)


def test_gff3_diagnostic_timing_contract(gff3_file, fasta_file):
    py_timings = {}
    cy_timings = {}
    assert py_parsers.gff3_stats(gff3_file, fasta_file, timings=py_timings) == \
        cy_parsers.gff3_stats(gff3_file, fasta_file, timings=cy_timings)
    expected = {"assembly_fasta_lengths", "gff3_scan", "gff3_finalize"}
    assert set(py_timings) == set(cy_timings) == expected
    assert all(value >= 0.0 for value in py_timings.values())
    assert all(value >= 0.0 for value in cy_timings.values())

    lengths = py_parsers.fasta_lengths(fasta_file)
    py_cached_timings = {}
    cy_cached_timings = {}
    assert py_parsers.gff3_stats(
        gff3_file, timings=py_cached_timings, fasta_lengths_map=lengths,
    ) == cy_parsers.gff3_stats(
        gff3_file, timings=cy_cached_timings, fasta_lengths_map=lengths,
    )
    expected_cached = {"gff3_scan", "gff3_finalize"}
    assert set(py_cached_timings) == set(cy_cached_timings) == expected_cached


def test_gff3_rejects_two_fasta_length_sources(gff3_file, fasta_file):
    lengths = py_parsers.fasta_lengths(fasta_file)
    with pytest.raises(ValueError, match="either fasta_path or fasta_lengths_map"):
        py_parsers.gff3_stats(gff3_file, fasta_file, fasta_lengths_map=lengths)
    with pytest.raises(ValueError, match="either fasta_path or fasta_lengths_map"):
        cy_parsers.gff3_stats(gff3_file, fasta_file, fasta_lengths_map=lengths)


def test_gff3_ascii_fast_path_preserves_percent_decoding_and_unicode_parity(tmp_path):
    fasta = tmp_path / "unicode.fa"
    fasta.write_text(">ctg\u03b1\n" + "A" * 30 + "\n", encoding="utf-8")
    gff = tmp_path / "mixed.gff3"
    gff.write_text(
        "##gff-version 3\n"
        "ctg\u03b1\tsource\tgene\t1\t30\t.\t+\t.\tID=g%20one;Note=\u03b2\n"
        "ctg\u03b1\tsource\tmRNA\t1\t30\t.\t+\t.\tID=m1;Parent=g%20one\n"
        "ctg\u03b1\tsource\tCDS\t1\t30\t.\t+\t0\tI%44=c1;Parent=m1\n"
        "ctg\u03b1\tsource\texon\t1\t30\t.\t+\t.\tID=;Parent=m1,m%31\n",
        encoding="utf-8",
    )
    assert py_parsers.gff3_stats(gff, fasta) == cy_parsers.gff3_stats(gff, fasta)


def test_protein_stats_parity(protein_file):
    assert py_parsers.protein_stats(protein_file, cds_count=4) == \
        cy_parsers.protein_stats(protein_file, cds_count=4)
    assert py_parsers.protein_stats(protein_file) == cy_parsers.protein_stats(protein_file)


def test_fasta_helpers_parity(fasta_file):
    assert py_parsers.fasta_lengths(fasta_file) == cy_parsers.fasta_lengths(fasta_file)
    assert py_parsers.fasta_record_count(fasta_file) == cy_parsers.fasta_record_count(fasta_file)
    assert list(py_parsers.iter_fasta(fasta_file)) == list(cy_parsers.iter_fasta(fasta_file))


def test_fastq_iterator_parity(fastq_file):
    assert list(py_parsers.iter_fastq(fastq_file)) == list(cy_parsers.iter_fastq(fastq_file))
    assert py_parsers.fastq_record_count(fastq_file) == cy_parsers.fastq_record_count(fastq_file)


def test_parse_attributes_parity():
    attribute_string = "ID=g1;Note=a%20b;empty=;noequals;x=."
    assert py_parsers.parse_attributes(attribute_string) == cy_parsers.parse_attributes(attribute_string)


def test_gzip_inputs_parity(tmp_path, fasta_file, fastq_file):
    fasta_gz = tmp_path / "assembly.fa.gz"
    with gzip.open(fasta_gz, "wt", encoding="utf-8") as handle:
        handle.write(FASTA_TEXT)
    fastq_gz = tmp_path / "reads.fq.gz"
    with gzip.open(fastq_gz, "wt", encoding="utf-8") as handle:
        handle.write(FASTQ_TEXT)
    assert py_parsers.fasta_stats(fasta_gz) == cy_parsers.fasta_stats(fasta_gz)
    assert py_parsers.fastq_stats(fastq_gz) == cy_parsers.fastq_stats(fastq_gz)


@pytest.mark.parametrize("content,loader", [
    ("ACGT\n>h\nACGT\n", "fasta"),          # sequence before first header
    (">h1\nACGT\n>\nAC\n", "fasta"),        # empty header
    ("r1\nACGT\n+\nIIII\n", "fastq"),       # header missing '@'
    ("@r1\nACGT\n+\nIII\n", "fastq"),       # sequence/quality length mismatch
    ("@r1\nACGT\nplus\nIIII\n", "fastq"),   # malformed plus line
])
def test_error_message_parity(tmp_path, content, loader):
    path = tmp_path / ("bad.fa" if loader == "fasta" else "bad.fq")
    path.write_text(content, encoding="utf-8")
    py_stats = py_parsers.fasta_stats if loader == "fasta" else py_parsers.fastq_stats
    cy_stats = cy_parsers.fasta_stats if loader == "fasta" else cy_parsers.fastq_stats
    with pytest.raises(QCError) as py_exc:
        py_stats(path)
    with pytest.raises(QCError) as cy_exc:
        cy_stats(path)
    assert str(py_exc.value) == str(cy_exc.value)


def test_empty_files_parity(tmp_path):
    fasta = tmp_path / "empty.fa"
    fasta.write_text("", encoding="utf-8")
    fastq = tmp_path / "empty.fq"
    fastq.write_text("", encoding="utf-8")
    assert py_parsers.fasta_stats(fasta) == cy_parsers.fasta_stats(fasta)
    assert py_parsers.fastq_stats(fastq) == cy_parsers.fastq_stats(fastq)


def test_fasta_iterator_validates_the_next_header_lazily(tmp_path):
    path = tmp_path / "lazy.fa"
    path.write_bytes(b">ok\nAC\n>\xff\nGT\n")
    py_iter = py_parsers.iter_fasta(path)
    cy_iter = cy_parsers.iter_fasta(path)
    assert next(py_iter) == next(cy_iter) == ("ok", "AC")
    with pytest.raises(QCError) as py_exc:
        next(py_iter)
    with pytest.raises(QCError) as cy_exc:
        next(cy_iter)
    assert str(py_exc.value) == str(cy_exc.value)
