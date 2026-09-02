# Release, lifecycle, and correctness guarantees

## Entity retirement and restoration

`retire` is a control-plane state change, not a file operation. It appends a direct `RETIRE` event to `entity_lifecycle_events` and an audit row to `changes`; it does not delete database rows, move files, modify checksums, revoke existing QC/analysis/workflow records, or rewrite already-created releases. Retiring a parent entity effectively retires its ownership descendants in `effective_retired_entities`: an organism covers its samples, runs, assemblies, and annotations; a sample covers its own runs, assemblies, and annotations; an assembly covers its annotations.

`restore` only reverses the target's own most recent direct `RETIRE`, appending a `RESTORE` that points back to the original event/audit row; history is never deleted. A child entity that inherited retirement from an ancestor cannot be restored individually — the root causing the isolation must be restored first. Conversely, if a child has its own direct retirement, it stays retired even when the parent is restored. This keeps the inverse operation strictly paired with the original one and never erases an independent human decision.

Active data consumers exclude effectively retired entities by default: descendant counts in `show`, status/report, batch QC, rule evaluation, external analysis candidates, metadata coverage, NCBI re-import reuse, and new releases. Use the corresponding `--include-retired` when explicitly querying history; `retired` lists current direct and inherited states. Backups, verification, remote residency, read-only SQL, existing releases, and audit history retain the complete archival view.

The current architecture has no `purge`. A retirement plan lists descendants, files, and QC/decision/analysis/workflow/source/remote/release references, with `physical_changes` explicitly zero. If physical removal is added in the future, it must take this auditable state and reference graph as a precondition, with separately defined retention periods, release/remote reference protection, a recoverable window, and irreversible confirmation.

## Release

`release --version <version> --profile <profile>` selects files with PASS/PASS_WITH_WARNINGS/manual ACCEPT_WITH_WARNING from `current_decisions` and generates:

```text
manifest.tsv / decisions.tsv / exclusions.tsv / profile_history.tsv
qc_summary.tsv / provenance.json / checksums.sha256
software_versions.tsv / README.md / metadata table snapshots / data/ member files
```

Releases default to `copy`, guaranteeing no shared inodes with raw/standardized; `--link hardlink` is an explicit space-optimization option. The release scope of coverage reads the metadata frozen here, not the TaxIDs in the current active database; the release summary/provenance stores the SHA-256 of every metadata TSV, and coverage recomputation verifies these identities first — so active metadata modifications after release creation do not rewrite historical coverage, and tampering with snapshots inside the release directory is rejected.

## Key correctness guarantees

- **Read-only queries**: `query` uses an independent read-only SQLite connection plus an authorizer, rejecting DML, DDL, write PRAGMA, ATTACH/VACUUM, and other side-effecting operations.
- **Atomic import**: metadata import completes in a single transaction; failure rolls back entirely.
- **Idempotence**: repeating with identical input neither produces duplicate files nor overwrites correct results; different input is explicitly rejected.
- **Traceability**: provenance is written to both `logs/workflow.jsonl` and `workflow_runs`. Transactional callers such as interactive import write `workflow_runs` within the same SQLite transaction and buffer the JSONL records, appending them only after the transaction finally commits; on transaction failure the uncommitted completion events are discarded and a parent failure event is recorded after rollback, so the log never claims completion of objects that do not actually exist.
- **Frozen denominators**: coverage is computed only against a reference-set TSV carrying a SHA-256; a taxonomy upgrade cannot silently change historical numbers.
- **Automatic migration**: when an old v1 database is opened, `qc_results` and `decisions` are migrated automatically to the v2 structure without losing old data (legacy QC is kept under a `legacy:` identity).
