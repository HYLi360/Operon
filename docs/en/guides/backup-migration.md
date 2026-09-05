# Backup, Migration, and Resumption

## Back up and migrate a project

Use `backup` to create a consistent SQLite snapshot. Do not copy database files directly while the database may be active. The `--output` directory must be outside the project root and must not exist yet; `backup create` refuses otherwise:

```bash
# Configuration, SQLite, audit records, and workflow logs
operon backup create --output /backups/my-project-control --scope control

# Adds QC, analysis, reports, taxonomy, and releases
operon backup create --output /backups/my-project-results --scope results

# Adds raw, standardized files, local placeholders, and all project-managed data
operon backup create --output /backups/my-project-full --scope full

operon backup verify --input /backups/my-project-full
```

Note the scope boundaries: `results` excludes `raw/` and `standardized/` (the bytes you usually cannot regenerate), so it is not a restorable substitute for `full`; only `full` can restore data files.

`backup verify` validates an exact snapshot. In addition to checking size and SHA-256 for files listed in the manifest, it rejects extra files in the backup directory. Keep notes, temporary files, and recovery records outside the backup directory.

With `REMOTE_ONLY` files, a local backup must include the SQLite database containing `file_locations`. Back up the remote mirror root independently, including `operon-manifest.json` and actual objects. Placeholder files are not recovery evidence. Safe hydration requires both local `files` identity and the remote manifest/bytes.

`report metadata` is not a backup. It exports metadata/manifest TSV files for browsing and exchange, but does not include complete QC, decisions, changes, workflows, remote locations, or migration state.

Create releases regularly and verify them in the release directory:

```bash
# Linux
sha256sum -c checksums.sha256
# macOS
shasum -a 256 -c checksums.sha256
```

Backup policy can be based on reconstruction cost:

| Type | Examples | Policy |
|---|---|---|
| Irreplaceable | Raw FASTQ, original external downloads, manually curated metadata | Multiple checksum-verified immutable copies |
| Expensive to rebuild | Assemblies, annotations, whole-genome alignments | Retain and back up |
| Easy to rebuild | Temporary indexes, intermediate sorted files, caches | Can be cleaned, but keep the generation rules |

This policy assumes that the tool environment and database versions remain reproducible; otherwise "rebuildable" is only theoretical.

Legacy v1 databases do not require manual migration. When opened by the current program, `qc_results` and `decisions` migrate automatically to the v2 structure. Old QC data is retained with a `legacy:` input identity, and old decisions remain available through `current_decisions`.

## Resume failed tasks

Core steps are idempotent:

- Repeating `ingest` for the same file returns the same `FIL_` for identical SHA-256 and does not copy it twice.
- `standardize` skips an existing target with the same checksum.
- `qc` upserts by `input_identity + stage + metric + tool/version/parameter_set` and does not create duplicate rows.
- `evaluate` appends a decision and does not overwrite history.
- `release` rejects an existing version directory rather than overwriting it.
- `taxonomy compile` reuses identical profile/taxonomy/TSV input and rejects different content under the same identity.
- `report coverage` validates and reuses an old report when input membership, profile, and reference-set identity match.
- After Ctrl+C/SIGTERM, `analyze` marks the current job `interrupted` and removes partial outputs (`--keep-partial` preserves them for debugging). On rerun, completed files use the cache; an old result with unchanged input and verified output is adopted (`adopted`); only unfinished work is recomputed.

Rerun the same command to continue from the interruption. Use `status` to inspect each entity's current state.
