"""Coverage-focused error-path tests for the NCBI Datasets adapter.

The happy paths are covered by ``tests/unit/test_ncbi_edge_cases.py`` and
``tests/integration/test_ncbi_datasets_adapter.py``.  This module targets the
remaining malformed-input, partial-failure, resume/skip-planning, schema
upgrade and download-retry branches.  Tests go through the adapter's public
entry points (``run_ncbi_datasets_adapter``, ``download_ncbi_dataset`` or the
module-level helpers the adapter exposes) and assert observable behaviour:
raised ``OperonError`` types/messages, committed rows, written files and
returned summaries.  Only network, subprocess and filesystem-limit boundaries
are faked.
"""

from __future__ import annotations

import asyncio
import errno
import io
import json
import queue
import ssl
import struct
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any, Sequence

import pytest
import requests
import yaml

from operon.adapters import ncbi_datasets as ncbi
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.files import raw_bucket


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


# --------------------------------------------------------------------------
# Payload builders
# --------------------------------------------------------------------------

def _report(accession: str = "GCF_000001405.40") -> dict[str, Any]:
    return {
        "accession": accession,
        "currentAccession": accession,
        "organism": {
            "organismName": "Homo sapiens",
            "taxId": 9606,
            "infraspecificNames": {"strain": "GRCh38", "sex": "male"},
        },
        "assemblyInfo": {
            "assemblyLevel": "Chromosome",
            "assemblyMethod": "multiple methods",
            "biosample": {
                "accession": "SAMN00000001",
                "attributes": [
                    {"name": "collection_date", "value": "2020-02-03"},
                    {"name": "geo_loc_name", "value": "USA"},
                ],
            },
            "bioprojectAccession": "PRJNA31257",
            "pairedAssembly": {},
            "refseqCategory": "reference genome",
            "releaseDate": "2022-02-03T00:00:00Z",
            "submitter": "Genome Reference Consortium",
        },
        "annotationInfo": {
            "provider": "NCBI RefSeq",
            "version": 110,
            "releaseDate": "2023-10-01",
        },
    }


def _write_package(
        path: Path,
        accession: str = "GCF_000001405.40",
        *,
        report: dict[str, Any] | None = None,
        include_report: bool = True,
        genome: bool = False,
        gff: bool = False,
        sequence_report: bool = False,
        catalog: bool = False,
        paired: str | None = None,
) -> Path:
    """Build a minimal NCBI Datasets ZIP package on disk."""
    document = report if report is not None else _report(accession)
    if paired is not None:
        document["assemblyInfo"]["pairedAssembly"] = {"accession": paired} if paired else {}
    prefix = f"ncbi_dataset/data/{accession}"
    with zipfile.ZipFile(path, "w") as archive:
        if include_report:
            archive.writestr(
                "ncbi_dataset/data/assembly_data_report.jsonl",
                "".join(json.dumps(row) + "\n" for row in document) if isinstance(document, list)
                else json.dumps(document) + "\n",
            )
        if genome:
            archive.writestr(f"{prefix}/genomic.fna", ">chr1\nACGTACGTACGT\n")
        if gff:
            archive.writestr(
                f"{prefix}/genomic.gff",
                "##gff-version 3\nchr1\tRefSeq\tgene\t1\t4\t.\t+\t.\tID=g1\n",
            )
        if sequence_report:
            archive.writestr(f"{prefix}/sequence_report.jsonl", '{"sequence_name":"chr1"}\n')
        if catalog:
            archive.writestr("ncbi_dataset/data/dataset_catalog.json", "{}")
    return path


def _write_directory_package(
        root: Path,
        accession: str = "GCF_000001405.40",
        *,
        genome: bool = True,
        gff: bool = False,
) -> Path:
    data = root / "ncbi_dataset" / "data"
    (data / accession).mkdir(parents=True)
    (data / "assembly_data_report.jsonl").write_text(
        json.dumps(_report(accession)) + "\n", encoding="utf-8"
    )
    if genome:
        (data / accession / "genomic.fna").write_text(">chr1\nACGTACGTACGT\n", encoding="utf-8")
    if gff:
        (data / accession / "genomic.gff").write_text("##gff-version 3\n", encoding="utf-8")
    return root


def _dataset_zip_bytes(accession: str = "GCF_000001405.40") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "ncbi_dataset/data/assembly_data_report.jsonl",
            json.dumps(_report(accession)) + "\n",
        )
        archive.writestr(f"ncbi_dataset/data/{accession}/genomic.fna", ">chr1\nACGT\n")
    return buffer.getvalue()


def _readme_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.md", "NCBI Datasets\n")
    return buffer.getvalue()


def _truncate_before_central_directory(payload: bytes) -> bytes:
    return payload[: payload.find(b"PK\x01\x02")]


def _central_directory_offsets(payload: bytes, member: str) -> dict[str, int]:
    """Locate a ZIP member's local and central-directory headers.

    The truncation test rewrites the declared uncompressed size of a member,
    which requires knowing both header offsets of a ZIP created by zipfile.
    """
    eocd = payload.rfind(b"PK\x05\x06")
    _sig, _disk, _cd_disk, _n_disk, total, _size, cd_offset, _clen = struct.unpack_from(
        "<IHHHHIIH", payload, eocd
    )
    position = cd_offset
    for _ in range(total):
        fields = struct.unpack_from("<IHHHHHHIIIHHHHHII", payload, position)
        name_length, extra_length, comment_length, local_offset = (
            fields[10], fields[11], fields[12], fields[16],
        )
        name = payload[position + 46: position + 46 + name_length].decode("utf-8")
        if name == member:
            return {"central": position, "local": local_offset}
        position += 46 + name_length + extra_length + comment_length
    raise AssertionError(f"member {member!r} not found in test ZIP")


