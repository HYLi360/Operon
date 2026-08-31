"""Focused edge-path coverage for the offline NCBI Datasets helpers."""

from __future__ import annotations

import io
import json
import asyncio
import queue
import ssl
import struct
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from operon.adapters import ncbi_datasets as ncbi
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ConflictError, ValidationError


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Complete Genome", "complete_genome"),
        ("chromosome", "chromosome"),
        ("Scaffold", "scaffold"),
        ("contig", "contig"),
        ("chromosome arm", None),
        (None, None),
    ],
)
def test_assembly_level_normalization(value, expected):
    assert ncbi._normalize_assembly_level(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("reference genome", "reference"),
        ("representative genome", "representative"),
        ("alternate locus", "alternate"),
        ("na", "other"),
        ("", None),
    ],
)
def test_reference_status_normalization(value, expected):
    assert ncbi._normalize_reference_status(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-02-29T12:30:00Z", "2024-02-29"),
        ("2024/02/29", "2024-02-29"),
        ("Feb 29, 2024", "2024-02-29"),
        ("2024-02", "2024-02-01"),
        ("bad", None),
        (None, None),
    ],
)
def test_date_normalization(value, expected):
    assert ncbi._date_only(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("38.9 N 77.0 W", (38.9, -77.0)),
        ("12.5 S 33.25 E", (-12.5, 33.25)),
        ("-12.5 33.25", (-12.5, 33.25)),
        ("91 N 0 E", (None, None)),
        ("not coordinates", (None, None)),
    ],
)
def test_lat_lon_normalization(value, expected):
    assert ncbi._lat_lon(value) == expected


def test_accession_and_scalar_helpers_cover_invalid_and_empty_values(tmp_path: Path):
    assert ncbi._canonical_accession(" gcf_000001405.40 ") == "GCF_000001405.40"
    assert ncbi._canonical_accession("") == ""
    with pytest.raises(ValidationError, match="invalid NCBI assembly accession"):
        ncbi._canonical_accession("not-an-accession")
    assert ncbi._split_accession("GCA_123456789") == ("GCA_123456789", None)
    assert ncbi._split_accession("bad") == ("BAD", None)
    assert ncbi._accession_version("GCA_123456789.7") == 7
    assert ncbi._assembly_namespace("gcf_123456789.1") == "NCBI_RefSeq_Assembly"
    assert ncbi._assembly_namespace("GCA_123456789.1") == "NCBI_GenBank_Assembly"
    assert ncbi._version_tuple("v2.10 beta 3") == (2, 10, 3)
    assert ncbi._version_tuple(None) == (0,)
    assert ncbi._integer_or_none("7") == 7
    assert ncbi._integer_or_none("x") is None
    assert ncbi._integer_or_none(None) is None
    assert ncbi._float_or_none("7.5") == 7.5
    assert ncbi._float_or_none(object()) is None
    assert ncbi._float_or_none("") is None
    assert ncbi._normalize_sex("MALE") == "male"
    assert ncbi._normalize_sex("unexpected") == "unknown"
    assert ncbi._normalize_sex("") is None
    assert ncbi._normalize_source_database("RefSeq", "GCA_123456789.1") == "RefSeq"
    assert ncbi._normalize_source_database("", "GCF_123456789.1") == "RefSeq"
    assert ncbi._normalize_source_database("GenBank", "GCA_123456789.1") == "GenBank"
    assert ncbi._normalize_source_database("", "GCA_123456789.1") == "GenBank"
    assert ncbi._normalize_source_database("ENA", "XYZ") == "other"

    accession_file = tmp_path / "accessions.txt"
    accession_file.write_text(
        "# comment\nGCF_000001405.40 extra\n\nGCA_000001405.29\n",
        encoding="utf-8",
    )
    assert ncbi._collect_accessions(["gcf_000001405.40"], accession_file) == [
        "GCF_000001405.40", "GCA_000001405.29"
    ]
    with pytest.raises(ValidationError, match="accession file does not exist"):
        ncbi._collect_accessions([], tmp_path / "missing.txt")
    assert list(ncbi._chunks(["a", "b", "c"], 2)) == [["a", "b"], ["c"]]


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({}, "provide at least one"),
        ({"inputs": ["offline"], "accessions": ["GCF_000001405.40"], "plan_only": True},
         "plan-only"),
        ({"accessions": ["GCF_000001405.40"], "includes": ["unknown"]}, "unknown NCBI include"),
        ({"accessions": ["GCF_000001405.40"], "batch_size": 0}, "batch-size"),
        ({"accessions": ["GCF_000001405.40"], "download_workers": 11}, "download-workers"),
        ({"accessions": ["GCF_000001405.40"], "max_retries": -1}, "retries"),
        ({"accessions": ["GCF_000001405.40"], "retry_backoff": -0.1}, "retry-backoff"),
    ],
)
def test_adapter_rejects_invalid_top_level_options_before_io(overrides, message):
    kwargs = {
        "inputs": (),
        "accessions": (),
        "includes": ncbi.DEFAULT_INCLUDES,
        "plan_only": False,
        "batch_size": 10,
        "download_workers": 3,
        "max_retries": 4,
        "retry_backoff": 1.0,
    }
    kwargs.update(overrides)
    with pytest.raises(ValidationError, match=message):
        ncbi.run_ncbi_datasets_adapter(SimpleNamespace(), SimpleNamespace(), **kwargs)


