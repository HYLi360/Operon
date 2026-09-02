# Remote storage commands

## remotes

```bash
operon remotes
```

- Lists the remotes defined in the `remotes:` section of `project.yaml` and tests connectivity to each one.
- Output columns: `name` / `type` / `address` / `root` / `files` (entries in the remote manifest) / `status` / `error`.
- Returns exit code 1 if any remote reports an `error`.
- Requires the optional dependency `operon[remote]` (paramiko); without it, configuration errors are only raised when SSH/SFTP features are actually used.
- Unknown SSH host keys are rejected by default; establish trust through `known_hosts` or `host_key_sha256`.

## push

```bash
operon push --remote NAME [--file-id FIL_...]...
```

- Uploads local manifest files to the named remote (an SFTP mirror); without `--file-id`, pushes all manifest files.
- Both file and directory artifacts are verified by sha256 + size; when the remote lacks `sha256sum`, streaming hashing over SFTP is used instead. Size-only comparison is never performed. Identical content is skipped; pre-existing content at the same path with different bytes raises `ConflictError` and is never silently overwritten.
- A batch push publishes `operon-manifest.json` exactly once. Writers are serialized during read-modify-write through the remote atomic directory `.operon-manifest.lock`, and the manifest itself is still replaced atomically via a unique temporary file plus POSIX rename. If a writer exits abnormally the lock remains, and the error reports the exact path that needs manual inspection.
- Every transfer is recorded in `workflow_runs` (step `push:<name>`). A failure on one file does not abort the rest; each item's result is printed, and if any item is `error` the command as a whole exits with code 1.
- Per-file outcomes: `uploaded` / `indexed` (bytes already present remotely, added to the manifest) / `skipped` / `error`.

## pull

```bash
operon pull --remote NAME [--file-id FIL_...]...
```

- Restores files from the named remote mirror; without `--file-id`, iterates the remote manifest, but each record must still match the local SQLite entry on `file_id + relative_path + sha256 + size_bytes`. Unknown objects that exist only on the remote are never imported into the local database.
- Also verified by sha256 + size and idempotent; when local bytes differ, overwriting is refused (`ConflictError`).
- After a locally missing file is restored, its `files.status` returns to `CHECKSUM_VERIFIED`; the transfer is recorded in `workflow_runs` (step `pull:<name>`) and the status change in `changes`.
- A failed item does not stop the rest of the batch; if any item is `error`, the command exits with code 1.

## evict

```bash
operon evict --remote NAME [--file-id FIL_...]...
```

- Explicitly deletes the local archived bytes; without `--file-id`, processes all manifest files.
- Before deletion it re-checks the local identity, the remote manifest identity, and the remote actual SHA-256 / directory-tree hash; any mismatch at any step refuses the deletion.
- On success `files.status` becomes `REMOTE_ONLY`, the location is recorded in `file_locations`, the status change in `changes`, and a small human-readable pointer is written to `.operon/placeholders/<file_id>.json`.
- A failed verification or deletion does not stop the rest of the batch; if any item is `error`, the command exits with code 1.
- `standardize` and `release` require a prior `pull`; with `execution.ssh.storage_remote` configured, `analyze --backend ssh` can use remote inputs directly.

## locations

```bash
operon locations [--file-id FIL_...]...
```

Joins `files` and `file_locations` to show local status, remote name, remote status, and last verification time. The command is read-only and does not contact the remote; for a live re-check, run `verify` (verification also happens as part of the pre-flight checks in `push`, `pull`, `evict`, and remote analysis).
