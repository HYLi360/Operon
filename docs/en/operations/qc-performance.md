# Built-in QC performance diagnostics

This page defines the QC JSONL timing fields, the representative re-measurement set, and the method for identifying hotspots.

`operon qc` stores high-precision per-stage timings in every QC run record of `logs/workflow.jsonl`. These diagnostic fields only describe the measurement process; they do not change QC metrics, decision thresholds, or file identity.

## JSONL timing structure

A QC record keeps the original `started_at`, `finished_at`, `tool`, `tool_version`, `parameter_set`, and `command`, and adds:

- `duration_seconds`: total elapsed time measured with `time.perf_counter()`, in seconds;
- `file_id`, `file_role`, `file_format`, `input_size_bytes`, and `input_sha256`: the identity of the manifest file directly processed by this command;
- `parser_backend`: the parser backend actually used — currently `cython` by default;
- `stage_timings_seconds`: per-stage timings shaped for direct aggregation by streaming tools;
- `qc_timing`: the full structure with `schema_version`, clock type, primary input, related inputs, and per-stage timings.

`qc_timing.integrity.verification_method` distinguishes `cached_stat_fingerprint`, `full_sha256`, `size_mismatch`, `changed_during_sha256`, `stat_error`, `sha256_error`, and `missing`; `rehash_requested` records whether `--rehash` was used this round. Cold verifications and normal repeated QC runs should therefore not be mixed into the same performance statistics.

The same `qc_timing` is also serialized into `workflow_runs.execution_details`, so diagnosis remains possible with only the database and no JSONL files. Old JSONL records lack these optional fields, and readers must remain compatible with them.

The main stages:

| Stage | Meaning |
|---|---|
| `state_qc_running` | Writing the `QC_RUNNING` state and audit record |
| `file_integrity` | Checking the file; on a fingerprint hit only stat is read, otherwise a full SHA-256 is computed |
| `fasta_stats` | Structure and sequence statistics of the current FASTA |
| `fastq_stats` | Structure, quality, and sampled-duplication statistics of the current FASTQ |
| `paired_read_check` | Looking up the paired file; on a cache miss also includes paired FASTQ counting |
| `annotation_manifest_lookup` | Looking up the annotation, assembly, and associated file identities |
| `assembly_fasta_integrity` | Verifying the manifest content identity of the associated assembly FASTA |
| `assembly_fasta_length_cache_lookup` | Finding and loading the content-identity-keyed seqid-length index |
| `assembly_fasta_lengths` | Streaming scan of the assembly FASTA to build the length map on a cache miss |
| `assembly_fasta_length_cache_write` | Atomic write of the rebuildable length index |
| `assembly_fasta_length_map_prepare` | Preparing the seqid-length lookup map for the current parser backend; Cython converts str keys to bytes keys |
| `gff3_scan` | Line-by-line parsing of GFF3 attributes, coordinates, and ID/Parent references |
| `gff3_finalize` | Aggregating missing Parents and final GFF3 metrics |
| `protein_manifest_lookup` | Looking up the associated protein FASTA |
| `protein_fasta_integrity` | Verifying the manifest content identity of the associated protein FASTA |
| `protein_stats` | Scanning the associated protein FASTA |
| `qc_results_write` | Batch write into `qc_results` |
| `state_qc_complete` / `state_qc_failed` | Writing the final state and audit record |
| `unattributed` | Small segments not individually wrapped, such as metric-dict construction; does not overlap the stages above |

Timing values are kept at microsecond resolution to reduce whole-second quantization error on short tasks; this does not mean OS scheduling and filesystem noise are microsecond-stable. Performance conclusions should be based on multiple paired runs in the same environment and on per-stage medians.

## The representative re-measurement set of 532 annotations

The machine-readable list lives at `benchmarks/qc_representative_entities.tsv` in the code repository. It was selected in strata based on measurements of the old implementation from 2026-08-18 and the Cython implementation from 2026-08-29:

- `largest_*_regression` / `*_net_regression`: objects whose total time increased in the new version;
- `largest_input*`: the largest inputs and longest tasks, amplifying stable hotspots;
- `annotation_speedup_control`: counterexamples where the annotation stage became noticeably faster, avoiding analysis of regressed samples only;
- `*_baseline_q*`: size baselines selected by the combined size of the three archived annotation files;
- `large_near_neutral_control`: large but overall near-unchanged control objects.

`archived_annotation_bytes` is the sum of an annotation entity's own three archived files and does not include the associated assembly FASTA read at GFF3 runtime. New JSONL records that assembly's `file_id + sha256 + size_bytes` in `qc_timing.related_inputs`; include it when analyzing total read volume later.