def _forbidden_download(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("no download was expected for a fully archived request")


def _forbidden_finish_run(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("a dry run must not finish a workflow run")


def _fake_parallel(handler) -> Any:
    """Return a replacement for ``download_ncbi_datasets_parallel``.

    The real function downloads ``batches`` and hands each finished/failed one
    to ``on_complete``/``on_error``; the replacement does the same while
    delegating the payload decision to ``handler(batch, staging_dir)``.
    """

    def fake(batches, staging_dir, **kwargs):
        for batch in batches:
            outcome = handler(list(batch), Path(staging_dir))
            if outcome is None:
                kwargs["on_error"](batch, ValidationError("synthetic download failure"))
                continue
            kwargs["on_complete"](batch, outcome)
        return []

    return fake


# --------------------------------------------------------------------------
# Source bundles, IDs and plan building
# --------------------------------------------------------------------------

def test_source_bundle_close_cleans_up_temporary_directory(tmp_path: Path):
    temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    root = Path(temporary.name)
    assert root.is_dir()
    bundle = ncbi.SourceBundle(source=root, root=root, label="staged", temporary=temporary)
    bundle.close()
    assert not root.exists()


def test_id_allocator_ignores_ids_that_do_not_match_the_prefix(project_db):
    _project, db = project_db
    # A development-era row whose ID does not follow the ORG_###### pattern must
    # not stop the allocator from starting at ORG_000001.
    db.insert_row("organisms", {"organism_id": "LEGACY_ORGANISM", "scientific_name": "Legacy"})
    plan = ncbi._PlanBuilder(db).build([_report()], [])
    assert plan.new_ids["organism"] == 1
    assert plan.tables["organisms"][0]["organism_id"] == "ORG_000001"


def test_plan_builder_matches_existing_assembly_without_accession_row(project_db):
    _project, db = project_db
    db.insert_row("organisms", {
        "organism_id": "ORG_000001", "scientific_name": "Homo sapiens", "taxon_id": 9606,
    })
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("assemblies", {
        "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
        "assembly_accession": "GCF_000001405.40", "assembly_version": 40,
    })
    plan = ncbi._PlanBuilder(db).build([_report()], [])
    assert plan.new_ids["assembly"] == 0
    assert plan.assembly_ids["GCF_000001405.40"] == "ASM_000001"
    assert plan.tables["assemblies"][0]["assembly_id"] == "ASM_000001"


def test_report_without_annotation_info_skips_empty_plan_tables(project_db, tmp_path):
    project, db = project_db
    report = _report()
    report.pop("annotationInfo")
    source = tmp_path / "report.jsonl"
    source.write_text(json.dumps(report) + "\n", encoding="utf-8")
    summary = ncbi.run_ncbi_datasets_adapter(db, project, inputs=[source])
    assert summary["metadata_rows"]["annotations"] == 0
    assert summary["new_ids"]["annotation"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM annotations")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM assemblies")[0]["n"] == 1
    # Every table is still validated/persisted, including the empty ones.
    states = {
        row["entity_type"]: row["state"]
        for row in db.query(
            "SELECT entity_type, state FROM entity_state WHERE entity_type != 'database'"
        )
    }
    assert states == {
        "organism": "METADATA_VALIDATED",
        "sample": "METADATA_VALIDATED",
        "assembly": "METADATA_VALIDATED",
    }


def test_paired_reports_across_inputs_merge_observed_groups(project_db, tmp_path):
    project, db = project_db
    gca = "GCA_000001405.29"
    gcf = "GCF_000001405.40"
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text(json.dumps(_report(gca)) + "\n", encoding="utf-8")
    second.write_text(json.dumps(_report(gcf)) + "\n", encoding="utf-8")
    first_gca = json.loads(first.read_text(encoding="utf-8"))
    first_gca["assemblyInfo"]["pairedAssembly"] = {"accession": gcf}
    first.write_text(json.dumps(first_gca) + "\n", encoding="utf-8")
    second_gcf = json.loads(second.read_text(encoding="utf-8"))
    second_gcf["assemblyInfo"]["pairedAssembly"] = {"accession": gca}
    second.write_text(json.dumps(second_gcf) + "\n", encoding="utf-8")

    summary = ncbi.run_ncbi_datasets_adapter(db, project, inputs=[first, second])
    # Both inputs resolve to the same assembly group, so the run reports one
    # assembly record rather than two.
    assert summary["assembly_records"] == 1
    assert len(summary["sources"]) == 2
    assert db.query("SELECT COUNT(*) AS n FROM assemblies")[0]["n"] == 1
    assert (
        db.query("SELECT assembly_accession FROM assemblies")[0]["assembly_accession"] == gcf
    )


# --------------------------------------------------------------------------
# Entrez fallback / inputs without records
# --------------------------------------------------------------------------

def test_package_without_reports_falls_back_to_entrez(project_db, monkeypatch):
    project, db = project_db
    document = {
        "AssemblyAccession": "GCF_000001405.40",
        "SpeciesName": "Homo sapiens",
        "Taxid": "9606",
        "AssemblyStatus": "Chromosome",
        "AssemblyName": "GRCh38",
        "BioSampleAccn": "SAMN00000001",
        "BioProjectAccn": "PRJNA31257",
        "RefSeq_category": "reference genome",
        "SubmissionDate": "2022-02-03",
        "SubmitterOrganization": "Genome Reference Consortium",
    }

    class Handle:
        def __init__(self, value: Any):
            self.value = value

        def __enter__(self) -> "Handle":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    entrez = type("Entrez", (), {})
    entrez.esearch = lambda **_kwargs: Handle({"IdList": ["1"]})
    entrez.esummary = lambda **_kwargs: Handle(
        {"DocumentSummarySet": {"DocumentSummary": [document]}}
    )
    entrez.read = lambda handle, **_kwargs: handle.value
    monkeypatch.setitem(sys.modules, "Bio", type("Bio", (), {"Entrez": entrez}))

    def handler(batch: list[str], staging: Path) -> Path:
        # A README/catalog-only package is exactly the "unusual package" the
        # Entrez metadata fallback exists for.
        return _write_package(staging / "batch.zip", catalog=True, include_report=False)

    monkeypatch.setattr(ncbi, "download_ncbi_datasets_parallel", _fake_parallel(handler))
    summary = ncbi.run_ncbi_datasets_adapter(
        db, project, accessions=["GCF_000001405.40"], includes=["genome"],
        email="curator@example.org",
    )
    assert summary["sources"][0]["reports"] == 1
    assert summary["sources"][0]["assets"] == 0
    assert summary["assembly_records"] == 1
    assert db.query("SELECT COUNT(*) AS n FROM assemblies")[0]["n"] == 1


def test_input_without_records_reports_no_assembly_records(project_db, tmp_path):
    project, db = project_db
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="no NCBI assembly records found"):
        ncbi.run_ncbi_datasets_adapter(db, project, inputs=[empty])
    runs = db.query("SELECT status, error FROM workflow_runs WHERE step='ncbi_datasets_import'")
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "no NCBI assembly records found" in runs[0]["error"]


# --------------------------------------------------------------------------
# Download orchestration (dry-run, aggregate failures, interruption)
# --------------------------------------------------------------------------

def test_dry_run_download_success_writes_nothing(project_db, monkeypatch):
    project, db = project_db
    seen: list[list[str]] = []

    def handler(batch: list[str], staging: Path) -> Path:
        seen.append(batch)
        return _write_package(staging / "batch.zip", batch[0], genome=True)

    monkeypatch.setattr(ncbi, "download_ncbi_datasets_parallel", _fake_parallel(handler))
    summary = ncbi.run_ncbi_datasets_adapter(
        db, project, accessions=["GCF_000001405.40"], includes=["genome"], dry_run=True,
    )
    assert seen == [["GCF_000001405.40"]]
    assert summary["dry_run"] is True
    assert summary["assembly_records"] == 1
    assert summary["discovered_files"] == 1
    assert summary["archived_files"] == []
    assert summary["download_failures"] == []
    assert db.query("SELECT COUNT(*) AS n FROM assemblies")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM workflow_runs")[0]["n"] == 0
    assert not (project.raw_root / "metadata").exists()


def test_dry_run_download_failure_is_summarized_not_raised(project_db, monkeypatch):
    project, db = project_db

    def handler(batch: list[str], staging: Path) -> None:
        return None

    monkeypatch.setattr(ncbi, "download_ncbi_datasets_parallel", _fake_parallel(handler))
    summary = ncbi.run_ncbi_datasets_adapter(
        db, project, accessions=["GCF_000001405.40"], includes=["genome"], dry_run=True,
    )
    assert summary["assembly_records"] == 0
    assert summary["download_failures"][0]["accessions"] == "GCF_000001405.40"
    assert summary["download_failures"][0]["includes"] == "genome"
    assert "synthetic download failure" in summary["download_failures"][0]["error"]
    assert db.query("SELECT COUNT(*) AS n FROM workflow_runs")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM adapter_run_items")[0]["n"] == 0


def test_all_failed_batches_raise_aggregated_download_error(project_db, monkeypatch):
    project, db = project_db

    def handler(batch: list[str], staging: Path) -> None:
        return None

    monkeypatch.setattr(ncbi, "download_ncbi_datasets_parallel", _fake_parallel(handler))
    with pytest.raises(ValidationError) as caught:
        ncbi.run_ncbi_datasets_adapter(
            db, project, accessions=["GCF_000001405.40"], includes=["genome"],
        )
    message = str(caught.value)
    assert "no NCBI assembly records could be imported; download batch failures:" in message
    assert "- GCF_000001405.40: ValidationError: synthetic download failure" in message
    runs = db.query("SELECT status FROM workflow_runs WHERE step='ncbi_datasets_import'")
    assert runs[0]["status"] == "failed"
    items = db.query("SELECT status, error FROM adapter_run_items")
    assert items[0]["status"] == "failed"
    assert "synthetic download failure" in items[0]["error"]


def test_more_than_twenty_failed_batches_truncate_details(project_db, monkeypatch):
    project, db = project_db
    accessions = [f"GCF_{number:09d}.1" for number in range(1, 23)]

    def handler(batch: list[str], staging: Path) -> Path | None:
        if batch[0] == accessions[0]:
            return _write_package(staging / "ok.zip", batch[0], genome=True)
        return None

    monkeypatch.setattr(ncbi, "download_ncbi_datasets_parallel", _fake_parallel(handler))
    with pytest.raises(ValidationError) as caught:
        ncbi.run_ncbi_datasets_adapter(
            db, project, accessions=accessions, includes=["genome"], batch_size=1,
        )
    message = str(caught.value)
    assert (
        "21 NCBI download batch(es) failed while other batches were imported successfully"
        in message
    )
    assert "- ... and 1 more failed batch(es)" in message
    # The successful batch is committed before the aggregate error is raised.
    assert db.query("SELECT COUNT(*) AS n FROM assemblies")[0]["n"] == 1
    assert (
        db.query("SELECT assembly_accession FROM assemblies")[0]["assembly_accession"]
        == accessions[0]
    )


def test_dry_run_keyboard_interrupt_is_not_audited(project_db, monkeypatch):
    project, db = project_db

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ncbi, "download_ncbi_datasets_parallel", explode)
    with pytest.raises(KeyboardInterrupt):
        ncbi.run_ncbi_datasets_adapter(
            db, project, accessions=["GCF_000001405.40"], dry_run=True,
        )
    assert db.query("SELECT COUNT(*) AS n FROM workflow_runs")[0]["n"] == 0


