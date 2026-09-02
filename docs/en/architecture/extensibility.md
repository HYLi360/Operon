# Extension boundaries

## Current boundaries

The built-in source adapter currently covers NCBI Datasets; sources such as ENA remain part of the future extension boundary. Taxonomy coverage currently supports only NCBI Taxonomy; GTDB and the NCBI↔GTDB crosswalk are not yet implemented. Built-in QC covers file level, reads basics, assembly structure, and annotation structure. BUSCO is natively integrated through directory output and a JSON summary parser; tools without a parser yet — QUAST, Merqury, Kraken2, CheckM2, and similar — can still be integrated through `run-external` + `import-qc`. Downstream comparative-genomics analysis is done by external workflows in `analysis/`; `operon` is responsible for data admission, provenance, and publication.

Deduplication is implemented by layer: byte-level duplication is already guaranteed by SHA-256 idempotence (same entity and role with identical bytes returns the same `FIL_`; different bytes are explicitly rejected); sequence-level duplication (normalized sequence digests / refget-style summaries) and biological near-duplication (Mash/ANI/k-mer similarity, duplicate clusters, and representative selection) are extension directions — they can be performed with external tools in `analysis/` with results written back to `qc_results`, and the representative-selection rules themselves should also be versioned.

For scale, SQLite WAL plus indexes suits metadata on the order of millions of rows, and all sequence parsing is streaming; if inode sharing is explicitly acceptable, hard links can be used for `standardized/` or releases.

Execution backends extend along the abstraction in `execution.py`: `local`, `slurm`, and `ssh` are provided today, and a new backend only needs to implement the same executor interface to plug into `run-external`/`analyze`. Cloud-vendor SDKs (AWS Batch, GCP Batch, etc.) and Slurm array jobs are not yet supported; remote storage currently consists of SFTP mirrors only, and object storage (S3 and the like) is likewise an extension direction.
