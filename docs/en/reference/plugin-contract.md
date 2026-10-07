# Plugin file contracts

Analysis add-ons are independent command-line programs. Operon invokes them as
subprocesses through recipes or `run-external`; they do not import Operon or
write SQLite. Visualization add-ons consume `report view` bundles. Built-in
parsers and presets remain available. In-process hooks and `operon.api` remain
deferred. Add-on distributions inherit Operon's developer and BSD-2-Clause
licence; distribution names and hosting are not prescribed here.

## Versions and validation

The initial measurement, hits, event and bundle contracts use integer version
`1`. These versions are independent of {{ operon_version }}, database schema
{{ db_schema }} and metadata schema {{ metadata_schema }}. Writers emit exactly
the documented version; importers reject unknown versions before any database
or artifact write. JSON Schema files ship in `operon/contracts/` (measurement in
`operon/qc/measure.schema.json`). No schema-validation dependency is needed.

Future additive bundle members/columns require a documented minor evolution;
consumers select columns by name and may ignore additions. Removing members,
renaming columns or changing meanings requires a new major integer version and
a migration note. The current producer emits only version `1`; this is not a
promise that a current importer accepts a future payload version. Direct SQLite
access is not a third-party compatibility contract.

## Inputs, outputs, logs and exits

Consume an `operon export` directory: `manifest.tsv` identifies materialized
inputs by `file_id`, `sha256` and `size_bytes`, accompanied by `checksums.sha256`,
`qc.tsv` and `provenance.json`. Paths locate bytes, never identify files. Read the
[external analysis guide](../guides/external-analysis.md) for export selection.

Exit zero only after outputs are complete; nonzero means failure. Declare
outputs with `run-external --expected-output` or recipe output fields. Operon
captures stdout/stderr in `logs/<run_id>.stdout.log` and `.stderr.log`, and records
the command, backend, versions, inputs and status in `workflow_runs`.

For adoption, emit a JSON list. Each item requires `path`, `entity_type`,
`entity_id`, `role`, `derived_from` (a nonempty list of manifest file IDs).
Optional fields are `format`, `compression`, `workflow_run_id`. Submit this list
with `operon adopt --from-manifest PATH`; identical bytes reuse identity, differing
bytes for the same entity/role are a conflict. Event artifacts produce drafts
only: adoption is a separate explicit operation.

## Measurement payload

`qc-measure` produces the existing built-in JSON format, described in
[File and QC Commands](cli-files-qc.md). Its `tool` is `operon.builtin`, not an
arbitrary plugin name. `schema_version`, `tool`, `tool_version`, `parameter_set`,
`file` and `metrics` are required. File identity includes `sha256` and
`size_bytes`; `file_id` resolves ambiguous identical bytes. Metrics carry
`qc_stage`, `metric_name`, `metric_value`, optional `metric_numeric`,
`metric_unit`, `parameter_set`. Optional `sequences` maps IDs to lengths.

An independent analysis program emits external QC TSV (the `import-qc` columns)
or metric events, preserving its own tool name/version. It can invoke
`operon qc-measure` as a separate command when built-in measurement is wanted.
The measurement Schema describes existing output; `import-qc` acceptance and
output are unchanged.

## Hits payload and preview

`operon import-hits --file hits.json [--dry-run]` accepts a JSON object:

```json
{
  "schema_version": 1,
  "job_id": 42,
  "file": {"file_id": "FIL_000001", "sha256": "<64 hex digits>", "size_bytes": 120},
  "results": [{"metric_name": "query_count", "metric_value": "1", "metric_numeric": 1}],
  "hits": [{"query_id": "q1", "subject_id": "s1", "hit_rank": 1,
            "metric_name": "evalue", "metric_value": "0.001", "metric_numeric": 0.001}],
  "alignments": [{"query_id": "q1", "subject_id": "s1", "hit_rank": 1,
                  "query_start": 1, "query_end": 12, "subject_start": 2, "subject_end": 13}]
}
```

All three arrays are required (and may be empty). `results` maps to
`analysis_results`, `hits` to `analysis_hits`, and `alignments` to
`analysis_alignments`. The existing completed `analysis_jobs` row supplies
entity, analysis name, tool, parameters and parent workflow. Its input hash and
file identity must match the current manifest. Obtain the job ID from
`operon query --sql 'SELECT job_id FROM analysis_jobs ...'` after `analyze`.

Ranks are positive integers **per query**, supplied by the plugin. Summary keys
are metric names; hit keys are `(query_id, hit_rank, metric_name)`; alignment keys
are `(query_id, hit_rank)`. Keys within a payload must be unique. Subject IDs at
the same query/rank must agree. Coordinates are optional positive 1-based
inclusive integers; reverse orientation may use start greater than end.
Optional alignment fields are `evalue`, `bitscore`, `percent_identity`, and
`extra` (an object stored as canonical JSON). Numeric values must be finite.

