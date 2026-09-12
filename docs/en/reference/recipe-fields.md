# Recipe field reference

## Input selection

### Selection fields

| Field | Default | Meaning |
|---|---|---|
| `entity_type` | empty | Restricts to entity types such as `assembly`, `annotation`, `organism` |
| `file_role` | empty | Must exactly match `files.file_role` in the manifest |
| `file_role_prefix` | empty | Plain character-prefix match on `files.file_role`; mutually exclusive with `file_role` |
| `format` | empty | Must exactly match `files.format` in the manifest |
| `input_kind` | `directory` when `format: directory`, otherwise `file` | The actual type the input path must have at runtime |

Selection fields and the runtime object type are two independent concepts:

- `file_role` and `format` answer "which manifest record to select";
- `input_kind` answers "what kind of filesystem object that record's path is".

`file_role` and `file_role_prefix` are mutually exclusive. The prefix is a plain character prefix, not a pattern — wildcard characters (`%`, `*`, `?`) are rejected when the recipe loads — so `file_role_prefix: "subfamily_alignment:"` selects `subfamily_alignment:SF01`, `subfamily_alignment:SF02`, and so on. Its typical use is selecting the per-unit files materialized by `operon fanout`, whose count is data-dependent and therefore cannot be enumerated as exact roles. Each selected file still has a unique entity + role identity, so the manifest conflict invariants are unaffected.

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

A recipe that resolves at least one runtime parameter does not use cross-fingerprint "output adoption": only an identical parameter fingerprint can hit the cache. This prevents an existing output for one lineage from being treated as an equivalent result for another. A recipe that declares parameters but resolves none of them — no `required`, no `default`, and no `--param` supplied — still gets adoption.

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
      - build-index
      - --input
      - ${input}
      - --index
      - ${work_dir}/index.bin
  - arguments:
      - search
      - --index
      - ${work_dir}/index.bin
      - --output
      - ${output}
    version_args: [--version]
    version_pattern: 'search\s+([^\s]+)'
