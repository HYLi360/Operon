# NCBI Taxonomy Coverage

This feature answers two related questions: which families/genera are registered in project metadata, and which families/genera were actually published in an immutable release. Both scopes are calculated against a precompiled denominator snapshot with checksums. Reports do not traverse a moving "latest" taxonomy, so historical numbers do not drift when NCBI Taxonomy changes.

Only NCBI Taxonomy is supported. GTDB TaxIDs do not enter the numerator and are not mapped by name.

## Workflow

```text
NCBI taxonomy_report.jsonl / Datasets package / taxdump archive
        | operon taxonomy import --version <explicit-version>
        v
Immutable source package + taxonomy_snapshots/nodes/aliases
        |
coverage YAML profile -- operon taxonomy compile
                                  |
                                  v
                  taxonomy/reference_sets/
                  <profile>@<taxonomy_version>.tsv
                                  |
                                  v
                      operon report coverage
                         ├── metadata scope
                         └── frozen release scope
```

Import, compilation, and reporting are explicit commands. Imported source files enter the file manifest and are identified by `file_id + sha256 + size_bytes`. Compilation is audited in `changes`; run details are written to `workflow_runs` and `logs/workflow.jsonl`.

## Prepare a coverage profile

`operon init` creates `config/profiles/coverage_viridiplantae_v1.yaml` as an example. It is a template, not a universal standard. Before production use, review the clade, exclusion rules, and thresholds, and save the result under a filename that reflects the study scope. QC and coverage profiles share the directory but are distinguished by the required `kind` field.

```yaml
kind: taxonomy_coverage
version: 1
name: coverage_viridiplantae_v1
description: NCBI Viridiplantae family/genus coverage

taxonomy:
  source: NCBI

scope:
  root_taxids: [33090]

targets:
  ranks: [family, genus]

filters:
  exclude_extinct: true
  exclude_subtrees: []
  exclude_name_patterns:
    - '(?i)^unclassified(?:\s|$)'
    - '(?i)environmental samples$'

thresholds:
  family:
    min_coverage_percent: 80
  genus:
    min_coverage_percent: 80
```

Constraints:

- `taxonomy.source` must be `NCBI`.
- `root_taxids` and `exclude_subtrees` must contain integer TaxIDs.
- `targets.ranks` may contain unique `family` and/or `genus` values.
- `thresholds` must correspond exactly to target ranks and use values from 0 to 100.
- Name exclusions are Python regular expressions and are validated during compilation.
- Thresholds come only from YAML; no family/genus default is hard-coded.

`exclude_extinct: true` requires explicit extinct Boolean semantics for every node in the taxonomy snapshot. Omitted Boolean values in NCBI Datasets taxonomy JSON use the schema default `false`. Traditional taxdump `nodes.dmp` has no extinct field, so compilation rejects the combination instead of pretending that the rule ran. Set it explicitly to `false` and define fossil exclusions with `exclude_subtrees` or name patterns, or import a Datasets report that includes extinct annotations.

Exclusion rules apply both to the compiled denominator and to lineage projection of sample TaxIDs. An excluded environmental, unclassified, or extinct observation cannot cover an ancestor target.

## Import an explicit NCBI Taxonomy version

```bash
operon taxonomy import \
  --input /data/ncbi_taxonomy/2026-08-01/taxonomy_report.jsonl \
  --version 2026-08-01

operon taxonomy list
```

`--input` accepts a Datasets `taxonomy_report.jsonl`, a ZIP/tar containing that member, or an official taxdump ZIP/tar containing at least `nodes.dmp` and `names.dmp`. Optional `merged.dmp` and `delnodes.dmp` are imported as current/deleted TaxID mappings. `--version` is the immutable project version label; use a download date or release identifier, not `latest`.

After import:

- The source is stored by SHA-256 under `raw/metadata/ncbi_taxonomy/`.
- `files` records a `taxonomy_snapshot` or `taxonomy_package` manifest row.
- `taxonomy_snapshots` records source, version, SHA-256, size, node count, and status.
- `taxonomy_nodes` stores TaxID, parent, rank, scientific name, and extinct flag.
- `taxonomy_aliases` maps secondary/merged TaxIDs to current TaxIDs.

Repeating an import with the same version and bytes reuses the snapshot. The same version label with different bytes is a conflict and is rejected.

## Compile the immutable denominator

```bash
operon taxonomy compile \
  --profile coverage_viridiplantae_v1 \
  --taxonomy-version 2026-08-01

operon taxonomy reference-sets
```

The output identity is `<profile-filename>@<taxonomy_version>`:

```text
taxonomy/reference_sets/
├── coverage_viridiplantae_v1@2026-08-01.tsv
└── coverage_viridiplantae_v1@2026-08-01.provenance.json
```

