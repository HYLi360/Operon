# Recipe field reference

## Input selection

### Selection fields

| Field | Default | Meaning |
|---|---|---|
| `entity_type` | empty | Restricts to entity types such as `assembly`, `annotation`, `organism` |
| `file_role` | empty | Must exactly match `files.file_role` in the manifest |
| `format` | empty | Must exactly match `files.format` in the manifest |
| `input_kind` | `directory` when `format: directory`, otherwise `file` | The actual type the input path must have at runtime |

Selection fields and the runtime object type are two independent concepts:

- `file_role` and `format` answer "which manifest record to select";
- `input_kind` answers "what kind of filesystem object that record's path is".

A plain protein FASTA:

```yaml
entity_type: annotation
file_role: protein_fasta
format: fasta
input_kind: file
```

A directory input:

```yaml
entity_type: organism
file_role: other
format: directory
input_kind: directory
```

A directory must first be archived as an artifact:

```bash
operon --project . ingest \
  --source proteome_set/ \
  --entity-type organism \
  --entity-id ORG_000001 \
  --role other \
  --format directory
```

A directory hash is determined by relative paths, empty directories, file sizes and contents, and symlink targets — not by mtime, owner, or permissions. If any file's content, name, or structure inside the directory changes, the manifest hash re-check before the run fails.

### Additional `analyze` filtering

The recipe determines the base candidate set; the command line can narrow it further:

```bash
operon --project . analyze \
  --analysis example_analysis \
  --entity-type annotation \
  --entity-id ANN_000001 \
  --limit 1
```

Command-line filtering never widens the input range allowed by the recipe. For example, when the recipe declares `entity_type: annotation`, passing an assembly ID does not force an assembly to be used as input.

## Output and naming

The output root path for each input has four parts:

```text
<project>/analysis/<output_subdir>/<entity_id>/<artifact_name>
```

Related fields:

| Field | Default | Meaning |
|---|---|---|
| `output_subdir` | recipe name | First-level directory under `analysis/` |
| `output_kind` | `file` | Output must be `file` or `directory` |
| `output_suffix` | `.tsv` for file output; empty for directory output | Only used in the default artifact name |
| `output_name` | empty | Optional single-level name template; when set, overrides the default naming formula |
| `parameters` | empty mapping | Runtime parameters the recipe explicitly allows from the CLI; undeclared parameters are rejected |

Without `output_name`:

```text
artifact_name = <file_id>.<file_role><output_suffix>
```

For example:

```yaml
output_subdir: blastn_nt
output_kind: file
output_suffix: .blastn.tsv
```

For a `genome_fasta` input of `FIL_000001` this yields:

```text
analysis/blastn_nt/ASM_000001/FIL_000001.genome_fasta.blastn.tsv
```

Once `output_name` is set, neither the default formula nor `output_suffix` participates in the final name:

```yaml
output_subdir: busco
output_kind: directory
output_name: ${file_id}.busco
```

Yielding:

```text
analysis/busco/ANN_000001/FIL_000003.busco/
```

`output_name` must render to a safe path component: not an empty string, `.`, `..`, an absolute path, or a nested path containing `/`. For hierarchy, use `output_subdir` and the `entity_id` level added automatically by the system.

The external program must create exactly the `${output}` computed by the recipe. A file must be non-empty; a directory must exist, have the correct type, and contain results. Before a new run that missed the cache, this exact target artifact is safely deleted so that leftover files from a previous run are not mistaken for this run's success — but no other path under `analysis/` is touched.

## Parameters and placeholders

### Declaring safe runtime parameters

When a recipe needs a value chosen per run (for example a BUSCO lineage), do not allow arbitrary arguments to be appended to the command. The recipe must first declare the name, requirement, and constraints through `parameters`:

```yaml
parameters:
  lineage_dataset:
    description: BUSCO lineage dataset name
    required: true
    pattern: '[A-Za-z0-9][A-Za-z0-9_.-]*'
```

Supported parameter constraints:

