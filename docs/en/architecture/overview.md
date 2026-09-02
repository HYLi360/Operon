# Architecture overview

This document corresponds to `operon` 0.6.0, internal database schema 2.9, and metadata schema 1.4.

## Design goals

`operon` is a small, verifiable, traceable management system for genomic data. It follows these principles:

1. Structured metadata is the single source of truth.
2. Raw data is immutable; derived data is rebuildable.
3. File identity is determined by checksum and stable ID, not by path.
4. QC is written as explicit rules; measurement and decision are separated.
5. Download, standardization, QC, aggregation, and release are all executed by deterministic workflows.

How the principles map to implementations:

| Design principle | Implementation |
|---|---|
| Entities modeled separately; external accessions are never primary keys | `organisms/samples/runs/assemblies/annotations/files/accessions` tables |
| External sources, citations, and licenses traceable | `data_sources/source_links`; non-INSDC sources require citation + License |
| Fields have types, required flags, allowed values, and meanings | YAML schema + strict validation |
| Raw immutable, standardized derived | Atomic ingest + `ConflictError` + independent copies by default |
| Filenames contain only stable ID/role/format/compression | `canonical_filename()` |
| Paths are not file identity | `files.file_id + sha256 + size_bytes` |
| Layered QC | `file_integrity/reads_basic/assembly_basic/annotation_basic` |
| Measurement separated from decision | `qc_results` long table + YAML profile rule engine |
| Taxonomy coverage does not drift with upstream upgrades | NCBI taxonomy snapshot + compiled reference-set TSV + SHA-256 |
| Automated state machine, explicit failures, idempotent resume | `entity_state` + strict transitions + atomic operations |
| Machine-readable provenance | `logs/workflow.jsonl` + `workflow_runs` table |
| Auditable manual changes | `changes` table + `curate` command |
| Mistakenly imported entities can be reversibly retired | Append-only `RETIRE`/`RESTORE` events + hierarchical effective-state views; archives are never deleted |
| Versioned dataset publication | `release` + checksums + exclusions + provenance |
| Code/configuration/metadata/data separation | `operon/` code, `project.yaml`, `operon.sqlite`, `raw/` |

## Overall layering

```text
┌────────────────────────────────────────────────────────────┐
│ CLI control plane: operon commands (init/ingest/qc/...)    │
├────────────────────────────────────────────────────────────┤
│ Business layer                                              │
│  files.py       immutable file archiving, verification,     │
│                 standardization                             │
│  lifecycle.py   logical entity retirement, restoration,     │
│                 impact preview, and auditing                │
│  adapters/      external source resolution, download, field │
│                 mapping, and archiving orchestration        │
│  qc/            streaming FASTA/FASTQ/GFF3/protein parsing  │
│                 and metric computation                      │
│  rules.py       YAML profile rule engine and decisions      │
│  taxonomy.py    NCBI taxonomy snapshots and coverage        │
│                 denominator compilation                     │
│  coverage.py    metadata/release taxonomic coverage reports │
│  tools.py       external tool configuration, version        │
│                 probing, cached execution, result sync      │
│  release.py     release snapshot generation                 │
│  workflow.py    state machine, JSONL logs, external command │
│                 executor                                    │
│  execution.py   execution backend abstraction               │
│                 (local/slurm/ssh)                           │
│  shutdown.py    graceful SIGINT/SIGTERM shutdown and        │
│                 interruption cleanup                        │
│  remotes.py     SFTP remote mirrors (push/pull, remote      │
│                 manifest)                                   │
├────────────────────────────────────────────────────────────┤
│ Data layer                                                  │
│  schema.py      YAML field contracts and TSV                │
│                 validation/normalization                    │
│  database.py    SQLite DDL, migrations, transactions,       │
│                 read-only queries                           │
│  reports.py     long/wide table exports and human-readable  │
│                 reports                                     │
├────────────────────────────────────────────────────────────┤
│ Configuration layer                                         │
│  project.yaml   project paths and default parameters        │
│  config/schemas.yaml   metadata field definitions           │
│  config/tools.yaml     external analysis programs and       │
│                        recipe configuration                 │
│  config/profiles/*.yaml  versioned QC/coverage profiles     │
├────────────────────────────────────────────────────────────┤
│ Filesystem layer                                            │
│  metadata/ raw/ standardized/ qc/ analysis/ reports/ logs/ releases/ │
└────────────────────────────────────────────────────────────┘
```

## Module responsibilities

