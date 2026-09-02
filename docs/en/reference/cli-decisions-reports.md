# Decision, release, and report commands

## evaluate

```bash
operon evaluate [--profile NAME] [--entity-type TYPE] [--entity-id ID]
```

- The default profile comes from `qc.default_profile` in `project.yaml`.
- `--entity-id` requires `--entity-type`.
- Stores a SHA-256 snapshot of the profile and appends a decision; the state machine is updated according to the outcome.
- Rules support dynamic threshold selection through `value_by.metric + value_by.values`, with `unknown` defining the policy for unknown selectors (`warning`/`fail`/`ignore`; by default a missing threshold means `NOT_EVALUATED`). `source.qc_stage` can bind a rule to a specific QC/analysis source.

## curate

```bash
operon curate \
  --entity-type TYPE --entity-id ID --profile NAME \
  --decision DECISION --reviewer REVIEWER --reason REASON [--evidence TEXT]
```

Updates the `curated_*` fields of the latest decision for that entity/profile and records the change in the `changes` audit table.

## release

```bash
operon release --version VERSION --profile NAME \
  [--link {copy|hardlink}] [--copy-files]
```

- The default is `copy`, producing a release that shares no inodes with raw/standardized.
- `--copy-files` is a compatibility alias for `--link copy`.
- An existing version directory is never recreated.
- Only files whose `current_decisions` are PASS, PASS_WITH_WARNINGS, or ACCEPT_WITH_WARNING are included; all other entities are written to `exclusions.tsv`.
- The release metadata snapshot contains `data_sources.tsv` and `source_links.tsv`, freezing sources, citations, licenses, and object associations, and is covered by the release checksum/provenance.

## export

```bash
operon export --output DIR \
  [--entity-type TYPE] [--entity-id ID ...] [--file-id FIL_... ...] \
  [--file-role ROLE] [--format FMT] [--state STATE] \
  [--decision DECISION --profile NAME] \
  [--link {copy,hardlink,symlink}] [--no-qc]
```

Materializes database entities into a directory by file identity for consumption by external analysis tools.

- `--output` is required; the directory must not exist or must be empty.
- At least one selection criterion is required: `--entity-type`, `--entity-id` (repeatable), `--file-id` (repeatable), `--file-role`, `--format`, `--state`, or `--decision`.
- `--decision` requires `--profile` and matches the effective decision under that profile in `current_decisions` (e.g. PASS, FAIL), case-insensitively.
- Effectively retired entities are always excluded.
- `--link` defaults to `copy`; a failed `hardlink` falls back to `copy`.
- The layout is `data/<entity_type>/<entity_id>/<filename>`; before materialization the source file SHA-256 is checked against the manifest, and any mismatch is refused.
- Artifacts:
  - `manifest.tsv` with columns `file_id`, `entity_type`, `entity_id`, `file_role`, `format`, `compression`, `export_relative_path`, `original_relative_path`, `source_url`, `size_bytes`, `sha256`; `sha256` is recomputed over the materialized bytes;
  - `qc.tsv`: a QC long-table snapshot of the exported entities, written by default; skipped with `--no-qc`;
  - `checksums.sha256`: checksums of the exported bytes;
  - `provenance.json`: records all selection criteria, `created_at`, `file_count`, the `operon` version, `link_kind`, and the manifest SHA-256.
- Every export writes one `workflow_runs` row (step `export`, `output_sha256` set to the manifest hash, `execution_details` containing the selection criteria).
- Semantically complementary to release: release targets publication (QC-gated, immutable snapshot), while export targets analysis inputs (arbitrary selection criteria, materialized on demand).

## run-pipeline

```bash
operon run-pipeline \
  --source FILE --entity-type {run|assembly|annotation} --entity-id ID \
  --role ROLE [--format FMT] [--compression C] [--source-url URL] \
  [--profile NAME]
```

Runs `ingest -> standardize -> qc -> evaluate` in order. Any stage failure returns a non-zero exit code.

## report

