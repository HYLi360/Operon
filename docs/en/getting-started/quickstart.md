# Quick Start

## End-to-end demo

The demo project uses deterministic synthetic data. It verifies the installation and exercises the complete pipeline without requiring real genomic data.

```bash
operon init-demo ./demo-project --project-id PRJ_DEMO_001
```

The command performs the following steps:

1. Initializes the project directory and configuration.
2. Creates 2 organisms, 3 samples, 1 run, 3 assemblies, and 2 annotations.
3. Generates synthetic FASTA, GFF3, protein FASTA, and paired-end FASTQ files.
4. Archives 9 files through the normal ingest workflow.
5. Copies files to `standardized/` by default.
6. Runs built-in QC.
7. Evaluates the results with `assembly_production_v1`, `annotation_release_v1`, and `reads_qc_v1`.
8. Creates release `2026.08.demo`.

Inspect the results:

```bash
operon --project ./demo-project status
operon --project ./demo-project report decisions
operon --project ./demo-project report qc --entity-type assembly
```

Expected demo outcomes:

- `ASM_000002` fails with `LOW_CONTIGUITY`.
- `ANN_000003` fails with `CDS_NOT_MULTIPLE_OF_3` and `BROKEN_GFF3_PARENTS`.
- Other entities pass.

Verify the release checksums:

```bash
cd ./demo-project/releases/2026.08.demo
# Linux
sha256sum -c checksums.sha256
# macOS
shasum -a 256 -c checksums.sha256
```
