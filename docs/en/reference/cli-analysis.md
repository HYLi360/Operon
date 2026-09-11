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
- Before execution the command prints one line with the new run ID, both log paths, and a follow hint, e.g. `run <run_id>: logs logs/<run_id>.stdout.log / logs/<run_id>.stderr.log; watch: operon workflow show <run_id> --follow`.
- Exit code, stdout/stderr files, and start/end times are recorded in `workflow_runs` and `logs/workflow.jsonl`; `workflow_runs` also carries `duration_seconds` (wall clock) plus the `max_rss_mb`/`avg_rss_mb`/`cpu_seconds` resource-usage columns as collected by the execution backend (NULL when collection is unavailable, never affecting the success verdict; per-backend collection is described in the [external analysis execution model](../architecture/external-analysis.md)).
- Success requires exit code 0 and every `--expected-output` to exist and be non-empty.
- `--tool NAME` references a tool configured in `config/tools.yaml`: on a match its version is detected automatically and recorded as `tool_version` and `tool_version_raw`; a failed probe degrades to a warning and does not block the run.
- `--input PATH` (repeatable) declares input files/directories: each file is hashed with SHA-256, the combined hash is written to `input_sha256`, and the full list goes into `execution_details`. With the SSH backend and a non-empty `remote_root`, declared inputs are also uploaded into the corresponding path in the remote project mirror before execution. Such inputs must resolve inside the local project root.
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

1. Selects files or directory inputs from the `files` manifest using `entity_type + file_role + format` (a recipe may instead declare `file_role_prefix` for a plain prefix match on `file_role`; see [Recipe field reference](recipe-fields.md)).
2. Rechecks file SHA-256 or directory tree hash according to `input_kind`.
3. Detects and records the external tool version.
4. Validates `--param NAME=VALUE` against the recipe `parameters` declarations and renders arguments. In addition to `${input}`, `${output}`, `${database}`, and `${threads}`, placeholders include `${input_parent}`, `${input_name}`, `${input_stem}`, `${output_parent}`, `${output_name}`, `${output_stem}`, `${file_id}`, `${file_role}`, `${entity_type}`, `${entity_id}`, and declared `${<parameter>}` values. Runtime parameters enter output naming and the cache fingerprint.
5. Skips execution when the completed-job cache in `analysis_jobs` matches, unless `--force` is used. If the exact fingerprint misses but an old completed job has the same input and an output whose hash verifies, the output is adopted under the current fingerprint and reported as `adopted` instead of recomputed.
6. Validates that a `file` or `directory` output exists and is non-empty, then calculates its content hash.
7. Parses results into `analysis_hits` and `analysis_results`, and synchronizes summary metrics to `qc_results`. Parsers with coordinates additionally write every parsed hit as a structured row to `analysis_alignments` (untruncated by `max_hits_per_query`).

Supported result parsers are `blast_tabular`, `hmmer_tblout`, `hmmer_domtblout`, `rpsbproc_tabular`, `busco_json`, and `none`. `busco_json` selects a unique specific JSON summary from a directory using `result_glob` and writes BUSCO completeness, single-copy/duplicated, fragmented, missing, marker-count, and lineage metrics.

A recipe can declare a `commands` chain instead of a single `arguments` command (the two are mutually exclusive). The rendered steps execute in order through the same backend under one `analysis_jobs` row; the first step with a non-zero exit code aborts the chain and fails the job with a `step N/M failed: ...` error. Each step gets its own `logs/<run_id>.step<N>.stdout.log` / `.stderr.log`, and every step's argv and exit code is listed in the run's `execution_details.steps`. Intermediate artifacts belong in the deterministic `${work_dir}` scratch directory (`<output_name>.work` next to the output), which is deleted and rebuilt before the run and removed after it finishes or fails; `--keep-partial` preserves it. Only the last step must produce `${output}`. The rendered `commands` participate in the parameter fingerprint. See "Command chains" in the [Recipe field reference](recipe-fields.md) and the `rpsblast_cdd` example in [Result parsers and examples](recipe-parsers-examples.md).

Outside `--dry-run`, `analyze` prints one progress line per file as processing advances, in the form `[i/N] file_id: running|done|failed`; dry runs stay silent and print only the plan table.

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

Before each candidate file is processed, the current recipe together with its referenced tool spec is snapshotted into `recipe_snapshots` (content-addressed, deduplicated), and `analysis_jobs.recipe_snapshot_id` points back to that snapshot; cache hits record a snapshot of the current configuration as well, and jobs adopted during resume inherit the original job's snapshot id. See the `recipes` command below and the [external analysis execution model](../architecture/external-analysis.md).

Default recipes are `blastn_nt`, `blastp_nr`, `hmmsearch_pfam`, `busco_autolineage`, `busco_lineage`, and the command-chain `rpsblast_cdd`; they can be changed. For the complete `config/tools.yaml` contract, see the [Recipe Configuration Model](recipe-overview.md).

## recipes

```bash
operon recipes list
operon recipes history NAME
operon recipes show NAME [--snapshot-id N]
```

