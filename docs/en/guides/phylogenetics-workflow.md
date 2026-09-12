# Phylogenetics Workflow

This guide walks through a complete phylogenetics study loop on top of an Operon project, using a gene-superfamily analysis as the running example: build a superfamily framework tree, assign sequences to subfamilies against references, refine per-subfamily trees, infer a species tree with OrthoFinder/ASTRAL, screen outliers with TreeShrink, and reconcile gene trees with the species tree in Notung.

## Division of labor

`operon` is deliberately not a workflow engine. Multi-step orchestration — dependency graphs, parallelism, retries — belongs to a workflow manager such as snakemake or nextflow (or, for small studies, to stepwise `run-external` calls that still record provenance). What `operon` owns in this loop is data admission and release:

1. **Export**: materialize the selected sequences out of the project as a checksummed input bundle.
2. **External computation**: the workflow manager consumes that bundle and produces trees, alignments, and reports outside the project.
3. **Adopt**: register key intermediates and final results back as first-class manifest files with `file_lineage` edges.
4. **Publish**: evaluate the adopted products and freeze them into an immutable release.

This is the export/adopt contract described in [Extension boundaries](../architecture/extensibility.md); downstream workflows should read and write through it instead of touching the database directly.

## Step 1: export the selected sequences

`export` selects manifest files by entity, role, format, state, or QC decision, verifies each SHA-256 against the manifest, and materializes them under `data/<entity_type>/<entity_id>/` together with `manifest.tsv`, `qc.tsv`, `checksums.sha256`, and `provenance.json`. To export only the protein FASTAs of annotations that currently pass a QC profile:

```bash
operon export --output analysis/external/superfamily_round1 \
  --entity-type annotation \
  --file-role protein_fasta \
  --decision PASS --profile annotation_release_v1
```

`--decision` requires `--profile`, and the profile must exist in `config/profiles/`. `annotation_release_v1` is one of the six profiles `operon init` writes there (`file_integrity_v1`, `assembly_production_v1`, `annotation_release_v1`, `annotation_busco_viridiplantae_odb12_v1`, `reads_qc_v1`, and the coverage example `coverage_viridiplantae_v1`); no profile named `default` ships with `operon`. The decision is matched in `current_decisions` (PASS, PASS_WITH_WARNINGS, and so on), so the workflow input is exactly the QC-approved subset. See the [export reference](../reference/cli-decisions-reports.md) for all selection criteria.

## Step 2: run the external pipeline

Point your workflow manager at the exported bundle — `manifest.tsv` is the machine-readable input list. A typical snakemake setup reads that manifest, runs the superfamily framework tree, reference-guided subfamily assignment, per-subfamily refined trees, OrthoFinder/ASTRAL species-tree inference, TreeShrink screening, and Notung reconciliation, writing all products under a work directory such as `analysis/external/superfamily_round1/results/`.

For a single long-running step you can also record provenance without a workflow manager:

```bash
operon run-external --step astral_species_tree \
  --tool astral \
  --input analysis/external/superfamily_round1/results/gene_trees.tre \
  --expected-output analysis/external/superfamily_round1/results/astral.species_tree.nwk \
  --command 'astral -i analysis/external/superfamily_round1/results/gene_trees.tre -o analysis/external/superfamily_round1/results/astral.species_tree.nwk'
```

`run-external` prints the new run ID, both log paths, and a `watch: operon workflow show <run_id> --follow` hint before executing; `--follow` streams the logs live, and the exit code, resource usage, and expected-output check land in `workflow_runs` as usual.

## Step 3: adopt intermediates and results back

