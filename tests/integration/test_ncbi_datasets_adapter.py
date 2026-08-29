"""NCBI Datasets adapter tests (offline paths do not require network)."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import threading
import time
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

from tests.helpers import PytestAssertions
from unittest.mock import patch

import requests
import yaml

from operon.adapters.ncbi_datasets import (
    _DownloadCancelled,
    _download_batch_aiohttp,
    _zip_package_diagnostic,
    download_ncbi_datasets_parallel,
    _read_report_file,
    _require_disk_space,
    _safe_extract_zip,
    download_ncbi_dataset,
    run_ncbi_datasets_adapter,
)
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ValidationError


def _report(accession: str = "GCF_000001405.40") -> dict:
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
                    {"name": "lat_lon", "value": "38.9 N 77.0 W"},
                ],
            },
            "bioprojectAccession": "PRJNA31257",
            "pairedAssembly": {"accession": "GCA_000001405.29"},
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


class TestNCBIDatasetsAdapter(PytestAssertions):
    def _init(self, root: Path) -> None:
        self.assertEqual(main(["--project", str(root), "init", str(root)]), 0)

    def _make_schema_legacy(self, root: Path) -> str:
        path = root / "config" / "schemas.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        document["schema_version"] = "1.0"
        fields = document["tables"]["assemblies"]["fields"]
        for name in (
            "assembly_name", "bioproject_accession", "source_database",
            "assembly_status", "assembly_type",
        ):
            fields.pop(name, None)
        text = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        path.write_text(text, encoding="utf-8")
        return text

    def test_import_jsonl_is_idempotent_and_maps_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init(root)
            self._make_schema_legacy(root)
            report_path = root / "assembly_data_report.jsonl"
            report_path.write_text(json.dumps(_report()) + "\n", encoding="utf-8")

            command = ["--project", str(root), "ncbi-datasets", "--input", str(report_path)]
            self.assertEqual(main(command), 0)
            self.assertEqual(main(command), 0)

            db = Database(root / "operon.sqlite")
            try:
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM organisms")[0]["n"], 1)
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM samples")[0]["n"], 1)
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM assemblies")[0]["n"], 1)
                assembly = dict(db.query("SELECT * FROM assemblies")[0])
                self.assertEqual(assembly["assembly_accession"], "GCF_000001405.40")
                self.assertEqual(assembly["assembly_version"], 40)
                self.assertEqual(assembly["assembly_level"], "chromosome")
                self.assertEqual(assembly["reference_status"], "reference")
                sample = dict(db.query("SELECT * FROM samples")[0])
                self.assertEqual(sample["biosample_accession"], "SAMN00000001")
                self.assertAlmostEqual(sample["latitude"], 38.9)
                self.assertAlmostEqual(sample["longitude"], -77.0)
                aliases = {
                    (row["namespace"], row["accession"], row["internal_id"])
                    for row in db.query("SELECT * FROM accessions")
                }
                self.assertIn(("NCBI_Assembly", "GCF_000001405.40", assembly["assembly_id"]), aliases)
                self.assertIn(("NCBI_GenBank_Assembly", "GCA_000001405.29", assembly["assembly_id"]), aliases)
                self.assertEqual(assembly["bioproject_accession"], "PRJNA31257")
            finally:
                db.close()
            self.assertEqual(main(["--project", str(root), "report", "metadata"]), 0)
            self.assertIn("GCF_000001405.40", (root / "reports" / "metadata" / "assemblies.tsv").read_text())
            upgraded_schema = yaml.safe_load((root / "config" / "schemas.yaml").read_text())
            self.assertEqual(upgraded_schema["schema_version"], "1.1")
            self.assertIn("bioproject_accession", upgraded_schema["tables"]["assemblies"]["fields"])
            self.assertTrue(any((root / "raw" / "metadata" / "ncbi_datasets").iterdir()))

    def test_import_zip_archives_dataset_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init(root)
            package = root / "ncbi_dataset.zip"
            accession = "GCF_000001405.40"
            prefix = f"ncbi_dataset/data/{accession}"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("ncbi_dataset/data/assembly_data_report.jsonl", json.dumps(_report()) + "\n")
                archive.writestr(f"{prefix}/genomic.fna", ">chr1\nACGTACGT\n")
                archive.writestr(f"{prefix}/genomic.gff", "##gff-version 3\nchr1\tNCBI\tgene\t1\t4\t.\t+\t.\tID=g1\n")
                archive.writestr(f"{prefix}/protein.faa", ">p1\nMK\n")
                archive.writestr(f"{prefix}/cds_from_genomic.fna", ">cds1\nATGAAA\n")
                archive.writestr(f"{prefix}/sequence_report.jsonl", '{"sequence_name":"chr1"}\n')

            command = ["--project", str(root), "ncbi-datasets", "--input", str(package)]
            self.assertEqual(main(command), 0)
            self.assertEqual(main(command), 0)
            db = Database(root / "operon.sqlite")
            try:
                files = [dict(row) for row in db.query("SELECT * FROM files ORDER BY file_role")]
                self.assertEqual(len(files), 5)
                self.assertEqual(
                    {row["file_role"] for row in files},
                    {"genome_fasta", "annotation_gff3", "protein_fasta", "cds_fasta", "assembly_report"},
                )
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM annotations")[0]["n"], 1)
                assembly = dict(db.query("SELECT * FROM assemblies")[0])
                annotation = dict(db.query("SELECT * FROM annotations")[0])
                self.assertIsNotNone(assembly["fasta_file_id"])
                self.assertIsNotNone(annotation["gff_file_id"])
                self.assertIsNotNone(annotation["protein_file_id"])
                self.assertIsNotNone(annotation["cds_file_id"])
                for row in files:
                    self.assertTrue((root / row["relative_path"]).exists())
            finally:
                db.close()

    def test_dry_run_does_not_change_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init(root)
            legacy_schema = self._make_schema_legacy(root)
            report_path = root / "report.jsonl"
            report_path.write_text(json.dumps(_report()) + "\n", encoding="utf-8")
            self.assertEqual(main([
                "--project", str(root), "ncbi-datasets", "--input", str(report_path), "--dry-run"
            ]), 0)
            db = Database(root / "operon.sqlite")
            try:
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM assemblies")[0]["n"], 0)
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM changes")[0]["n"], 0)
            finally:
                db.close()
            self.assertFalse((root / "raw" / "metadata").exists())
            self.assertEqual((root / "config" / "schemas.yaml").read_text(), legacy_schema)

    def test_zip_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "bad.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("../../outside.txt", "bad")
            with self.assertRaises(ValidationError):
                _safe_extract_zip(package, root / "unpack")

    def test_new_accession_version_creates_a_new_immutable_assembly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init(root)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            previous = _report("GCF_000001405.39")
            previous["assemblyInfo"]["pairedAssembly"] = {"accession": "GCA_000001405.28"}
            first.write_text(json.dumps(previous) + "\n", encoding="utf-8")
            updated = _report("GCF_000001405.40")
            updated["assemblyInfo"]["pairedAssembly"] = {"accession": "GCA_000001405.29"}
            second.write_text(json.dumps(updated) + "\n", encoding="utf-8")
            self.assertEqual(main(["--project", str(root), "ncbi-datasets", "--input", str(first)]), 0)
            self.assertEqual(main(["--project", str(root), "ncbi-datasets", "--input", str(second)]), 0)
            db = Database(root / "operon.sqlite")
            try:
                assemblies = [dict(row) for row in db.query(
                    "SELECT assembly_id, assembly_accession, assembly_version, sample_id "
                    "FROM assemblies ORDER BY assembly_version"
                )]
                self.assertEqual(len(assemblies), 2)
                self.assertEqual([row["assembly_version"] for row in assemblies], [39, 40])
                self.assertNotEqual(assemblies[0]["assembly_id"], assemblies[1]["assembly_id"])
                self.assertEqual(assemblies[0]["sample_id"], assemblies[1]["sample_id"])
            finally:
                db.close()

    def test_download_streams_and_validates_zip(self):
        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                yield self.payload

            def close(self):
                return None

        class FakeSession:
            def __init__(self, payload):
                self.response = FakeResponse()
                self.response.payload = payload
                self.calls = []

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return self.response

        memory = io.BytesIO()
        with zipfile.ZipFile(memory, "w") as archive:
            archive.writestr("README.md", "ok")
        memory.seek(0)
        session = FakeSession(memory.read())
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "dataset.zip"
            result = download_ncbi_dataset(
                ["GCF_000001405.40"], destination, includes=["genome"], session=session
            )
            self.assertEqual(result, destination)
            self.assertTrue(zipfile.is_zipfile(destination))
            self.assertIn("/datasets/v2/genome/accession/GCF_000001405.40/download", session.calls[0][0])
            self.assertEqual(
                session.calls[0][1]["params"],
                [("include_annotation_type", "GENOME_FASTA")],
            )

    def test_zip_import_does_not_fully_extract_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init(root)
            package = root / "ncbi_dataset.zip"
            accession = "GCF_000001405.40"
            prefix = f"ncbi_dataset/data/{accession}"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(
                    "ncbi_dataset/data/assembly_data_report.jsonl",
                    json.dumps(_report()) + "\n",
                )
                archive.writestr(f"{prefix}/genomic.gff", "##gff-version 3\n")
                archive.writestr(f"{prefix}/protein.faa", ">p1\nMK\n")

            with patch(
                "operon.adapters.ncbi_datasets._safe_extract_zip",
                side_effect=AssertionError("whole-package extraction must not be used"),
            ):
                self.assertEqual(
                    main(["--project", str(root), "ncbi-datasets", "--input", str(package)]),
                    0,
                )

    def test_jsonl_report_is_read_line_by_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "assembly_data_report.jsonl"
            path.write_text(json.dumps(_report()) + "\n", encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=AssertionError("read_text is not streaming")):
                records = _read_report_file(path)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["accession"], "GCF_000001405.40")

    def test_disk_preflight_reports_actionable_error(self):
        usage = type("DiskUsage", (), {"free": 1024})()
        with patch("operon.adapters.ncbi_datasets.shutil.disk_usage", return_value=usage):
            with self.assertRaisesRegex(
                ValidationError,
                r"insufficient space.*--batch-size.*--no-preserve-source",
            ):
                _require_disk_space(Path("/tmp"), 1024 * 1024, "archive test asset")

    def test_three_hundred_accessions_are_processed_in_bounded_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(io.StringIO()):
                self._init(root)
            project = load_project(root)
            accessions = [f"GCF_{number:09d}.1" for number in range(1, 301)]
            download_dirs: list[Path] = []

            def fake_download(batch, destination, **kwargs):
                del kwargs
                for previous in download_dirs:
                    self.assertFalse(
                        previous.exists(),
                        f"download staging directory survived into the next batch: {previous}",
                    )
                asset_staging = project.raw_root / ".ncbi_datasets_staging"
                if asset_staging.exists():
                    self.assertEqual(list(asset_staging.glob("asset-*")), [])
                destination = Path(destination)
                download_dirs.append(destination.parent)
                reports = []
                for accession in batch:
                    report = _report(accession)
                    report["assemblyInfo"]["pairedAssembly"] = {}
                    report["annotationInfo"] = {}
                    reports.append(report)
                with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr(
                        "ncbi_dataset/data/assembly_data_report.jsonl",
                        "".join(json.dumps(report) + "\n" for report in reports),
                    )
                    for accession in batch:
                        prefix = f"ncbi_dataset/data/{accession}"
                        archive.writestr(f"{prefix}/genomic.gff", "##gff-version 3\n")
                        archive.writestr(f"{prefix}/protein.faa", ">p1\nMK\n")
                        archive.writestr(f"{prefix}/cds_from_genomic.fna", ">cds1\nATGAAA\n")
                return destination

            def fake_parallel_download(batches, staging_dir, **kwargs):
                del staging_dir
                on_complete = kwargs["on_complete"]
                completed = []
                for batch in batches:
                    with tempfile.TemporaryDirectory(
                        prefix=".operon-ncbi-download-", dir=str(project.root)
                    ) as temp_name:
                        destination = Path(temp_name) / "ncbi_dataset.zip"
                        fake_download(batch, destination)
                        on_complete(batch, destination)
                        completed.append(destination)
                return completed

            db = Database(project.db_path)
            try:
                with patch(
                    "operon.adapters.ncbi_datasets.download_ncbi_datasets_parallel",
                    side_effect=fake_parallel_download,
                ):
                    result = run_ncbi_datasets_adapter(
                        db,
                        project,
                        accessions=accessions,
                    )
                self.assertEqual(result["assembly_records"], 300)
                self.assertEqual(len(result["sources"]), 30)
                self.assertEqual(len(download_dirs), 30)
                self.assertEqual(result["discovered_files"], 900)
                self.assertEqual(len(result["archived_files"]), 900)
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM assemblies")[0]["n"], 300)
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM annotations")[0]["n"], 300)
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM files")[0]["n"], 900)
                self.assertEqual(db.query("SELECT COUNT(*) AS n FROM organisms")[0]["n"], 1)
            finally:
                db.close()
            for directory in download_dirs:
                self.assertFalse(directory.exists())
            asset_staging = project.raw_root / ".ncbi_datasets_staging"
            if asset_staging.exists():
                self.assertEqual(list(asset_staging.iterdir()), [])


    def test_download_retries_ssl_record_layer_failure(self):
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, "w") as archive:
            archive.writestr("README.md", "ok")
        memory.seek(0)
        payload = memory.read()

        class FakeResponse:
            status_code = 200
            headers = {"Content-Length": str(len(payload))}

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                yield payload

            def close(self):
                return None

        class FlakySSLSession:
            def __init__(self):
                self.calls = 0

            def get(self, url, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise requests.exceptions.SSLError("[SSL] record layer failure (_ssl.c:2658)")
                return FakeResponse()

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "dataset.zip"
            session = FlakySSLSession()
            result = download_ncbi_dataset(
                ["GCF_000001405.40"],
                destination,
                includes=["genome"],
                session=session,
                max_retries=2,
                retry_backoff=0.0,
            )
            self.assertEqual(result, destination)
            self.assertTrue(zipfile.is_zipfile(destination))
            self.assertEqual(session.calls, 2)

    def test_parallel_download_respects_worker_limit(self):
        async def fake_batch_download(batch, destination, **kwargs):
            cancel_event = kwargs.get("cancel_event")
            self.assertFalse(cancel_event.is_set())
            async with lock:
                active[0] += 1
                max_active[0] = max(max_active[0], active[0])
            await asyncio.sleep(0.03)
            Path(destination).write_text("staged", encoding="utf-8")
            async with lock:
                active[0] -= 1
            return Path(destination)

        active = [0]
        max_active = [0]
        lock = asyncio.Lock()
        consumed: list[tuple[int, Path]] = []

        def consume(batch, path):
            consumed.append((len(batch), path))
            path.unlink(missing_ok=True)

        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            with patch(
                "operon.adapters.ncbi_datasets._download_batch_aiohttp",
                side_effect=fake_batch_download,
            ):
                completed = download_ncbi_datasets_parallel(
                    [["GCF_000000001.1"], ["GCF_000000002.1"], ["GCF_000000003.1"],
                     ["GCF_000000004.1"], ["GCF_000000005.1"], ["GCF_000000006.1"]],
                    staging,
                    max_workers=2,
                    max_retries=0,
                    retry_backoff=0.0,
                    on_complete=consume,
                )
        self.assertEqual(len(completed), 6)
        self.assertEqual(len(consumed), 6)
        self.assertLessEqual(max_active[0], 2)
        self.assertEqual(max_active[0], 2)

    def test_parallel_download_isolates_failed_batches(self):
        async def fake_batch_download(batch, destination, **kwargs):
            if "BAD" in batch[0]:
                raise ValidationError("synthetic invalid accession")
            Path(destination).write_text("staged", encoding="utf-8")
            return Path(destination)

        consumed: list[list[str]] = []
        failures: list[tuple[list[str], Exception]] = []

        def consume(batch, path):
            consumed.append(list(batch))
            path.unlink(missing_ok=True)

        def record_error(batch, error):
            failures.append((list(batch), error))

        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            with patch(
                "operon.adapters.ncbi_datasets._download_batch_aiohttp",
                side_effect=fake_batch_download,
            ):
                completed = download_ncbi_datasets_parallel(
                    [["GCF_000000001.1"], ["GCF_BAD_000000.1"], ["GCF_000000002.1"]],
                    staging,
                    max_workers=2,
                    max_retries=0,
                    retry_backoff=0.0,
                    on_complete=consume,
                    on_error=record_error,
                )
        self.assertEqual(len(completed), 2)
        self.assertEqual(len(consumed), 2)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], ["GCF_BAD_000000.1"])

    def test_parallel_download_consumer_interrupt_never_deadlocks(self):
        async def fake_batch_download(batch, destination, **kwargs):
            Path(destination).write_text("staged", encoding="utf-8")
            return Path(destination)

        consumed = [0]

        def consume(batch, path):
            consumed[0] += 1
            path.unlink(missing_ok=True)
            time.sleep(0.01)  # let the bounded completion queue fill up
            if consumed[0] >= 2:
                raise KeyboardInterrupt  # simulated Ctrl-C mid-batch

        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "operon.adapters.ncbi_datasets._download_batch_aiohttp",
                side_effect=fake_batch_download,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    download_ncbi_datasets_parallel(
                        [[f"GCF_{number:09d}.1"] for number in range(1, 51)],
                        Path(tmp),
                        max_workers=2,
                        max_retries=0,
                        retry_backoff=0.0,
                        on_complete=consume,
                        on_error=lambda batch, error: None,
                    )
        # The download thread must be gone: a producer blocked on the full
        # completion queue used to deadlock the consumer's thread join and
        # hang interpreter shutdown ("Exception ignored while joining a
        # thread in _thread._shutdown()").
        leftovers = [t for t in threading.enumerate() if t.name == "operon-ncbi-download"]
        deadline = time.time() + 5
        while leftovers and time.time() < deadline:
            time.sleep(0.05)
            leftovers = [t for t in threading.enumerate() if t.name == "operon-ncbi-download"]
        self.assertFalse(leftovers)

    def test_download_batch_stops_immediately_when_cancelled(self):
        cancel_event = threading.Event()
        cancel_event.set()
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "dataset.zip"
            with self.assertRaises(_DownloadCancelled):
                asyncio.run(
                    _download_batch_aiohttp(
                        ["GCF_000001405.40"],
                        destination,
                        includes=["genome"],
                        email=None,
                        api_key=None,
                        timeout=1.0,
                        max_retries=4,
                        retry_backoff=0.0,
                        cancel_event=cancel_event,
                    )
                )
            self.assertFalse(destination.exists())

    def test_interrupted_download_run_is_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(io.StringIO()):
                self._init(root)
            project = load_project(root)
            db = Database(project.db_path)
            try:
                with patch(
                    "operon.adapters.ncbi_datasets.download_ncbi_datasets_parallel",
                    side_effect=KeyboardInterrupt,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        run_ncbi_datasets_adapter(
                            db, project, accessions=["GCF_000001405.40"],
                        )
                rows = db.query(
                    "SELECT status, error FROM workflow_runs WHERE step='ncbi_datasets_import'"
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "interrupted")
            finally:
                db.close()

    def test_empty_ncbi_package_diagnostic_identifies_bad_accession(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "bad.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("README.md", "NCBI Datasets\n")
            payload = package.read_bytes()
            central = payload.find(b"PK\x01\x02")
            self.assertGreater(central, 0)
            malformed = Path(tmp) / "malformed.zip"
            malformed.write_bytes(payload[:central])
            retryable, detail = _zip_package_diagnostic(malformed, ["GCF_999999999.1"])
            self.assertFalse(retryable)
            self.assertIn("GCF_999999999.1", detail)
            self.assertIn("README.md", detail)
