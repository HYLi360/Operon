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