```bash
operon report qc [--entity-type TYPE] [--entity-id ID] [--export] [--include-retired]
operon report decisions [--profile NAME] [--include-retired]
operon report analysis [--analysis NAME] [--entity-type TYPE] [--entity-id ID] \
  [--hits] [--limit N] [--include-retired]
operon report coverage --reference-set NAME@TAXONOMY_VERSION [--scope metadata]
operon report coverage --reference-set NAME@TAXONOMY_VERSION --release VERSION
operon report metadata [--output DIRECTORY] [--include-retired]
```

- `qc`: prints the QC long table; `--export` additionally writes `qc/aggregate/qc_results.tsv` and `qc_results.wide.tsv`.
- `decisions`: shows `current_decisions` (the latest decision per entity/profile).
- `analysis`: shows the analysis summaries synced to the database; `--hits` shows top hits instead, with `--limit` defaulting to 20.
- `coverage`: computes family/genus coverage only against the named frozen taxonomy reference set. The default `--scope metadata` audits the current `organisms`; `--release VERSION` instead counts the published dataset along `release_members` and the frozen in-release metadata, re-verifying the metadata SHA-256 saved at creation time. The two options are mutually exclusive.
- Coverage reports are written to `reports/coverage/COV_<input-hash>/`, including numerator/denominator, complete targets, missing lists, included/excluded observations, and provenance. Identical input is verified and the existing report reused.
- `metadata`: exports a read-only TSV snapshot of `organisms/samples/runs/assemblies/annotations/accessions/files` plus the normalized sources `data_sources/source_links` from the current SQLite database, together with a `manifest.json` containing row counts and SHA-256 values; written to `reports/metadata/` by default. It is a derived report, not a backup, and cannot overwrite the database in reverse.
- `qc`, `decisions`, `analysis`, and `metadata` exclude effectively retired entities by default; use `--include-retired` explicitly when auditing the full history. Metadata-scope coverage likewise counts only active organisms by default; an existing release keeps the scope frozen at creation time and is not affected by later retirements.

A coverage run returns 0 when it succeeds and meets every threshold in the profile; it returns 1 when the report is generated but at least one rank misses its threshold. Thresholds are never hard-coded in the command or in code.

## query

```bash
operon query "SQL"
```

Read-only SQL. SELECT and read-only PRAGMA statements (such as `table_info` and `foreign_key_list`) are allowed; DML/DDL/write PRAGMA/ATTACH/VACUUM and similar statements are rejected.

## show

```bash
operon show ORG_000001
operon show LAB:HX-ROOT
operon show GCF_000001405.40 --json
operon show GCF_000001405.40 --scope organism
operon show ANN_000001 --include-superseded
operon show ASM_000001 --include-retired
```

Resolves an internal stable ID, a bare accession, or a `NAMESPACE:ACCESSION`. The default `--scope matched` shows the matched entity's upstream lineage and its own downstream subtree, so that querying one assembly does not fold in the counts of other samples/assemblies under the same organism:

- organism: shows all descendants of that organism;
- sample: shows the organism, the sample, and its runs, assemblies, and annotations;
- run: shows the organism, the owning sample, and the run;
- assembly: shows the organism, the owning sample, the assembly, and its annotations;
- annotation: shows the organism, the owning sample, the owning assembly, and the annotation.

Use `--scope organism` for the legacy full-organism relationship view. Descendants logically replaced through `entity_supersessions` are not counted in section totals or file sets by default; the output still lists the relevant `Supersessions` so hidden history remains explainable. `--include-superseded` explicitly restores the full historical view; when you query a superseded entity directly, the matched entity itself is still shown.

Effectively retired descendants are likewise excluded from section totals and file sets by default; the `Retirements` section states which direct retirement root isolates them. `--include-retired` restores the full historical view. When you query a retired target directly, the target and its subtree are still shown, so retirement never removes the audit entry point.

A bare accession that matches multiple entities is rejected with a request to use the namespaced form. `--json` emits the complete machine-readable object, including the `scope`, `include_superseded`, `include_retired`, `supersessions`, and `retirements` fields. `show` uses a read-only SQLite connection, so it is safe against read-only mounts or read-only database copies. If a non-empty `operon.sqlite-wal` still exists on read-only media, the command refuses the immutable fallback and asks you to checkpoint on a writable mount first, rather than ignoring unmerged transactions and showing stale data.
