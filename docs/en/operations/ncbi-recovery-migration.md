# NCBI adapter recovery and migration

This procedure migrates a long-running legacy `operon` database to database schema 2.8 and metadata schema 1.4, and repairs the annotation duplication, GCA/GCF canonical drift, and QC state downgrades the old NCBI Datasets adapter may have left behind. The whole procedure respects these boundaries:

- It does not initialize a project and does not delete old annotations, files, workflows, QC, analyses, releases, or raw files;
- Schema migrations only add columns, tables, and indexes; business repairs are expressed as new repair workflows, logical supersessions, and `changes` compensation records;
- `backup create` opens the source database read-only and never migrates first just to back up;
- The planning phase of `ncbi-reconcile` uses a read-only connection, reading only metadata, SHA-256, and QC/analysis/release references in SQLite — it neither migrates the schema nor opens raw biological content;
- `ncbi-datasets --plan-only` uses a read-only connection and checks whether the local paths recorded in the manifest exist, but it does not migrate the schema, read file contents, download data, or write workflows.

The commands below assume execution from the code repository's `.venv`. Replace the example paths with real ones; the backup directory must lie outside the project directory and must not already exist.

## 1. Stop writes and set paths

First stop all `ncbi-datasets`, `ingest`, QC, analysis, release, and any other processes that may write to SQLite or the project directory. Read-only queries may continue, but ideally nobody operates during the migration window either.

```bash
OPERON_CODE=/path/to/Operon
OPERON_PROJECT=/path/to/operon-project
OPERON_BACKUP=/path/to/backups/operon-pre-2.8
OPERON_STAGE=/path/to/staging/operon-2.8-rehearsal
OPERON_ACTOR=database-maintainer

cd "$OPERON_CODE"
```

Do not point these variables at the project's parent directory, a disk root, or a home root. Later commands do not delete these paths; if a destination already exists, pick a new directory.

## 2. Verify the program to be used

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m operon --version
```

The full test suite must pass. If production runs a cx_Freeze build, rebuild it and complete the rehearsal below in staging with the same binary first.

## 3. Record the pre-migration read-only baseline

`query` opens SQLite read-only and does not trigger schema migration. Save at least the following results; they are the count-conservation acceptance baseline after migration.

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "PRAGMA integrity_check"
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "PRAGMA foreign_key_check"
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "SELECT 'annotations' AS item, COUNT(*) AS n FROM annotations UNION ALL SELECT 'files', COUNT(*) FROM files UNION ALL SELECT 'qc_results', COUNT(*) FROM qc_results UNION ALL SELECT 'analysis_jobs', COUNT(*) FROM analysis_jobs UNION ALL SELECT 'releases', COUNT(*) FROM releases UNION ALL SELECT 'workflow_runs', COUNT(*) FROM workflow_runs"
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "SELECT entity_type, state, COUNT(*) AS n FROM entity_state GROUP BY entity_type, state ORDER BY entity_type, state"
```

The first should return `ok`; the second should return no rows. If not, stop the migration and investigate the pre-existing corruption — do not mix integrity problems with adapter repair in one operation.

## 4. Create and verify the pre-migration backup

A `results` backup contains the consistent SQLite, configuration, logs, QC, analysis, reports, taxonomy, and releases — enough to protect expensive BUSCO/QC results — but does not copy raw. If full disaster recovery is needed and space allows, use `--scope full` instead.

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" backup create \
  --output "$OPERON_BACKUP" --scope results
.venv/bin/python -m operon backup verify --input "$OPERON_BACKUP"
```

The verification result must be `"ok": true`. Keep the backup directory read-only; do not continue running it as a production project.

## 5. Rehearse the control-plane migration on a backup copy

Copy the verified backup as staging. After copying, the `backup-manifest.json` in staging no longer certifies content changed afterwards; the original backup directory stays untouched.

```bash
cp -a "$OPERON_BACKUP" "$OPERON_STAGE"
.venv/bin/python -m operon --project "$OPERON_STAGE" migrate \
  > "$OPERON_STAGE/migrate-result.json"
