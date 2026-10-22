# Feature index commands

`operon features` queries the flattened GFF3/GTF feature cache under
`index/features/` (EXT-8; see [Derived index caches](../architecture/files-and-storage.md#derived-index-caches)). One GFF3/GTF annotation archive yields one
`{file_id}.{sha256}.v2` directory holding `data.parquet` plus a `meta.json`
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
- `--attribute KEY=VALUE` matches the verbatim attribute block (percent-encoded
  GFF3 or quoted GTF); `--attribute KEY~=PATTERN` matches the value with a
  regular expression. Repeated `--attribute` tokens form one OR group
  (a feature matching any token is kept), and every repeated column filter
  above stays AND-composed with that group. For `ID`/`Parent` (GFF3) and
  `gene_id`/`transcript_id` (both dialects) the match short-circuits onto the
  flattened decoded columns, so a regex anchor applies to the decoded value.
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
  and `gene_id`/`transcript_id`, the raw `attributes` text, and the source
  `line_number` for trace-back. At v2 the `id`/`parent` pair is
  dialect-unified: GFF3 reads `ID`/`Parent` (percent-decoded); GTF
  materializes the structural identity its attributes imply — the gene row
  aliases `gene_id` as its id, a transcript row aliases `transcript_id` with
  `parent = gene_id`, and CDS/exon rows carry no own id and point at their
  transcript. Any earlier v1 cache is rebuilt transparently.


## features bed

```bash
operon features bed (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                    [--type T] [--seqid S] [--source S] [--strand STRAND]
                    [--start N] [--end N] [--attribute KEY=VALUE|KEY~=PATTERN]
                    [--longest-transcript] [--measure {CDS,mRNA}]
                    [--name-attr ATTR] [--limit N] [--output PATH] [--rebuild]
```

- Writes one BED6 row (`chrom`, `chromStart`, `chromEnd`, `name`, `score`,
  `strand`) per selected feature; `chromStart` is `start - 1` (BED's
  zero-based half-open form). `score .` becomes `0`; a missing strand becomes
  `.`. Directory filters combine as in `features query` (`--limit` defaults
  to `0` = unlimited — a BED is meant to be complete).
- `--longest-transcript` instead writes one row per gene: the gene supplies
  `chrom`, `chromStart`, `chromEnd`, `score` and `strand`, the name is its
  longest transcript, and genes with no transcript are not written. The
  ranking measure is the summed CDS length (`--measure CDS`, default) or the
  transcript span (`--measure mRNA`); net ties break on the smaller
  transcript id. `--name-attr protein_id` names the winning transcript with
  an attribute value when present, falling back to its id.
- BED is written as a coordinate tool format: no spreadsheet formula
  escaping is applied (unlike `features query --format tsv`, whose ODR-83
  contract stays untouched).
- `--output PATH` writes a plain file on disk (not an archive, not manifest
  data); without it the BED goes to stdout. Selection and skip semantics
  match `features query`; no rows for an explicit `--file-id` exit 1.

## features parents / features children

```bash
operon features parents  (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                         --id X [--id Y …] [--until gene] [--max-depth 100]
                         [--format {tsv,json}] [--rebuild]
operon features children (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                         --id X [--id Y …] [--recursive] [--type T]
                         [--format {tsv,json}] [--rebuild]
```

- The two verbs answer the reverse question of "which id is this?"
  `parents` walks up the hierarchy until `--until TYPE` (default `gene`) or a
  parent-less chain root; `children` walks down (direct children, or every
  level with `--recursive`; `--type T` restricts the reachable rows).
- The dialect defines the hierarchy edges. GFF3 uses the `parent` chain;
  GTF has no `Parent` edges, so a transcript's gene resolves through the
  `gene_id` attribute materialized in `gene_id`/`transcript_id` columns —
  gene children are the transcript rows carrying that `gene_id`, and
  `--recursive` adds every row referencing those transcripts.
- Output columns and rendering match `features query` exactly (same TSV
  columns, same JSON document shape). Stroke an unknown `--id` and the rows
  prove empty; an explicit `--file-id` then exits 1.

## features bins

```bash
operon features bins (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                     [--bin-size N] [--type T] [--format {tsv,json}]
                     [--rebuild]
```

- Counts features per `(seqid, bin)` — the first bin covers 1-based
  coordinates `1..bin_size`, and a feature lands in the bin containing its
  1-based `start`. Default `--type gene` (gene density for chromosome
  plotting; repeatable like `features query`).
- TSV columns: `seqid`, `bin_start`, `bin_end`, `count`,
  `feature_start_min`, `feature_end_max` (the coordinates the bin's features
  actually occupy) and `features_per_bp` (count divided by bin size).
- Selection and skip semantics match `features query`.

## features stats

```bash
operon features stats (--file-id FIL_… | --entity-type TYPE --entity-id ID)
                      [--format {tsv,json}] [--rebuild]
```

- Prints the manifest identity plus per-`type`, per-`source` and
  per-`seqid` tables (count, min start, max end).
- `--format json` returns the same three tables plus identity in one
  document. Selection and skip semantics match `features query`.
