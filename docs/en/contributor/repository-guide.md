# Repository collaboration guide

## Project principles

The five core design invariants are stated once in the [Architecture overview](../architecture/overview.md#design-goals); keep them intact when changing code.

## Repository structure

- `operon/`: the Python package; CLI entry points are `operon/cli.py` and `operon/__main__.py`.
- `operon/adapters/`: external source adapters, currently NCBI Datasets.
- `operon/qc/`: home for all QC functionality — streaming sequence-file parsers, built-in QC, and alignment QC (`alignment.py`). `parsers.py` is the pure-Python behavioral reference; `_parsers.pyx` is the Cython implementation used in production. The two must keep metrics and error texts identical through parity regression tests.
- `operon/execution.py`: the `local`, `slurm`, and `ssh` execution backends.
- `operon/remotes.py`: SFTP mirrors, push/pull, and remote URL downloads.
- `tests/`: `unit/`, `integration/`, `regression/`, `compatibility/` tests.
- `docs/`: user, architecture, and operations documentation.

## Collaboration conventions

- `pyproject.toml` is the single source of truth for dependencies. Adding a runtime dependency requires explicit approval and must go into the narrowest possible optional extra.
- Code, comments, docstrings, and commit messages are written in English; user documentation is maintained in both Chinese and English.
- Headings use `Operon`; in body text the command-line tool is written as `` `operon` ``.
- Thresholds must never be hard-coded in QC code.
- Archived files must never be silently overwritten; identical bytes are idempotent, and different bytes for the same entity and role must raise `ConflictError`.
- Manual modifications such as `curate` and forced `set-state` must be written to `changes`.
- Before changing database migrations or the NCBI adapter schema-upgrade path, read the [database compatibility code inventory](../operations/database-compatibility.md) first.
- The project license is AGPL-3.0-or-later.
