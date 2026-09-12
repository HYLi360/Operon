"""Alignment QC: parity against a naive reference, error paths, writers, CLI."""

from __future__ import annotations

import json
import random
import tempfile
from collections import Counter
from pathlib import Path

import pytest

from operon.qc import (
    AlignmentQCResult,
    alignment_qc,  # noqa: F821
    compute_alignment_qc,  # noqa: F821
    write_alignment_qc,
)
from operon.cli import main
from operon.errors import QCError

pytestmark = pytest.mark.unit


def naive_alignment_qc(records):
    """Direct translation of the legacy cmd_alignment_qc double loop."""
    records = list(records)
    if not records:
        raise QCError("alignment is empty")
    lengths = {len(sequence) for _, sequence in records}
    if len(lengths) != 1:
        raise QCError(f"alignment sequences have unequal lengths: {sorted(lengths)[:10]}")
    aln_len = lengths.pop()
    sequence_rows = []
    for header, sequence in records:
        nongap = sum(char not in "-." for char in sequence)
        sequence_rows.append({
            "safe_id": header,
            "alignment_length": aln_len,
            "non_gap_sites": nongap,
            "coverage": f"{nongap / aln_len:.6f}" if aln_len else "0",
            "gap_fraction": f"{1 - nongap / aln_len:.6f}" if aln_len else "1",
        })
    column_rows = []
    for col in range(aln_len):
        chars = [sequence[col] for _, sequence in records]
        nongap_chars = [char for char in chars if char not in "-."]
        counts = Counter(nongap_chars)
        column_rows.append({
            "column_1based": col + 1,
            "occupancy": f"{len(nongap_chars) / len(records):.6f}",
            "distinct_residues": len(counts),
            "consensus": counts.most_common(1)[0][0] if counts else "-",
            "consensus_fraction": (
                f"{counts.most_common(1)[0][1] / len(nongap_chars):.6f}" if nongap_chars else "0"
            ),
        })
    coverages = sorted(float(row["coverage"]) for row in sequence_rows)
    summary = {
        "sequences": len(records),
        "alignment_length": aln_len,
        "mean_coverage": sum(coverages) / len(coverages),
        "median_coverage": coverages[len(coverages) // 2],
        "columns_occupancy_ge_0_9": sum(float(row["occupancy"]) >= 0.9 for row in column_rows),
        "columns_occupancy_ge_0_7": sum(float(row["occupancy"]) >= 0.7 for row in column_rows),
    }
    return sequence_rows, column_rows, summary


def formatted_result(result: AlignmentQCResult):
    """Render a result through the public write layer into comparable dicts."""
    from operon.qc.alignment import (
        _format_consensus_fraction,
        _format_coverage,
        _format_gap_fraction,
        _format_occupancy,
    )
    sequence_rows = [{
        "safe_id": row["safe_id"],
        "alignment_length": row["alignment_length"],
        "non_gap_sites": row["non_gap_sites"],
        "coverage": _format_coverage(row),
        "gap_fraction": _format_gap_fraction(row),
    } for row in result.sequence_rows]
    column_rows = [{
        "column_1based": row["column_1based"],
        "occupancy": _format_occupancy(row),
        "distinct_residues": row["distinct_residues"],
        "consensus": row["consensus"],
        "consensus_fraction": _format_consensus_fraction(row),
    } for row in result.column_rows]
    return sequence_rows, column_rows, result.summary


def assert_parity(records):
    expected = naive_alignment_qc(records)
    actual = formatted_result(compute_alignment_qc(iter(records)))
    assert actual == expected


def random_alignment(rng: random.Random, n_sequences: int, n_columns: int):
    alphabet = "ACGT-."
    return [
        (f"seq{i:04d}", "".join(rng.choice(alphabet) for _ in range(n_columns)))
        for i in range(n_sequences)
    ]


class TestNaiveParity:
    # Corpora already covered by tests/regression/test_cython_alignment_parity.py
    # (dot gaps, single sequence, all-gap column, consensus tie) are deliberately
    # not repeated here; the regression file runs both backends on them.

    def test_single_residue_column(self):
        assert_parity([
            ("a", "AAAA"),
            ("b", "-A-A"),
            ("c", ".AA."),
        ])

    def test_length_one_column(self):
        assert_parity([("a", "A"), ("b", "-"), ("c", "G")])

    def test_all_gap_sequence(self):
        assert_parity([
            ("a", "----"),
            ("b", "ACGT"),
        ])

    def test_even_sequence_count_median(self):
        assert_parity([
            ("a", "AAAA"),
            ("b", "AA--"),
            ("c", "A---"),
            ("d", "----"),
        ])

    def test_random_medium_alignment(self):
        rng = random.Random(20260911)
        assert_parity(random_alignment(rng, 300, 200))

    def test_random_alignment_with_empty_sequences(self):
        assert_parity([("a", ""), ("b", ""), ("c", "")])


class TestErrors:
    def test_empty_alignment(self):
        with pytest.raises(QCError, match="alignment is empty"):
            compute_alignment_qc([])

    def test_empty_alignment_file(self, tmp_path):
        path = tmp_path / "empty.faa"
        path.write_text("", encoding="utf-8")
        with pytest.raises(QCError, match="alignment is empty"):
            alignment_qc(path)

    def test_unequal_lengths(self):
        with pytest.raises(QCError, match=r"unequal lengths: \[2, 4\]"):
            compute_alignment_qc([("a", "ACGT"), ("b", "AC"), ("c", "ACGT")])

    def test_unequal_lengths_lists_up_to_ten(self):
        records = [(f"s{i}", "A" * (i + 1)) for i in range(12)]
        with pytest.raises(QCError) as excinfo:
            compute_alignment_qc(records)
        message = str(excinfo.value)
        assert "unequal lengths" in message
        assert "[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]" in message


class TestWriters:
    RECORDS = [
        ("s1", "AC-T"),
        ("s2", "AC.A"),
    ]

    def _result(self) -> AlignmentQCResult:
        return compute_alignment_qc(self.RECORDS)

    def test_sequence_qc_tsv_bytes(self, tmp_path):
        write_alignment_qc(self._result(), tmp_path / "out")
        expected = (
            "safe_id\talignment_length\tnon_gap_sites\tcoverage\tgap_fraction\r\n"
            "s1\t4\t3\t0.750000\t0.250000\r\n"
            "s2\t4\t3\t0.750000\t0.250000\r\n"
        )
        assert (tmp_path / "out" / "sequence_qc.tsv").read_bytes() == expected.encode("utf-8")

    def test_column_qc_tsv_bytes(self, tmp_path):
        write_alignment_qc(self._result(), tmp_path / "out")
        expected = (
            "column_1based\toccupancy\tdistinct_residues\tconsensus\tconsensus_fraction\r\n"
            "1\t1.000000\t1\tA\t1.000000\r\n"
            "2\t1.000000\t1\tC\t1.000000\r\n"
            "3\t0.000000\t0\t-\t0\r\n"
            "4\t1.000000\t2\tT\t0.500000\r\n"
        )
        assert (tmp_path / "out" / "column_qc.tsv").read_bytes() == expected.encode("utf-8")

    def test_summary_json_bytes(self, tmp_path):
        result = self._result()
        write_alignment_qc(result, tmp_path / "out")
        expected_summary = {
            "sequences": 2,
            "alignment_length": 4,
            "mean_coverage": 0.75,
            "median_coverage": 0.75,
            "columns_occupancy_ge_0_9": 3,
            "columns_occupancy_ge_0_7": 3,
        }
        assert result.summary == expected_summary
        payload = tmp_path / "out" / "alignment_qc.json"
        assert payload.read_bytes() == (
            json.dumps(expected_summary, indent=2) + "\n"
        ).encode("utf-8")

    def test_zero_length_alignment_strings(self):
        result = compute_alignment_qc([("a", ""), ("b", "")])
        assert result.sequence_rows[0]["coverage"] == 0.0
        assert result.sequence_rows[0]["gap_fraction"] == 1.0
        assert result.summary["mean_coverage"] == 0.0

    def test_outdir_created(self, tmp_path):
        outdir = tmp_path / "nested" / "deeper" / "out"
        write_alignment_qc(self._result(), outdir)
        assert (outdir / "sequence_qc.tsv").is_file()
        assert (outdir / "column_qc.tsv").is_file()
        assert (outdir / "alignment_qc.json").is_file()


class TestCli:
    def _write_alignment(self, directory: Path) -> Path:
        path = directory / "aln.faa"
        path.write_text(">s1\nAC-T\n>s2\nAC.A\n", encoding="utf-8")
        return path

    def test_cli_runs_without_project(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        alignment = self._write_alignment(tmp_path)
        outdir = tmp_path / "qc_out"
        exit_code = main([
            "alignment-qc", "--alignment", str(alignment), "--outdir", str(outdir),
        ])
        assert exit_code == 0
        captured = capsys.readouterr()
        summary = json.loads(captured.out)
        assert summary == {
            "sequences": 2,
            "alignment_length": 4,
            "mean_coverage": 0.75,
            "median_coverage": 0.75,
            "columns_occupancy_ge_0_9": 3,
            "columns_occupancy_ge_0_7": 3,
        }
        assert captured.out == json.dumps(summary, indent=2) + "\n"
        assert (outdir / "sequence_qc.tsv").is_file()
        assert (outdir / "column_qc.tsv").is_file()
        assert (outdir / "alignment_qc.json").is_file()

    def test_cli_error_exit_code(self, tmp_path, capsys):
        alignment = tmp_path / "bad.faa"
        alignment.write_text(">a\nACGT\n>b\nAC\n", encoding="utf-8")
        exit_code = main([
            "alignment-qc", "--alignment", str(alignment), "--outdir", str(tmp_path / "out"),
        ])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "unequal lengths" in captured.err
        assert captured.out == ""


class TestPerformanceSmoke:
    def test_large_alignment_completes(self, tmp_path):
        rng = random.Random(42)
        records = random_alignment(rng, 2000, 500)
        path = tmp_path / "large.faa"
        path.write_text(
            "".join(f">{header}\n{sequence}\n" for header, sequence in records),
            encoding="utf-8",
        )
        result = alignment_qc(path)
        assert result.summary["sequences"] == 2000
        assert result.summary["alignment_length"] == 500
        assert len(result.column_rows) == 500
        write_alignment_qc(result, tmp_path / "out")
        assert (tmp_path / "out" / "alignment_qc.json").is_file()
