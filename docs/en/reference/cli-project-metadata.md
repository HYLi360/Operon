# Project and Metadata Commands

## General form

```text
operon [--project PATH] [--version] <subcommand> [arguments]
```

- `--project PATH`: project root or path to `project.yaml`; defaults to the current directory. Place it before the subcommand.
- `--version`: prints the `operon` version.

## init

```bash
operon init [path] [--project-id PRJ_000001] [--name NAME]
```

Creates `project.yaml`, an empty current-schema `operon.sqlite`, `config/`, and lifecycle directories. Read-only preview commands can run immediately after initialization. `metadata/` retains only the 0.4 migration note; empty TSV files that could be imported back are no longer generated. Existing `project.yaml` causes an error.

## init-demo

```bash
operon init-demo [path] [--project-id PRJ_DEMO_001]
```

Creates deterministic synthetic data, archives 9 files, runs QC/evaluation, and creates `releases/2026.08.demo`.

## status

```bash
operon status [--entity-type TYPE] [--entity-id ID] [--include-retired]
```

Prints entity states and messages from `entity_state`. Effectively retired entities are hidden by default; use `--include-retired` for historical audits.

## schema

```bash
operon schema            # Print the schema file path
operon schema --dump     # Print the full schema
```

## migrate

```bash
operon --project /path/to/project migrate
```

Applies additive database schema migrations for the current version, then prints the target version, migration ledger, `PRAGMA integrity_check`, and number of foreign-key violations. It does not perform business repairs such as `ncbi-reconcile`. Run read-only `backup create` before migration.

## import

```bash
operon import dataset

operon import table --table TABLE --template template.xlsx
operon import table --table TABLE --file data.csv \
  [--on-conflict {error,skip,update}] [--yes]
```

- `dataset`: starts the English questionary wizard. Existing organisms can be selected by scientific name. The source section distinguishes INSDC/non-INSDC and requests database/repository, provider, record URL, citation, and license. Citation/DOI and license are mandatory for non-INSDC data. Other fields and files may be skipped, but warnings remain on the summary page. Editing a section returns to the summary rather than resuming the original linear flow. The project is not modified before final confirmation.
- `table --template`: creates an empty `.csv` or `.xlsx` template. XLSX also contains a read-only `schema` worksheet with types, required fields, allowed values, and descriptions.
- `table --file`: reads CSV or the first XLSX worksheet, validates schema and foreign keys, and prints a row-level preview.
- Importable tables are `organisms`, `samples`, `runs`, `assemblies`, `annotations`, and `accessions`. The system-managed `files` table cannot be overwritten by table import. Table updates and foreign-key references cannot target effectively retired entities; restore them explicitly first.
- On collision, `error` rejects, `skip` skips existing rows, and `update` updates fields and writes per-field records to `changes`. Non-interactive execution requires `--yes`; updates also require an explicit `--on-conflict`.
- SQLite is the only writable metadata source of truth. The old `import-metadata` and `export-metadata` commands have been removed.

## add

```bash
operon add {organism|sample|run|assembly|annotation} \
  [--id INTERNAL_ID] [--field KEY=VALUE ...]
```

- `--field` can be repeated.
- If `--id` is omitted, the next stable internal ID is assigned.
- Data is written to SQLite and audited. No second writable TSV mirror is maintained.
- Example: `operon add organism --field scientific_name="Escherichia coli" --field taxonomy_source=NCBI`.

## add-accession

```bash
operon add-accession \
  --internal-type {organism|sample|run|assembly|annotation} \
  --internal-id ID --namespace NS --accession ACC \
  [--version VERSION] [--primary]
```

Example: `--namespace NCBI_Assembly --accession GCA_000000001`.

## ncbi-datasets

```bash
operon ncbi-datasets \
  [--input PATH ...] \
  [--accession GCF_OR_GCA ...] \
  [--accession-file FILE] \
  [--include {genome,gff3,protein,cds,sequence-report} ...] \
  [--no-archive-files] [--standardize] [--dry-run] \
  [--no-preserve-source] [--email EMAIL] [--api-key API_KEY] \
  [--timeout SECONDS] [--batch-size N] \
  [--download-workers N] [--retries N] [--retry-backoff SECONDS] \
  [--resume-run WF_ID] [--plan-only]
```

Provide at least one source:

- `--input PATH`: an existing `assembly_data_report.json/jsonl`, dataformat TSV/CSV, NCBI Datasets ZIP, or unpacked directory; repeatable.
- `--accession ACC`: downloads a genome package through the NCBI Datasets v2 API; repeatable.
- `--accession-file FILE`: one GCA/GCF accession per line; blank lines and `#` comments are ignored.

Default behavior:

- Online downloads request genome FASTA, GFF, protein FASTA, CDS FASTA, and sequence report.
- Organism, taxon, BioSample, BioProject, assembly, and annotation metadata are parsed automatically.
- Internal IDs are assigned or reused; paired GCA/GCF accessions point to the same assembly. An existing canonical accession is not rewritten by a later alias, while a new paired assembly deterministically prefers GCF.
- A fully versioned accession is part of assembly identity; a new version does not overwrite the old version.
- Original ZIP/report files are stored by SHA-256 under `raw/metadata/ncbi_datasets/`.
- Biological package files enter raw storage and the `files` manifest through the normal `ingest` path.
- SQLite, entity state, `changes`, and workflow provenance are updated.

Common variants:

```bash
# Inspect mappings and planned IDs without writing the project.
operon ncbi-datasets --input ncbi_dataset.zip --dry-run

# Import metadata without archiving package files.
operon ncbi-datasets --input assembly_data_report.jsonl --no-archive-files

# Download only genome FASTA and GFF.
operon ncbi-datasets --accession GCF_000005845.2 \
  --include genome --include gff3

# Calculate missing files and download groups only; do not download or create a workflow.
operon ncbi-datasets --accession-file accessions.txt \
  --include genome --include sequence-report --plan-only

# Create standardized copies after archiving.
operon ncbi-datasets --input ncbi_dataset.zip --standardize
```

`--email` and `--api-key` can also be provided through `NCBI_EMAIL` and `NCBI_API_KEY`. Biopython Entrez is used only as a metadata fallback for rare packages without an assembly report; normal downloads use the NCBI Datasets API, streamed ZIP writes, and integrity checks.

`--batch-size` defaults to 10 (range 1–100). `--download-workers` defaults to 3 (range 1–10) and downloads multiple batches concurrently with aiohttp. Each completed batch is imported, archived, and cleaned immediately. Before downloading, the planner checks manifest/file status and calculates the missing include set for each accession. Accessions with the same missing set are grouped. For example, if GFF/CDS/protein exist and only genome/sequence-report are missing, the request contains only `genome,sequence-report`. Annotation roles must all belong to one non-superseded `ANN_`; Operon does not assemble a false complete set from multiple annotations. With `--standardize`, the standardized copy must also exist. Fully satisfied accessions are reported as `skipped_existing`.

`--plan-only` supports only accession download sources. It emits the same `download_plan` and `skipped_existing` output and runs read-only: no download, schema migration, database write, or workflow creation. Manifest filtering does not apply with `--no-archive-files`.

`--retries` defaults to 4 (range 0–10); `--retry-backoff` defaults to 1.0 second and uses exponential backoff. Transient SSL record-layer failures, interrupted connections, timeouts, and HTTP 429/5xx are retried. README-only packages for invalid or withdrawn accessions are identified and reported, while other batches continue; the final summary reports failure and exits non-zero. ZIP reports are read directly, and package members are staged one at a time on the project filesystem rather than accumulating all batches or unpacked content in `/tmp`. A disk-space precheck reports target filesystem, required space, and available space.

Interruption and graceful shutdown: on Ctrl+C (SIGINT) or SIGTERM, concurrent downloads are cancelled, no new batches are accepted, temporary ZIPs for the current batch are removed, and the workflow is recorded as `interrupted`. Each accession remains in `adapter_run_items` as `pending`, `downloading`, `completed`, `failed`, or `interrupted`. To resume, rerun the same request with `--resume-run WF_ID`. The new workflow links the old run through `resumes_run_id`; the old run is not rewritten. A changed request fingerprint is rejected. Completed content is skipped exactly from the manifest.

## ncbi-reconcile

```bash
operon ncbi-reconcile
operon ncbi-reconcile --apply [--actor NAME]
```

By default, the command builds a repair plan from SQLite metadata, file SHA-256 values, and QC/analysis/release references. It does not read raw biological content or modify business rows. `--apply` runs the plan as a separate repair workflow:

- Duplicate annotations with the same assembly/provider/version/date and no different SHA-256 for a file role are merged logically through `entity_supersessions`; original `ANN_`, `FIL_`, and raw bytes are retained.
- For paired GCA/GCF, the display canonical accession is restored to the historical canonical value indicated by existing file evidence. Without historical evidence, the valid current canonical value is retained; GCF is preferred deterministically only when no evidence is available.
- Assembly report/genome files from the non-canonical source use independent roles `assembly_report_genbank/refseq` and `genome_fasta_genbank/refseq`. Their files are physically moved to the new roles' canonical paths. All target paths are checked before moving; a byte conflict rejects the entire plan. Locally missing rows are not moved and are listed in `skipped_path_moves`. Historical rows renamed without a path move are handled by `file_path_repairs`.
- An annotation that has QC results but was downgraded to an earlier state by re-import is restored to `QC_COMPLETE`.
- Every field's before/after values, reason, evidence, and repair run are written to `changes`.

If a target source role already has a different SHA-256, `--apply` refuses to run. Review the dry run manually first. After application, another dry run excludes existing supersessions; when no new anomaly exists, all summary counts are zero.

## next-id

```bash
operon next-id {organism|sample|run|assembly|annotation|file}
```
