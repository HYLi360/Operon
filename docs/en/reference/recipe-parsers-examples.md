# Result parsers and examples

## Result parsers

`result_parser` controls how a successful output enters SQLite:

| Parser | Expected output | Main write-back |
|---|---|---|
| `none` | Any verified artifact | Stores only job and output provenance; no domain metrics parsed |
| `blast_tabular` | Tab-separated file | Top hits, query/hit summary, best e-value, plus full structured alignment rows |
| `hmmer_tblout` | HMMER `--tblout` file | Query-target e-value/score and summary |
| `hmmer_domtblout` | HMMER `--domtblout` file | Per-domain i-Evalue/score hits plus full structured alignment rows |
| `rpsbproc_tabular` | rpsbproc tabular report | Per-domain e-value/bitscore hits plus full structured alignment rows |
| `busco_json` | BUSCO output directory or JSON file | Completeness, single-copy/duplicated, fragmented/missing, lineage and version metadata |

All summary metrics are written to `analysis_results` and synced to `qc_results` with `qc_stage: analysis:<recipe>`; they therefore appear naturally in the `report qc` wide-table export and can be consumed directly by QC profiles. Top hits are additionally written to `analysis_hits` (EAV rows, truncated to `max_hits_per_query` per query). Parsers with coordinates (`blast_tabular`, `hmmer_domtblout`, `rpsbproc_tabular`) also write every parsed hit as a structured row to `analysis_alignments` — query/subject IDs, hit rank, query/subject intervals, e-value, bitscore, and percent identity as dedicated columns, with unmapped columns kept in `extra_json`; this table is never truncated by `max_hits_per_query`, and `report analysis --hits` reads from it.

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

When the output includes BLAST coordinate columns, they are recognized automatically and every hit row is also written to `analysis_alignments` in structured form:

```yaml
arguments:
  - -outfmt
  - "6 qseqid sseqid pident length qstart qend sstart send evalue bitscore"
result_columns: [qseqid, sseqid, pident, length, qstart, qend, sstart, send, evalue, bitscore]
```

The common names `qstart`/`qend`/`sstart`/`send`/`evalue`/`bitscore`/`pident` are detected by default; `qstart_column`/`qend_column`/`sstart_column`/`send_column`/`evalue_column`/`bitscore_column`/`pident_column` override them when a tool uses different headers. Alignment rows are written in full regardless of `max_hits_per_query`.

For rpsblast, which emits no standard coordinate header names in some pipelines, declare the mapping explicitly:

```yaml
tools:
  rpsblast:
    executable: rpsblast
    run_method: "conda run --no-capture-output -n blast"
    version_args: ["-version"]
    version_pattern: 'rpsblast:\s*([^\s]+)'
    recipes:
      rpsblast_cdd:
        description: Annotation proteins against the CDD database
        entity_type: annotation
        file_role: protein_fasta
        format: fasta
        database: /data/db/cdd/Cdd
        database_version: "3.21"
        output_subdir: rpsblast_cdd
        output_suffix: .rpsblast.tsv
        arguments:
          - -db
          - ${database}
          - -query
          - ${input}
          - -out
          - ${output}
          - -outfmt
          - "6 qseqid sseqid pident length qstart qend sstart send evalue bitscore"
          - -num_threads
          - ${threads}
        result_parser: blast_tabular
        result_columns: [qseqid, sseqid, pident, length, qstart, qend, sstart, send, evalue, bitscore]
        hit_metric_columns: [pident, length, evalue, bitscore]
        query_column: qseqid
        subject_column: sseqid
        qstart_column: qstart
        qend_column: qend
        sstart_column: sstart
        send_column: send
        evalue_column: evalue
        bitscore_column: bitscore
        pident_column: pident
        max_hits_per_query: 5
```

When the rpsblast run is instead post-processed by `rpsbproc` (the recommended CDD pipeline), use a `commands` chain with the `rpsbproc_tabular` parser — see below.

### 10.2 `hmmer_tblout`

This parser reads target, query, full-sequence E-value, and score from a standard HMMER tblout, ignores comment lines, and keeps the first `max_hits_per_query` targets per query in input order. tblout carries no alignment coordinates, so this parser writes no `analysis_alignments` rows.

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

### 10.3 `hmmer_domtblout`

