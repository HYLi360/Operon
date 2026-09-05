# Database compatibility code inventory

This inventory records the code that exists only to read development-era legacy databases or legacy project schemas, and that is planned for removal at 1.0.

It collects the compatibility code scheduled for retirement at the official 1.0 release. "Removal" here does not cover table creation, indexes, views required by the current schema, or the dynamic-extension capability of `ensure_metadata_columns()`.

## SQLite database migrations

File: `operon/database.py`

| Location | Current purpose | 1.0 handling |
|---|---|---|
| The call to `_migrate_pre_1_0_schema()` in `Database.__init__()` | Checks for development-era legacy structures on every writable database open | Remove the call |
| The assembly-column backfill segment of `Database._migrate_pre_1_0_schema()` | Adds the 5 columns later introduced by the NCBI adapter to old `assemblies` tables | Remove |
| The `qc_results` migration segment of the same method | Migrates v1 tables without `input_identity` to the file-aware structure, keeping old record identities under `legacy:` | Remove |
| The `decisions` migration segment of the same method | Migrates v1 tables without `profile_snapshot_id` to the appendable-history structure | Remove |

`Database._ensure_current_schema_objects()` is not compatibility code. It maintains indexes still needed by the current version, plus the `current_decisions`, `current_entity_lifecycle`, and `effective_retired_entities` views, and must be kept in 1.0.

`Database._migrate_remote_schema_2_2()` is also not part of the "development-era v1 compatibility layer" above. It upgrades a 2.1 database to 2.2 purely additively: adding `executor`, `scheduler_job_id`, and `execution_details` to `workflow_runs`, and creating `file_locations`. It must be kept as long as opening 2.1 projects is supported; if that support ever ends, it should be replaced through the formal database migration policy, not deleted together with `_migrate_pre_1_0_schema()`. The corresponding test is `test_schema_2_2_adds_remote_location_and_executor_provenance`.

`Database._migrate_taxonomy_schema_2_3()` is likewise a purely additive migration needed by current functionality, not part of `_migrate_pre_1_0_schema()`: for 2.2 projects it creates `taxonomy_snapshots`, `taxonomy_nodes`, `taxonomy_aliases`, `taxonomy_reference_sets`, `coverage_reports`, and `coverage_report_metrics` plus related indexes, without modifying existing business rows. It must be kept as long as opening 2.2 projects is supported. The corresponding regression test is `test_schema_2_3_adds_taxonomy_and_coverage_history`.

`Database._migrate_source_schema_2_4()` is another purely additive migration needed by current functionality: for 2.3 projects it creates `data_sources` and `source_links`, storing normalized external databases/repositories, citations, licenses, and their associated objects. Non-INSDC sources must contain both citation and License; source content is deduplicated by SHA-256 identity. It must be kept as long as opening 2.3 projects is supported. The corresponding regression test is `test_schema_2_4_adds_normalized_source_provenance`.

`Database._migrate_integrity_cache_schema_2_5()` adds `local_file_verifications` for 2.4 projects. The table stores only the stat fingerprint of the last successful full local SHA-256 pass; it can be emptied at any time and rebuilt by ingest, `verify`, or QC; it does not change the content identity in `files`. It must be kept as long as opening 2.4 projects is supported. The corresponding regression test is `test_schema_2_5_adds_local_file_verification_cache`.

`Database._migrate_recovery_schema_2_6()` adds, purely additively, for 2.5 projects:

- `workflow_runs.resumes_run_id`;
- `changes.workflow_run_id` and `changes.reverts_change_id`;
- `schema_migrations`, `adapter_run_items`;
- `ncbi_assembly_records`, `ncbi_annotation_records`;
- `entity_supersessions`.

The migration does not delete or rewrite existing assembly, annotation, file, QC, analysis, release, workflow, or changes rows. Business anomalies of the old NCBI adapter are handled by the explicit `operon ncbi-reconcile` and must not be hidden inside a schema migration. The corresponding regression test is `test_schema_2_6_adds_resumable_adapter_and_repair_history`.

`Database._migrate_lifecycle_schema_2_7()` adds, purely additively, for 2.6 projects `entity_lifecycle_events`, `current_entity_lifecycle`, and `effective_retired_entities`. The event table only appends `RETIRE`/`RESTORE`; a restore event points back to the reversed direct retirement through `reverts_event_id` and the corresponding `changes.reverts_change_id`; the effective-retired view propagates state along the organism → sample → run/assembly → annotation ownership relation. The migration does not delete, move, or rewrite metadata, files, QC, analyses, releases, workflows, or archived bytes. It must be kept as long as opening 2.6 projects is supported. The corresponding regression test is `test_schema_2_7_adds_append_only_entity_lifecycle`.

