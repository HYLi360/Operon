"""Frozen NCBI taxonomy denominators and metadata/release coverage reports."""

from __future__ import annotations

import json
import io
import tarfile
import tempfile
from pathlib import Path

import yaml

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.files import ingest_file
from operon.qc_module import qc_file
from operon.release import create_release
from operon.rules import evaluate_entity
from operon.schema import read_tsv
from tests.helpers import PytestAssertions


def _taxonomy_records() -> list[dict]:
    def node(
        taxid: int, parent: int, rank: str, name: str, *,
        extinct: bool = False, aliases: list[int] | None = None,
    ) -> dict:
        return {
            "taxId": taxid,
            "parents": [parent] if taxid != parent else [],
            "rank": rank.upper(),
            "currentScientificName": {"name": name},
            "extinct": extinct,
            "secondaryTaxIds": aliases or [],
        }

    return [
        node(1, 1, "no rank", "root"),
        node(33090, 1, "clade", "Viridiplantae"),
        node(10, 33090, "family", "Plantaceae"),
        node(11, 10, "genus", "Alpha"),
        node(12, 11, "species", "Alpha one", aliases=[1200]),
        node(20, 33090, "family", "Fossilaceae", extinct=True),
        node(21, 20, "genus", "Fossilus", extinct=True),
        node(31, 10, "genus", "environmental samples"),
        node(40, 33090, "family", "Missingaceae"),
        node(41, 40, "genus", "Missinggenus"),
        node(42, 41, "species", "Missinggenus one"),
        node(50, 10, "genus", "unclassified Alpha"),
        node(60, 33090, "family", "Excludedaceae"),
        node(61, 60, "genus", "Excludedgenus"),
        node(62, 61, "species", "Excludedgenus one"),
    ]


def _write_taxdump(path: Path) -> None:
    records = _taxonomy_records()
    nodes = "".join(
        f"{record['taxId']}\t|\t"
        f"{(record.get('parents') or [record['taxId']])[0]}\t|\t"
        f"{str(record['rank']).lower()}\t|\t\t|\t0\t|\t0\t|\t1\t|\t0\t|\t1\t|\t0\t0\t|\t0\t|\t0\t|\t\t|\n"
        for record in records
    )
    names = "".join(
        f"{record['taxId']}\t|\t{record['currentScientificName']['name']}\t|\t\t|\tscientific name\t|\n"
        for record in records
    )
    merged = "1200\t|\t12\t|\n"
    deleted = "9999\t|\n"
    with tarfile.open(path, "w:gz") as archive:
        for name, content in {
            "nodes.dmp": nodes,
            "names.dmp": names,
            "merged.dmp": merged,
            "delnodes.dmp": deleted,
        }.items():
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))


