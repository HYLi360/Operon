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

Supported operators are `>=`, `<=`, `>`, `<`, `==`, `!=`, `between` (requires `min` and `max`), `in`, `not_in` (requires `values`), and `exists`.

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