| Field | Meaning |
|---|---|
| `description` | Human-readable description |
| `required` | Whether absence without a default is an error |
| `default` | Optional default value |
| `pattern` | Regex the entire value must match |
| `choices` | Optional list of allowed values |

At runtime, use the repeatable `--param NAME=VALUE`:

```bash
operon analyze --analysis busco_lineage \
  --param lineage_dataset=fabales_odb12.2
```

Runtime parameters can be used as placeholders like `${lineage_dataset}` in `arguments` and `output_name`. Parameter values enter the argument fingerprint; the command fails before launching the external program if a parameter is undeclared, a required value is missing, a value violates pattern/choices, a value is passed twice, or unresolved placeholders remain.

A recipe with runtime parameters does not use cross-fingerprint "output adoption": only an identical parameter fingerprint can hit the cache. This prevents an existing output for one lineage from being treated as an equivalent result for another.

`arguments` is an argument array, not a shell command string. Each YAML list item corresponds to one argv element:

```yaml
arguments:
  - --input
  - ${input}
  - --label
  - "sample with spaces"
```

Here `sample with spaces` is a single argument and is not split again on spaces. Conversely, the following is also a single argument and is not automatically split into `--cpu` and `24`:

```yaml
# Wrong, unless the target program really requires one space-containing argument
- "--cpu ${threads}"
```

### Placeholders available in `arguments`

| Placeholder | Rendered content |
|---|---|
| `${input}` | Absolute path of the input file or directory |
| `${input_parent}` | Absolute path of the input's parent directory |
| `${input_name}` | Basename of the input artifact, including extension |
| `${input_stem}` | Input stem in the `Path.stem` sense; only the last suffix is removed |
| `${output}` | Computed absolute path of the output file or directory |
| `${output_parent}` | Absolute path of the output artifact's parent directory |
| `${output_name}` | Basename of the output artifact |
| `${output_stem}` | Output stem in the `Path.stem` sense |
| `${database}` | Resolved absolute path of the database or shared cache; empty string when not configured |
| `${threads}` | CLI `--threads` or the project default thread count |
| `${work_dir}` | Deterministic per-run scratch directory for intermediate artifacts; defined only in `commands` chains (see below) |
| `${file_id}` | Stable file ID of the current input |
| `${file_role}` | Current manifest file role |
| `${entity_type}` | Current entity type |
| `${entity_id}` | Current entity ID |
| `${<parameter>}` | Runtime parameter declared in `parameters` and resolved |

Placeholders can be embedded inside an argument:

```yaml
- --prefix=${file_id}
```

But shell environment variables, `~`, globs, and command substitution are not expanded. Paths need no manual shell quoting because `operon` passes the argv array directly.

### Placeholders available in `output_name`

`output_name` is rendered before the full output path is established, so it supports only:

```text
${file_id}
${file_role}
${entity_type}
${entity_id}
${input_name}
${input_stem}
${<parameter>}
```

It cannot reference `${output}`, `${output_parent}`, or `${output_name}` itself. `output_name` is not rendered when the configuration is loaded; unrecognized placeholders are reported when the recipe is rendered, i.e. when `analyze` runs (including `--dry-run`).

### Command chains (`commands`)

Some tools are really two programs in sequence — for example `rpsblast` emits an ASN.1 archive (`-outfmt 11`) that `rpsbproc` then renders into the tabular report. A recipe can declare such a pipeline with `commands`, a non-empty list of command blocks where each block carries its own `arguments` list:

```yaml
commands:
  - arguments:
      - rpsblast
      - -query
      - ${input}
      - -db
      - ${database}
      - -out
      - ${work_dir}/hits.asn
      - -outfmt
      - "11"
  - arguments:
      - rpsbproc
      - -i
      - ${work_dir}/hits.asn
      - -o
      - ${output}
    version_args: [-version]
    version_pattern: 'rpsbproc:\s*([^\s]+)'
```

