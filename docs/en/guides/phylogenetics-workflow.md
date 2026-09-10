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
  --decision PASS --profile default
```

`--decision` requires `--profile` and matches the effective decision in `current_decisions` (PASS, PASS_WITH_WARNINGS, and so on), so the workflow input is exactly the QC-approved subset. See the [export reference](../reference/cli-decisions-reports.md) for all selection criteria.

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

After the pipeline finishes, re-register the products worth keeping — key intermediates (the framework tree, per-subfamily alignments) and final results (the species tree, TreeShrink output, Notung reconciliation) — with a batch adopt manifest. The TSV form needs a header row with `path`, `entity_type`, `entity_id`, `role`, and `derived_from`; `format`, `compression`, and `workflow_run_id` columns are optional (auto-detected when omitted). `derived_from` carries comma-separated file IDs that must already be registered; relative paths resolve from the project root:

```text
path	entity_type	entity_id	role	format	compression	derived_from
analysis/external/superfamily_round1/results/framework.treefile	organism	ORG_000001	superfamily_framework_tree	other	none	FIL_000012
analysis/external/superfamily_round1/results/subfamilies/OG000102.aln.faa	organism	ORG_000001	subfamily_alignment	fasta	none	FIL_000012,FIL_000013
analysis/external/superfamily_round1/results/astral.species_tree.nwk	organism	ORG_000001	species_tree	other	none	FIL_000021
analysis/external/superfamily_round1/results/treeshrink/	organism	ORG_000001	treeshrink_output	directory	none	FIL_000021
analysis/external/superfamily_round1/results/notung/	organism	ORG_000001	notung_reconciliation	directory	none	FIL_000021,FIL_000022
```

```bash
operon adopt --from-manifest adopt_manifest.tsv
```

Roles are freely named by your workflow. Every product is materialized under `analysis/adopted/<entity_id>/` (tree/alignment files typically detect as format `other` or `fasta`; directories adopt as `format: directory` with `compression: none`), inherits the ingest idempotency/conflict invariants, and gains `file_lineage` edges back to its inputs. The full batch commits in one transaction; on failure only newly created artifacts are removed.

## Step 4: cascade analyses onto adopted products

An adopted file is a normal manifest member, so `analyze` recipes can select it as input through `entity_type + file_role + format`. For example, domain-scanning every adopted subfamily alignment with a recipe declaring `entity_type: organism`, `file_role: subfamily_alignment`, `format: fasta`, or re-QCing an adopted FASTA with `operon qc --file-id FIL_...`. Results flow into `analysis_results`/`analysis_alignments`/`qc_results` exactly like analyses over raw files, so each workflow round can build on the adopted output of the previous round.

## Step 5: evaluate and release

Adopted products participate in the standard quality gate. Run `operon qc` for the formats with built-in parsers (or `run-external` + `import-qc` for external metrics), then `operon evaluate --profile <name>` and finally:

```bash
operon release --version v1.0 --profile default
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
