"""Benchmark: pure-Python reference vs Cython alignment QC backends.

Deterministic (fixed seed).  For each alignment size, reports the median
wall time of the core-only `compute_alignment_qc(records)` (5 runs) and of
the end-to-end `alignment_qc(file)` (3 runs; both backends parse via the
same `operon.qc._parsers.iter_fasta`, so end-to-end includes shared FASTA
parsing).

Run with: .venv/bin/python benchmarks/alignment_qc_benchmark.py
"""

from __future__ import annotations

import random
import statistics
import tempfile
import time
from pathlib import Path

from operon.qc import _alignment as cy_alignment
from operon.qc import alignment as py_alignment

SEED = 20260911
SIZES = [(300, 1200), (2000, 3000), (150, 20000)]
CORE_RUNS = 5
E2E_RUNS = 3

# protein-ish alphabet: 20 amino acids + a few ambiguities, ~30% gaps
ALPHABET = (
    "ACDEFGHIKLMNPQRSTVWY" + "BXZ" + "acdefg"
    + "-" * 12 + "." * 6
)


def make_alignment(n_sequences: int, n_columns: int):
    rng = random.Random(SEED)
    return [
        (f"seq{i:05d}", "".join(rng.choice(ALPHABET) for _ in range(n_columns)))
        for i in range(n_sequences)
    ]


def median_time(func, runs: int) -> float:
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        func()
        samples.append(time.perf_counter() - start)
    return statistics.median(samples)


def main() -> None:
    print(f"alignment QC backend benchmark (seed={SEED}, "
          f"core median of {CORE_RUNS}, e2e median of {E2E_RUNS})")
    print()
    header = (
        f"{'size (seqs x cols)':>18}  {'backend':>8}  "
        f"{'core (s)':>10}  {'e2e (s)':>10}  "
        f"{'core speedup':>12}  {'e2e speedup':>11}"
    )
    print(header)
    print("-" * len(header))
    with tempfile.TemporaryDirectory() as tmp:
        for n_sequences, n_columns in SIZES:
            records = make_alignment(n_sequences, n_columns)
            path = Path(tmp) / f"aln_{n_sequences}x{n_columns}.faa"
            path.write_text(
                "".join(f">{header}\n{sequence}\n" for header, sequence in records),
                encoding="utf-8",
            )
            size = f"{n_sequences}x{n_columns}"

            py_core = median_time(
                lambda: py_alignment.compute_alignment_qc(iter(records)), CORE_RUNS)
            cy_core = median_time(
                lambda: cy_alignment.compute_alignment_qc(iter(records)), CORE_RUNS)
            py_e2e = median_time(lambda: py_alignment.alignment_qc(path), E2E_RUNS)
            cy_e2e = median_time(lambda: cy_alignment.alignment_qc(path), E2E_RUNS)

            print(f"{size:>18}  {'python':>8}  {py_core:>10.4f}  {py_e2e:>10.4f}  "
                  f"{'1.00x':>12}  {'1.00x':>11}")
            print(f"{'':>18}  {'cython':>8}  {cy_core:>10.4f}  {cy_e2e:>10.4f}  "
                  f"{py_core / cy_core:>11.2f}x  {py_e2e / cy_e2e:>10.2f}x")


if __name__ == "__main__":
    main()
