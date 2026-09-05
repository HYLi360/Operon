# Implicit Behaviors, Edge Cases, and Known Issues

This page corresponds to `operon` 0.6.2 (database schema 2.9, metadata schema 1.4). It documents behavior that is observable in the code but not stated on the task- or architecture-facing pages: implicit semantics, edge cases, and known issues. Items are described as they behave today, with the implementing module named so you can verify each claim. Nothing on this page is a usage recommendation; for the supported workflows see the [guides](../guides/index.md) and [troubleshooting](../guides/troubleshooting.md).

Each item is classified:

- **Intended but implicit** — deliberate design whose consequences can still surprise (for example, what a state transition does to audit rows).
- **Limitation** — a capability boundary or robustness gap that is accepted for now.
- **Known issue** — a defect or data-semantics surprise reported here instead of being silently left undocumented. Known issues are not yet fixed; each lists a workaround where one exists.

## Known issues at a glance

| # | Area | Issue | Workaround |
|---|---|---|---|
| K1 | Decisions | Re-running `evaluate` appends a new decision row whose `curated_decision` is empty, so `current_decisions` (latest row per entity/profile) silently drops a previous manual `curate` override | After re-evaluating, re-run `curate` for any entity whose manual decision must stand; or avoid re-running `evaluate` for that profile |
| K2 | QC state | For a multi-file entity (e.g. annotation with GFF3 + protein FASTA + assembly FASTA), each file's QC outcome overwrites the entity state; the last processed file wins, so an early failure can end in `QC_COMPLETE` with failing metrics stored | Inspect `operon report qc` rather than entity state alone; re-run `operon qc` for the failing file so its outcome is last |
| K3 | Standardize | `operon standardize` always exits 0; per-file errors are only visible in its per-file output lines | Check the per-file output, or confirm expected `standardized/` targets exist |
| K4 | Release | A failed `release` run leaves the partial release directory on disk, and the existing directory blocks any retry (`FileExistsError`) | Delete the partial directory manually after confirming nothing consumed it, then re-run |
| K5 | Release | Member entities are set to `RELEASED` after the release commit in a loop outside any transaction, bypassing the transition machine; a crash mid-loop leaves some entities `RELEASED` and others not, with no audit row | Compare `operon status` with the release manifest; fix stragglers with an audited `set-state --force` |
| K6 | Export | A failed `export` run leaves a partial export directory, which blocks re-running into the same directory | Delete the partial directory, then re-run |
| K7 | Table import | `import table` overwrite sets `entity_state` to `METADATA_VALIDATED` directly (no transition check), demoting `QC_COMPLETE` or `RELEASED` entities | Re-run the affected QC/evaluate steps after patching metadata; keep metadata patches to entities early in their lifecycle |
| K8 | Ingest (`move` across filesystems) | With `move=True` across filesystems, the fallback copies bytes, removes the source, then verifies; if verification fails the target is deleted but the source is already gone | Verify before moving; keep the original until `verify` passes on the ingested copy |
| K9 | Utilities (cosmetic) | `format_table` with zero rows prints the header twice | Ignore; no data is implied |
| K10 | Import wizard (cosmetic) | The "Record an annotation release?" prompt defaults to yes regardless of the draft's content | Answer explicitly at the prompt; the summary review still shows the final plan |

## Identity, archiving, and the filesystem

