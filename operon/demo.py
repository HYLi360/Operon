"""Deterministic synthetic demo project.

The demo creates a small project with three assemblies, two annotations and one
paired-end sequencing run, archives them through the normal ingest path, runs
the built-in QC stages and the versioned rule engine, and creates a release.
It is intentionally small and fast; the same code path handles real files.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.files import ingest_file
from operon.qc_module import qc_all
from operon.release import create_release
from operon.reports import export_qc_tsv
from operon.rules import evaluate_all, evaluate_entity

AA_ALPHABET = "ACDEFGHIKLMNPQRSTVWY"


def _random_dna(rng: random.Random, length: int) -> str:
    return "".join(rng.choices("ACGT", k=length))


def _random_protein(rng: random.Random, length: int = 200) -> str:
    return "M" + "".join(rng.choices(AA_ALPHABET, k=length - 1))


def write_fasta(path: Path, sequences: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for seqid, sequence in sequences:
            handle.write(f">{seqid}\n")
            for i in range(0, len(sequence), 60):
                handle.write(sequence[i:i + 60] + "\n")


def write_gff(path: Path, lines: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("##gff-version 3\n")
        for fields in lines:
            handle.write("\t".join(str(v) for v in fields) + "\n")


def write_fastq(path: Path, records: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for header, sequence in records:
            handle.write(f"@{header}\n{sequence}\n+\n{'I' * len(sequence)}\n")


def _metadata_rows() -> dict[str, list[dict[str, Any]]]:
    return {
        "organisms": [
            {"organism_id": "ORG_000001", "scientific_name": "Syntheticus alpha", "taxon_id": 100001, "taxonomic_rank": "species", "taxonomy_source": "NCBI", "taxonomy_version": "demo.1"},
            {"organism_id": "ORG_000002", "scientific_name": "Syntheticus beta", "taxon_id": 100002, "taxonomic_rank": "species", "taxonomy_source": "NCBI", "taxonomy_version": "demo.1"},
        ],
        "samples": [
            {"sample_id": "SMP_000001", "organism_id": "ORG_000001", "biosample_accession": "SAMN0000001", "strain": "alpha-A", "tissue": "leaf", "tissue_normalized": "young leaf", "tissue_ontology_id": "PO:0009025", "collection_date": "2026-01-10", "country": "China", "country_iso": "CN", "latitude": 28.2, "longitude": 112.9},
            {"sample_id": "SMP_000002", "organism_id": "ORG_000002", "biosample_accession": "SAMN0000002", "strain": "beta-B", "collection_date": "2026-02-01", "country": "Germany", "country_iso": "DE", "latitude": 52.5, "longitude": 13.4},
            {"sample_id": "SMP_000003", "organism_id": "ORG_000001", "biosample_accession": "SAMN0000003", "strain": "alpha-C", "collection_date": "2026-03-01", "country": "United States", "country_iso": "US", "latitude": 40.0, "longitude": -100.0},
        ],
        "runs": [
            {"run_id": "RUN_000001", "sample_id": "SMP_000001", "run_accession": "SRR0000001", "experiment_accession": "SRX0000001", "library_strategy": "WGS", "library_source": "GENOMIC", "library_layout": "PAIRED", "platform": "ILLUMINA", "instrument_model": "NovaSeq", "read_length": 150},
        ],
        "assemblies": [
            {"assembly_id": "ASM_000001", "sample_id": "SMP_000001", "assembly_accession": "GCA_000000001", "assembly_version": 1, "assembly_level": "scaffold", "assembly_method": "demo assembler", "reference_status": "representative"},
            {"assembly_id": "ASM_000002", "sample_id": "SMP_000002", "assembly_accession": "GCA_000000002", "assembly_version": 1, "assembly_level": "contig", "assembly_method": "demo assembler", "reference_status": "other"},
            {"assembly_id": "ASM_000003", "sample_id": "SMP_000003", "assembly_accession": "GCA_000000003", "assembly_version": 1, "assembly_level": "scaffold", "assembly_method": "demo assembler", "reference_status": "other"},
        ],
        "annotations": [
            {"annotation_id": "ANN_000001", "assembly_id": "ASM_000001", "annotation_source": "demo annotator", "annotation_version": 1},
            {"annotation_id": "ANN_000003", "assembly_id": "ASM_000003", "annotation_source": "demo annotator", "annotation_version": 1},
        ],
        "accessions": [
            {"internal_type": "sample", "internal_id": "SMP_000001", "namespace": "NCBI_BioSample", "accession": "SAMN0000001", "version": "1"},
            {"internal_type": "assembly", "internal_id": "ASM_000001", "namespace": "NCBI_Assembly", "accession": "GCA_000000001", "version": "1"},
        ],
    }


def _gff_for_assembly(seqids: list[str], broken: bool = False) -> list[list[str]]:
    lines: list[list[str]] = []
    if not broken:
        gene_no = 0
        for seqid in seqids[:3]:
            for offset in (100, 1200):
                gene_no += 1
                gene_id = f"gene{gene_no}"
                mrna_id = f"mrna{gene_no}"
                cds_id = f"cds{gene_no}"
                start = offset + 1
                end = offset + 600
                lines.append([seqid, "demo", "gene", start, end, ".", "+", ".", f"ID={gene_id};Name={gene_id}"])
                lines.append([seqid, "demo", "mRNA", start, end, ".", "+", ".", f"ID={mrna_id};Parent={gene_id}"])
                lines.append([seqid, "demo", "CDS", start, end, ".", "+", "0", f"ID={cds_id};Parent={mrna_id}"])
    else:
        lines.append([seqids[0], "demo", "gene", 101, 801, ".", "+", ".", "ID=gene_bad1;Name=gene_bad1"])
        lines.append([seqids[0], "demo", "mRNA", 101, 801, ".", "+", ".", "ID=mrna_bad1;Parent=gene_bad1"])
        # CDS length 601 is deliberately not a multiple of 3.
        lines.append([seqids[0], "demo", "CDS", 101, 701, ".", "+", "0", "ID=cds_bad1;Parent=mrna_bad1"])
        # A transcript whose Parent never appears in the file -> broken Parent.
        lines.append([seqids[0], "demo", "mRNA", 900, 1100, ".", "-", ".", "ID=mrna_orphan;Parent=ghost_gene"])
    return lines


def init_demo(path: str | Path, project_id: str = "PRJ_DEMO_001") -> Project:
    path = Path(path)
    project = Project.init(path, project_id=project_id, name="Operon synthetic demo")
    db = Database(project.db_path)
    try:
        for table, rows in _metadata_rows().items():
            for row in rows:
                db.insert_row(table, row)

        rng = random.Random(20260816)
        asm1 = [(f"ctg{i}", _random_dna(rng, length)) for i, length in enumerate([6000, 5000, 4000, 3000, 2000], start=1)]
        asm2 = [(f"tig{i:02d}", _random_dna(rng, 200)) for i in range(1, 11)]
        asm3 = [(f"scaf{i}", _random_dna(rng, length)) for i, length in enumerate([5000, 4000, 3000, 2000, 1000], start=1)]

        source_dir = project.root / "examples" / "synthetic_source"
        fa1 = source_dir / "ASM_000001.genome.fasta"
        fa2 = source_dir / "ASM_000002.genome.fasta"
        fa3 = source_dir / "ASM_000003.genome.fasta"
        write_fasta(fa1, asm1)
        write_fasta(fa2, asm2)
        write_fasta(fa3, asm3)

        gff1 = _gff_for_assembly([s[0] for s in asm1], broken=False)
        gff3 = _gff_for_assembly([s[0] for s in asm3], broken=True)
        write_gff(source_dir / "ANN_000001.annotation.gff3", gff1)
        write_gff(source_dir / "ANN_000003.annotation.gff3", gff3)

        protein1 = [(f"protein{i}", _random_protein(rng, 200) + "*") for i in range(1, 7)]
        protein3 = [(f"protein_bad1", _random_protein(rng, 200) + "*")]
        write_fasta(source_dir / "ANN_000001.protein.faa", protein1)
        write_fasta(source_dir / "ANN_000003.protein.faa", protein3)

        ctg1_seq = asm1[0][1]
        reads = []
        for i in range(400):
            pos = rng.randrange(0, len(ctg1_seq) - 100)
            reads.append((f"demo_read_{i:05d}", ctg1_seq[pos:pos + 100]))
        write_fastq(source_dir / "RUN_000001.R1.fastq", [(h + "/1", s) for h, s in reads])
        write_fastq(source_dir / "RUN_000001.R2.fastq", [(h + "/2", _random_dna(rng, 100)) for h, _ in reads])

        # Register files through the normal ingest path. Proteins are registered
        # before their GFF3 so annotation QC sees complete annotation releases.
        ingest_file(db, project, fa1, "assembly", "ASM_000001", "genome_fasta")
        ingest_file(db, project, fa2, "assembly", "ASM_000002", "genome_fasta")
        ingest_file(db, project, fa3, "assembly", "ASM_000003", "genome_fasta")
        ingest_file(db, project, source_dir / "ANN_000001.protein.faa", "annotation", "ANN_000001", "protein_fasta")
        ingest_file(db, project, source_dir / "ANN_000001.annotation.gff3", "annotation", "ANN_000001", "annotation_gff3")
        ingest_file(db, project, source_dir / "ANN_000003.protein.faa", "annotation", "ANN_000003", "protein_fasta")
        ingest_file(db, project, source_dir / "ANN_000003.annotation.gff3", "annotation", "ANN_000003", "annotation_gff3")
        ingest_file(db, project, source_dir / "RUN_000001.R1.fastq", "run", "RUN_000001", "reads_r1")
        ingest_file(db, project, source_dir / "RUN_000001.R2.fastq", "run", "RUN_000001", "reads_r2")

        from operon.files import standardize_all
        standardize_all(db, project)

        qc_all(db, project)

        evaluate_all(db, project, "assembly_production_v1", entity_type="assembly")
        evaluate_all(db, project, "annotation_release_v1", entity_type="annotation")
        evaluate_all(db, project, "reads_qc_v1", entity_type="run")

        export_qc_tsv(db, project)
        create_release(db, project, version="2026.08.demo", profile="assembly_production_v1")

        from operon.schema import write_tsv
        decisions = db.export_rows("decisions", ["decision_id", "entity_type", "entity_id", "profile", "profile_version", "profile_snapshot_id", "profile_sha256", "decision", "curated_decision", "reason_codes", "observed", "thresholds", "evaluated_at", "curated_by", "curated_reason", "curated_evidence", "curated_at"])
        write_tsv(project.reports_root / "decisions.tsv", list(decisions[0].keys()) if decisions else ["entity_type", "entity_id", "profile"], decisions)
    finally:
        db.close()
    return project