| Module | Main responsibilities |
|---|---|
| `operon/cli.py` | argparse command parsing, dispatch, human-readable output |
| `operon/config.py` | Reads `project.yaml`, locates the project root, generates the directory structure |
| `operon/schema.py` | Built-in metadata field definitions, type validation and normalization, derived TSV output |
| `operon/database.py` | SQLite DDL, WAL/foreign keys/indexes, development-time compatibility migrations and incremental schema 2.2–2.9 migrations, transactions, read-only queries |
| `operon/files.py` | File format/compression detection, atomic archiving, idempotent ingest, checksum verification, standardized views |
| `operon/lifecycle.py` | Retire/restore plans, append-only lifecycle events, hierarchical propagation, and the current retired list |
| `operon/import_wizard.py` | English questionary import wizard, draft summary review, non-linear section editing, preflight and commit |
| `operon/table_import.py` | CSV/XLSX templates, first-worksheet reading, collision preview, audited insert/patch |
| `operon/entity_view.py` | Internal ID/accession resolution and organism-rooted entity graph expansion |
| `operon/backup.py` | Consistent SQLite backup, control/results/full scopes, checksum manifest verification |
| `operon/adapters/ncbi_datasets.py` | NCBI Datasets JSON/JSONL/TSV/ZIP parsing, REST download, Entrez fallback, stable-ID deduplication, and automatic archiving |
| `operon/qc_module/parsers.py` | Pure-Python behavioral reference implementation, used to regression-test the Cython parsers' metrics and error semantics |
| `operon/qc_module/_parsers.pyx` | Cython production parsers required by built-in QC; metric output and error messages match the pure-Python reference bit for bit |
| `operon/qc_module/__init__.py` | Assembles built-in QC stages, loads the Cython parsers, and writes metrics into `qc_results` |
| `operon/rules.py` | Loads profiles, computes PASS/FAIL decisions, stores profile snapshots and decision history |
| `operon/taxonomy.py` | Archives/imports immutable NCBI Taxonomy, compiles frozen denominators and provenance per coverage profile |
| `operon/coverage.py` | Validates reference sets, computes family/genus coverage and missing lists against frozen metadata or release scopes |
| `operon/tools.py` | Reads `config/tools.yaml`; wraps external program launch, version probing, input validation, cached execution, and result write-back |
| `operon/workflow.py` | Legal state transitions, `workflow.jsonl` structured logs, external command execution |
| `operon/execution.py` | Execution backend abstraction: `local`/`slurm`/`ssh`; sbatch script generation and polling, SSH/SFTP transfer, path mapping |
| `operon/shutdown.py` | Converts SIGINT/SIGTERM into `ShutdownRequested`, drives per-backend process/job cleanup and second-signal forced exit |
| `operon/remotes.py` | SFTP remote mirrors: remote manifest maintenance, content-verified idempotent push/pull, `sftp://`/`remote://` downloads |
| `operon/release.py` | Generates immutable release directories and checksums |
| `operon/reports.py` | QC long/wide table export, derived metadata snapshots, status and decision reports |
| `operon/demo.py` | Generates a deterministic synthetic demo project |

## Project directory structure

`operon init` creates the following directories and files. The SQLite database is not created at init time, but on the first command that needs it.

```text
project/
├── project.yaml              # project config: paths, default QC profile, resource parameters
├── operon.sqlite           # file-based database (created on first command use)
├── config/
│   ├── schemas.yaml          # metadata field contract (types/required/allowed values/regex)
│   ├── tools.yaml            # external analysis programs (BLAST/HMMER/BUSCO, artifact types)
│   └── profiles/
│       ├── file_integrity_v1.yaml
│       ├── assembly_production_v1.yaml
│       ├── annotation_release_v1.yaml
│       ├── reads_qc_v1.yaml
│       └── coverage_viridiplantae_v1.yaml
├── metadata/                 # legacy layout compatibility note; no longer a read/write data source
├── raw/                      # immutable raw archive; NCBI source reports/packages under metadata/
├── standardized/             # processing view named by stable IDs (independent copies by default)
├── qc/                       # QC output and aggregate/ summary tables
├── analysis/                 # analysis workspace (external tool output, downstream analysis)
├── reports/                  # decisions, summary exports, and coverage reports
├── taxonomy/reference_sets/  # compiled immutable family/genus denominators and provenance
├── logs/workflow.jsonl       # machine-readable workflow log
├── .operon/placeholders/     # small, non-authoritative pointers for REMOTE_ONLY files
└── releases/                 # immutable dataset release snapshots
```

Data lifecycle:

```text
external sources
  └─> raw/           archived as-is, recorded in the files manifest with SHA-256
       └─> standardized/   verified, uniformly named derived copies/links
            └─> qc/        metrics only, written to qc_results
                 └─> evaluate   profile rules produce decisions
                      └─> release  only passing entities enter the release snapshot
```
