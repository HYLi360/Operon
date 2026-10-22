#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Small pytest-native helpers used while keeping test bodies readable."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest


def copy_project_tree(source: str | Path, target: str | Path) -> Path:
    """Copy a project directory into a *writable* one and return the copy.

    ``shutil.copytree`` preserves file modes, and a project whose database was
    read while it was itself read-only carries side files with that read-only
    mode: SQLite gives ``operon.sqlite-shm``/``-wal`` the mode of the database
    it opened.  A copy inherits them and can no longer be written — opening it
    raises ``attempt to write a readonly database`` (ODR-21).  Dropping the
    shared-memory file is safe (SQLite rebuilds it from the log on the next
    open) and restoring write permission on the database, log and journal files
    makes the copy behave like the project it was copied from.
    """

    source_path = Path(source)
    target_path = Path(target)
    shutil.copytree(source_path, target_path)
    for shm_path in target_path.rglob("*.sqlite-shm"):
        shm_path.unlink()
    for sqlite_path in target_path.rglob("*.sqlite*"):
        if sqlite_path.is_file():
            sqlite_path.chmod(0o644)
    return target_path


class PytestAssertions:
    """Assertion adapter backed by plain assertions and :func:`pytest.raises`.

    The suite used class-based standard-library tests historically. Keeping this
    tiny adapter makes the migration reviewable without retaining that runner's
    lifecycle, discovery, or exception machinery.
    """

    def setup_method(self) -> None:
        self._cleanup_callbacks: list[
            tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]
        ] = []

    def teardown_method(self) -> None:
        while self._cleanup_callbacks:
            callback, args, kwargs = self._cleanup_callbacks.pop()
            callback(*args, **kwargs)

    def addCleanup(
        self, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> None:
        self._cleanup_callbacks.append((callback, args, kwargs))

    @staticmethod
    def assertEqual(left: Any, right: Any, message: Any = None) -> None:
        assert left == right, message

    @staticmethod
    def assertNotEqual(left: Any, right: Any, message: Any = None) -> None:
        assert left != right, message

    @staticmethod
    def assertTrue(value: Any, message: Any = None) -> None:
        assert value, message

    @staticmethod
    def assertFalse(value: Any, message: Any = None) -> None:
        assert not value, message

    @staticmethod
    def assertIn(member: Any, container: Any, message: Any = None) -> None:
        assert member in container, message

    @staticmethod
    def assertNotIn(member: Any, container: Any, message: Any = None) -> None:
        assert member not in container, message

    @staticmethod
    def assertIsNone(value: Any, message: Any = None) -> None:
        assert value is None, message

    @staticmethod
    def assertIsNotNone(value: Any, message: Any = None) -> None:
        assert value is not None, message

    @staticmethod
    def assertGreater(left: Any, right: Any, message: Any = None) -> None:
        assert left > right, message

    @staticmethod
    def assertGreaterEqual(left: Any, right: Any, message: Any = None) -> None:
        assert left >= right, message

    @staticmethod
    def assertLessEqual(left: Any, right: Any, message: Any = None) -> None:
        assert left <= right, message

    @staticmethod
    def assertAlmostEqual(
        left: float, right: float, places: int = 7, message: Any = None
    ) -> None:
        assert round(abs(left - right), places) == 0, message

    @staticmethod
    def assertRaises(exception: type[BaseException]) -> AbstractContextManager[Any]:
        return pytest.raises(exception)

    @staticmethod
    def assertRaisesRegex(
        exception: type[BaseException], pattern: str
    ) -> AbstractContextManager[Any]:
        return pytest.raises(exception, match=pattern)


def _bam_segment(
    qname: str,
    flag: int,
    rname: int,
    pos: int,
    mapq: int,
    cigar: str | None,
    seq: str,
    tlen: int = 0,
    mate: int = -1,
    mpos: int = -1,
    tags: tuple[tuple[str, int], ...] = (),
):
    import pysam

    segment = pysam.AlignedSegment()
    segment.query_name = qname
    segment.flag = flag
    segment.reference_id = rname
    segment.reference_start = pos
    segment.mapping_quality = mapq
    segment.cigarstring = cigar
    segment.query_sequence = seq
    segment.query_qualities = pysam.qualitystring_to_array("I" * len(seq))
    segment.template_length = tlen
    segment.next_reference_id = mate
    segment.next_reference_start = mpos
    for key, value in tags:
        segment.set_tag(key, value)
    return segment


def write_test_bam(path: str | Path) -> Path:
    """Write the deterministic 12-read BAM used by the alignment-QC tests.

    References: chr1 (1000 bp), chr2 (500 bp), chr3 (200 bp, no reads).
    Expected measurements (verified by hand against flagstat conventions):
    12 alignments (1 QC-failed, 1 secondary, 1 supplementary), 10 mapped,
    8 primary-mapped, 2 unmapped, 1 duplicate; 7 paired (4 read1, 3 read2),
    4 proper pairs, 1 singleton, 2 mates on a different reference (1 with
    MAPQ>=5); 5 primary-mapped reads at MAPQ>=30; insert sizes {300, 100};
    error rate 7/450 from NM tags (r7 carries none); coverage over 440 of
    1700 reference bases (450 aligned bases).
    """
    import pysam

    path = Path(path)
    header = {
        "HD": {"VN": "1.6", "SO": "coordinate"},
        "SQ": [
            {"SN": "chr1", "LN": 1000},
            {"SN": "chr2", "LN": 500},
            {"SN": "chr3", "LN": 200},
        ],
    }
    reads = [
        _bam_segment(
            "r1",
            0x63,
            0,
            100,
            60,
            "50M",
            "A" * 50,
            tlen=300,
            mate=0,
            mpos=350,
            tags=(("NM", 0),),
        ),
        _bam_segment(
            "r2",
            0xA3,
            0,
            350,
            60,
            "50M",
            "C" * 50,
            tlen=-300,
            mate=0,
            mpos=100,
            tags=(("NM", 2),),
        ),
        _bam_segment("r3", 0x4, -1, -1, 0, None, "G" * 50),
        _bam_segment("r4", 0x100, 0, 200, 20, "50M", "T" * 50, tags=(("NM", 1),)),
        _bam_segment("r5", 0x800, 1, 10, 60, "50M", "A" * 50, tags=(("NM", 0),)),
        _bam_segment("r6", 0x400, 0, 150, 10, "50M", "N" * 50, tags=(("NM", 3),)),
        _bam_segment("r7", 0x49, 0, 500, 40, "50M", "ACGT" * 12 + "AC", mate=0, mpos=0),
        _bam_segment("r8", 0x204, -1, -1, 0, None, "T" * 40),
        _bam_segment(
            "r9", 0x41, 0, 600, 20, "50M", "G" * 50, mate=1, mpos=50, tags=(("NM", 0),)
        ),
        _bam_segment(
            "r10", 0x81, 1, 50, 2, "50M", "A" * 50, mate=0, mpos=600, tags=(("NM", 1),)
        ),
        _bam_segment(
            "r11",
            0x63,
            1,
            100,
            60,
            "50M",
            "C" * 50,
            tlen=100,
            mate=1,
            mpos=150,
            tags=(("NM", 0),),
        ),
        _bam_segment(
            "r12",
            0xA3,
            1,
            150,
            60,
            "50M",
            "T" * 50,
            tlen=-100,
            mate=1,
            mpos=100,
            tags=(("NM", 0),),
        ),
    ]
    with pysam.AlignmentFile(str(path), "wb", header=header) as out:
        for read in reads:
            out.write(read)
    return path