def test_mapping_merge_and_deduplication_helpers():
    assert ncbi._pick({"Assembly Info": 0, "assembly_info": "yes"}, "assembly info") == "yes"
    assert ncbi._pick({"Assembly Info": 0}, "assembly info") == 0
    assert ncbi._pick([], "x") is None
    assert ncbi._mapping({"x": 1}) == {"x": 1}
    assert ncbi._mapping([]) == {}
    assert ncbi._merge_nonempty({"a": 1}, {"a": "", "b": None, "c": 3}) == {
        "a": 1, "b": None, "c": 3
    }
    assert ncbi._deep_merge(
        {"nested": {"a": 1}, "keep": "x"},
        {"nested": {"b": 2}, "keep": "", "new": [1]},
    ) == {"nested": {"a": 1, "b": 2}, "keep": "x", "new": [1]}
    assert ncbi._unique([None, "", "a", "a", "b"]) == ["a", "b"]

    first = {
        "accession": "GCA_000001405.29",
        "currentAccession": "GCF_000001405.40",
        "organism": {"organismName": "Example"},
    }
    second = {
        "accession": "GCF_000001405.40",
        "assemblyInfo": {"assemblyLevel": "Chromosome"},
    }
    assert len(ncbi._deduplicate_reports([{}, first, second])) == 1
    assert ncbi._report_has_accession(first, "GCF_000001405.40")
    assert not ncbi._report_has_accession(first, "GCA_999999999.1")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("assembly_data_report.jsonl", None),
        ("sequence_report.jsonl", "assembly_report"),
        ("genomic.gff.gz", "annotation_gff3"),
        ("protein.faa", "protein_fasta"),
        ("random.faa", None),
        ("cds_from_genomic.fna.gz", "cds_fasta"),
        ("rna.fna", None),
        ("genomic.fna", "genome_fasta"),
        ("README.md", None),
    ],
)
def test_asset_role_detection(name, expected):
    assert ncbi._asset_role(Path(name)) == expected


def test_accession_path_and_role_selection():
    path = Path("ncbi_dataset/data/GCF_000001405.40/GCF_000001405_GRCh38_genomic.fna")
    assert ncbi._accession_from_path(path) == "GCF_000001405.40"
    assert ncbi._accession_from_path(Path("GCA_123456789") / "genomic.fna") == "GCA_123456789"
    assert ncbi._accession_from_path(Path("plain.txt")) == ""

    current = {"assembly_accession": "GCA_000001405.29"}
    related = ["GCA_000001405.29", "GCF_000001405.40"]
    assert ncbi._select_canonical_assembly_accession(current, related, related[0]) == related[0]
    assert ncbi._select_canonical_assembly_accession({}, related, related[0]) == related[1]
    assert ncbi._select_canonical_assembly_accession({}, [related[0]], related[0]) == related[0]
    assert ncbi._assembly_asset_role("protein_fasta", related[0], related[1]) == "protein_fasta"
    assert ncbi._assembly_asset_role("genome_fasta", related[1], related[1]) == "genome_fasta"
    assert ncbi._assembly_asset_role("genome_fasta", related[1], related[0]) == "genome_fasta_refseq"
    assert ncbi._assembly_asset_role("assembly_report", related[0], related[1]) == "assembly_report_genbank"


