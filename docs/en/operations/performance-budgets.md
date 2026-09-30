# Performance Budgets

This page defines where Operon's performance budgets live, what each one
measures, and how a number becomes binding. Its purpose is to make performance
a **reviewable decision** instead of a surprise found by the first operator
with a large project.

## Status

**Policy binding, baseline not yet measured.** No number on this page is a
commitment until it carries a measured baseline and the command that produced
it. The first change that touches the TUI data path (the visual refactor in
progress) is expected to produce the TUI baseline; `report view` gets its
baseline when the command lands.

Until then, treat any performance claim as unverified.

## Budget targets

| ID | Target | What is measured | Measured on |
|----|--------|------------------|-------------|
| PERF-1 | TUI first frame | `operon tui` → the Home panel composed and painted (cold process, warm OS cache) | a project fixture with 10⁴ and 10⁵ files |
| PERF-2 | TUI panel switch | keypress → the destination panel painted, with its data loaded (median of ≥5 switches, same session) | same fixtures |
| PERF-3 | Machine-readable export (`report view` bundle, `report … --format json/tsv --out`) | wall-clock time to a complete, atomically renamed output | same fixtures |

Two rules apply to every budget:

- **A budget is a maximum, not an average.** The measurement is defined as the
  worst of three consecutive runs on the fixture, so that a budget cannot be
  met by a lucky cache.
- **A budget must be reproducible by a third party.** The record states the
  fixture, the exact command and the machine class it was measured on. A
  number without its command is not a baseline.

## Fixtures

The fixtures are the benchmark sets described in
[Built-In QC Performance Diagnostics](qc-performance.md), extended to the same
row-count ladder used there (10³ / 10⁴ / 10⁵ files). The deterministic demo
project (`operon init-demo`) is the smoke-level fixture and is too small to
measure anything but regressions in startup work.

## How a budget is recorded

1. Measure on the fixture with the command written next to the number.
2. Record the baseline here, in both language trees, in the same change that
   writes the number into a test or a diagnostic script.
3. When a change makes a measurement worse than the recorded budget, the
   change **MUST** either fix the regression or update the budget with the
   reason — silently accepting a slower path is not an option.
4. Raise a budget only with a measurement that justifies the new value.

## Measurement method

The measurement has to be scripted — never by hand — because the numbers are
reviewed in pull requests. The script does not exist yet; adding the first
budget number and the script that produces it **MUST** happen in one change.
Until that lands, the recipe below is the shape the script is expected to take:

```bash
# 1. build the fixture (deterministic, same seed every run)
operon init-demo <fixture-root>          # smoke level
# 10^4 / 10^5-file fixtures: see operations/qc-performance.md

# 2. TUI first frame / panel switch: measure inside the app
#    (a Textual pilot run that composes the panel and stops the clock on the
#     first painted frame — never a screenshot timestamp)
# 3. machine-readable export
time operon report view --out /tmp/view_bundle
```

Instrumentation rules for the TUI numbers:

- Measure **composed and painted**, not "the worker started": the expensive
  failure mode is a data query that blocks the first frame.
- Measure with the app started from a cold process, because the TUI's queries
  and the tools config load happen on startup.
- Never measure through a screenshot or a recording: those add a frame of
  latency that is not in the code path under test.

## Related

- [Built-In QC Performance Diagnostics](qc-performance.md) — QC timing fields
  and the benchmark sets.
- [Decision Records](../architecture/decisions.md) — GATE-7, the record this
  page enforces.
