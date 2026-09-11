# AGENTS.md

Guidance for AI agents and contributors working in this repository.

## Project overview

Operon is a Python-based, file-backed database for large-scale genomic data:
archiving, metadata management, quality control (QC), rule-based decisions,
deterministic automation, taxonomy coverage auditing, and versioned dataset
releases. A single SQLite file (`operon.sqlite`) is the sole writable source
of truth inside each managed project; large sequence files never enter the
database, only their manifest records, QC metrics, and provenance do.

The core design invariants are:

1. Structured metadata is the single source of truth.
2. Raw data is immutable; derived data is rebuildable.
3. File identity is `file_id + sha256 + size_bytes`, never the path.
4. QC tools only measure metrics; decisions come from versioned YAML profiles.
5. All processing runs as an explicit, idempotent state machine with
   machine-readable provenance.

Keep these invariants intact when changing code. See
`docs/en/architecture/` (or the mirrored `docs/zh/architecture/`) for the
principle-to-implementation mapping.

Current version markers (must stay consistent across code and docs):

- `operon` 0.7.0 (`pyproject.toml`)
- database schema 2.10 (`operon/database.py`, `SCHEMA_VERSION`)
- metadata schema 1.4 (`operon/schema.py`, `METADATA_SCHEMA_VERSION`)

The project is licensed AGPL-3.0-or-later (`LICENSE` at the repo root).

## Repository layout

