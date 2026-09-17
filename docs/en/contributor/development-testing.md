# Development and testing

## Setup and test runs

Install the repository checkout with the `dev` extra as described in [Installation](../getting-started/installation.md#install-from-the-repository); it adds pytest, Cython, and
Sphinx. An editable install resolves `operon.__version__` from install-time metadata, so after every version bump in `pyproject.toml` re-run `pip install -e '.[dev]'` — otherwise the CLI/TUI keep displaying the previous version outside the checkout directory. Then:

```bash
python -m pytest

# or run by category
python -m pytest tests/unit
python -m pytest tests/integration
python -m pytest tests/regression tests/compatibility

# build the Sphinx documentation strictly
sphinx-build -W --keep-going -b html docs docs/_build/html
```

The pytest suite is organized into four categories — `unit`, `integration`, `regression`, `compatibility` — covering:
Python 3.10 syntax and runtime gates, schema validation and controlled vocabularies, metadata round-trips and
transaction rollback, stable IDs, default copy isolation, query read-only constraints, file-aware QC identity,
profile/decision history, gzip FASTA recognition, assembly/annotation QC, rule decisions, idempotent ingest and conflict
protection, checksum-tamper detection, the demo end-to-end pipeline and release verification, the NCBI Datasets adapter,
wrapped BLAST/HMMER/BUSCO execution, directory artifacts, JSON summaries, conda run prefix parsing, cache hits/forced
re-runs, result write-back, and input-tamper rejection.
The taxonomy coverage integration tests additionally cover taxonomy source-package identity conflicts, profile
type/content conflicts, exclusion rules, secondary TaxIDs, denominator/report idempotence, and that active metadata
modifications do not affect the release-frozen scope.

The project uses `pytest-xdist` to parallel testing. Avoid sharing state between tests to prevent unexpected or random
test results.

## Fast local verification

The full suite takes about seven minutes serially; the loop below aims to run it at most once per change.

1. **Re-run only what failed.** `python -m pytest --lf -q --no-cov` replays pytest's
   last-failed cache before anything else.
2. **Stop at the first failure** while iterating: `python -m pytest tests/unit -x -q --no-cov`,
   with the file or test you actually touched rather than a whole category.
3. **Parallel execution.** On a 24-core workstation, running `python -m pytest -n 24 --dist loadfile` 
   (with `pytest-xdist`) can reduce the execution time to 30–40 seconds. `--dist loadfile` ensures that each test file
   is assigned to only one worker, which is required for Textual UI testing.
   Coverages are the most resource-intensive: use `--no-cov` during iterations and run the test only once at the end.
4. **Cross-version matrix in one pass.** `scripts/setup-test-matrix.sh` creates uv-managed CPython 3.10-3.15
   interpreters plus one venv per version inside `.matrix/`. `scripts/run-test-matrix.sh` runs all six concurrently:

   ```bash
   scripts/setup-test-matrix.sh                      # once per machine
   scripts/run-test-matrix.sh                        # whole suite, six versions, 2~3 minutes
   scripts/run-test-matrix.sh tests/unit -x          # extra pytest arguments are forwarded
   MATRIX_JOBS=4 scripts/run-test-matrix.sh          # workers per version
   ```

   Each version logs to `.matrix/logs/<version>.log`, so a failure there can be re-run
   directly with that interpreter. The scripts work on Linux and macOS. `.matrix/` is
   git-ignored.
5. **Platform-specific code cannot be verified by one matrix job alone.** Linux and macOS
   differ in the system utilities the environment probe calls and in `pathlib`/`resource`
   behaviour. Code guarded by `sys.platform`/`os.name`, or shelling out to system tools,
   should carry a test that reproduces the other platform's condition on Linux — for
   example an empty `PATH` without GNU `timeout` — rather than depending on the macOS job
   to notice.

## Special Note For Codex/ChatGPT

Due to specific limitations of the sandbox environment, executing certain TUI test code may cause the system to freeze;
this often occurs on Codex/ChatGPT. The specific reason is that when this test code runs in the sandbox, it may result 
a Textual/asyncio cleanup block, and reports "FAIL" due to timeout.

If you are Codex/ChatGPT, please execute TUI-related test code OUTSIDE the sandbox.

This information has also been updated in AGENTS.md.

## Coverage gate

`python -m pytest` always measures coverage (`--cov=operon --cov-branch`, configured in `pyproject.toml`) and fails when
the reported total drops below the `fail_under` threshold. The reported total is the combined figure
`(covered lines + covered branches) / (valid lines + valid branches)`, so a change that adds only lines without their
branches lowers it. Branch coverage must additionally stay at or above 95% of all valid branches.

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