def test_interrupt_audit_failure_does_not_mask_shutdown(project_db, monkeypatch):
    project, db = project_db

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise KeyboardInterrupt

    def broken_finish(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("audit backend unavailable")

    monkeypatch.setattr(ncbi, "download_ncbi_datasets_parallel", explode)
    monkeypatch.setattr(ncbi, "finish_run", broken_finish)
    with pytest.raises(KeyboardInterrupt):
        ncbi.run_ncbi_datasets_adapter(db, project, accessions=["GCF_000001405.40"])
    # The audit write failed, so the run row is left untouched instead of the
    # shutdown being converted into an unrelated error.
    runs = db.query("SELECT status FROM workflow_runs WHERE step='ncbi_datasets_import'")
    assert runs[0]["status"] == "running"


def test_enospc_during_import_is_reported_as_no_space(project_db, tmp_path, monkeypatch):
    project, db = project_db
    package = _write_package(tmp_path / "package.zip", genome=True)

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ncbi, "_ingest_dataset_asset", explode)
    with pytest.raises(ValidationError) as caught:
        ncbi.run_ncbi_datasets_adapter(db, project, inputs=[package])
    assert "ran out of space" in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)
    runs = db.query("SELECT status, error FROM workflow_runs WHERE step='ncbi_datasets_import'")
    assert runs[0]["status"] == "failed"
    assert "ran out of space" in runs[0]["error"]


def test_dry_run_failure_is_not_audited(project_db, tmp_path):
    project, db = project_db
    accession = "GCF_000001405.40"
    package = tmp_path / "conflict.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "ncbi_dataset/data/assembly_data_report.jsonl",
            json.dumps(_report(accession)) + "\n",
        )
        archive.writestr(f"ncbi_dataset/data/{accession}/one_genomic.fna", ">a\nACGT\n")
        archive.writestr(f"ncbi_dataset/data/{accession}/two_genomic.fna", ">b\nTTTT\n")
    with pytest.raises(ConflictError, match="multiple different files"):
        ncbi.run_ncbi_datasets_adapter(db, project, inputs=[package], dry_run=True)
    assert db.query("SELECT COUNT(*) AS n FROM workflow_runs")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM assemblies")[0]["n"] == 0


def test_dry_run_download_processing_failure_propagates(project_db, monkeypatch):
    project, db = project_db
    accession = "GCF_000001405.40"

    def handler(batch: list[str], staging: Path) -> Path:
        destination = staging / "conflict.zip"
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr(
                "ncbi_dataset/data/assembly_data_report.jsonl",
                json.dumps(_report(accession)) + "\n",
            )
            archive.writestr(f"ncbi_dataset/data/{accession}/one_genomic.fna", ">a\nACGT\n")
            archive.writestr(f"ncbi_dataset/data/{accession}/two_genomic.fna", ">b\nTTTT\n")
        return destination

    monkeypatch.setattr(ncbi, "download_ncbi_datasets_parallel", _fake_parallel(handler))
    monkeypatch.setattr(ncbi, "finish_run", _forbidden_finish_run)
    with pytest.raises(ConflictError, match="multiple different files"):
        ncbi.run_ncbi_datasets_adapter(
            db, project, accessions=[accession], includes=["genome"], dry_run=True,
        )
    # A dry run never records workflow rows, even when the batch fails mid-import.
    assert db.query("SELECT COUNT(*) AS n FROM workflow_runs")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM adapter_run_items")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM assemblies")[0]["n"] == 0


