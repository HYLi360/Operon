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
    _accession_from_path,
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
from operon.files import canonical_filename, ingest_file
from operon.ncbi_reconcile import apply_ncbi_reconciliation, plan_ncbi_reconciliation
from operon.utils import now_iso, sha256_file


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
            self.assertEqual(upgraded_schema["schema_version"], "1.4")
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

    def test_accession_from_path_prefers_versioned_match(self):
        # Real NCBI packages name members "<accession>_<description>.ext"; the
        # versioned accession (from the per-accession directory) must win over
        # the truncated unversioned match inside the filename.
        path = Path("ncbi_dataset/data/GCF_000001405.40/GCF_000001405.40_GRCh38.p14_genomic.fna")
        self.assertEqual(_accession_from_path(path), "GCF_000001405.40")
        self.assertEqual(_accession_from_path(Path("data/GCF_000001405/genomic.gff")), "GCF_000001405")

    def test_already_archived_accessions_are_skipped_before_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(io.StringIO()):
                self._init(root)
            project = load_project(root)
            accession = "GCF_000001405.40"
            with_annotation = [False]
            calls: list[tuple[list[str], tuple[str, ...]]] = []

            def fake_parallel(batches, staging_dir, **kwargs):
                on_complete = kwargs["on_complete"]
                for batch in batches:
                    calls.append((list(batch), tuple(kwargs["includes"])))
                    destination = Path(staging_dir) / f"batch_{len(calls)}.zip"
                    with zipfile.ZipFile(destination, "w") as archive:
                        archive.writestr(
                            "ncbi_dataset/data/assembly_data_report.jsonl",
                            json.dumps(_report(accession)) + "\n",
                        )
                        prefix = f"ncbi_dataset/data/{accession}"
                        archive.writestr(f"{prefix}/{accession}_genomic.fna", ">c1\nATGC\n")
                        archive.writestr(f"{prefix}/sequence_report.jsonl", "{}\n")
                        if with_annotation[0]:
                            archive.writestr(f"{prefix}/genomic.gff", "##gff-version 3\n")
                    on_complete(batch, destination)
                return []

            db = Database(project.db_path)
            try:
                with patch(
                    "operon.adapters.ncbi_datasets.download_ncbi_datasets_parallel",
                    side_effect=fake_parallel,
                ):
                    first = run_ncbi_datasets_adapter(
                        db, project, accessions=[accession],
                        includes=["genome", "sequence-report"],
                    )
                    self.assertEqual(first["skipped_existing"], [])
                    self.assertEqual(len(first["archived_files"]), 2)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(calls[0][1], ("genome", "sequence-report"))

                    # Same include set, fully archived: no download attempted.
                    second = run_ncbi_datasets_adapter(
                        db, project, accessions=[accession],
                        includes=["genome", "sequence-report"],
                    )
                    self.assertEqual(second["skipped_existing"], [accession])
                    self.assertEqual(len(calls), 1)

                    preview = run_ncbi_datasets_adapter(
                        db, project, accessions=[accession],
                        includes=["genome", "sequence-report", "gff3"],
                        plan_only=True,
                    )
                    self.assertTrue(preview["plan_only"])
                    self.assertEqual(preview["download_plan"][0]["includes"], ["gff3"])
                    self.assertEqual(len(calls), 1)

                    # A widened include set downloads only the missing role.
                    third = run_ncbi_datasets_adapter(
                        db, project, accessions=[accession],
                        includes=["genome", "sequence-report", "gff3"],
                    )
                    self.assertEqual(third["skipped_existing"], [])
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[1][1], ("gff3",))
            finally:
                db.close()

    def test_non_ncbi_provider_reuses_annotation_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(io.StringIO()):
                self._init(root)
            report = _report()
            report["annotationInfo"] = {
                "provider": "National Institute of Genetics",
                "version": 7,
                "releaseDate": "2025-04-11",
            }
            report_path = root / "report.jsonl"
            report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
            command = ["--project", str(root), "ncbi-datasets", "--input", str(report_path)]
            self.assertEqual(main(command), 0)
            self.assertEqual(main(command), 0)
            db = Database(root / "operon.sqlite")
            try:
                self.assertEqual(db.query("SELECT COUNT(*) n FROM annotations")[0]["n"], 1)
                annotation = db.query("SELECT * FROM annotations")[0]
                self.assertEqual(annotation["annotation_source"], "National Institute of Genetics")
                self.assertEqual(annotation["annotation_version"], 7)
                self.assertEqual(
                    db.query("SELECT COUNT(*) n FROM ncbi_annotation_records")[0]["n"], 1,
                )
            finally:
                db.close()

    def test_paired_gca_gcf_keep_canonical_and_source_specific_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(io.StringIO()):
                self._init(root)
            gca = "GCA_000001405.29"
            gcf = "GCF_000001405.40"

            def make_package(path: Path, accession: str, paired: str, report_text: str) -> None:
                report = _report(accession)
                report["assemblyInfo"]["pairedAssembly"] = {"accession": paired}
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(
                        "ncbi_dataset/data/assembly_data_report.jsonl",
                        json.dumps(report) + "\n",
                    )
                    prefix = f"ncbi_dataset/data/{accession}"
                    archive.writestr(f"{prefix}/sequence_report.jsonl", report_text)

            gca_package = root / "gca.zip"
            gcf_package = root / "gcf.zip"
            make_package(gca_package, gca, gcf, '{"source":"GenBank"}\n')
            make_package(gcf_package, gcf, gca, '{"source":"RefSeq"}\n')
            self.assertEqual(main([
                "--project", str(root), "ncbi-datasets", "--input", str(gca_package),
            ]), 0)
            self.assertEqual(main([
                "--project", str(root), "ncbi-datasets", "--input", str(gcf_package),
            ]), 0)
            db = Database(root / "operon.sqlite")
            try:
                assembly = db.query("SELECT * FROM assemblies")[0]
                self.assertEqual(assembly["assembly_accession"], gcf)
                files = db.query(
                    "SELECT file_role FROM files WHERE entity_type='assembly' ORDER BY file_role"
                )
                self.assertEqual(
                    [row["file_role"] for row in files],
                    ["assembly_report", "assembly_report_genbank"],
                )
                self.assertEqual(
                    db.query("SELECT COUNT(*) n FROM ncbi_assembly_records")[0]["n"], 2,
                )
            finally:
                db.close()

    def test_reimport_does_not_demote_qc_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(io.StringIO()):
                self._init(root)
            package = root / "package.zip"
            accession = "GCF_000001405.40"
            prefix = f"ncbi_dataset/data/{accession}"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(
                    "ncbi_dataset/data/assembly_data_report.jsonl",
                    json.dumps(_report(accession)) + "\n",
                )
                archive.writestr(f"{prefix}/genomic.gff", "##gff-version 3\n")
            command = ["--project", str(root), "ncbi-datasets", "--input", str(package)]
            self.assertEqual(main(command), 0)
            db = Database(root / "operon.sqlite")
            try:
                annotation_id = db.query("SELECT annotation_id FROM annotations")[0]["annotation_id"]
                db.set_entity_state("annotation", annotation_id, "QC_COMPLETE", "test evidence")
            finally:
                db.close()
            self.assertEqual(main(command), 0)
            db = Database(root / "operon.sqlite")
            try:
                state = db.query(
                    "SELECT state FROM entity_state WHERE entity_type='annotation'"
                )[0]["state"]
                self.assertEqual(state, "QC_COMPLETE")
            finally:
                db.close()

    def test_reconcile_preserves_rows_and_records_compensating_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(io.StringIO()):
                self._init(root)
            report_path = root / "report.jsonl"
            report = _report()
            report["annotationInfo"]["provider"] = "National Institute of Genetics"
            report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
            self.assertEqual(main([
                "--project", str(root), "ncbi-datasets", "--input", str(report_path),
            ]), 0)
            project = load_project(root)
            db = Database(project.db_path)
            try:
                assembly = dict(db.query("SELECT * FROM assemblies")[0])
                original = dict(db.query("SELECT * FROM annotations")[0])
                gcf_gff = root / "GCF_000001405.40_genomic.gff"
                gcf_gff.write_text("##gff-version 3\n", encoding="utf-8")
                ingest_file(
                    db, project, gcf_gff, "annotation", original["annotation_id"],
                    "annotation_gff3", source_url=(
                        "ncbi-datasets:test:GCF_000001405.40/genomic.gff"
                    ),
                )
                original = dict(db.query(
                    "SELECT * FROM annotations WHERE annotation_id=?",
                    (original["annotation_id"],),
                )[0])
                duplicate_id = db.next_id("annotation")
                duplicate = dict(original)
                duplicate["annotation_id"] = duplicate_id
                db.insert_row("annotations", duplicate)
                db.insert_qc_result({
                    "entity_type": "annotation", "entity_id": original["annotation_id"],
                    "qc_stage": "test", "metric_name": "parseable", "metric_value": "1",
                    "metric_numeric": 1.0, "metric_unit": None, "tool": "test",
                    "tool_version": "1", "parameter_set": "test", "evaluated_at": now_iso(),
                })
                db.set_entity_state(
                    "annotation", original["annotation_id"], "CHECKSUM_VERIFIED", "simulated downgrade",
                )
                db.conn.execute(
                    "UPDATE assemblies SET assembly_accession='GCA_000001405.29', "
                    "source_database='GenBank' WHERE assembly_id=?",
                    (assembly["assembly_id"],),
                )
                db.conn.commit()
                sequence_report = root / "GCA_000001405.29_sequence_report.jsonl"
                sequence_report.write_text("{}\n", encoding="utf-8")
                report_file = ingest_file(
                    db, project, sequence_report, "assembly", assembly["assembly_id"],
                    "assembly_report", source_url=(
                        "ncbi-datasets:test:GCA_000001405.29/sequence_report.jsonl"
                    ),
                )

                preview = plan_ncbi_reconciliation(db)
                self.assertEqual(len(preview["annotation_supersessions"]), 1)
                self.assertEqual(len(preview["assembly_updates"]), 1)
                self.assertEqual(len(preview["file_role_updates"]), 1)
                result = apply_ncbi_reconciliation(db, project, actor="test")

                self.assertEqual(db.query("SELECT COUNT(*) n FROM annotations")[0]["n"], 2)
                supersession = db.query(
                    "SELECT * FROM entity_supersessions WHERE object_type='annotation'"
                )[0]
                self.assertEqual(supersession["object_id"], duplicate_id)
                self.assertEqual(supersession["superseded_by_id"], original["annotation_id"])
                repaired = db.query(
                    "SELECT assembly_accession, source_database FROM assemblies WHERE assembly_id=?",
                    (assembly["assembly_id"],),
                )[0]
                self.assertEqual(repaired["assembly_accession"], "GCF_000001405.40")
                self.assertEqual(repaired["source_database"], "RefSeq")
                role = db.query(
                    "SELECT file_role FROM files WHERE file_id=?", (report_file["file_id"],)
                )[0]["file_role"]
                self.assertEqual(role, "assembly_report_genbank")
                moved_rel = db.query(
                    "SELECT relative_path FROM files WHERE file_id=?", (report_file["file_id"],)
                )[0]["relative_path"]
                self.assertIn("assembly_report_genbank", moved_rel)
                self.assertTrue((root / moved_rel).exists())
                state = db.get_entity_state("annotation", original["annotation_id"])
                self.assertEqual(state, "QC_COMPLETE")
                workflow = db.query(
                    "SELECT status FROM workflow_runs WHERE run_id=?", (result["run_id"],)
                )[0]
                self.assertEqual(workflow["status"], "completed")
                self.assertGreater(
                    db.query(
                        "SELECT COUNT(*) n FROM changes WHERE workflow_run_id=?",
                        (result["run_id"],),
                    )[0]["n"],
                    0,
                )
                repeated = plan_ncbi_reconciliation(db)
                self.assertEqual(repeated["summary"], {
                    "annotation_supersessions": 0,
                    "assembly_updates": 0,
                    "file_role_updates": 0,
                    "file_path_repairs": 0,
                    "accession_primary_updates": 0,
                    "state_restorations": 0,
                    "warnings": 0,
                })
            finally:
                db.close()

    def test_paired_packages_with_identical_annotation_info_get_distinct_annotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init(root)
            gca = "GCA_000001405.29"
            gcf = "GCF_000001405.40"

            def write_package(path: Path, accession: str, gff_body: str) -> None:
                report = _report(accession)
                report["assemblyInfo"]["pairedAssembly"] = {
                    "accession": gcf if accession == gca else gca,
                }
                if accession == gca:
                    report["assemblyInfo"].pop("refseqCategory", None)
                prefix = f"ncbi_dataset/data/{accession}"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(
                        "ncbi_dataset/data/assembly_data_report.jsonl",
                        json.dumps(report) + "\n",
                    )
                    archive.writestr(f"{prefix}/genomic.gff", gff_body)

            gca_package = root / "gca.zip"
            gcf_package = root / "gcf.zip"
            write_package(
                gca_package, gca,
                "##gff-version 3\nchr1\tGenBank\tgene\t1\t4\t.\t+\t.\tID=gca1\n",
            )
            write_package(
                gcf_package, gcf,
                "##gff-version 3\nchr1\tRefSeq\tgene\t1\t9\t.\t+\t.\tID=gcf1\n",
            )

            # Identical annotationInfo but different GFF bytes: the paired
            # import must not bridge onto the first package's annotation.
            command = ["--project", str(root), "ncbi-datasets", "--input"]
            self.assertEqual(main([*command, str(gca_package)]), 0)
            self.assertEqual(main([*command, str(gcf_package)]), 0)
            self.assertEqual(main([*command, str(gcf_package)]), 0)
            db = Database(root / "operon.sqlite")
            try:
                self.assertEqual(db.query("SELECT COUNT(*) n FROM assemblies")[0]["n"], 1)
                annotations = [dict(row) for row in db.query("SELECT * FROM annotations")]
                self.assertEqual(len(annotations), 2)
                for annotation in annotations:
                    self.assertIsNotNone(annotation["gff_file_id"])
                self.assertEqual(
                    db.query(
                        "SELECT COUNT(*) n FROM files WHERE file_role='annotation_gff3'"
                    )[0]["n"],
                    2,
                )
            finally:
                db.close()

    def test_single_package_with_paired_reports_keeps_annotations_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init(root)
            gca = "GCA_000001405.29"
            gcf = "GCF_000001405.40"
            reports = []
            for accession in (gca, gcf):
                report = _report(accession)
                report["assemblyInfo"]["pairedAssembly"] = {
                    "accession": gcf if accession == gca else gca,
                }
                if accession == gca:
                    report["assemblyInfo"].pop("refseqCategory", None)
                reports.append(report)
            package = root / "ncbi_dataset.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(
                    "ncbi_dataset/data/assembly_data_report.jsonl",
                    "".join(json.dumps(report) + "\n" for report in reports),
                )
                archive.writestr(
                    f"ncbi_dataset/data/{gca}/genomic.gff",
                    "##gff-version 3\nchr1\tGenBank\tgene\t1\t4\t.\t+\t.\tID=gca1\n",
                )
                archive.writestr(
                    f"ncbi_dataset/data/{gcf}/genomic.gff",
                    "##gff-version 3\nchr1\tRefSeq\tgene\t1\t9\t.\t+\t.\tID=gcf1\n",
                )

            command = ["--project", str(root), "ncbi-datasets", "--input", str(package)]
            self.assertEqual(main(command), 0)
            self.assertEqual(main(command), 0)
            db = Database(root / "operon.sqlite")
            try:
                self.assertEqual(db.query("SELECT COUNT(*) n FROM assemblies")[0]["n"], 1)
                self.assertEqual(db.query("SELECT COUNT(*) n FROM annotations")[0]["n"], 2)
                self.assertEqual(
                    db.query(
                        "SELECT COUNT(*) n FROM files WHERE file_role='annotation_gff3'"
                    )[0]["n"],
                    2,
                )
            finally:
                db.close()

    def test_ingest_relocates_file_left_at_stale_path_after_role_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init(root)
            accession = "GCF_000001405.40"
            package = root / "ncbi_dataset.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(
                    "ncbi_dataset/data/assembly_data_report.jsonl",
                    json.dumps(_report()) + "\n",
                )
                archive.writestr(
                    f"ncbi_dataset/data/{accession}/sequence_report.jsonl",
                    '{"sequence_name":"chr1"}\n',
                )
            self.assertEqual(main(["--project", str(root), "ncbi-datasets", "--input", str(package)]), 0)
            project = load_project(root)
            db = Database(project.db_path)
            try:
                assembly = dict(db.query("SELECT * FROM assemblies")[0])
                row = dict(db.query("SELECT * FROM files WHERE file_role LIKE 'assembly_report%'")[0])
                old_path = root / row["relative_path"]
                self.assertTrue(old_path.exists())
                # Simulate a pre-fix reconciliation: role renamed in the
                # manifest, but the file left at the plain canonical path.
                db.conn.execute(
                    "UPDATE files SET file_role='assembly_report_genbank' WHERE file_id=?",
                    (row["file_id"],),
                )
                db.conn.commit()
                # A later ingest of the plain role with different bytes must
                # relocate the renamed file instead of raising ConflictError.
                other = root / "other_sequence_report.jsonl"
                other.write_text('{"sequence_name":"chr2"}\n', encoding="utf-8")
                new_row = ingest_file(
                    db, project, other, "assembly", assembly["assembly_id"], "assembly_report",
                )
                renamed_path = old_path.with_name(canonical_filename(
                    assembly["assembly_id"], "assembly_report_genbank",
                    row["format"], row["compression"],
                ))
                # The plain canonical path now holds the newly ingested bytes,
                # while the renamed file keeps its own bytes at its own path.
                self.assertEqual(sha256_file(old_path), sha256_file(other))
                self.assertTrue(renamed_path.exists())
                self.assertEqual(sha256_file(renamed_path), row["sha256"])
                relocated = dict(db.query(
                    "SELECT * FROM files WHERE file_id=?", (row["file_id"],),
                )[0])
                self.assertEqual(relocated["relative_path"], str(renamed_path.relative_to(root)))
                self.assertEqual(new_row["file_role"], "assembly_report")
                self.assertTrue((root / new_row["relative_path"]).exists())
                audit = db.query(
                    "SELECT * FROM changes WHERE object_type='files' AND object_id=? "
                    "AND field='relative_path'",
                    (row["file_id"],),
                )
                self.assertEqual(len(audit), 1)
            finally:
                db.close()

    def test_ingest_quarantines_untracked_leftover_at_target_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._init(root)
            accession = "GCF_000001405.40"
            package = root / "ncbi_dataset.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr(
                    "ncbi_dataset/data/assembly_data_report.jsonl",
                    json.dumps(_report()) + "\n",
                )
            self.assertEqual(main(["--project", str(root), "ncbi-datasets", "--input", str(package)]), 0)
            project = load_project(root)
            db = Database(project.db_path)
            try:
                assembly = dict(db.query("SELECT * FROM assemblies")[0])
                entity_dir = root / "raw" / "assemblies" / assembly["assembly_id"]
                entity_dir.mkdir(parents=True, exist_ok=True)
                orphan = entity_dir / f"{assembly['assembly_id']}.assembly_report.json"
                orphan.write_text('{"leftover": true}\n', encoding="utf-8")
                orphan_sha = sha256_file(orphan)
                report = root / "sequence_report.jsonl"
                report.write_text('{"sequence_name":"chr1"}\n', encoding="utf-8")
                row = ingest_file(
                    db, project, report, "assembly", assembly["assembly_id"], "assembly_report",
                )
                quarantined = entity_dir / f"{orphan.name}.orphan-{orphan_sha[:12]}"
                self.assertTrue(quarantined.exists())
                self.assertEqual(quarantined.read_text(encoding="utf-8"), '{"leftover": true}\n')
                # The canonical path now holds the newly ingested bytes.
                self.assertEqual(sha256_file(orphan), sha256_file(report))
                self.assertTrue((root / row["relative_path"]).exists())
                audit = db.query("SELECT * FROM changes WHERE field='quarantined'")
                self.assertEqual(len(audit), 1)
            finally:
                db.close()

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
                    "SELECT run_id, status, error FROM workflow_runs WHERE step='ncbi_datasets_import'"
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "interrupted")
                interrupted_run = rows[0]["run_id"]
                item = db.query(
                    "SELECT status FROM adapter_run_items WHERE run_id=?",
                    (interrupted_run,),
                )[0]
                self.assertEqual(item["status"], "pending")

                def fake_parallel(batches, staging_dir, **kwargs):
                    for batch in batches:
                        accession = batch[0]
                        destination = Path(staging_dir) / "resume.zip"
                        with zipfile.ZipFile(destination, "w") as archive:
                            archive.writestr(
                                "ncbi_dataset/data/assembly_data_report.jsonl",
                                json.dumps(_report(accession)) + "\n",
                            )
                            prefix = f"ncbi_dataset/data/{accession}"
                            archive.writestr(f"{prefix}/genomic.fna", ">c1\nATGC\n")
                            archive.writestr(f"{prefix}/sequence_report.jsonl", "{}\n")
                            archive.writestr(f"{prefix}/genomic.gff", "##gff-version 3\n")
                            archive.writestr(f"{prefix}/protein.faa", ">p1\nMK\n")
                            archive.writestr(f"{prefix}/cds_from_genomic.fna", ">c1\nATG\n")
                        kwargs["on_complete"](batch, destination)
                    return []

                with patch(
                    "operon.adapters.ncbi_datasets.download_ncbi_datasets_parallel",
                    side_effect=fake_parallel,
                ):
                    result = run_ncbi_datasets_adapter(
                        db, project, accessions=["GCF_000001405.40"],
                        resume_run_id=interrupted_run,
                    )
                resumed = db.query(
                    "SELECT status, resumes_run_id FROM workflow_runs WHERE run_id=?",
                    (result["run_id"],),
                )[0]
                self.assertEqual(resumed["status"], "completed")
                self.assertEqual(resumed["resumes_run_id"], interrupted_run)
                resumed_item = db.query(
                    "SELECT status FROM adapter_run_items WHERE run_id=?",
                    (result["run_id"],),
                )[0]
                self.assertEqual(resumed_item["status"], "completed")
            finally:
                db.close()

    def _run_download_with_broken_ingest(self, root: Path) -> Database:
        """Run one downloaded batch whose import stage raises; return the open DB."""
        with redirect_stdout(io.StringIO()):
            self._init(root)
        project = load_project(root)
        db = Database(project.db_path)

        def fake_parallel(batches, staging_dir, **kwargs):
            for batch in batches:
                accession = batch[0]
                destination = Path(staging_dir) / "batch.zip"
                with zipfile.ZipFile(destination, "w") as archive:
                    archive.writestr(
                        "ncbi_dataset/data/assembly_data_report.jsonl",
                        json.dumps(_report(accession)) + "\n",
                    )
                    archive.writestr(f"ncbi_dataset/data/{accession}/genomic.fna", ">c1\nATGC\n")
                kwargs["on_complete"](batch, destination)
            return []

        try:
            with patch(
                "operon.adapters.ncbi_datasets.download_ncbi_datasets_parallel",
                side_effect=fake_parallel,
            ), patch(
                "operon.adapters.ncbi_datasets._ingest_dataset_asset",
                side_effect=RuntimeError("ingest boom"),
            ):
                with self.assertRaises(RuntimeError):
                    run_ncbi_datasets_adapter(
                        db, project, accessions=["GCF_000001405.40"], includes=["genome"],
                    )
        except Exception:
            db.close()
            raise
        return db

    def test_failed_import_marks_run_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._run_download_with_broken_ingest(Path(tmp))
            try:
                rows = db.query(
                    "SELECT status, exit_code, error FROM workflow_runs "
                    "WHERE step='ncbi_datasets_import'"
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "failed")
                self.assertEqual(rows[0]["exit_code"], 1)
                self.assertIn("ingest boom", rows[0]["error"])
            finally:
                db.close()

    def test_failed_import_marks_accession_items_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._run_download_with_broken_ingest(Path(tmp))
            try:
                items = db.query("SELECT status, error FROM adapter_run_items")
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["status"], "failed")
                self.assertIn("RuntimeError: ingest boom", items[0]["error"])
            finally:
                db.close()

    def test_partial_batch_failure_raises_after_keeping_successes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(io.StringIO()):
                self._init(root)
            project = load_project(root)
            db = Database(project.db_path)
            good = "GCF_000001405.40"
            bad = "GCF_000002035.6"

            def fake_parallel(batches, staging_dir, **kwargs):
                for batch in batches:
                    accession = batch[0]
                    if accession == bad:
                        kwargs["on_error"](batch, ValidationError(f"{bad} not found"))
                        continue
                    destination = Path(staging_dir) / "batch.zip"
                    with zipfile.ZipFile(destination, "w") as archive:
                        archive.writestr(
                            "ncbi_dataset/data/assembly_data_report.jsonl",
                            json.dumps(_report(accession)) + "\n",
                        )
                        archive.writestr(
                            f"ncbi_dataset/data/{accession}/genomic.fna", ">c1\nATGC\n"
                        )
                    kwargs["on_complete"](batch, destination)
                return []

            try:
                with patch(
                    "operon.adapters.ncbi_datasets.download_ncbi_datasets_parallel",
                    side_effect=fake_parallel,
                ):
                    with self.assertRaises(ValidationError) as caught:
                        run_ncbi_datasets_adapter(
                            db, project, accessions=[good, bad],
                            includes=["genome"], batch_size=1,
                        )
                message = str(caught.value)
                self.assertIn("1 NCBI download batch(es) failed", message)
                self.assertIn(bad, message)
                # The successfully imported batch stays archived.
                self.assertEqual(
                    db.query("SELECT assembly_accession FROM assemblies")[0]["assembly_accession"],
                    good,
                )
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
