# File Archiving and Built-In QC

## Archive paired-end sequencing reads

1. Create the sample and run:

```bash
operon add run --field sample_id=SMP_000001 --field library_layout=PAIRED
```

2. Archive R1 and R2 separately:

```bash
operon ingest --source /data/SRR001_R1.fastq.gz \
  --entity-type run --entity-id RUN_000001 --role reads_r1

operon ingest --source /data/SRR001_R2.fastq.gz \
  --entity-type run --entity-id RUN_000001 --role reads_r2
```

3. Verify and run QC:

```bash
operon verify
operon qc --entity-type run --entity-id RUN_000001
```

Modern FASTQ defaults to Phred+33 for Q20/Q30. Use `--phred-offset 64` only for confirmed legacy Phred+64 data. If the encoding is uncertain, use `--phred-offset auto`; ambiguous input is recorded as `ambiguous_assumed_phred33`. R1 and R2 `read_count` values are stored under their own `input_identity`, and the paired check writes `paired_read_count_match`.

`ingest` and `operon verify` perform full SHA-256 verification, and later `operon qc` runs reuse that result while the file's stat fingerprint is unchanged; the assembly `seqid -> length` index is cached under `qc/cache/fasta_lengths/` for the same content identity, and annotation QC applies the same identity check to the assembly/protein files it reads. Use `operon qc --rehash` for a full-byte audit (`operon verify` always performs full content verification). Both caches are deletable, rebuildable derived data; see the [QC pipeline](../architecture/qc-and-rules.md#qc-pipeline) for the guarantee.

## Archive assemblies and annotations

```bash
# Assembly FASTA
operon ingest --source /data/ASM.fna.gz \
  --entity-type assembly --entity-id ASM_000001 --role genome_fasta

# Annotation files
operon ingest --source /data/ANN.gff3.gz \
  --entity-type annotation --entity-id ANN_000001 --role annotation_gff3
operon ingest --source /data/ANN.cds.faa.gz \
  --entity-type annotation --entity-id ANN_000001 --role cds_fasta
operon ingest --source /data/ANN.protein.faa.gz \
  --entity-type annotation --entity-id ANN_000001 --role protein_fasta

# Run QC after all files are archived.
operon qc
```

`ingest` updates:

- `assemblies.fasta_file_id`
- `annotations.gff_file_id`, `cds_file_id`, and `protein_file_id`

## Import external QC results

Convert BUSCO, QUAST, FastQC, fastp, or other outputs to TSV:

```text
entity_type	entity_id	file_id	qc_stage	metric_name	metric_value	metric_unit	tool	tool_version	parameter_set
assembly	ASM_000001	FIL_000001	busco	complete_percent	96.4	percent	busco	5.8.2	embryophyta_odb12
assembly	ASM_000001	FIL_000001	quast	contig_n50	2845913	bp	quast	5.2.0	default
```

The required and optional columns, and the manifest checks on `file_id` and `file_sha256`, are specified in [import-qc](../reference/cli-files-qc.md#import-qc); `metric_unit` defaults to empty and `evaluated_at` to the import time.

Import the file:

```bash
operon import-qc --file external_qc.tsv
```