def test_failed_run_audit_failure_does_not_mask_original_error(project_db, tmp_path, monkeypatch):
    project, db = project_db
    package = _write_package(tmp_path / "package.zip", genome=True)

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("ingest kaboom")

    def broken_finish(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("audit backend unavailable")

    monkeypatch.setattr(ncbi, "_ingest_dataset_asset", explode)
    monkeypatch.setattr(ncbi, "finish_run", broken_finish)
    with pytest.raises(RuntimeError, match="ingest kaboom"):
        ncbi.run_ncbi_datasets_adapter(db, project, inputs=[package])


# --------------------------------------------------------------------------
# Archival options: --no-archive-files, --standardize
# --------------------------------------------------------------------------

def test_archive_files_disabled_imports_metadata_only(project_db, tmp_path):
    project, db = project_db
    package = _write_package(tmp_path / "package.zip", genome=True, gff=True)
    summary = ncbi.run_ncbi_datasets_adapter(
        db, project, inputs=[package], archive_files=False,
    )
    assert summary["discovered_files"] == 2
    assert summary["archived_files"] == []
    assert summary["standardized_files"] == []
    assert db.query("SELECT COUNT(*) AS n FROM files")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM assemblies")[0]["n"] == 1
    assert db.query("SELECT COUNT(*) AS n FROM annotations")[0]["n"] == 1
    # The input package itself is still preserved for provenance.
    assert len(list((project.raw_root / "metadata" / "ncbi_datasets").iterdir())) == 1


def test_standardize_creates_standardized_copies(project_db, tmp_path):
    project, db = project_db
    directory = _write_directory_package(tmp_path / "package", genome=True)
    summary = ncbi.run_ncbi_datasets_adapter(
        db, project, inputs=[directory], standardize=True,
    )
    assert summary["archived_files"] and summary["standardized_files"]
    assert summary["standardized_files"] == summary["archived_files"]
    row = dict(db.query("SELECT * FROM files")[0])
    assert row["status"] == "STANDARDIZED"
    assert (project.root / row["relative_path"]).exists()
    standardized = [path for path in project.standardized_root.rglob("*") if path.is_file()]
    assert len(standardized) == 1
    assert standardized[0].name == Path(row["relative_path"]).name
    assert standardized[0].read_text(encoding="utf-8") == ">chr1\nACGTACGTACGT\n"


# --------------------------------------------------------------------------
# Resume/skip planning against existing archived files
# --------------------------------------------------------------------------

def test_unversioned_request_reuses_archive_and_downloads_only_missing_roles(
        project_db, tmp_path, monkeypatch):
    project, db = project_db
    gca = "GCA_000001405.29"
    gcf = "GCF_000001405.40"
    package = _write_package(
        tmp_path / "gca.zip", gca, genome=True, paired=gcf,
    )
    ncbi.run_ncbi_datasets_adapter(db, project, inputs=[package])
    assert db.query("SELECT assembly_accession FROM assemblies")[0]["assembly_accession"] == gcf

    # An unversioned request resolves to the archived version without a download.
    monkeypatch.setattr(ncbi, "download_ncbi_datasets_parallel", _forbidden_download)
    skipped = ncbi.run_ncbi_datasets_adapter(
        db, project, accessions=["GCA_000001405"], includes=["genome"],
    )
    assert skipped["skipped_existing"] == ["GCA_000001405"]
    assert skipped["download_plan"] == []

    # A widened include set downloads only the role that is actually missing.
    seen: list[tuple[list[str], tuple[str, ...]]] = []

    def handler(batch: list[str], staging: Path) -> Path:
        seen.append((batch, ("sequence-report",)))
        return _write_package(staging / "batch.zip", gca, sequence_report=True, paired=gcf)

    monkeypatch.setattr(ncbi, "download_ncbi_datasets_parallel", _fake_parallel(handler))
    widened = ncbi.run_ncbi_datasets_adapter(
        db, project, accessions=[gca], includes=["genome", "sequence-report"],
    )
    assert seen == [([gca], ("sequence-report",))]
    assert widened["skipped_existing"] == []
    assert [row["file_role"] for row in db.query(
        "SELECT file_role FROM files WHERE entity_type='assembly' ORDER BY file_role"
    )] == ["assembly_report_genbank", "genome_fasta_genbank"]


def test_missing_annotation_mapping_falls_back_to_existing_annotation_rows(
        project_db, tmp_path, monkeypatch):
    project, db = project_db
    accession = "GCF_000001405.40"
    package = _write_package(tmp_path / "package.zip", genome=True, gff=True)
    ncbi.run_ncbi_datasets_adapter(db, project, inputs=[package])
    annotation_id = db.query("SELECT annotation_id FROM annotations")[0]["annotation_id"]
    # Simulate a project written before the annotation-identity table existed.
    db.conn.execute("DELETE FROM ncbi_annotation_records")
    db.conn.commit()
    monkeypatch.setattr(ncbi, "download_ncbi_datasets_parallel", _forbidden_download)
    summary = ncbi.run_ncbi_datasets_adapter(
        db, project, accessions=[accession], includes=["gff3"],
    )
    assert summary["skipped_existing"] == [accession]
    assert db.query("SELECT COUNT(*) AS n FROM annotations")[0]["n"] == 1
    assert db.query("SELECT annotation_id FROM annotations")[0]["annotation_id"] == annotation_id


def test_file_satisfies_include_requires_standardized_copy(project_db):
    project, _db = project_db
    relative = "raw/assemblies/ASM_000001/GCF_000001405.40.genome_fasta.fna"
    local = project.root / relative
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(">chr1\nACGT\n", encoding="utf-8")
    row = {
        "status": "CHECKSUM_VERIFIED",
        "entity_id": "ASM_000001",
        "relative_path": relative,
    }
    assert ncbi._file_satisfies_include(
        project, row, entity_type="assembly", standardize=False
    ) is True
    assert ncbi._file_satisfies_include(
        project, row, entity_type="assembly", standardize=True
    ) is False
    standardized = (
        project.standardized_root / raw_bucket("assembly") / "ASM_000001" / Path(relative).name
    )
    standardized.parent.mkdir(parents=True, exist_ok=True)
    standardized.write_text(">chr1\nACGT\n", encoding="utf-8")
    assert ncbi._file_satisfies_include(
        project, row, entity_type="assembly", standardize=True
    ) is True


# --------------------------------------------------------------------------
# ZIP entry scanning
# --------------------------------------------------------------------------

def test_local_zip_entry_names_handles_truncated_names_and_zip64_markers(tmp_path: Path):
    name = b"ncbi_dataset/data/GCF_000001405.40/genomic.fna"
    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(
        struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0, 0, 0, 0, 4096, 0) + name
    )
    assert ncbi._local_zip_entry_names(truncated) == []

    zip64 = tmp_path / "zip64.zip"
    zip64.write_bytes(
        struct.pack(
            "<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0, 0, 0xFFFFFFFF, 0, len(name), 0
        ) + name
    )
    assert ncbi._local_zip_entry_names(zip64) == [name.decode("utf-8")]


# --------------------------------------------------------------------------
# Report/asset discovery edge cases
# --------------------------------------------------------------------------

def test_load_dataset_reports_handles_missing_root_and_jsonl_fallback(tmp_path: Path):
    accession = "GCF_000001405.40"
    package = _write_package(tmp_path / "package.zip", genome=True)
    from_zip = ncbi.load_dataset_reports(package)
    assert [row["accession"] for row in from_zip] == [accession]
    # A non-existent root still honours an explicitly supplied report file.
    direct = tmp_path / "direct_report.jsonl"
    direct.write_text(json.dumps(_report(accession)) + "\n", encoding="utf-8")
    assert ncbi.load_dataset_reports(tmp_path / "missing", direct_file=direct) == from_zip

    directory = tmp_path / "unpacked"
    directory.mkdir()
    fallback = directory / "unusual_name.jsonl"
    fallback.write_text(json.dumps(_report(accession)) + "\n", encoding="utf-8")
    (directory / "sequence_report.jsonl").write_text("{}\n", encoding="utf-8")
    assert [row["accession"] for row in ncbi.load_dataset_reports(directory)] == [accession]


