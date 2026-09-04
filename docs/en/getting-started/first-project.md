# First Real Project

This page builds a minimal but complete project. Commands can be rerun after a failure; completed idempotent steps are skipped or validated rather than duplicated.

## Initialize the project

```bash
operon init ./my-genome-project --project-id PRJ_MY_001 --name "My first genome project"
cd ./my-genome-project
```

The command creates:

```text
project.yaml         Project configuration
config/              Schemas, QC/coverage profiles, and tools.yaml
metadata/            Legacy-layout compatibility note
raw/ standardized/ qc/ analysis/ reports/ logs/ releases/ taxonomy/
```

`operon.sqlite` is created the first time a command needs the database.

> The global `--project` option must appear before the subcommand. It can be omitted inside the project root. Outside the project, use `operon --project /path/to/my-genome-project <subcommand>`.

## Use the interactive import wizard

For collaborator deliveries, local pipelines, and other non-NCBI datasets, start the English-language wizard:

```bash
operon import dataset
```

The wizard collects source, organism, sample, sequencing, assembly, annotation, and file information. Existing organisms can be selected by scientific name. The source section distinguishes INSDC from non-INSDC sources and records database/repository, provider, record URL, citation, and license. Citation and license are mandatory for non-INSDC data.

Optional fields can be skipped, but the summary page continues to display warnings. Selecting `Edit source`, `Edit files`, or another section returns directly to the summary after editing. SQLite and the file archive are modified only after `Execute import` is selected and remaining warnings are confirmed.

## Import an NCBI Datasets package

For an existing NCBI Datasets report or genome package, first inspect the plan without modifying the project:

```bash
operon ncbi-datasets --input /data/ncbi_dataset.zip --dry-run
```

Then import metadata and archive genome/GFF/CDS/protein/report files from the package:

```bash
operon ncbi-datasets --input /data/ncbi_dataset.zip
```

To download by accession:

```bash
export NCBI_EMAIL='you@example.org'
operon ncbi-datasets --accession GCF_000005845.2
```

For a large batch, downloads use three workers by default and retry transient SSL/network errors with exponential backoff:

```bash
operon ncbi-datasets --accession-file accessions.txt \
  --download-workers 3 --retries 4 --retry-backoff 1.0
```