The importer stores supplied values without sorting ranks, deriving summaries,
writing QC metrics or changing entity state. An identical reimport is a no-op,
including audit records. Differing evidence for an already populated job is a
conflict; rerun the recipe to obtain a new job. Import is transactional, records
a `changes` entry and an `import-hits` child workflow of the producing run.
Preview validates the same input and conflicts using a read-only connection.

A recipe may set `result_parser: plugin:<name>` to declare that its independent
program owns parsing and emits a payload for later import. Operon runs and
checks outputs normally and does not parse them. Built-in parser names retain
their behaviour; unrecognized names outside the `plugin:` namespace are errors.

## Preset fragments

`operon tools add-preset --file preset.yaml [--dry-run]` adds a `version: 1`
fragment containing a `tools` mapping. It does not replace project defaults or
remove built-in presets. New tools or new recipes of an existing, identically
configured tool may be added. Identical definitions are a no-op; differing tool
fields or existing recipe definitions are conflicts. Recipe names must be
unique project-wide; tool/recipe names must be safe configuration names.

Every affected recipe is validated through the normal recipe loader and stored
as a content-addressed `{"recipe": ..., "tool": ...}` snapshot. Publishing uses
an atomic file replacement, round-trip validation and exact byte restoration
on failure. A dry run writes neither configuration nor database. This imports
a declarative fragment, not Python modules or plugin packages.

## Events JSONL

Each nonblank line is one JSON object with integer `schema_version: 1`, a
nonempty `event_id` unique within the producing run, and `type`. Event IDs are
opaque strings. Events carry measurements and artifact facts, never verdicts,
states or decisions. Schemas ship as `operon/contracts/events.schema.json`.

A `metric` event has `metric` containing `entity_type`, `entity_id`, `qc_stage`,
`metric_name`, `metric_value`, `tool`, `tool_version`, `parameter_set` and optional
`metric_numeric`, `metric_unit`, `file` (the complete three-part identity).
An `artifact` event has `artifact` in the adopt item format above. The importer
attaches the producing `workflow_run_id`; a conflicting supplied run is refused.
Unknown event types are skipped and counted, but still need a valid envelope
and supported version. Invalid known events or a later invalid line reject the
whole import before any write.

`run-external --events PATH` and `analyze --events PATH` register the event
location in `workflow_runs.execution_details.events_path`. This is a declared
expected output, so SSH transfers it with the other outputs. For multiple
analysis inputs, use a template containing `${file_id}`; `${output}` is also
available. The path is provenance only: it does not pass a new argument to the
program or automatically import events. Requesting events uses a distinct cache
fingerprint and requires a fresh execution rather than reusing untracked files.

`operon import-events --run RUN_ID --file events.jsonl [--out adopt.json]
[--dry-run]` validates the producing run and imports metric facts through the
QC core. Artifacts become a JSON list draft, defaulting to
`analysis/event-drafts/<run_id>.json`. Deduplication is `(run_id, event_id)` in
that run's `execution_details`; identical events are no-ops, changed content
under an existing ID conflicts. The ledger stores canonical event hashes and
artifact drafts, so a reimport can reconstruct the same draft. Metrics, ledger,
child workflow and audit commit together; a failed draft publication restores
previous bytes. Import never adopts artifacts or evaluates a profile.

## Read-only view bundle

`operon report view --out DIR` writes one complete, deterministic snapshot using
a read-only connection. It adds no workflow or audit records. The destination
must be absent, or already contain the identical bundle; a different existing
destination conflicts. A staging directory is renamed only after all members
are complete. Failure leaves no partial destination.

| Member | Content |
| --- | --- |
| `manifest.json` | `bundle_schema_version`, members' SHA-256 and row counts; no clock or absolute project path |
| `entities.tsv` | Entity identities and states, including effective retirement |
| `files.tsv` | Manifest files |
| `qc_wide.tsv` | The shared `qc_wide` renderer, active entities by default |
| `decisions.tsv` | Current decisions |
| `analysis_results.tsv`, `analysis_hits.tsv`, `analysis_alignments.tsv` | Stored evidence |
| `lineage.tsv` | File-to-file lineage |
| `coverage/reports.tsv`, `coverage/metrics.tsv` | Previously stored coverage evidence, no new calculation |

Tables except wide QC include recorded retired evidence; the entity retirement
column lets consumers filter it. All members have deterministic row order and
TSV escaping. `qc_wide.tsv` equals `report qc --wide --format tsv` byte for byte.
Consumers verify member hashes before reading and never reverse-write a bundle
into the database. Visualization dependencies belong to the add-on distribution.
Runnable examples accompany the implementation.

Metric event parameter sets receive a stable `:events:<run/event hash>` suffix
so separate events remain independently attributable. Analysis sidecars must
be separate paths under `analysis/`.

Try the [standalone toy plugin file loop](plugin-examples.md).

Structural IDs, sizes, ranks and coordinates fit SQLite signed 64-bit integers;
numeric evidence is stored as finite SQLite REAL values.

Plugin JSON/JSONL decoding rejects nonfinite numbers, including `NaN`,
`Infinity` and decimal exponents that overflow a finite REAL.
