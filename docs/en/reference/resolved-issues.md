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
| ODR-0004 | Remote mirrors | Upload verification now happens at the staging name inside `put`, before the final rename; a truncated or corrupted upload is removed with the staging bytes and never occupies the final remote path, so a later push retries instead of marking the location `CORRUPT`. |
| ODR-0005 | Release | The release preflight no longer returns early when `config/profiles/<profile>.yaml` is missing: `release` and decision-based `export` load and validate the named QC profile up front, so a typo fails with a validation error instead of publishing a zero-member release or an empty export bundle. |
| ODR-0006 | Release | A crash between the final rename and the `releases` insert no longer wedges the version: the retry recognizes the orphaned tree by its `provenance.json`, removes it, and rebuilds the release; any other occupant of the path still raises `FileExistsError`. |
| ODR-0007 | Export | The export `workflow_runs` row is now committed only after the workspace has been renamed to the final destination, with the command naming that destination; a recording failure removes the published tree, so a completed export whose destination does not exist can no longer be reported. |
| ODR-0008 | Export | Exports no longer use an existing empty destination as the workspace: every export stages in a hidden sibling directory, publishes by `rmdir` + atomic rename (failing safe if the destination gained content), and a failure only ever removes the staging tree — the caller's directory children are never deleted. |
| ODR-0009 | Ingest | An idempotent re-ingest no longer rewrites `files.status` unconditionally: a `STANDARDIZED` file keeps its status, and any real transition goes through `set_file_status` with its `changes` audit row. |
| ODR-0010 | Standardize | `standardize` no longer writes `STANDARDIZED` directly: the file status goes through `set_file_status` and the entity transition through `set_state`, both audited in `changes`; illegal transitions (for example re-standardizing a `RELEASED` entity) are rejected, and `CHECKSUM_FAILED` gained a legal recovery edge to `STANDARDIZED` for re-verified bytes. |
| ODR-0011 | QC / decisions | Re-running `qc` or `evaluate` no longer demotes `ACCEPTED`/`RELEASED` (or `REVIEW`/`REJECTED`) entities: the batch state writes go through `set_state_guarded`, which still records fresh QC evidence and decision rows but leaves the lifecycle state to an explicit `curate` or forced `set-state`. |
| ODR-0012 | Database | The derived-view rebuild on writable open now runs in a single immediate transaction (plain `execute`, because `executescript` would implicitly commit), so two processes can no longer collide between another connection's DROP and CREATE with `view ... already exists`. |
| ODR-0013 | Files / QC | Directory artifacts are now verified with the same deterministic tree hash used at ingest: `verify_local_file_identity` branches on the manifest format, a file↔directory type flip reports missing, and the stat-fingerprint cache is bypassed for directories (a directory's own mtime changes on any member touch). Built-in QC's checksum stage and `fanout` source verification now pass for intact directory trees. |
| ODR-0014 | TimeTree | Snapshot loading and calibration inputs now fail with `ValidationError` instead of bare exceptions: a missing/malformed/incomplete `snapshot.json`, an unreadable raw payload, non-numeric taxon IDs in the taxa/constraints tables, and an unreadable or malformed dating tree all report a contextual `error:` message (exit 2) rather than a traceback. |
| ODR-0015 | Shutdown | Interrupt-cleanup sites now call `shutdown.cleanup_completed()` once their bookkeeping is finalized, re-arming graceful handling: a signal arriving after cleanup (batch unwinding, exit-130 reporting) raises a fresh `ShutdownRequested` instead of force-exiting a process with nothing left to clean. The `os._exit(128+signum)` escape hatch remains for a second signal while cleanup is genuinely still running. |
| ODR-0016 | Analysis tools | The tool-version and database-identity caches are now TTL-bound (300 s) instead of process-lifetime: a batch still pays for one probe, but a long-lived process (the TUI) re-probes after expiry, so an in-place tool upgrade or a reference database replaced at the same path is noticed instead of planning with a stale identity. |
| ODR-0017 | Tests | The version-source test no longer races a concurrent sdist build: it re-reads `operon.__version__` via `importlib.reload()` while holding the machine-wide sdist build lock, so an xdist worker that imported `operon` during a sibling worker's in-place `OperonDBS.egg-info` rewrite cannot freeze a stale or missing version into the assertion. |
| ODR-0018 | Analysis tools | A cancellation raised by the analysis `progress_callback` on the Slurm-array path (the TUI's `AnalysisCancelled`) is no longer swallowed as per-file failures: callback exceptions now abort the batch — tasks that already wrote their exit-code file finalize as completed/failed exactly like the executor-interrupt path, every remaining plan is marked `interrupted`, and the original exception propagates to the caller. KeyboardInterrupt subclasses keep the existing outer interrupt path. |
| ODR-0040 | QC / reports | Text cells that a spreadsheet would execute are escaped with a leading apostrophe — a value beginning with `=`, `+`, `-`, `@`, TAB or CR. This covers `sequence_qc.tsv` from both alignment backends (byte parity kept) and every `write_tsv` consumer (release and export manifests, report TSVs, TimeTree candidate tables). Non-string cells keep their exact bytes, an already-escaped value is not escaped twice, and provenance hashes are computed over the escaped bytes, so re-reading a release stays consistent. |
| ODR-0044 | Export / reports | `write_tsv` now decides cell quoting itself instead of delegating to `csv`: a cell containing TAB, CR, LF or `"` is quoted with its inner quotes doubled, everything else is written verbatim. CPython 3.11 changed the csv rule for CR/LF cells, so on 3.10 the same row produced different bytes and artifacts hashed for provenance (release manifests, export identities) depended on the interpreter. The bytes are now identical on 3.10–3.15 and unchanged from the previous 3.11+ output. |
| ODR-0045 | TUI | A panel load is stamped with a generation: `reload()` hands the worker the generation stamped on the UI thread, and a payload a newer load superseded is dropped instead of rendered. Before, a read already inside a thread could land after a newer one and restore the rows a just-typed filter had removed — the macOS/Python 3.15 CI shape, where the entity filter never latched. |

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
