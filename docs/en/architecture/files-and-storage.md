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

## BGZF archive compression

New projects default to storing plain sequence and annotation archives (FASTA, FASTQ, GFF3, GTF) as deterministic [BGZF](https://samtools.github.io/hts-specs/): on `ingest` (and the ingest stage of `run-pipeline`) an uncompressed sequence or annotation file is first rewritten as BGZF next to the archive target, and the manifest row records the **compressed** bytes (`compression: bgzip`, a `.gz` canonical name, and the compressed SHA-256/size — identity binds the stored bytes, per INV-3). BGZF block headers carry no mtime and blocks deflate independently at a fixed size, so identical input bytes produce identical output bytes. BGZF is a valid multi-member gzip stream, so every built-in QC parser and any gzip-transparent tool reads the archive unchanged, while the block structure additionally supports random access (`pysam.BGZFile` exposes htslib's BGZF seek); the identity-keyed `index/fasta_lengths/` length cache is rebuilt against the new checksum on first use.

The switch is `storage.compress_archive` in `project.yaml`, resolved per ingest as: the CLI flag `--compress-archive`/`--no-compress-archive` > the project key > absent key. Projects initialized before the key existed carry no such entry and keep plain storage — no migration happens implicitly. Sources already gzip/BGZF-compressed are stored as-is, an explicit `--compression` argument pins the storage form, and directories and other binary formats are never transformed.

Compression that is intrinsic to a format is not a storage transform. BAM's container is BGZF by specification and CRAM compresses its own blocks, so a plain-named `.bam`/`.cram` records `compression: none` and keeps a canonical name without `.gz`; only an explicit gzip filename suffix or `--compression` records an outer layer. Rows archived before this rule keep their recorded path and compression. The `alignment_bam` and `alignment_cram` roles select the two formats when a filename has no usable extension; projects initialized before these roles existed gain them (and a trailing `schema_version` bump) in `config/schemas.yaml` on the first BAM/CRAM ingest, mirroring the GTF format extension.

`operon recompress` is the explicit one-shot migration for existing projects: it rewrites each eligible plain FASTA/FASTQ/GFF3/GTF archive in place (full re-hash of the current bytes, BGZF rewrite, decompressed re-hash as the content-preservation proof, then a per-field audited manifest update), deletes the file's recorded remote locations (the next `push` supersedes the stale remote copies), and rebuilds the `standardized/` view when the file was standardized. Release members are refused — releases are immutable — and remote-only or retired-entity files are skipped with a stated reason. Derived data keyed by the old SHA-256 (`qc_results`, `analysis_jobs`, the `fasta_lengths` cache) is naturally invalidated and lazily rebuilt; the `sequences` table, keyed by `file_id`, is unaffected. See [File and QC Commands](../reference/cli-files-qc.md#recompress).

## Derived index caches

`index/` is the project's shared home for **rebuildable derived index caches**
(EXT-8). Entries are keyed by the manifest identity, never registered in the
manifest, and therefore never enter a release snapshot or a backup — a
restored project rebuilds whatever it needs lazily. The layout convention is
`index/<kind>/…`:

- `index/fasta_lengths/{file_id}.{sha256}.v1.tsv` — the assembly
  `seqid -> length` map used by built-in QC (relocated from the pre-`index/`
  layout `qc/cache/fasta_lengths/`; legacy entries are adopted on first use).
- `index/features/{file_id}.{sha256}.v2/` — the flattened GFF3/GTF feature
  cache (`data.parquet` + `meta.json` identity marker) behind
  `operon features`; built lazily on first query and rebuilt transparently
  when it is stale, corrupt or missing its commit marker. Row groups follow
  source order (no global sort), so region filters prune on row-group
  statistics; queries run through polars `scan_parquet` with projection and
  predicate pushdown.

Only `operon features` writes into the feature namespace; deletes thereof are
always followed by a rebuild, never a dangling cache.

## Remote mirrors (SFTP)

The `remotes:` section of `project.yaml` can configure one or more SFTP remote mirrors (`operon/remotes.py`), synchronizing manifest files to a remote without breaking the invariants of this section:

- Plain files and directory artifacts are all verified by `sha256 + size_bytes`; when the server lacks `sha256sum`, SHA-256 is computed by streaming over SFTP, never degrading to size-only comparison; directories use exactly the same deterministic tree hash as locally (including empty directories and symlink targets);
- The remote maintains an `operon-manifest.json` v2 manifest (project_id + relative_path → file_id/sha256/size/kind/synced_at) whose updates are published atomically and serialized between concurrent control ends;
- All relative paths are root-constrained on both sides; absolute paths, `..`, and path escapes are rejected. The remote manifest's `project_id` and every entry identity must match the local SQLite;
- Every transfer reuses `workflow_runs` for provenance (step `push:<name>` / `pull:<name>`); successful locations are also cached in `file_locations`;
- After `pull` restores a locally missing file, `files.status` returns to `CHECKSUM_VERIFIED`, and this change is written to `changes` just like the status changes from `verify`/`evict`.

Manifest publication and locking, batch semantics and per-item exit codes, and `sftp://`/`remote://` ingest are command-level details; see [Remote storage commands](../reference/cli-remote.md) and [File and QC Commands](../reference/cli-files-qc.md#ingest).

Paramiko is a core runtime dependency but remains lazily imported, so local-only commands do not initialize the SSH stack.

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