Each TSV row is a target taxon with fixed columns:

```text
rank    taxid    scientific_name
family  12345    Exampleaceae
genus   67890    Examplea
```

Rows are deterministically sorted by rank (`family`, then `genus`) and TaxID. The database and provenance sidecar record taxonomy version and source SHA-256, complete profile document and SHA-256, TSV SHA-256/size, row count per rank, compiler version, and workflow run ID.

Identical profile, taxonomy, and output are reused idempotently without adding audit records. An existing `<profile>@<taxonomy_version>` with different profile content, taxonomy snapshot, or TSV bytes is not overwritten. To change the denominator, create a new profile name (for example `_v2`) or import a new taxonomy version, then compile explicitly. A new compilation is recorded in `changes`.

## Generate a coverage report

### Metadata scope (default)

```bash
operon report coverage \
  --reference-set coverage_viridiplantae_v1@2026-08-01
```

This scope audits the `organisms` table and answers "what is registered as sampled in the database?" Only organisms with `taxonomy_source=NCBI` and a parseable TaxID enter lineage projection. Secondary TaxIDs are mapped through the frozen taxonomy alias table. GTDB, missing TaxIDs, TaxIDs unknown to the snapshot, and observations excluded by the profile are listed as exclusions and do not enter the numerator.

### Release scope

```bash
operon report coverage \
  --reference-set coverage_viridiplantae_v1@2026-08-01 \
  --release 2026.08
```

This scope verifies member identity and file checksums through `release_members`, then reads the frozen metadata TSV files in the release directory and follows member relationships back to organisms. It answers "what does this published dataset cover?" It does not replace the release snapshot with current active organism TaxIDs. Metadata SHA-256 values saved at release creation are rechecked. A modified, missing, or inconsistent snapshot is rejected. Development-era releases that predate this checksum contract must be recreated before release-scope coverage can use them.

### Formula and outputs

Each target rank is calculated independently:

```text
numerator   = number of snapshot TaxIDs covered by at least one in-scope organism
denominator = number of TaxIDs for the rank in the denominator snapshot
coverage    = numerator / denominator * 100%
```

Sampling several organisms from one family/genus counts that target once; `organism_count` is reported separately as diagnostic information. The report directory is determined by the SHA-256 of all input identities:

```text
reports/coverage/COV_<input-hash>/
├── coverage_summary.tsv
├── coverage_targets.tsv
├── coverage_missing.tsv
├── coverage_observations.tsv
├── coverage_excluded_observations.tsv
└── provenance.json
```

The exit code is 0 when every configured rank meets its YAML threshold. If a report is generated but at least one rank misses its threshold, the exit code is 1. Configuration, identity, checksum, and conflict errors return 2. A threshold miss is not a calculation failure; the report is still written and recorded in `coverage_reports` and `coverage_report_metrics`.

Input identity includes reference-set SHA-256, profile SHA-256, scope, release version when present, and member hash. Identical input validates and reuses an existing report. Changed active metadata or release membership creates a new report ID instead of overwriting the old one.

## Interpret missing and excluded lists

`coverage_missing.tsv` lists denominator targets with no eligible observation. It can be used directly as a candidate list for future sampling. It differs from `coverage_excluded_observations.tsv`, which lists organisms that exist in the database but did not enter the numerator because of TaxID or scope issues, including:

- `UNSUPPORTED_TAXONOMY_SOURCE`: GTDB or another non-NCBI source
- `MISSING_TAXID`: no TaxID
- TaxID absent from the snapshot with no usable secondary alias
- `EXCLUDED_EXTINCT`, name-pattern exclusion, or excluded-subtree match
- `MISSING_TARGET_RANK`: no required family/genus rank in the lineage
- `OUTSIDE_REFERENCE_SCOPE`: rank exists but the TaxID is outside the frozen reference set

Reports do not infer TaxIDs from scientific names. Fix `organisms` metadata or use a taxonomy snapshot with the correct aliases, then generate a new report with a new identity.

## Known limitations

- Only NCBI Taxonomy is supported; GTDB and NCBI↔GTDB crosswalks are not implemented.
- Users obtain and import taxonomy data explicitly; Operon does not download a hidden `latest` version.
- Traditional taxdump lacks extinct Boolean annotations. To prevent silent weakening of exclusion rules, `exclude_extinct: true` is rejected for those snapshots.
- Family/genus coverage measures sampling breadth, not assembly quality, annotation completeness, phylogenetic representativeness, or clade balance.
- NCBI ranks, names, merged TaxIDs, and extinct annotations change between versions. Do not compare numbers across versions without stating the denominator snapshot.
- Release scope counts organisms reachable through frozen metadata relationships from release members. Incomplete relationships are excluded explicitly rather than inferred from filenames or names.
