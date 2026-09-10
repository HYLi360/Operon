# Remote-First Operation

The remote-first mode targets setups where the data volume and the compute live on a remote HPC system, while a workstation or laptop only records events and collects results:

- The local project is the control plane: `operon.sqlite` is the sole writable source of truth, and every transfer, eviction, run, and QC import lands in `workflow_runs`, `changes`, and `logs/workflow.jsonl`.
- The remote mirror holds the bytes: an SFTP mirror keeps checksum-verified copies of every evicted file.
- The remote host runs the compute: `run-external` and `analyze` submit commands through the `ssh` execution backend (optionally remote Slurm), and the built-in QC parsers run remotely through `qc-measure`.

```text
workstation (control plane)                HPC (bytes + compute)
  operon.sqlite, config/, logs/    SFTP     remote mirror root
  decisions, QC, provenance      <------->  (operon-manifest.json + objects)
  operon CLI / TUI                 SSH      ssh/slurm backend + qc-measure
```

This page combines [SFTP Remote Storage](remote-storage.md) and [Remote Execution with Slurm and SSH](remote-execution.md) into one workflow; the field-level details stay on those pages.

## Configuration

One SFTP mirror plus one SSH execution section pointing at the same mirror:

```yaml
# project.yaml
remotes:                            # field details: SFTP Remote Storage
  mycluster:
    type: sftp
    host: hpc.example.org
    user: hyli360
    root: /data/operon-mirror

execution:                          # field details: Remote Execution
  backend: ssh
  ssh:
    storage_remote: mycluster       # same mirror; host and root are inherited
    scheduler: slurm                # or none for direct execution on the host
```

With `storage_remote` set, a locally absent (`REMOTE_ONLY`) input is verified against the local manifest, the remote manifest, and the actual remote bytes, then consumed in place under the remote root — it is never pulled back to the workstation just to run a job.

## End-to-end workflow

### a. Archive locally to establish trusted identity

```bash
operon ingest --source ASM.fna.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta
```

### b. Mirror the bytes to the remote

```bash
operon push --remote mycluster --file-id FIL_000001
```

`push` verifies the remote content and records `file_locations`; see [push](../reference/cli-remote.md#push).

### c. Evict the local bytes

```bash
operon evict --remote mycluster --file-id FIL_000001
operon locations --file-id FIL_000001
```

`evict` deletes the local copy only after a triple verification: remote manifest identity, a live SHA-256 check of the actual remote bytes, and a final check of the local bytes. Afterward the file status is `REMOTE_ONLY`, a small placeholder pointer remains under `.operon/placeholders/<file_id>.json`, and the state change is audited in `changes`. See [evict](../reference/cli-remote.md#evict).

### d. Run analysis remotely

Arbitrary commands go through `run-external`; configured recipes go through `analyze`:

```bash
operon run-external --step quast --backend ssh \
  --command 'quast -o qc/quast_out raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta' \
  --expected-output qc/quast_out/report.tsv

operon analyze --analysis blastn_nt --backend ssh \
  --entity-type assembly --entity-id ASM_000001
```

`REMOTE_ONLY` inputs are consumed in place through `storage_remote`; expected outputs are retrieved back into the project after a successful run. See [Remote Execution with Slurm and SSH](remote-execution.md).

### e. Run the built-in QC remotely

Install `operon` on the HPC (see the limitations below), measure the file in place, pull the JSON payload back as a run output, and import it into `qc_results`:

```bash
operon run-external --step qc_builtin --backend ssh \
  --entity-type assembly --entity-id ASM_000001 \
  --command 'operon qc-measure \
    --file raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta \
    --format fasta --role genome_fasta \
    --sha256 <sha256> --size-bytes <size> --file-id FIL_000001 \
    --out qc/metrics/FIL_000001.json' \
  --expected-output qc/metrics/FIL_000001.json

operon import-qc --file qc/metrics/FIL_000001.json
operon evaluate
```

`qc-measure` verifies the byte identity before parsing and produces the same stage/metric names as local `qc`; `import-qc` recomputes the affected entities' QC state and records an `import-qc` step in `workflow_runs`; `evaluate` then applies the versioned profile as usual. See [qc-measure](../reference/cli-files-qc.md#qc-measure) and [import-qc](../reference/cli-files-qc.md#import-qc).

The command needs the manifest identity of the file. Get it with any of:

```bash
operon locations --file-id FIL_000001     # residency per file
operon verify --file-id FIL_000001        # live check; prints current_sha256
operon report metadata                    # reports/metadata/files.tsv: full manifest
operon query "SELECT file_id, file_role, format, sha256, size_bytes FROM files WHERE file_id='FIL_000001'"
```

### f. Pull bytes back before publishing

`standardize`, `release`, and `export` need local bytes, so hydrate the required files first:

```bash
operon pull --remote mycluster --file-id FIL_000001
```

### g. Back up both planes

With `REMOTE_ONLY` files, the local backup must include the SQLite database holding `file_locations`, and the remote mirror root (including `operon-manifest.json` and the objects) needs its own independent backup. Placeholder files are not recovery evidence. See [Backup, Migration, and Resumption](backup-migration.md).

## Limitations

- `run-external` and `analyze` are synchronous and blocking: the local CLI process holds the remote job for its whole lifetime. Interrupting the process or hitting `--timeout` cancels the remote job (`scancel` for remote Slurm, TERM/KILL to the process group for direct SSH). There is no submit-and-disconnect mode that recovers a job later.
- `qc-measure` requires `operon` installed on the HPC side (this documentation covers {{ operon_version }}):

  ```bash
  pip install OperonDBS==<version>
  ```

  `import-qc` only prints a warning when the payload's `tool_version` differs from the local installation, but metric semantics can drift between versions — keep the two installations on the same version.
- `operon qc` is a local-only command (no `--backend`). Files whose status is `REMOTE_ONLY` are skipped, not failed: no `qc_results` rows are written, the entity state is untouched, a `SKIPPED` warning goes to stderr, and an explicit `--file-id` run where every selected file is skipped exits with code 1. For such files either `pull` the bytes back and run `qc` locally, or use the `qc-measure` + `import-qc` path above.
- `operon tools-check` always probes tool versions on the local machine, regardless of the configured execution backend. (Version detection *during* `analyze`/`run-external` runs through the configured backend.)