def test_read_report_supports_json_variants_and_csv_errors():
    jsonl = io.StringIO('\n{"accession":"GCF_000001405.40"}\n7\n')
    assert ncbi._read_report_handle(jsonl, "report.jsonl") == [
        {"accession": "GCF_000001405.40"}
    ]
    with pytest.raises(ValidationError, match="invalid JSON on line 2"):
        ncbi._read_report_handle(io.StringIO('{}\n{bad}\n'), "report.jsonl")

    assert ncbi._read_report_handle(io.StringIO("  \n"), "empty.txt") == []
    assert ncbi._read_report_handle(io.StringIO('[{"x": 1}, 2]'), "report.json") == [{"x": 1}]
    assert ncbi._read_report_handle(io.StringIO('{"reports":[{"x":1},2]}'), "report.json") == [{"x": 1}]
    assert ncbi._read_report_handle(io.StringIO('{"assemblies":[{"x":2}]}'), "report.json") == [{"x": 2}]
    assert ncbi._read_report_handle(io.StringIO('{"data":[{"x":3}]}'), "report.json") == [{"x": 3}]
    assert ncbi._read_report_handle(io.StringIO('{"x":4}'), "report.json") == [{"x": 4}]
    assert ncbi._read_report_handle(io.StringIO('1'), "report.json") == []
    assert ncbi._read_report_handle(
        io.StringIO('{"x":1}\n{"x":2}\n'), "unknown.txt"
    ) == [{"x": 1}, {"x": 2}]
    with pytest.raises(ValidationError, match="invalid JSON on line 2"):
        ncbi._read_report_handle(io.StringIO('{"x":1}\nnope\n'), "unknown.txt")

    rows = ncbi._read_report_handle(
        io.StringIO("Assembly Accession,Organism Name,Tax ID\nGCF_000001405.40,Human,9606\n"),
        "report.csv",
    )
    assert rows[0]["organism"]["taxId"] == "9606"
    with pytest.raises(ValidationError, match="no metadata header"):
        ncbi._read_report_tsv(io.StringIO(""), Path("empty.csv"))


def test_biosample_and_metadata_extraction_accept_alternate_keys():
    report = {
        "current_accession": "GCF_000001405.40",
        "organism": {
            "name": "Example species",
            "tax_id": "123",
            "infraspecific_names": {"isolate": "I1", "sex": "odd"},
        },
        "assembly_info": {
            "biosample": {
                "accession": "SAMN1",
                "sampleAttributes": [
                    {"attributeName": "latitude and longitude", "attributeValue": "1 S 2 W"},
                    {"harmonizedName": "host", "value": "plant"},
                    "ignored",
                ],
            },
            "paired_assembly": {"accession": "GCA_000001405.29"},
        },
        "annotation_info": {"annotationProvider": "RefSeq", "annotationVersion": 3},
    }
    metadata = ncbi._extract_metadata(report)
    assert metadata["accession"] == "GCF_000001405.40"
    assert metadata["paired_accession"] == "GCA_000001405.29"
    assert metadata["scientific_name"] == "Example species"
    assert metadata["latitude"] == -1.0
    assert metadata["longitude"] == -2.0
    assert metadata["host"] == "plant"
    assert metadata["annotation"]["provider"] == "RefSeq"


def test_zip_validation_extraction_and_diagnostics(tmp_path: Path):
    package = tmp_path / "package.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("dir/", "")
        archive.writestr("dir/file.txt", "ok")
    destination = tmp_path / "out"
    ncbi._safe_extract_zip(package, destination)
    assert (destination / "dir" / "file.txt").read_text() == "ok"

    symlink = zipfile.ZipInfo("link")
    symlink.create_system = 3
    symlink.external_attr = 0o120777 << 16
    with pytest.raises(ValidationError, match="symbolic link"):
        ncbi._validate_zip_info(symlink)
    absolute = zipfile.ZipInfo("/absolute")
    with pytest.raises(ValidationError, match="unsafe path"):
        ncbi._validate_zip_info(absolute)

    readme_only = tmp_path / "readme.zip"
    with zipfile.ZipFile(readme_only, "w") as archive:
        archive.writestr("README.md", "none")
    retryable, detail = ncbi._zip_package_diagnostic(readme_only, ["GCF_000001405.40"])
    assert retryable is False
    assert "README-only" in detail

    truncated = tmp_path / "truncated.zip"
    payload = readme_only.read_bytes()
    # Keep the local header and data while removing the central directory.
    truncated.write_bytes(payload[: payload.find(b"PK\x01\x02")])
    assert ncbi._local_zip_entry_names(truncated) == ["README.md"]
    retryable, detail = ncbi._zip_package_diagnostic(truncated, ["GCF_000001405.40"])
    assert retryable is False
    assert "README-only" in detail

    data_header = tmp_path / "data-header.zip"
    name = b"ncbi_dataset/data/GCF_000001405.40/genomic.fna"
    data_header.write_bytes(
        struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0, 0, 0, 0, len(name), 0) + name
    )
    retryable, detail = ncbi._zip_package_diagnostic(data_header, ["GCF_000001405.40"])
    assert retryable is True
    assert "truncated" in detail
    retryable, detail = ncbi._zip_package_diagnostic(tmp_path / "missing.zip", ["GCF_000001405.40"])
    assert retryable is True
    assert "no recognizable ZIP content" in detail