def test_discover_dataset_assets_skips_directories_and_unattributed_files(tmp_path: Path):
    accession = "GCF_000001405.40"
    report = _report(accession)
    package = tmp_path / "package.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("ncbi_dataset/data/", "")
        archive.writestr(f"ncbi_dataset/data/{accession}/genomic.fna", ">x\nACGT\n")
    assets = ncbi.discover_dataset_assets(package, [report], "zip")
    assert [asset.role for asset in assets] == ["genome_fasta"]
    assert assets[0].path is None and assets[0].archive_member is not None
    # A member whose path carries no accession cannot be attributed without a report.
    anonymous = tmp_path / "anonymous.zip"
    with zipfile.ZipFile(anonymous, "w") as archive:
        archive.writestr("ncbi_dataset/data/genomic.fna", ">x\nACGT\n")
    assert ncbi.discover_dataset_assets(anonymous, [], "zip") == []
    assert [asset.role for asset in ncbi.discover_dataset_assets(anonymous, [report], "zip")] == [
        "genome_fasta"
    ]

    directory = tmp_path / "unpacked"
    (directory / accession).mkdir(parents=True)
    (directory / accession / "genomic.fna").write_text(">x\nACGT\n", encoding="utf-8")
    (directory / accession / "nested").mkdir()
    assert [asset.role for asset in ncbi.discover_dataset_assets(directory, [report], "dir")] == [
        "genome_fasta"
    ]
    # A file without an accession in its path falls back to the single report.
    unnamed = tmp_path / "unnamed"
    unnamed.mkdir()
    (unnamed / "genomic.fna").write_text(">x\nACGT\n", encoding="utf-8")
    assert [
        asset.accession for asset in ncbi.discover_dataset_assets(unnamed, [report], "dir")
    ] == [accession]
    # Two reports make the package accession ambiguous: unnamed files are dropped.
    assert ncbi.discover_dataset_assets(
        unnamed, [report, _report("GCF_000001406.1")], "dir"
    ) == []


def test_non_utf8_report_is_rejected_with_source_name(tmp_path: Path):
    jsonl = tmp_path / "assembly_data_report.jsonl"
    jsonl.write_bytes(b'{"accession": "GCF_000001405.40"}\n\xff\xfe broken\n')
    with pytest.raises(ValidationError, match="not UTF-8 text"):
        ncbi.load_dataset_reports(jsonl)

    csv_report = tmp_path / "assembly_report.csv"
    csv_report.write_bytes(b"Assembly Accession,Organism Name\n\xff\xfe,broken\n")
    with pytest.raises(ValidationError, match="not UTF-8 text"):
        ncbi.load_dataset_reports(csv_report)


def test_json_report_fallback_skips_blank_lines_and_non_objects():
    handle = io.StringIO('{"x": 1}\n\n7\n{"x": 2}\n')
    assert ncbi._read_report_handle(handle, "unknown.txt") == [{"x": 1}, {"x": 2}]
    assert ncbi._read_report_handle(io.StringIO('{"only": "header"}\n'), "report.txt") == [
        {"only": "header"}
    ]


def test_biosample_attributes_ignore_rows_without_name_or_value():
    report = _report()
    report["assemblyInfo"]["biosample"]["attributes"] = [
        {"name": "country", "value": ""},
        {"name": "", "value": "ignored"},
        {"name": "host", "value": "human"},
    ]
    metadata = ncbi._extract_metadata(report)
    assert metadata["host"] == "human"
    assert metadata["country"] is None


def test_accession_from_path_keeps_first_unversioned_match():
    # Neither path part is versioned, so the file's own accession (searched
    # first, from the right) is the fallback that wins.
    path = Path("ncbi_dataset/data/GCA_000001405/GCA_000001406.fna")
    assert ncbi._accession_from_path(path) == "GCA_000001406"


# --------------------------------------------------------------------------
# Project schema upgrade
# --------------------------------------------------------------------------

def _schema_path(root: Path) -> Path:
    return root / "config" / "schemas.yaml"


def _load_schema_document(root: Path) -> dict[str, Any]:
    return yaml.safe_load(_schema_path(root).read_text(encoding="utf-8"))


def _store_schema_document(root: Path, document: dict[str, Any]) -> None:
    _schema_path(root).write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def test_adapter_reports_unreadable_project_schema(project_db, tmp_path, capsys):
    _project, _db = project_db
    source = tmp_path / "report.jsonl"
    source.write_text(json.dumps(_report()) + "\n", encoding="utf-8")
    _schema_path(tmp_path).unlink()
    assert main(["--project", str(tmp_path), "ncbi-datasets", "--input", str(source)]) == 2
    assert "cannot read project metadata schema" in capsys.readouterr().err


def test_adapter_rejects_schema_without_tables_mapping(project_db, tmp_path):
    project, db = project_db
    _store_schema_document(tmp_path, {"schema_version": "1.4"})
    with pytest.raises(ValidationError, match="must contain a 'tables' mapping"):
        ncbi.run_ncbi_datasets_adapter(db, project, accessions=["GCF_000001405.40"], plan_only=True)


def test_adapter_rejects_schema_without_assembly_fields(project_db, tmp_path):
    project, db = project_db
    document = _load_schema_document(tmp_path)
    document["tables"]["assemblies"].pop("fields")
    _store_schema_document(tmp_path, document)
    with pytest.raises(ValidationError, match="no assemblies.fields mapping"):
        ncbi.run_ncbi_datasets_adapter(db, project, accessions=["GCF_000001405.40"], plan_only=True)


def test_adapter_rejects_schema_without_file_role_allowed_list(project_db, tmp_path):
    project, db = project_db
    document = _load_schema_document(tmp_path)
    document["tables"]["files"]["fields"]["file_role"].pop("allowed")
    _store_schema_document(tmp_path, document)
    with pytest.raises(ValidationError, match="no files.file_role.allowed list"):
        ncbi.run_ncbi_datasets_adapter(db, project, accessions=["GCF_000001405.40"], plan_only=True)


def test_adapter_upgrades_schema_with_missing_ncbi_roles(project_db, tmp_path):
    project, db = project_db
    document = _load_schema_document(tmp_path)
    allowed = document["tables"]["files"]["fields"]["file_role"]["allowed"]
    for role in ncbi.NCBI_SOURCE_FILE_ROLES:
        while role in allowed:
            allowed.remove(role)
    _store_schema_document(tmp_path, document)
    package = _write_package(
        tmp_path / "gca.zip", "GCA_000001405.29", genome=True, paired="GCF_000001405.40",
    )
    ncbi.run_ncbi_datasets_adapter(db, project, inputs=[package])
    upgraded = _load_schema_document(tmp_path)
    assert set(ncbi.NCBI_SOURCE_FILE_ROLES) <= set(
        upgraded["tables"]["files"]["fields"]["file_role"]["allowed"]
    )
    # The restored role is what lets the GenBank paired artifact be validated.
    assert db.query("SELECT file_role FROM files")[0]["file_role"] == "genome_fasta_genbank"


# --------------------------------------------------------------------------
# Per-asset ingestion
# --------------------------------------------------------------------------

def test_ingest_asset_without_readable_source_is_rejected(project_db):
    project, db = project_db
    asset = ncbi.DatasetAsset(
        path=None, accession="GCF_000001405.40", role="genome_fasta",
        source_url="ncbi-datasets:test",
    )
    with pytest.raises(ValidationError, match="no readable source"):
        ncbi._ingest_dataset_asset(
            db, project, asset, "assembly", "ASM_000001", run_id="WF_test", standardize=False,
        )


def test_zip_member_with_declared_size_mismatch_is_rejected(project_db, tmp_path):
    project, db = project_db
    accession = "GCF_000001405.40"
    package = tmp_path / "package.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "ncbi_dataset/data/assembly_data_report.jsonl",
            json.dumps(_report(accession)) + "\n",
        )
        archive.writestr(f"ncbi_dataset/data/{accession}/genomic.fna", ">x\nACGTACGTACGT\n")
    member = f"ncbi_dataset/data/{accession}/genomic.fna"
    payload = bytearray(package.read_bytes())
    offsets = _central_directory_offsets(bytes(payload), member)
    # Claim a larger uncompressed size than the member actually carries: the
    # checksum still matches, but the extracted file is short.
    struct.pack_into("<I", payload, offsets["local"] + 22, 9999)
    struct.pack_into("<I", payload, offsets["central"] + 24, 9999)
    package.write_bytes(bytes(payload))
    assert zipfile.ZipFile(package).getinfo(member).file_size == 9999

    with pytest.raises(ValidationError) as caught:
        ncbi.run_ncbi_datasets_adapter(db, project, inputs=[package])
    assert "truncated NCBI ZIP member" in str(caught.value)
    assert "expected 9999 bytes" in str(caught.value)


