"""Validation and archive edge cases for frozen taxonomy snapshots."""

from __future__ import annotations

import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

from operon import taxonomy
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ValidationError


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def valid_profile():
    return {
        "kind": "taxonomy_coverage",
        "version": 1,
        "name": "plants",
        "taxonomy": {"source": "NCBI"},
        "scope": {"root_taxids": [2, 1, 2]},
        "targets": {"ranks": ["genus", "family"]},
        "filters": {
            "exclude_extinct": False,
            "exclude_subtrees": [9],
            "exclude_name_patterns": ["unclassified"],
        },
        "thresholds": {
            "family": {"min_coverage_percent": 50},
            "genus": {"min_coverage_percent": 60},
        },
    }


def test_tokens_ids_suffixes_and_scalar_parsing(project_db, tmp_path):
    _project, db = project_db
    assert taxonomy._safe_token("v1.2_test-x", "version") == "v1.2_test-x"
    with pytest.raises(ValidationError, match="invalid version"):
        taxonomy._safe_token("bad/value", "version")
    assert taxonomy._next_snapshot_id(db) == "TAX_000001"
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute(
        "INSERT INTO taxonomy_snapshots(taxonomy_snapshot_id, source, taxonomy_version, "
        "source_file_id, source_sha256, source_size_bytes, node_count, status, imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("TAX_000009", "NCBI", "v", "F", "sha", 1, 1, "READY", "now"),
    )
    assert taxonomy._next_snapshot_id(db) == "TAX_000010"
    assert taxonomy._source_suffix(Path("x.tar.gz")) == ".tar.gz"
    assert taxonomy._source_suffix(Path("x.tar.bz2")) == ".tar.bz2"
    assert taxonomy._source_suffix(Path("x")) == ".dat"
    assert taxonomy._pick({"a": None, "b": 0}, "a", "b") == 0
    assert taxonomy._pick({}, "a") is None
    assert taxonomy._taxid({"taxId": "2"}) == 2
    assert taxonomy._taxid({"id": "bad"}) is None
    assert taxonomy._taxid("") is None
    assert taxonomy._taxids(None) == []
    assert taxonomy._taxids([1, "bad", {"id": 2}]) == [1, 2]
    assert taxonomy._scientific_name({"currentScientificName": {"text": " Name "}}) == "Name"
    assert taxonomy._scientific_name({}) == ""
    assert taxonomy._unwrap_taxonomy_record({"taxonomy": {"taxId": 1}}) == {"taxId": 1}
    assert taxonomy._unwrap_taxonomy_record({"taxonomyNode": []}) == {"taxonomyNode": []}


def test_iter_and_normalize_taxonomy_records_reject_bad_input():
    rows = list(taxonomy._iter_taxonomy_records(
        io.StringIO('\n{"taxonomy_node":{"taxId":1}}\n'), "source"
    ))
    assert rows == [{"taxId": 1}]
    with pytest.raises(ValidationError, match="invalid taxonomy JSON on line 1"):
        list(taxonomy._iter_taxonomy_records(io.StringIO("bad\n"), "source"))
    with pytest.raises(ValidationError, match="is not an object"):
        list(taxonomy._iter_taxonomy_records(io.StringIO("[]\n"), "source"))
    with pytest.raises(ValidationError, match="no valid taxId"):
        taxonomy._normalized_node({}, "source")
    with pytest.raises(ValidationError, match="no current scientific name"):
        taxonomy._normalized_node({"taxId": 2}, "source")

    root, aliases = taxonomy._normalized_node({
        "taxId": 1, "rank": "no rank", "taxName": "root",
        "secondaryTaxIds": [1, 10, 10], "isFormal": False,
    }, "source")
    assert root["parent_taxid"] == 1
    assert root["rank"] == "no_rank"
    assert root["is_formal"] == 0
    assert aliases == [{"alias_taxid": 10, "current_taxid": 1, "status": "secondary"}]
    child, _ = taxonomy._normalized_node({
        "tax_id": "2", "parents": [1], "scientificName": "child", "extinct": True,
    }, "source")
    assert child["parent_taxid"] == 1 and child["is_extinct"] == 1
    assert taxonomy._dmp_fields("1\t|\t2\t|\n", "x", 1, 2)[:2] == ["1", "2"]
    with pytest.raises(ValidationError, match="malformed NCBI taxdump"):
        taxonomy._dmp_fields("one", "x", 1, 3)


