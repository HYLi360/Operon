# Defect tracking

The current defect tracking of Operon records the confirmed defects in
a machine-readable registry: `defects.yml` at the repository root,
instead of external issue tracker. The registry is the single source
of truth for defect reports and their disposition; [Resolved issues](../reference/resolved-issues.md)
is the user-facing summary and keeps the historical K-series numbers
alongside the ODR series.

> K-series numbers are archived only in [Resolved Issues](../reference/resolved-issues.md).
> The registry will not be updated with, or supplemented by K-series numbers.

## Registry

Every confirmed defect gets one record under `defects:` with a sequential
`ODR-XXXX` id. Records load from `defects.yml` plus any `defects/*.yml`
shards (sorted by name), so an overgrown registry can be split without
changing tooling.

Record fields:

| Field | Content |
|---|---|
| `id` | `ODR-XXXX`, sequential, unique (validated) |
| `title` | one-line summary |
| `reported` | ISO date the defect was confirmed |
| `introduced_in` | version or commit that introduced the defect, or `null` if undetermined |
| `affected` | affected versions and the preconditions for triggering |
| `severity` | `low` / `medium` / `high` / `critical` |
| `component` | primary module or subsystem |
| `status` | `open` → `confirmed` → `fixed` → `verified`; `wontfix` / `duplicate` are terminal |
| `reproduction` | observed behavior and how it was reproduced |
| `disposition` | what was done (or why no action) |
| `fix_commit` | 40-hex commit id once the fix lands; `null` while pending (format-validated only) |
| `fixed_in` | release version once shipped; `null` until then |
| `regression_tests` | `path::test` list; mandatory for `fixed`/`verified` |

Use `scripts/defects.sh` to append and query records:

```bash
scripts/defects.sh list [--status open] [--component tools]
scripts/defects.sh show ODR-0001
scripts/defects.sh add --title "..." --severity medium --component tools \
    --reproduction "..."
```

`add` allocates the next id and appends a `status: open` record.

## Rules

1. **Register first.** When an audit or investigation confirms a defect,
   append its record to `defects.yml` before the fix lands.
2. **One commit per defect.** The fix and its regression tests ride in the
   same commit; fill in `fix_commit` when committing.
3. **Close the test loop.** Every regression test for a defect carries
   `@pytest.mark.bug("ODR-XXXX")` (registered in `pyproject.toml`), and
   every `fixed`/`verified` record lists at least one such test.
   `tests/unit/test_defect_registry.py` validates the registry schema and
   both directions of the marker closure, so a missing record, a dangling
   marker, or a `fixed` record without a regression test fails the suite.
   `python -m pytest -m bug` runs exactly the defect regressions.

`verified` additionally requires `fix_commit` and `fixed_in`, and is set
when the fix ships in a release.