- `operon/` — the Python package (CLI entry points: `operon/cli.py`,
  `operon/__main__.py`; console script `operon = operon.cli:main`).
  - `operon/adapters/` — external source adapters: NCBI Datasets
    (offline-first: JSON/JSONL, ZIP, or unpacked directories, plus optional
    online download) and TimeTree (query-cache-only REST client caching the
    verbatim responses of exact queries under `adapters_cache/timetree/`;
    mirroring or redistribution is forbidden by TimeTree's terms).
  - `operon/qc/` — home for all QC functionality: streaming
    FASTA/FASTQ/GFF3/protein parsers and built-in QC stages, plus alignment
    QC (`alignment.py`, the pure-Python multiple-alignment QC reference
    implementation behind `operon alignment-qc`; a Cython build
    `_alignment.pyx` is coming). `parsers.py` is the pure-Python reference
    implementation; `_parsers.pyx` is the Cython-accelerated build of the
    same API (compiled in place as `operon.qc._parsers`). The Cython
    module is the required production backend; the pure-Python module is the
    behavioral reference used by regression tests. Both must produce
    identical metrics and error messages (enforced by
    `tests/regression/test_cython_parser_parity.py`). `measure.py` backs
    `operon qc-measure`, a project-independent measurement-only path whose
    JSON payload can be imported back through `operon import-qc` (the remote
    built-in QC workflow).
  - `operon/execution.py` — execution backends for external commands:
    `local` subprocess, `slurm` (sbatch submit + squeue poll), and `ssh`
    (Paramiko; HPC head nodes and cloud VMs, optionally through remote
    Slurm). All backends share one provenance contract.
  - `operon/remotes.py` — SFTP remote storage mirrors (push/pull with
    checksum verification) and `sftp://` / `remote://` URL fetching.
  - `operon/tui/` — Textual-based terminal UI (`operon tui`, optional `tui`
    extra): Home dashboard, Entities browser, Files browser, workflow-run
    monitor, a Decisions screen, a Config screen, a Publish screen (nav key
    `7`; release builder + selective export builder with read-only previews),
    a Coverage screen (nav key `8`; taxonomy snapshots, reference sets,
    coverage report generation and `COV_*` report browsing), and the import
    dataset wizard (`operon/tui/screens/import_wizard.py`; Home button or
    global `i`, except on the Files screen where `i` stays ingest). Read
    access lives in
    `operon/tui/data.py` and is strictly read-only (short-lived read-only
    connections only). Phase 2 write operations (evaluate, curate,
    retire/restore, ingest, verify, QC batch) live in
    `operon/tui/actions.py`: each function opens its own short-lived
    *writable* `Database`, calls the same core functions as the CLI
    (identical `changes`/`workflow_runs` provenance), and returns plain
    dicts; writable connections are never held by the UI. Every write in the
    UI follows form/plan preview → equivalent CLI command shown → explicit
    Confirm → background worker → notify + reload or inline error. Phase 3
    actions in the same module: `import_dataset` (commits wizard drafts
    through the shared single-transaction `import_wizard._commit`),
    `reserve_entity_ids`, `create_release`, `export`, and `run_coverage`
    (a below-threshold coverage report returns `exit_code=1` in the result
    dict — a warning, not an exception). The
    Config screen (`operon/tui/screens/config.py`, nav key `6`) edits
    `config/profiles/*.yaml` (kind `qc`) and single recipes inside
    `config/tools.yaml` through structured control-based forms (no free-text
    YAML): every save bumps the `version`, records the same content-addressed
    snapshot the CLI records (`qc_profiles` / `recipe_snapshots`), and
    restores the previous file bytes on failure; keys the forms do not model
    are preserved verbatim; history modals restore snapshots into the editor
    as the next version; tools-check runs in a worker with per-row updates.
    Textual is imported only inside this package, which the `tui` command
    handler imports lazily.
  - Other top-level modules by responsibility: `database.py` (SQLite schema
    and migrations), `schema.py` (YAML metadata schema and validation),
    `config.py` (project configuration and directory layout), `files.py`
    (immutable manifest archival and verification), `profiles.py` +
    `rules.py` (versioned QC profiles and the decision engine),
    `workflow.py` (state machine and run logs), `tools.py` (external-tool
    recipes from `config/tools.yaml`), `taxonomy.py` + `coverage.py` (frozen
    NCBI Taxonomy snapshots and coverage denominators), `release.py` +
    `export.py` (immutable releases and selective exports), `lifecycle.py`
    (audited reversible entity retirement), `lineage.py` (adopting external
    workflow outputs), `sequence_tools.py` (alignment-driven domain
    extraction and sequence selection), `timetree.py` (TimeTree query/calibration CLI
    group backed by the query-cache adapter), `backup.py`, `reports.py`,
    `table_import.py`,
    `import_wizard.py`, `entity_view.py`, `environment.py`
    (execution-environment capture), `shutdown.py` (graceful SIGINT/SIGTERM
    handling), `ncbi_reconcile.py` (development-era adapter anomaly repair),
    `demo.py` (deterministic synthetic demo project), `errors.py`,
    `utils.py`.
- `tests/` — pytest suite organized as `unit/`, `integration/`,
  `regression/`, `compatibility/`, with shared fixtures in
  `tests/helpers.py`.
- `docs/` — Sphinx documentation in two mirrored language trees, `docs/en/`
  and `docs/zh/`, each split into `overview.md`, `getting-started/`,
  `guides/`, `architecture/`, `reference/`, `operations/`, and
  `contributor/`. Built with `docs/conf.py`; published via Read the Docs
  (`.readthedocs.yaml`).
- `benchmarks/` — representative entity sets for QC performance diagnostics
  (see `docs/*/operations/qc-performance.md`).

## Setup, test, and build

Always work inside the project virtual environment (`.venv/` exists in the
repo root; activate it or invoke `.venv/bin/python` explicitly).

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'   # runtime + pytest + Cython + Sphinx;
                                    # also compiles the qc parsers extension

python -m pytest                    # full suite (coverage gate: 90% branch)
python -m pytest tests/unit         # by category: unit / integration /
                                    # regression / compatibility

python setup.py build_ext --inplace # rebuild only the Cython extension

sphinx-build -W --keep-going -b html docs docs/_build/html  # strict docs build
```

Run the relevant test category after any change; run the full suite before
considering work done. CI (`.github/workflows/deploy.yml`) runs pytest on
Python 3.10–3.14 and the strict Sphinx build. Releases are published
exclusively to PyPI; see `docs/*/contributor/pypi-release.md`.

Do not commit or perform other git mutations unless the user explicitly
asks.

## Conventions

- Python 3.10+. Treat `pyproject.toml` as the authoritative dependency
  list: `[project.dependencies]` contains core runtime dependencies, while
  `[project.optional-dependencies]` contains separately installable extras
  (`test`, `docs`, `dev`). Runtime-feature extras must
  remain lazy-imported by their feature paths (e.g. Paramiko is only
  imported inside remote/SSH code); test/build extras must stay out of
  normal runtime paths. Do not promote an extra dependency to core, or add a
  new core runtime dependency, unless the user explicitly authorizes that
  dependency. Approval for one dependency does not authorize others unless
  the user grants a broader allowance. Merely informing the user is not
  authorization; new optional dependencies must still be surfaced and kept
  in the narrowest appropriate extra.
- Documentation language: `docs/` is maintained in parallel English
  (`docs/en/`) and Chinese (`docs/zh/`) trees — keep both in sync;
  `README.md` is English and `README_ZH.md` is Chinese. Code, comments,
  docstrings, and commit messages are in English.
- Naming in prose: headings use the stylized `Operon`; body text refers to
  the tool as `` `operon` `` (code-formatted).
- Never hard-code thresholds in QC code — they belong in versioned YAML
  profiles (under `config/profiles/` inside each managed project).
- Never silently overwrite archived files: same entity + role with different
  bytes must raise `ConflictError`; identical bytes must be idempotent.
- Manual overrides (e.g. `curate`, forced `set-state`) must always be
  recorded in the `changes` audit table.
- `docs/*/operations/database-compatibility.md` lists migration code that
  exists only for pre-1.0 databases and is scheduled for removal at the 1.0
  release; check it before touching `operon/database.py` migrations or the
  NCBI adapter's schema-upgrade path.

## Documentation sync

When you change behavior, CLI surface, configuration fields, or storage
layout, update both language trees (`docs/en/` and `docs/zh/`) in the same
change:

- CLI commands/flags → `docs/*/reference/cli-*.md`
- Task-level workflows → `docs/*/guides/` and `docs/*/getting-started/`
- Architecture, data model, state machine, guarantees → `docs/*/architecture/`
- `tools.yaml` recipes/placeholders/parsers → `docs/*/reference/recipe-*.md`
- Migrations, performance diagnostics, compatibility boundaries →
  `docs/*/operations/`
- Contributor-facing processes → `docs/*/contributor/`; navigation →
  `docs/*/index.md`

Version markers in docs (`operon` 0.7.0, database schema 2.10, metadata
schema 1.4) must match `pyproject.toml` and the code. Do not write the
current values literally in Markdown sources: use the `myst_substitutions`
references `{{ operon_version }}`, `{{ db_schema }}`, and
`{{ metadata_schema }}`, which `docs/conf.py` resolves from the single
sources above at build time. Substitutions expand in paragraph text only,
never inside code spans or fenced code blocks — examples there use
`<version>` placeholders instead. Intentional historical pins stay literal — either on the
allowlisted era-pinned pages (`docs/*/operations/database-compatibility.md`,
`docs/*/operations/ncbi-recovery-migration.md`) or on a line carrying an
inline `<!-- version-pin -->` marker — and
`tests/unit/test_docs_versions.py` fails on any other hardcoded current
version. Only this `AGENTS.md` keeps literal current markers (it is not
Sphinx-rendered); update the list above when bumping.

## Special Note For Codex/ChatGPT

Due to specific limitations of the sandbox environment, executing certain TUI test code may cause the system to freeze; this often occurs on Codex/ChatGPT. The specific reason is that when this test code runs in the sandbox, it may results a Textual/asyncio cleanup block, and reports "FAIL" due to timeout.

If you are Codex/ChatGPT, please execute TUI-related test code OUTSIDE the sandbox.
