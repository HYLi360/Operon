# Daily Workflow

## Single-file pipeline

For a single file whose entity record already exists:

```bash
operon run-pipeline \
  --source /data/GCA_999999999.fna.gz \
  --entity-type assembly \
  --entity-id ASM_000001 \
  --role genome_fasta \
  --profile assembly_production_v1
```

The pipeline runs:

```text
ingest -> standardize (including checksum verification) -> QC -> evaluate
```

## Recommended routine sequence

```bash
# 1. Add or update metadata
operon import dataset
operon add ...
operon import table --table ...

# 2. Archive new data
operon ingest ...
operon verify

# 3. Standardize and run QC
operon standardize
operon qc

# 4. Run external QC or encapsulated analyses
operon import-qc --file ...
operon tools-check
operon analyze --analysis blastn_nt
operon report analysis --analysis blastn_nt

# 5. Evaluate and release
operon evaluate --profile ...
operon report decisions
operon release --version ... --profile ...
```

## Common problems

| Symptom | Cause and action |
|---|---|
| `no project.yaml found` | The current directory is outside a project. Use `--project /path` or change to the project root. |
| `already has FIL_... for role ... with sha256 ...` | A different byte sequence already exists for the same entity and role. Raw data is immutable; create a new assembly/run version instead of overwriting it. |
| `CHECKSUM_FAILED` | The file changed after archiving. Restore the original bytes or archive the source again as a new entity version. |
| Table import reports a field error | Read the row and field in the error, fix the CSV/XLSX, or extend `config/schemas.yaml` first. |
| `query` rejects UPDATE/PRAGMA | This is intentional. Use controlled commands such as `add`, `import table`, or `curate` to modify data. |
| `tools-check` reports `cannot launch ...` | Edit `executable` or `run_method` in `config/tools.yaml`. See [External Analysis](../guides/external-analysis.md). |
| `analyze` reports that the database does not exist | Set the recipe `database` to the actual BLAST/HMM database path. |
