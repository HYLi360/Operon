"""Helpers for creating and exporting the authoritative metadata TSV files."""

from __future__ import annotations

from pathlib import Path

from operon.schema import default_schemas
from operon.schema import write_tsv


def create_empty_metadata_files(metadata_dir: str | Path) -> None:
    """Create empty header-only TSVs for the manually curated metadata tables."""
    metadata_dir = Path(metadata_dir)
    metadata_dir.mkdir(parents=True, exist_ok=True)
    schemas = default_schemas()
    for table, spec in schemas["tables"].items():
        columns = list(spec["fields"].keys())
        write_tsv(metadata_dir / spec["file"], columns, [])
    (metadata_dir / "README.md").write_text(
        "# Metadata directory\n\n"
        "These TSV files are the human-editable metadata exchange format.\n"
        "The SQLite database (`operon.sqlite`) is the queryable file-based database.\n"
        "Use `operon import-metadata` to validate and load TSV files, or\n"
        "`operon export-metadata` to regenerate TSVs from the database.\n",
        encoding="utf-8",
    )