- **Raw immutability is absolute, even after local loss.** Re-ingesting different bytes for the same entity + role raises `ConflictError` even when the previously archived file is missing or corrupted locally (`files.py`, ingest). There is no in-place replacement path; archive a new entity version or use an audited repair flow.
- **Untracked leftovers are quarantined, not deleted.** When the canonical target path inside `raw/` is occupied by bytes the manifest does not know, the occupant is moved to `<name>.orphan-<sha12-prefix>` in the same directory (`files.py`). `.orphan-*` files can appear inside the otherwise immutable archive; they are not manifest members.
- **Compression detection is asymmetric.** A file *named* `.gz` whose magic bytes are not gzip is rejected, but a plain-named file whose content is gzip is silently recorded as `compression=gzip` (`files.py`, `detect_compression`).
- **Format detection reads extensions, not content.** At most one `.gz` suffix is stripped; `.fasta.bz2` or other unknown extensions yield format `other` (`files.py`, `detect_format`).
- **`standardize --link-kind hardlink` falls back to a copy for directory artifacts** — only single files can be hardlinked (`files.py`, `standardize_file`).
- **`verify` always computes full SHA-256; `qc` uses the stat-fingerprint cache.** A `touch` or copy invalidates the cache and forces a rehash in QC, but `operon verify` never consults the cache (`files.py`, `verify_local_file_identity` vs `verify_files`).
- **`REMOTE_UNVERIFIED` is an output-only status.** It is printed by `verify` when a remote is unreachable, never persisted, and does not write an audit row — but it does drive exit code 1 (`files.py`). A network hiccup is therefore not misclassified as data loss, yet it does fail the command.
- **`verify` only live-checks remotes for files whose local bytes are absent.** When local bytes exist, remote drift is not detected (`files.py`).
- **`operon` commands run against the nearest enclosing project.** `Project.find` walks up parent directories looking for `project.yaml`, so a command in a subdirectory silently operates on the enclosing project (`config.py`).
- **Directory identity excludes timestamps and ownership.** `sha256_directory` covers relative paths, empty directories, file bytes, sizes, and symlink targets; a FIFO/socket inside a directory makes ingest/verify of that tree fail with `OSError` (`utils.py`).
- **Interrupted copies leave hidden temp directories.** `atomic_copytree` works in a sibling `.target.XXXX` temporary directory; a crash mid-copy leaves it behind for manual cleanup (`utils.py`).
- **Manifest relative paths are POSIX paths.** On Windows, computed relative paths would contain backslashes; remote and export code normalizes `\` to `/`, but the storage contract is POSIX-only (`config.py`, `remotes.py`).

## Database, transactions, and concurrency

- **Concurrent writers are serialized only up to 30 seconds.** The database runs in WAL mode with `busy_timeout=30000` and deferred transactions; two writers both pass the read phase and the loser fails at first write/commit with `database is locked` (exit 1). There is no automatic retry (`database.py`). Related: stable-ID allocation (`next_id`) scans for max+1 and is racy across processes — concurrent ingests can attempt the same ID and the loser dies with a PRIMARY KEY error.
- **Migrations run on every writable open, not only on `operon migrate`.** Opening a project with an older schema applies pending additive migrations as a side effect of any command (`database.py`). Migrations are additive (new columns/tables); there is no destructive migration and no data backfill, except the pre-1.0 rebuilds (see [database compatibility](../operations/database-compatibility.md)).
- **Read-only access requires an empty WAL.** A read-only mount can only be opened when the `-wal` file is empty; otherwise the open fails with instructions to checkpoint on a writable host (`database.py`).
- **`operon query` rejects more than writes.** A SQL authorizer denies DML/DDL/ATTACH/SAVEPOINT and allows only a whitelist of PRAGMAs, so `PRAGMA journal_mode` or `VACUUM` fail with *not authorized* even though they are not writes to tables (`database.py`).
- **Setting the current state is a silent no-op.** `set_state` writes the state and an audit row only when the state actually changes; an equal state produces no audit row (`workflow.py`).
- **Bulk state writes bypass the transition table.** QC/evaluate loops use `set_state_bulk`, which forces every transition; the audit reason is the only trace (`workflow.py`).

## Timestamps, logs, and ordering

- **Timestamps are local time with offset, not UTC.** `now_iso()` emits e.g. `2026-09-05T14:30:00+08:00`. Most timestamp-ordered logic compares these strings lexicographically, which is only safe because all writers share the same clock/offset; only `workflow list` compensates via `julianday()` (`utils.py`, `workflow.py`). Cross-timezone collaborators may see misleading orderings.
- **`logs/workflow.jsonl` and the database can disagree.** JSONL run records are appended without inter-process locking and flushed after the DB commit; a rolled-back transaction discards its buffered JSONL records, and two concurrent processes can interleave partial lines (`utils.py`, `workflow.py`).
- **Run input identity embeds absolute paths.** `workflow_runs.input_sha256` hashes the list of `path:sha256` lines; the same content at a different path (moved project) yields a different input identity (`workflow.py`).
- **Output limits are implicit.** `workflow list` defaults to 50 rows (`--limit 0` = unlimited, `--to` is exclusive); `report analysis` defaults to 20 rows with no truncation hint; empty results print literal sentinels such as `(no QC results)` (`cli.py`).
- **Executor environment probes fail silently.** If environment capture fails, the run completes with no `environment_id` and no warning (`workflow.py`).

## Metrics and decisions

- **A missing metric never fails a gate.** A required rule whose metric has no value evaluates to `NOT_EVALUATED` and the entity lands in `QC_COMPLETE` (not `QC_FAILED`). Entities whose formats have no parser (e.g. BAM, directories) stay `QC_COMPLETE` and are excluded from releases only by never reaching `PASS` (`rules.py`).
- **`evaluate` only sees entities that have QC results.** Entities never processed by `qc` receive no decision and do not appear in release exclusions (`rules.py`).
- **Multi-file entities mix metrics from different files.** `latest_metrics` partitions by input identity: conservative booleans (`file_exists`, `sha256_match`, `parseable`, `paired_read_count_match`) take the minimum across all inputs, but every other metric takes the most recently evaluated input's value — a decision can combine counts from one file with booleans from another (`database.py`).
- **Changing QC sampling parameters accumulates rows.** QC results upsert on the parameter set; re-running `qc` with a different `--sample-size`/`--phred-offset` adds a parallel set of rows rather than replacing, and the newest silently wins in `latest_metrics` (`database.py`).
- **Threshold semantics are inclusive and string-based for sets.** `>=`/`<=` are inclusive, `between` is inclusive on both ends, `in`/`not_in` compare metric values as strings, and a rule without `operator` always passes (`rules.py`). Profile-shape gaps (missing `min`/`max`/`values`) behave as described in the [QC profiles guide](../guides/qc-profiles.md).
- **`curate` accepts any decision string.** Values are uppercased but not validated against the decision enum; an unrecognized value stores verbatim and maps the entity to `QC_COMPLETE` (`rules.py`).
- **Every `.yaml` in `config/profiles/` must parse.** A scratch or partially edited YAML in that directory breaks every `evaluate`/`analyze` command that loads profiles (`profiles.py`).
- **Manual overrides do not survive re-evaluation.** See known issue K1 above.

## Built-in QC parsing

- **Whitespace inside FASTA sequence lines is counted as invalid bases.** Lines are stripped at the ends but interior spaces remain and inflate `invalid_base_count`; sequence data must be ASCII (`qc_module/parsers.py`).
- **The seqid is the first whitespace token of the header.** Duplicate detection separately counts full headers and seqids (`qc_module/parsers.py`).
- **Protein internal-stop counting forgives a terminal `*`.** A trailing stop codon is subtracted (clamped at 0); `missing_start` requires the first residue to be `M` (`qc_module/parsers.py`).
- **FASTQ duplication is measured on the first N reads only.** `duplicate_sampling_strategy=first_n` with the default sample of 1,000,000 reads; duplicates clustered late in a much larger file are invisible (`qc_module/parsers.py`).
- **Phred `auto` assumes 33 when ranges overlap.** Overlapping Sanger/Illumina quality ranges are resolved silently; the uncertainty is only visible via `quality_encoding=ambiguous_assumed_phred33`. Quality characters outside ASCII 33–126 abort QC (`qc_module/parsers.py`).
- **GFF3 tolerates several irregularities.** Rows with ≠9 tab-separated fields count as `coordinate_error_count` and are skipped; content after `##FASTA` is ignored; the CDS multiple-of-3 check uses coordinates only and ignores the phase column; Parent integrity is checked against the whole file, so forward references pass (`qc_module/parsers.py`).
- **`parseable` exists only for formats with a parser.** FASTA/FASTQ/GFF3 record it; other formats leave the `parseable == 1` gates permanently `NOT_EVALUATED` (`qc_module/__init__.py`).
- **Paired-read matching is silently skipped** when the sibling FASTQ has no manifest row or is not on disk — no metric and no warning (`qc_module/__init__.py`).
- **A corrupt FASTA-length cache is silently rebuilt.** Digest/count mismatch deletes and regenerates the cache (`qc_module/__init__.py`).
- **Cython and pure-Python parsers have zero tolerated differences.** Metrics and error message strings must match byte-identically; this is enforced by `tests/regression/test_cython_parser_parity.py`.
- **Entity state reflects the last QC'd file, not the worst result.** See known issue K2 above.

