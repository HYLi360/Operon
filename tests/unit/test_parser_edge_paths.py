"""Rare structural and error branches in the pure-Python QC parser reference."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from operon.errors import QCError
from operon.qc_module import parsers


def test_binary_line_endings_across_one_byte_chunks_and_trailing_line(tmp_path):
    path = tmp_path / "lines"
    path.write_bytes(b"a\r\nb\rc\nd")
    assert list(parsers._iter_binary_lines(path, chunk_size=1)) == [b"a", b"b", b"c", b"d"]


def test_fasta_unicode_space_header_and_empty_sequence_statistics(tmp_path):
    bad = tmp_path / "bad.fa"
    bad.write_bytes(">\xc2\xa0\nA\n".encode("latin1"))
    with pytest.raises(QCError, match="empty FASTA header"):
        list(parsers.iter_fasta(bad))

    fasta = tmp_path / "edge.fa"
    fasta.write_text(
        ">one topology:circular\nNNRY?\n"
        ">one topology:circular\n\n"
        ">three\nA\n",
        encoding="utf-8",
    )
    stats = parsers.fasta_stats(fasta)
    assert stats["empty_sequence_count"] == 1
    assert stats["duplicate_sequence_id_count"] == 1
    assert stats["duplicate_header_count"] == 1
    assert stats["circular_sequence_count"] == 2
    assert stats["invalid_base_count"] == 1 and stats["ambiguous_base_percent"] > 0


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"@ \nA\n+\n!\n", "empty FASTQ identifier"),
        (b"@r\nA\n+\n \n", "quality character outside"),
    ],
)
def test_fastq_identifier_and_quality_range_errors(tmp_path, content, message):
    path = tmp_path / "bad.fastq"
    path.write_bytes(content)
    with pytest.raises(QCError, match=message):
        list(parsers.iter_fastq(path))


def test_phred_and_nx_edge_branches():
    with pytest.raises(QCError, match=r"phred\+64 minimum"):
        parsers._resolve_phred_offset(64, 63, 100)
    with pytest.raises(ValueError, match="phred_offset"):
        parsers._resolve_phred_offset(42, 70, 80)
    assert parsers._resolve_phred_offset("auto", 70, -1)[1] == "sanger_phred33"
    assert parsers._resolve_phred_offset("auto", 70, 80)[1] == "ambiguous_assumed_phred33"
    assert parsers._nx_from_histogram(Counter({0: 2}), 0, 0.5) == (0.0, 1)


def test_attributes_empty_chunks_percent_decoding_and_ignored_tokens():
    assert parsers.parse_attributes("") == {}
    assert parsers.parse_attributes(".") == {}
    assert parsers.parse_attributes(";ignored;ID=x%201;;=bad;Parent=") == {
        "ID": "x 1", "Parent": ""
    }


def test_gff3_all_coordinate_identity_and_boundary_errors(tmp_path):
    gff = tmp_path / "edge.gff3"
    gff.write_text(
        "\n"
        "##gff-version 3\n"
        ">ignored-fasta-header\n"
        "malformed\n"
        "chr1\ts\tgene\tbad\t2\t.\t+\t.\tID=g0\n"
        "chr1\ts\tgene\t0\t2\t.\t+\t.\tID=g1\n"
        "missing\ts\tgene\t1\t2\t.\t+\t.\tID=g2\n"
        "chr1\ts\tgene\t1\t20\t.\t+\t.\tID=g2\n"
        "chr1\ts\tmRNA\t1\t3\t.\t+\t.\tParent=missing, ;\n"
        "chr1\ts\tCDS\t1\t4\t.\t+\tx\tID=c1;Parent=g2\n"
        "chr1\ts\tgene\t1\t2\t.\t+\t.\tID=g2\n"
        "##FASTA\n"
        "chr1\ts\tgene\t1\t2\t.\t+\t.\tID=after\n",
        encoding="utf-8",
    )
    timings = {}
    stats = parsers.gff3_stats(gff, fasta_lengths_map={"chr1": 10}, timings=timings)
    assert stats["directive_count"] == 1
    assert stats["coordinate_error_count"] >= 4
    assert stats["seqid_mismatch_count"] == 1
    assert stats["end_beyond_sequence_count"] == 1
    assert stats["missing_id_count"] == 1
    assert stats["duplicate_id_count"] >= 1
    assert stats["missing_parent_count"] == 1
    assert stats["cds_not_multiple3_count"] == 1
    assert "gff3_scan" in timings and "gff3_finalize" in timings
    with pytest.raises(ValueError, match="either fasta_path"):
        parsers.gff3_stats(gff, fasta_path=tmp_path / "x", fasta_lengths_map={})


def test_protein_empty_duplicate_stops_and_optional_match(tmp_path):
    path = tmp_path / "proteins.fa"
    path.write_text(">p\n\n>p\nAX*X\n>q\nMXX\n", encoding="utf-8")
    stats = parsers.protein_stats(path, cds_count=3)
    assert stats["protein_empty_count"] == 1
    assert stats["protein_duplicate_id_count"] == 1
    assert stats["protein_internal_stop_count"] == 1
    assert stats["protein_missing_start_count"] == 1
    assert stats["protein_missing_stop_count"] == 2
    assert stats["cds_protein_count_match"] == 1
