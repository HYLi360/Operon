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

`ingest` and `operon verify` perform full SHA-256 verification. If size, device, inode, mtime, and ctime are unchanged, later `operon qc` runs reuse that verification result. Use `operon qc --rehash` for a full-byte audit. `operon verify` always performs full content verification.

Annotation GFF3 also verifies and reads associated assembly/protein files. When an assembly FASTA first participates in coordinate checks, Operon streams it and builds an index under `qc/cache/fasta_lengths/`. The index is keyed by assembly `file_id + sha256 + size_bytes`, can be deleted safely, and is rebuilt if damaged. `--rehash` revalidates all actual inputs; when the assembly SHA-256 is unchanged, the verified length index can still be reused.

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

Required columns:

```text
entity_type, entity_id, qc_stage, metric_name, metric_value, tool, tool_version, parameter_set
```

Optional columns:

- `file_id`: must exist in the manifest and belong to the entity in the row.
- `file_sha256`: when present, must match the manifest checksum for the file.

Import the file:

```bash
operon import-qc --file external_qc.tsv
```
