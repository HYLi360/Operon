# Resolved Issues

Historical known issues that were reported against
[Implicit Behaviors, Edge Cases, and Known Issues](behaviors-and-limitations.md)
and are now covered by the current implementation and regression tests.

Two numbering series are in use:

- **K series** — the original numbering from when this list lived inside the
  behaviors-and-limitations page. Kept unchanged for historical continuity;
  no new K numbers are issued.
- **ODR series** — records from the defect registry (`defects.yml` at the
  repository root). Each ODR record carries the report date, affected
  versions, reproduction, disposition, and status, and every fixed record is
  backed by regression tests marked with its ID. See
  [Defect tracking](../contributor/defect-tracking.md) for the process.

New resolved issues are appended to the ODR table.

## ODR series

| # | Area | Resolution |
|---|---|---|
| ODR-0001 | Import wizard | Schema field names are no longer interpolated into INSERT columns: the wizard commit boundary validates and quotes every identifier (`operon.sql.quote_identifier()`), and a hostile schema raises `ValidationError` with the whole commit rolled back. |
| ODR-0002 | Database API | `insert_row()`/`upsert_rows()` and the read helpers `table_columns()`/`export_rows()`/`export_active_rows()` validate and quote every dynamic identifier; values remain parameter-bound, and keyword column names still work. |
| ODR-0003 | Table import | `apply_table_import()` revalidates the table against `IMPORTABLE_TABLES` and quotes all identifiers at the execution boundary; a tampered preview raises `ValidationError` and rolls back. |

## K series (historical)

| # | Area | Resolution |
|---|---|---|
| K1 | Decisions | Re-evaluation appends an automatic row while carrying the current `curated_*` fields forward. The CLI previews all affected curated entities and asks once before any write; non-interactive runs require `--yes`. |
| K2 | QC state | Each file has an aggregate QC status. An entity takes the worst sibling status (`QC_FAILED` > `QC_RUNNING` > `QC_COMPLETE`), and `operon qc` lists every file status. |
| K3 | Standardize | Batch standardization returns exit code 1 when any file fails. |
| K4 | Release | Releases are built in a hidden staging directory and published atomically. Failed builds remove staging output; a database commit failure also removes the published tree. |
| K5 | Release | Release membership, audited `RELEASED` transitions, and the release row commit in one transaction, so a partial state update is rolled back. |
| K6 | Export | Exports are built in a temporary sibling and renamed only after all artifacts are complete; failures clean the temporary tree or restore an existing empty destination. |
| K7 | Table import | Updating existing metadata preserves its lifecycle state and audit history. Release preflight treats changes after evaluation as stale and requires a fresh QC/evaluation. |
| K8 | Ingest (`move`) | Move mode copies and verifies the archive, registers it, and removes the source last. Copy, checksum, or transaction failures leave the source recoverable. |
| K9 | Utilities | Empty tables render one header and separator, without a duplicate data row. |
| K10 | Import wizard | The annotation prompt defaults to the draft's current presence (`yes` only when an annotation is already selected). |
| K11 | Classification | A missing field never satisfies a condition in any form — including inside a `not:` negation and as a disjunct of an `any:` group; `between` on non-numeric operands raises a validation error naming the field and value instead of a bare `ValueError`. |
| K12 | Classification | `classify-sequences` no longer hides what it ignores: superseded (non-latest) completed jobs are counted as `ignored_completed_jobs` and surfaced with a warning, and files skipped for being absent from the `sequences` registry are counted explicitly. |
| K13 | Fan-out | `fanout --dry-run` now performs the full preflight — source checksum and registry freshness, unit identity computation, and conflict/occupancy checks — and raises `ConflictError` exactly as a real run would, while still writing no files and opening no run row; each planned unit is annotated `would_create`/`would_reuse`. Unit seqids are also canonicalized into sorted order before the FASTA is generated, so reordering the assignment TSV no longer changes unit bytes. |
| K14 | Fan-out | An interrupt (Ctrl+C) now finishes the fan-out workflow run as `interrupted` with exit code 130 instead of leaving the row `running`; targets created by the run are still removed. |
| K15 | Recipes | `file_role_prefix` matches at a `:` boundary: `sub` selects the exact role `sub` and every `sub:*`, and no longer captures `sub2:*`. |
| K16 | Execution backends | A signal-killed Slurm job is no longer recorded as success: the `sacct` fallback folds a nonzero signal component of `exit:signal` into a shell-style exit code (an OOM kill's `0:9` becomes 137) and records the signal as `slurm_exit_signal` in the run details, and the exit-code/accounting retries are raised from 5 attempts × 1 s to 10 × 2 s (`_SLURM_EXIT_CODE_RETRIES`/`_SLURM_EXIT_CODE_RETRY_SECONDS`). |
| K17 | Execution backends | The SSH backend no longer deletes remote outputs before a run: an existing remote output is first renamed to a `<path>.operon-prev-<uuid>` backup, the backup is dropped once the run succeeds and the new output verifies, and a failed or interrupted run best-effort restores the backup in place, so a failure no longer destroys the previous remote output. |
| K18 | External analyses | The stale-`RUNNING` sweep is now scoped to the current analysis: a non-dry `analyze` marks only that analysis's `RUNNING` rows `interrupted`, so a concurrent run of a different analysis no longer loses its live jobs (`tools.py`, `_sweep_stale_running_jobs`). |
