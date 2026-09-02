# SFTP Remote Storage

In addition to local backups, `project.yaml` can define one or more SFTP mirrors. A mirror synchronizes manifest files to a remote endpoint with content verification.

## Configure a remote

```yaml
# project.yaml
remotes:
  mycluster:
    type: sftp
    host: hpc.example.org
    user: hyli360
    port: 22
    key_file: ~/.ssh/id_rsa
    root: /data/operon-mirror
    known_hosts: ~/.ssh/known_hosts
    # Alternatively pin an administrator-provided fingerprint:
    # host_key_sha256: SHA256:base64...
    insecure_accept_unknown_host: false
```

Install the optional dependency:

```bash
pip install 'operon[remote]'
```

List remotes and test connectivity:

```bash
operon remotes
```

The command exits with code 1 if any remote reports an error.

## Push, restore, and inspect locations

```bash
# Upload every manifest file.
operon push --remote mycluster

# Upload selected files.
operon push --remote mycluster --file-id FIL_000001 --file-id FIL_000002

# Restore all entries in the remote manifest.
operon pull --remote mycluster

# Show local and remote residency.
operon locations
```

The remote model preserves the raw-file invariants:

- Files and directory artifacts are verified by SHA-256 plus size and are idempotent. Directory hashes include relative paths, empty directories, file contents, and symlink targets. Different bytes at the remote path produce `ConflictError`.
- The remote maintains `operon-manifest.json` v2 with a `project_id`. Atomic replacement requires the SFTP server's OpenSSH `posix-rename@openssh.com` extension. If unsupported, the operation fails closed rather than deleting the old manifest before writing the new one.
- A batch push publishes the manifest once. Remote `.operon-manifest.lock` serializes read-modify-write access. If a crash leaves a lock, the error gives the exact path; remove it manually only after confirming that no push is active.
- Remote relative paths must remain safely under the remote root. By default, `pull` checks every record against local SQLite `file_id + relative_path + sha256 + size_bytes`; the remote manifest cannot rewrite local identity.
- Every transfer writes workflow provenance (`push:<name>` or `pull:<name>`), and successful locations are recorded in `file_locations`.
- A failed item does not stop the rest of a push/pull/evict batch. Every item receives a result, and the command exits with code 1 if any item has `error`.
- After `pull` restores a missing local file, `files.status` returns to `CHECKSUM_VERIFIED` and the change is audited in `changes`.

## Keep the control plane local and large files remote

A common HPC workflow is:

```bash
# 1. Archive locally first to establish trusted identity.
operon ingest --source ASM.fna.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta

# 2. Push to the remote; push verifies remote content and records file_locations.
operon push --remote mycluster --file-id FIL_000001

# 3. Verify the remote again, then remove local bytes.
operon evict --remote mycluster --file-id FIL_000001
operon locations --file-id FIL_000001

# locations is a cached view; verify checks the remote manifest and content live.
operon verify --file-id FIL_000001

# 4. Run remotely without downloading the input.
operon analyze --analysis blastn_nt --backend ssh \
  --entity-type assembly --entity-id ASM_000001

# 5. Hydrate the file only when a local workflow needs its bytes.
operon pull --remote mycluster --file-id FIL_000001
```

Point execution at the same remote mirror:

```yaml
execution:
  backend: ssh
  ssh:
    storage_remote: mycluster
    scheduler: slurm            # or none for direct execution on the SSH host
```

`evict` explicitly deletes local bytes; without `--file-id`, it processes every manifest object. It first validates local identity, remote manifest identity, and actual remote SHA-256/tree hash. The state change is written to `changes`. `standardize` and `release` require local bytes, so run `pull` first. External `analyze` can consume `REMOTE_ONLY` input directly.

When a local object is missing, `verify` checks the remote in real time rather than treating `file_locations.status=AVAILABLE` as permanent proof. A deleted or damaged remote object returns `MISSING` and updates the cache. An unreachable SSH host returns `REMOTE_UNVERIFIED` and exit code 1 while preserving the last persistent state, so a network failure is not misclassified as data loss.

Remote files can also be archived directly from URLs:

```bash
operon ingest --source sftp://hyli360@hpc.example.org:22/data/ASM.fna.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta

operon ingest --source remote://mycluster/raw/assemblies/ASM_000001/ASM_000001.genome_fasta.fasta.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta
```

This page covers content-verified remote mirroring. For whole-project backup and migration of `operon.sqlite`, `config/`, and related directories, see [Backup, Migration, and Resumption](backup-migration.md).
