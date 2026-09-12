# Development and testing

## Setup and test runs

Install the repository checkout with the `dev` extra as described in [Installation](../getting-started/installation.md#install-from-the-repository); it adds pytest, Cython, and Sphinx. Then:

```bash
python -m pytest

# or run by category
python -m pytest tests/unit
python -m pytest tests/integration
python -m pytest tests/regression tests/compatibility

# build the Sphinx documentation strictly
sphinx-build -W --keep-going -b html docs docs/_build/html
```

The pytest suite is organized into four categories — `unit`, `integration`, `regression`, `compatibility` — covering: Python 3.10 syntax and runtime gates, schema validation and controlled vocabularies, metadata round-trips and transaction rollback, stable IDs, default copy isolation, query read-only constraints, file-aware QC identity, profile/decision history, gzip FASTA recognition, assembly/annotation QC, rule decisions, idempotent ingest and conflict protection, checksum-tamper detection, the demo end-to-end pipeline and release verification, the NCBI Datasets adapter, wrapped BLAST/HMMER/BUSCO execution, directory artifacts, JSON summaries, conda run prefix parsing, cache hits/forced re-runs, result write-back, and input-tamper rejection.
The taxonomy coverage integration tests additionally cover taxonomy source-package identity conflicts, profile type/content conflicts, exclusion rules, secondary TaxIDs, denominator/report idempotence, and that active metadata modifications do not affect the release-frozen scope.

## Fast local verification

The full suite takes about seven minutes serially; the loop below aims to run it at most
once per change.

1. **Re-run only what failed.** `python -m pytest --lf -q --no-cov` replays pytest's
   last-failed cache before anything else.
2. **Stop at the first failure** while iterating: `python -m pytest tests/unit -x -q --no-cov`,
   with the file or test you actually touched rather than a whole category.
3. **Parallelize.** `python -m pytest -n 4 --dist loadfile` (pytest-xdist) runs the suite in
   about a quarter of the time — roughly 7 minutes to under 2 on a 24-core workstation — and
   `--dist loadfile` keeps each test file on one worker, which the Textual UI tests need.
   It ships with the `test` and `dev` extras (`python -m pip install -e '.[dev]'`). Coverage is the expensive part:
   iterate with `--no-cov` and measure once, at the end.
4. **Cross-version matrix in one pass.** `scripts/setup-test-matrix.sh` creates uv-managed
   CPython 3.10-3.13 interpreters plus one venv per version inside `.matrix/` (3.14 comes
   from the project `.venv`), and `scripts/run-test-matrix.sh` runs all five concurrently:

   ```bash
   scripts/setup-test-matrix.sh                      # once per machine
   scripts/run-test-matrix.sh                        # whole suite, five versions, ~3 minutes
   scripts/run-test-matrix.sh tests/unit -x          # extra pytest arguments are forwarded
   MATRIX_JOBS=8 scripts/run-test-matrix.sh          # workers per version
   ```

   Each version logs to `.matrix/logs/<version>.log`, so a failure there can be re-run
   directly with that interpreter. The scripts work on Linux and macOS; `.matrix/` is
   git-ignored.
5. **Platform-specific code cannot be verified by one matrix job alone.** Linux and macOS
   differ in the system utilities the environment probe calls and in `pathlib`/`resource`
   behaviour. Code guarded by `sys.platform`/`os.name`, or shelling out to system tools,
   should carry a test that reproduces the other platform's condition on Linux — for
   example an empty `PATH` without GNU `timeout` — rather than depending on the macOS job
   to notice.
6. **CI gates the expensive part.** The `gate` job (unit tests plus the strict
   documentation build on one interpreter, run with four workers) must pass before the
   ten-job matrix starts, so a broken push is rejected in about two minutes; the matrix
   itself also runs four workers per job.

## Special Note For Codex/ChatGPT

Due to specific limitations of the sandbox environment, executing certain TUI test code may cause the system to freeze; this often occurs on Codex/ChatGPT. The specific reason is that when this test code runs in the sandbox, it may results a Textual/asyncio cleanup block, and reports "FAIL" due to timeout.

If you are Codex/ChatGPT, please execute TUI-related test code OUTSIDE the sandbox.

This information has also been updated in AGENTS.md.

## Coverage gate

`python -m pytest` always measures coverage (`--cov=operon --cov-branch`, configured in `pyproject.toml`) and fails when the reported total drops below the `fail_under` threshold. The reported total is the combined figure `(covered lines + covered branches) / (valid lines + valid branches)`, so a change that adds only lines without their branches lowers it. Branch coverage must additionally stay at or above 90 % of all valid branches.

Read the gaps with:

```bash
python -m pytest --cov-report=term-missing          # per-file missing lines and partial branches
python -m pytest tests/unit                          # the category you are changing
```

Lines that cannot be exercised by any supported environment — platform-only guards, dependency-import fallbacks that cannot be triggered from an installed checkout, and defensive branches that are unreachable by construction — carry a trailing `# pragma: no cover` comment (already part of `exclude_also` in `pyproject.toml`). The pragma is not a substitute for a test: code reachable through a public entry point must be tested rather than excluded.

## Documentation synchronization

When changing the CLI, configuration fields, behavior, or storage layout, update the Chinese and English documentation in the same change:

| Change type | Documentation location |
|---|---|
| Commands or arguments | `docs/*/reference/` |
| Task workflows | `docs/*/guides/` and `docs/*/getting-started/` |
| Data model, state machine, or correctness guarantees | `docs/*/architecture/` |
| `tools.yaml` recipes, placeholders, or parsers | `docs/*/reference/recipe-*.md` |
| Migrations, performance diagnostics, or compatibility boundaries | `docs/*/operations/` |

Software versions, database schema versions, and metadata schema versions stated in the documentation must stay consistent with `pyproject.toml` and the code. Write current version markers in the Markdown sources as substitutions:

```text
{{ operon_version }}  {{ db_schema }}  {{ metadata_schema }}
```

`docs/conf.py` resolves them from `pyproject.toml` and the code constants at build time. Substitutions expand in paragraph text only, not inside code spans or fenced code blocks; examples there use `<version>` placeholders instead. Intentional historical pins stay literal: they either live on the allowlisted era-pinned pages under `docs/*/operations/` or carry an inline `<!-- version-pin -->` marker. `tests/unit/test_docs_versions.py` enforces the rule.
