# Operon Documentation

The [Project Overview](overview.md) introduces Operon, a file-backed database for large-scale genomic data, and lists its capabilities and boundaries.

This documentation matches `operon` {{ operon_version }}, database schema {{ db_schema }}, and metadata schema {{ metadata_schema }}. The Chinese and English documentation use the same directory structure.

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

The design invariants are stated once in the [Architecture overview](architecture/overview.md#design-goals).