```

- `commands` and the single-command `arguments` field are mutually exclusive on the same recipe.
- Each block's `arguments` is rendered with the same placeholders as a single command, plus `${work_dir}`: a deterministic scratch directory named `<output_name>.work` next to the output artifact. It is deleted and recreated before the run, and removed again after the run finishes (or fails); `analyze --keep-partial` keeps it for debugging. The path is deterministic because the rendered commands enter the cache fingerprint.
- Steps run in order through the same executor (the parent tool's `run_method` prefix applies to every step) under one `analysis_jobs` row. The first step with a non-zero exit code aborts the chain and fails the whole job; the error message names the failing step (`step N/M failed: ...`).
- The first command is the recipe's logical owner: the `tool_version` recorded on the job — and mixed into the cache identity — is the first command's version. It comes from the first block's own `version_args`/`version_pattern` when declared, otherwise from the parent tool's probe, so recipe loading rejects a chain whose first command's executable differs from the tool's `executable` unless the first block declares its own probe. Any later block may also declare `version_args` (a non-empty argument list appended to that block's executable) and `version_pattern` (an optional extraction regex); the probe runs through the same launcher and executor. A step whose executable equals the parent tool's `executable` inherits the already-probed tool version when it has no command-level probe. Any other unconfigured executable is recorded as version `unknown`; `operon` never guesses a version flag.
- Each step gets its own logs, `logs/<run_id>.step<N>.stdout.log` / `.stderr.log`. Its `argv`, `executable`, `tool_version`, `tool_version_raw`, `version_source`, `version_command`, and `exit_code` are recorded in the run's `execution_details.steps`.
- Only the last step is expected to produce `${output}`; the non-empty check, content hash, and result parsing run once after the chain completes.
- The rendered `commands` and their version-probe declarations are preserved in the recipe snapshot. Every probed step version is also mixed into the cache fingerprint, so upgrading any step's program (for example `rpsbproc`) invalidates the exact cache even when the recipe text and the primary tool version are unchanged; verified-output adoption still applies, exactly as for a primary tool upgrade.

The shipped `rpsblast` + `rpsbproc` command-chain recipe (`rpsblast_cdd`) is the worked example in [Result parsers and examples](recipe-parsers-examples.md) 10.5.

The `commands` system is NOT intended to replace Snakemake or Nextflow, but rather to bundle tools that are frequently used together
to reduce repetitive work, and avoid using the “bulky” Snakemake or Nextflow in such scenario. We have intentionally imposed the following hard constraints on the `commands`:

- Each program must use a shared runtime environment — one recipe, one environment. For example, if you run the RPS-BLAST recipe using Conda, you must have both NCBI-BLAST+ and `rpsbproc` installed in the same Conda environment. Per-step environment keys (such as `run_method` inside a command block) are rejected when the recipe is loaded; pipelines that genuinely span multiple environments belong to Snakemake/Nextflow, with their results brought back into the database via `operon adopt`.

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

`result_parser` names the parser that turns a successful output into SQLite rows; the allowed values and what each one reads and writes back are listed in [Result parsers and examples](recipe-parsers-examples.md). This section defines the field contract. Every hit-producing parser records the summary metrics `query_count`, `query_with_hit_count`, `hit_count`, and `best_evalue` in `analysis_results` and syncs them to `qc_results` under `analysis:<recipe>`; `busco_json` records the BUSCO completeness metrics instead.

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

### `hmmer_tblout`

`hmmer_tblout` reads only HMMER `--tblout` output and needs no column declarations: full-sequence E-value and score per query–profile pair, plus the usual EAV hits. tblout carries no alignment coordinates, so it writes no `analysis_alignments` rows. Hit direction is normalized exactly as for `hmmer_domtblout`; see "HMMER hit direction and `hmmer_mode`" below. Worked example and parsing details: [Result parsers and examples](recipe-parsers-examples.md) 10.2.

### HMMER hit direction and `hmmer_mode`

Both `hmmer_tblout` and `hmmer_domtblout` normalize hit direction when writing to the database: `query_id` is always the analyzed sequence and `subject_id` is always the HMM profile, regardless of which HMMER program produced the file. Normalization is needed because hmmsearch places the sequence in the target column and the profile in the query column, while hmmscan, phmmer, and jackhmmer use the opposite order. The parser decides whether to swap the columns in three tiers:

1. **Recipe field `hmmer_mode`** — optional; `hmmsearch` or `hmmscan`. When set it has the highest priority and overrides the header, which covers files whose header lines were stripped (concatenated or trimmed outputs). Any other value is rejected as invalid.
2. **Header sniffing** — HMMER output always begins with a `# <program> :: ...` line. `hmmsearch` swaps the columns; every other program keeps the existing mapping.
3. **Fallback** — with neither the field nor a program header, the parser keeps the existing (hmmscan-style) mapping, preserving backward compatibility for files parsed before direction normalization existed.

After a swap, `rank` is counted per sequence, so `max_hits_per_query` also truncates per sequence — consistent with every other parser. Like the alignment mapping keys above, `hmmer_mode` enters the parameter fingerprint only when it is actually set, so setting or changing it invalidates the completed cache exactly once.

### `hmmer_domtblout`

`hmmer_domtblout` parses HMMER `--domtblout` per-domain rows and needs no column declarations. After direction normalization (see above), the per-domain i-Evalue and domain score become `evalue`/`bitscore`, and the alignment coordinates land in `query_start`/`query_end` — sequence coordinates under both hmmsearch and hmmscan. The HMM and envelope coordinates are written to the named `extra_json` keys `hmm_from`, `hmm_to`, `env_from`, and `env_to`; `subject_start`/`subject_end` stay NULL because domtblout carries no subject/profile coordinates. The parser writes both EAV hits and full structured alignment rows. Worked example and parsing details: [Result parsers and examples](recipe-parsers-examples.md) 10.3.

### `rpsbproc_tabular`

`rpsbproc_tabular` parses the tabular report produced by NCBI `rpsbproc` (the `DATA`/`SESSION`/`QUERY`/`DOMAINS` structure) and needs no column declarations: the alignment `query_id` is the QUERY definition line and `subject_id` is the accession, while `from`/`to` become `query_start`/`query_end`. EAV hits are truncated to `max_hits_per_query` per query as usual, while `analysis_alignments` keeps every domain row; queries without any domain produce no rows. It is the intended parser for `commands` chains that pipe `rpsblast -outfmt 11` into `rpsbproc` (see "Command chains" above). The 12-column layout, the `extra_json` field names, and the block/key rules are documented with the shipped recipe in [Result parsers and examples](recipe-parsers-examples.md) 10.5.

