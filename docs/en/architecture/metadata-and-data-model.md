# Metadata flow and data model

## Metadata flow

```text
interactive import / import table / dedicated adapter / add
        │
        ▼
Draft or input-table preview            project not modified
        │
        ▼
schema + cross-reference + conflict     types, required fields, allowed values,
preflight                               foreign keys, existing primary keys
        │
        ▼
user confirmation                       summary review or table diff
        │
        ▼
controlled transaction into             SQLite is the only writable source
SQLite + changes                        of truth
        │
        ├─> ingest ─> raw/files manifest
        └─> report metadata ─> derived read-only TSV snapshot
```

- `operon import dataset` builds a draft through an English-only questionary wizard; existing organisms are auto-completed for selection by scientific name. The source section distinguishes INSDC from non-INSDC, collecting database/repository, provider, record URL, citation, and License; for non-INSDC sources, citation and License are mandatory entry conditions. Nothing is written to the project before final confirmation, and editing any section from the summary page returns directly to that summary page.
- `operon import table` accepts only human-managed metadata tables, with CSV/XLSX templates, preview, collision policies, and field-level auditing; it does not import the system-managed `files` manifest.
- `operon report metadata` generates a read-only TSV snapshot from SQLite with row counts and a SHA-256 manifest, including `data_sources.tsv` and `source_links.tsv`. Editing these reports does not change the database.
- The `metadata/` directory is retained only for the legacy layout; no TSV is read or written there automatically.

## NCBI Datasets adapter

`ncbi-datasets` adds a source adaptation layer ahead of the generic TSV flow, but does not establish a second data model:

```text
existing JSON/JSONL/TSV/ZIP/directory ─┐
                                       ├─> report parser ─> normalized mapping ─> schema validation ─> SQLite
NCBI Datasets v2 download ─────────────┘                          │
                                                                  └─> ingest ─> files manifest/raw
```

Online download and offline import share exactly the same back half. The download layer uses aiohttp to download multiple accession batches concurrently (`--download-workers`) in a background asyncio thread, handing finished batches back to the calling thread through a queue for import, avoiding cross-thread SQLite use. Transient errors — SSL record layer failures, connection drops, timeouts, 429/5xx — are retried automatically with exponential backoff; the single-batch compatibility interface `download_ncbi_dataset()` has the same outer SSL/network retry. Downloads use streaming writes, temporary files, disk-space preflight, and ZIP integrity verification; when a package abnormally lacks an assembly report and an NCBI email is configured, Biopython Entrez serves as a metadata fallback. For invalid/withdrawn accessions, NCBI may return a README-only "empty package" (a ZIP without a central directory); the download layer parses the local file header to recognize this non-transient error, reports the specific accession, and lets the download and import of other batches continue.

Identity and relationship policy:

- Taxon ID, BioSample, and fully versioned GCA/GCF are used to reuse entities;
- A paired GCA/GCF points to the same `ASM_`; canonical is never rewritten by arrival order, and GCF wins deterministically for new entities;
- `.1` → `.2` is treated as a new immutable assembly version;
- BioProject is a one-to-many ordinary field and does not enter the unique accession mapping table;
- Records without a BioSample use an assembly-specific sample;
- Annotation identity includes source accession, provider, version, and release date, with files automatically assigned to the corresponding `ANN_`; pre-2.6 rows are continued with strictly identical metadata, avoiding duplicate assignment when the provider is not `NCBI *`.

Before writing metadata, the adapter computes SHA-256 for the files to be archived and checks both in-package conflicts for the same entity/role and existing manifest conflicts. Alternate genomes/reports from paired sources use controlled roles with `_genbank`/`_refseq` suffixes, so bytes from different sources can coexist without relaxing the no-overwrite constraint on the same entity and role. Original reports/ZIPs are stored by SHA-256 under `raw/metadata/ncbi_datasets/`; import summaries are written to `changes` and the workflow provenance. On a formal import into an old project, adapter-owned fields and source-file roles are merged in, and the metadata schema is upgraded to {{ metadata_schema }}; custom fields are preserved, and dry runs use only the in-memory upgraded schema.

An adapter run writes a `running` workflow before processing begins; each accession's state is kept in `adapter_run_items`. Failed or interrupted runs keep their state, and a resumed run uses a new run ID with `resumes_run_id`; a request whose SHA-256 does not match is refused. Field-level before/after values of metadata upserts are linked to the concrete run through `changes.workflow_run_id`. Anomalies from the old adapter are handled by an explicit `ncbi-reconcile` that generates and applies a compensation plan, preserving all old rows and files through `entity_supersessions`.

## NCBI Taxonomy coverage snapshots

Taxonomy coverage is separate from the NCBI genome adapter: it reads an NCBI Datasets `taxonomy_report.jsonl`/package, or an official taxdump archive containing `nodes.dmp` and `names.dmp` (plus optional `merged.dmp`/`delnodes.dmp`). The original package is archived by SHA-256 under `raw/metadata/ncbi_taxonomy/`, and tree nodes plus secondary TaxIDs are imported into `taxonomy_snapshots/nodes/aliases`. The version label must be specified explicitly by the caller; the same version with different bytes is rejected as a conflict.

`config/profiles/*.yaml` distinguishes `qc` from `taxonomy_coverage` through the required `kind`. A coverage profile declares one or more root TaxIDs, family/genus target ranks, extinct/excluded-subtree/name-regex rules, and per-rank thresholds. `taxonomy compile` traverses descendants against a concrete taxonomy version and produces a deterministically sorted `taxonomy/reference_sets/<profile>@<taxonomy_version>.tsv`. SHA-256 is recorded for the TSV, the taxonomy source package, and the profile; identical input is reused idempotently, different content is never overwritten, and the first compilation enters the `changes` audit.

The extinct boolean in Datasets JSON supports `exclude_extinct`; classic taxdump has no such field, and its nodes are stored as unknown. If a profile requests extinct exclusion, the compiler rejects that combination and requires explicit excluded-subtree/name rules or a snapshot with extinct annotations, rather than silently changing the computation basis.

`report coverage` reads only this TSV denominator:

- The metadata scope reads `organisms` directly, expressing "what the database has registered as sampled";
- The release scope validates `release_members` and the release manifest, tracing organisms back through the metadata tables frozen inside the release directory, expressing "what the published dataset covers".

The numerator is the number of distinct family/genus TaxIDs after projection onto the reference set, not the number of organisms. Secondary TaxIDs can be mapped through aliases of the same taxonomy snapshot; non-NCBI, missing/unknown TaxIDs, and profile exclusions go into the exclusion list — no name guessing. The report outputs the summary, complete targets, missing targets, included/excluded observations, and provenance; identical input identity is verified and reused, and new reports are appended when metadata/release membership changes. For the detailed contract, see [NCBI Taxonomy coverage](../guides/taxonomy-coverage.md).

For detailed table structures, see the [data model reference](../reference/data-model.md).