## External analyses

- **Reference-database identity is (path, size, mtime), not content.** Without an explicit `database_checksum`, touching or copying a BLAST database changes the identity (spurious cache misses) while an in-place edit preserving size+mtime reuses stale cache (`tools.py`).
- **Tool-version probing has a coarse fallback.** If the version regex misses, the first version-looking token — or the entire first output line truncated to 200 characters — becomes `tool_version`; raw probe output is truncated to 4000 characters in provenance (`tools.py`).
- **BLAST tabular parsing drops silently.** Lines whose field count differs from `result_columns` are skipped without error, and `max_hits_per_query` (default 5) truncates stored hits per query — `analysis_results` summaries can under-count (`tools.py`).
- **Result parsing is not atomic.** Hits and results are written in separate transactions; a crash mid-parse leaves partial hits for a job that is then marked failed (`tools.py`).
- **Empty tool output is a failure.** A run is `completed` only when the exit code is 0 and every expected output exists and is non-empty; a legitimately empty 0-byte TSV fails the run (`workflow.py`).
- **BUSCO auto-lineage rejects paths containing `fasta`.** This is a deliberate guard against the SEPP path-rewriting bug; see the [recipe examples](recipe-parsers-examples.md) (`tools.py`).
- **Stale cache entries self-heal.** A cached job whose output was deleted or modified is marked `superseded` and re-run; leftover stale output is removed first (`tools.py`).
- **Dry runs against non-local backends cannot probe versions.** They use a placeholder tool version, so the printed cache verdict may differ from a real run (`tools.py`).
- **Leftover `RUNNING` jobs are swept at startup.** Every non-dry `analyze` first re-marks jobs left `RUNNING` by a killed process as `interrupted` (`tools.py`).
- **`analyze --limit N` takes the first N files in `file_id` order** — a batch-size control, not a fairness guarantee (`tools.py`).
- **Interrupted external commands leave no run row.** `run-external`/`analyze` log their `workflow_runs` row at the end; a `KeyboardInterrupt`/`ShutdownRequested` mid-run leaves only stdout/stderr logs and (for analyses) an `interrupted` `analysis_jobs` row (`workflow.py`).

