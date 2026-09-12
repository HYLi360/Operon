# QC Profiles

## Create a profile

Add a YAML file under `config/profiles/`, for example `phylogenomics_v1.yaml`:

```yaml
kind: qc
version: 1
description: Admission rules for phylogenomics
applies_to: [assembly]
required:
  - metric: sha256_match
    operator: "=="
    value: 1
    code: SHA256_MISMATCH
  - metric: parseable
    operator: "=="
    value: 1
    code: FORMAT_INVALID
  - metric: busco_complete_percent
    operator: ">="
    value: 90
    code: LOW_BUSCO_COMPLETENESS
  - metric: contamination_percent
    operator: "<="
    value: 3
    code: HIGH_CONTAMINATION
warnings:
  - metric: busco_duplicated_percent
    operator: ">"
    value: 20
    code: HIGH_BUSCO_DUPLICATION
```

Supported operators are `>=`, `<=`, `>`, `<`, `==`, `!=`, `between` (requires `min` and `max`), `in`, `not_in` (requires `values`), and `exists`. Comparisons are inclusive at the boundaries, and `in`/`not_in` compare metric values as strings. Note two profile-validation gaps: a `between` rule missing `min`/`max`, or an `in`/`not_in` rule missing `values`, is not rejected when the profile loads — the former surfaces as a Python traceback at evaluation time, and the latter silently evaluates against an empty value set.

Hand-editing the YAML file is fully supported. The audited alternative is the
TUI Config screen (`operon tui`, key `6`, QC Profiles tab): a structured form
(description, `applies_to` checkboxes, rule rows) that validates the composed
document, bumps the `version`, writes the file, and records the same
content-addressed snapshot `operon evaluate` records — with snapshot history
and restore-as-new-version built in.

Run the profile:

```bash
operon evaluate --profile phylogenomics_v1
operon report decisions --profile phylogenomics_v1
```

Every evaluation stores a profile content snapshot and appends a decision to the history.

## Select thresholds from another metric: `value_by`

Use `value_by` when the appropriate threshold depends on another metric. For BUSCO auto-lineage across Viridiplantae, BUSCO first selects the lineage and the profile then chooses the completeness threshold from the observed `busco_lineage_dataset`:

```yaml
kind: qc
version: 1
description: BUSCO 6.1.0 / odb12.2 auto-lineage gates for Viridiplantae
applies_to: [annotation]

required:
  - metric: busco_complete_percent
    operator: ">="
    value_by:
      metric: busco_lineage_dataset
      values:
        eudicotyledons_odb12.2: 70
        poales_odb12.2: 80
        fabales_odb12.2: 75
        lamiales_odb12.2: 70
        embryophyta_odb12.2: 70
        liliopsida_odb12.2: 75
        brassicales_odb12.2: 80
        solanales_odb12.2: 75
        malpighiales_odb12.2: 75
        rosaceae_odb12.2: 85
        chlorophyceae_odb12.2: 60
        viridiplantae_odb12.2: 65
        rosales_odb12.2: 90
        trebouxiophyceae_odb12.2: 80
        chlorophyta_odb12.2: 85
      unknown: warning
    source:
      qc_stage: analysis:busco_autolineage
    code: BUSCO_COMPLETENESS_FAIL
    unknown_code: BUSCO_LINEAGE_UNCONFIGURED
```

`value_by.metric` and the evaluated `metric` are read from the same source. When the selector value matches `values`, the selected number becomes the ordinary `value` for the operator.

Unknown-selector policies:

| `unknown` | Behavior for a required rule |
|---|---|
| `warning` | Do not fail the required rule, but create a warning. Suitable for a newly added BUSCO lineage. |
| `fail` | Fail the required rule. |
| `ignore` | Skip the rule. The decision is unaffected, but the ignored code is persisted in `reason_codes`. |

Warning rules normally use `warning` or `ignore`; these policies do not promote a warning to required failure. If `unknown` is omitted, the rule is treated as lacking a usable threshold and the result is `NOT_EVALUATED`. The default ignore code is `{SELECTOR}_IGNORED`; override it with `unknown_code`.

## Pin the metric source with `source.qc_stage`

An entity can have auto-lineage and multiple fixed-lineage BUSCO results. A formal decision must not depend on which same-named metric was written most recently. Bind the rule to a source:

```yaml
source:
  qc_stage: analysis:busco_autolineage
```

If the required metric is absent from that stage, the result is missing-metric/`NOT_EVALUATED`; Operon does not fall back to another stage. A fixed lineage can also be selected:

```yaml
source:
  qc_stage: analysis:busco_lineage:lineage_dataset=fabales_odb12.2
```

## Built-in Viridiplantae BUSCO profile

New projects contain:

```text
config/profiles/annotation_busco_viridiplantae_odb12_v1.yaml
```

The profile binds `analysis:busco_autolineage` and implements four checks:

1. Lineage-specific lower bound for complete BUSCOs; below the bound is `FAIL`.
2. Complete BUSCOs below the suggested pass line produce `PASS_WITH_WARNINGS`.
3. Fragmented BUSCOs above lineage-specific empirical levels produce `BUSCO_FRAGMENTED_HIGH`.
4. Duplicated BUSCOs above lineage-specific empirical levels produce `BUSCO_DUPLICATION_REVIEW` for review rather than immediate failure.