`Database._migrate_environment_schema_2_8()` adds execution-environment capture, purely additively, for 2.7 projects: it creates the `execution_environments` table (primary key `environment_id` is the SHA-256 of the normalized JSON document, content-addressed; plus `document` and `created_at` columns) and adds an `environment_id` column to both `workflow_runs` and `analysis_jobs`. The migration is idempotent (ALTER TABLE ADD COLUMN + CREATE TABLE IF NOT EXISTS) and recorded in the ledger as `2.8-execution-environments`; it rewrites no existing rows, and historical rows keep `environment_id` NULL. It must be kept as long as opening 2.7 projects is supported. The corresponding tests are `test_schema_2_8_columns_and_table_exist` and `test_migration_adds_columns_to_pre_2_8_database` in `tests/unit/test_environment.py`.

`Database._migrate_schema_2_9()` adds derived-file lineage, recipe snapshots, and run resource usage, purely additively, for 2.8 projects: it creates the `file_lineage` and `recipe_snapshots` tables, adds the `duration_seconds`, `avg_rss_mb`, and `cpu_seconds` columns to `workflow_runs`, and adds the `recipe_snapshot_id` column to `analysis_jobs`. The migration is idempotent (ALTER TABLE ADD COLUMN + CREATE TABLE IF NOT EXISTS) and recorded in the ledger as `2.9-lineage-recipes-resources`; it rewrites no existing rows, and the new columns of historical rows stay NULL. It must be kept as long as opening 2.8 projects is supported. The corresponding tests are `test_schema_2_9_columns_and_tables_exist` and `test_migration_backfills_dropped_2_9_objects` in `tests/unit/test_schema_2_9.py`.

The corresponding regression test is `test_v1_qc_and_decisions_migrate_without_data_loss` in `tests/regression/test_correctness.py`. When the migration code is removed, remove that test as well, and record the incompatibility with legacy databases in the 1.0 release notes.

## Automatic project metadata-schema upgrades

File: `operon/adapters/ncbi_datasets.py`

`_adapter_schema()` currently merges the adapter-owned assembly fields into an old project's `config/schemas.yaml`, appends paired-source file roles, and raises the old version to `1.4`. The official release should instead explicitly require a supported schema version with an actionable error message, and no longer silently modify old project schemas.

The `compatible_rows` logic in `_validate_plan_rows()` that projects adapter rows onto the known columns of an old schema belongs to the same compatibility layer. Once schema 1.1+ is required, this projection should be removed so that unknown or missing fields directly trigger validation errors, instead of the official release continuing to silently drop adapter fields.

The corresponding tests are all cases calling `_make_schema_legacy()` in `tests/integration/test_ncbi_datasets_adapter.py`; when the compatibility layer is removed, they must be converted into tests asserting that legacy schemas are explicitly rejected.

Metadata schema 1.2 adds `REMOTE_ONLY` to `files.status`. New projects currently generate 1.4 directly, which includes this controlled vocabulary; for an old project, the first `operon evict` that passes the remote/local identity preflight, or an `operon verify` that live-confirms a remote copy when local bytes are missing, appends only this allowed value while preserving custom fields and raises the version to 1.2. That upgrade is a necessary contract of the current remote-residency feature, not part of the NCBI adapter's 1.0/1.1 compatibility projection.

File: `operon/taxonomy.py`

Metadata schema 1.3 adds the `taxonomy_snapshot` entity type, the `TAX_` ID prefix, and the `taxonomy_package` role for taxonomy source-package manifests. New projects generate 1.4 directly; before an old project's first successful `operon taxonomy import`, `_ensure_taxonomy_metadata_schema()` preserves custom fields, appends only these controlled vocabulary items, and raises the version to 1.3. This is a current contract required for taxonomy snapshot file identity and must not be deleted together with the NCBI genome adapter's 1.0/1.1 compatibility projection.

Metadata schema 1.4 adds the `genome_fasta_genbank/refseq` and `assembly_report_genbank/refseq` roles for paired GCA/GCF source files. The NCBI adapter or `ncbi-reconcile --apply` appends only these controlled values and preserves project custom fields; the taxonomy 1.3 upgrade logic only runs when the current version is below 1.3 and must not downgrade 1.4 back to 1.3.

## Compatibility behaviors not in this inventory

- `--copy-files` is a command-line argument alias, not database compatibility code.
- The hardlink/symlink modes of `standardize` are storage policies, not database migrations.
- The old method name `upsert_decision()` is a Python API compatibility concern and does not affect the database file format.
- `ensure_metadata_columns()` is a current custom-schema feature and must not be removed together with the legacy database migrations.
