"""Stateless storage guards shared by the NCBI Datasets adapter submodules.

Disk-space preflights and the ENOSPC translation they raise are needed by
acquisition (`_ncbi_sources`), download (`_ncbi_download`) and planning
(`_ncbi_plan`) alike, so they live here rather than in any one of them.  The
functions depend on nothing but `shutil` and the `Path` they are handed,
which keeps the dependency direction one-way: model -> storage -> the rest.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from operon.errors import ValidationError


def _require_disk_space(path: Path, required_bytes: int, action: str) -> None:
    """Fail before a large write when the target filesystem is clearly full."""

    path = Path(path)
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    free = shutil.disk_usage(existing).free
    # Keep a small reserve for SQLite, metadata exports and filesystem
    # bookkeeping.  This is intentionally fixed rather than proportional so
    # multi-gigabyte genomes do not receive an excessive safety multiplier.
    reserve = 64 * 1024 * 1024
    needed = max(0, int(required_bytes)) + reserve
    if free < needed:
        raise ValidationError(
            f"insufficient space to {action} on filesystem containing {existing}: "
            f"need about {_format_bytes(needed)}, only {_format_bytes(free)} available. "
            "Free space, reduce --batch-size/--include content, or use "
            "--no-preserve-source when the original package is already archived elsewhere."
        )


def _no_space_error(path: Path, action: str, exc: OSError) -> ValidationError:
    existing = Path(path)
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    try:
        free = shutil.disk_usage(existing).free
        available = f" ({_format_bytes(free)} currently available)"
    except OSError:
        available = ""
    return ValidationError(
        f"filesystem ran out of space while attempting to {action} at {existing}{available}. "
        "The NCBI adapter processes one batch at a time; free space, reduce --batch-size or "
        "download fewer --include file types, then rerun (completed batches are idempotent)."
    )


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TiB"  # pragma: no cover
