# Project Overview

`operon` organizes genomic files, metadata, QC metrics, analysis results, and release records as an auditable project. A single SQLite database stores structured state; the project directory stores raw files, derived files, analysis outputs, reports, logs, and releases.

## Capabilities

- **Metadata and source management**: models organisms, samples, runs, assemblies, and annotations under a YAML schema; separates external accessions from internal stable IDs; tracks sources, citations, and licenses.
- **Immutable file archiving**: `ingest` calculates SHA-256 checksums, writes raw files atomically, and rejects different bytes for the same entity and role.
- **NCBI Datasets adapter**: imports offline reports and packages or downloads accessions online, creates metadata relationships, and archives package files.
- **Streaming built-in QC**: parses FASTA, FASTQ, GFF3, and protein FASTA without loading whole files into memory; writes metrics to a shared long table.
- **Rule engine and manual curation**: YAML profiles produce append-only decisions; manual overrides are written to the audit table.
- **Encapsulated external analysis**: runs BLAST, HMMER, BUSCO, or other commands from `config/tools.yaml`, recording tool version, input identity, database identity, and output checksums.
- **Remote storage and execution**: supports SFTP mirrors, `REMOTE_ONLY` files, and local, Slurm, and SSH execution backends.
- **Taxonomy coverage**: imports an explicit NCBI Taxonomy version, compiles a frozen denominator, and audits either current metadata or a frozen release.
- **Verifiable releases**: a release contains member manifests, exclusion reports, metadata snapshots, provenance, and `checksums.sha256`.

## Scope and boundaries

Operon manages data admission, identity verification, provenance, rule evaluation, and publication. It does not replace downstream comparative-genomics workflows. Those workflows can run under `analysis/` and write metrics back to Operon.

The current source adapter supports NCBI Datasets. Taxonomy coverage currently supports NCBI Taxonomy only; GTDB and NCBI↔GTDB crosswalks are extension work.

## Recommended workflow

```text
metadata import/add
  -> ingest
  -> verify
  -> standardize
  -> qc / analyze
  -> evaluate / curate
  -> release
```

See the [Daily Workflow](getting-started/daily-workflow.md) for the operational sequence.
