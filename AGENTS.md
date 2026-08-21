# AGENTS.md

Guidance for AI agents and contributors working in this repository.

## Project overview

Operon is a Python-based, file-backed database for large-scale genomic data:
archiving, metadata management, quality control (QC), rule-based decisions,
deterministic automation, and versioned dataset releases. The core design
invariants are:

1. Structured metadata is the single source of truth.
2. Raw data is immutable; derived data is rebuildable.
3. File identity is `file_id + sha256 + size_bytes`, never the path.
4. QC tools only measure metrics; decisions come from versioned YAML profiles.
5. All processing runs as an explicit, idempotent state machine with
   machine-readable provenance.

Keep these invariants intact when changing code. See `docs/architecture.md`
for the principle-to-implementation mapping.

## Repository layout

- `operon/` — the Python package (CLI entry point: `operon/cli.py`,
  `operon/__main__.py`).
  - `operon/adapters/` — external source adapters (currently NCBI Datasets).
  - `operon/qc_module/` — streaming FASTA/FASTQ/GFF3/protein parsers and
    built-in QC stages.
  - `operon/execution.py` — execution backends for external commands:
    `local` subprocess, `slurm` (sbatch submit + squeue poll), and `ssh`
    (Paramiko; HPC head nodes and cloud VMs, optionally through remote Slurm).
  - `operon/remotes.py` — SFTP remote storage mirrors (push/pull with
    checksum verification) and `sftp://` / `remote://` URL fetching.
- `tests/` — pytest suite organized as `unit/`, `integration/`,
  `regression/`, `compatibility/`, with shared fixtures in `tests/helpers.py`.
- `docs/` — full user and architecture documentation, written in Chinese.
- `build/release/` — cx_Freeze standalone application output (generated).

## Setup, test, and build

Always work inside the project virtual environment (`.venv/` exists in the
repo root; activate it or invoke `.venv/bin/python` explicitly).

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'   # runtime + pytest + cx_Freeze

python -m pytest                    # full suite
python -m pytest tests/unit         # by category

python -m cx_Freeze build           # standalone app -> build/release/
build/release/operon --version
```

Run the relevant test category after any change; run the full suite before
considering work done. Do not commit or perform other git mutations unless
the user explicitly asks.

## Conventions

- Python 3.10+; runtime dependencies are limited to `PyYAML`, `requests`,
  `aiohttp`, and `Biopython`. The optional `remote` extra adds `Paramiko`
  (lazy-imported only by the SSH/SFTP code paths; keep it optional). Do not
  add new dependencies without surfacing it to the user first, unless user
  has indicated any dependency can add.
- Documentation language: `docs/` and `README.md` are written in Chinese;
  code, comments, docstrings, and commit messages are in English.
- Naming in prose: headings use the stylized `Operon`; body text refers to
  the tool as `` `operon` `` (code-formatted).
- Never hard-code thresholds in QC code — they belong in versioned YAML
  profiles under `config/profiles/`.
- Never silently overwrite archived files: same entity + role with different
  bytes must raise `ConflictError`; identical bytes must be idempotent.
- Manual overrides (e.g. `curate`, forced `set-state`) must always be
  recorded in the `changes` audit table.
- `docs/database-compatibility.md` lists migration code that exists only for
  pre-1.0 databases and is scheduled for removal at the 1.0 release; check it
  before touching `operon/database.py` migrations or the NCBI adapter's
  schema-upgrade path.

## Documentation sync

When you change behavior, CLI surface, configuration fields, or storage
layout, update the corresponding doc in the same change:

- CLI commands/flags → `docs/cli-reference.md`
- Task-level workflows → `docs/howto.md`
- Architecture, data model, state machine, guarantees → `docs/architecture.md`
- `tools.yaml` recipes/placeholders/parsers → `docs/recipe-reference.md`
- Onboarding flows → `docs/getting-started.md`; navigation → `docs/index.md`

Version markers in docs (`operon` 0.3.0, database schema 2.2, metadata
schema 1.2) must match `pyproject.toml` and the code.