The thresholds were estimated on 2026-08-27 from BUSCO 6.1.0/odb12.2 results for 532 Viridiplantae annotations. They are an empirical profile for the current study set, not an official BUSCO standard. When BUSCO/OrthoDB, taxonomic scope, or intended use changes, copy the profile to a new version and re-estimate thresholds; do not modify the old profile silently.

Run it:

```bash
operon evaluate \
  --profile annotation_busco_viridiplantae_odb12_v1 \
  --entity-type annotation
operon report decisions \
  --profile annotation_busco_viridiplantae_odb12_v1
```

Existing projects are not overwritten by `operon init`. Copy the profile from a new project template or create the versioned YAML manually under `config/profiles/`.

## Sequence classification profiles

A profile of `kind: sequence_classification` labels individual sequences instead of deciding entities. The alignment hits stored in `analysis_alignments` are observed data; the profile decides. Run it with `operon classify-sequences --profile NAME` (see the [command reference](../reference/cli-analysis.md#classify-sequences)); labels land in `sequence_labels` with full audit. Every threshold lives in this YAML — never in code.

```yaml
kind: sequence_classification
version: 1
description: bHLH tier classification from CDD core hits plus a Pfam rescue
applies_to:
  entity_type: annotation
  file_role: protein_fasta
sources:
  core:
    analysis: rpsbproc_cdd
    # A core hit is CDD cl00081 or a bhlh/bhlh_* short name ...
    filter:
      - any:
          - {field: subject_id, operator: "==", value: cl00081}
          - {field: short_name, operator: like, value: bhlh}
          - {field: short_name, operator: like, value: bhlh_%}
      # ... but a bhlh-myc_n short name never counts as a core hit.
      - not: {field: short_name, operator: "==", value: bhlh-myc_n}
    best_by:
      - {field: hit_type, rank: {Specific: 0, Motif: 1, Partial: 2}}
      - {field: incomplete, rank: {"-": 0, NC: 1}}
      - {field: evalue, direction: asc}
      - {field: bitscore, direction: desc}
      - {field: span, direction: desc}
  rescue:
    analysis: hmmsearch_pf00010
    filter:
      - {field: subject_id, operator: "==", value: PF00010}
    best_by:
      - {field: evalue, direction: asc}
rules:
  - label: A
    source: core
    when:
      - {field: hit_type, operator: "==", value: Specific}
      - {field: incomplete, operator: "==", value: "-"}
      - {field: span, operator: ">=", value: 40}
  - label: B
    source: core
    when:
      - {field: span, operator: ">=", value: 30}
      - {field: incomplete, operator: "!=", value: NC}
  - label: R
    source: rescue
    when:
      - {field: i_evalue, operator: "<=", value: 1e-5}
      - {field: span, operator: ">=", value: 30}
  - {label: U, source: core, absent: true}
  - {label: C, default: true}
```

Grammar:

- `applies_to` is a mapping with `entity_type` and `file_role` (unlike the list form of `kind: qc` profiles); it selects the target manifest files. Superseded and effectively retired entities are excluded.
- `sources` names the hit sources the rules can reference. Each source declares:
  - `analysis`: the analysis name whose `analysis_alignments` rows count; only rows of the latest `completed` job per target file contribute.
  - `filter`: which rows count as hits at all (a list of conditions, AND-ed; empty means every row).
  - `best_by`: an ordered best-hit ranking; the first surviving row per seqid is the best hit the rules see. Each entry is a `field` with `direction: asc|desc` (default `asc`), or a `rank` map from value to rank (unmapped values sort after all mapped ones; an entry-level `default` sets their rank explicitly). Without `best_by`, rows order by `hit_rank` ascending.
- `rules` are evaluated in order and the first match wins. A rule carries a `label` plus exactly one of: a non-empty `when` list of conditions checked against the source's best hit, `absent: true` (the source has no hit row at all for the sequence — tier U above), or `default: true` (catch-all; takes no `source`/`when`/`absent`). A sequence matched by nothing stays unlabeled.
- Conditions — the grammar is shared between `filter` and `when` — are mappings with a `field` and an `operator`: `>=`, `<=`, `>`, `<`, `==`, `!=` (numeric, with string fallback for equality), `in`/`not_in` (with a `values` list), `between` (with `min`/`max`), `exists`, and `like` (case-insensitive SQL LIKE pattern with `%` and `_` wildcards). A condition may instead be `any: [...]` (a group whose conditions are OR-ed) or `not: {...}` (negation of a single condition); the top-level list is always AND-ed.
- Fields resolve against the alignment columns (`subject_id`, `hit_rank`, `query_start`, `query_end`, `evalue`, `bitscore`, `percent_identity`, …), then the derived `span` field (`query_end - query_start + 1`) and `seqid` (the query id up to the first whitespace), and finally the keys of the hit row's `extra_json` — parser-specific fields such as `hit_type`, `incomplete`, `short_name`, or `i_evalue`. A missing field makes the test False for every operator (including `!=`, `not_in`, and `exists`), so only wrapping the condition in `not: {...}` makes it hold — the negation of that False is True.

Re-running with the same profile content and the same inputs is a no-op; editing the profile re-decides the affected sequences and audits every label change in `changes`.
