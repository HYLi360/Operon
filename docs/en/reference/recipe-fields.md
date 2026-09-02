# Recipe field reference

## Input selection

### 4.1 Selection fields

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

### 4.2 Additional `analyze` filtering

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

### 6.1 Declaring safe runtime parameters

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

### 6.2 Placeholders available in `arguments`

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

### 6.3 Placeholders available in `output_name`

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

It cannot reference `${output}`, `${output_parent}`, or `${output_name}` itself. Unrecognized placeholders are reported during configuration validation.

## Databases and cache directories

| Field | Default | Meaning |
|---|---|---|
| `database` | empty | Database file, database directory, or shared tool download directory; relative paths resolve against the project root |
| `database_version` | empty | Human-readable logical version; also participates in the database cache identity |
| `database_checksum` | empty | Optional explicit SHA-256 identity, suitable for frozen large databases |
| `database_mode` | `reference` | `reference` or `mutable_cache` |

### 7.1 `reference`

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

### 7.2 `mutable_cache`

For shared directories such as BUSCO's, which gradually download lineages at runtime:

```yaml
database: resources/busco_downloads
database_version: odb12
database_mode: mutable_cache
```

The directory is created automatically before the actual run. Its identity is determined by path, the explicit `database_version`, and an optional checksum — downloading another lineage later does not invalidate the cache of all older BUSCO jobs. `mutable_cache` requires a non-empty `database_version`.

If the goal is strict freezing and offline reproduction, pre-download the chosen lineage, switch BUSCO to `--lineage_dataset ... --offline`, and then use `reference` mode with a maintained version or checksum.

### 7.3 Databases on SSH remotes

When SSH uses a non-empty `remote_root`, paths under the local project root in `${database}` are mapped to the remote root; absolute paths outside the project root are kept as-is. `operon` never uploads large reference databases with every job:

- `reference` must be placed at the remote target path by an administrator in advance, with `database_checksum` configured; path existence is checked before the run. The explicit checksum enters the database cache identity together with the SSH host/root;
- `mutable_cache` must have a `database_version`, and the target directory is created over SFTP when missing;
- A database existing locally under the same name does not mean it is deployed remotely, and vice versa; a missing database is reported clearly before the analysis is submitted;
- Different SSH hosts/roots do not share analysis cache identity, avoiding cross-cluster reuse of results when content location is unclear.

Here `database_checksum` is the recipe's explicit declaration of a frozen database's published identity. For reference databases that need byte-level auditing, additionally run the publisher's verification or generate an Operon-verifiable manifest at deployment time; the runtime does not repeatedly traverse multi-terabyte databases for every candidate input.

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
