# Importing and Downloading NCBI Datasets

## Preview existing metadata

The adapter accepts NCBI Datasets JSON/JSONL reports, dataformat TSV/CSV files, complete ZIP packages, and unpacked directories. Run a dry run before the first import:

```bash
operon ncbi-datasets \
  --input /data/assembly_data_report.jsonl \
  --dry-run
```

The output includes the number of organism/sample/assembly/annotation IDs to create in `new_ids` and the rows to upsert in `metadata_rows`. A dry run does not copy input, write the database, or create logs.

If the project still uses an old metadata schema, a formal import preserves custom fields, adds the fields and paired-source file roles required by the adapter, and upgrades the schema to 1.4. A dry run does not modify the schema.

After review, remove `--dry-run`:

```bash
operon ncbi-datasets --input /data/assembly_data_report.jsonl
```

## Import an existing ZIP and archive its files

```bash
operon ncbi-datasets --input /data/ncbi_dataset.zip
```

The package is unpacked safely and mapped as follows:

| Datasets file | Operon role/entity |
|---|---|
| `genomic.fna` | `genome_fasta` → assembly |
| `genomic.gff` / `.gff3` | `annotation_gff3` → annotation |
| `protein.faa` | `protein_fasta` → annotation |
| `cds_from_genomic.fna` | `cds_fasta` → annotation |
| `sequence_report.jsonl` / assembly report | `assembly_report` → assembly |

The original ZIP is stored by SHA-256 under `raw/metadata/ncbi_datasets/`. Biological files enter raw storage and the `files` manifest through the normal `ingest` path. Re-importing the same package reuses the same internal IDs and file IDs.

To import metadata only:

```bash
operon ncbi-datasets --input /data/ncbi_dataset.zip --no-archive-files
```

## Download and archive online

```bash
export NCBI_EMAIL='you@example.org'
# Optional, for a higher request quota:
# export NCBI_API_KEY='...'

operon ncbi-datasets \
  --accession GCF_000005845.2 \
  --accession GCA_000001405.29
```

For a large batch:

```bash
operon ncbi-datasets --accession-file accessions.txt \
  --download-workers 3
```

The default batch size is 10 (allowed range: 1–100), and aiohttp downloads up to 3 batches concurrently (allowed range: 1–10). Each completed batch is imported and archived immediately, and its temporary ZIP is removed. Reports are streamed from the ZIP; biological members are extracted one at a time on the project filesystem and moved to `raw/`. The process does not accumulate all ZIPs and unpacked copies in `/tmp`.

Before downloading, the planner checks the manifest and file status, calculates the missing `include` set for each accession, and groups accessions with the same missing set. If GFF/CDS/protein already exist and only genome/sequence-report are missing, only the latter are requested. The summary contains `download_plan` and `skipped_existing`.

Generate the same plan without downloading or writing a workflow:

```bash
operon ncbi-datasets --accession-file accessions.txt \
  --include genome --include sequence-report \
  --plan-only > ncbi-download-plan.json
```

`--plan-only` accepts only `--accession` or `--accession-file`. Because it checks manifest status and local paths, run it against the real project or a full snapshot. A control/results backup without raw files is not sufficient for inferring production download gaps.

Transient errors such as `[SSL] record layer failure`, interrupted connections, timeouts, and HTTP 429/5xx are retried with exponential backoff:

```bash
operon ncbi-datasets --accession-file accessions.txt \
  --retries 4 --retry-backoff 1.0
```

The default is four retries. NCBI can return a README-only package for an invalid, withdrawn, or unavailable accession. Operon identifies the accession, continues other batches, reports the failure in the summary, and exits non-zero. To control disk use, reduce `--batch-size` or `--download-workers`, and repeat `--include` only for required file types.

By default, all supported types are requested. To request only genome and annotation:

```bash
operon ncbi-datasets \
  --accession GCF_000005845.2 \
  --include genome \
  --include gff3
```

Downloads use the NCBI Datasets v2 API. Biopython Entrez is a metadata fallback when a package lacks a report and an email is configured. ZIP files are streamed to the project filesystem and validated before import.

Resume an interrupted run with a new workflow:

```bash
operon ncbi-datasets --accession-file accessions.txt \
  --include genome --include sequence-report \
  --resume-run WF_20260830_001233+0800_4f212100
```

The accession, include, and archiving options must match the original request. The new workflow points to the old one through `resumes_run_id`; the old run remains `failed` or `interrupted`. Per-accession attempts are stored in `adapter_run_items`, and completed files are skipped exactly by manifest identity.

## Deduplication and versioning rules

- Same taxon ID: reuse the organism.
- Same BioSample accession: reuse the sample.
- Paired GCF/GCA accessions: map to one assembly. A new entity deterministically prefers GCF as the display canonical accession; an existing valid canonical value is not rewritten by a later alias.
- Same fully versioned accession: idempotent update.
- Changed accession version: create a new `ASM_`; old files are not overwritten.
- BioProject is one-to-many for assemblies and is stored in `assemblies.bioproject_accession`, not in the unique accession mapping table.
- Missing BioSample: create an assembly-specific sample and reuse it through the assembly on repeat imports.

If a package contains different bytes for an existing entity/role, the command rejects the import before metadata is committed. Create a new assembly or annotation version instead.

Paired GCA/GCF reports or genomes may contain different bytes. The canonical source uses `assembly_report` or `genome_fasta`; the other source uses a controlled `_genbank` or `_refseq` role. Annotation identity includes source accession, provider, version, and release date. On first re-import into an old database, strict metadata matching continues the existing `ANN_` where possible.

## Repair old adapter artifacts

Preview the repair plan:

```bash
operon --project /path/to/project ncbi-reconcile > ncbi-reconcile-plan.json
```

Review the plan and warnings, then apply it:

```bash
operon --project /path/to/project ncbi-reconcile --apply --actor "$USER"
```

Repairs use logical supersession and compensating change records. They do not delete old annotations, files, workflows, or raw bytes. If the plan finds different SHA-256 values for a target source role, application stops. Do not bypass the conflict with manual SQL; first determine whether the two source records belong to different assembly/annotation versions.

For a complete old-database procedure—backup, schema migration, rehearsal, repair, acceptance, and download resumption—follow [NCBI Adapter Recovery and Migration](../operations/ncbi-recovery-migration.md).