def test_download_retry_and_fallback_error_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ncbi.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ncbi.random, "uniform", lambda _a, _b: 0.0)
    attempts = []

    def fail_once(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise ssl.SSLError("transient")
        return kwargs["destination"]

    monkeypatch.setattr(ncbi, "_download_ncbi_dataset_once", fail_once)
    destination = tmp_path / "result.zip"
    assert ncbi.download_ncbi_dataset(
        ["GCF_000001405.40"], destination, session=object(), max_retries=1
    ) == destination
    with pytest.raises(ValidationError, match="no NCBI assembly accessions"):
        ncbi.download_ncbi_dataset([], destination, session=object())

    def always_timeout(**_kwargs):
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(ncbi, "_download_ncbi_dataset_once", always_timeout)
    with pytest.raises(ValidationError, match="failed after 2 attempt"):
        ncbi.download_ncbi_dataset(
            ["GCF_000001405.40"], destination, session=object(), max_retries=1
        )


def test_single_download_falls_back_and_rejects_readme_package(tmp_path: Path):
    class Response:
        headers = {"Content-Length": "not-an-int"}

        def __init__(self, status, payload=b""):
            self.status_code = status
            self.payload = payload
            self.closed = False

        def close(self):
            self.closed = True

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

        def iter_content(self, chunk_size):
            assert chunk_size > 0
            yield b""
            yield self.payload

    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w") as archive:
        archive.writestr("README.md", "nothing available")
    primary = Response(404)
    payload = memory.getvalue()
    fallback = Response(200, payload[: payload.find(b"PK\x01\x02")])

    class Session:
        def __init__(self):
            self.responses = iter([primary, fallback])

        def get(self, *_args, **_kwargs):
            return next(self.responses)

    with pytest.raises(ValidationError, match="README-only"):
        ncbi._download_ncbi_dataset_once(
            canonical=["GCF_000001405.40"],
            destination=tmp_path / "dataset.zip",
            includes=["genome"],
            email=None,
            api_key="secret",
            timeout=1,
            session=Session(),
        )
    assert primary.closed and fallback.closed


def test_identity_helpers_are_deterministic():
    identity = ncbi._annotation_identity(
        "ASM_1", "GCF_000001405.40", " RefSeq ", 3, "2024-01-01"
    )
    assert identity == ncbi._annotation_identity(
        "ASM_1", "GCF_000001405.40", "refseq", 3, "2024-01-01"
    )
    assert ncbi._metadata_identity({"x": 1}, "GCF_000001405.40") == ncbi._metadata_identity(
        {"x": 1}, "GCF_000001405.40"
    )


def test_report_loading_and_asset_discovery_from_zip_and_directory(tmp_path):
    accession = "GCF_000001405.40"
    report = {"accession": accession, "organism": {"organismName": "Example"}}
    package = tmp_path / "dataset.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("data/assembly_data_report.jsonl", json.dumps(report) + "\n")
        archive.writestr(f"data/{accession}/genomic.fna", ">x\nA\n")
        archive.writestr(f"data/{accession}/ignored.txt", "x")
        archive.writestr("data/no-accession/protein.faa", ">p\nM\n")
    reports = ncbi.load_dataset_reports(package)
    assert len(reports) == 1
    assets = ncbi.discover_dataset_assets(package, reports, "zip")
    assert {asset.role for asset in assets} == {"genome_fasta", "protein_fasta"}
    assert all(asset.path is None and asset.archive_path == package for asset in assets)
    assert all(ncbi._asset_sha256(asset) for asset in assets)

    directory = tmp_path / "directory"
    directory.mkdir()
    direct = directory / "dataset_report.jsonl"
    direct.write_text(json.dumps(report) + "\n", encoding="utf-8")
    genome = directory / "genomic.fna"
    genome.write_text(">x\nA\n", encoding="utf-8")
    assert ncbi.load_dataset_reports(directory, direct_file=package) == reports
    directory_assets = ncbi.discover_dataset_assets(directory, reports, "dir")
    assert len(directory_assets) == 1 and directory_assets[0].accession == accession
    assert ncbi.discover_dataset_assets(tmp_path / "missing", reports, "x") == []

    fallback = tmp_path / "fallback.zip"
    with zipfile.ZipFile(fallback, "w") as archive:
        archive.writestr("unusual.jsonl", json.dumps(report) + "\n")
        archive.writestr("sequence_report.jsonl", "{}\n")
    assert len(ncbi.load_dataset_reports(fallback)) == 1


def test_asset_sha_rejects_missing_sources_and_members(tmp_path):
    missing = ncbi.DatasetAsset(
        path=None, accession="GCF_000001405.40", role="genome_fasta", source_url="x"
    )
    with pytest.raises(ValidationError, match="no readable source"):
        ncbi._asset_sha256(missing)
    package = tmp_path / "package.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("x", "x")
    missing.archive_path = package
    missing.archive_member = "missing"
    with pytest.raises(ValidationError, match="cannot read NCBI ZIP asset"):
        ncbi._asset_sha256(missing)


def test_disk_space_formatting_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(ncbi.shutil, "disk_usage", lambda _p: type("Usage", (), {"free": 10})())
    with pytest.raises(ValidationError, match="insufficient space"):
        ncbi._require_disk_space(tmp_path / "not-created", 100, "download")
    assert ncbi._format_bytes(0) == "0.0 B"
    assert ncbi._format_bytes(1024) == "1.0 KiB"
    assert ncbi._format_bytes(1024 ** 2) == "1.0 MiB"
    assert ncbi._format_bytes(1024 ** 3) == "1.0 GiB"
    assert ncbi._format_bytes(1024 ** 4) == "1.0 TiB"
    error = ncbi._no_space_error(tmp_path, "write", OSError("full"))
    assert isinstance(error, ValidationError) and "ran out of space" in str(error)


def test_open_and_preserve_sources_are_idempotent(tmp_path, monkeypatch):
    project = type("Project", (), {"raw_root": tmp_path / "raw"})()
    with pytest.raises(ValidationError, match="input does not exist"):
        ncbi._open_source(tmp_path / "missing", project, False)
    directory = tmp_path / "directory"
    directory.mkdir()
    bundle = ncbi._open_source(directory, project, True, "label")
    assert bundle.root == directory.resolve() and bundle.label == "label"
    source = tmp_path / "source.jsonl"
    source.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(ncbi, "_require_disk_space", lambda *_a: None)
    preserved = ncbi._preserve_source(source, project)
    assert preserved.is_file()
    assert ncbi._preserve_source(source, project) == preserved
    moved = tmp_path / "moved.jsonl"
    moved.write_text("{}\n", encoding="utf-8")
    assert ncbi._preserve_source(moved, project, move=True) == preserved
    assert not moved.exists()
    preserved.write_text("tampered", encoding="utf-8")
    with pytest.raises(Exception, match="unexpected content"):
        ncbi._preserve_source(source, project)


def test_preflight_assets_deduplicates_and_detects_internal_conflict(tmp_path):
    accession = "GCF_000001405.40"
    first = tmp_path / "first.fna"
    same = tmp_path / "same.fna"
    different = tmp_path / "different.fna"
    first.write_text(">x\nA\n", encoding="utf-8")
    same.write_bytes(first.read_bytes())
    different.write_text(">x\nC\n", encoding="utf-8")
    make = lambda path: ncbi.DatasetAsset(
        path=path, accession=accession, role="genome_fasta", source_url="x"
    )
    plan = ncbi.ImportPlan(
        tables={}, assets=[make(first), make(same)], assembly_ids={accession: "ASM_1"},
        annotation_ids={}, assembly_records=[], annotation_records=[],
    )

    class DB:
        class Conn:
            @staticmethod
            def execute(*_a):
                return type("Cursor", (), {"fetchone": lambda self: None})()
        conn = Conn()

    ncbi._preflight_assets(DB(), plan)
    assert len(plan.assets) == 1
    plan.assets = [make(first), make(different)]
    with pytest.raises(Exception, match="multiple different files"):
        ncbi._preflight_assets(DB(), plan)


def test_download_batches_async_success_error_and_cancellation(tmp_path, monkeypatch):
    async def fake_download(batch, destination, **kwargs):
        if batch[0].endswith("2"):
            raise ValidationError("bad")
        destination.write_bytes(b"zip")
        return destination

    monkeypatch.setattr(ncbi, "_download_batch_aiohttp", fake_download)
    completed = queue.Queue()
    cancel = threading.Event()
    asyncio.run(ncbi._download_batches_async(
        batches=[["GCF_000000001.1"], ["GCF_000000002.2"]],
        staging_dir=tmp_path, includes=["genome"], email=None, api_key=None,
        timeout=1, max_workers=2, max_retries=0, retry_backoff=0,
        completed_queue=completed, cancel_event=cancel,
    ))
    items = [completed.get_nowait(), completed.get_nowait()]
    assert sum(item[1] is not None for item in items) == 1
    assert sum(item[2] is not None for item in items) == 1

    cancel.set()
    completed = queue.Queue()
    asyncio.run(ncbi._download_batches_async(
        batches=[["GCF_000000001.1"]], staging_dir=tmp_path, includes=["genome"],
        email=None, api_key=None, timeout=1, max_workers=1, max_retries=0,
        retry_backoff=0, completed_queue=completed, cancel_event=cancel,
    ))
    assert completed.empty()


def test_parallel_download_wrapper_aggregates_or_reports_errors(tmp_path, monkeypatch):
    async def fake_runner(**kwargs):
        kwargs["completed_queue"].put((kwargs["batches"][0], None, ValidationError("bad")))

    monkeypatch.setattr(ncbi, "_download_batches_async", fake_runner)
    with pytest.raises(ValidationError, match="download batch"):
        ncbi.download_ncbi_datasets_parallel(
            [["GCF_000000001.1"]], tmp_path, max_workers=1, on_complete=lambda *_a: None
        )
    errors = []
    assert ncbi.download_ncbi_datasets_parallel(
        [["GCF_000000001.1"]], tmp_path, max_workers=1,
        on_complete=lambda *_a: None, on_error=lambda batch, error: errors.append((batch, error)),
    ) == []
    assert len(errors) == 1

    async def crashed(**_kwargs):
        raise RuntimeError("runner")

    monkeypatch.setattr(ncbi, "_download_batches_async", crashed)
    with pytest.raises(RuntimeError, match="runner"):
        ncbi.download_ncbi_datasets_parallel(
            [["GCF_000000001.1"]], tmp_path, max_workers=1, on_complete=lambda *_a: None
        )


def test_interruptible_retry_sleep_cancel_and_completion(monkeypatch):
    event = threading.Event()
    monkeypatch.setattr(asyncio, "sleep", lambda *_a: asyncio.sleep(0))
    # Avoid monkeypatching asyncio.sleep recursively by using a small custom awaitable.
    async def no_sleep(_seconds):
        return None
    monkeypatch.setattr(ncbi.asyncio, "sleep", no_sleep)
    asyncio.run(ncbi._interruptible_retry_sleep(0.3, event))
    event.set()
    with pytest.raises(ncbi._DownloadCancelled):
        asyncio.run(ncbi._interruptible_retry_sleep(0.3, event))


class _FakeHTTPError(Exception):
    def __init__(self, status=0, message="error"):
        super().__init__(message)
        self.status = status


class _FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, status, chunks=(), headers=None):
        self.status = status
        self.content = _FakeContent(chunks)
        self.headers = headers or {}
        self.released = False

    def release(self):
        self.released = True

    def raise_for_status(self):
        if self.status >= 400:
            raise _FakeHTTPError(self.status, f"HTTP {self.status}")