## Execution backends

- **Local CPU time can over-count.** `cpu_seconds` is a delta of `getrusage(RUSAGE_CHILDREN)`, which includes any other children reaped concurrently by the same process; Windows has no `resource` module, so `cpu_seconds` is `None` there (`execution.py`).
- **The Slurm payload runs only if `cd` succeeds.** The generated batch script guards the payload with the `cd` exit code and always writes the exit-code file (`execution.py`).
- **`sacct` memory parsing treats bare numbers as bytes.** Slurm prints small exact values without suffixes, so `MaxRSS=123` is parsed as a tiny MB value (`execution.py`).
- **SSH timeouts may leave the remote process running.** The payload runs under `setsid --wait` with a `/tmp` pidfile; on timeout the process group receives SIGTERM then SIGKILL, but a missing pidfile means the error can only warn that the remote process may still be running (`execution.py`).
- **Output pull is asymmetric.** A remote output that does not exist is skipped (the expected-output check reports it), but a local output with different content raises `ConflictError` (`execution.py`).
- **Path rewriting is lexical.** Remote execution maps only absolute paths lexically inside the project root; symlinked arguments are resolved first, and a path that is lexically inside but symlinked elsewhere raises (`execution.py`).

## Remote mirrors

- **A stale manifest lock blocks all locked operations until removed by hand.** The lock is an atomic remote `mkdir` with no expiry; a crashed push deliberately leaves `.operon-manifest.lock` behind, and the error message names the exact path (`remotes.py`).
- **A failed manifest publication leaves unindexed remote files.** If per-file uploads succeeded but the final manifest write failed, remote objects stay on the server unindexed while the local batch reports error; the next push finds the identical bytes and records them as `indexed` (`remotes.py`).
- **A remote manifest without a `project_id` is silently adopted.** Pointing a remote at an empty or unmanaged directory claims it without confirmation (`remotes.py`).
- **`pull` without `--file-id` requires the entries to exist locally.** It iterates the remote manifest and raises `ConflictError` for any entry absent from the local database; remotes are mirrors of an existing manifest, not standalone restores into an empty project (`remotes.py`).
- **Eviction checks one named remote.** `evict` sets `REMOTE_ONLY` based on a single remote; other configured remotes may lack the file, and `verify` accepts any single verified remote as sufficient (`remotes.py`, `files.py`).
- **`sftp://` ingest has no integrity anchor.** There is no expected-hash or host-key pinning option; correctness relies on the ingest-time hashing of the received bytes (`remotes.py`).
- **Directory artifacts stream the whole tree per check.** `matches()` on a directory walks every file over SFTP, so push/pull/evict of directory artifacts costs O(tree size) per check (`remotes.py`).
- **The manifest-lock wait equals `connect_timeout`.** The per-remote `connect_timeout` (default 30 s) also bounds how long a push/pull waits for the lock (`remotes.py`).

## Releases and exports

