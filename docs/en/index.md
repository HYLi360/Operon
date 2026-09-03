# Operon Documentation

Operon is a file-backed database for large-scale genomic data. It supports metadata management, immutable file archiving, quality control (QC), rule-based decisions, external analysis, remote storage and execution, and versioned dataset releases.

This documentation matches `operon` 0.6.1, database schema 2.9, and metadata schema 1.4. The Chinese and English documentation use the same directory structure.

## Reading paths

1. New users: read the [Project Overview](overview.md), then complete [Installation](getting-started/installation.md) and the [Quick Start](getting-started/quickstart.md).
2. Routine users: open the [How-To Guides](guides/index.md) and select a task.
3. Configuration and command lookup: use the [Command and Configuration Reference](reference/index.md).
4. Maintainers: read the [Architecture](architecture/index.md), [Operations](operations/index.md), and [Contributor Guide](contributor/index.md).

## Documentation map

- [Project Overview](overview.md)
- [Getting Started](getting-started/index.md)
- [How-To Guides](guides/index.md)
- [Command and Configuration Reference](reference/index.md)
- [Architecture](architecture/index.md)
- [Operations](operations/index.md)
- [Contributor Guide](contributor/index.md)

```{toctree}
:hidden:
:maxdepth: 2

overview
getting-started/index
guides/index
reference/index
architecture/index
operations/index
contributor/index
```

## Core concepts

- Structured metadata in SQLite is the only writable source of truth.
- Raw files are immutable; derived data must be rebuildable.
- File identity is `file_id + sha256 + size_bytes`; a path only records the current location.
- QC tools produce metrics. Acceptance decisions are produced by versioned YAML profiles.
- Processing is recorded in the entity state machine, SQLite, and JSONL provenance logs.
- A release is an immutable snapshot containing a manifest, checksums, exclusions, metadata snapshots, and provenance.