- `list`: lists all recipes configured in `config/tools.yaml` (name/version/tool/entity_type/file_role/format).
- `history`: shows the recorded snapshot history of one recipe (snapshot_id/version/sha256 prefix/recorded_at/number of associated `analysis_jobs`).
- `show`: prints a snapshot document as YAML (the latest one by default, or the one given by `--snapshot-id`). Restoring an old version from the CLI is print-only: copy the output back into `config/tools.yaml` manually — the CLI never rewrites the file in place, so comments are not lost. The audited alternative is the TUI Config screen (Tools & Recipes tab): its History dialog restores a snapshot into the recipe editor and saving creates the next version, recording a new snapshot (note that TUI saves normalize the file's formatting and drop hand-written comments).

## profiles

```bash
operon profiles history [NAME]
operon profiles show NAME [--snapshot-id N]
```

Inspects the profile snapshots recorded into `qc_profiles` during `evaluate` and `classify-sequences`:

- `history`: without NAME, summarizes per profile (snapshot count and latest recording time); with NAME, lists that profile's snapshot history (snapshot_id/version/sha256 prefix/recorded_at/number of associated decisions).
- `show`: prints a snapshot document as YAML (the latest one by default). Also print-only: to restore from the CLI, copy the output back into `config/profiles/` manually — no in-place rewrite. The TUI Config screen (QC Profiles tab) offers the audited alternative: restore a snapshot into the profile editor and save it as the next version with a new recorded snapshot.

## report analysis

```bash
operon report analysis [--analysis NAME] [--entity-type TYPE] [--entity-id ID] \
  [--hits [--format {text,tsv,json}] [--out PATH] \
          [--query-id ID] [--subject-id ID] [--evalue-max VALUE]] \
  [--limit N] [--include-retired]
```

- By default, displays summary metrics from `analysis_results`.
- `--hits` displays the structured alignment hit rows stored in `analysis_alignments` for `completed` jobs, with columns `analysis_name`, `entity_type`, `entity_id`, `query_id`, `subject_id`, `hit_rank`, `query_start`, `query_end`, `subject_start`, `subject_end`, `evalue`, `bitscore`, `percent_identity`. The rows are the full parsed hit set, not the `max_hits_per_query`-truncated EAV view.
- `--format` selects the `--hits` rendering: an aligned `text` table (default), `tsv` with a header row, or a `json` array. `--out PATH` writes the chosen format atomically to a file instead of stdout.
- `--query-id`, `--subject-id`, and `--evalue-max` filter the `--hits` rows (e-value filter keeps rows with `evalue <= VALUE`). All of `--format`/`--out`/`--query-id`/`--subject-id`/`--evalue-max` require `--hits`; passing any of them without it is a validation error.
- `--limit` defaults to 20.
- Effectively retired entities are excluded by default; `--include-retired` displays historical results.

## extract-domains

```bash
operon extract-domains --file-id FIL_... \
  (--analysis NAME | --regions-tsv TSV) \
  [--flank N] [--min-length N] [--best-only | --all-regions] \
  [--subject-like PATTERN] [--evalue-max E] \
  --out FASTA [--manifest TSV]
```

- Extracts query intervals stored in `analysis_alignments` (only `completed` jobs count) as substrings of one manifest FASTA. `--analysis` names the completed analysis whose alignments define the regions; `--subject-like` (SQL LIKE matched against `subject_id` and the hit's `short_name` in `extra_json`) and `--evalue-max` narrow them. Alternatively `--regions-tsv` supplies external coordinates with columns `seqid,start,end` (optional `subject`,`evalue`) for regions that did not come from an analysis.
- `--flank` (default 5) extends each region on both sides, truncated at sequence boundaries; `--min-length` (default 30) discards shorter regions before flanking.
- `--best-only` (default) keeps the single best-evalue region per query and headers stay the plain seqid; `--all-regions` emits one record per region with headers `<seqid>|region:<start>-<end>` using the extracted (post-flank, boundary-truncated) coordinates.
- The output FASTA is written atomically. `--manifest` records every candidate region — including excluded ones with their reason (`missing_coordinates`, `below_min_length`, `seqid_not_in_fasta`, `region_outside_sequence`) — with source file, analysis, subject, original and extracted coordinates, length, and e-value.
- A `REMOTE_ONLY` source file fails with an actionable error: restore it with `operon pull` and re-run. Each run writes an `extract-domains` step with the full command line to `workflow_runs`; the product re-enters the manifest through `operon adopt` (see [External Analysis](../guides/external-analysis.md)).

## select-sequences

```bash
operon select-sequences --file-id FIL_... \
  [--analysis NAME ...] [--subject-like PATTERN] [--evalue-max E] \
  [--min-span N] [--hit-type TYPE] [--entity-type TYPE] [--entity-id ID] \
  [--require-hit | --require-no-hit] \
  --out FASTA [--manifest TSV]
```

- Writes the subset of one manifest FASTA whose sequences have (or lack) matching alignment hits in `analysis_alignments` (only `completed` jobs count). A repeated `--analysis` is OR-ed (a hit in any listed analysis counts); `--subject-like`, `--evalue-max`, `--min-span`, and `--hit-type` (exact match against the hit's `hit_type` in `extra_json`) are AND-ed. Passing no criterion at all is an error.
- `--require-hit` (default) keeps sequences with at least one matching hit; `--require-no-hit` keeps the complement, computed against the full seqid set of the `sequences` table (falling back to scanning the FASTA itself when the table has no rows for the file).
- `--entity-type`/`--entity-id` restrict which jobs' alignments count, supporting per-taxon batch strategies.
- `--manifest` lists every sequence of the file with its selection state and evidence (`matched_analysis`, `best_evalue`, `best_subject`, `hit_count`).
- Writes a `select-sequences` step to `workflow_runs`; the subset re-enters the manifest through `operon adopt`.

## classify-sequences

```bash
operon classify-sequences --profile NAME
```

Labels every sequence of the profile's target files from stored alignment hits, writing one row per labeled sequence into `sequence_labels`. `--profile` is resolved from `config/profiles/<name>.yaml` and must declare `kind: sequence_classification`; the full YAML grammar is described under [Sequence classification profiles](../guides/qc-profiles.md#sequence-classification-profiles). Thresholds are never hard-coded in the engine — they live in the versioned profile.

- Target files are the manifest files matching the profile's `applies_to.entity_type` + `applies_to.file_role`; superseded and effectively retired entities are excluded. Per file, each seqid registered in the `sequences` table is classified against the hits of the latest `completed` job per declared source analysis in `analysis_alignments`.
- Rules are evaluated in order and the first match wins; a sequence matched by no rule (and no `default`) stays unlabeled. Each label records its decision evidence (rule index, source, job/alignment id, observed values) in `details_json`.
- Idempotent and audited: re-running with the same profile content and the same inputs changes nothing and appends no `changes` rows; a changed profile rewrites the affected labels, audits each change (object type `sequence_label`), and deletes labels that no longer apply. The content-addressed profile snapshot is recorded in `qc_profiles` exactly as `evaluate` records it (inspectable with `operon profiles history`/`show`).
- Each run writes one `workflow_runs` row (step `classify-sequences`) with per-label counts in `execution_details`, and prints a per-label summary table.
- Exit codes: 0 on success, including a no-op rerun; 2 on validation errors (unknown profile, wrong profile `kind`, malformed profile).

```bash
operon classify-sequences --profile bhlh
```

## timetree

```bash
operon timetree taxon --name NAME
operon timetree pairwise (--taxon NAME | --taxon-id N) ...
operon timetree mrca (--taxon NAME ... | --taxa A,B,C) [--taxon-id N ...]
operon timetree timeline --taxon NAME
operon timetree calibrations (--taxa A,B,C | --taxon NAME ...) [--pairs] [--out TSV]
operon timetree fetch --pairs TSV --output DIR
operon timetree calibrate --snapshot DIR --tree FILE --taxa TSV --constraints TSV --output DIR
```

The `timetree` group queries the TimeTree REST API for divergence-time evidence and compiles MCMCTree calibration priors. Any use of TimeTree data must cite Kumar et al. 2022 (Mol Biol Evol, <https://doi.org/10.1093/molbev/msac174>); the citation is printed on every query.

- `taxon` resolves a scientific name to TimeTree/NCBI taxonomy candidates (`taxon_id`, `scientific_name`, `rank`). `pairwise` reports the divergence-time summary for exactly two taxa; `mrca` for the MRCA of N taxa; `timeline` lists the node timetable from one taxon back to the last universal ancestor. Taxa are given as repeated `--taxon NAME` / `--taxon-id N` (or comma-separated `--taxa` for `mrca` and `calibrations`); an ambiguous name is never auto-resolved — the error lists the candidates and asks for `--taxon-id`. All query commands accept `--format text|json` (default `text`) and `--refresh` to bypass the cache.
- Responses are cached inside the project at `adapters_cache/timetree/<sha256(url)>.json`, keeping the request URL, fetch time, and verbatim body so replayed queries stay auditable. TimeTree's terms forbid mirroring or redistributing the database, so only the exact queries made are cached, and requests stay serial with a pause between them. Each query records a `timetree:<subcommand>` step in `workflow_runs`.
- `calibrations` builds a calibration prior table for MCMCTree: one whole-set MRCA row by default, or one row per pair with `--pairs`. Columns are `node_label`, `taxa`, `taxon_ids`, `age_median`, `ci_low`, `ci_high`, `study_count`, `source`, `queried_at`, `cache_file`. `--out` additionally writes the TSV; archive it into the project with `operon adopt` when it should be versioned.
- `fetch` and `calibrate` are project-independent and work on explicit file paths: `fetch` downloads the summary and per-study evidence for selected NCBI taxon pairs into a new immutable snapshot directory (raw responses plus a checksummed manifest, never overwriting a prior run); `calibrate` compiles reviewed soft bounds from such a snapshot onto a rooted, strictly bifurcating species tree (every constraint needs `approved=yes` and a rationale; summary confidence intervals are evidence, never fossil bounds).
