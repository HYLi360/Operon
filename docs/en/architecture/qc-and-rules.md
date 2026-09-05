# QC, the rule engine, and the state machine

## QC pipeline

Built-in QC loads the Cython streaming parsers by default and requires them; the pure-Python version serves only as a per-metric, per-error-text behavioral reference. FASTA, FASTQ, GFF3, and protein FASTA uniformly recognize LF, CRLF, and lone-CR line endings; sequence and quality fields must be ASCII, and header/GFF3 text is validated as UTF-8. FASTQ structure must consist of complete four-line records — truncation, empty headers, missing `+` lines, or illegal quality characters all produce `parseable=0`. `parseable` reflects only the real execution result of a format parser; formats without a built-in parser (`other`, directories, etc.) do not record this metric and are treated as not evaluated.

| Stage | Applicable input | Representative metrics |
|---|---|---|
| `file_integrity` | All files | `file_exists`, `size_bytes`, `sha256_match`, `parseable` |
| `assembly_basic` | genome FASTA | `total_length`, `contig_n50/n90`, `contig_l50/l90`, `gc_percent`, `n_percent` (strictly N only), `gap_count`/`gap_percent` (runs and fraction of alignment gap characters `-`), `ambiguous_base_percent`, duplicate seqids/complete headers, circular/empty sequences |
| `sequence_basic` | other FASTA (e.g. standalone CDS or protein FASTA) | `sequence_count`, `total_length`, `empty_sequence_count`, `duplicate_sequence_id_count` |
| `reads_basic` | FASTQ | `read_count`, `total_bases`, `q20_percent`, `q30_percent`, `gc_percent`, `duplicate_percent`, sampling count/strategy, `overrepresented_sequence_count`, read length N50, R1/R2 pairing |
| `annotation_basic` | GFF3 (+ assembly FASTA/protein FASTA) | gene/mRNA/CDS counts, CDS triplet ratio, ID/Parent integrity, coordinate errors, seqid matching, protein duplicate IDs, X ratio, internal stop codons |

After archiving or an explicit `verify` completes a full SHA-256 computation successfully, the system binds the manifest SHA-256 and the local file's `size + device + inode + mtime_ns + ctime_ns` into a rebuildable cache. Later built-in QC reuses the verification result only while this fingerprint is completely unchanged; a change in size or any stat field automatically falls back to a full SHA-256. `operon qc --rehash` bypasses the cache unconditionally. File identity always remains `file_id + sha256 + size_bytes` — the fingerprint is neither a new identity nor a substitute for periodic explicit `verify`.

Annotation QC applies the same identity verification to the associated assembly/protein inputs read at runtime; `--rehash` covers both the primary input and these associated inputs. The assembly's `seqid -> length` mapping is written atomically to `qc/cache/fasta_lengths/` keyed by `file_id + sha256 + size_bytes + cache format`. This index is deletable, rebuildable derived data: when missing or corrupt it is rebuilt by streaming the FASTA, it can be reused across processes while the content identity is unchanged, and it does not enter the metadata source of truth.

External tool metrics can enter the same long table through `import-qc`, or be executed in a structured way with provenance through `run-external`.

FASTQ accumulates a histogram of quality characters up to 256 in a single parse, then computes Q20/Q30 under an explicit Phred offset, without re-reading or re-decompressing the file. The default offset is modern Phred+33; `auto` still computes with 33 when the observable character ranges overlap, but preserves the uncertainty through `quality_encoding=ambiguous_assumed_phred33`. The duplication rate uses exact counting over a deterministic first-N-reads sample, recording `duplicate_sampled_read_count`, `duplicate_is_sampled`, and `duplicate_sampling_strategy=first_n`; paired read counts are cached and reused within the same QC batch.

Each built-in QC file task also records total elapsed time and non-overlapping stage timings with a monotonic high-precision clock. The JSONL records are versioned through `qc_timing.schema_version` and store the identity of the current file plus the assembly/protein associated files read at annotation runtime; the same structure is serialized into `workflow_runs.execution_details`. This allows the costs of full SHA-256/fingerprint cache, FASTA/FASTQ, assembly length, GFF3 scan/finalize, protein scan, SQLite writes, and state transitions to be aggregated separately, without changing QC metric semantics. For the full field list and re-measurement method, see [Built-in QC performance diagnostics](../operations/qc-performance.md).

## Rule engine

Thresholds do not live in QC code, but in `config/profiles/*.yaml`:

```yaml
kind: qc
version: 1
applies_to: [assembly]
required:
  - metric: sha256_match
    operator: "=="
    value: 1
    code: SHA256_MISMATCH
  - metric: contig_n50
    operator: ">="
    value: 1000
    code: LOW_CONTIGUITY
warnings:
  - metric: n_percent
    operator: ">"
    value: 1
    code: HIGH_GAP_CONTENT
```

Decision outputs are data:

```text
PASS / PASS_WITH_WARNINGS / REVIEW / FAIL / EXCLUDED / NOT_EVALUATED
```

Each decision records `reason_codes`, `observed`, `thresholds`, and the profile version with its SHA-256 snapshot.

## State machine

```text
DISCOVERED -> METADATA_FETCHED -> METADATA_VALIDATED -> DOWNLOAD_PENDING
-> DOWNLOADED -> CHECKSUM_VERIFIED -> STANDARDIZED -> QC_RUNNING
-> QC_COMPLETE -> ACCEPTED / REVIEW / REJECTED -> RELEASED
```

Failure states also exist explicitly: `DOWNLOAD_FAILED`, `CHECKSUM_FAILED`, `FORMAT_INVALID`, `METADATA_INVALID`, `STANDARDIZATION_FAILED`, `QC_FAILED`.

`set_state` validates legal transitions; batch flows internally use forced but audited transitions, and a manual forced transition must record a reason and enter the `changes` table.