## Cache identity

A completed analysis is reused only when all of the following identity components are identical:

```text
analysis name
+ file_id
+ input content hash
+ rendered arguments
+ resolved runtime parameters
+ threads
+ tool version (for a `commands` chain: the first command's version,
  plus the probed version of every later step)
+ parser and output settings (`result_parser`, `max_hits_per_query`, `input_kind`,
  `output_kind`, `output_name`, `output_suffix`, `result_glob`, and any explicitly
  set column-mapping key, `hmmer_mode`, or `environment_policy`)
+ database identity
```

After a database record hits, `operon` also checks that the output artifact still exists and recomputes the file or directory hash against the recorded value. If the output was deleted or modified, the old job is marked `superseded` and re-executed.

That fingerprint does **not** cover every parser/output field: `result_columns`, `hit_metric_columns`, `numeric_columns`, `query_column`, and `subject_column` are absent from it. Changing one of those five alone therefore leaves the fingerprint unchanged, the existing `completed` job is treated as a cache hit, and `operon` neither re-runs the program nor re-parses the stored output — the new column contract only takes effect for jobs that are actually re-executed. Use `--force` to force that re-run.

The second-level continuation when the exact identity misses (verified-output adoption): if an old `completed` job exists for the same `(analysis, file_id)` whose input content hash matches the current one, and whose recorded output artifact is still on disk with a byte-identical hash, `operon` does not recompute. Instead it adopts that output into the current fingerprint — inserting a new `completed` row pointing at the same output under the current parameter fingerprint/database identity (linked to the original `workflow_run_id`), and recording the adoption reason in the `changes` audit table against that new `analysis_job` row. The result record of the adopted run carries `"status": "adopted"`; the manifest `files.status` is not changed by adoption. This covers scenarios such as software upgrades changing the fingerprint formula, or recipe renames. Outputs that were modified, or inputs whose content changed, are not adopted and are recomputed as usual. Adoption applies only to completed results with verified outputs; `--force` semantics are unchanged and always recompute. In dry-run output, the status column shows `cached`/`adoptable`/`planned`, meaning a completed-cache hit, the adoption path, and actual execution respectively; under `--force`, even a cache that would have hit shows as `planned`. Once a recipe resolves at least one runtime parameter, second-level adoption is disabled and only exact cache hits can reuse a result.

`--force` only means "ignore an otherwise valid completed cache". It preserves the historical job record, marks the old record `superseded`, deletes the exact old output target, and creates a new job. It cannot fix wrong parameters, a wrong output name, or failures of the external program itself.

Checking selection, command, and cache with a dry run first is the safest approach:

```bash
operon --project . analyze \
  --analysis busco_autolineage \
  --entity-id ANN_000001 \
  --threads 24 \
  --dry-run
```

### Environment policy on cache reuse

A recipe can also gate cache reuse on the execution environment with the optional `environment_policy` field:

| Value | Behavior on a cache hit with a mismatching environment |
|---|---|
| `ignore` | Reuse unconditionally; environment capture stays provenance-only |
| `warn` (default) | Reuse the cached result, record a warning in the run details, and print it |
| `strict` | Treat the hit as a miss and recompute |

Any other value is rejected at configuration validation. Like `hmmer_mode` and the alignment column-mapping keys, `environment_policy` enters the parameter fingerprint only when it is explicitly set, so setting or changing it invalidates the completed cache exactly once.

The comparison key is the environment-relevance fingerprint: the composite of the captured document's `system_fingerprint`, `hardware_fingerprint`, and `conda.package_fingerprint`. Hostname, paths, CPU affinity, and runtime threading variables are excluded — the thread count already participates in the parameter fingerprint, and affinity is transient scheduler state. Matching fingerprints reuse the cache without further output.

Behavior differs per backend and per document age:

- `local` and direct `ssh` probe the environment before the payload, so both sides of the comparison are always available.
- `slurm` and remote Slurm have no pre-job probe and cannot compare before reuse; `strict` degrades to `warn` and the run details record `environment_policy_degraded: strict->warn`.
- When either side's environment document lacks sub-fingerprints (captures recorded before database schema {{ db_schema }}), the comparison is recorded as `environment_compare: unavailable`: `warn` reuses as usual and `strict` likewise degrades to `warn`, so legacy documents never cause spurious recomputation.

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
