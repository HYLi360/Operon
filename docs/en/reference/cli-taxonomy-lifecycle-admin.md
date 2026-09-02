# Taxonomy, lifecycle, and administration commands

## taxonomy

```bash
operon taxonomy import --input PATH --version VERSION
operon taxonomy list
operon taxonomy compile --profile NAME --taxonomy-version VERSION
operon taxonomy reference-sets
```

- `import`: archives and imports an NCBI Datasets `taxonomy_report.jsonl`/package, or an official NCBI taxdump ZIP/tar containing at least `nodes.dmp` and `names.dmp`; optional `merged.dmp`/`delnodes.dmp` files are converted into TaxID aliases. `--version` is an explicit, immutable taxonomy version label.
- `list`: shows source-file identity, version, node count, and import status.
- `compile`: reads the scope, ranks, exclusion rules, and thresholds of a `kind: taxonomy_coverage` profile in `config/profiles/<NAME>.yaml`, and generates `taxonomy/reference_sets/<NAME>@<VERSION>.tsv` plus a provenance sidecar.
- `reference-sets`: lists the family/genus row counts, SHA-256, and compile time of each frozen denominator.
- Same taxonomy version with different bytes, or the same reference-set identity with a different profile/result, is rejected as a conflict; repeating with identical input is idempotent and reuses the existing artifact.

For the full profile format and invariants, see [NCBI Taxonomy coverage](../guides/taxonomy-coverage.md).

## retire

```bash
operon retire IDENTIFIER \
  --reason-code {accidental_import,wrong_source,duplicate,withdrawn_upstream,policy_exclusion,metadata_error,other} \
  --reason TEXT [--evidence TEXT] [--actor NAME]

operon retire IDENTIFIER --reason-code accidental_import --reason TEXT \
  --apply [--yes] [--evidence TEXT] [--actor NAME]
```

By default the command prints only a JSON plan and does not modify the project. The plan resolves internal IDs/accessions and lists the target's ownership subtree, its associated files, and reference counts across accessions, QC, decisions, analyses, workflows, sources, remote locations, and release members/versions; `physical_changes` is explicitly zero. Databases older than schema 2.7 can be upgraded first with `operon migrate`; a read-only old copy is never migrated implicitly by the preview command.

Only `--apply` appends the direct `RETIRE` event, the `changes` audit entries, and the lifecycle workflow; non-interactive execution additionally requires `--yes`. Retirement does not delete or move files, does not change checksums, does not delete QC/analysis/workflow records, and does not alter existing releases. The effective state propagates along ownership: organism → sample → run/assembly → annotation, sample → run/assembly → annotation, assembly → annotation. Active ingest, QC, evaluate, analyze, `run-external`, report, NCBI reuse, and new releases reject or exclude these entities by default.

## restore

```bash
operon restore IDENTIFIER --reason TEXT [--evidence TEXT] [--actor NAME]
operon restore IDENTIFIER --reason TEXT --apply [--yes] [--evidence TEXT] [--actor NAME]
```

Also preview-only by default. `--apply` appends a `RESTORE` event that points back to the target's most recent direct retirement via `reverts_event_id` and `changes.reverts_change_id`; the retirement history is never deleted. It only undoes the target's own direct retirement: if the target merely inherited its state from an ancestor, restore the retirement root indicated in the plan. When a child entity has its own independent direct retirement, restoring the parent does not restore that child.

## retired

```bash
operon retired [--direct-only] [--json]
```

Lists all currently effective retired entities by default, together with `retired_by_type/id`, distinguishing direct retirement roots from inherited retired descendants. `--direct-only` lists only direct roots; `--json` emits machine-readable records. The command is read-only.

There is currently no `purge` command. Retire/restore provide safe isolation and a complete inverse operation only; physical removal would require separately defined reference protection, retention periods, remote copies, and irreversible confirmation, and must not be improvised with manual SQL or by deleting raw files.

## backup

```bash
operon backup create --output /backups/project-2026-08-28 --scope control
operon backup create --output /backups/project-full --scope full
operon backup verify --input /backups/project-2026-08-28
```

`create` uses a read-only database connection and the SQLite backup API to produce a consistent database snapshot, and does not trigger the new program's automatic migration first; the destination must lie outside the project directory and must not already exist.
Every backup includes `backup-manifest.json`, recording the size and SHA-256 of every member.

- `control` (default): `project.yaml`, `config/`, a consistent `operon.sqlite`, `logs/`.
- `results`: control plus `qc/analysis/reports/taxonomy/releases`; raw and standardized large files are not copied.
- `full`: results plus `raw/standardized/.operon/metadata/examples`.

`verify` does not need to open the original project; it checks path safety, size, and SHA-256 file by file, and compares the actual file set in the directory against the manifest: missing, modified, or unlisted extra files all fail verification. Remote mirrors should still be backed up independently; the control/results scopes keep only control-plane records such as `file_locations`, not the actual remote bytes.

## set-state

```bash
operon set-state --entity-type TYPE --entity-id ID --state STATE \
  [--message TEXT] [--force]
```

- Validates the transition; an illegal transition requires `--force` and is recorded in the `changes` audit table.
- Legal states include: `DISCOVERED`, `METADATA_FETCHED`, `METADATA_VALIDATED`, `DOWNLOAD_PENDING`, `DOWNLOADED`, `CHECKSUM_VERIFIED`, `STANDARDIZED`, `QC_RUNNING`, `QC_COMPLETE`, `ACCEPTED`, `REVIEW`, `REJECTED`, `RELEASED`, plus `DOWNLOAD_FAILED`, `CHECKSUM_FAILED`, `FORMAT_INVALID`, `METADATA_INVALID`, `STANDARDIZATION_FAILED`, and `QC_FAILED`.

## Exit codes

| Exit code | Meaning |
|---|---|
| `0` | Success |
| `1` | Command completed but checks did not pass, or a runtime failure occurred (e.g. coverage below a YAML threshold, verify/QC/external command failure) |
| `2` | `operon` domain error (configuration error, validation failure, entity not found, conflict, etc.) |
