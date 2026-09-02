# External Analysis

External programs are declared in `config/tools.yaml`; command lines are not assembled manually for each run. New projects receive the file from `operon init`. Older projects create a missing file on the first `tools-check` or `analyze` run without overwriting an existing configuration.

The default template contains `blastn_nt`, `blastp_nr`, `hmmsearch_pfam`, `busco_autolineage`, and `busco_lineage`. Edit launch methods and database paths for the local environment. For the complete execution model, field contract, placeholders, cache identity, parser options, and new-tool checklist, see the [Recipe Configuration Model](../reference/recipe-overview.md).

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
| `parameters` | Runtime parameters allowed through `analyze --param NAME=VALUE`. |
| `result_parser` | `blast_tabular`, `hmmer_tblout`, `busco_json`, or `none`. |
| `result_glob` | Result glob inside a directory output; BUSCO usually uses `short_summary*.json`. |
| `max_hits_per_query` | Maximum hits per query synchronized to SQLite. |

Placeholders include `${input}`, `${output}`, `${database}`, `${threads}`, `${input_parent}`, `${input_name}`, `${input_stem}`, `${output_parent}`, `${output_name}`, `${output_stem}`, `${file_id}`, `${file_role}`, `${entity_type}`, and `${entity_id}`.

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
- stdout and stderr are saved to `logs/<WF_ID>.stdout.log` and `.stderr.log`.
- Run records are written to `logs/workflow.jsonl` and `workflow_runs`.
- The run is `completed` only when the exit code is 0 and every `--expected-output` exists and is non-empty; otherwise it is `failed` and the command exits non-zero.