- **`checksums.sha256` covers the data files only.** Metadata TSVs and `manifest.tsv` are hashed into `provenance.json` and the DB summary but not into `checksums.sha256`, so `sha256sum -c` verifies a subset of a release (`release.py`).
- **Release exclusions only include entities that have decisions.** Entities never QC'd/evaluated appear neither as members nor in `exclusions.tsv` (`release.py`).
- **`--link hardlink` shares inodes with `raw/`.** The default is copy, and the docs' immutability reasoning assumes copies; choosing hardlinks re-introduces inode sharing between the release and the raw archive (`release.py`).
- **An export selecting zero files succeeds.** Exit 0 with an empty `manifest.tsv` and an empty `checksums.sha256` (`export.py`).
- **Export symlinks store fully resolved targets.** `--link symlink` points at `source.resolve()`; moving the project breaks the links, although checksum verification through them still works (`export.py`).
- **Interrupted release/export runs leave blocking partial directories.** See known issues K4 and K6 above.

## Lifecycle and identity resolution

- **Retire is idempotent; restore is root-only.** Retiring an already-retired entity reports `changed: False`. An entity that inherited retirement from an ancestor cannot be restored alone — restore the retirement root; a direct child retirement survives a parent restore (`lifecycle.py`).
- **Retirement is purely logical.** `physical_changes` are hard-coded zeros: no metadata rows are deleted, no bytes are moved, and historical releases are untouched (`lifecycle.py`).
- **Bare accessions are matched case-sensitively.** Internal IDs (`ASM_000001`) are case-insensitive and uppercased, but a bare accession must match its stored case; an accession mapping to several entities requires `NAMESPACE:ACCESSION` (`entity_view.py`).
- **`show` refuses retired entities by default.** It errors on a retired match unless `--include-retired` is given, and silently hides retired/superseded descendants otherwise (`entity_view.py`).

## Metadata, table import, and taxonomy

- **The literals "na", "n/a", "null", "none" become NULL.** In any string field, case-insensitively, unless the field's `allowed` list contains that exact token — a strain literally named "None" or isolate "NA" is silently dropped (`schema.py`).
- **`id`-type fields are uppercased; `allowed` values are canonicalized.** Input is matched case-insensitively and stored in the schema's spelling (`schema.py`).
- **TSV comments are structural.** Any line whose first non-space character is `#` is skipped, including data rows; the header must be the first non-comment line; a ragged row is an error except for one missing trailing empty column when the header ends with an empty name. Files with a BOM are handled (`schema.py`).
- **Duplicate-key detection can be incomplete.** Once any earlier row in a table import produced a field error, duplicate-PK/unique checks are skipped for later rows in the same batch, so the failure list may be shorter than reality (`schema.py`).
- **Date validation follows the Python version.** `datetime.fromisoformat` on Python 3.11+ accepts loose formats such as `20240115`; on 3.10 the same value errors (`schema.py`).
- **XLSX reads only the first worksheet** in workbook order, whatever its name; other sheets are ignored. Excel serial dates with a fractional part become datetimes (`table_import.py`).
- **A blank cell clears an existing value on update.** The preview diff shows it, but omitted columns retain current values; `--on-conflict error` refuses to run if any row would change. The whole apply is one transaction (`table_import.py`).
- **Metadata patches demote entity state.** See known issue K7 above.
- **Intra-file forward references are not resolvable.** Reference validation cannot resolve an ID that appears later in the same file; import organisms before samples before runs/assemblies, in separate runs (`table_import.py`).
- **Taxdump snapshots have no extinction data.** `is_extinct` is stored as NULL for taxdump imports, so a coverage profile with `exclude_extinct: true` fails loudly against them; only NCBI Datasets JSONL sources carry the field (`taxonomy.py`).
- **A failed taxonomy import leaves the source copy on disk.** The source is copied into `raw/metadata/ncbi_taxonomy/` before the import transaction; on failure the transaction rolls back but the copied file remains unregistered (`taxonomy.py`).
- **Taxonomy versions are immutable.** Re-importing the same version with different bytes raises `ConflictError`; parent/alias referential integrity is enforced at import time, so incomplete taxdumps are rejected (`taxonomy.py`).
- **Coverage percentages are rounded before comparison.** Values are rounded half-up to 4 decimals before the inclusive `>=` threshold comparison, so 79.99996 % rounds to 80.0000 % and passes an 80 % threshold (`coverage.py`).
- **Coverage only supports NCBI taxonomy.** Observations with a taxonomy source other than `NCBI` are excluded as `UNSUPPORTED_TAXONOMY_SOURCE`, and a reused cached report whose decision is FAIL surfaces exit code 1 (`coverage.py`).

