# External Analysis Commands

## run-external

```bash
operon run-external \
  --step STEP --command 'CMD ARGS' \
  [--entity-type TYPE] [--entity-id ID] \
  [--parameter-set PS] [--tool NAME] [--input PATH ...] [--threads N] \
  [--expected-output PATH ...] \
  [--cwd DIR] [--timeout SECONDS] [--backend {local,slurm,ssh}]
```

- The command is parsed with `shlex` and is not executed through a shell.
- Exit code, stdout/stderr files, and start/end times are recorded in `workflow_runs` and `logs/workflow.jsonl`.
- Success requires exit code 0 and every `--expected-output` to exist and be non-empty.
- `--tool NAME` references a tool configured in `config/tools.yaml`: on a match its version is detected automatically and recorded as `tool_version` and `tool_version_raw`; a failed probe degrades to a warning and does not block the run.
- `--input PATH` (repeatable) declares input files/directories: each file is hashed with SHA-256, the combined hash is written to `input_sha256`, and the full list goes into `execution_details`.
- `--threads N` records and requests the thread count from the execution backend.
- `--backend` overrides `project.yaml`'s `execution.backend`: `local` (default subprocess), `slurm` (submit to a local Slurm cluster), or `ssh` (run on an SSH host). See [Remote Execution with Slurm and SSH](../guides/remote-execution.md).

## tools-check

```bash
operon tools-check
```

Reads `config/tools.yaml`, runs each tool's `version_args`, and extracts the version with `version_pattern`. A missing program displays `ERROR` and configuration guidance without modifying the database. If any program is unavailable, the command exits with code 1.

## analyze

```bash
operon analyze --analysis NAME \
  [--param NAME=VALUE ...] [--entity-type TYPE] [--entity-id ID] \
  [--threads N] [--limit N] [--dry-run] [--force] [--keep-partial] \
  [--backend {local,slurm,ssh}]
```

For each run, the recipe:

1. Selects files or directory inputs from the `files` manifest using `entity_type + file_role + format`.
2. Rechecks file SHA-256 or directory tree hash according to `input_kind`.
3. Detects and records the external tool version.
4. Validates `--param NAME=VALUE` against the recipe `parameters` declarations and renders arguments. In addition to `${input}`, `${output}`, `${database}`, and `${threads}`, placeholders include `${input_parent}`, `${input_name}`, `${input_stem}`, `${output_parent}`, `${output_name}`, `${output_stem}`, `${file_id}`, `${file_role}`, `${entity_type}`, `${entity_id}`, and declared `${<parameter>}` values. Runtime parameters enter output naming and the cache fingerprint.
5. Skips execution when the completed-job cache in `analysis_jobs` matches, unless `--force` is used. If the exact fingerprint misses but an old completed job has the same input and an output whose hash verifies, the output is adopted under the current fingerprint and reported as `adopted` instead of recomputed.
6. Validates that a `file` or `directory` output exists and is non-empty, then calculates its content hash.
7. Parses results into `analysis_hits` and `analysis_results`, and synchronizes summary metrics to `qc_results`.

Supported result parsers are `blast_tabular`, `hmmer_tblout`, `busco_json`, and `none`. `busco_json` selects a unique specific JSON summary from a directory using `result_glob` and writes BUSCO completeness, single-copy/duplicated, fragmented, missing, marker-count, and lineage metrics.

`--backend` overrides `project.yaml`'s `execution.backend` and can be `local` (default), `slurm`, or `ssh`. Tool-version detection also uses the selected backend. See [Remote Execution with Slurm and SSH](../guides/remote-execution.md). With SSH `storage_remote`, a locally missing candidate input in `REMOTE_ONLY` state is first validated against the remote manifest and actual content, then used in place remotely.

`--dry-run` lists the plan without execution. Status values are `cached` (completed cache hit), `adoptable` (verified old output will be adopted), or `planned` (execution will run). The output column contains planned output paths, and `tool_version` contains the detected version.

`--param` can set only parameters declared by the recipe. Missing required parameters, unknown parameters, repeated values, or values failing `pattern`/`choices` are configuration errors. Default `busco_lineage` usage:

```bash
operon analyze --analysis busco_lineage \
  --param lineage_dataset=fabales_odb12.2
```

`report analysis` displays every parameter variant still marked `completed`; it does not retain only the latest run for a recipe.

Interruption and graceful shutdown: on Ctrl+C (SIGINT) or SIGTERM, `analyze`:

- Terminates the current job completely. The local backend sends SIGTERM and then SIGKILL to the process group, including grandchildren. The Slurm backend runs `scancel`. The SSH backend terminates the remote `setsid` process group or cancels the remote Slurm job.
- Marks the current `analysis_jobs` row `interrupted` so it cannot pollute the completed cache. Partial output is removed, while stdout/stderr logs remain for debugging. `--keep-partial` preserves partial output.
- Stops the batch and exits with code 130. Rerunning the same command resumes unfinished files because `interrupted` rows do not match the cache.
- A second signal during cleanup exits immediately with code `128 + signum`.

If a process is killed by SIGKILL or another uncatchable mechanism, a residual `RUNNING` row is cleaned to `interrupted` on the next `analyze` startup.

Default recipes are `blastn_nt`, `blastp_nr`, `hmmsearch_pfam`, and `busco_autolineage`; they can be changed. For the complete `config/tools.yaml` contract, see the [Recipe Configuration Model](recipe-overview.md).

## report analysis

```bash
operon report analysis [--analysis NAME] [--entity-type TYPE] [--entity-id ID] \
  [--hits] [--limit N] [--include-retired]
```

- By default, displays summary metrics from `analysis_results`.
- `--hits` displays top hits from `analysis_hits`.
- `--limit` defaults to 20.
- Effectively retired entities are excluded by default; `--include-retired` displays historical results.