def _tar(path: Path, files: dict[str, str]):
    with tarfile.open(path, "w:gz") as archive:
        for name, text in files.items():
            payload = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_archive_detection_and_text_contexts(tmp_path):
    package = tmp_path / "taxonomy.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("nested/taxonomy_report.jsonl", '{"taxId":1}\n')
    assert taxonomy._archive_member_basenames(package) == {"taxonomy_report.jsonl"}
    assert taxonomy._taxonomy_source_format(package) == "ncbi_datasets_jsonl"
    with taxonomy._taxonomy_text(package) as handle:
        assert "taxId" in handle.read()
    with pytest.raises(ValidationError, match="member nodes.dmp not found"):
        with taxonomy._archive_text_member(package, "nodes.dmp"):
            pass

    dump = tmp_path / "taxdump.tar.gz"
    _tar(dump, {"nested/nodes.dmp": "1 | 1 | no rank |\n", "names.dmp": "1 | root | | scientific name |\n"})
    assert taxonomy._taxonomy_source_format(dump) == "ncbi_taxdump"
    with taxonomy._archive_text_member(dump, "nodes.dmp") as handle:
        assert handle.readline().startswith("1")
    direct = tmp_path / "direct.jsonl"
    direct.write_text('{"taxId":1}\n', encoding="utf-8")
    with taxonomy._taxonomy_text(direct) as handle:
        assert "taxId" in handle.read()


def test_archive_missing_members_and_unknown_formats(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("README", "x")
    with pytest.raises(ValidationError, match="expected nodes.dmp"):
        taxonomy._taxonomy_source_format(bad)
    with pytest.raises(ValidationError, match="no taxonomy_report"):
        with taxonomy._taxonomy_text(bad):
            pass
    bad_tar = tmp_path / "bad.tar.gz"
    _tar(bad_tar, {"README": "x"})
    with pytest.raises(ValidationError, match="no taxonomy_report"):
        with taxonomy._taxonomy_text(bad_tar):
            pass
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    assert taxonomy._archive_member_basenames(plain) == set()
    with pytest.raises(ValidationError, match="must be a ZIP or tar"):
        with taxonomy._archive_text_member(plain, "x"):
            pass


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda p: p.update(version="bad"), "version must be"),
        (lambda p: p.update(version=0), "version must be"),
        (lambda p: p.update(name="other"), "does not match filename"),
        (lambda p: p.update(taxonomy=[1]), "taxonomy must be a mapping"),
        (lambda p: p["taxonomy"].update(source="GTDB"), "require taxonomy.source"),
        (lambda p: p.update(scope=[1]), "scope must be a mapping"),
        (lambda p: p["scope"].update(root_taxids=[]), "non-empty list"),
        (lambda p: p["scope"].update(root_taxids=["x"]), "must be integers"),
        (lambda p: p["scope"].update(root_taxids=[0]), "positive integers"),
        (lambda p: p.update(targets=[1]), "targets must be a mapping"),
        (lambda p: p["targets"].update(ranks=[]), "non-empty list"),
        (lambda p: p["targets"].update(ranks=["family", "family"]), "must be unique"),
        (lambda p: p.update(filters=[1]), "filters must be a mapping"),
        (lambda p: p["filters"].update(exclude_subtrees=["x"]), "integer TaxIDs"),
        (lambda p: p["filters"].update(exclude_subtrees=[-1]), "positive TaxIDs"),
        (lambda p: p["filters"].update(exclude_extinct="yes"), "true or false"),
        (lambda p: p["filters"].update(exclude_name_patterns="x"), "list of regular expressions"),
        (lambda p: p["filters"].update(exclude_name_patterns=["["]), "invalid coverage exclusion"),
        (lambda p: p.update(thresholds=[1]), "thresholds must be a mapping"),
        (lambda p: p["thresholds"].pop("genus"), "exactly the configured"),
        (lambda p: p["thresholds"].update(family=[1]), "thresholds.family must be a mapping"),
        (lambda p: p["thresholds"]["family"].update(min_coverage_percent="x"), "must be numeric"),
        (lambda p: p["thresholds"]["family"].update(min_coverage_percent=101), "between 0 and 100"),
        (lambda p: p["thresholds"]["family"].update(min_coverage_percent=float("nan")), "between 0 and 100"),
    ],
)
def test_coverage_profile_validation_errors(mutate, message):
    profile = valid_profile()
    mutate(profile)
    with pytest.raises(ValidationError, match=message):
        taxonomy._validate_coverage_profile("plants", profile)


