# Workflow History Commands

`operon workflow` provides read-only search and terminal rendering for records
in `workflow_runs`. It does not modify the project, append another workflow
record, or require users to write SQL.

## List workflow runs

```bash
operon workflow list \
  [--from TIME] [--to TIME] \
  [--run-id WF_ID] \
  [--step STEP ...] [--status STATUS ...] \
  [--entity-type TYPE] [--entity-id ID] \
  [--parent-run-id WF_ID] [--resumes-run-id WF_ID] \
  [--tool NAME] [--executor NAME] \
  [--limit N] [--offset N] [--oldest-first] \
  [--format {table,json,jsonl}]
```

With no filters, the command prints the newest 50 runs. The terminal table
keeps the primary diagnostic fields together: start time converted to the
machine's local time zone, status, step, entity, duration, and complete run
ID. Long step and entity labels are shortened only in this table to keep a
normal row near 120 columns; `show`, JSON, and JSONL output never truncate
fields or replace the stored timestamp.

`--from` is inclusive and `--to` is exclusive. Both filter `started_at`, so a
pair of adjacent ranges cannot return the same boundary run twice. Values are
ISO-8601 dates or timestamps. A date or timestamp without an offset is
interpreted in the machine's local time zone; `Z` and explicit offsets are
accepted and compared as absolute times.

`--step` and `--status` use exact matching and can be repeated; repeated values
within one option are combined with OR. Different filters are combined with
AND. `--parent-run-id` finds item runs belonging to a parent workflow, while
`--resumes-run-id` finds a later attempt that explicitly resumes a previous
run.

Results are newest first unless `--oldest-first` is present. `--limit` defaults
to 50; use `--offset` for pagination or `--limit 0` to remove the limit. JSON
emits one array and JSONL emits one object per line. A valid JSON value stored
in `execution_details` is decoded into an object or array in both machine
formats; legacy plain text remains a string.

Examples:

```bash
# Failures that started during one local calendar day.
operon workflow list \
  --from 2026-09-01 --to 2026-09-02 \
  --status failed --status interrupted

# All QC runs for one assembly, in chronological order.
operon workflow list --step qc \
  --entity-type assembly --entity-id ASM_000123 \
  --oldest-first --limit 0

# Runs dispatched through one execution backend, as JSONL.
operon workflow list --executor slurm --format jsonl

# The child runs of a batch/import workflow.
operon workflow list --parent-run-id WF_20260901_120000+0800_abcd1234
```

When no row matches, table output prints `no workflow runs matched`, JSON
prints `[]`, and JSONL prints no lines. All three cases exit successfully.

## Show one workflow run

```bash
operon workflow show WF_ID [--format {text,json}]
```

Text output is grouped into identity and lineage, timing and resources,
execution, artifacts and log paths, outcome, and execution details. Long
commands and errors wrap to the current terminal width; hashes and paths stay
copyable. `--format json` returns every `workflow_runs` column without terminal
shortening and decodes valid `execution_details` JSON.

A missing run ID is a validation error and exits with code 2.

## Provenance boundary and current limitations

The managed project's SQLite database is the sole writable source of truth.
These commands query its `workflow_runs` table. `logs/workflow.jsonl` remains
an append-only machine-readable provenance log and backup/export artifact; it
is not edited or rebuilt by these read-only commands.

This interface currently covers workflow-run records. Changes in `changes`,
direct lifecycle events in `entity_lifecycle_events`, and other domain history
remain available through their dedicated commands or read-only SQL; there is
not yet one cross-table event timeline. Full-text search, live following, and
an interactive TUI are also intentionally deferred until the backend event
model and operational interfaces are mature. The current interface is stable,
scriptable CLI output only.
