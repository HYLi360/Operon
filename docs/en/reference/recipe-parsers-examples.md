# Result parsers and examples

## Result parsers

`result_parser` controls how a successful output enters SQLite:

| Parser | Expected output | Main write-back |
|---|---|---|
| `none` | Any verified artifact | Stores only job and output provenance; no domain metrics parsed |
| `blast_tabular` | Tab-separated file | Top hits, query/hit summary, best e-value |
| `hmmer_tblout` | HMMER `--tblout` file | Query-target e-value/score and summary |
| `busco_json` | BUSCO output directory or JSON file | Completeness, single-copy/duplicated, fragmented/missing, lineage and version metadata |

All summary metrics are written to `analysis_results` and synced to `qc_results` with `qc_stage: analysis:<recipe>`; they therefore appear naturally in the `report qc` wide-table export and can be consumed directly by QC profiles. Top hits are additionally written to `analysis_hits`.

### 10.1 `blast_tabular`

At least the columns must be declared:

```yaml
result_parser: blast_tabular
result_columns:
  - qseqid
  - sseqid
  - pident
  - length
  - evalue
  - bitscore
hit_metric_columns: [pident, length, evalue, bitscore]
numeric_columns: [pident, length, evalue, bitscore]
query_column: qseqid
subject_column: sseqid
max_hits_per_query: 5
```

`result_columns` must exactly match the actual column order of the external program. By default the first two columns serve as query/subject and the remaining columns as hit metrics; the dedicated fields above override this. Input order determines hit rank, so make the tool emit hits in the priority you want to keep.

### 10.2 `hmmer_tblout`

This parser reads target, query, full-sequence E-value, and score from a standard HMMER tblout, ignores comment lines, and keeps the first `max_hits_per_query` targets per query in input order.

```yaml
arguments:
  - --tblout
  - ${output}
  - --cpu
  - ${threads}
  - ${database}
  - ${input}
result_parser: hmmer_tblout
max_hits_per_query: 5
```

### 10.3 `busco_json`

BUSCO typically uses directory output:

```yaml
result_parser: busco_json
result_glob: short_summary*.json
```

`result_glob` must stay inside the output directory — no absolute paths and no `..`. If it matches exactly one JSON, that file is used; if both generic and specific summaries exist, the unique `short_summary.specific.*.json` wins; if multiple specific summaries still match, the parser refuses to guess and you should narrow the glob.

The JSON must contain at least `results.Complete percentage` and `results.n_markers`. Parsed results include:

- Percentages and counts of complete, single-copy, duplicated, fragmented, and missing;
- Marker count, domain, and one-line summary;
- Lineage name, creation date, BUSCO count, and species count;
- datasets/OrthoDB/dataset versions, NCBI taxid, and BUSCO software version.

## BUSCO example

```yaml
tools:
  busco:
    description: Benchmarking Universal Single-Copy Ortholog assessment
    executable: busco
    run_method: "mamba run -n busco_6.1.0"
    version_args: ["--version"]
    version_pattern: 'BUSCO\s+([^\s]+)'

    recipes:
      busco_autolineage:
        description: BUSCO protein mode with automatic lineage selection
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

BUSCO's `-o` is a short run name, not an input path, and should not receive the full `${output}`; `--out_path` is the parent directory. Hence the separate use of `${output_name}` and `${output_parent}`.

In addition, the SEPP used by BUSCO auto-lineage incorrectly runs `replace("fasta", "jplace")` on the full output path. No level of the output path may contain lowercase `fasta`. Using `${file_id}.busco` avoids the default `<file_id>.protein_fasta` name; `operon` also checks for and rejects such dangerous paths before launching auto-lineage.

Running:

```bash
operon --project . tools-check
operon --project . analyze \
  --analysis busco_autolineage \
  --entity-id ANN_000001 \
  --threads 24 \
  --dry-run
operon --project . analyze \
  --analysis busco_autolineage \
  --entity-id ANN_000001 \
  --threads 24
operon --project . report analysis \
  --analysis busco_autolineage \
  --entity-id ANN_000001
```

### 11.1 Explicit-lineage recipes and result coexistence

`busco_lineage` accepts `--lineage_dataset` through a declared runtime parameter:

```yaml
busco_lineage:
  description: BUSCO protein mode with an explicitly selected lineage
  entity_type: annotation
  file_role: protein_fasta
  format: fasta
  input_kind: file
  parameters:
    lineage_dataset:
      required: true
      pattern: '[A-Za-z0-9][A-Za-z0-9_.-]*'
  database: resources/busco_downloads
  database_version: odb12
  database_mode: mutable_cache
  output_subdir: busco_lineage
  output_kind: directory
  output_name: ${file_id}.${lineage_dataset}.busco
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
    - --lineage_dataset
    - ${lineage_dataset}
    - -c
    - ${threads}
    - --opt-out-run-stats
    - --tar
  result_parser: busco_json
  result_glob: short_summary.specific.*.json
```

Run examples:

```bash
operon analyze --analysis busco_lineage \
  --entity-id ANN_000001 \
  --param lineage_dataset=fabales_odb12.2