- `commands` and the single-command `arguments` field are mutually exclusive on the same recipe.
- Each block's `arguments` is rendered with the same placeholders as a single command, plus `${work_dir}`: a deterministic scratch directory named `<output_name>.work` next to the output artifact. It is deleted and recreated before the run, and removed again after the run finishes (or fails); `analyze --keep-partial` keeps it for debugging. The path is deterministic because the rendered commands enter the cache fingerprint.
- Steps run in order through the same executor (the parent tool's `run_method` prefix applies to every step) under one `analysis_jobs` row. The first step with a non-zero exit code aborts the chain and fails the whole job; the error message names the failing step (`step N/M failed: ...`).
- A command block may declare `version_args` (a non-empty argument list appended to that block's executable) and `version_pattern` (an optional extraction regex). The probe runs through the same launcher and executor. A step whose executable equals the parent tool's `executable` inherits the already-probed tool version when it has no command-level probe. Any other unconfigured executable is recorded as version `unknown`; `operon` never guesses a version flag.
- Each step gets its own logs, `logs/<run_id>.step<N>.stdout.log` / `.stderr.log`. Its `argv`, `executable`, `tool_version`, `tool_version_raw`, `version_source`, `version_command`, and `exit_code` are recorded in the run's `execution_details.steps`.
- Only the last step is expected to produce `${output}`; the non-empty check, content hash, and result parsing run once after the chain completes.
- The rendered `commands` and their version-probe declarations are preserved in the recipe snapshot. Step-level version collection is provenance-only and does not change the current cache policy or fingerprint.

A complete `rpsblast` + `rpsbproc` recipe appears in [Result parsers and examples](recipe-parsers-examples.md).

The `commands` system is NOT intended to replace Snakemake or Nextflow, but rather to bundle tools that are frequently used together
to reduce repetitive work, and avoid using the “bulky” Snakemake or Nextflow in such scenario. We have intentionally imposed the following hard constraints on the `commands`:

- Each program must use a shared runtime environment. For example, if you run the RPS-BLAST recipe using Conda, you must have both NCBI-BLAST+ and `rpsbproc` installed in the Conda
environment.
- Because executing command chains introduces uncertainty, recipes that use `commands` cannot benefit from cache hits.

## Databases and cache directories

| Field | Default | Meaning |
|---|---|---|
| `database` | empty | Database file, database directory, or shared tool download directory; relative paths resolve against the project root |
| `database_version` | empty | Human-readable logical version; also participates in the database cache identity |
| `database_checksum` | empty | Optional explicit SHA-256 identity, suitable for frozen large databases |
| `database_mode` | `reference` | `reference` or `mutable_cache` |

### `reference`

For databases that must not change during analysis:

```yaml
database: /data/db/Pfam-A.hmm
database_version: "37.0"
database_mode: reference
```

A single file is identified by content SHA-256 by default; a directory uses a fast directory fingerprint over relative paths, sizes, and mtimes. For large directory databases that must be strictly reproducible, provide the publisher's checksum explicitly:

```yaml
database_checksum: 0123456789abcdef...
```

### `mutable_cache`

For shared directories such as BUSCO's, which gradually download lineages at runtime:

```yaml
database: resources/busco_downloads
database_version: odb12
database_mode: mutable_cache
```

The directory is created automatically before the actual run. Its identity is determined by path, the explicit `database_version`, and an optional checksum — downloading another lineage later does not invalidate the cache of all older BUSCO jobs. `mutable_cache` requires a non-empty `database_version`.

If the goal is strict freezing and offline reproduction, pre-download the chosen lineage, switch BUSCO to `--lineage_dataset ... --offline`, and then use `reference` mode with a maintained version or checksum.

### Databases on SSH remotes

When SSH uses a non-empty `remote_root`, paths under the local project root in `${database}` are mapped to the remote root; absolute paths outside the project root are kept as-is. `operon` never uploads large reference databases with every job:

- `reference` must be placed at the remote target path by an administrator in advance, with `database_checksum` configured; path existence is checked before the run. The explicit checksum enters the database cache identity together with the SSH host/root;
- `mutable_cache` must have a `database_version`, and the target directory is created over SFTP when missing;
- A database existing locally under the same name does not mean it is deployed remotely, and vice versa; a missing database is reported clearly before the analysis is submitted;
- Different SSH hosts/roots do not share analysis cache identity, avoiding cross-cluster reuse of results when content location is unclear.

Here `database_checksum` is the recipe's explicit declaration of a frozen database's published identity. For reference databases that need byte-level auditing, additionally run the publisher's verification or generate an Operon-verifiable manifest at deployment time; the runtime does not repeatedly traverse multi-terabyte databases for every candidate input.

## Result parsing and alignment columns

`result_parser` selects how a successful output enters SQLite: `none`, `blast_tabular`, `hmmer_tblout`, `hmmer_domtblout`, `rpsbproc_tabular`, or `busco_json`. Per-parser semantics and complete examples live in [Result parsers and examples](recipe-parsers-examples.md); this section defines the field contract.

### Tabular column fields

For `blast_tabular`:

| Field | Default | Meaning |
|---|---|---|
| `result_columns` | none (required) | Column names in the exact order the external program emits them |
| `hit_metric_columns` | all columns after the first two | Columns synced as EAV hit metrics into `analysis_hits` |
| `numeric_columns` | same as `hit_metric_columns` | Subset of hit metrics parsed as numbers |
| `query_column` | first column | Query ID column |
| `subject_column` | second column | Subject ID column |
| `max_hits_per_query` | `5` | EAV hit rows kept per query in `analysis_hits` |

### Alignment column mapping keys

Seven optional keys map columns of `result_columns` onto the structured `analysis_alignments` fields:

| Field | Recognized by default | Structured field | Type |
|---|---|---|---|
| `qstart_column` | `qstart` | `query_start` | integer |
| `qend_column` | `qend` | `query_end` | integer |
| `sstart_column` | `sstart` | `subject_start` | integer |
| `send_column` | `send` | `subject_end` | integer |
| `evalue_column` | `evalue` | `evalue` | float |
| `bitscore_column` | `bitscore` | `bitscore` | float |
| `pident_column` | `pident` | `percent_identity` | float |

When a key is absent, the parser looks for the default common name in `result_columns`; declare the key explicitly when the tool uses a different header (see the rpsblast example in [Result parsers and examples](recipe-parsers-examples.md)); a declared value that matches no column falls back to the default common names. Structured alignment rows are always written to `analysis_alignments` in full — `max_hits_per_query` truncates only the EAV `analysis_hits` rows. Columns of `result_columns` not mapped to a structured field are preserved verbatim in the alignment row's `extra_json`. A mapping key enters the parameter fingerprint only when it is actually set, so adding a key invalidates the completed cache exactly once.

### `hmmer_domtblout`

`hmmer_domtblout` parses HMMER `--domtblout` per-domain rows and needs no column declarations: the query is the HMM profile name, the subject is the target sequence, the per-domain i-Evalue and domain score become `evalue`/`bitscore`, and the alignment coordinates land in `query_start`/`query_end` (HMM and envelope coordinates go to `extra_json`; subject coordinates are not present in domtblout and stay NULL). It writes both EAV hits and full structured alignment rows. The older `hmmer_tblout` parser reads only `--tblout`, which carries no coordinates, so it never writes `analysis_alignments` rows — prefer `--domtblout` for new recipes.

### `rpsbproc_tabular`

`rpsbproc_tabular` parses the tabular report produced by NCBI `rpsbproc` (the `DATA`/`SESSION`/`QUERY`/`DOMAINS` structure) and needs no column declarations. Each domain row has 12 columns (session, query id, hit type, PSSM id, from, to, e-value, bitscore, accession, short name, incomplete, superfamily PSSM id). The alignment `query_id` is the QUERY definition line and `subject_id` is the accession; `from`/`to` become `query_start`/`query_end`, e-value and bitscore are parsed as numbers, and the remaining fields (`hit_type`, `pssm_id`, `short_name`, `incomplete`, `superfamily_pssm`, `session`, `rps_query_id`) are preserved in `extra_json`. EAV hits are truncated to `max_hits_per_query` per query as usual, while `analysis_alignments` keeps every domain row; queries without any domain produce no rows. It is the intended parser for `commands` chains that pipe `rpsblast -outfmt 11` into `rpsbproc` (see "Command chains" above).

## Cache identity

A completed analysis is reused only when all of the following identity components are identical:

```text
analysis name
+ file_id
+ input content hash
+ rendered arguments
+ resolved runtime parameters
+ threads
+ tool version
+ parser/output-related recipe settings
+ database identity
```

After a database record hits, `operon` also checks that the output artifact still exists and recomputes the file or directory hash against the recorded value. If the output was deleted or modified, the old job is marked `superseded` and re-executed.

The second-level continuation when the exact identity misses (verified-output adoption): if an old `completed` job exists for the same `(analysis, file_id)` whose input content hash matches the current one, and whose recorded output artifact is still on disk with a byte-identical hash, `operon` does not recompute. Instead it adopts that output into the current fingerprint — inserting a new `completed` row pointing at the same output under the current parameter fingerprint/database identity (linked to the original `workflow_run_id`), recording the adoption reason in the `changes` audit table, and marking the file as `adopted`. This covers scenarios such as software upgrades changing the fingerprint formula, or recipe renames. Outputs that were modified, or inputs whose content changed, are not adopted and are recomputed as usual. Adoption applies only to completed results with verified outputs; `--force` semantics are unchanged and always recompute. In dry-run output, the status column shows `cached`/`adoptable`/`planned`, meaning a completed-cache hit, the adoption path, and actual execution respectively; under `--force`, even a cache that would have hit shows as `planned`. Recipes declaring runtime parameters disable second-level adoption and allow only exact cache hits.

`--force` only means "ignore an otherwise valid completed cache". It preserves the historical job record, marks the old record `superseded`, deletes the exact old output target, and creates a new job. It cannot fix wrong parameters, a wrong output name, or failures of the external program itself.

Checking selection, command, and cache with a dry run first is the safest approach:

```bash
operon --project . analyze \
  --analysis busco_autolineage \
  --entity-id ANN_000001 \
  --threads 24 \
  --dry-run
```

## Slurm resource overrides

When the project uses the Slurm execution backend (`execution.backend: slurm` in `project.yaml`, or `--backend slurm` on the command line), all recipes share the resource settings of `execution.slurm` by default. An individual recipe can override same-named fields with a `slurm:` mapping (empty values and empty strings do not override) — for example, adjusting memory and time limit for BUSCO alone:

```yaml
tools:
  busco:
    executable: busco
    run_method: ""
    version_args: ["--version"]
    version_pattern: 'BUSCO\s+([^\s]+)'
    recipes:
      busco_autolineage:
        # ... other fields unchanged ...
        slurm:
          mem_gb: 64
          time: "72:00:00"
```

Overridable fields match `execution.slurm`:

| Field | Default | Meaning |
|---|---|---|
| `partition` | empty | Slurm partition; empty means no `--partition` is written |
| `time` | `24:00:00` | Job time limit |
| `mem_gb` | `0` | Memory limit (GB); `0` means no `--mem` is written |
| `extra_sbatch` | `[]` | Extra `#SBATCH` lines, e.g. `["--gres=gpu:1"]` |
| `setup_commands` | `[]` | Lines inserted before the command, e.g. `["module load blast/2.15"]` |
| `poll_interval` | `15` | `squeue` polling interval (seconds); honored fully by both local and remote Slurm (only clamped to a 0.1-second floor) |

Unlisted fields inherit from `execution.slurm`. The thread count always comes from `--threads` (mapped to `--cpus-per-task`) and is not recipe-overridable.