class TestTaxonomyCoverage(PytestAssertions):
    def setup_method(self):
        super().setup_method()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.assertEqual(main(["--project", str(self.root), "init", str(self.root)]), 0)
        self.project = load_project(self.root)
        self.taxonomy_path = self.root / "taxonomy_report.jsonl"
        self.taxonomy_path.write_text(
            "".join(json.dumps(record) + "\n" for record in _taxonomy_records()),
            encoding="utf-8",
        )
        profile = {
            "kind": "taxonomy_coverage",
            "version": 1,
            "name": "plants_v1",
            "taxonomy": {"source": "NCBI"},
            "scope": {"root_taxids": [33090]},
            "targets": {"ranks": ["family", "genus"]},
            "filters": {
                "exclude_extinct": True,
                "exclude_subtrees": [60],
                "exclude_name_patterns": [
                    r"(?i)^unclassified(?:\s|$)",
                    r"(?i)environmental samples$",
                ],
            },
            "thresholds": {
                "family": {"min_coverage_percent": 50},
                "genus": {"min_coverage_percent": 60},
            },
        }
        (self.project.profiles_dir / "plants_v1.yaml").write_text(
            yaml.safe_dump(profile, sort_keys=False), encoding="utf-8",
        )

    def _db(self) -> Database:
        db = Database(self.project.db_path)
        self.addCleanup(db.close)
        return db

    def _import_and_compile(self) -> None:
        self.assertEqual(main([
            "--project", str(self.root), "taxonomy", "import",
            "--input", str(self.taxonomy_path), "--version", "test.1",
        ]), 0)
        self.assertEqual(main([
            "--project", str(self.root), "taxonomy", "compile",
            "--profile", "plants_v1", "--taxonomy-version", "test.1",
        ]), 0)

    def test_compile_is_idempotent_and_profile_filters_define_denominator(self):
        self._import_and_compile()
        db = self._db()
        reference = self.project.taxonomy_reference_sets_dir / "plants_v1@test.1.tsv"
        rows = read_tsv(reference)
        self.assertEqual(
            [(row["rank"], int(row["taxid"]), row["scientific_name"]) for row in rows],
            [
                ("family", 10, "Plantaceae"),
                ("family", 40, "Missingaceae"),
                ("genus", 11, "Alpha"),
                ("genus", 41, "Missinggenus"),
            ],
        )
        first_changes = db.query(
            "SELECT COUNT(*) AS n FROM changes WHERE object_type='taxonomy_reference_set'"
        )[0]["n"]
        before = reference.read_bytes()
        self.assertEqual(main([
            "--project", str(self.root), "taxonomy", "compile",
            "--profile", "plants_v1", "--taxonomy-version", "test.1",
        ]), 0)
        self.assertEqual(reference.read_bytes(), before)
        self.assertEqual(
            db.query("SELECT COUNT(*) AS n FROM changes WHERE object_type='taxonomy_reference_set'")[0]["n"],
            first_changes,
        )
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM taxonomy_snapshots")[0]["n"], 1)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM taxonomy_reference_sets")[0]["n"], 1)

        profile_path = self.project.profiles_dir / "plants_v1.yaml"
        changed_profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        changed_profile["thresholds"]["family"]["min_coverage_percent"] = 51
        profile_path.write_text(
            yaml.safe_dump(changed_profile, sort_keys=False), encoding="utf-8"
        )
        self.assertEqual(main([
            "--project", str(self.root), "taxonomy", "compile",
            "--profile", "plants_v1", "--taxonomy-version", "test.1",
        ]), 2)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM taxonomy_reference_sets")[0]["n"], 1)

        self.taxonomy_path.write_text(
            self.taxonomy_path.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        self.assertEqual(main([
            "--project", str(self.root), "taxonomy", "import",
            "--input", str(self.taxonomy_path), "--version", "test.1",
        ]), 2)
        self.assertEqual(db.query("SELECT COUNT(*) AS n FROM taxonomy_snapshots")[0]["n"], 1)

        self.assertEqual(main([
            "--project", str(self.root), "taxonomy", "compile",
            "--profile", "assembly_production_v1", "--taxonomy-version", "test.1",
        ]), 2)
        self.assertGreaterEqual(
            db.query(
                "SELECT COUNT(*) AS n FROM workflow_runs "
                "WHERE step='taxonomy_compile' AND status='failed'"
            )[0]["n"],
            2,
        )

    def test_official_taxdump_archive_imports_nodes_merged_and_deleted_taxids(self):
        taxdump = self.root / "taxdump.tar.gz"
        _write_taxdump(taxdump)
        self.assertEqual(main([
            "--project", str(self.root), "taxonomy", "import",
            "--input", str(taxdump), "--version", "dump.1",
        ]), 0)
        db = self._db()
        snapshot = db.query(
            "SELECT taxonomy_snapshot_id, node_count FROM taxonomy_snapshots "
            "WHERE taxonomy_version='dump.1'"
        )[0]
        self.assertEqual(snapshot["node_count"], len(_taxonomy_records()))
        aliases = db.query(
            "SELECT alias_taxid, current_taxid, status FROM taxonomy_aliases "
            "WHERE taxonomy_snapshot_id=? ORDER BY alias_taxid",
            (snapshot["taxonomy_snapshot_id"],),
        )
        self.assertEqual([dict(row) for row in aliases], [
            {"alias_taxid": 1200, "current_taxid": 12, "status": "merged"},
            {"alias_taxid": 9999, "current_taxid": None, "status": "deleted"},
        ])
        self.assertEqual(main([
            "--project", str(self.root), "taxonomy", "compile",
            "--profile", "plants_v1", "--taxonomy-version", "dump.1",
        ]), 2)
        self.assertEqual(
            db.query(
                "SELECT COUNT(*) AS n FROM workflow_runs "
                "WHERE step='taxonomy_compile' AND status='failed'"
            )[0]["n"],
            1,
        )
        profile_path = self.project.profiles_dir / "plants_v1.yaml"
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        profile["filters"]["exclude_extinct"] = False
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        self.assertEqual(main([
            "--project", str(self.root), "taxonomy", "compile",
            "--profile", "plants_v1", "--taxonomy-version", "dump.1",
        ]), 0)
        rows = read_tsv(
            self.project.taxonomy_reference_sets_dir / "plants_v1@dump.1.tsv"
        )
        self.assertIn(("family", "20", "Fossilaceae"), {
            (row["rank"], row["taxid"], row["scientific_name"]) for row in rows
        })

    def test_metadata_and_frozen_release_scopes(self):
        self._import_and_compile()
        db = self._db()
        db.insert_row("organisms", {
            "organism_id": "ORG_000001", "scientific_name": "Alpha one",
            "taxon_id": 1200, "taxonomy_source": "NCBI",
        })
        db.insert_row("organisms", {
            "organism_id": "ORG_000002", "scientific_name": "Missinggenus one",
            "taxon_id": 42, "taxonomy_source": "NCBI",
        })
        db.insert_row("organisms", {
            "organism_id": "ORG_000003", "scientific_name": "Unsupported",
            "taxon_id": 12, "taxonomy_source": "GTDB",
        })
        db.insert_row("organisms", {
            "organism_id": "ORG_000004", "scientific_name": "No taxid",
            "taxonomy_source": "NCBI",
        })
        db.insert_row("organisms", {
            "organism_id": "ORG_000005", "scientific_name": "Fossilus",
            "taxon_id": 21, "taxonomy_source": "NCBI",
        })
        db.insert_row("organisms", {
            "organism_id": "ORG_000006", "scientific_name": "Taxonomy root",
            "taxon_id": 1, "taxonomy_source": "NCBI",
        })
        db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
        db.insert_row("assemblies", {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_level": "contig", "assembly_version": 1,
        })
        fasta = self.root / "alpha.fa"
        fasta.write_text(">ctg\n" + "ACGT" * 700 + "\n", encoding="utf-8")
        archived = ingest_file(db, self.project, fasta, "assembly", "ASM_000001", "genome_fasta")
        self.assertTrue(qc_file(db, self.project, archived["file_id"])["ok"])
        self.assertEqual(
            evaluate_entity(db, self.project, "assembly", "ASM_000001", "assembly_production_v1")["decision"],
            "PASS",
        )

        self.assertEqual(main([
            "--project", str(self.root), "report", "coverage",
            "--reference-set", "plants_v1@test.1",
        ]), 0)
        metadata_report = db.query(
            "SELECT report_id, relative_path, result_sha256 FROM coverage_reports "
            "WHERE scope_kind='metadata'"
        )[0]
        self.assertEqual(main([
            "--project", str(self.root), "report", "coverage",
            "--reference-set", "plants_v1@test.1",
        ]), 0)
        self.assertEqual(
            db.query("SELECT COUNT(*) AS n FROM coverage_reports WHERE scope_kind='metadata'")[0]["n"],
            1,
        )
        self.assertEqual(
            db.query(
                "SELECT report_id, relative_path, result_sha256 FROM coverage_reports "
                "WHERE scope_kind='metadata'"
            )[0],
            metadata_report,
        )
        metadata_metric = db.query(
            "SELECT rank, numerator, denominator FROM coverage_report_metrics ORDER BY rank"
        )
        self.assertEqual({row["rank"]: (row["numerator"], row["denominator"]) for row in metadata_metric}, {
            "family": (2, 2), "genus": (2, 2),
        })
        report_row = db.query("SELECT relative_path FROM coverage_reports WHERE scope_kind='metadata'")[0]
        excluded = read_tsv(self.root / report_row["relative_path"] / "coverage_excluded_observations.tsv")
        self.assertEqual(
            {row["reason"] for row in excluded},
            {"UNSUPPORTED_TAXONOMY_SOURCE", "MISSING_TAXID", "EXCLUDED_EXTINCT", "MISSING_TARGET_RANK"},
        )

        release = create_release(db, self.project, "scope-test", "assembly_production_v1")
        self.assertTrue(Path(release["path"]).is_dir())
        db.conn.execute("UPDATE organisms SET taxon_id=42 WHERE organism_id='ORG_000001'")
        db.conn.commit()
        self.assertEqual(main([
            "--project", str(self.root), "report", "coverage",
            "--reference-set", "plants_v1@test.1", "--release", "scope-test",
        ]), 1)
        release_metrics = db.query(
            "SELECT m.rank, m.numerator, m.denominator FROM coverage_report_metrics m "
            "JOIN coverage_reports r ON r.report_id=m.report_id WHERE r.scope_kind='release'"
        )
        self.assertEqual({row["rank"]: (row["numerator"], row["denominator"]) for row in release_metrics}, {
            "family": (1, 2), "genus": (1, 2),
        })
        release_report = db.query(
            "SELECT relative_path FROM coverage_reports WHERE scope_kind='release'"
        )[0]
        observations = read_tsv(self.root / release_report["relative_path"] / "coverage_observations.tsv")
        self.assertEqual(len(observations), 1)
        self.assertEqual(int(observations[0]["resolved_taxid"]), 12)
        self.assertEqual(observations[0]["mapping_status"], "MAPPED_ALIAS")
        frozen_organisms = Path(release["path"]) / "organisms.tsv"
        frozen_organisms.write_text(
            frozen_organisms.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        self.assertEqual(main([
            "--project", str(self.root), "report", "coverage",
            "--reference-set", "plants_v1@test.1", "--release", "scope-test",
        ]), 2)
        self.assertEqual(
            db.query(
                "SELECT COUNT(*) AS n FROM workflow_runs "
                "WHERE step='coverage_report' AND status='failed'"
            )[0]["n"],
            1,
        )
