# Development and testing

## Setup and test runs

```bash
python -m pip install -e '.[dev]'
python -m pytest

# or run by category
python -m pytest tests/unit
python -m pytest tests/integration
python -m pytest tests/regression tests/compatibility

# build the Sphinx documentation strictly
python -m pip install -e '.[docs]'
sphinx-build -W --keep-going -b html docs docs/_build/html
```

The pytest suite is organized into four categories — `unit`, `integration`, `regression`, `compatibility` — covering: Python 3.10 syntax and runtime gates, schema validation and controlled vocabularies, metadata round-trips and transaction rollback, stable IDs, default copy isolation, query read-only constraints, file-aware QC identity, profile/decision history, gzip FASTA recognition, assembly/annotation QC, rule decisions, idempotent ingest and conflict protection, checksum-tamper detection, the demo end-to-end pipeline and release verification, the NCBI Datasets adapter, wrapped BLAST/HMMER/BUSCO execution, directory artifacts, JSON summaries, conda run prefix parsing, cache hits/forced re-runs, result write-back, and input-tamper rejection.
The taxonomy coverage integration tests additionally cover taxonomy source-package identity conflicts, profile type/content conflicts, exclusion rules, secondary TaxIDs, denominator/report idempotence, and that active metadata modifications do not affect the release-frozen scope.

## Special Note For Codex/ChatGPT

To ensure security, code testing in Codex/ChatGPT runs in a sandbox by default. However, this causes the `test_tui.py` section to experience Textual/asyncio cleanup blocking during testing, resulting in a "FAIL" report due to a timeout.

To run the full test suite, first exclude `test_tui.py`, then run it separately outside the sandbox.

This information has also been updated in AGENTS.md.

## Documentation synchronization

When changing the CLI, configuration fields, behavior, or storage layout, update the Chinese and English documentation in the same change:

| Change type | Documentation location |
|---|---|
| Commands or arguments | `docs/*/reference/` |
| Task workflows | `docs/*/guides/` and `docs/*/getting-started/` |
| Data model, state machine, or correctness guarantees | `docs/*/architecture/` |
| `tools.yaml` recipes, placeholders, or parsers | `docs/*/reference/recipe-*.md` |
| Migrations, performance diagnostics, or compatibility boundaries | `docs/*/operations/` |

Software versions, database schema versions, and metadata schema versions stated in the documentation must stay consistent with `pyproject.toml` and the code.
