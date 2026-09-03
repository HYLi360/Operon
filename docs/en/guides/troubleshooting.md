# Troubleshooting

## Checksum and format problems

```bash
operon verify          # Find MISSING / CHECKSUM_FAILED
operon status          # Inspect entity-level state
operon query "SELECT file_id, entity_type, entity_id, file_role, status, relative_path FROM files WHERE status != 'CHECKSUM_VERIFIED'"
```

Recommended actions:

| State | Action |
|---|---|
| `REMOTE_ONLY` | Expected state. Use `operon locations` for cached locations, `operon verify` for live verification, and `pull` when local bytes are required. |
| `REMOTE_UNVERIFIED` (verify output only) | The remote is temporarily unreachable and the copy's survival is unconfirmed. Check SSH/networking and rerun `verify`. |
| `MISSING` | Restore the file to `relative_path` or archive the source again as a new entity version. |
| `CHECKSUM_FAILED` | Stop QC. Determine whether the file was modified and restore it from the original source. |
| `QC_FAILED` | Inspect files with `parseable=0` in `operon report qc`, then use `operon workflow list --step qc --status failed` and `operon workflow show WF_ID` for the recorded error and execution details. |
| Format parsing failure | Check with an external validator such as `seqkit stats` or a GFF3 validator. Archive the repaired file as a new version; do not overwrite raw data. |