The core set contains 10 entities with a historical new-version total of about 256 seconds; the full set contains 18 entities with a historical new-version total of about 315 seconds. The core set suits fast iteration; the full set confirms whether hotspots are stable across sizes and control samples.

Run the core set from the project root:

```bash
awk -F '\t' 'NR > 1 && $1 == "core" {print $3}' \
  benchmarks/qc_representative_entities.tsv |
while IFS= read -r entity_id; do
  operon qc --entity-type annotation --entity-id "$entity_id"
done
```

Run the core plus extended set:

```bash
awk -F '\t' 'NR > 1 {print $3}' benchmarks/qc_representative_entities.tsv |
while IFS= read -r entity_id; do
  operon qc --entity-type annotation --entity-id "$entity_id"
done
```

For a formal comparison, repeat at least three rounds and alternate the versions being compared, so that page cache, background I/O, or machine load is not mistaken for a code change. Keep the runs of each entity's three files complete, because annotation QC on the GFF3 reads the associated assembly/protein, while the other two FASTA tasks serve as internal controls on an already-confirmed acceleration path.

In the three full rounds over 18 entities on 2026-08-29, SSD and HDD took about 238.1 and 295.3 seconds respectively; the paired-median total difference was about 56.4 seconds. The difference came almost entirely from `file_integrity` (SSD about 12.0 s, HDD about 68.8 s), while `gff3_scan` took about 155.8 s and 153.8 s respectively — showing that the first full SHA-256 on HDD was the main media-dependent loss, and GFF3 parsing was a shared CPU hotspot on both media. That batch of downloads had no `assembly_fasta` — only GFF3, CDS, and protein — so `assembly_fasta_lengths` was not exercised; after assembly FASTAs were later added, a new baseline should be established separately on HDD rather than comparing total time directly against the old SSD data.

Accordingly, the current implementation adds two optimizations aimed directly at the hotspots: unchanged immutable raw files reuse a completed SHA-256's stat fingerprint; and the Cython GFF3 parser splits common ASCII lines directly as bytes and extracts only the QC-needed `ID`/`Parent`, falling back to full UTF-8/attribute parsing on non-ASCII or percent escapes. Both paths are covered by Python/Cython parity regressions, and the metric-dict and error-message contracts are unchanged.

After assembly FASTAs were added on 2026-08-31, the same 18 entities on HDD required reading about 66.58 GB of assembly per round, with `assembly_fasta_lengths` at 417.9, 403.7, and 403.4 seconds across three rounds — about 60% of total time. The length map is therefore now stored under `qc/cache/fasta_lengths/` keyed by the assembly's `file_id + sha256 + size_bytes + cache format`: the first run still does a full scan, while later processes record `related_inputs[].length_cache.status=hit` and only pay the index-loading cost. A corrupt cache is deleted and rebuilt automatically; a SHA-256 digest in the cache header also detects index rows whose format is legal but whose content has changed. The JSONL records this round's behavior explicitly as `built`, `hit`, or `write_failed`.

In 0.5.3's three-round re-measurement of the same 18 entities, 54 files, on HDD, the first round built all 18 caches (`built`) and the following two rounds hit all 36 times (`hit`). The average total time of the last two rounds of 0.5.2 was 670.13 seconds; 0.5.3 with warm caches averaged 269.05 seconds — a 59.85% reduction, about 2.49× overall; annotation GFF3 files dropped from 588.51 to 186.32 seconds combined, about 3.16×. An assembly scan of about 403.6 seconds per round was replaced by about 4.43 seconds of cache loading. The 18 indexes total about 114 MiB — small relative to 66.58 GB of raw assembly reads per round.

## Identifying optimization hotspots

When aggregating, compare per-entity, per-stage medians first, rather than only total wall-clock time:

1. High `file_integrity` with `verification_method=full_sha256`: full SHA-256 or storage throughput dominates; after a fingerprint hit this stage should be near-constant;
2. High `assembly_fasta_lengths`: the associated assembly length index was built for the first time this round; if repeated runs still show this stage instead of `assembly_fasta_length_cache_lookup`, check the cache path or cache corruption;
3. High `gff3_scan`: line splitting, UTF-8 decoding, field/attribute splitting, set and Counter operations dominate;
4. High `gff3_finalize`: ID/Parent set aggregation dominates;
5. High `protein_stats`: protein FASTA scanning or record concatenation dominates;
6. High `qc_results_write` / state stages: SQLite writes and fsync dominate;
7. Abnormally high `unattributed`: finer timing boundaries are needed.

A targeted optimization should only begin when the hotspot reproduces across regressed samples, size baselines, and at least one control sample; after the optimization, the Python/Cython parity tests must still pass, keeping metrics and error texts unchanged.
