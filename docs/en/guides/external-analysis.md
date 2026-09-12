# External Analysis

External programs are declared in `config/tools.yaml`; command lines are not assembled manually for each run. New projects receive the file from `operon init`. Older projects create a missing file on the first `tools-check` or `analyze` run without overwriting an existing configuration.

The default template contains `blastn_nt`, `blastp_nr`, `hmmsearch_pfam`, `busco_autolineage`, `busco_lineage`, and the command-chain `rpsblast_cdd`. Edit launch methods and database paths for the local environment. For the complete execution model, field contract, placeholders, cache identity, parser options, and new-tool checklist, see the [Recipe Configuration Model](../reference/recipe-overview.md).

## Configure program launch

Run a program from `PATH`:

```yaml
tools:
  blastn:
    executable: blastn
    run_method: ""
    version_args: ["-version"]
    version_pattern: 'blastn:\s*([^\s]+)'
```

Use a conda environment:

```yaml
tools:
  blastn:
    executable: blastn
    run_method: "conda run --no-capture-output -n blast"
```

Absolute conda paths, containers, and other prefixes are supported:

```yaml
run_method: "/opt/conda/bin/conda run --no-capture-output -n blast"
run_method: "singularity exec /data/images/blast.sif"
```

Key recipe fields:

| Field | Meaning |
|---|---|
| `entity_type` / `file_role` / `format` | Selects input artifacts from the manifest. Use `format: directory` for directories. |
| `input_kind` | `file` (default) or `directory`; the actual type and content hash are checked before execution. |
| `output_kind` | `file` (default) or `directory`; both support non-empty checks, content hashing, and cache validation. |
| `output_subdir` / `output_suffix` | Controls the default `analysis/<recipe>/<entity_id>/<file_id>.<role><suffix>` path. |
| `output_name` | Optional single-component name template. For BUSCO, use `${file_id}.busco` to avoid the SEPP `fasta` path replacement defect. |
| `database` | Reference database or shared download-cache path. Relative paths resolve from the project root. |
| `database_version` | Logical database version; participates in cache identity. |
| `database_checksum` | Optional explicit checksum for strict database identity. |
| `database_mode` | `reference` (default, content-based identity) or `mutable_cache` (shared growing cache; requires `database_version`). |
| `arguments` | Command arguments and placeholders. |
| `commands` | Ordered multi-step command chain (mutually exclusive with `arguments`); steps share the deterministic `${work_dir}` scratch directory. |
| `parameters` | Runtime parameters allowed through `analyze --param NAME=VALUE`. |
| `result_parser` | `blast_tabular`, `hmmer_tblout`, `hmmer_domtblout`, `rpsbproc_tabular`, `busco_json`, or `none`. |
| `result_glob` | Result glob inside a directory output; BUSCO usually uses `short_summary*.json`. |
| `max_hits_per_query` | Maximum hits per query synchronized to SQLite. |
| `version` | Optional positive integer (default 1, invalid values rejected); together with the configuration content it enters the recipe snapshot recorded by `analyze`, inspectable via `operon recipes history/show`. |

Placeholders include `${input}`, `${output}`, `${database}`, `${threads}`, `${input_parent}`, `${input_name}`, `${input_stem}`, `${output_parent}`, `${output_name}`, `${output_stem}`, `${file_id}`, `${file_role}`, `${entity_type}`, and `${entity_id}`. Recipes with a `commands` chain additionally get `${work_dir}`, a deterministic per-run scratch directory for intermediate artifacts that is rebuilt before the run and cleaned up afterwards.

All command blocks use the parent tool's `run_method`. A block invoking another program can add `version_args` and `version_pattern`; that program's version is then recorded in `workflow_runs.execution_details.steps` alongside the command argv and exit code.

A directory can be archived as an input artifact:

```bash
operon ingest --source proteome_set/ --entity-type organism \
  --entity-id ORG_000001 --role other --format directory
```

Check configuration and versions:

```bash
operon tools-check
```

## Run BLAST and HMMER

After setting the database paths:

```bash
# Run blastn for genome FASTA files from all assemblies.
operon analyze --analysis blastn_nt

# Restrict to one assembly.
operon analyze --analysis blastn_nt --entity-id ASM_000001

# Run blastp for protein FASTA files from all annotations.
operon analyze --analysis blastp_nr

# Run hmmsearch against Pfam-A.hmm.
operon analyze --analysis hmmsearch_pfam
```