## NCBI Datasets adapter

- **Imports can add or update but never clear fields.** Empty values in the source do not overwrite existing non-empty values (`adapters/ncbi_datasets.py`).
- **Some normalization is silent.** Unknown sex values become `unknown`; unparseable dates and out-of-range latitude/longitude become NULL — no errors or reason codes (`adapters/ncbi_datasets.py`).
- **Only `GCA_`/`GCF_` accessions are accepted** (optionally versioned, uppercased); SRA runs and other identifier schemes are hard validation errors (`adapters/ncbi_datasets.py`).
- **Partial download failures keep committed batches.** When some batches fail but others import, the run raises a validation error after the successful batches are committed; a rerun skips the completed batches idempotently (`adapters/ncbi_datasets.py`).
- **The pre-2.6 annotation bridge is conservative.** It reuses an existing annotation row only for the assembly's canonical accession and only when the row is not already claimed by a different accession — a workaround for paired GCA/GCF packages with identical annotation metadata but different GFF bytes (`adapters/ncbi_datasets.py`).
- **Disk-space preflight reserves a fixed 64 MiB** and stages downloads under the project root, never `/tmp` (`adapters/ncbi_datasets.py`).
- **The adapter auto-upgrades old metadata schemas.** Opening a pre-1.4 `config/schemas.yaml` upgrades it in place, normalizing formatting and dropping hand-written comments (the same normalization the first remote-only eviction applies; see the [remote storage guide](../guides/remote-storage.md)) (`adapters/ncbi_datasets.py`).

## Backup and the import wizard

- **`backup create` snapshots SQLite via the backup API.** The snapshot is consistent even while other connections write, and no WAL files are copied (`backup.py`).
- **Scope `results` cannot restore your data.** It adds QC/analysis/reports/taxonomy/releases but excludes `raw/` and `standardized/`; only `full` includes data bytes. The destination must be outside the project root and must not exist (`backup.py`).
- **`backup verify` rejects unexpected extra files**, not only missing or changed ones (`backup.py`).
- **The wizard holds the write lock while hashing and copying.** Ingest happens inside one large transaction; on failure the database rolls back, buffered JSONL records are discarded, and only newly created `raw/` targets are deleted — pre-existing targets stay (`import_wizard.py`).
- **The wizard needs a TTY and accepts only regular files** (no directory artifacts). If another process consumed a shown ID in the meantime, preflight raises and the wizard must be restarted (`import_wizard.py`).
- **The demo registers proteins before their GFF3** so annotation QC sees complete annotation releases; file insertion order matters for multi-file entities (`demo.py`).

## Environment and shutdown

- **Local and remote environment documents differ by construction.** Local captures include the Python and `operon` versions; remote probes cannot report them, so the same machine can produce different `environment_id`s for local vs SSH execution (`environment.py`).
- **A second signal skips cleanup.** The first SIGINT/SIGTERM triggers graceful shutdown (exit code 130); a second signal during cleanup calls `os._exit(128+signum)` immediately. `graceful_shutdown` is a no-op outside the main thread (`shutdown.py`).

## CLI conventions

- **Exit codes:** 0 success; 1 runtime/SQLite/OSError (including `FileExistsError` from release/export) and any per-item failures in `qc`/`verify`/`analyze`/`push`/`pull`/`evict`/`backup verify`/`report coverage`; 2 for every `OperonError` (validation, conflict, checksum, remote, configuration); 130 for the first interrupt (with the message that progress was saved and the command can be re-run — true only for the resumable NCBI adapter and analysis paths); `128+signum` for a second signal (`cli.py`, `shutdown.py`).
- **`operon verify` exits 1 when any file is not `CHECKSUM_VERIFIED` or `REMOTE_ONLY`**, including transient `REMOTE_UNVERIFIED` (`cli.py`).

## Deferred to the 1.0 release

Several development-era compatibility shims exist only for databases created before 1.0 and are scheduled for removal at 1.0 (marked `TODO(1.0)` in the code):

- The pre-1.0 migration call and `_migrate_pre_1_0_schema()` rebuild in `database.py` (legacy rows keep `legacy:` input identities and never dedupe against new QC rows).
- The adapter's defensive field projection for legacy schemas and its automatic metadata-schema upgrade in `adapters/ncbi_datasets.py`.

See [Database compatibility](../operations/database-compatibility.md) for the full inventory and the removal policy.
