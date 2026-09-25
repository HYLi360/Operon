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

- `operon` 0.9.0 (`pyproject.toml`)
- database schema 2.11 (`operon/database.py`, `SCHEMA_VERSION`)
- metadata schema 1.4 (`operon/schema.py`, `METADATA_SCHEMA_VERSION`)

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
    implementation behind `operon alignment-qc`, and `_alignment.pyx`, its
    Cython production backend; parity enforced by
    `tests/regression/test_cython_alignment_parity.py`). `parsers.py` is the
    pure-Python reference
    implementation; `_parsers.pyx` is the Cython-accelerated build of the
    same API (compiled in place as `operon.qc._parsers`). The Cython
    module is the required production backend; the pure-Python module is the
    behavioral reference used by regression tests. Both must produce
    identical metrics and error messages (enforced by
    `tests/regression/test_cython_parser_parity.py`). `measure.py` backs
    `operon qc-measure`, a project-independent measurement-only path whose
    JSON payload can be imported back through `operon import-qc` (the remote
    built-in QC workflow; `imports.py` is the shared core of that import —
    payload/TSV detection, manifest validation, metric insertion, QC-state
    recomputation and the run record — and `plan_qc_import` its write-free
    preview).
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
    coverage report generation and `COV_*` report browsing, plus *Import
    taxonomy…* / *Compile reference set…* write entries mirroring
    `operon taxonomy import`/`operon taxonomy compile`), and the import
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
    `ncbi_datasets` (backing the Home screen's NCBI Datasets import dialog:
    a mandatory dry-run preflight — `dry_run` for offline inputs,
    `plan_only` for accession-only requests — gates Confirm, and Cancel sets
    a cooperative `cancel_event` the core records as an `interrupted`,
    `--resume-run`-able run),
    `reserve_entity_ids`, `create_release`, `export`, `run_coverage`
    (a below-threshold coverage report returns `exit_code=1` in the result
    dict — a warning, not an exception), `preflight_backend`, `run_analysis`
    (backing the AnalyzeModal launched from the Config screen's Run analysis
    button or the Tasks screen's New analysis button; the modal mirrors
    `--backend` — project default / local / slurm / ssh — and preflights the
    selection inline, and cancellation sets the core's cooperative
    `cancel_event`, so a queued Slurm job or array is cancelled with one
    `scancel`), `run_external` (backing the Tasks screen's Run external
    dialog, which mirrors `run-external` field by field and opens the run
    record when it finishes) and `write_analysis_report` (backing the
    Analysis hits browser's Export button: it runs the same read-only query
    and the same renderer as `report analysis --hits --format … --out …`, so
    the file is byte-identical and, like that command, writes no provenance
    rows). Phase 3 also covers the
    derived-artifact loop: `run_classify` (the Config screen's Run classify
    dialog, mirroring `classify-sequences` with the CLI's summary and a re-run
    button), and — on the Files screen — `extract_domains`, `select_sequences`,
    `adopt` and `fanout` (modals in `operon/tui/screens/derived_ops.py`:
    extract/select chain into the adopt dialog with `derived_from` prefilled and
    leave their output unregistered, adopt's manifest mode previews before
    Confirm, and fanout's dry run is a mandatory preflight). Read-only TUI views added for `report analysis --hits` (alignment
    hits with the CLI's filters and columns) and for `sequence_labels` (the
    classify-sequences output: a per-file section in the Files detail plus
    the project-wide label summary behind the Files screen's `l` binding), which
    has no CLI reader at all. The
    Config screen (`operon/tui/screens/config.py`, nav key `6`) edits
    `config/profiles/*.yaml` (kinds `qc`, `sequence_classification` and
    `taxonomy_coverage` — each kind has its own form, dispatched by the document's
    own kind, with the classification widgets in `operon/tui/screens/config_classification.py`,
    the coverage widgets in `operon/tui/screens/config_coverage.py`, and
    `actions.save_classification_profile` / `actions.save_coverage_profile`
    sharing `save_profile`'s version, snapshot and rollback machinery; a
    classification profile whose conditions nest deeper than one `any:`/`not:`
    level, or a coverage profile whose structure exceeds the flat grammar, opens
    read-only) and single recipes inside
    `config/tools.yaml` through structured control-based forms (no free-text
    YAML): every save bumps the `version`, records the same content-addressed
    snapshot the CLI records (`qc_profiles` / `recipe_snapshots`), and
    restores the previous file bytes on failure; keys the forms do not model
    are preserved verbatim; history modals restore snapshots into the editor
    as the next version; tools-check runs in a worker with per-row updates.
    `operon/tui/parity.py` is the CLI/TUI parity registry: every CLI leaf
    command is registered `implemented`/`cli-only`/`planned`, enforced by
    `tests/unit/test_tui_cli_parity.py` (see Conventions).
    Textual is imported only inside this package, which the `tui` command
    handler imports lazily.
  - Other top-level modules by responsibility: `database.py` (SQLite schema
    and migrations), `schema.py` (YAML metadata schema and validation),
    `config.py` (project configuration and directory layout), `files.py`
    (immutable manifest archival and verification), `profiles.py` +
    `rules.py` (versioned QC profiles and the decision engine),
    `workflow.py` (state machine and run logs), `pipeline.py` (the
    four-stage ingest → standardize → QC → evaluate runner shared by
    `run-pipeline`'s CLI and TUI), `tools.py` (external-tool
    recipes from `config/tools.yaml`), `taxonomy.py` + `coverage.py` (frozen
    NCBI Taxonomy snapshots and coverage denominators), `release.py` +
    `export.py` (immutable releases and selective exports), `lifecycle.py`
    (audited reversible entity retirement), `lineage.py` (adopting external
    workflow outputs), `sequence_tools.py` (alignment-driven domain
    extraction and sequence selection), `classify.py` (versioned
    sequence_classification profiles labelling `sequence_labels`),
    `fanout.py` (data-derived fan-out of registered sequence files into
    per-unit FASTAs under `analysis/derived/`, selected downstream by recipe
    `file_role_prefix`), `timetree.py` (TimeTree query/calibration CLI
    group backed by the query-cache adapter), `backup.py`, `reports.py`,
    `table_import.py`,
    `import_wizard.py`, `entity_view.py`, `environment.py`
    (execution-environment capture with at-capture redaction; recipe
    `environment_policy` governs environment-aware cache reuse),
    `shutdown.py` (graceful SIGINT/SIGTERM
    handling), `ncbi_reconcile.py` (development-era adapter anomaly repair),
    `demo.py` (deterministic synthetic demo project), `errors.py`,
    `utils.py`.
- `tests/` — pytest suite organized as `unit/`, `integration/`,
  `regression/`, `compatibility/`, with shared fixtures in
  `tests/helpers.py`.
- `docs/` — Sphinx documentation in two mirrored language trees, `docs/en/`
  and `docs/zh/`, each split into `overview.md`, `getting-started/`,
  `guides/`, `architecture/`, `reference/`, `operations/`, and
  `contributor/`. Each tree is a Sphinx project of its own
  (`docs/<language>/conf.py`, sharing settings from `docs/conf_common.py`) and
  a Read the Docs project of its own, linked there as parent and translation:
  `operonproject` (English) and `operonproject-zh` (Chinese). Read the Docs
  accepts no build-configuration file name other than `.readthedocs.yaml`, so
  each tree carries one next to its `conf.py`
  (`docs/<language>/.readthedocs.yaml`) and the dashboard points the project at
  that path. `docs/locales/` is the catalog directory
  for a future gettext-maintained language; `tests/unit/test_docs_projects.py`
  guards the layout.
- `benchmarks/` — representative entity sets for QC performance diagnostics
  (see `docs/*/operations/qc-performance.md`).
- `scripts/` — local developer tooling; `setup-test-matrix.sh` and
  `run-test-matrix.sh` build and drive the uv-managed Python 3.10–3.15
  matrix under .matrix/ (see `docs/*/contributor/development-testing.md`);
  `release-preflight.sh` is the release gate and the script the publish
  workflow runs on the tag; `defects.sh` appends to and queries the defect
  registry (`defects.yml`, see "Defect reports" below).

## Setup, test, and build

Always work inside the project virtual environment (`.venv/` exists in the
repo root; activate it or invoke `.venv/bin/python` explicitly).

```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'   # runtime + pytest + Cython + Sphinx;
                                    # also compiles the qc parsers extension

python -m pytest                    # full suite (coverage gate: >=95% combined
                                    # line+branch, and >=95% branch coverage)
python -m pytest tests/unit         # by category: unit / integration /
                                    # regression / compatibility

python -m pytest --lf -q --no-cov   # iterate: last failures, no coverage
python -m pytest -n 4 --dist loadfile   # parallel full suite (xdist, in the dev/test extras)
scripts/setup-test-matrix.sh        # once per machine: uv-managed 3.10-3.15 venvs
scripts/run-test-matrix.sh          # whole suite on 3.10-3.15, in waves
                                    # (MATRIX_CONCURRENCY / MATRIX_JOBS override the split)
scripts/release-preflight.sh        # the release gate: version, full suite, both doc
                                    # trees, defect registry, matrix evidence for HEAD;
                                    # --run-matrix runs the matrix inside the gate, and
                                    # --tag vX.Y.Z asserts the tag's name/signature/target

python setup.py build_ext --inplace # rebuild only the Cython extension

sphinx-build -W --keep-going -b html docs/en docs/_build/en/html  # strict build,
sphinx-build -W --keep-going -b html docs/zh docs/_build/zh/html  # one per language
```

Run the relevant test category after any change; run the full suite before
considering work done.

CI (`.github/workflows/test.yml`) runs pytest on Python 3.10–3.15 and the strict
Sphinx build. Releases are published exclusively to PyPI, and
`.github/workflows/publish.yml` builds nothing and uploads nothing until its
`verify-release` job has seen a `success` conclusion for the tagged commit's `test`
run *and* a passing `scripts/release-preflight.sh --ci --tag <tag>`; see
`docs/*/contributor/pypi-release.md`.

The project uses `pytest-xdist` to parallel testing. Avoid sharing state between tests
to prevent unexpected or random test results.

> Running `python -m pytest` directly takes approx 35 seconds
> (auto: 24 workers). Running the local test matrix takes about 5 minutes
> (6 versions, 3 at a time × 7 workers by default; `MATRIX_CONCURRENCY` /
> `MATRIX_JOBS` override the split).
> 
> Measured on Intel Core i7-13700HX (16c24t).

## Git

If the user requests or grants permission, do `git commit` after modify.
Avoid including overly large changes in a single commit. For commit messages,
follow the pattern used in the last five commits; otherwise, the Conventional
Commits guidelines.

**DO NOT** push any commit or tag unless the user **explicitly** requests that.

If times out, assume the user is not present and that a GPG signature
is required. Add `-c commit.gpgsign=false` behind `git` may help (However,
re-signing will reset the commit hash, and corrupt the defects.yml. Please
proceed with caution).

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
- CLI-first: new capabilities land in the CLI/core first, and the TUI never
  runs ahead of the CLI. Any change to CLI commands, flags, or the TUI
  surface must update the parity registry `operon/tui/parity.py` in the same
  commit — the parity tests in `tests/unit/test_tui_cli_parity.py` fail
  otherwise. Commands intentionally not offered in the TUI are registered
  `cli-only` with a reason; known gaps are registered `planned` with a
  milestone.
- `docs/*/operations/database-compatibility.md` lists migration code that
  exists only for pre-1.0 databases and is scheduled for removal at the 1.0
  release; check it before touching `operon/database.py` migrations or the
  NCBI adapter's schema-upgrade path.
- Using `ruff` to re-format the code.

## Defect reports

Confirmed defects are tracked in the machine-readable registry
`defects.yml` at the repository root (plus any `defects/*.yml` shards;
`scripts/defects.sh` appends and queries records). The full process is in
`docs/*/contributor/defect-tracking.md`; the binding rules are:

1. **Register first.** When an audit or investigation confirms a defect,
   append its `ODR-XXXX` record to `defects.yml` before the fix lands.
2. **One commit per defect.** The fix and its regression tests ride in the
   same commit; fill in `fix_commit` when committing.
3. **Close the test loop.** Regression tests for a defect carry
   `@pytest.mark.bug("ODR-XXXX")`, and every `fixed`/`verified` record
   lists at least one such test; `tests/unit/test_defect_registry.py`
   validates the registry schema and both directions of this closure.

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
- The set of language projects → `docs/<language>/conf.py`, its
  `docs/<language>/.readthedocs.yaml`, and the registry in
  `tests/unit/test_docs_projects.py`

Version markers in docs (`operon` 0.9.0, database schema 2.11, metadata
schema 1.4) must match `pyproject.toml` and the code. Do not write the
current values literally in Markdown sources: use the `myst_substitutions`
references `{{ operon_version }}`, `{{ db_schema }}`, and
`{{ metadata_schema }}`, which `docs/conf_common.py` resolves from the single
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

Due to specific limitations of the sandbox environment, executing certain TUI
test code may cause the system to freeze; this often occurs on Codex/ChatGPT.
The specific reason is that when this test code runs in the sandbox, it may
result a Textual/asyncio cleanup block, and reports "FAIL" due to timeout.

If you are Codex/ChatGPT, please execute TUI-related test code OUTSIDE the sandbox.