Preview selection and cache status:

```bash
operon analyze --analysis blastn_nt --dry-run
```

Useful batch controls: `--limit N` processes only the first N matching files (in `file_id` order), and `--threads` overrides the recipe default.

Inspect synchronized results:

```bash
operon report analysis --analysis blastn_nt
operon report analysis --analysis blastn_nt --hits
```

Results are written to:

- Full output artifact: `analysis/<recipe>/<entity_id>/<FIL_ID>.<role><output_suffix>`
- `analysis_jobs`: command, tool version, parameter fingerprint, input/database fingerprints, output hash, and status
- `analysis_results`: `query_count`, `hit_count`, `query_with_hit_count`, `best_evalue`
- `analysis_hits`: query, subject, metric values, and rank for top hits
- `analysis_alignments`: every parsed hit as a structured row (query/subject IDs, rank, intervals, e-value, bitscore, percent identity) for parsers with coordinates, never truncated by `max_hits_per_query`
- `qc_results`: summary metrics under stage `analysis:<recipe>`

The cache key consists of analysis name, `file_id`, input SHA-256, parameter fingerprint, tool version, and database identity. Matching jobs are skipped unless `--force` is used. If the exact fingerprint misses but an old completed job has the same input and a verified output, the output is adopted under the current fingerprint and audited as `adopted`.

Input SHA-256 or directory tree hash is rechecked against the manifest before every run. Modified raw input is rejected and is not passed to the external program.

## Run BUSCO natively and parse JSON summaries

The default `busco_autolineage` recipe uses protein FASTA input and directory output. BUSCO `-o` is a short run name, so the recipe passes `${output_name}`; `--out_path` receives `${output_parent}`. BUSCO then creates exactly `${output}`.

```yaml
tools:
  busco:
    executable: busco
    run_method:
      mode: conda
      bin: mamba
      env: busco_6.1.0
    version_args: ["--version"]
    version_pattern: 'BUSCO\s+([^\s]+)'
    recipes:
      busco_autolineage:
        description: BUSCO auto-lineage in protein mode
        entity_type: annotation
        file_role: protein_fasta
        format: fasta
        input_kind: file
        database: resources/busco_downloads
        database_version: odb12
        database_mode: mutable_cache
        output_subdir: busco
        output_kind: directory
        output_name: ${file_id}.busco
        arguments:
          - -m
          - protein
          - -i
          - ${input}
          - -o
          - ${output_name}
          - --out_path
          - ${output_parent}
          - --download_path
          - ${database}
          - -c
          - ${threads}
          - --auto-lineage
          - --opt-out-run-stats
          - --tar
        result_parser: busco_json
        result_glob: short_summary*.json
```

For strict reproducibility, download and freeze the required lineage datasets in advance, remove `--auto-lineage`, set `--lineage_dataset`, add `--offline`, and use `database_mode: reference`. Update `database_version` or `database_checksum` when the data changes. A `mutable_cache` identity is based on path, explicit version, and optional checksum; downloading another lineage later does not invalidate older jobs.

Run and inspect BUSCO:

```bash
operon tools-check
operon analyze --analysis busco_autolineage --entity-id ANN_000001 --threads 24 --dry-run
operon analyze --analysis busco_autolineage --entity-id ANN_000001 --threads 24
operon report analysis --analysis busco_autolineage --entity-id ANN_000001
```

The output directory resembles:

```text
analysis/busco/ANN_000001/FIL_000003.busco/
```

`busco_json` selects a unique `short_summary.specific.*.json` from `result_glob`. If several specific summaries match, the parser rejects the ambiguous result; narrow the glob. Parsed metrics include complete/single-copy/duplicated/fragmented/missing percentages and counts, marker count, domain, lineage dataset, dataset date, OrthoDB/dataset versions, species count, NCBI taxid, and BUSCO report version. They are written to both `analysis_results` and `qc_results`.

### Fixed lineages and coexisting results

For a clade-specific check, use `busco_lineage`:

```bash
operon analyze --analysis busco_lineage \
  --entity-id ANN_000001 \
  --threads 24 \
  --param lineage_dataset=fabales_odb12.2
```

`lineage_dataset` must be declared by the recipe `parameters` section and pass its pattern. It cannot inject arbitrary command arguments. The lineage enters the output name and cache fingerprint, so multiple fixed-lineage results coexist:

```text
analysis/busco_lineage/ANN_000001/FIL_000003.fabales_odb12.2.busco/
analysis/busco_lineage/ANN_000001/FIL_000003.eudicotyledons_odb12.2.busco/
```

The QC long table stores lineage-specific stages. The wide table can hold only one column per metric and therefore shows the latest value. Formal profiles should set `source.qc_stage`; the default BUSCO QC profile binds `analysis:busco_autolineage` and is not silently changed by later fixed-lineage runs.

## Run another external tool with provenance

```bash
operon run-external \
  --step quast \
  --parameter-set quast_v1 \
  --entity-type assembly \
  --entity-id ASM_000001 \
  --expected-output qc/assemblies/ASM_000001/quast/report.tsv \
  --command 'quast -o qc/assemblies/ASM_000001/quast raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta'
```

- `--command` is parsed with shell-style quoting but is not run through a shell.
- `--tool` records a tool version probed from `config/tools.yaml`; `--input` declares an input file/directory that is hashed for provenance (repeatable); `--threads`, `--cwd`, `--timeout`, and `--backend` (default `local`; also `slurm` or `ssh`) control execution.
- stdout and stderr are saved to `logs/<WF_ID>.stdout.log` and `.stderr.log`.
- Run records are written to `logs/workflow.jsonl` and `workflow_runs`.
- The run is `completed` only when the exit code is 0 and every `--expected-output` exists and is non-empty; otherwise it is `failed` and the command exits non-zero.

## Sequence selection and domain extraction

Analyses whose parser writes structured rows to `analysis_alignments` (`blast_tabular`, `hmmer_domtblout`, `rpsbproc_tabular`) can drive sequence-level subsetting without re-parsing tool output. Two commands materialize FASTA subsets with provenance; the products re-enter the manifest through `adopt`.

Control completeness with e-value thresholds, not max-hit limits. A `max_target_seqs`-style cutoff silently drops true hits from the tool's output, and any later "sequences without a hit" selection is then wrong. Keep the tool's hit list complete (raise or drop the max-hits limit in the recipe) and encode stringency in selection-time thresholds such as `--evalue-max`: `analysis_alignments` stores the full parsed hit set, so thresholds stay re-adjustable without re-running the analysis.

`select-sequences` writes the subset of one manifest FASTA whose sequences have (or lack) matching hits:

```bash
# Proteins with a Specific CDD hit at e-value <= 1e-5:
operon select-sequences --file-id FIL_000003 \
  --analysis rpsblast_cdd --hit-type Specific --evalue-max 1e-5 \
  --out analysis/external/cdd_positives.faa \
  --manifest analysis/external/cdd_positives.tsv

# The complement — sequences with no matching hit:
operon select-sequences --file-id FIL_000003 \
  --analysis rpsblast_cdd --hit-type Specific --evalue-max 1e-5 \
  --require-no-hit \
  --out analysis/external/cdd_negatives.faa \
  --manifest analysis/external/cdd_negatives.tsv
```

Filter semantics: a repeated `--analysis` is OR-ed (a hit in any listed analysis counts, e.g. blast OR hmmsearch); `--subject-like`, `--evalue-max`, `--min-span`, and `--hit-type` are AND-ed. An intersection across analyses (blast AND hmmsearch) takes two runs: select with the first analysis, `adopt` the subset, then select again from the adopted file with the second analysis. For taxon-specific thresholds, run the analysis in per-taxon batches and restrict which alignments count with `--entity-type`/`--entity-id`.

`extract-domains` cuts the hit intervals themselves out of a FASTA, with flanks and boundary truncation:

```bash
operon extract-domains --file-id FIL_000003 \
  --analysis rpsblast_cdd --subject-like 'bhlh%' --evalue-max 1e-5 \
  --flank 5 --min-length 30 --best-only \
  --out analysis/external/bhlh_domains.faa \
  --manifest analysis/external/bhlh_domains.tsv
```

`--best-only` (default) keeps the best-evalue region per query; `--all-regions` emits every region with `<seqid>|region:<start>-<end>` headers. The manifest TSV records every candidate region, including exclusions and their reasons. Regions can also come from an external coordinate table (`--regions-tsv` with columns `seqid,start,end`) instead of an analysis.

