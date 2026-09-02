# Data model

## Core entities

```text
organisms (ORG_)
    └── samples (SMP_)
            ├── runs (RUN_)         sequencing run; produces reads
            └── assemblies (ASM_)   assembly version
                    └── annotations (ANN_)   annotation version
                            ├── GFF3
                            ├── CDS FASTA
                            └── protein FASTA
```

External accessions live in a separate `accessions` table and are never primary keys:

```text
internal_type   internal_id    namespace        accession         version
assembly        ASM_000001     NCBI_Assembly    GCA_000000001     1
sample          SMP_000001     NCBI_BioSample   SAMN0000001       1
```

## files: the file manifest

`files` is the manifest of archived files. Key fields:

```text
file_id, entity_type, entity_id, file_role, format, compression,
relative_path, source_url, size_bytes, sha256, downloaded_at, status
```

File identity is defined by `file_id + sha256 + size_bytes`. `relative_path` only records where the file currently resides in the project.

## qc_results: the QC long table

Both built-in QC and external QC write into the same long table. In the current version each result additionally binds:

```text
file_id        the manifest file this metric corresponds to (nullable)
file_sha256    SHA-256 of that input file (nullable)
input_identity unique input identifier:
               file:{file_id}:{sha256} or entity:{entity_type}:{entity_id}
               built-in annotation QC that reads associated inputs uses input-set:v1:{sha256}
```

The `input-set:v1` digest is computed from the normalized `kind + file_id + sha256 + size_bytes` of the primary GFF3, the assembly FASTA, and the protein FASTA actually read. Verification cache-hit state and length-index paths do not participate in the identity, so the same set of content upserts to the same result whether built for the first time or hit later; when the content identity of any associated file changes, a new QC input identity is generated and old results are retained. The assembly length index is a rebuildable derived artifact: besides binding the manifest content identity above, it also verifies the SHA-256 digest of its own index rows.

The unique constraint is:

```text
(input_identity, qc_stage, metric_name, tool, tool_version, parameter_set)
```

This guarantees that same-named metrics for different input files of the same entity — R1, R2, GFF3, protein FASTA — never overwrite each other. When querying `latest_metrics()`, metrics of the "any file failing means failure" kind such as `file_exists`, `sha256_match`, `parseable`, and `paired_read_count_match` take the minimum across multiple inputs (the conservative value).

Different recipes/run parameters of external analyses coexist as different `qc_stage` and `parameter_set` values. For example, a fixed BUSCO lineage uses `analysis:busco_lineage:lineage_dataset=<name>`. The long table preserves all these results; the wide table, with only one column per metric, provides a browsing view of the most recent values only. The rule engine can read only a designated stage through `source.qc_stage`, so a formal decision is never shifted by the "latest value" of another analysis variant.

## qc_profiles and decisions: traceable decisions

On every `evaluate`, the rule engine:

1. Serializes the YAML profile content as normalized JSON and computes its SHA-256;
2. Writes the profile snapshot into `qc_profiles` (deduplicated by same name, same version, same content);
3. **Appends** the new automatic decision to `decisions`, never overwriting old ones;
4. The `current_decisions` view returns the latest decision per `(entity_type, entity_id, profile)`.

Therefore re-evaluating after changing profile thresholds creates new decision history, and release plus `report decisions` read `current_decisions` by default; use the corresponding `report` subcommand when a tabular snapshot is needed.

A rule's threshold can be given as a scalar `value` or selected through `value_by` from another metric in the same source. For example, the BUSCO complete threshold is mapped from `busco_lineage_dataset`. When the selector is absent from the mapping, the profile explicitly specifies `warning`, `fail`, or `ignore`; by default the rule is treated as lacking a usable threshold (`NOT_EVALUATED`), and `ignore` leaves a persistent trace in the decision's reason_codes. There is no implicit categorical fallback.

## Other system tables