def _fake_aiohttp(monkeypatch, outcomes, captured=None):
    class RetryableConnectionError(Exception):
        pass

    class Session:
        def __init__(self, **kwargs):
            if captured is not None:
                captured.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    fake = type("FakeAiohttp", (), {
        "ClientSession": Session,
        "ClientTimeout": lambda **kwargs: kwargs,
        "ClientSSLError": RetryableConnectionError,
        "ClientConnectionError": RetryableConnectionError,
        "ServerDisconnectedError": RetryableConnectionError,
        "ClientPayloadError": RetryableConnectionError,
        "ClientResponseError": _FakeHTTPError,
    })
    monkeypatch.setitem(sys.modules, "aiohttp", fake)
    return fake


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ncbi_dataset/data/dataset_catalog.json", "{}")
    return buffer.getvalue()


def _download_kwargs(destination, cancel=None, max_retries=0):
    return dict(
        accessions=["GCF_000000001.1"], destination=destination,
        includes=["genome", "protein"], email="test@example.org", api_key="key",
        timeout=1, max_retries=max_retries, retry_backoff=0,
        cancel_event=cancel or threading.Event(),
    )


def test_aiohttp_download_success_headers_content_length_and_fallback(tmp_path, monkeypatch):
    data = _zip_bytes()
    captured = []
    first = _FakeResponse(404)
    second = _FakeResponse(200, [data[:10], data[10:]], {"Content-Length": str(len(data))})
    _fake_aiohttp(monkeypatch, [first, second], captured)
    disk_checks = []
    monkeypatch.setattr(ncbi, "_require_disk_space", lambda *args: disk_checks.append(args))
    destination = tmp_path / "package.zip"
    result = asyncio.run(ncbi._download_batch_aiohttp(**_download_kwargs(destination)))
    assert result == destination and zipfile.is_zipfile(destination)
    assert first.released and second.released and disk_checks
    assert captured[0]["headers"]["api-key"] == "key"
    assert "test@example.org" in captured[0]["headers"]["User-Agent"]