Both commands write a `workflow_runs` provenance step with the full command line but never register their output themselves. Adopt the product explicitly so downstream recipes can select it as input:

```bash
operon adopt --file analysis/external/bhlh_domains.faa \
  --entity-type annotation --entity-id ANN_000001 \
  --role bhlh_domain_fasta --format fasta --derived-from FIL_000003
```

The full flag contract is in the reference: [extract-domains and select-sequences](../reference/cli-analysis.md).

## Re-register external workflow outputs (adopt)

`operon export` materializes the selected entities as an input-side manifest; after a workflow manager such as snakemake/nextflow consumes it, `operon adopt` registers the derived artifacts back into the database. Adopted files enter the `files` manifest, become eligible for QC, evaluate, export, and release, and can be selected as inputs by later recipes through `entity_type + file_role + format`, enabling cascading analysis.

A single artifact:

```bash
operon adopt \
  --file analysis/external/ASM_000001/megahit/final.contigs.fa \
  --entity-type assembly --entity-id ASM_000002 \
  --role megahit_contigs --format fasta \
  --derived-from FIL_000001
```

Batch mode lets a workflow re-register all outputs at the end of a rule. The manifest can be JSON (a list of dicts):

```json
[
  {
    "path": "analysis/external/ASM_000002/megahit/final.contigs.fa",
    "entity_type": "assembly",
    "entity_id": "ASM_000002",
    "role": "megahit_contigs",
    "format": "fasta",
    "derived_from": ["FIL_000001"]
  }
]
```

or a TSV with a header row (the `format`, `compression`, and `workflow_run_id` columns are optional; the `derived_from` column carries comma-separated file_ids):

```text
path	entity_type	entity_id	role	format	derived_from
analysis/external/ASM_000002/megahit/final.contigs.fa	assembly	ASM_000002	megahit_contigs	fasta	FIL_000001,FIL_000004
```

```bash
operon adopt --from-manifest adopt_manifest.json
```

- Each item requires `path`, `entity_type`, `entity_id`, `role`, and `derived_from` (at least one already-registered file_id); relative paths resolve from the project root.
- Artifacts are materialized under `analysis/adopted/<entity_id>/`; same entity and role with identical bytes is reused idempotently, different bytes raise `ConflictError`. The whole batch is preflighted, then registered in one transaction. A failure before commit rolls back metadata, lineage, state and workflow rows and removes newly created artifacts; existing files are preserved. Resolve conflicting occupied targets explicitly before retrying. Completed JSONL records are written only after commit.
- Roles are freely named by the workflow; lineage edges are written to the `file_lineage` table and can be audited with `operon query`.

## Reconstruct a captured Conda environment

Run the analysis using an explicit `conda run`, `mamba run` or `micromamba run`
launcher. Find its `environment_id` in `operon workflow show RUN_ID --format json`
or list stored snapshots, then inspect the capture status and limitations:

```bash
operon environments list
operon environments show ENVIRONMENT_ID
operon environments export ENVIRONMENT_ID > explicit.txt
micromamba create -p ./restored-env --file explicit.txt
# Alternatively: conda create -p ./restored-env --file explicit.txt
```

Use a new prefix on the same compatible OS/architecture. Available package caches
can support offline reconstruction (`micromamba create --offline ...`); long-term
reconstruction requires retaining package archives or accessible channels. The
export does not bundle those archives. Private URLs have credentials removed.

Environment documents are redacted before they are stored: the hostname is kept
only as a truncated SHA-256 token and home-directory prefixes in path values are
collapsed to `~`. A published or exported project therefore carries no readable
hostname and no user home paths in its environment records, and these documents
can be shared with a release without extra scrubbing. Records captured before
redaction was introduced are not rewritten and may still contain the original
values.

Run the same small input and arguments through the restored environment using
`operon run-external`, then compare the stored `conda.package_fingerprint`,
`system_fingerprint` and `hardware_fingerprint`, as well as the actual output
checksums (or an explicitly chosen numeric tolerance). The independent package
fingerprint excludes the installation prefix. A matching package inventory does
not prove that manually edited installed files or pip packages have been restored.

Environment capture does not change existing cache reuse or automatic adoption.
Use `analyze --force` when testing an actual recomputation; `run-external` also
executes the command directly. Missing fingerprints in older runs do not imply
an environment match.