After the pipeline finishes, re-register the products worth keeping — key intermediates (the framework tree, the subfamily assignment table) and final results (the species tree, TreeShrink output, Notung reconciliation) — with a batch adopt manifest; the required and optional columns are in the [adopt reference](../reference/cli-decisions-reports.md#adopt). For example:

```text
path	entity_type	entity_id	role	format	compression	derived_from
analysis/external/superfamily_round1/results/framework.treefile	organism	ORG_000001	superfamily_framework_tree	other	none	FIL_000012
analysis/external/superfamily_round1/results/subfamily_assignments.tsv	organism	ORG_000001	subfamily_assignments	other	none	FIL_000021
analysis/external/superfamily_round1/results/astral.species_tree.nwk	organism	ORG_000001	species_tree	other	none	FIL_000021
analysis/external/superfamily_round1/results/treeshrink/	organism	ORG_000001	treeshrink_output	directory	none	FIL_000021
analysis/external/superfamily_round1/results/notung/	organism	ORG_000001	notung_reconciliation	directory	none	FIL_000021,FIL_000022
```

```bash
operon adopt --from-manifest adopt_manifest.tsv
```

Roles are freely named by your workflow. Every product is materialized under `analysis/adopted/<entity_id>/` (tree/alignment files typically detect as format `other` or `fasta`; directories adopt as `format: directory` with `compression: none`), inherits the ingest idempotency/conflict invariants, and gains `file_lineage` edges back to its inputs. The full batch commits in one transaction; on failure only newly created artifacts are removed. Data-determined units such as the per-subfamily sequence sets are *not* adopted row by row here — the next step fans them out from the adopted assignment table instead.

## Step 4: fan out data-determined units, then cascade analyses

Subfamily assignment is the point where the pipeline's shape becomes data-dependent: only after the external assign-subfamilies step reads the adopted framework tree is the number of subfamilies known. Do not hand-write one adopt manifest entry per subfamily. Have the external step emit a clean two-column assignment TSV (`unit`, `seqid` — project-specific filtering such as dropping unresolved sequences happens there), adopt that TSV as shown in step 3, then admit the units with `operon fanout`:

```bash
# Inspect the plan first: no files, no run row.
operon fanout --assignments-file FIL_000022 \
  --source-file FIL_000012 \
  --entity-type organism --entity-id ORG_000001 \
  --role-prefix subfamily_alignment --dry-run

# Then admit the units.
operon fanout --assignments-file FIL_000022 \
  --source-file FIL_000012 \
  --entity-type organism --entity-id ORG_000001 \
  --role-prefix subfamily_alignment
```

Each unit becomes a registered FASTA under `analysis/derived/ORG_000001/` with the role `subfamily_alignment:<unit>` and lineage back to the source FASTA and the assignment TSV, which is what the downstream analysis selects on. The flag contract, the seqid-resolution rules, and the reuse/conflict behaviour are in the [fanout reference](../reference/cli-decisions-reports.md#fanout).

Because the unit count is data-dependent, the downstream recipe declares a role prefix instead of an exact role:

```yaml
# config/tools.yaml
tools:
  iqtree:
    executable: iqtree2
    version_args: ["--version"]
    version_pattern: '([0-9][^\s]*)'
    recipes:
      iqtree_subfamily:
        entity_type: organism
        file_role_prefix: "subfamily_alignment:"
        format: fasta
        output_suffix: .treefile
        arguments: [-s, ${input}, -T, ${threads}, --prefix, ${output_stem}]
        result_parser: none
```

See [Recipe field reference](../reference/recipe-fields.md) for the full field contract; then:

```bash
operon analyze --analysis iqtree_subfamily
```

`analyze` runs one job per unit file, serially or through the configured backend, with caching, adoption, and audit exactly as for any other input. Orchestration beyond that — retries, parallelism, and the TreeShrink/Notung batches over the unit products — stays with the workflow manager, as before.

Adopted and fanned-out files are normal manifest members in every other respect: an adopted file with an exact role is selected by `entity_type + file_role + format` as before, a FASTA can be re-QC'd with `operon qc --file-id FIL_...`, and results flow into `analysis_results`/`analysis_alignments`/`qc_results` exactly like analyses over raw files, so each workflow round can build on the products of the previous round.

## Step 5: evaluate and release

Adopted products participate in the standard quality gate. Run `operon qc` for the formats with built-in parsers (or `run-external` + `import-qc` for external metrics), then `operon evaluate --profile <name>` and finally:

```bash
operon release --version v1.0 --profile annotation_release_v1
```

Only entities whose effective decision admits them enter the release; everything else lands in `exclusions.tsv`. Because adopted intermediates carry lineage edges, the release remains explainable back to the original raw inputs.

## Step 6: audit lineage with SQL

`file_lineage` records `derived_file_id -> input_file_id` edges, so the provenance graph is queryable directly:

```bash
# What went into the adopted species tree?
operon query "SELECT l.input_file_id, f.file_role, f.entity_id
              FROM file_lineage l JOIN files f ON f.file_id = l.input_file_id
              WHERE l.derived_file_id = 'FIL_000021'"

# Everything derived from one exported protein FASTA:
operon query "SELECT l.derived_file_id, f.file_role
              FROM file_lineage l JOIN files f ON f.file_id = l.derived_file_id
              WHERE l.input_file_id = 'FIL_000012'"
```

## Step 7: locate sequences and alignment hits

Two schema additions make sequence-level questions answerable without re-parsing files:

- `sequences` holds one row per FASTA record (`file_id`, `entity_type`, `entity_id`, `seqid`, `length`), populated automatically by built-in QC. To find which assembly contains a seqid:

```bash
operon query "SELECT entity_type, entity_id, file_id, length
              FROM sequences WHERE seqid = 'NW_012345678.1'"

# Or let show fall back to the sequences table when the ID is not an entity:
operon show NW_012345678.1
```

- `analysis_alignments` holds every parsed alignment hit of a completed job — query/subject IDs, rank, query/subject intervals, e-value, bitscore, percent identity — untruncated by `max_hits_per_query`. To list all hit intervals of one query in a domain scan:

```bash
operon query "SELECT subject_id, hit_rank, query_start, query_end, evalue, bitscore
              FROM analysis_alignments
              WHERE analysis_name = 'hmmsearch_pfam_domains'
                AND query_id = 'PF00046'
              ORDER BY hit_rank"
```

The same rows are available without SQL through `operon report analysis --hits --query-id PF00046`, with `--format tsv|json` and `--out` for downstream consumption.

## Domain-focused variant: scan, select, extract, date

A domain-centric study — for example a superfamily defined by one CDD/Pfam domain — can stay inside the project for the first rounds and hand off to the workflow manager later:

1. **Scan**: run the `rpsblast_cdd` recipe (the rpsblast + rpsbproc command chain parsed by `rpsbproc_tabular`) over the protein FASTAs; every domain row lands in `analysis_alignments` with hit type, accession, and short name.
2. **Select**: `operon select-sequences --analysis rpsblast_cdd --hit-type Specific --evalue-max 1e-5 ...` splits each proteome into domain carriers and non-carriers. Control completeness with the e-value threshold, never a max-hits cutoff — a truncated tool hit list silently breaks the "no hit" complement (see [External Analysis](external-analysis.md)).
3. **Extract**: `operon extract-domains --analysis rpsblast_cdd --flank 5 --min-length 30 ...` writes the flanked domain FASTA plus a per-region manifest; adopt both products so later recipes and exports can select them.
4. **Align and build trees externally**: the adopted domain FASTAs feed the external aligner and tree inference exactly as in steps 2–3 above. Measure the resulting alignment on any machine with `operon alignment-qc --alignment ... --outdir ...` (per-sequence and per-column metrics) before adopting the alignment and trees back.
5. **Date the species tree**: `operon timetree calibrations --taxa A,B,C --out calibrations.tsv` compiles MCMCTree calibration priors from TimeTree (with the required Kumar et al. 2022 citation printed on every query); adopt the TSV alongside the dating inputs. For the stricter reviewed-constraint workflow, `operon timetree fetch` freezes the raw per-pair evidence into an immutable snapshot and `operon timetree calibrate` compiles only approved, rationalized soft bounds onto the rooted tree. See [timetree](../reference/cli-analysis.md#timetree).

## Dating with TimeTree

The `timetree` group is the supported path to secondary calibrations; it prints the required Kumar et al. 2022 citation on every query, and every response is cached under `adapters_cache/timetree/`. A cache record keeps the request URL, HTTP status, retrieval time, and the verbatim response body. An unreadable cache record is a hard error rather than a silent re-query, so damaged cache evidence is never passed off as a fresh result. Only the exact queries you make are cached — TimeTree's terms forbid mirroring or redistributing the database.

Five query subcommands cover the exploratory work:

```bash
operon timetree taxon --name "Arabidopsis thaliana"
operon timetree pairwise --taxon "Arabidopsis thaliana" --taxon "Oryza sativa"
operon timetree mrca --taxa A,B,C
operon timetree timeline --taxon "Arabidopsis thaliana"
operon timetree calibrations --taxa A,B,C --out calibrations.tsv
```

`taxon` resolves a scientific name to candidate taxon IDs and never auto-picks among ambiguous names — the error lists the candidates so you can pass `--taxon-id`. `pairwise` and `mrca` report divergence-time summaries for exactly two taxa and for the MRCA of N taxa; `timeline` lists the node timetable from one taxon back to the last universal ancestor; `calibrations` builds the MCMCTree prior table (one whole-set MRCA row, or one row per pair with `--pairs`), and `--out` also writes it as a TSV that `operon adopt` can register. `--format json`, `--refresh`, `--timeout`, `--retries`, and `--delay` behave as described in the [timetree reference](../reference/cli-analysis.md#timetree).

For the stricter reviewed-constraint path, `operon timetree fetch --pairs pairs.tsv --output snapshot/` freezes the per-pair evidence into a new immutable snapshot directory (an existing output directory is never overwritten):

- `snapshot.json`: the checksummed manifest, with one record per pair and a SHA-256 for every stored response.
- `raw/<taxon_a>_<taxon_b>.<flag>.json`: the verbatim TimeTree responses, where `<flag>` is `summaryjson` (the divergence-time summary) or `json` (the per-study evidence).
- `candidates.tsv`: a review worksheet with one row per pair — names, `age_ma`, the reported confidence interval, `studies`, and an `approved` column that starts as `no`.

`operon timetree calibrate --snapshot snapshot/ --tree species.nwk --taxa taxa.tsv --constraints constraints.tsv --output dated/` then compiles only approved, rationalized bounds onto a rooted, strictly bifurcating tree. The input contracts are:

- `--taxa` TSV header `leaf,taxon_id`: every tree leaf exactly once, each mapped to one unique positive NCBI taxon ID.
- `--constraints` TSV header `taxon_a,taxon_b,members,min_ma,max_ma,approved,rationale`: the pair must be present in the snapshot and in the taxa table, `members` must equal the exact comma-separated leaf set of the target MRCA clade, `approved` must be `yes`, and `rationale` must be non-empty. `min_ma` must be less than `max_ma`, and bounds may not contradict ancestor/descendant ordering.

The output directory contains `calibrated.tree` (PAML format: a `<leaf-count> 1` header line followed by the topology with branch lengths removed and `B(low,high)` labels on the constrained nodes), a byte copy of `constraints.tsv`, and `provenance.json` with the SHA-256 values of the snapshot, the input tree, the taxa table, the constraints table, and the calibrated tree. `--unit-ma` (default 100) states how many Ma one time unit represents, so `min_ma=20, max_ma=30` with the default becomes `B(0.2,0.3)`.
