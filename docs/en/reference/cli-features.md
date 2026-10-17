# Feature index commands

`operon features` queries the flattened GFF3/GTF feature cache under
`index/features/` (EXT-8; see [Derived index caches](../architecture/files-and-storage.md#derived-index-caches)). One GFF3/GTF annotation archive yields one
`{file_id}.{sha256}.v1` directory holding `data.parquet` plus a `meta.json`
identity marker. The index is unregistered rebuildable derived data: it is
never part of the manifest, a release snapshot or a backup, and a stale,
corrupt or partially-written cache is rebuilt transparently from the archived
bytes on the next use. Index builds stream in bounded batches and are strict —
a source line that is not exactly 9 valid columns aborts the build and names
the line number.

## features query

```bash
operon features query (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                      [--start N] [--end N] [--limit N] [--format {tsv,json}]
                      [--rebuild]
```

- Choose exactly one scope. `--file-id` answers for one manifest row; an
  explicitly selected remote-only or non-annotation file fails the command
  (exit 1 with a stderr note). `--entity-type`/`--entity-id` batch over the
  entity's annotation files, skipping unusable rows with stderr notes
  (mirroring `operon qc`).
- Repeatable column filters: `--type`, `--seqid`, `--source`, `--strand`,
  `--gene-id`, `--transcript-id`, `--parent-id`.
- Region filters use tabix overlap semantics: a feature matches when its
  inclusive `[start, end]` span overlaps `[--start, --end]`; each bound may
  also be given alone.
- `--limit N` caps printed rows (default 1000; `0` = unlimited). `--format
  tsv` (default) prints the compact columns `seqid source type start end
  strand phase id parent gene_id transcript_id line_number`; `--format json`
  prints the full row list — including `score` and the verbatim
  `attributes` — grouped per file with index statistics.
- `--rebuild` drops the cached index and rebuilds it from the archive first.
- Flattened columns: the nine GFF feature columns, normalized `id`/`parent`
  (GFF3 `ID`/`Parent`, percent-decoded) and `gene_id`/`transcript_id`
  (literal attribute keys of either dialect), the raw `attributes` text, and
  the source `line_number` for trace-back.

## features stats

```bash
operon features stats (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                      [--format {tsv,json}] [--rebuild]
```

- Prints the manifest identity plus per-`type`, per-`source` and
  per-`seqid` tables (count, min start, max end).
- `--format json` returns the same three tables plus identity in one
  document. Selection and skip semantics match `features query`.