```

`migrate-result.json` must satisfy:

- `schema_version` is `2.8`;
- `integrity_check` is `ok`;
- `foreign_key_violations` is `0`;
- The migration ledger contains `2.6-recovery-and-ncbi-identities`, `2.7-entity-lifecycle-retirement`, and `2.8-execution-environments` (a database already at a newer version only gains the missing entries).

Then generate the business repair plan:

```bash
.venv/bin/python -m operon --project "$OPERON_STAGE" ncbi-reconcile \
  > "$OPERON_STAGE/ncbi-reconcile-plan.json"
```

Review `warnings`, `annotation_supersessions`, `assembly_updates`, `file_role_updates`, `file_path_repairs`, `accession_primary_updates`, and `state_restorations` item by item. Any `alternate_role_conflict` is a blocker: do not apply, and do not bypass it by hand-editing SQL.

`file_role_updates` and `file_path_repairs` also involve physical file moves: after a role rename (e.g. `assembly_report` → `assembly_report_genbank`), the archived file is moved to the new role's canonical path, with `files.relative_path` updated and a `changes` audit written in the same transaction. If the target path turns out to be occupied by different bytes before the move, the whole plan fails and no file is moved; rows whose files are locally missing (e.g. REMOTE_ONLY) are only recorded, not moved, and their file_ids appear in the result's `skipped_path_moves`. This step is the precondition for later paired downloads to resume: only with the plain canonical path vacated will the canonical-side ingest not collide with old bytes.

Once the plan looks reasonable, apply it in staging:

```bash
.venv/bin/python -m operon --project "$OPERON_STAGE" ncbi-reconcile \
  --apply --actor "$OPERON_ACTOR" \
  > "$OPERON_STAGE/ncbi-reconcile-apply.json"
```

## 6. Accept the staging result

```bash
.venv/bin/python -m operon --project "$OPERON_STAGE" migrate
.venv/bin/python -m operon --project "$OPERON_STAGE" query \
  "SELECT COUNT(*) AS supersessions FROM entity_supersessions"
.venv/bin/python -m operon --project "$OPERON_STAGE" query \
  "SELECT status, COUNT(*) AS n FROM workflow_runs WHERE step='ncbi_datasets_reconcile' GROUP BY status"
.venv/bin/python -m operon --project "$OPERON_STAGE" query \
  "SELECT COUNT(*) AS repair_changes FROM changes WHERE workflow_run_id IN (SELECT run_id FROM workflow_runs WHERE step='ncbi_datasets_reconcile')"
.venv/bin/python -m operon --project "$OPERON_STAGE" query \
  "SELECT COUNT(*) AS annotations FROM annotations UNION ALL SELECT COUNT(*) FROM files UNION ALL SELECT COUNT(*) FROM qc_results UNION ALL SELECT COUNT(*) FROM analysis_jobs UNION ALL SELECT COUNT(*) FROM releases"
.venv/bin/python -m operon --project "$OPERON_STAGE" ncbi-reconcile \
  > "$OPERON_STAGE/ncbi-reconcile-postcheck.json"
```

Acceptance conditions:

- Integrity is still `ok`; foreign-key violations still 0;
- The latest repair workflow is `completed`;
- Total counts of annotations/files/QC/analysis/releases are exactly identical to the pre-migration baseline;
- `entity_supersessions` only gains logical mappings; old `ANN_` and `FIL_` rows still exist;
- Every repaired field in `changes` is linked to the repair workflow;
- The original BUSCO/analysis output directories still exist, and the related `analysis_jobs`/`analysis_results` counts have not decreased;
- The postcheck summary is all zeros; already-superseded rows do not re-enter the repair plan.

A results/control staging has no raw, so do not use `--plan-only` here to judge which downloads production still needs: the path-existence check would correctly treat uncopied raw files as missing. If you need to validate download planning in staging, you must use a full backup or a filesystem snapshot.

## 7. Run the formal migration on the production project

After staging acceptance passes, confirm again that all write processes are stopped. If a long time has passed between rehearsal and the formal migration, take another `results` backup into a new directory and verify it, to cover new results from the interval.

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" migrate \
  > operon-production-migrate.json
.venv/bin/python -m operon --project "$OPERON_PROJECT" ncbi-reconcile \
  > operon-production-reconcile-plan.json
```

