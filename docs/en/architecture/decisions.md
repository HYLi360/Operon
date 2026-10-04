# Decision Records

This page indexes the decisions that bind Operon and everything built on top
of it: the invariants, the boundaries of the extension surface, the contracts a
plugin may rely on, and the gates that keep them true. It exists for the same
reason as the defect registry — **an external commitment must be discoverable
in version control, not only argued in a plan**.

Two rules follow from that:

- **Commitments live in `docs/`.** Every statement another project, plugin,
  release or operator may rely on — contracts, invariants, version policies,
  compatibility windows, interaction contracts — **MUST** be documented in this
  tree. Planning notes, milestone logs and reasoning-in-progress live outside
  the repository; they are useful to maintainers but are **not** a reference
  for anyone else.
- **A record names its enforcement.** Where a decision has a test, a gate or a
  CLI surface, the record points at it. A decision without an enforcement
  mechanism says so, and a decision that is still a proposal is marked as one.

Records are grouped by area. Nothing here overrides `AGENTS.md`, which states
the same invariants as project requirements.

## Core invariants

| ID | Decision | Enforced by |
|----|----------|-------------|
| INV-1 | Structured metadata in `operon.sqlite` is the single source of truth; large sequence files never enter the database. | schema validation, `operon/schema.py` |
| INV-2 | Raw data is immutable; derived data is rebuildable. | `ConflictError` on differing bytes, `operon/files.py` |
| INV-3 | File identity is `file_id + sha256 + size_bytes`, never the path. | manifest archival/verification, `operon/files.py` |
| INV-4 | QC tools only measure metrics; decisions come from versioned YAML profiles. | `operon/profiles.py`, `operon/rules.py` |
| INV-5 | Every processing step is an explicit, idempotent state machine transition with machine-readable provenance. | `operon/workflow.py`, `changes`/`workflow_runs` |

## Extension boundaries

| ID | Decision | Status | Where it stands |
|----|----------|--------|-----------------|
| EXT-1 | Analysis add-ons are **independent CLI distributions** invoked as subprocesses through recipes (`operon analyze`) or `operon run-external`. Operon is not their runtime dependency, and they do not import it. | binding | [Extension boundaries](extensibility.md), [External analysis](external-analysis.md) |
| EXT-2 | Why not in-process hooks: execution is subprocess-based and backend-agnostic (local / Slurm / SSH) under one provenance contract. An in-process hook would run only on the controller host and fall out of provenance, cache fingerprinting and environment comparison. Entry-point hooks are deferred, not rejected. | binding | [Extension boundaries](extensibility.md) |
| EXT-3 | The entry-point / `operon.api` design is **deferred**. It is reconsidered when at least two external plugins need in-process behaviour, or when a custom result parser or executor backend is required. | deferred | this page |
| EXT-4 | Add-ons may emit an optional events JSONL stream, imported with `operon import-events`: `metric` events become QC results, `artifact` events become `adopt` manifest drafts. Unknown event types are skipped and counted; an unknown `schema_version` is an error and writes nothing. | planned | this page |
| EXT-5 | Events carry **facts, not judgements**: a plugin may report what it measured or produced, never a state, a decision or a QC verdict — those come only from versioned profiles (INV-4). | binding (follows from INV-4) | this page |
| EXT-6 | Visualization add-ons read a `report view` bundle (a read-only directory with a `bundle_schema_version` and per-member checksums). They never write to the project database. | planned | this page |
| EXT-7 | Plugin distributions inherit the current developer and licence of Operon; distribution names and hosting remain open. | binding | this page |

## Contracts and gates

| ID | Decision | Enforced by |
|----|----------|-------------|
| GATE-1 | CLI first: every capability lands in the CLI/core before the TUI, and the TUI never runs ahead of it. The parity registry `operon/tui/parity.py` records each command as `implemented`, `cli-only` (with a reason) or `planned` (with a milestone). | `tests/unit/test_tui_cli_parity.py`; CI exports `OPERON_PARITY_STRICT=1`, so a `planned` entry that reappears fails the build |
| GATE-2 | Every TUI write is a preview → equivalent CLI command → explicit confirm, calls the same core function as the CLI, and leaves the same `changes`/`workflow_runs` provenance. | `operon/tui/actions.py`, TUI screen tests |
| GATE-3 | Manual overrides (`curate`, forced `set-state`) are always recorded in the `changes` audit table. | `operon/lifecycle.py`, audit tests |
| GATE-4 | Coverage and branch-coverage gates, the Codacy policy and the contribution flow. | [Development and testing](../contributor/development-testing.md) |
| GATE-5 | Version markers have a single source of truth (`pyproject.toml`, `operon/database.py`, `operon/schema.py`) and **MUST NOT** be written literally in documentation — use the `{{ operon_version }}`, `{{ db_schema }}` and `{{ metadata_schema }}` substitutions. | `tests/unit/test_docs_versions.py` |
| GATE-6 | Defects are registered before their fix lands, one commit per defect, and every `fixed`/`verified` record lists at least one regression test carrying `@pytest.mark.bug("ODR-XXXX")`. | `scripts/defects.sh`, `tests/unit/test_defect_registry.py` |
| GATE-7 | Performance budgets for the TUI and for the machine-readable exports. | [Performance budgets](../operations/performance-budgets.md) — policy binding, baseline still to be measured |
| GATE-8 | TUI changes must not introduce Textual race defects: assert UI state only through predicate waits, repair framework-level guards with the smallest possible override, and run the TUI modules at least three times with random ordering locally. | [Development and testing](../contributor/development-testing.md); defects ODR-0027, ODR-0050, ODR-0051, ODR-0055, ODR-0056, ODR-0057 |

## Open questions

These are recorded so that they are answered in `docs/`, not only in planning
notes. Until a row is promoted to a decision, no external consumer may rely on
either outcome.

- **Read-only SQLite access for third-party consumers** — whether to commit to
  a documented read-only table/column surface (beyond the bundle) and, if so,
  what migration window accompanies a schema bump.
- **Bundle evolution policy** — the exact compatibility promise for
  `bundle_schema_version` (the working assumption is append-only fields, with
  a major bump plus a migration note for anything breaking).
- **Event stream ownership** — whether the event schema is embedded in the
  plugin contract or ships as its own document.

## Phase-two file contract

The [plugin contract](../reference/plugin-contract.md) specifies version rejection, bundle evolution, hits import and preset conflicts. Enforcement lands with the phase-two commands and contract tests; EXT-4 and EXT-6 remain planned until those commands land. Event schemas ship as independent package resources; direct SQLite compatibility remains open.