def test_valid_coverage_profile_is_normalized():
    parsed = taxonomy._validate_coverage_profile("plants", valid_profile())
    assert parsed["root_taxids"] == [1, 2]
    assert parsed["ranks"] == ["family", "genus"]
    assert parsed["thresholds"] == {"family": 50.0, "genus": 60.0}
    assert parsed["compiled_name_patterns"][0].search("unclassified thing")


def test_descendant_targets_validates_roots_exclusions_and_extinct_data(project_db):
    _project, db = project_db
    db.conn.execute("PRAGMA foreign_keys=OFF")
    db.conn.execute(
        "INSERT INTO taxonomy_snapshots(taxonomy_snapshot_id, source, taxonomy_version, "
        "source_file_id, source_sha256, source_size_bytes, node_count, status, imported_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("TAX_000001", "NCBI", "v", "F", "sha", 1, 4, "READY", "now"),
    )
    db.conn.executemany(
        "INSERT INTO taxonomy_nodes(taxonomy_snapshot_id,taxid,parent_taxid,rank,scientific_name,is_extinct) "
        "VALUES(?,?,?,?,?,?)",
        [
            ("TAX_000001", 1, 1, "no_rank", "root", 0),
            ("TAX_000001", 2, 1, "family", "Family", 0),
            ("TAX_000001", 3, 2, "genus", "Genus", 0),
            ("TAX_000001", 4, 2, "genus", "Extinct", 1),
        ],
    )
    with pytest.raises(ValidationError, match="root TaxID"):
        taxonomy._descendant_targets(db, "TAX_000001", [99], [], ["family"], False)
    with pytest.raises(ValidationError, match="excluded subtree"):
        taxonomy._descendant_targets(db, "TAX_000001", [1], [99], ["family"], False)
    rows = taxonomy._descendant_targets(db, "TAX_000001", [1], [3], ["family", "genus"], False)
    assert {(row["rank"], row["taxid"]) for row in rows} == {("family", 2), ("genus", 4)}
    rows = taxonomy._descendant_targets(db, "TAX_000001", [1], [], ["family", "genus"], True)
    assert {(row["rank"], row["taxid"]) for row in rows} == {("family", 2), ("genus", 3)}
    db.conn.execute("UPDATE taxonomy_nodes SET is_extinct=NULL WHERE taxid=3")
    with pytest.raises(ValidationError, match="has no complete extinct annotation"):
        taxonomy._descendant_targets(db, "TAX_000001", [1], [], ["genus"], True)