def test_aiohttp_download_retries_transient_status_and_connection(tmp_path, monkeypatch):
    data = _zip_bytes()
    _fake_aiohttp(monkeypatch, [
        _FakeResponse(503), _FakeResponse(503),
        _FakeResponse(200, [data], {"Content-Length": "bad"}),
    ])
    async def no_sleep(*_args):
        return None
    monkeypatch.setattr(ncbi, "_interruptible_retry_sleep", no_sleep)
    destination = tmp_path / "retry.zip"
    assert asyncio.run(ncbi._download_batch_aiohttp(
        **_download_kwargs(destination, max_retries=1)
    )) == destination


def test_aiohttp_download_validation_retry_exhaustion_and_http_error(tmp_path, monkeypatch):
    _fake_aiohttp(monkeypatch, [_FakeResponse(200, [b"temporary gateway failure"])])
    with pytest.raises(ValidationError, match="after 1 attempt"):
        asyncio.run(ncbi._download_batch_aiohttp(
            **_download_kwargs(tmp_path / "invalid.zip")
        ))

    _fake_aiohttp(monkeypatch, [_FakeResponse(400)])
    with pytest.raises(ValidationError, match="download failed"):
        asyncio.run(ncbi._download_batch_aiohttp(
            **_download_kwargs(tmp_path / "bad-request.zip")
        ))

    with pytest.raises(ValidationError, match="no NCBI assembly accessions"):
        kwargs = _download_kwargs(tmp_path / "empty.zip")
        kwargs["accessions"] = []
        asyncio.run(ncbi._download_batch_aiohttp(**kwargs))


