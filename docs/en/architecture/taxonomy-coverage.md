# Taxonomy coverage architecture

## NCBI Taxonomy coverage snapshots

Taxonomy coverage is separate from the NCBI genome adapter: it reads an NCBI Datasets `taxonomy_report.jsonl`/package, or an official taxdump archive containing `nodes.dmp` and `names.dmp` (plus optional `merged.dmp`/`delnodes.dmp`). The original package is archived by SHA-256 under `raw/metadata/ncbi_taxonomy/`, and tree nodes plus secondary TaxIDs are imported into `taxonomy_snapshots/nodes/aliases`. The version label must be specified explicitly by the caller; the same version with different bytes is rejected as a conflict.

`config/profiles/*.yaml` distinguishes `qc` from `taxonomy_coverage` through the required `kind`. A coverage profile declares one or more root TaxIDs, family/genus target ranks, extinct/excluded-subtree/name-regex rules, and per-rank thresholds. `taxonomy compile` traverses descendants against a concrete taxonomy version and produces a deterministically sorted `taxonomy/reference_sets/<profile>@<taxonomy_version>.tsv`. SHA-256 is recorded for the TSV, the taxonomy source package, and the profile; identical input is reused idempotently, different content is never overwritten, and the first compilation enters the `changes` audit.

The extinct boolean in Datasets JSON supports `exclude_extinct`; classic taxdump has no such field, and its nodes are stored as unknown. If a profile requests extinct exclusion, the compiler rejects that combination and requires explicit excluded-subtree/name rules or a snapshot with extinct annotations, rather than silently changing the computation basis.

`report coverage` reads only this TSV denominator:

- The metadata scope reads `organisms` directly, expressing "what the database has registered as sampled";
- The release scope validates `release_members` and the release manifest, tracing organisms back through the metadata tables frozen inside the release directory, expressing "what the published dataset covers".

The numerator is the number of distinct family/genus TaxIDs after projection onto the reference set, not the number of organisms. Secondary TaxIDs can be mapped through aliases of the same taxonomy snapshot; non-NCBI, missing/unknown TaxIDs, and profile exclusions go into the exclusion list — no name guessing. The report outputs the summary, complete targets, missing targets, included/excluded observations, and provenance; identical input identity is verified and reused, and new reports are appended when metadata/release membership changes. For the detailed contract, see [NCBI Taxonomy coverage](../guides/taxonomy-coverage.md).