| Table | Purpose |
|---|---|
| `entity_state` | Entity-level state machine, including the database schema marker row |
| `workflow_runs` | Structured run records (mirroring `logs/workflow.jsonl`) |
| `execution_environments` | Content-addressed execution-environment documents (hostname, OS/kernel, Python/operon versions, relevant environment variables, docker probe); referenced by `workflow_runs` and `analysis_jobs` through `environment_id` |
| `data_sources` | External databases/repositories, providers, record URLs, citations, licenses, and normalized content identity |
| `source_links` | Many-to-many associations between sources and organism/sample/run/assembly/annotation/file, plus import provenance |
| `schema_migrations` | Stable IDs, script identities, and application times of applied database migrations |
| `adapter_run_items` | Accession/item-level state, attempts, errors, and result write-sets of resumable adapters |
| `ncbi_assembly_records` | Mapping of GCA/GCF source records to stable `ASM_` IDs, canonical flags, and source-file pointers |
| `ncbi_annotation_records` | Annotation identities normalized from source accession/provider/version/date |
| `entity_supersessions` | Logical replacement relationships without deleting old rows, plus repair provenance |
| `entity_lifecycle_events` | Append-only `RETIRE`/`RESTORE` history of entities: reason, evidence, actor, workflow, and reverse-event pointers |
| `current_entity_lifecycle` | Latest direct lifecycle event per entity; expresses only that entity itself, not inherited ancestor state |
| `effective_retired_entities` | Currently effective retired set; propagates along organism → sample → run/assembly → annotation and keeps the root retirement event identity |
| `file_locations` | URI, identity copy, availability status, and last verification time of each `file_id` on each remote mirror; rebuildable from remote manifests |
| `local_file_verifications` | Stat fingerprint of the last full local SHA-256 pass; a rebuildable QC acceleration cache only — it does not change manifest file identity |
| `releases` / `release_members` | Release metadata and member file lists |
| `analysis_jobs` | External analysis jobs: command, version, parameter fingerprint, input/database fingerprints, output checksums, cache state |
| `analysis_results` / `analysis_hits` | Analysis summary metrics and top-hits long tables synced into the database |
| `taxonomy_snapshots` | NCBI Taxonomy versions, source manifest identities, node counts, and import status |
| `taxonomy_nodes` / `taxonomy_aliases` | Frozen taxonomy tree nodes and secondary/merged TaxID mappings |
| `taxonomy_reference_sets` | Denominator TSV identities and per-rank row counts compiled from coverage profiles and taxonomy versions |
| `coverage_reports` / `coverage_report_metrics` | Coverage report history per immutable input identity, with family/genus metrics |
| `changes` | Audit log of manual modifications |

## Entity retirement and restoration: isolate first, decide on physical removal later

`retire` is a control-plane state change, not a file operation. It appends a direct `RETIRE` event to `entity_lifecycle_events` and an audit row to `changes`; it does not delete database rows, move files, modify checksums, revoke existing QC/analysis/workflow records, or rewrite already-created releases. Retiring a parent entity effectively retires its ownership descendants in `effective_retired_entities`: an organism covers its samples, runs, assemblies, and annotations; a sample covers its own runs, assemblies, and annotations; an assembly covers its annotations.

`restore` only reverses the target's own most recent direct `RETIRE`, appending a `RESTORE` that points back to the original event/audit row; history is never deleted. A child entity that inherited retirement from an ancestor cannot be restored individually — the root causing the isolation must be restored first. Conversely, if a child has its own direct retirement, it stays retired even when the parent is restored. This keeps the inverse operation strictly paired with the original one and never erases an independent human decision.

Active data consumers exclude effectively retired entities by default: descendant counts in `show`, status/report, batch QC, rule evaluation, external analysis candidates, metadata coverage, NCBI re-import reuse, and new releases. Use the corresponding `--include-retired` when explicitly querying history; `retired` lists current direct and inherited states. Backups, verification, remote residency, read-only SQL, existing releases, and audit history retain the complete archival view.

The current architecture has no `purge`. A retirement plan lists descendants, files, and QC/decision/analysis/workflow/source/remote/release references, with `physical_changes` explicitly zero. If physical removal is added in the future, it must take this auditable state and reference graph as a precondition, with separately defined retention periods, release/remote reference protection, a recoverable window, and irreversible confirmation.
