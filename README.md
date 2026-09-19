# Operon the Database System

[![test status](https://github.com/HYLi360/Operon/actions/workflows/test.yml/badge.svg)](https://github.com/HYLi360/Operon/actions/workflows/test.yml)
[![RtD badge](https://img.shields.io/badge/Read-the_Document-blue?style=flat)](https://operonproject.readthedocs.io/)
![PyPI Version](https://img.shields.io/pypi/v/OperonDBS?link=https%3A%2F%2Fpypi.org%2Fproject%2FOperonDBS%2F)
![GitHub commit activity](https://img.shields.io/github/commit-activity/t/HYLi360/Operon?logo=github)
[![Codacy Code quality Badge](https://app.codacy.com/project/badge/Grade/47d652651ff645ff86fa0646768a08b3)](https://app.codacy.com/gh/HYLi360/Operon/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)
[![Codacy Coverage Badge](https://app.codacy.com/project/badge/Coverage/47d652651ff645ff86fa0646768a08b3)](https://app.codacy.com/gh/HYLi360/Operon/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_coverage)

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
- Runtime dependencies: `PyYAML`, `requests`, `aiohttp`, `Biopython`, `Paramiko`, `Textual`, and `questionary`; the built-in QC acceleration extension is compiled when the package is built
- Optional extras: `test` (pytest and Cython), `docs` (documentation tooling), and `dev` (all development/build dependencies)

## Install

```bash
# Install the published package
python3 -m venv .venv
source .venv/bin/activate
python -m pip install OperonDBS
```

For an editable checkout, run from the repository root:

```bash
python -m pip install -e '.[dev]'
```

## Documentation

The complete documentation is maintained in [English](docs/en/index.md) and [Chinese](docs/zh/index.md). Each language is its own Sphinx project; build both without warnings:

```bash
python -m pip install -e '.[docs]'
sphinx-build -W --keep-going -b html docs/en docs/_build/en/html
sphinx-build -W --keep-going -b html docs/zh docs/_build/zh/html
```

Read the Docs publishes two projects linked as parent and translation: `operonproject` (English, `https://operonproject.readthedocs.io/en/latest/`) and `operonproject-zh` (Chinese, `https://operonproject.readthedocs.io/zh-cn/latest/`), each built from its own `docs/<language>/.readthedocs.yaml`.