def test_aiohttp_download_cancellation_during_transfer_and_no_space(tmp_path, monkeypatch):
    event = threading.Event()
    event.set()
    _fake_aiohttp(monkeypatch, [])
    with pytest.raises(ncbi._DownloadCancelled):
        asyncio.run(ncbi._download_batch_aiohttp(
            **_download_kwargs(tmp_path / "cancelled.zip", cancel=event)
        ))

    class LaterCancel:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls >= 2

    _fake_aiohttp(monkeypatch, [_FakeResponse(200, [_zip_bytes()])])
    with pytest.raises(ncbi._DownloadCancelled):
        asyncio.run(ncbi._download_batch_aiohttp(
            **_download_kwargs(tmp_path / "midstream.zip", cancel=LaterCancel())
        ))

    _fake_aiohttp(monkeypatch, [_FakeResponse(200, [_zip_bytes()])])
    def no_space(_fd):
        error = OSError("full")
        error.errno = 28
        raise error
    monkeypatch.setattr(ncbi.os, "fsync", no_space)
    with pytest.raises(ValidationError, match="ran out of space"):
        asyncio.run(ncbi._download_batch_aiohttp(
            **_download_kwargs(tmp_path / "full.zip")
        ))


def _assembly_report(accession="GCF_000000001.1", **overrides):
    report = {
        "accession": accession,
        "organism": {
            "organismName": "Example species", "taxId": 123,
            "infraspecificNames": {"strain": "S1"},
        },
        "assemblyInfo": {
            "assemblyName": "Example", "assemblyLevel": "Complete Genome",
            "biosample": {"accession": "SAMN000001"},
            "pairedAssembly": {"accession": "GCA_000000001.1"},
            "releaseDate": "2025-01-02", "submitter": "Submitter",
        },
        "annotationInfo": {
            "annotationProvider": "RefSeq", "annotationVersion": 2,
            "releaseDate": "2025-02-03",
        },
    }
    report.update(overrides)
    return report


def test_plan_builder_full_metadata_assets_and_reuse(project_db, tmp_path):
    _project, db = project_db
    builder = ncbi._PlanBuilder(db)
    assets = [
        ncbi.DatasetAsset(tmp_path / "genome", "GCF_000000001", "genome_fasta"),
        ncbi.DatasetAsset(tmp_path / "gff", "GCF_000000001.1", "annotation_gff3"),
        ncbi.DatasetAsset(tmp_path / "ignored", "GCF_999999999.1", "genome_fasta"),
    ]
    plan = builder.build([_assembly_report()], assets)
    assert plan.new_ids == {"organism": 1, "sample": 1, "assembly": 1, "annotation": 1}
    assert len(plan.assets) == 2
    assert plan.assets[0].role.startswith("genome_fasta")
    assert plan.annotation_ids["GCF_000000001.1"].startswith("ANN_")
    assert plan.record_count > 0

    # Persist only the identity rows needed to exercise accession-based reuse.
    organism = plan.tables["organisms"][0]
    sample = plan.tables["samples"][0]
    assembly = plan.tables["assemblies"][0]
    annotation = plan.tables["annotations"][0]
    for table, row in (("organisms", organism), ("samples", sample),
                       ("assemblies", assembly), ("annotations", annotation)):
        db.insert_row(table, row)
    for row in plan.tables["accessions"]:
        db.insert_row("accessions", row)
    reused = ncbi._PlanBuilder(db).build([_assembly_report()], [])
    assert reused.new_ids == {"organism": 0, "sample": 0, "assembly": 0, "annotation": 0}


