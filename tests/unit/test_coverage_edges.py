"""Failure and exclusion branches for deterministic taxonomy coverage reports."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from operon import coverage, taxonomy
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ConflictError, ValidationError
from operon.schema import write_tsv
from operon.taxonomy import canonical_document
from operon.utils import sha256_file


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _ReferenceConnection:
    def __init__(self, reference, snapshot):
        self.reference = reference
        self.snapshot = snapshot

    def execute(self, sql, _params=()):
        if "taxonomy_reference_sets" in sql:
            return _Result(self.reference)
        return _Result(self.snapshot)


def _reference_fixture(tmp_path: Path, rows: list[dict[str, object]]):
    path = tmp_path / "reference.tsv"
    write_tsv(path, ["rank", "taxid", "scientific_name"], rows)
    profile = {}
    document, profile_sha = canonical_document(profile)
    reference = {
        "reference_set_id": "REF_1",
        "relative_path": "reference.tsv",
        "tsv_sha256": sha256_file(path),
        "tsv_size_bytes": path.stat().st_size,
        "profile_document": document,
        "profile_sha256": profile_sha,
        "profile_name": "test",
        "family_count": sum(row["rank"] == "family" for row in rows),
        "genus_count": sum(row["rank"] == "genus" for row in rows),
        "taxonomy_snapshot_id": "TAX_1",
        "taxonomy_version": "v1",
    }
    snapshot = {"source_sha256": "a" * 64}
    project = SimpleNamespace(root=tmp_path)
    return path, reference, snapshot, project


def test_load_reference_set_happy_path_and_missing_record(tmp_path, monkeypatch):
    _path, reference, snapshot, project = _reference_fixture(tmp_path, [
        {"rank": "family", "taxid": 10, "scientific_name": "F"},
        {"rank": "genus", "taxid": 20, "scientific_name": "G"},
    ])
    monkeypatch.setattr(coverage, "_validate_coverage_profile", lambda *_: {
        "ranks": ["family", "genus"]
    })
    monkeypatch.setattr(coverage, "_validate_reference_provenance", lambda *_a, **_k: None)
    loaded = coverage._load_reference_set(
        SimpleNamespace(conn=_ReferenceConnection(reference, snapshot)), project, "REF_1"
    )
    assert [row["rank"] for row in loaded[1]] == ["family", "genus"]
    with pytest.raises(ValidationError, match="not found"):
        coverage._load_reference_set(
            SimpleNamespace(conn=_ReferenceConnection(None, snapshot)), project, "MISSING"
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing_file", "file is missing"),
        ("checksum", "checksum or size mismatch"),
        ("unsupported_rank", "unsupported rank"),
        ("invalid_taxid", "invalid TaxID"),
        ("duplicate", "duplicate target"),
        ("profile", "inconsistent frozen profile"),
        ("empty_rank", "denominator is empty"),
        ("count", "row count does not match"),
        ("snapshot", "is unavailable"),
    ],
)
def test_load_reference_set_validation_cases(tmp_path, monkeypatch, case, message):
    rows = [
        {"rank": "family", "taxid": 10, "scientific_name": "F"},
        {"rank": "genus", "taxid": 20, "scientific_name": "G"},
    ]
    if case == "unsupported_rank":
        rows[0]["rank"] = "species"
    if case == "invalid_taxid":
        rows[0]["taxid"] = "bad"
    if case == "duplicate":
        rows[1] = dict(rows[0])
    path, reference, snapshot, project = _reference_fixture(tmp_path, rows)
    parsed = {"ranks": ["family", "genus"]}
    if case == "empty_rank":
        parsed = {"ranks": ["family", "genus"]}
        rows = [{"rank": "family", "taxid": 10, "scientific_name": "F"}]
        path, reference, snapshot, project = _reference_fixture(tmp_path, rows)
        reference["genus_count"] = 0
    if case == "missing_file":
        path.unlink()
    elif case == "checksum":
        path.write_text("changed", encoding="utf-8")
    elif case == "profile":
        reference["profile_sha256"] = "bad"
    elif case == "count":
        reference["family_count"] = 99
    elif case == "snapshot":
        snapshot = None
    monkeypatch.setattr(coverage, "_validate_coverage_profile", lambda *_: parsed)
    monkeypatch.setattr(coverage, "_validate_reference_provenance", lambda *_a, **_k: None)
    with pytest.raises(ValidationError, match=message):
        coverage._load_reference_set(
            SimpleNamespace(conn=_ReferenceConnection(reference, snapshot)), project, "REF_1"
        )


def test_observation_resolution_covers_every_exclusion_reason(monkeypatch):
    observations = [
        {"organism_id": "O1", "taxonomy_source": "GTDB", "taxon_id": 1},
        {"organism_id": "O2", "taxonomy_source": "NCBI", "taxon_id": None},
        {"organism_id": "O3", "taxonomy_source": "NCBI", "taxon_id": "bad"},
        *[
            {"organism_id": f"O{taxid}", "scientific_name": f"name{taxid}",
             "taxonomy_source": "NCBI", "taxon_id": taxid}
            for taxid in range(4, 12)
        ],
    ]
    resolution = {
        4: (None, "DELETED_TAXID"), 5: (50, "EXACT"), 6: (60, "EXACT"),
        7: (70, "EXACT"), 8: (80, "EXACT"), 9: (90, "EXACT"),
        10: (100, "MAPPED_ALIAS"), 11: (110, "EXACT"),
    }
    lineages = {
        50: [],
        60: [{"taxid": 60, "rank": "species", "scientific_name": "Extinct", "is_extinct": 1}],
        70: [{"taxid": 70, "rank": "species", "scientific_name": "uncultured thing", "is_extinct": 0}],
        80: [{"taxid": 999, "rank": "order", "scientific_name": "Excluded", "is_extinct": 0}],
        90: [{"taxid": 90, "rank": "species", "scientific_name": "No ranks", "is_extinct": 0}],
        100: [{"taxid": 9999, "rank": "family", "scientific_name": "Outside", "is_extinct": 0}],
        110: [
            {"taxid": 20, "rank": "genus", "scientific_name": "G", "is_extinct": 0},
            {"taxid": 10, "rank": "family", "scientific_name": "F", "is_extinct": 0},
        ],
    }
    monkeypatch.setattr(coverage, "_resolve_taxids", lambda *_: resolution)
    monkeypatch.setattr(coverage, "_lineages", lambda *_: lineages)
    accepted, excluded, observed = coverage._resolve_observations(
        SimpleNamespace(), "TAX_1", observations,
        [
            {"rank": "family", "taxid": 10, "scientific_name": "F"},
            {"rank": "genus", "taxid": 20, "scientific_name": "G"},
        ],
        {
            "exclude_subtrees": [999], "exclude_extinct": True,
            "compiled_name_patterns": [re.compile("uncultured")],
            "ranks": ["family", "genus"],
        },
    )
    assert accepted[0]["organism_id"] == "O11"
    assert accepted[0]["mapping_status"] == "EXACT"
    assert observed[("family", 10)] == {"O11"}
    assert observed[("genus", 20)] == {"O11"}
    assert {row["reason"] for row in excluded} == {
        "UNSUPPORTED_TAXONOMY_SOURCE", "MISSING_TAXID", "INVALID_TAXID",
        "DELETED_TAXID", "UNKNOWN_RESOLVED_TAXID", "EXCLUDED_EXTINCT",
        "EXCLUDED_NAME_PATTERN", "EXCLUDED_SUBTREE", "MISSING_TARGET_RANK",
        "OUTSIDE_REFERENCE_SCOPE",
    }


def test_percentage_and_cached_report_missing_files(tmp_path):
    assert coverage._percentage(1, 6) == 16.6667
    with pytest.raises(ValidationError, match="greater than zero"):
        coverage._percentage(1, 0)
    with pytest.raises(ConflictError, match="files are missing"):
        coverage._cached_report(
            SimpleNamespace(), SimpleNamespace(root=tmp_path),
            {"relative_path": "missing"},
        )


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _ReleaseConnection:
    def __init__(self, release, members):
        self.release = release
        self.members = members

    def execute(self, sql, _params=()):
        if "FROM releases" in sql:
            return _Rows([self.release] if self.release else [])
        if "FROM release_members" in sql:
            return _Rows(self.members)
        raise AssertionError(sql)


def _release_fixture(tmp_path, members):
    root = tmp_path / "release"
    root.mkdir()
    write_tsv(root / "manifest.tsv", [
        "file_id", "entity_type", "entity_id", "sha256", "size_bytes"
    ], members)
    tables = {
        "organisms": [{"organism_id": "ORG_1", "scientific_name": "O", "taxon_id": 1,
                       "taxonomy_source": "NCBI"}],
        "samples": [{"sample_id": "SMP_1", "organism_id": "ORG_1"}],
        "runs": [{"run_id": "RUN_1", "sample_id": "SMP_1"}],
        "assemblies": [{"assembly_id": "ASM_1", "sample_id": "SMP_1"}],
        "annotations": [{"annotation_id": "ANN_1", "assembly_id": "ASM_1"}],
    }
    hashes = {}
    for table, rows in tables.items():
        columns = list(rows[0])
        path = root / f"{table}.tsv"
        write_tsv(path, columns, rows)
        hashes[f"{table}.tsv"] = sha256_file(path)
    release = {
        "version": "v1", "path": str(root),
        "manifest_sha256": sha256_file(root / "manifest.tsv"),
        "summary": __import__("json").dumps({"metadata_sha256": hashes}),
    }
    project = SimpleNamespace(root=tmp_path)
    db = SimpleNamespace(conn=_ReleaseConnection(release, members))
    return root, release, project, db


def test_release_scope_all_entity_traces(tmp_path):
    members = [
        {"file_id": "F1", "entity_type": "organism", "entity_id": "ORG_1", "sha256": "a", "size_bytes": 1},
        {"file_id": "F2", "entity_type": "sample", "entity_id": "SMP_1", "sha256": "b", "size_bytes": 1},
        {"file_id": "F3", "entity_type": "run", "entity_id": "RUN_1", "sha256": "c", "size_bytes": 1},
        {"file_id": "F4", "entity_type": "assembly", "entity_id": "ASM_1", "sha256": "d", "size_bytes": 1},
        {"file_id": "F5", "entity_type": "annotation", "entity_id": "ANN_1", "sha256": "e", "size_bytes": 1},
    ]
    _root, _release, project, db = _release_fixture(tmp_path, members)
    observations, membership, details = coverage._release_scope(db, project, "v1")
    assert len(observations) == 1 and membership
    assert observations[0]["file_ids"] == "F1;F2;F3;F4;F5"
    assert details["release_member_count"] == 5


def test_release_scope_early_validation_failures(tmp_path):
    project = SimpleNamespace(root=tmp_path)
    with pytest.raises(ValidationError, match="not found"):
        coverage._release_scope(
            SimpleNamespace(conn=_ReleaseConnection(None, [])), project, "missing"
        )
    release = {"path": str(tmp_path / "missing"), "manifest_sha256": "x", "summary": "{}"}
    with pytest.raises(ValidationError, match="directory is missing"):
        coverage._release_scope(
            SimpleNamespace(conn=_ReleaseConnection(release, [])), project, "v1"
        )

    root, release, project, db = _release_fixture(tmp_path, [])
    (root / "manifest.tsv").write_text("changed", encoding="utf-8")
    with pytest.raises(ValidationError, match="manifest checksum mismatch"):
        coverage._release_scope(db, project, "v1")


def test_release_scope_manifest_summary_metadata_and_member_failures(tmp_path):
    member = {"file_id": "F1", "entity_type": "organism", "entity_id": "ORG_1", "sha256": "a", "size_bytes": 1}
    root, release, project, db = _release_fixture(tmp_path, [member])
    db.conn.members = [{**member, "sha256": "different"}]
    with pytest.raises(ValidationError, match="manifest and release_members disagree"):
        coverage._release_scope(db, project, "v1")
    db.conn.members = [member]
    release["summary"] = "not-json"
    with pytest.raises(ValidationError, match="invalid summary provenance"):
        coverage._release_scope(db, project, "v1")
    release["summary"] = "{}"
    with pytest.raises(ValidationError, match="predates frozen metadata"):
        coverage._release_scope(db, project, "v1")

    import json
    release["summary"] = json.dumps({"metadata_sha256": {}})
    (root / "organisms.tsv").unlink()
    with pytest.raises(ValidationError, match="metadata snapshot is missing"):
        coverage._release_scope(db, project, "v1")


def test_release_scope_rejects_unsupported_and_untraceable_members(tmp_path):
    unsupported = {"file_id": "F1", "entity_type": "taxonomy", "entity_id": "T1", "sha256": "a", "size_bytes": 1}
    _root, _release, project, db = _release_fixture(tmp_path, [unsupported])
    with pytest.raises(ValidationError, match="unsupported entity type"):
        coverage._release_scope(db, project, "v1")

    # A syntactically accepted entity type still has to resolve through frozen metadata.
    tmp2 = tmp_path / "second"
    tmp2.mkdir()
    untraceable = {"file_id": "F2", "entity_type": "sample", "entity_id": "SMP_MISSING", "sha256": "b", "size_bytes": 1}
    _root, _release, project, db = _release_fixture(tmp2, [untraceable])
    with pytest.raises(ValidationError, match="cannot be traced"):
        coverage._release_scope(db, project, "v1")


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def test_resolve_taxids_classifies_exact_alias_deleted_and_unknown(project_db):
    _project, db = project_db
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute(
        "INSERT INTO taxonomy_snapshots(taxonomy_snapshot_id, source, taxonomy_version, "
        "source_file_id, source_sha256, source_size_bytes, node_count, status, imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("TAX_000001", "NCBI", "v", "F", "sha", 1, 2, "READY", "now"),
    )
    db.conn.executemany(
        "INSERT INTO taxonomy_nodes(taxonomy_snapshot_id,taxid,parent_taxid,rank,scientific_name,is_extinct) "
        "VALUES(?,?,?,?,?,?)",
        [
            ("TAX_000001", 1, 1, "no_rank", "root", 0),
            ("TAX_000001", 5, 1, "genus", "Gen", 0),
        ],
    )
    db.conn.executemany(
        "INSERT INTO taxonomy_aliases(taxonomy_snapshot_id,alias_taxid,current_taxid,status) "
        "VALUES(?,?,?,?)",
        [
            ("TAX_000001", 7, 5, "merged"),
            ("TAX_000001", 8, None, "deleted"),
        ],
    )
    assert coverage._resolve_taxids(db, "TAX_000001", [1, 7, 8, 42]) == {
        1: (1, "EXACT"),
        7: (5, "MAPPED_ALIAS"),
        8: (None, "DELETED_TAXID"),
        42: (None, "UNKNOWN_TAXID"),
    }


def _metadata_coverage_report(project, db, tmp_path: Path) -> dict:
    """Import a minimal taxonomy, compile a zero-threshold reference set, report once."""
    source = tmp_path / "taxonomy.jsonl"
    records = [
        {"taxId": 1, "rank": "no rank", "taxName": "root"},
        {"taxId": 10, "parents": [1], "rank": "family", "taxName": "Fam"},
        {"taxId": 20, "parents": [10], "rank": "genus", "taxName": "Gen"},
    ]
    source.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    taxonomy.import_ncbi_taxonomy(db, project, source, "cov.1")
    profile = {
        "kind": "taxonomy_coverage",
        "version": 1,
        "name": "cov",
        "taxonomy": {"source": "NCBI"},
        "scope": {"root_taxids": [1]},
        "targets": {"ranks": ["family", "genus"]},
        "thresholds": {
            "family": {"min_coverage_percent": 0},
            "genus": {"min_coverage_percent": 0},
        },
    }
    (project.profiles_dir / "cov.yaml").write_text(
        yaml.safe_dump(profile, sort_keys=False), encoding="utf-8"
    )
    reference = taxonomy.compile_reference_set(db, project, "cov", "cov.1")
    return coverage.report_coverage(db, project, reference["reference_set_id"])


def test_cached_report_rejects_modified_result_bytes(project_db, tmp_path):
    project, db = project_db
    report = _metadata_coverage_report(project, db, tmp_path)
    summary = Path(report["path"]) / "coverage_summary.tsv"
    summary.write_bytes(summary.read_bytes() + b"tampered\n")
    with pytest.raises(ConflictError, match="cached coverage report has changed"):
        coverage.report_coverage(db, project, report["reference_set_id"])


def test_cached_report_rejects_modified_provenance(project_db, tmp_path):
    project, db = project_db
    report = _metadata_coverage_report(project, db, tmp_path)
    provenance_path = Path(report["path"]) / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["decision"] = "FAIL"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    with pytest.raises(ConflictError, match="cached coverage report has changed"):
        coverage.report_coverage(db, project, report["reference_set_id"])


def test_report_rejects_leftover_directory_without_matching_history(project_db, tmp_path):
    project, db = project_db
    report = _metadata_coverage_report(project, db, tmp_path)
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute("DELETE FROM coverage_report_metrics")
    db.conn.execute("DELETE FROM coverage_reports")
    with pytest.raises(ConflictError, match="already exists without matching history"):
        coverage.report_coverage(db, project, report["reference_set_id"])
    assert db.query("SELECT COUNT(*) AS n FROM coverage_reports")[0]["n"] == 0
    assert Path(report["path"]).is_dir()
