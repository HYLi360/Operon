# File archiving, standardization, and remote storage

## File archiving and standardization

Guarantees of `ingest`:

1. The entity must exist.
2. Format and compression are detected automatically; `.fna.gz`/`.fastq.gz` are recognized correctly.
3. Same entity and role with a different SHA-256: refused outright (`ConflictError`), preventing raw overwrite.
4. Re-ingesting identical content: idempotent, returning the same `FIL_`.
5. Writes to raw use "temporary file + fsync + atomic rename".
6. After archiving, the checksum is verified once more; only on success is the manifest registered and relationships such as `assemblies.fasta_file_id` and `annotations.*_file_id` backfilled.

`standardize` **copies** into `standardized/` by default, so the raw, standardized, and release layers never share writable inodes; `--link hardlink` or `--link symlink` is an explicit compatibility option.

## Remote mirrors (SFTP)

The `remotes:` section of `project.yaml` can configure one or more SFTP remote mirrors (`operon/remotes.py`), synchronizing manifest files to a remote without breaking the invariants of this section:

- Plain files and directory artifacts are all verified by `sha256 + size_bytes`; when the server lacks `sha256sum`, SHA-256 is computed by streaming over SFTP, never degrading to size-only comparison; directories use exactly the same deterministic tree hash as locally (including empty directories and symlink targets);
- The remote maintains an `operon-manifest.json` v2 manifest (project_id + relative_path → file_id/sha256/size/kind/synced_at). Manifest updates require the server to support the OpenSSH POSIX rename extension and are published via "unique temporary file + atomic replacement"; one push batch publishes the manifest exactly once, and read-modify-write is serialized through the remote atomic directory `.operon-manifest.lock`, so concurrent pushes from multiple control ends cannot lose entries;
- All relative paths are root-constrained on both sides; absolute paths, `..`, and path escapes are rejected. The remote manifest's `project_id` and every entry identity must match the local SQLite;
- Every transfer reuses `workflow_runs` for provenance (step `push:<name>` / `pull:<name>`); successful locations are also cached in `file_locations`;
- push/pull/evict use per-item result semantics: a failed item records `error` and the rest of the batch continues; the CLI returns non-zero if any error exists;
- After `pull` restores a locally missing file, `files.status` returns to `CHECKSUM_VERIFIED`, and this change is written to `changes` just like the status changes from `verify`/`evict`;
- `ingest --source` also directly accepts `sftp://[user@]host[:port]/path` and `remote://<name>/<path>`; the latter must exist in the remote manifest and is identity-checked first, while the former is downloaded and assigned a fresh identity by ingest, then follows exactly the same archiving flow as a local file.

paramiko is an optional dependency (`pip install 'operon[remote]'`), imported lazily in code; core dependencies and local functionality are unaffected. The cx_Freeze `build` extra and release packages include paramiko.

## Local control plane and remote data plane

The remote model of `operon` 0.3 splits "store, compute, execute" into three composable roles:

```text
local machine: CLI + project.yaml + tools.yaml + SQLite + logs
                          │ SSH/SFTP
                          ▼
remote login/scheduler node: execute directly or submit to Slurm
                          │ shared filesystem
                          ▼
remote data plane: raw / reference DBs / temporary analysis output
```

`push` creates an identity-verified copy on the remote; `evict` deletes local bytes only after re-verifying the remote's actual content, sets `files.status` to `REMOTE_ONLY`, records the location in `file_locations`, and writes a human-friendly pointer to `.operon/placeholders/<file_id>.json`. The pointer is not a source of truth — `files` and `file_locations` are the machine-decision basis. `pull` can hydrate an object back to its logical `relative_path` at any time.

`file_locations.status=AVAILABLE` is only a local residency cache, not proof of permanent availability. When local bytes are missing, `verify` must read the remote manifest live and check actual content; after loss/corruption is confirmed it sets the file to `MISSING`, while a temporarily unreachable remote only yields a failing `REMOTE_UNVERIFIED` check result without rashly rewriting persistent state. When availability is confirmed, it refreshes the remote location and keeps `REMOTE_ONLY`.

Here "raw is immutable" constrains that the content identity of a `file_id` cannot be replaced by a different set of bytes — it does not require every control end to keep a physical copy forever. `evict` is a verified location migration: at least one remote copy still exists with the same SHA-256/size, so the logical raw identity is unchanged; when the remote copy is untrusted or missing, local bytes are never deleted.

When `execution.ssh.storage_remote` points at the same remote filesystem, `analyze` does not download a locally missing input first; it verifies the remote manifest and actual SHA-256, then maps the local logical path to the remote root so the remote command reads the object directly. This currently requires the compute nodes to see that remote root through the SSH host or its Slurm nodes; "server-side movement between object storage and a completely different compute cluster" is not yet implemented.
