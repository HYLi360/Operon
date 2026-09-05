# Curation, Queries, and Entity Lifecycle

## Override a decision manually

```bash
operon curate \
  --entity-type assembly --entity-id ASM_000003 \
  --profile assembly_production_v1 \
  --decision PASS \
  --reviewer "zhang.san" \
  --reason "Elevated N content comes from known centromeres and does not affect this study" \
  --evidence "Cytology report dated 2026-08-01"
```

Rules:

- Only the `curated_*` fields of the latest decision for the entity/profile are changed.
- The automatic decision and older decision history remain unchanged.
- The update is recorded in the `changes` audit table.
- The state machine updates to `ACCEPTED`, `REJECTED`, or `REVIEW` according to the curated decision.

## Run read-only SQL queries

`query` is read-only. Examples:

```bash
# All PASS/FAIL states
operon query "SELECT entity_type, entity_id, decision, reason_codes FROM current_decisions"

# One assembly and its files
operon query "
SELECT a.assembly_id, f.file_id, f.file_role, f.relative_path, f.sha256
FROM assemblies a JOIN files f ON f.entity_id=a.assembly_id
WHERE a.assembly_id='ASM_000001'
"

# Wide-table-style view of QC metrics
operon query "
SELECT entity_id, metric_name, metric_numeric, metric_unit, evaluated_at
FROM qc_results
WHERE entity_type='assembly' AND qc_stage='assembly_basic'
ORDER BY entity_id, metric_name
"

# Database schema version marker
operon query "SELECT entity_id, state, message FROM entity_state WHERE entity_type='database'"
```

`SELECT` and read-only PRAGMA statements such as `PRAGMA table_info` are allowed. `UPDATE`, `INSERT`, `DROP`, `PRAGMA user_version=...`, and `ATTACH` are rejected.

To inspect an organism-rooted data tree without writing joins:

```bash
operon show NCBI_Taxonomy:3702
operon show ORG_000001
operon show GCF_000001405.40 --json
```

`show` resolves any matched entity upward to its organism. The default `--scope matched` lists the matched entity's upstream lineage and its own subtree, rather than counting every assembly under the same organism. Use `--scope organism` for the complete organism graph. Superseded and retired descendants are hidden by default; use `--include-superseded` and `--include-retired` for the full history. Use `namespace:accession` when a bare accession is ambiguous.

## Retire and restore entities

When an assembly, annotation, or an entire organism subtree was imported accidentally, comes from an unsuitable source, or was withdrawn upstream, retire it logically. Do not delete database rows or raw files.

Always begin with a read-only preview:

```bash
operon retire GCA_000751015.1 \
  --reason-code accidental_import \
  --reason "Imported into this project by mistake; waiting for re-import from the correct source"
```

The JSON plan lists the target, owned subtree, file count and paths, and references from accessions, QC, decisions, analysis, workflows, sources, remote locations, and releases. Confirm that all `physical_changes` are zero and review existing release and remote references; retirement does not remove them.

Apply the plan explicitly:

```bash
operon retire GCA_000751015.1 \
  --reason-code accidental_import \
  --reason "Imported into this project by mistake; waiting for re-import from the correct source" \
  --evidence "Import batch review dated 2026-09-01" \
  --actor hyli360 --apply --yes
```

Retirement appends a lifecycle event, `changes` audit row, and workflow provenance. It does not move or delete files and does not rewrite existing QC, analysis, or releases. State propagates along ownership: organism → sample → run/assembly → annotation, sample → run/assembly → annotation, and assembly → annotation. Active `show` counts, status/report, bulk QC/evaluate/analyze, targeted `run-external`, new releases, and NCBI reuse exclude these entities by default.

Inspect current status:

```bash
operon retired
operon retired --direct-only
operon show GCA_000751015.1 --include-retired
```

Preview and then apply restoration:

```bash
operon restore GCA_000751015.1 --reason "Source mapping confirmed"
operon restore GCA_000751015.1 --reason "Source mapping confirmed" \
  --actor hyli360 --apply --yes
```

Restoration is the strict inverse operation: it appends `RESTORE` and points back to the target's most recent direct `RETIRE`; history is not deleted. A descendant that inherited retirement from an ancestor cannot be restored separately; restore the root identified by the plan. If the child also has an independent direct retirement, restoring the parent does not restore that child.

For databases older than schema 2.7, run `operon migrate` first.

## Force a state transition manually

`operon set-state` performs an audited manual state change when a transition is needed that the automatic workflow does not produce (for example, recovering a stuck entity):

```bash
operon set-state --entity-type assembly --entity-id ASM_000001 --state QC_COMPLETE \
  --message "Manual review confirmed metrics are complete" --force
```

The normal path enforces the legal transition table and appends a `changes` audit row with the message; `--force` bypasses the transition check for manual recovery while the audit row keeps the action traceable. Note that `RELEASED` is a terminal state: entities published in a release cannot leave it without `--force`, and setting a state that equals the current state is a silent no-op. Prefer `curate` (for decisions) or rerunning the affected step (for provenance) whenever possible.

There is currently no `purge` command. Do not use manual SQL, `rm`, or remote-object deletion as a substitute. Physical deletion requires separately designed retention periods, release/remote reference protection, a restoration window, and irreversible confirmation. Until then, auditable retirement and restoration are the supported safe-disposal path.
