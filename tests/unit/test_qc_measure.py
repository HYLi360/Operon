"""qc-measure payloads (project-independent) and import-qc JSON ingestion."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from tests.helpers import PytestAssertions

from operon import __version__, cli
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ChecksumError, QCError, ValidationError
from operon.files import ingest_file
from operon.qc import file_qc_status, measure_file, qc_file
from operon.utils import sha256_file

FASTA = ">ctg1\n" + "ACGT" * 50 + "\n>ctg2\n" + "GGGG" * 25 + "\n"
FASTQ_R1 = "@r1\nACGT\n+\nIIII\n@r2\nTGCA\n+\nIIII\n"
FASTQ_R2 = "@r1\nACGT\n+\nIIII\n@r2\nTGCA\n+\nIIII\n"
GFF3 = (
    "##gff-version 3\n"
    "ctg1\ttest\tgene\t1\t120\t.\t+\t.\tID=gene1\n"
    "ctg1\ttest\tmRNA\t1\t120\t.\t+\t.\tID=mrna1;Parent=gene1\n"
    "ctg1\ttest\tCDS\t1\t120\t.\t+\t0\tID=cds1;Parent=mrna1\n"
)
PROTEIN = ">p1\nMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA*\n"


def _identity(path: Path) -> dict:
    return {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}


def _metric_tuples(rows) -> set:
    return {
        (row["qc_stage"], row["metric_name"], row["metric_value"],
         row["metric_numeric"], row["metric_unit"], row["parameter_set"])
        for row in rows
    }


def _payload_tuples(payload: dict) -> set:
    return _metric_tuples(payload["metrics"])


class TestMeasureFileParity(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(
            main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_QM_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        self.db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Testus exemplar",
            "taxonomy_source": "NCBI",
        })
        self.db.insert_row("samples", {
            "sample_id": "SMP_000001", "organism_id": "ORG_000001", "sex": "unknown",
        })
        self.db.insert_row("assemblies", {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_level": "scaffold", "assembly_version": 1,
        })
        self.db.insert_row("runs", {
            "run_id": "RUN_000001", "sample_id": "SMP_000001", "library_layout": "PAIRED",
        })

    def _ingest(self, name: str, text: str, entity_type: str, entity_id: str, role: str) -> dict:
        source = self.root / name
        source.write_text(text, encoding="utf-8")
        return ingest_file(self.db, self.project, source, entity_type, entity_id, role)

    def _local_tuples(self, file_id: str) -> set:
        result = qc_file(self.db, self.project, file_id)
        self.assertTrue(result["ok"], result)
        return _metric_tuples(self.db.query("SELECT * FROM qc_results WHERE file_id=?", (file_id,)))

    def test_fasta_genome_role_matches_qc_file(self):
        row = self._ingest("asm.fa", FASTA, "assembly", "ASM_000001", "genome_fasta")
        path = self.root / row["relative_path"]
        payload = measure_file(
            path, file_format="fasta", file_role="genome_fasta",
            file_id=row["file_id"], **_identity(path),
        )
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["tool"], "operon.builtin")
        self.assertEqual(payload["tool_version"], __version__)
        self.assertEqual(payload["parser_backend"], "cython")
        self.assertEqual(payload["parameter_set"], "builtin_v2")
        self.assertEqual(payload["file"]["file_id"], row["file_id"])
        self.assertEqual(payload["file"]["format"], "fasta")
        stages = {m["qc_stage"] for m in payload["metrics"]}
        self.assertEqual(stages, {"file_integrity", "assembly_basic"})
        self.assertEqual(_payload_tuples(payload), self._local_tuples(row["file_id"]))

    def test_fasta_non_genome_role_matches_qc_file(self):
        row = self._ingest("prots.fa", PROTEIN, "assembly", "ASM_000001", "protein_fasta")
        path = self.root / row["relative_path"]
        payload = measure_file(
            path, file_format="fasta", file_role="protein_fasta", **_identity(path),
        )
        stages = {m["qc_stage"] for m in payload["metrics"]}
        self.assertEqual(stages, {"file_integrity", "sequence_basic"})
        self.assertEqual(_payload_tuples(payload), self._local_tuples(row["file_id"]))

    def test_fastq_with_paired_read_matches_qc_file(self):
        r1 = self._ingest("r1.fastq", FASTQ_R1, "run", "RUN_000001", "reads_r1")
        r2 = self._ingest("r2.fastq", FASTQ_R2, "run", "RUN_000001", "reads_r2")
        r1_path = self.root / r1["relative_path"]
        payload = measure_file(
            r1_path, file_format="fastq", file_role="reads_r1",
            paired_read=self.root / r2["relative_path"], **_identity(r1_path),
        )
        names = {m["metric_name"] for m in payload["metrics"]}
        self.assertIn("paired_read_count_match", names)
        fastq_metrics = [m for m in payload["metrics"] if m["qc_stage"] == "reads_basic"]
        self.assertTrue(
            all(m["parameter_set"] == "builtin_v2:sample_1000000:phred_33" for m in fastq_metrics))
        self.assertEqual(_payload_tuples(payload), self._local_tuples(r1["file_id"]))

    def test_gff3_with_related_inputs_matches_qc_file(self):
        self.db.insert_row("annotations", {
            "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
            "annotation_version": 1,
        })
        assembly = self._ingest("ann-asm.fa", FASTA, "assembly", "ASM_000001", "genome_fasta")
        gff = self._ingest("ann.gff3", GFF3, "annotation", "ANN_000001", "annotation_gff3")
        protein = self._ingest("ann.faa", PROTEIN, "annotation", "ANN_000001", "protein_fasta")
        gff_path = self.root / gff["relative_path"]
        payload = measure_file(
            gff_path, file_format="gff3", file_role="annotation_gff3",
            assembly_fasta=self.root / assembly["relative_path"],
            protein_fasta=self.root / protein["relative_path"],
            **_identity(gff_path),
        )
        names = {m["metric_name"] for m in payload["metrics"]}
        self.assertIn("gene_count", names)
        self.assertIn("protein_count", names)
        self.assertIn("cds_protein_count_match", names)
        self.assertEqual(_payload_tuples(payload), self._local_tuples(gff["file_id"]))

    def test_other_format_measures_integrity_only(self):
        row = self._ingest("notes.txt", "plain text\n", "assembly", "ASM_000001", "other")
        path = self.root / row["relative_path"]
        payload = measure_file(path, file_format="other", file_role="other", **_identity(path))
        names = {m["metric_name"] for m in payload["metrics"]}
        self.assertEqual(names, {"file_exists", "size_bytes", "sha256_match"})
        self.assertEqual(_payload_tuples(payload), self._local_tuples(row["file_id"]))

    def test_identity_mismatch_raises_checksum_error(self):
        source = self.root / "x.fa"
        source.write_text(FASTA, encoding="utf-8")
        identity = _identity(source)
        with self.assertRaises(ChecksumError):
            measure_file(source, file_format="fasta", file_role="genome_fasta",
                         sha256="0" * 64, size_bytes=identity["size_bytes"])
        with self.assertRaises(ChecksumError):
            measure_file(source, file_format="fasta", file_role="genome_fasta",
                         sha256=identity["sha256"], size_bytes=identity["size_bytes"] + 1)
        with self.assertRaises(ChecksumError):
            measure_file(self.root / "missing.fa", file_format="fasta", file_role="genome_fasta",
                         **identity)

    def test_gff3_without_related_inputs_raises_qc_error(self):
        source = self.root / "x.gff3"
        source.write_text(GFF3, encoding="utf-8")
        with self.assertRaisesRegex(QCError, "--assembly-fasta"):
            measure_file(source, file_format="gff3", file_role="annotation_gff3", **_identity(source))

    def test_parser_failure_propagates(self):
        source = self.root / "broken.fa"
        source.write_text("not a fasta at all\n", encoding="utf-8")
        with self.assertRaisesRegex(QCError, "sequence data before first FASTA header"):
            measure_file(source, file_format="fasta", file_role="genome_fasta", **_identity(source))


class TestQCMeasureCLI(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _fasta(self) -> Path:
        path = self.root / "remote.fa"
        path.write_text(FASTA, encoding="utf-8")
        return path

    def test_stdout_payload_without_project(self, capsys):
        path = self._fasta()
        identity = _identity(path)
        code = main([
            "--project", str(self.root / "no-project-here"), "qc-measure",
            "--file", str(path), "--format", "fasta", "--role", "genome_fasta",
            "--sha256", identity["sha256"], "--size-bytes", str(identity["size_bytes"]),
        ])
        self.assertEqual(code, 0)
        payload = json.loads(capsys.readouterr().out)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["file"]["file_role"], "genome_fasta")
        self.assertTrue(payload["metrics"])

    def test_out_writes_atomically(self, capsys):
        path = self._fasta()
        identity = _identity(path)
        out = self.root / "nested" / "payload.json"
        code = main([
            "qc-measure", "--file", str(path), "--format", "fasta", "--role", "genome_fasta",
            "--sha256", identity["sha256"], "--size-bytes", str(identity["size_bytes"]),
            "--file-id", "FIL_000042", "--out", str(out),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(capsys.readouterr().out, "")
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["file"]["file_id"], "FIL_000042")
        leftovers = [p for p in out.parent.iterdir() if p.name != "payload.json"]
        self.assertEqual(leftovers, [])

    def test_identity_failure_exits_1(self, capsys):
        path = self._fasta()
        code = main([
            "qc-measure", "--file", str(path), "--format", "fasta", "--role", "genome_fasta",
            "--sha256", "0" * 64, "--size-bytes", str(path.stat().st_size),
        ])
        self.assertEqual(code, 1)
        self.assertIn("error:", capsys.readouterr().err)

    def test_gff3_without_related_exits_1(self, capsys):
        path = self.root / "remote.gff3"
        path.write_text(GFF3, encoding="utf-8")
        identity = _identity(path)
        code = main([
            "qc-measure", "--file", str(path), "--format", "gff3", "--role", "annotation_gff3",
            "--sha256", identity["sha256"], "--size-bytes", str(identity["size_bytes"]),
        ])
        self.assertEqual(code, 1)
        self.assertIn("--assembly-fasta", capsys.readouterr().err)

    def test_unwritable_out_exits_1(self, capsys):
        path = self._fasta()
        identity = _identity(path)
        code = main([
            "qc-measure", "--file", str(path), "--format", "fasta", "--role", "genome_fasta",
            "--sha256", identity["sha256"], "--size-bytes", str(identity["size_bytes"]),
            "--out", str(path / "payload.json"),
        ])
        self.assertEqual(code, 1)
        self.assertIn("error:", capsys.readouterr().err)


class TestImportQCJSON(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(
            main(["--project", str(self.root), "init", str(self.root), "--project-id", "PRJ_IQJ_001"]), 0)
        self.project = load_project(self.root)
        self.db = Database(self.project.db_path)
        self.addCleanup(self.db.close)
        self.db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Testus exemplar",
            "taxonomy_source": "NCBI",
        })
        self.db.insert_row("samples", {
            "sample_id": "SMP_000001", "organism_id": "ORG_000001", "sex": "unknown",
        })
        self.db.insert_row("assemblies", {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_level": "scaffold", "assembly_version": 1,
        })

    def _ingest_assembly(self, name: str = "asm.fa", text: str = FASTA,
                         entity_id: str = "ASM_000001") -> dict:
        source = self.root / name
        source.write_text(text, encoding="utf-8")
        return ingest_file(self.db, self.project, source, "assembly", entity_id, "genome_fasta")

    def _measure_payload(self, row: dict, **overrides) -> dict:
        path = self.root / row["relative_path"]
        options = {
            "file_format": "fasta", "file_role": "genome_fasta",
            "file_id": row["file_id"], **_identity(path),
        }
        options.update(overrides)
        return measure_file(path, **options)

    def _write_payload(self, payload: dict, name: str = "payload.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _import_runs(self) -> list[dict]:
        return self.db.query("SELECT * FROM workflow_runs WHERE step='import-qc'")

    def test_roundtrip_matches_local_qc_results(self, capsys):
        remote_row = self._ingest_assembly("remote.fa")
        self.db.insert_row("assemblies", {
            "assembly_id": "ASM_000002", "sample_id": "SMP_000001",
            "assembly_level": "scaffold", "assembly_version": 1,
        })
        local_row = self._ingest_assembly("local.fa", entity_id="ASM_000002")
        payload = self._measure_payload(remote_row)
        payload_path = self._write_payload(payload)

        code = main(["--project", str(self.root), "import-qc", "--file", str(payload_path)])

        self.assertEqual(code, 0)
        self.assertIn(f"for {remote_row['file_id']}", capsys.readouterr().out)
        self.assertEqual(file_qc_status(self.db, remote_row["file_id"]), "QC_COMPLETE")
        self.assertEqual(self.db.get_entity_state("assembly", "ASM_000001"), "QC_COMPLETE")
        self.assertTrue(
            qc_file(self.db, self.project, local_row["file_id"])["ok"])
        remote_metrics = _metric_tuples(
            self.db.query("SELECT * FROM qc_results WHERE file_id=?", (remote_row["file_id"],)))
        local_metrics = _metric_tuples(
            self.db.query("SELECT * FROM qc_results WHERE file_id=?", (local_row["file_id"],)))
        self.assertEqual(remote_metrics, local_metrics)
        runs = self._import_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["command"], f"operon import-qc --file {payload_path}")
        self.assertEqual(runs[0]["entity_type"], "assembly")
        self.assertEqual(runs[0]["entity_id"], "ASM_000001")
        self.assertEqual(runs[0]["status"], "completed")

    def test_sha256_reverse_lookup_without_file_id(self):
        row = self._ingest_assembly()
        payload = self._measure_payload(row, file_id=None)
        self.assertIsNone(payload["file"]["file_id"])
        code = main(["--project", str(self.root), "import-qc", "--file",
                     str(self._write_payload(payload))])
        self.assertEqual(code, 0)
        self.assertEqual(file_qc_status(self.db, row["file_id"]), "QC_COMPLETE")

    def _import(self, payload: dict, name: str = "payload.json") -> int:
        path = self._write_payload(payload, name)
        return cli._cmd_import_qc(
            SimpleNamespace(tsv_file=str(path)), self.project, self.db)

    def test_ambiguous_sha256_lookup_rejected(self):
        self._ingest_assembly("a.fa")
        self.db.insert_row("assemblies", {
            "assembly_id": "ASM_000002", "sample_id": "SMP_000001",
            "assembly_level": "scaffold", "assembly_version": 1,
        })
        second = self._ingest_assembly("b.fa", entity_id="ASM_000002")
        payload = self._measure_payload(second, file_id=None)
        with self.assertRaisesRegex(ValidationError, "matches 2 manifest files"):
            self._import(payload)

    def test_unknown_sha256_lookup_rejected(self):
        row = self._ingest_assembly()
        payload = self._measure_payload(row, file_id=None)
        payload["file"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "no manifest file matches"):
            self._import(payload)

    def test_manifest_mismatch_rejected(self):
        row = self._ingest_assembly()
        payload = self._measure_payload(row)
        payload["file"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "sha256 does not match manifest"):
            self._import(payload)
        payload = self._measure_payload(row)
        payload["file"]["size_bytes"] += 1
        with self.assertRaisesRegex(ValidationError, "size_bytes does not match manifest"):
            self._import(payload)
        payload = self._measure_payload(row, file_id="FIL_MISSING")
        with self.assertRaisesRegex(ValidationError, "file_id FIL_MISSING does not exist"):
            self._import(payload)
        self.assertEqual(self.db.query("SELECT COUNT(*) AS n FROM qc_results")[0]["n"], 0)

    def test_envelope_validation(self):
        row = self._ingest_assembly()
        payload = self._measure_payload(row)
        with self.assertRaisesRegex(ValidationError, "schema_version"):
            self._import(dict(payload, schema_version=2))
        with self.assertRaisesRegex(ValidationError, "payload tool"):
            self._import(dict(payload, tool="other.tool"))
        with self.assertRaisesRegex(ValidationError, "file identity"):
            self._import(dict(payload, file={"file_id": row["file_id"]}))
        with self.assertRaisesRegex(ValidationError, "metrics must be a list"):
            self._import(dict(payload, metrics="nope"))
        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "invalid qc-measure JSON"):
            cli._cmd_import_qc(
                SimpleNamespace(tsv_file=str(broken)), self.project, self.db)

    def test_tool_version_mismatch_warns_but_imports(self, capsys):
        row = self._ingest_assembly()
        payload = self._measure_payload(row)
        payload["tool_version"] = "0.0.0-pretend"
        code = main(["--project", str(self.root), "import-qc", "--file",
                     str(self._write_payload(payload))])
        self.assertEqual(code, 0)
        self.assertIn("0.0.0-pretend", capsys.readouterr().err)
        versions = {r["tool_version"] for r in self.db.query("SELECT * FROM qc_results")}
        self.assertEqual(versions, {"0.0.0-pretend"})

    def test_extensionless_json_detected_by_content(self):
        row = self._ingest_assembly()
        payload_path = self._write_payload(self._measure_payload(row), name="payload.out")
        code = main(["--project", str(self.root), "import-qc", "--file", str(payload_path)])
        self.assertEqual(code, 0)
        self.assertEqual(file_qc_status(self.db, row["file_id"]), "QC_COMPLETE")

    def test_tsv_import_recomputes_state_and_logs_provenance(self, capsys):
        row = self._ingest_assembly()
        tsv = self.root / "external.tsv"
        tsv.write_text(
            "entity_type\tentity_id\tfile_id\tfile_sha256\tqc_stage\tmetric_name\t"
            "metric_value\ttool\ttool_version\tparameter_set\n"
            f"assembly\tASM_000001\t{row['file_id']}\t{row['sha256']}\tfile_integrity\t"
            "file_exists\t1\tquast\t5.2.0\texternal\n",
            encoding="utf-8",
        )
        code = main(["--project", str(self.root), "import-qc", "--file", str(tsv)])
        self.assertEqual(code, 0)
        self.assertIn("imported 1", capsys.readouterr().out)
        self.assertEqual(file_qc_status(self.db, row["file_id"]), "QC_COMPLETE")
        self.assertEqual(self.db.get_entity_state("assembly", "ASM_000001"), "QC_COMPLETE")
        runs = self._import_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["command"], f"operon import-qc --file {tsv}")
        self.assertEqual(runs[0]["entity_type"], "assembly")
