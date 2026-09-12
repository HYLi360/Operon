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
    connect_timeout: 30            # seconds; also bounds the remote manifest lock wait
```

Paramiko is included in the standard `OperonDBS` installation.

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

Remote mirrors preserve the raw-file invariants: file and directory artifacts are verified by SHA-256 plus size and are idempotent, different bytes at the remote path raise `ConflictError`, and directory hashes cover relative paths, empty directories, file contents, and symlink targets. Atomic manifest replacement needs the SFTP server's OpenSSH `posix-rename@openssh.com` extension and fails closed without it; if a crash leaves `.operon-manifest.lock` behind, remove it manually only after confirming that no push is active. A file that was already `STANDARDIZED` before eviction keeps that status after `pull` restores it. Batch publication, manifest v2 identity, and per-item exit codes are specified in [Remote storage commands](../reference/cli-remote.md).

## Keep the control plane local and large files remote

The complete archive → push → evict → analyze → pull workflow lives in [Remote-First Operation](remote-first.md). To let `analyze` consume locally absent (`REMOTE_ONLY`) inputs in place, point execution at the same remote mirror:

```yaml
execution:
  backend: ssh
  ssh:
    storage_remote: mycluster   # inherits the remote's host and root
    scheduler: slurm            # or none for direct execution on the SSH host
```

Eviction writes a small placeholder pointer file under `.operon/placeholders/<file_id>.json` (deleted again when `pull` restores the bytes). The first remote-only status also extends `config/schemas.yaml` with the `REMOTE_ONLY` file status. `schema_version` is raised to 1.2 only when that file still carries the legacy value `""`, `1.0`, or `1.1`; a project on a current schema keeps its version and only gains the new enum value. Whenever the file is changed it is rewritten with normalized formatting, so hand-written comments in it are dropped.

`standardize` and `release` need local bytes, so `pull` first; the command-level semantics of `evict` and of `locations` as a cached view versus a live `verify` are in [Remote storage commands](../reference/cli-remote.md).

Remote files can also be archived directly from `sftp://` and `remote://` URLs; see [ingest](../reference/cli-files-qc.md#ingest).

This page covers content-verified remote mirroring. For the full workflow that keeps data and compute on an HPC while the local project only records events, see [Remote-First Operation](remote-first.md). For whole-project backup and migration of `operon.sqlite`, `config/`, and related directories, see [Backup, Migration, and Resumption](backup-migration.md).
