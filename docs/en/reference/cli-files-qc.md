# File and QC Commands

## ingest

```bash
operon ingest \
  --source FILE --entity-type TYPE --entity-id ID --role ROLE \
  [--format FMT] [--compression COMPRESSION] \
  [--source-url URL] [--move]
```

- Common `--role` values: `genome_fasta`, `annotation_gff3`, `cds_fasta`, `protein_fasta`, `reads_r1`, `reads_r2`, `reads_single`.
- `--source` accepts a local path, `sftp://[user@]host[:port]/path`, or `remote://<remote-name>/<path-relative-to-remote-root>`. `remote://` uses the `remotes:` section in `project.yaml`. The source is downloaded to a temporary file and then follows the normal archive workflow. If `--source-url` is omitted, the URL is recorded as `source_url`. Paramiko for SFTP sources is included in the standard installation.
- A `remote://` path must be a safe root-relative path and must already exist in that remote's `operon-manifest.json`; manifest SHA-256 and size are verified before and after download. A bare `sftp://` source has no mirror manifest, so `ingest` calculates and registers a new local identity after download.
- Compression such as `.gz` is detected automatically. A file with a gzip suffix but no gzip magic bytes is rejected.
- Different SHA-256 bytes for the same entity and role are rejected.
- If the target canonical path is occupied by different bytes, Operon does not overwrite it. If another manifest row claims the occupant and its bytes match, the occupant is first moved to that row's own canonical role path and its `relative_path` is updated. An unclaimed interrupted remnant is quarantined as `<filename>.orphan-<first-12-sha>` in the same directory. Both cases are audited in `changes` and preserve bytes. `ConflictError` is raised only when the occupying bytes do not match the claiming row's checksum either.
- `--move` removes the source only after the archived copy has been checksum-verified and the manifest/workflow transaction has committed. If any earlier step fails, the source remains available for retry.
- On success, the entity state becomes `CHECKSUM_VERIFIED` and relevant entity file-ID fields are updated.

## verify

```bash
operon verify [--file-id FIL_...]...
```

Checks every manifest path and SHA-256, or all files when `--file-id` is omitted. When local bytes are missing, Operon does not trust the cached `file_locations` state alone. It connects to every remote marked `AVAILABLE`, rechecks the remote manifest, and verifies actual SHA-256 or directory-tree identity. At least one live verified copy produces `REMOTE_ONLY`. A definitely missing or damaged remote object produces `MISSING` and updates `file_locations` and `files.status`. If SSH is temporarily unreachable and data loss cannot be determined, the result is `REMOTE_UNVERIFIED` and the existing `files.status` is retained. `MISSING`, `REMOTE_UNVERIFIED`, and local verification failures return a non-zero exit code.

Status changes caused by `verify` are written to the `changes` audit table. When an old metadata schema 1.0/1.1 project first confirms `REMOTE_ONLY`, custom fields are preserved and the schema is upgraded to 1.2.

## standardize

```bash
operon standardize [--file-id FIL_...]... [--link {copy|hardlink|symlink}]
```

- The default `copy` mode keeps raw and standardized files on separate inodes.
- `hardlink` and `symlink` are explicit compatibility and space-saving options.
- An existing target with the same checksum is skipped; a different checksum is rejected.
- New targets are built under a temporary sibling and published atomically. If copying, checksum verification, or the status transaction fails, the target and temporary link are removed so the operation can be retried without a partial artifact.

## qc

```bash
operon qc [--file-id FIL_...] [--entity-type TYPE] [--entity-id ID] \
          [--sample-size N] [--phred-offset {33,64,auto}] [--rehash]
```

- By default, all manifest files are processed.
- `--sample-size` sets the maximum number of leading FASTQ reads used for duplicate-rate and overrepresented-sequence statistics. It must be a positive integer; default: 1,000,000.
- `--phred-offset` controls FASTQ quality interpretation; default: `33`. Use `64` only for confirmed legacy data. `auto` uses modern Phred+33 when character ranges overlap and records `quality_encoding=ambiguous_assumed_phred33`.
- By default, QC reuses the most recent full SHA-256 verification from `ingest` or `verify` when the stat fingerprint is unchanged. Any fingerprint change triggers a new SHA-256 calculation. `--rehash` always bypasses the cache; use it for periodic audits, the first check after storage migration, or cold-verification benchmarks. For annotation GFF3, it also revalidates the associated assembly and protein inputs actually read at runtime.
- The assembly FASTA `seqid -> length` map is written to `qc/cache/fasta_lengths/` on first use. Later QC runs reuse it only for the same complete content identity. A missing, malformed, or identity-mismatched cache is rebuilt automatically. `--rehash` revalidates the source SHA-256, but the length index can still be reused when content identity is unchanged because the index is keyed by the verified SHA-256.
- Results are written to `qc_results` by `file_id + file_sha256 + input_identity`.
- Each file receives its own `QC_COMPLETE`, `QC_FAILED`, or `QC_PENDING` status. The entity state is the worst sibling status (`QC_FAILED` > `QC_RUNNING` > `QC_COMPLETE`), and the command lists every file status. A failed file makes the command exit non-zero.
- For each file, `logs/workflow.jsonl` records `duration_seconds`, the actual parser backend, primary/related input identity, and high-resolution `stage_timings_seconds`/`qc_timing`. The same details are written to `workflow_runs.execution_details`. See [Built-In QC Performance Diagnostics](../operations/qc-performance.md).

## import-qc

```bash
operon import-qc --file TSV
```

Required columns:

```text
entity_type, entity_id, qc_stage, metric_name, metric_value, tool, tool_version, parameter_set
```

Optional columns: `file_id`, `file_sha256`, `metric_unit`, and `evaluated_at`. `file_id` and `file_sha256` must match the manifest when provided.