def test_plan_builder_validation_conflicting_accessions_and_fallback_matching(project_db):
    _project, db = project_db
    with pytest.raises(ValidationError, match="no assembly accession"):
        ncbi._PlanBuilder(db)._add_record({"organism": {"organismName": "X"}})
    with pytest.raises(ValidationError, match="neither organism name nor taxon ID"):
        report = _assembly_report()
        report["organism"] = {}
        ncbi._PlanBuilder(db).build([report], [])

    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "A"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("assemblies", {
        "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
        "assembly_accession": "GCA_000000001.1", "assembly_version": 1,
    })
    db.insert_row("assemblies", {
        "assembly_id": "ASM_000002", "sample_id": "SMP_000001",
        "assembly_accession": "GCF_000000001.1", "assembly_version": 1,
    })
    db.insert_row("accessions", {
        "internal_type": "assembly", "internal_id": "ASM_000001",
        "namespace": "NCBI_GenBank_Assembly", "accession": "GCA_000000001.1",
    })
    db.insert_row("accessions", {
        "internal_type": "assembly", "internal_id": "ASM_000002",
        "namespace": "NCBI_RefSeq_Assembly", "accession": "GCF_000000001.1",
    })
    with pytest.raises(ConflictError, match="map to different assemblies"):
        ncbi._PlanBuilder(db).build([_assembly_report()], [])

    builder = ncbi._PlanBuilder(db)
    assert builder._find_assembly(None) is None
    assert builder._find_assembly("GCF_000000001.1") == "ASM_000002"
    builder.rows["accessions"]["X\0A"] = {
        "internal_type": "sample", "internal_id": "SMP_000001",
    }
    with pytest.raises(ConflictError, match="already maps"):
        builder._put_accession("assembly", "ASM_000001", "X", "A", 1, True)


def test_plan_builder_organism_sample_and_annotation_lookup_fallbacks(project_db):
    _project, db = project_db
    db.insert_row("organisms", {
        "organism_id": "ORG_000001", "scientific_name": "Example species", "taxon_id": 123,
    })
    db.insert_row("samples", {
        "sample_id": "SMP_000001", "organism_id": "ORG_000001",
        "biosample_accession": "SAMN000001",
    })
    db.insert_row("assemblies", {
        "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
        "assembly_accession": "GCF_000000001.1", "assembly_version": 1,
    })
    db.insert_row("annotations", {
        "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
        "annotation_source": "NCBI RefSeq", "annotation_version": 1,
    })
    builder = ncbi._PlanBuilder(db)
    assert builder._ensure_organism({"taxon_id": None, "scientific_name": "example species"}) == "ORG_000001"
    assert builder._ensure_organism({"taxon_id": 123, "scientific_name": "Changed"}) == "ORG_000001"
    assert builder._ensure_sample({
        "biosample_accession": "SAMN000001", "accession": "GCF_000000001.1"
    }, "ORG_000001", "ASM_000001") == "SMP_000001"
    assert builder._ensure_sample({
        "biosample_accession": "", "accession": "GCF_000000001.1"
    }, "ORG_000001", "ASM_000001") == "SMP_000001"
    builder.plan.canonical_accessions["ASM_000001"] = "GCF_000000001.1"
    annotation_id = builder._ensure_annotation("ASM_000001", "GCF_000000001.1", {})
    assert annotation_id == "ANN_000001"
    assert builder._ensure_annotation("ASM_000001", "GCF_000000001.1", {}) == annotation_id


def test_entrez_fallback_validation_empty_and_complete_records(monkeypatch):
    with pytest.raises(ValidationError, match="requires --email"):
        ncbi.fetch_entrez_assembly_reports(["GCF_000000001.1"], email=None)

    class Handle:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    searches = iter([
        {"IdList": []}, {"IdList": ["2"]}, {"IdList": ["3"]}, {"IdList": ["4"]},
    ])
    summaries = iter([
        {"DocumentSummarySet": {"DocumentSummary": []}},
        {"DocumentSummarySet": {"DocumentSummary": [{
            "AssemblyAccession": "GCF_000000003.1", "SpeciesName": "Species",
            "Taxid": "3", "AssemblyStatus": "Complete Genome", "AssemblyName": "A",
            "BioSampleAccn": "SAMN3", "BioProjectAccn": "PRJ3",
            "Synonym": {"Genbank": "GCA_000000003.1"}, "RefSeq_category": "reference genome",
            "SubmissionDate": "2025-01-01", "SubmitterOrganization": "Org",
        }]}},
        {"DocumentSummarySet": {"DocumentSummary": [{
            "AssemblyAccession": "GCA_000000004.1", "SpeciesName": "Other",
            "Taxid": "bad", "Synonym": None,
        }]}},
    ])
    fake = type("Entrez", (), {})
    fake.esearch = lambda **_kwargs: Handle(next(searches))
    fake.esummary = lambda **_kwargs: Handle(next(summaries))
    fake.read = lambda handle, **_kwargs: handle.value
    monkeypatch.setitem(sys.modules, "Bio", type("Bio", (), {"Entrez": fake}))
    reports = ncbi.fetch_entrez_assembly_reports(
        ["GCF_000000001.1", "GCF_000000002.1", "GCF_000000003.1", "GCA_000000004.1"],
        email="test@example.org", api_key="key",
    )
    assert [row["accession"] for row in reports] == ["GCF_000000003.1", "GCA_000000004.1"]
    assert reports[0]["sourceDatabase"] == "SOURCE_DATABASE_REFSEQ"
    assert reports[1]["sourceDatabase"] == "SOURCE_DATABASE_GENBANK"
