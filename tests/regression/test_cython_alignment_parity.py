"""Parity regressions: the Cython alignment QC must match the reference.

`operon.qc._alignment` is the required production backend, while
`operon.qc.alignment` is its behavioral reference. Both backends must
produce equal AlignmentQCResult values, byte-identical report files, and
identical QCError messages, because QC metrics feed downstream consumers.
"""

from __future__ import annotations

import random

import pytest

from operon.errors import QCError
from operon.qc import _alignment as cy_alignment
from operon.qc import alignment as py_alignment
from operon.qc.alignment import write_alignment_qc


def assert_result_parity(records):
    py_result = py_alignment.compute_alignment_qc(iter(records))
    cy_result = cy_alignment.compute_alignment_qc(iter(records))
    assert py_result == cy_result


def test_consensus_tie_first_seen_wins():
    records = [("a", "A"), ("b", "C"), ("c", "C"), ("d", "A")]
    assert_result_parity(records)
    result = cy_alignment.compute_alignment_qc(records)
    assert result.column_rows[0]["consensus"] == "A"


def test_consensus_tie_first_seen_wins_reversed():
    records = [("a", "C"), ("b", "A"), ("c", "A"), ("d", "C")]
    assert_result_parity(records)
    result = cy_alignment.compute_alignment_qc(records)
    assert result.column_rows[0]["consensus"] == "C"


def test_all_gap_column():
    assert_result_parity([
        ("a", "A-C"),
        ("b", "G.T"),
        ("c", "T-A"),
    ])


def test_dot_gaps():
    assert_result_parity([
        ("a", "AC.T"),
        ("b", "A..T"),
        ("c", "GC.A"),
    ])


def test_mixed_case_and_ambiguous_residues():
    assert_result_parity([
        ("a", "ACGTRYSWKMBDHVNacgtn"),
        ("b", "acgtryswkmbdhvnACGTN"),
        ("c", "AR-N.YrXxZz*!?acgt-A"),
    ])


def test_single_sequence():
    assert_result_parity([("only", "ACGT-AC.GT")])


def test_zero_length_sequences():
    assert_result_parity([("a", ""), ("b", ""), ("c", "")])


def test_non_ascii_residue_fallback():
    assert_result_parity([
        ("a", "ACΩT"),
        ("b", "ACGT"),
        ("c", "AΩ-T"),
    ])
    result = cy_alignment.compute_alignment_qc([
        ("a", "ACΩT"),
        ("b", "ACGT"),
        ("c", "AΩ-T"),
    ])
    assert result.column_rows[1]["consensus"] == "C"
    assert result.column_rows[2]["distinct_residues"] == 2


def test_non_ascii_residue_tie_across_backends():
    # Ω appears first in a fallback sequence, A first in an ASCII sequence;
    # the tie must be resolved by cross-source first-seen order.
    assert_result_parity([
        ("a", "Ω"),
        ("b", "A"),
        ("c", "Ω"),
        ("d", "A"),
    ])
    result = cy_alignment.compute_alignment_qc([
        ("a", "Ω"),
        ("b", "A"),
        ("c", "Ω"),
        ("d", "A"),
    ])
    assert result.column_rows[0]["consensus"] == "Ω"


def test_seeded_random_alignment():
    rng = random.Random(20260911)
    alphabet = "ACDEFGHIKLMNPQRSTVWYBXZacdefg" + "-" * 15 + "." * 10
    records = [
        (f"seq{i:04d}", "".join(rng.choice(alphabet) for _ in range(800)))
        for i in range(200)
    ]
    assert_result_parity(records)


def test_error_message_parity():
    for records in (
        [],
        [("a", "ACGT"), ("b", "AC"), ("c", "ACGT")],
        [(f"s{i}", "A" * (i + 1)) for i in range(12)],
    ):
        with pytest.raises(QCError) as py_exc:
            py_alignment.compute_alignment_qc(iter(records))
        with pytest.raises(QCError) as cy_exc:
            cy_alignment.compute_alignment_qc(iter(records))
        assert str(py_exc.value) == str(cy_exc.value)


def test_report_files_byte_parity(tmp_path):
    rng = random.Random(7)
    alphabet = "ACGTNacgtn-."
    records = [
        (f"seq{i:03d}", "".join(rng.choice(alphabet) for _ in range(120)))
        for i in range(40)
    ]
    py_result = py_alignment.compute_alignment_qc(iter(records))
    cy_result = cy_alignment.compute_alignment_qc(iter(records))
    write_alignment_qc(py_result, tmp_path / "py")
    write_alignment_qc(cy_result, tmp_path / "cy")
    for name in ("sequence_qc.tsv", "column_qc.tsv", "alignment_qc.json"):
        assert (tmp_path / "py" / name).read_bytes() == \
            (tmp_path / "cy" / name).read_bytes()


def test_alignment_qc_file_parity(tmp_path):
    path = tmp_path / "aln.faa"
    path.write_text(
        ">s1\nAC-TRYSW\n>s2\nAC.AKMBD\n>s3\nGC-Trysw\n>s4\nGCTA----\n",
        encoding="utf-8",
    )
    assert py_alignment.alignment_qc(path) == cy_alignment.alignment_qc(path)