operon analyze --analysis busco_lineage \
  --entity-id ANN_000001 \
  --param lineage_dataset=eudicotyledons_odb12.2
```

The two results do not "overwrite the old with the latest"; they are stored side by side:

```text
analysis/busco_lineage/ANN_000001/FIL_000003.fabales_odb12.2.busco/
analysis/busco_lineage/ANN_000001/FIL_000003.eudicotyledons_odb12.2.busco/
```

They have distinct parameter fingerprints, output artifacts, `analysis_jobs`/`analysis_results` rows, and QC stages:

```text
analysis:busco_lineage:lineage_dataset=fabales_odb12.2
analysis:busco_lineage:lineage_dataset=eudicotyledons_odb12.2
```

`report analysis --analysis busco_lineage` shows all parameter variants that are still `completed`; when the same exact parameters are re-run with `--force`, the old job is marked `superseded` and the new job becomes the effective result for that variant.

The `qc_results` long table is the source of truth and fully expresses coexisting results. `qc_results.wide.tsv` is only suitable for browsing and exploratory statistics: same-named metrics must be collapsed into one column, so it shows the most recent value. A formal QC profile should not rely on this implicit "latest value"; it should use `source.qc_stage` to state whether to consume the auto-lineage stage or a fixed-lineage stage.

For study scopes covering all green plants, the recommended practice is to use `busco_autolineage` as the uniform QC input across the whole dataset; fixed-lineage recipes are for re-checking a taxonomic subset, comparing on a common scale, or diagnosing anomalies — not for forcing the entire project onto a single lineage.

## Directory input and output

The following example assumes a wrapper that accepts a directory and creates a non-empty result directory. For programs that natively accept only a single file, do not pretend directory support by merely changing `input_kind` to `directory`; the wrapper should traverse the directory explicitly and define failure semantics.

```yaml
tools:
  directory_tool:
    executable: directory-wrapper
    run_method: ""
    version_args: ["--version"]
    version_pattern: 'directory-wrapper\s+([^\s]+)'
    recipes:
      directory_roundtrip:
        entity_type: organism
        file_role: other
        format: directory
        input_kind: directory
        output_subdir: directory_roundtrip
        output_kind: directory
        output_name: ${file_id}.results
        arguments:
          - --input-dir
          - ${input}
          - --output-dir
          - ${output}
          - --threads
          - ${threads}
        result_parser: none
```

## Onboarding a new tool

When onboarding a new tool, start from a minimal runnable configuration and add fields step by step. The following order is usually the easiest to debug:

1. Determine the `entity_type`, `file_role`, and `format` of one real manifest input record;
2. Configure tool launch and version probing until `tools-check` succeeds;
3. Start with `result_parser: none` and just make the tool produce a file or directory artifact correctly;
4. Use `output_name` to pin the root directory of directory-producing programs, confirming that `${output}` matches the location the tool actually creates;
5. Add the database path and version policy;
6. Inspect the full command with `analyze --dry-run --limit 1`;
7. Run one small input for real, checking stdout, stderr, `analysis_jobs`, and the output structure;
8. Finally enable the parser and cross-check `report analysis` against `report qc`;
9. Then scale up to all candidate entities.

## Common errors

| Symptom | Common cause | Resolution |
|---|---|---|
| `no candidate files` | Manifest role/format/entity does not exactly match the recipe | Check actual values in `files.tsv` or the database |
| Input checksum mismatch | Raw file or directory modified after archiving | Restore the original content, or re-archive as a new version instead of overwriting raw |
| Output missing or empty | The tool's created location differs from `${output}` | After a dry run, compare against the tool's output/run-name semantics |
| Directory program treated as a file | Missing `output_kind: directory` | State the artifact type explicitly |
| Cache not reused after parameter change | Expected behavior; rendered arguments participate in identity | Inspect the command diff with a dry run |
| Every database growth invalidates the cache | A download area mistakenly set to `reference` | Use a versioned `mutable_cache` for shared download areas |
| Parser cannot find the file | `result_glob` written relative to the wrong base | Check the real relative position from the `${output}` root |
| Multiple BUSCO specific JSONs conflict | Glob spans summaries of multiple lineages | Narrow `result_glob` to the final summary |
| BUSCO/SEPP produces `protein_jplace` paths | Output path contains `fasta` | Use `output_name: ${file_id}.busco` and check parent directories |
| Still failing with `--force` | Not a cache problem but a command, output, or tool error | Fix the recipe first; `--force` only controls the completed cache |

## Pre-release checklist

Before saving a new recipe, confirm item by item:

- The input selection fields exactly match the manifest's actual values;
- `input_kind` and `output_kind` match the filesystem object types;
- The external program's final created location is exactly `${output}`;
- Each `arguments` list item is one independent argv element;
- Growable download directories use `mutable_cache`; frozen reference databases use `reference`;
- Tool version, database logical version, and parser are explicit;
- `tools-check` passes;
- The full command from `analyze --dry-run --limit 1` matches expectations;
- A single small input has been run for real and `report analysis` cross-checked against `report qc` before batch execution.
