# Operon

[![deploy status](https://github.com/HYLi360/Operon/actions/workflows/deploy.yml/badge.svg)](https://github.com/HYLi360/Operon/actions/workflows/tests.yml)

A Python-based, **file-based database** designed for large-scale genomic data, used for archiving, quality control, analysis, and deterministic automation.

[点此阅读中文自述文件。](README_ZH.md)

## Features

- **File-based**: A single SQLite file (`operon.sqlite`) serves as the sole writable source of truth; CSV/XLSX are used for controlled imports, TSV reports are used for read-only exchange, and field contracts are defined by a YAML schema
- **NCBI Datasets Adapter**: Offline-first import of JSON/JSONL, ZIP, or unpacked directories; also supports online download of genome packages with automatic archiving
- **Frozen NCBI Taxonomy Coverage**: Versioned YAML profiles are compiled into family/genus denominators with SHA-256 hashes, allowing separate auditing of current metadata and immutable releases, and generating a list of missing samples
- **Streaming Parsing and Built-in QC**: FASTA, FASTQ, GFF3, and protein FASTA files are not loaded entirely into memory; metrics are written to a long table, and decisions are delegated to the versioned YAML profile rule engine; `value_by` allows thresholds to be selected based on classification metrics such as BUSCO auto-lineage
- **Encapsulated External Analysis**: `config/tools.yaml` specifies the launch methods for BLAST/HMMER/BUSCO, artifact types, constrained runtime parameters, version detection, caching, and result write-back; `analyze` executes the entire library or specified categories with a single command
- **Local Control, Remote Storage and Computing**: SQLite, configuration, and provenance are retained locally, while raw large files can reside on a verified SFTP mirror; the execution backend supports local, Slurm, SSH, and remote Slurm environments
- **Universal Executor**: The structured command executor and `import-qc` can integrate with any external tools, such as QUAST, FastQC, fastp, and CheckM2
- **Immutable release**: Dataset snapshots with manifests, checksums, exclusion reports, and provenance; verifiable via `sha256sum -c`

## Dependencies

- Python 3.10+
- Runtime dependencies: `PyYAML`, `requests`, `aiohttp`, `Biopython`, `Cython` (built with the built-in QC acceleration extension enabled by default)
- Optional extras: `test` (pytest), `remote` (Paramiko), `build` (cx_Freeze and remote features), `dev` (all development/build dependencies)

## Install

```bash
# Create & activate standard Python venv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

# execute it if you need restore & compute on remote via SSH/SFTP
python -m pip install -e '.[remote]'
```

Or, build a standalone executable application by a build script:

```bash
python -m pip install -e '.[build]'
python tools/build.py
```

## Documentation

The complete documentation is maintained in [English](docs/en/index.md) and [Chinese](docs/zh/index.md). To build the Sphinx site locally:

```bash
python -m pip install -e '.[docs]'
sphinx-build -W --keep-going -b html docs docs/_build/html
```

Read the Docs uses the repository's `.readthedocs.yaml` configuration and publishes a language-selection page with mirrored `/en/` and `/zh/` documentation trees.