The production plan should match the staging plan just completed; at minimum, its summary counts and warnings must match. After confirming:

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" ncbi-reconcile \
  --apply --actor "$OPERON_ACTOR" \
  > operon-production-reconcile-apply.json
```

Repeat the acceptance queries from section 6 and compare against the section 3 baseline. At this point the metadata schema is raised to 1.4, and project custom fields are preserved.

## 8. Preview first, then resume the halted NCBI downloads

On the actual production project, only compute the missing set of the 532 accessions:

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" ncbi-datasets \
  --accession-file "$OPERON_PROJECT/accession.txt" \
  --include genome --include sequence-report \
  --plan-only > operon-ncbi-download-plan.json
```

Key checks:

- `download_plan` may contain only `genome` and `sequence-report` — never gff3/protein/cds;
- Roles that already exist with valid status/path must not be listed again;
- A paired GCA/GCF may belong to different download groups, but no longer contends for the same assembly file role;
- `skipped_existing` lists accessions already fully satisfying this request.

Once the plan is correct, run the real download. To preserve the chain of "standing back up from an old failed run", pass the workflow ID of the halted run:

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" ncbi-datasets \
  --accession-file "$OPERON_PROJECT/accession.txt" \
  --include genome --include sequence-report \
  --resume-run WF_PREVIOUS_INTERRUPTED \
  > operon-ncbi-resume-result.json
```

The new run points back to the old one through `workflow_runs.resumes_run_id`; the old run is not overwritten. Each accession's state and attempts are kept in `adapter_run_items`. On another interruption, re-run the same request with the ID of the latest failed/interrupted run; roles that already succeeded are skipped item by item from the manifest.

During the resume phase, ingest self-heals two kinds of historical residue instead of reporting a checksum conflict:

- A role was renamed but the file still sits at the old canonical path (caused by an early reconcile or manual SQL rename): when the occupying bytes match the manifest row, the file is moved to the canonical path of that row's own role with `files.relative_path` updated, and the new content is then archived normally;
- Orphan files claimed by no manifest row (left by an interrupted run before the shutdown): they are quarantined as `<filename>.orphan-<first 12 chars of sha>` in the same directory, bytes preserved and a `changes` audit written — never silently overwritten.

If the occupying bytes do not match the claiming row's checksum either, `ConflictError` is still raised — this means the archived content itself is untrustworthy and must be checked by hand; do not bypass it by deleting files.

Monitoring queries:

```bash
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "SELECT run_id, resumes_run_id, status, started_at, finished_at, error FROM workflow_runs WHERE step='ncbi_datasets_import' ORDER BY started_at DESC LIMIT 10"
.venv/bin/python -m operon --project "$OPERON_PROJECT" query \
  "SELECT status, COUNT(*) AS n FROM adapter_run_items WHERE run_id='WF_CURRENT' GROUP BY status ORDER BY status"
```

## 9. Rollback and recovery principles

The schema 2.6/2.7 migrations are purely additive, but metadata schema 1.4 and the compensating repairs should still be rolled back as a whole:

1. Immediately stop all write processes;
2. Do not run reverse `DELETE`/`UPDATE` on the original database; the current version has no automatic reverse-repair command;
3. Prefer restoring the pre-migration backup into a new directory and running `backup verify`, the read-only baseline queries, and application-level checks there;
4. Only after the restored copy is confirmed sound, switch the project path within a maintenance window or restore from a reviewed filesystem snapshot;
5. If the interruption happened only during the download phase, do not roll back the database: keep the failed workflow and create the next attempt with `--resume-run` — that is exactly the intended history model.

Do not replace only `operon.sqlite` while keeping the migrated config/logs, and do not restore only the config: the database, configuration, logs, and result indexes must all come from the same backup point in time. Restoring a full backup or an external raw snapshot likewise must end with checksum verification.