def test_extraction_failures_are_wrapped_as_validation_errors(project_db, tmp_path, monkeypatch):
    project, db = project_db
    package = _write_package(tmp_path / "package.zip", genome=True)

    def broken_copyfileobj(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("simulated read failure")

    monkeypatch.setattr(ncbi.shutil, "copyfileobj", broken_copyfileobj)
    with pytest.raises(ValidationError, match="cannot extract NCBI ZIP asset"):
        ncbi.run_ncbi_datasets_adapter(db, project, inputs=[package], preserve_sources=False)


def test_extraction_enospc_is_reported_as_no_space(project_db, tmp_path, monkeypatch):
    project, db = project_db
    package = _write_package(tmp_path / "package.zip", genome=True)

    def full_disk(*_args: Any, **_kwargs: Any) -> None:
        error = OSError("No space left on device")
        error.errno = errno.ENOSPC
        raise error

    monkeypatch.setattr(ncbi.shutil, "copyfileobj", full_disk)
    with pytest.raises(ValidationError, match="ran out of space"):
        ncbi.run_ncbi_datasets_adapter(db, project, inputs=[package], preserve_sources=False)


def test_directory_input_ingests_files_without_staging(project_db, tmp_path):
    project, db = project_db
    directory = _write_directory_package(tmp_path / "package", genome=True, gff=True)
    nested = directory / "ncbi_dataset" / "data" / "GCF_000001405.40" / "subdir"
    nested.mkdir()
    summary = ncbi.run_ncbi_datasets_adapter(db, project, inputs=[directory], preserve_sources=True)
    assert summary["discovered_files"] == 2
    assert len(summary["archived_files"]) == 2
    row = db.query("SELECT relative_path, source_url FROM files ORDER BY file_role LIMIT 1")[0]
    assert row["source_url"].startswith("ncbi-datasets:")
    assert (project.root / row["relative_path"]).exists()
    # Directory inputs are already on the project filesystem: nothing to preserve.
    assert not (project.raw_root / "metadata" / "ncbi_datasets").exists()


# --------------------------------------------------------------------------
# Disk space / source preservation helpers
# --------------------------------------------------------------------------

def test_no_space_error_walks_up_and_tolerates_usage_failure(tmp_path, monkeypatch):
    error = ncbi._no_space_error(tmp_path / "missing" / "deeper", "archive asset", OSError("full"))
    assert "ran out of space" in str(error)
    assert str(tmp_path) in str(error)

    def broken_usage(_path: Path) -> Any:
        raise OSError("statvfs failed")

    monkeypatch.setattr(ncbi.shutil, "disk_usage", broken_usage)
    error = ncbi._no_space_error(tmp_path, "archive asset", OSError("full"))
    assert "currently available" not in str(error)


def test_open_source_preserves_existing_files(project_db, tmp_path, monkeypatch):
    project, _db = project_db
    monkeypatch.setattr(ncbi, "_require_disk_space", lambda *_args: None)
    source = tmp_path / "report.jsonl"
    source.write_text(json.dumps(_report()) + "\n", encoding="utf-8")
    bundle = ncbi._open_source(source, project, True, label="explicit")
    try:
        assert bundle.preserved_path is not None
        assert bundle.preserved_path.is_file()
        assert bundle.preserved_path.read_bytes() == source.read_bytes()
        assert bundle.label == "explicit"
    finally:
        bundle.close()


# --------------------------------------------------------------------------
# Synchronous (requests) download path
# --------------------------------------------------------------------------

class _SyncResponse:
    """Minimal stand-in for ``requests.Response`` with a streamed body."""

    def __init__(
            self,
            status: int = 200,
            payload: bytes = b"",
            headers: dict[str, str] | None = None,
            body_error: BaseException | None = None,
    ) -> None:
        self.status_code = status
        self.payload = payload
        self.headers = {"Content-Length": str(len(payload))} if headers is None else headers
        self.body_error = body_error
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int):
        assert chunk_size > 0
        if self.body_error is not None:
            raise self.body_error
        yield self.payload

    def close(self) -> None:
        self.closed = True


