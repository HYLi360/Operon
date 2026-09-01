"""Compatibility notice for the former live metadata TSV directory."""

from __future__ import annotations

from pathlib import Path


def initialize_metadata_directory(metadata_dir: str | Path) -> None:
    """Create a notice; SQLite is the only writable metadata source in 0.4+."""
    metadata_dir = Path(metadata_dir)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    (metadata_dir / "README.md").write_text(
        "# Metadata directory\n\n"
        "Since Operon 0.4, `operon.sqlite` is the sole writable metadata source.\n"
        "This directory is retained only so existing project layouts remain valid.\n"
        "Use `operon import table` for controlled CSV/XLSX loading and\n"
        "`operon report metadata` for derived, read-only TSV snapshots.\n",
        encoding="utf-8",
    )