For structured per-domain hits, prefer `--domtblout` and the `hmmer_domtblout` parser. Each non-comment line is one domain: the query is the HMM profile name, the subject is the target sequence, the per-domain i-Evalue and domain score are recorded as `evalue`/`bitscore`, and the alignment coordinates become `query_start`/`query_end`; the HMM and envelope coordinates are preserved in `extra_json`, and subject coordinates stay NULL because domtblout does not carry them. EAV hits still respect `max_hits_per_query`, while `analysis_alignments` keeps every domain row.

```yaml
hmmsearch_pfam_domains:
  description: Annotation proteins against Pfam-A.hmm (per-domain hits)
  entity_type: annotation
  file_role: protein_fasta
  format: fasta
  database: /path/to/Pfam-A.hmm
  database_version: ""
  output_subdir: hmmsearch_pfam_domains
  output_suffix: .hmmsearch.domtblout
  arguments:
    - --domtblout
    - ${output}
    - --cpu
    - ${threads}
    - ${database}
    - ${input}
  result_parser: hmmer_domtblout
  max_hits_per_query: 5
```

### 10.4 `busco_json`

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

### 10.5 `rpsbproc_tabular`

NCBI's CDD pipeline runs `rpsblast` with ASN.1 output (`-outfmt 11`) and renders the archive into a tabular report with `rpsbproc`. The report is structured in `DATA`/`SESSION`/`QUERY`/`DOMAINS`/`ENDDATA` blocks; each line inside a `DOMAINS` block is one domain with 12 columns:

```text
session  query-id  hit-type  PSSM-ID  from  to  E-Value  bitscore  accession  short-name  incomplete  superfamily-PSSM-ID
```

The parser attributes domain rows to their enclosing `QUERY` block, keyed by `(session, query-id)` because the `Query_N` numbering repeats across sessions — a repeated key is an error, as is a malformed row. `SITES`/`MOTIFS` blocks and anything after `ENDDATA` are ignored. For every domain row:

- `query_id` is the QUERY definition line (matching the FASTA header of the input), `subject_id` is the accession;
- `from`/`to` become `query_start`/`query_end`; e-value and bitscore are parsed as numbers;
- `hit_type`, `pssm_id`, `short_name`, `incomplete`, `superfamily_pssm`, `session`, and `rps_query_id` are preserved in `extra_json`.

EAV hits keep the usual `max_hits_per_query` truncation; `analysis_alignments` keeps every domain row. A query without any domain produces no rows — downstream selection recognizes "no hit" by left-joining against the `sequences` table (see `select-sequences` in [External Analysis Commands](cli-analysis.md)). The parser needs no column declarations:

```yaml
result_parser: rpsbproc_tabular
max_hits_per_query: 5
```

The default `tools.yaml` template ships a complete `rpsblast_cdd` recipe that couples both programs through a `commands` chain (field contract in [Recipe field reference](recipe-fields.md)):

```yaml
tools:
  rpsblast:
    description: NCBI RPS-BLAST against CDD, post-processed by rpsbproc
    executable: rpsblast
    run_method: "conda run --no-capture-output -n blast"
    version_args: ["-version"]
    version_pattern: 'rpsblast:\s*([^\s]+)'
    recipes:
      rpsblast_cdd:
        description: Annotation proteins against CDD via rpsblast + rpsbproc
        entity_type: annotation
        file_role: protein_fasta
        format: fasta
        database: /path/to/cdd/Cdd
        database_version: ""
        output_subdir: rpsblast_cdd
        output_suffix: .rpsbproc.tsv
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
              - -evalue
              - "0.001"
              - -num_threads
              - ${threads}
          - arguments:
              - rpsbproc
              - -i
              - ${work_dir}/hits.asn
              - -o
              - ${output}
              - -e
              - "0.001"
              - -m
              - rep
            version_args: [-version]
            version_pattern: 'rpsbproc:\s*([^\s]+)'
        result_parser: rpsbproc_tabular
        max_hits_per_query: 5
```

Step 1 writes the ASN.1 archive into the per-run `${work_dir}`; step 2 renders it into `${output}`; the scratch directory is cleaned up after the run. The first step inherits the parent `rpsblast` tool version, while the second records the independent `rpsbproc -version` result in step provenance; both probed versions participate in the cache identity, so an `rpsbproc` upgrade alone already invalidates the exact cache. rpsbproc flags differ between builds, so adjust the second command block to the locally installed rpsbproc version.

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