class _ScriptedSession:
    """Returns/raises one scripted outcome per ``get`` call."""

    def __init__(self, outcomes: Sequence[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []
        self.closed = False

    def get(self, url: str, **_kwargs: Any) -> Any:
        self.calls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


def test_download_builds_retrying_session_when_none_supplied(tmp_path, monkeypatch):
    payload = _dataset_zip_bytes()
    created: list[Any] = []

    class FakeSession:
        def __init__(self) -> None:
            self.mounted: list[tuple[str, Any]] = []
            self.closed = False
            created.append(self)

        def mount(self, prefix: str, adapter: Any) -> None:
            self.mounted.append((prefix, adapter))

        def get(self, *_args: Any, **_kwargs: Any) -> _SyncResponse:
            return _SyncResponse(200, payload)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(requests, "Session", FakeSession)
    destination = tmp_path / "dataset.zip"
    assert ncbi.download_ncbi_dataset(
        ["GCF_000001405.40"], destination, includes=["genome"], max_retries=0,
    ) == destination
    assert zipfile.is_zipfile(destination)
    assert destination.stat().st_size == len(payload)
    session = created[0]
    assert [prefix for prefix, _adapter in session.mounted] == ["https://"]
    assert isinstance(session.mounted[0][1], requests.adapters.HTTPAdapter)
    assert session.closed is True


@pytest.mark.parametrize(
    ("error", "fragment", "calls"),
    [
        # The three transport classes below are swallowed for the primary API
        # base and only re-raised on the alpha fallback, so each attempt costs
        # two calls.
        (ssl.SSLError("ssl record layer failure"), "ssl record layer failure", 4),
        (requests.exceptions.ConnectionError("connection reset by peer"),
         "connection reset by peer", 4),
        (requests.exceptions.Timeout("read timed out"), "read timed out", 4),
        (requests.exceptions.ChunkedEncodingError("chunked encoding error"), "chunked", 2),
    ],
)
def test_download_retries_transient_transport_errors(tmp_path, monkeypatch, error, fragment, calls):
    monkeypatch.setattr(ncbi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ncbi.random, "uniform", lambda _low, _high: 0.0)
    session = _ScriptedSession([error] * calls)
    with pytest.raises(ValidationError, match="failed after 2 attempt") as caught:
        ncbi.download_ncbi_dataset(
            ["GCF_000001405.40"], tmp_path / "retry.zip", session=session,
            max_retries=1, retry_backoff=0.0,
        )
    assert fragment in str(caught.value)
    assert len(session.calls) == calls


def test_download_retries_retryable_http_status_from_both_bases(tmp_path, monkeypatch):
    monkeypatch.setattr(ncbi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ncbi.random, "uniform", lambda _low, _high: 0.0)
    primary = _SyncResponse(503)
    fallback = _SyncResponse(503)
    session = _ScriptedSession([primary, fallback])
    with pytest.raises(ValidationError, match="failed after 1 attempt") as caught:
        ncbi.download_ncbi_dataset(
            ["GCF_000001405.40"], tmp_path / "busy.zip", session=session,
            max_retries=0, retry_backoff=0.0,
        )
    assert "HTTP 503 from NCBI Datasets" in str(caught.value)
    assert primary.closed and fallback.closed
    assert session.calls[1].startswith(ncbi.NCBI_DATASETS_API_FALLBACK)


def test_download_recovers_from_missing_client_response(tmp_path, monkeypatch):
    payload = _dataset_zip_bytes()
    session = _ScriptedSession([None, _SyncResponse(200, payload)])
    monkeypatch.setattr(ncbi, "_require_disk_space", lambda *_args: None)
    destination = tmp_path / "fallback.zip"
    assert ncbi.download_ncbi_dataset(
        ["GCF_000001405.40"], destination, session=session, max_retries=0,
    ) == destination
    assert zipfile.is_zipfile(destination)
    assert session.calls[0].endswith("/datasets/v2/genome/accession/GCF_000001405.40/download")
    assert session.calls[1].startswith(ncbi.NCBI_DATASETS_API_FALLBACK)


@pytest.mark.parametrize(
    "error",
    [
        ssl.SSLError("ssl fallback failure"),
        requests.exceptions.ConnectionError("fallback reset"),
        requests.exceptions.Timeout("fallback timeout"),
    ],
)
@pytest.mark.parametrize("fallback_fails", [False, True])
def test_download_handles_transport_errors_from_the_fallback_base(
        tmp_path, monkeypatch, error, fallback_fails):
    monkeypatch.setattr(ncbi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ncbi.random, "uniform", lambda _low, _high: 0.0)
    payload = _dataset_zip_bytes()
    if fallback_fails:
        # Primary answers 404 (prompting the alpha fallback), which then fails.
        session = _ScriptedSession([_SyncResponse(404), error])
        with pytest.raises(ValidationError, match="failed after 1 attempt") as caught:
            ncbi.download_ncbi_dataset(
                ["GCF_000001405.40"], tmp_path / "alpha.zip", session=session, max_retries=0,
            )
        assert str(error) in str(caught.value)
    else:
        # The fallback base recovers, so the primary transport error is retried
        # against the alpha endpoint within the same attempt.
        session = _ScriptedSession([error, _SyncResponse(200, payload)])
        destination = tmp_path / "alpha.zip"
        assert ncbi.download_ncbi_dataset(
            ["GCF_000001405.40"], destination, session=session, max_retries=0,
        ) == destination
        assert session.calls[1].startswith(ncbi.NCBI_DATASETS_API_FALLBACK)
    assert len(session.calls) == 2


def test_download_retries_retryable_non_zip_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(ncbi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ncbi.random, "uniform", lambda _low, _high: 0.0)
    session = _ScriptedSession([_SyncResponse(200, b"temporary gateway failure")])
    destination = tmp_path / "broken.zip"
    with pytest.raises(ValidationError, match="failed after 1 attempt") as caught:
        ncbi.download_ncbi_dataset(
            ["GCF_000001405.40"], destination, session=session, max_retries=0,
        )
    assert "no recognizable ZIP content" in str(caught.value)
    assert not destination.exists()


def test_download_cleanup_failure_does_not_hide_payload_error(tmp_path, monkeypatch):
    def broken_unlink(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(ncbi.os, "unlink", broken_unlink)
    session = _ScriptedSession([_SyncResponse(200, b"temporary gateway failure")])
    destination = tmp_path / "broken.zip"
    with pytest.raises(ValidationError, match="no recognizable ZIP content"):
        ncbi.download_ncbi_dataset(
            ["GCF_000001405.40"], destination, session=session, max_retries=0,
        )
    assert not destination.exists()


def test_download_enospc_is_reported_as_no_space(tmp_path, monkeypatch):
    def full_disk(_fd: int) -> None:
        error = OSError("No space left on device")
        error.errno = errno.ENOSPC
        raise error

    monkeypatch.setattr(ncbi.os, "fsync", full_disk)
    session = _ScriptedSession([_SyncResponse(200, _dataset_zip_bytes())])
    destination = tmp_path / "full.zip"
    with pytest.raises(ValidationError, match="ran out of space") as caught:
        ncbi.download_ncbi_dataset(
            ["GCF_000001405.40"], destination, session=session, max_retries=0,
        )
    assert "download NCBI dataset package" in str(caught.value)
    assert not destination.exists()


# --------------------------------------------------------------------------
# Asynchronous (aiohttp) download path
# --------------------------------------------------------------------------

class _FakeClientError(Exception):
    """Stands in for aiohttp's connection/payload error classes."""


class _FakeClientResponseError(Exception):
    def __init__(self, status: int = 0, message: str = "error") -> None:
        super().__init__(message)
        self.status = status


class _FakeContent:
    def __init__(self, chunks: Sequence[bytes] = (), error: BaseException | None = None) -> None:
        self.chunks = list(chunks)
        self.error = error

    async def iter_chunked(self, _size: int):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


class _FakeAiohttpResponse:
    def __init__(
            self,
            status: int = 200,
            chunks: Sequence[bytes] = (),
            headers: dict[str, str] | None = None,
            body_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self.content = _FakeContent(chunks, body_error)
        self.released = False

    def release(self) -> None:
        self.released = True

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise _FakeClientResponseError(self.status, f"HTTP {self.status}")


def _install_fake_aiohttp(monkeypatch, outcomes: list[Any]) -> Any:
    class Session:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, *_args: Any, **_kwargs: Any) -> Any:
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    fake = type("FakeAiohttp", (), {
        "ClientSession": Session,
        "ClientTimeout": lambda **kwargs: kwargs,
        "ClientSSLError": _FakeClientError,
        "ClientConnectionError": _FakeClientError,
        "ServerDisconnectedError": _FakeClientError,
        "ClientPayloadError": _FakeClientError,
        "ClientResponseError": _FakeClientResponseError,
    })
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    return fake


def _aiohttp_kwargs(destination: Path, *, max_retries: int = 0) -> dict[str, Any]:
    return dict(
        accessions=["GCF_000001405.40"],
        destination=destination,
        includes=["genome"],
        email=None,
        api_key=None,
        timeout=1.0,
        max_retries=max_retries,
        retry_backoff=0.0,
        cancel_event=threading.Event(),
    )


def test_aiohttp_download_wraps_connection_errors_from_both_bases(tmp_path, monkeypatch):
    _install_fake_aiohttp(
        monkeypatch, [_FakeClientError("primary reset"), _FakeClientError("alpha reset")]
    )
    with pytest.raises(ValidationError, match="failed after 1 attempt") as caught:
        asyncio.run(ncbi._download_batch_aiohttp(**_aiohttp_kwargs(tmp_path / "x.zip")))
    assert "alpha reset" in str(caught.value)


def test_aiohttp_download_retries_payload_errors_during_transfer(tmp_path, monkeypatch):
    _install_fake_aiohttp(monkeypatch, [
        _FakeAiohttpResponse(200, [b"partial"], body_error=_FakeClientError("body reset")),
    ])
    with pytest.raises(ValidationError, match="failed after 1 attempt") as caught:
        asyncio.run(ncbi._download_batch_aiohttp(**_aiohttp_kwargs(tmp_path / "x.zip")))
    assert "body reset" in str(caught.value)


def test_aiohttp_download_treats_non_enospc_oserror_as_transient(tmp_path, monkeypatch):
    def broken_fsync(_fd: int) -> None:
        raise OSError(errno.EIO, "fsync failed")

    monkeypatch.setattr(ncbi.os, "fsync", broken_fsync)
    _install_fake_aiohttp(monkeypatch, [_FakeAiohttpResponse(200, [_dataset_zip_bytes()])])
    destination = tmp_path / "io-error.zip"
    with pytest.raises(ValidationError, match="failed after 1 attempt") as caught:
        asyncio.run(ncbi._download_batch_aiohttp(**_aiohttp_kwargs(destination)))
    assert "fsync failed" in str(caught.value)
    assert not destination.exists()


def test_aiohttp_download_retries_retryable_client_response_error(tmp_path, monkeypatch):
    _install_fake_aiohttp(monkeypatch, [_FakeClientResponseError(503, "503 from client")])
    with pytest.raises(ValidationError, match="failed after 1 attempt") as caught:
        asyncio.run(ncbi._download_batch_aiohttp(**_aiohttp_kwargs(tmp_path / "x.zip")))
    assert "503 from client" in str(caught.value)


def test_aiohttp_download_rejects_truncated_readme_package(tmp_path, monkeypatch):
    payload = _truncate_before_central_directory(_readme_zip_bytes())
    _install_fake_aiohttp(monkeypatch, [
        _FakeAiohttpResponse(200, [payload], {"Content-Length": str(len(payload))}),
    ])
    destination = tmp_path / "readme-only.zip"
    with pytest.raises(ValidationError, match="README-only") as caught:
        asyncio.run(ncbi._download_batch_aiohttp(**_aiohttp_kwargs(destination)))
    assert "GCF_000001405.40" in str(caught.value)
    assert not destination.exists()


def test_download_retries_body_errors_during_transfer(tmp_path, monkeypatch):
    monkeypatch.setattr(ncbi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ncbi.random, "uniform", lambda _low, _high: 0.0)
    body_error = requests.exceptions.ChunkedEncodingError("body truncated")
    session = _ScriptedSession([
        _SyncResponse(200, body_error=body_error),
        _SyncResponse(200, body_error=body_error),
    ])
    destination = tmp_path / "truncated.zip"
    with pytest.raises(ValidationError, match="failed after 2 attempt") as caught:
        ncbi.download_ncbi_dataset(
            ["GCF_000001405.40"], destination, session=session,
            max_retries=1, retry_backoff=0.0,
        )
    assert "body truncated" in str(caught.value)
    assert not destination.exists()


def test_download_retries_connection_error_raised_while_closing(tmp_path, monkeypatch):
    monkeypatch.setattr(ncbi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ncbi.random, "uniform", lambda _low, _high: 0.0)
    payload = _dataset_zip_bytes()

    class FlakyCloseResponse(_SyncResponse):
        """Fails the first close() of the attempt, mimicking a dropped connection."""

        close_calls: list[Any] = []

        def close(self) -> None:
            FlakyCloseResponse.close_calls.append(self)
            if len(FlakyCloseResponse.close_calls) == 1:
                raise requests.exceptions.ConnectionError("close failed")
            self.closed = True

    first = FlakyCloseResponse(200, payload)
    second = FlakyCloseResponse(200, payload)
    session = _ScriptedSession([first, second])
    destination = tmp_path / "close-retry.zip"
    assert ncbi.download_ncbi_dataset(
        ["GCF_000001405.40"], destination, session=session,
        max_retries=1, retry_backoff=0.0,
    ) == destination
    assert zipfile.is_zipfile(destination)
    assert FlakyCloseResponse.close_calls == [first, second]
    assert second.closed is True


# --------------------------------------------------------------------------
# Parallel download orchestration
# --------------------------------------------------------------------------

def test_parallel_download_skips_entries_without_payload_or_error(tmp_path, monkeypatch):
    async def fake_runner(**kwargs: Any) -> None:
        kwargs["completed_queue"].put((kwargs["batches"][0], None, None))

    monkeypatch.setattr(ncbi, "_download_batches_async", fake_runner)
    consumed: list[Any] = []
    assert ncbi.download_ncbi_datasets_parallel(
        [["GCF_000001405.40"]], tmp_path, max_workers=1,
        on_complete=lambda batch, path: consumed.append((batch, path)),
    ) == []
    assert consumed == []


def test_parallel_download_truncates_long_failure_details(tmp_path, monkeypatch):
    failures = 21

    async def fake_runner(**kwargs: Any) -> None:
        for index in range(failures):
            batch = [f"GCF_{index:09d}.1"]
            kwargs["completed_queue"].put((batch, None, ValidationError("not found")))

    monkeypatch.setattr(ncbi, "_download_batches_async", fake_runner)
    batches = [[f"GCF_{index:09d}.1"] for index in range(failures)]
    with pytest.raises(ValidationError) as caught:
        ncbi.download_ncbi_datasets_parallel(
            batches, tmp_path, max_workers=1, on_complete=lambda *_args: None,
        )
    message = str(caught.value)
    assert f"{failures}/{failures} NCBI download batch(es) failed" in message
    assert "- GCF_000000000.1: not found" in message
    assert "- ... and 1 more failed batch(es)" in message


def test_download_batches_rechecks_cancellation_after_waiting_for_a_worker(tmp_path, monkeypatch):
    class CountingEvent:
        """Reports "cancelled" only for the worker that waited on the semaphore."""

        def __init__(self) -> None:
            self.calls = 0

        def is_set(self) -> bool:
            self.calls += 1
            return self.calls == 4

    async def fake_download(batch, destination, **_kwargs):
        Path(destination).write_bytes(b"zip")
        return Path(destination)

    monkeypatch.setattr(ncbi, "_download_batch_aiohttp", fake_download)
    completed: queue.Queue[Any] = queue.Queue()
    asyncio.run(ncbi._download_batches_async(
        batches=[["GCF_000000001.1"], ["GCF_000000002.2"]],
        staging_dir=tmp_path, includes=["genome"], email=None, api_key=None, timeout=1.0,
        max_workers=1, max_retries=0, retry_backoff=0.0,
        completed_queue=completed, cancel_event=CountingEvent(),
    ))
    items = [completed.get_nowait(), completed.get_nowait()]
    cancelled = [item for item in items if isinstance(item[2], ncbi._DownloadCancelled)]
    succeed = [item for item in items if item[2] is None]
    assert len(cancelled) == 1 and cancelled[0][1] is None
    assert len(succeed) == 1 and succeed[0][1] is not None


def test_download_batches_cancel_pending_tasks_after_a_failure(tmp_path, monkeypatch):
    started = threading.Event()
    cancelled = threading.Event()

    async def blocked(batch, destination, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    class ExplodingEvent:
        """Fails the coordinator while the first batch is still downloading."""

        def __init__(self) -> None:
            self.calls = 0

        def is_set(self) -> bool:
            self.calls += 1
            if self.calls >= 3:
                raise RuntimeError("cancel probe")
            return False

    monkeypatch.setattr(ncbi, "_download_batch_aiohttp", blocked)
    with pytest.raises(RuntimeError, match="cancel probe"):
        asyncio.run(ncbi._download_batches_async(
            batches=[["GCF_000000001.1"], ["GCF_000000002.2"]],
            staging_dir=tmp_path, includes=["genome"], email=None, api_key=None, timeout=1.0,
            max_workers=1, max_retries=0, retry_backoff=0.0,
            completed_queue=queue.Queue(), cancel_event=ExplodingEvent(),
        ))
    assert started.is_set() is True
    assert cancelled.is_set() is True
