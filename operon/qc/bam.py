#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""BAM/CRAM alignment-file QC measurement via pysam.

``bam_stats`` is the alignment-file counterpart of the streaming text parsers
in ``operon.qc.parsers``: it measures only (INV-4 — PASS/FAIL stays with the
versioned YAML profiles) and mirrors the counting conventions of
``samtools flagstat``/``stats``/``coverage`` so the numbers are comparable
with existing reports.  Unlike the text parsers it needs no Cython twin —
pysam/htslib is already the compiled backend.

Coverage breadth is computed with one difference array per reference
(4 bytes per base).  A reference whose length exceeds
``COVERAGE_MAX_REFERENCE_BASES`` — or that would push the accumulated
allocation past ``COVERAGE_MAX_TOTAL_BASES`` — is skipped; when any reference
is skipped the breadth/depth-threshold metrics are reported as ``None``
(dropped from storage) and ``coverage_breadth_skipped`` is set to 1, because
a partial breadth over a subset of references would be a dishonest aggregate.
``mean_depth`` is always exact: it only needs the aligned-block base sum and
the header lengths, not the sweep.
"""

from __future__ import annotations

import itertools
from array import array
from collections import Counter
from math import sqrt
from pathlib import Path
from typing import Any

import pysam

from operon.errors import QCError
from operon.utils import pct

#: Per-reference breadth arrays are only allocated within these budgets.
COVERAGE_MAX_REFERENCE_BASES = 250_000_000
COVERAGE_MAX_TOTAL_BASES = 1_000_000_000

_OPEN_MODES = {"bam": "rb", "cram": "rc"}

#: Reads excluded from coverage/depth accounting: unmapped, secondary,
#: QC-failed.  Supplementary and duplicate reads stay in.
_COVERAGE_EXCLUDE_FLAG = 0x4 | 0x100 | 0x200


def _median_from_histogram(counts: Counter[int], total: int) -> float:
    """Median of a value histogram without materializing the value list."""
    if not total:
        return 0.0
    middle1 = (total - 1) // 2
    middle2 = total // 2
    cumulative = 0
    value1 = value2 = None
    for value in sorted(counts):
        cumulative += counts[value]
        if value1 is None and cumulative > middle1:
            value1 = value
        if cumulative > middle2:
            value2 = value
            break
    assert value1 is not None and value2 is not None
    return (value1 + value2) / 2.0


def bam_stats(path: str | Path, *, file_format: str) -> dict[str, Any]:
    """Measure alignment QC metrics for one BAM or CRAM file.

    ``file_format`` is the manifest-recorded format; the file is opened in the
    matching mode so content/format disagreement fails loudly.  Any htslib
    failure (corrupt file, CRAM without a reachable reference, ...) is
    re-raised as :class:`QCError` so callers record ``parseable = 0``.
    """
    path = Path(path)
    mode = _OPEN_MODES.get(file_format)
    if mode is None:
        raise ValueError(f"file_format must be one of {sorted(_OPEN_MODES)}")
    try:
        with pysam.AlignmentFile(str(path), mode) as handle:
            return _scan(handle)
    except (OSError, ValueError) as exc:
        raise QCError(f"{path}: cannot parse {file_format}: {exc}") from exc


def _scan(handle: pysam.AlignmentFile) -> dict[str, Any]:
    lengths = [int(length) for length in handle.lengths]
    reference_count = len(lengths)
    total_reference_bases = sum(lengths)

    alignment_count = 0
    qc_failed = 0
    primary = 0
    secondary = 0
    supplementary = 0
    mapped = 0
    primary_mapped = 0
    unmapped = 0
    duplicates = 0
    paired = 0
    read1 = 0
    read2 = 0
    proper_pair = 0
    both_mates_mapped = 0
    singletons = 0
    mate_different_reference = 0
    mate_different_reference_mapq5 = 0
    mapq_ge30 = 0
    length_min: int | None = None
    length_max = 0
    length_total = 0
    length_n = 0
    gc = 0
    atgc = 0
    nm_sum = 0
    nm_bases = 0
    nm_present = 0
    insert_sizes: Counter[int] = Counter()
    references_with_mapped: set[int] = set()
    coverage_bases = 0
    diffs: dict[int, array] = {}
    skipped_references: set[int] = set()
    diff_total = 0

    for read in handle:
        alignment_count += 1
        flag = read.flag
        if flag & 0x200:
            qc_failed += 1
        is_secondary = bool(flag & 0x100)
        is_supplementary = bool(flag & 0x800)
        if is_secondary:
            secondary += 1
        elif is_supplementary:
            supplementary += 1
        else:
            primary += 1
        is_unmapped = bool(flag & 0x4)
        if is_unmapped:
            unmapped += 1
        else:
            mapped += 1
            references_with_mapped.add(read.reference_id)
            if not is_secondary and not is_supplementary:
                primary_mapped += 1
                if read.mapping_quality >= 30:
                    mapq_ge30 += 1
        if flag & 0x400:
            duplicates += 1
        if flag & 0x1:
            paired += 1
            if flag & 0x40:
                read1 += 1
            if flag & 0x80:
                read2 += 1
            if flag & 0x2:
                proper_pair += 1
            if not is_unmapped:
                if flag & 0x8:
                    singletons += 1
                else:
                    both_mates_mapped += 1
                    if read.next_reference_id != read.reference_id:
                        mate_different_reference += 1
                        if read.mapping_quality >= 5:
                            mate_different_reference_mapq5 += 1
        if (flag & 0x3) == 0x3 and flag & 0x40 and read.template_length > 0:
            insert_sizes[read.template_length] += 1
        sequence = read.query_sequence
        if sequence is not None:
            read_length = len(sequence)
            length_min = (
                read_length if length_min is None else min(length_min, read_length)
            )
            length_max = max(length_max, read_length)
            length_total += read_length
            length_n += 1
            if not is_unmapped:
                gc += sequence.count("G") + sequence.count("C")
                gc += sequence.count("g") + sequence.count("c")
                atgc += (
                    sequence.count("A")
                    + sequence.count("C")
                    + sequence.count("G")
                    + sequence.count("T")
                    + sequence.count("a")
                    + sequence.count("c")
                    + sequence.count("g")
                    + sequence.count("t")
                )
        if not is_unmapped and read.has_tag("NM"):
            nm_sum += int(read.get_tag("NM"))
            nm_present += 1
            aligned_query = read.query_alignment_length
            if aligned_query:
                nm_bases += aligned_query
        if flag & _COVERAGE_EXCLUDE_FLAG:
            continue
        blocks = read.get_blocks()
        coverage_bases += sum(end - start for start, end in blocks)
        tid = read.reference_id
        ref_length = lengths[tid]
        diff = diffs.get(tid)
        if diff is None and tid not in skipped_references:
            needed = ref_length + 1
            if (
                needed > COVERAGE_MAX_REFERENCE_BASES
                or diff_total + needed > COVERAGE_MAX_TOTAL_BASES
            ):
                skipped_references.add(tid)
            else:
                diff = array("i", [0]) * needed
                diffs[tid] = diff
                diff_total += needed
        if diff is not None:
            for start, end in blocks:
                diff[start] += 1
                diff[end] -= 1

    insert_count = sum(insert_sizes.values())
    if insert_count:
        insert_mean = (
            sum(size * count for size, count in insert_sizes.items()) / insert_count
        )
        insert_variance = (
            sum(
                count * (size - insert_mean) ** 2
                for size, count in insert_sizes.items()
            )
            / insert_count
        )
        insert_median = _median_from_histogram(insert_sizes, insert_count)
        insert_min = min(insert_sizes)
        insert_max = max(insert_sizes)
    else:
        insert_mean = insert_variance = insert_median = 0.0
        insert_min = insert_max = 0

    coverage_breadth_skipped = 1 if skipped_references else 0
    covered_bases: int | None = None
    depth_ge10_bases: int | None = None
    depth_ge30_bases: int | None = None
    if not skipped_references:
        covered_bases = 0
        depth_ge10_bases = 0
        depth_ge30_bases = 0
        for tid, diff in diffs.items():
            histogram = Counter(
                itertools.islice(itertools.accumulate(diff), lengths[tid])
            )
            ref_length = lengths[tid]
            covered_bases += ref_length - histogram.get(0, 0)
            depth_ge10_bases += sum(
                count for depth, count in histogram.items() if depth >= 10
            )
            depth_ge30_bases += sum(
                count for depth, count in histogram.items() if depth >= 30
            )

    return {
        "alignment_count": alignment_count,
        "qc_failed_count": qc_failed,
        "primary_count": primary,
        "secondary_count": secondary,
        "supplementary_count": supplementary,
        "mapped_count": mapped,
        "mapped_percent": pct(mapped, alignment_count),
        "primary_mapped_count": primary_mapped,
        "primary_mapped_percent": pct(primary_mapped, alignment_count),
        "unmapped_count": unmapped,
        "duplicate_count": duplicates,
        "duplicate_percent": pct(duplicates, alignment_count),
        "paired_count": paired,
        "read1_count": read1,
        "read2_count": read2,
        "proper_pair_count": proper_pair,
        "proper_pair_percent": pct(proper_pair, paired),
        "both_mates_mapped_count": both_mates_mapped,
        "singleton_count": singletons,
        "singleton_percent": pct(singletons, paired),
        "mate_different_reference_count": mate_different_reference,
        "mate_different_reference_mapq5_count": mate_different_reference_mapq5,
        "read_length_min": length_min or 0,
        "read_length_max": length_max,
        "read_length_mean": (length_total / length_n) if length_n else 0.0,
        "gc_percent": pct(gc, atgc),
        "mapq_ge30_count": mapq_ge30,
        "mapq_ge30_percent": pct(mapq_ge30, primary_mapped),
        "error_rate": (nm_sum / nm_bases) if nm_bases else None,
        "nm_tag_present_count": nm_present,
        "insert_size_count": insert_count,
        "insert_size_mean": insert_mean,
        "insert_size_std": sqrt(insert_variance),
        "insert_size_median": insert_median,
        "insert_size_min": insert_min,
        "insert_size_max": insert_max,
        "reference_count": reference_count,
        "references_with_mapped_count": len(references_with_mapped),
        "mean_depth": (
            coverage_bases / total_reference_bases if total_reference_bases else 0.0
        ),
        "covered_bases": covered_bases,
        "coverage_percent": pct(covered_bases, total_reference_bases)
        if covered_bases is not None
        else None,
        "depth_ge10_percent": pct(depth_ge10_bases, total_reference_bases)
        if depth_ge10_bases is not None
        else None,
        "depth_ge30_percent": pct(depth_ge30_bases, total_reference_bases)
        if depth_ge30_bases is not None
        else None,
        "coverage_breadth_skipped": coverage_breadth_skipped,
    }