The adapter creates organism → sample → assembly/annotation relationships, assigns stable IDs, records GCA/GCF/BioSample/Taxonomy mappings, and stores the original ZIP under `raw/metadata/ncbi_datasets/`. After this import path, continue at [Verify the archive](#verify-the-archive).

## Add metadata manually

The minimum relationship chain is:

```text
organism (ORG_) -> sample (SMP_) -> assembly (ASM_) / run (RUN_)
                                  -> annotation (ANN_)
```

Add an organism, sample, and assembly:

```bash
operon add organism \
  --field scientific_name="Arabidopsis thaliana" \
  --field taxon_id=3702 \
  --field taxonomic_rank=species \
  --field taxonomy_source=NCBI

operon add sample \
  --field organism_id=ORG_000001 \
  --field strain=Col-0 \
  --field tissue=leaf \
  --field tissue_normalized="young leaf" \
  --field country=China \
  --field country_iso=CN \
  --field collection_date=2026-04-15

operon add assembly \
  --field sample_id=SMP_000001 \
  --field assembly_accession=GCA_999999999 \
  --field assembly_version=1 \
  --field assembly_level=chromosome \
  --field reference_status=representative
```

If `--id` is omitted, the next stable internal ID is assigned. Use `operon next-id assembly` to inspect or reserve the next ID.

For sequencing data, add a run:

```bash
operon add run \
  --field sample_id=SMP_000001 \
  --field run_accession=SRR999999999 \
  --field library_strategy=WGS \
  --field library_source=GENOMIC \
  --field library_layout=PAIRED \
  --field platform=ILLUMINA
```

Skip the run when the project contains assemblies without reads.

## View and export metadata

```bash
operon query "SELECT * FROM assemblies"
operon report metadata
```

`report metadata` writes `reports/metadata/*.tsv` and a `manifest.json` containing row counts and SHA-256 checksums. These TSV files are read-only derived snapshots. Editing them does not change SQLite. The snapshot includes `data_sources.tsv` and `source_links.tsv` for sources, citations, licenses, and linked objects. For bulk writes, use the CSV/XLSX template and preview workflow in `operon import table`.

## Archive files

Archive an assembly FASTA:

```bash
operon ingest \
  --source /data/GCA_999999999.fna.gz \
  --entity-type assembly \
  --entity-id ASM_000001 \
  --role genome_fasta \
  --source-url https://ftp.ncbi.nlm.nih.gov/...
```

Example output:

```text
registered FIL_000001 -> raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta.gz (sha256 7b5a0aa0...)
```

The command recognizes `.fna.gz` as gzipped FASTA, calculates SHA-256, copies the file atomically to `raw/assemblies/ASM_000001/`, verifies the archived copy, records it in the `files` manifest, and updates `assemblies.fasta_file_id`.

Archive paired-end reads with two commands:

```bash
operon ingest --source /data/SRR999999999_1.fastq.gz \
  --entity-type run --entity-id RUN_000001 --role reads_r1

operon ingest --source /data/SRR999999999_2.fastq.gz \
  --entity-type run --entity-id RUN_000001 --role reads_r2
```

## Verify the archive

```bash
operon verify
```

A healthy file has status `CHECKSUM_VERIFIED`. Moved, deleted, or modified files are reported as `MISSING` or `CHECKSUM_FAILED`, and the command returns a non-zero exit code.

## Standardize files

```bash
operon standardize
```

The default is `copy`, so raw, standardized, and release files do not share writable inodes. If space is constrained and the risk is understood, explicitly use:

```bash
operon standardize --link hardlink
```

## Run built-in QC

```bash
operon qc
operon qc --entity-type assembly --entity-id ASM_000001
```

FASTQ quality characters default to Phred+33. Use `operon qc --phred-offset 64` only for confirmed legacy Phred+64 data. Duplicate-rate and overrepresented-sequence metrics sample the first 1,000,000 reads by default; use a positive integer `--sample-size` to change that limit.

View and export QC results:

```bash
operon report qc --entity-type assembly
operon report qc --export
```

The export creates `qc/aggregate/qc_results.tsv` and `qc_results.wide.tsv`.

## Import external QC metrics

Prepare an external TSV with these required columns:

```text
entity_type, entity_id, qc_stage, metric_name, metric_value,
tool, tool_version, parameter_set
```

Optional columns are `file_id` and `file_sha256`; when present, they are checked against the manifest. Import the file:

```bash
operon import-qc --file busco_results.tsv
```

## Run BLAST, HMMER, or BUSCO

External programs are configured in `config/tools.yaml`. The default template includes `blastn_nt`, `blastp_nr`, `hmmsearch_pfam`, and `busco_autolineage`; edit the launch method and database paths for the local environment.

```yaml
tools:
  blastn:
    executable: blastn
    run_method: "conda run --no-capture-output -n blast"
    version_args: ["-version"]
    version_pattern: 'blastn:\s*([^\s]+)'
    recipes:
      blastn_nt:
        entity_type: assembly
        file_role: genome_fasta
        database: /data/db/nt
```

Check launch and version detection:

```bash
operon tools-check
```

Run a recipe and inspect the plan first:

```bash
operon analyze --analysis blastn_nt --dry-run
operon analyze --analysis blastn_nt
```

Filter by entity:

```bash
operon analyze --analysis blastn_nt --entity-type assembly
operon analyze --analysis blastn_nt --entity-id ASM_000001
```

View synchronized summaries and top hits:

```bash
operon report analysis --analysis blastn_nt
operon report analysis --analysis blastn_nt --hits
```

BUSCO uses directory output and reads `short_summary*.json` directly:

```bash
operon analyze --analysis busco_autolineage --entity-id ANN_000001 --threads 24 --dry-run
operon analyze --analysis busco_autolineage --entity-id ANN_000001 --threads 24
operon report analysis --analysis busco_autolineage --entity-id ANN_000001
```

Each successful run records the tool, version, full command, input hash, database identity, and output hash. Identical input, parameters, tool version, and database identity hit the cache; `--force` reruns explicitly.

## Evaluate rules

```bash
operon evaluate --profile assembly_production_v1
operon report decisions
```

Example decisions:

```text
entity_type  entity_id   profile                 decision  reasons
assembly     ASM_000001  assembly_production_v1  PASS      -
assembly     ASM_000002  assembly_production_v1  FAIL      LOW_CONTIGUITY
```

Changing a profile and rerunning `evaluate` appends a new decision rather than overwriting the old one. `report decisions` displays the latest decision by default. QC profiles use `kind: qc`; taxonomy coverage profiles use `kind: taxonomy_coverage`.

## Curate a decision

```bash
operon curate \
  --entity-type assembly \
  --entity-id ASM_000002 \
  --profile assembly_production_v1 \
  --decision ACCEPT_WITH_WARNING \
  --reviewer "$USER" \
  --reason "Known low-contiguity reference used only for taxonomy" \
  --evidence "Review record dated 2026-08-16"
```

The automatic decision remains unchanged. Curation updates `curated_*` fields and writes an audit record to `changes`.

## Query SQLite read-only

```bash
operon query "
SELECT f.file_id, f.entity_id AS annotation_id,
       a.assembly_id, s.sample_id, o.organism_id, o.scientific_name
FROM files f
JOIN annotations ann ON ann.annotation_id=f.entity_id
JOIN assemblies a ON a.assembly_id=ann.assembly_id
JOIN samples s ON s.sample_id=a.sample_id
JOIN organisms o ON o.organism_id=s.organism_id
WHERE f.file_role='protein_fasta'
"
```

`query` uses a read-only connection and authorizer. `SELECT` and read-only schema PRAGMA statements are allowed; DML, DDL, write PRAGMA, `ATTACH`, and `VACUUM` are rejected.

## Create a release

```bash
operon release --version 2026.08 --profile assembly_production_v1
```

The default `copy` mode creates a release containing `manifest.tsv`, `exclusions.tsv`, `qc_summary.tsv`, `profile_history.tsv`, `data_sources.tsv`, `source_links.tsv`, `provenance.json`, and `checksums.sha256`.

Verify it:

```bash
cd releases/2026.08
# Linux
sha256sum -c checksums.sha256
# macOS
shasum -a 256 -c checksums.sha256
```