def test_schema_upgrade_rejects_invalid_documents(project_db):
    project, _db = project_db
    project.schema_path.write_text("tables: []\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="cannot upgrade"):
        taxonomy._ensure_taxonomy_metadata_schema(project)
    project.schema_path.write_text(yaml.safe_dump({
        "schema_version": "1.0",
        "tables": {"files": {"fields": {
            "entity_type": {"allowed": []},
            "entity_id": {"pattern": "["},
            "file_role": {"allowed": []},
        }}},
    }), encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid files.entity_id pattern"):
        taxonomy._ensure_taxonomy_metadata_schema(project)


def test_taxdump_import_with_merged_and_deleted_aliases(project_db, tmp_path):
    project, db = project_db
    package = tmp_path / "taxdump.tar.gz"
    _tar(package, {
        "nodes.dmp": (
            "1 | 1 | no rank |\n"
            "2 | 1 | family |\n"
            "3 | 2 | genus |\n"
        ),
        "names.dmp": (
            "1 | root | | scientific name |\n"
            "1 | all | | synonym |\n"
            "2 | Family | | scientific name |\n"
            "3 | Genus | | scientific name |\n"
        ),
        "merged.dmp": "99 | 3 |\n",
        "delnodes.dmp": "100 |\n",
    })
    result = taxonomy.import_ncbi_taxonomy(db, project, package, "taxdump-1")
    assert result["node_count"] == 3 and result["source_format"] == "ncbi_taxdump"
    aliases = db.query(
        "SELECT alias_taxid, current_taxid, status FROM taxonomy_aliases ORDER BY alias_taxid"
    )
    assert [tuple(row) for row in aliases] == [(99, 3, "merged"), (100, None, "deleted")]
    reused = taxonomy.import_ncbi_taxonomy(db, project, package, "taxdump-1")
    assert reused["reused"] is True


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({
            "nodes.dmp": "1 | 1 | no rank |\n2 | 1 | genus |\n",
            "names.dmp": "1 | root | | scientific name |\n",
        }, "has no scientific name"),
        ({
            "nodes.dmp": "",
            "names.dmp": "1 | root | | scientific name |\n",
        }, "contains no taxonomy nodes"),
        ({
            "nodes.dmp": "bad | 1 | no rank |\n",
            "names.dmp": "1 | root | | scientific name |\n",
        }, "invalid TaxID"),
        ({
            "nodes.dmp": "1 | 1 | no rank |\n",
            "names.dmp": "bad | root | | scientific name |\n",
        }, "invalid scientific-name row"),
        ({
            "nodes.dmp": "1 | 1 | no rank |\n",
            "names.dmp": "1 | root | | scientific name |\n",
            "merged.dmp": "bad | 1 |\n",
        }, "merged.dmp: invalid row"),
        ({
            "nodes.dmp": "1 | 1 | no rank |\n",
            "names.dmp": "1 | root | | scientific name |\n",
            "delnodes.dmp": "bad |\n",
        }, "delnodes.dmp: invalid row"),
    ],
)
def test_taxdump_import_validation_paths(project_db, tmp_path, files, message):
    project, db = project_db
    package = tmp_path / "bad-taxdump.tar.gz"
    _tar(package, files)
    with pytest.raises(ValidationError, match=message):
        taxonomy.import_ncbi_taxonomy(db, project, package, "bad-1")


def test_json_taxonomy_rejects_incomplete_parent(project_db, tmp_path):
    project, db = project_db
    source = tmp_path / "taxonomy.jsonl"
    records = [
        {"taxId": 1, "parentTaxId": 1, "rank": "no rank", "taxName": "root"},
        {"taxId": 2, "parentTaxId": 99, "rank": "genus", "taxName": "orphan"},
    ]
    source.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    with pytest.raises(ValidationError, match="refers to missing parent"):
        taxonomy.import_ncbi_taxonomy(db, project, source, "json-1")


def test_taxdump_rejects_alias_target_missing_from_nodes(project_db, tmp_path):
    project, db = project_db
    package = tmp_path / "missing-alias-target.tar.gz"
    _tar(package, {
        "nodes.dmp": "1 | 1 | no rank |\n",
        "names.dmp": "1 | root | | scientific name |\n",
        "merged.dmp": "99 | 999 |\n",
    })
    with pytest.raises(ValidationError, match="alias TaxID 99"):
        taxonomy.import_ncbi_taxonomy(db, project, package, "alias-1")
