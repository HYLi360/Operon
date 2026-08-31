"""Edge cases for profiles, backups, reports, and shared filesystem utilities."""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest
import yaml

from operon import backup, profiles, reports, utils
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ConflictError, ValidationError


@pytest.fixture
def project_db(tmp_path: Path):
    root = tmp_path / "project"
    assert main(["--project", str(root), "init", str(root)]) == 0
    project = load_project(root)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def test_profile_writing_loading_filtering_and_rejection(tmp_path):
    directory = tmp_path / "profiles"
    profiles.write_default_profiles(directory)
    assert profiles.load_profile(directory, "file_integrity_v1", expected_kind="qc")["version"] == 1
    assert "coverage_viridiplantae_v1" not in profiles.load_profiles(directory, kind="qc")
    assert "coverage_viridiplantae_v1" in profiles.load_profiles(directory, kind="taxonomy_coverage")
    assert profiles.load_profiles(tmp_path / "missing") == {}
    for name in ("", "../x", ".", ".."):
        with pytest.raises(ValidationError, match="invalid profile name"):
            profiles.load_profile(directory, name, expected_kind="qc")
    with pytest.raises(ValidationError, match="not found"):
        profiles.load_profile(directory, "missing", expected_kind="qc")
    with pytest.raises(ValidationError, match="has kind"):
        profiles.load_profile(directory, "coverage_viridiplantae_v1", expected_kind="qc")
    invalid = directory / "invalid.yaml"
    invalid.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid profile"):
        profiles.load_profiles(directory)
    invalid.write_text("kind: qc\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid profile"):
        profiles.load_profile(directory, "invalid", expected_kind="qc")


def test_backup_scope_destination_and_manifest_validation(project_db, tmp_path):
    project, db = project_db
    with pytest.raises(ValidationError, match="backup scope"):
        backup.create_backup(db, project, tmp_path / "x", scope="bad")
    with pytest.raises(ValidationError, match="outside the project root"):
        backup.create_backup(db, project, project.root / "backup")
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ConflictError, match="already exists"):
        backup.create_backup(db, project, existing)
    missing = tmp_path / "missing-backup"
    missing.mkdir()
    with pytest.raises(ValidationError, match="manifest is missing"):
        backup.verify_backup(missing)
    (missing / "backup-manifest.json").write_text("bad", encoding="utf-8")
    with pytest.raises(ValidationError, match="cannot read backup manifest"):
        backup.verify_backup(missing)

    target = tmp_path / "backup"
    backup.create_backup(db, project, target)
    manifest_path = target / "backup-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    first = manifest["files"][0]
    candidate = target / first["relative_path"]
    candidate.unlink()
    result = backup.verify_backup(target)
    assert {item["error"] for item in result["failures"]} >= {"missing"}

    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("wrong-size", encoding="utf-8")
    assert "size mismatch" in {item["error"] for item in backup.verify_backup(target)["failures"]}
    candidate.write_bytes(b"x" * int(first["size_bytes"]))
    assert "checksum mismatch" in {item["error"] for item in backup.verify_backup(target)["failures"]}

    manifest["files"].append({"relative_path": "../escape", "size_bytes": 1, "sha256": "x"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert "unsafe path" in {item["error"] for item in backup.verify_backup(target)["failures"]}


def test_backup_staging_is_removed_on_failure(project_db, tmp_path, monkeypatch):
    project, db = project_db
    monkeypatch.setattr(
        backup, "_copy_known_path", lambda *_a: (_ for _ in ()).throw(RuntimeError("fail"))
    )
    with pytest.raises(RuntimeError):
        backup.create_backup(db, project, tmp_path / "failed")
    assert not list(tmp_path.glob(".failed.staging-*"))


def test_report_queries_wide_pivot_and_reason_rendering(project_db):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Example"})
    db.insert_qc_result({
        "entity_type": "organism", "entity_id": "ORG_000001", "file_id": None,
        "input_identity": "entity:organism:ORG_000001", "qc_stage": "x",
        "metric_name": "m", "metric_value": "1", "metric_numeric": 1,
        "tool": "t", "tool_version": "1", "parameter_set": "p",
        "evaluated_at": "now",
    })
    db.set_entity_state("organism", "ORG_000001", "METADATA_VALIDATED", "ok")
    assert len(reports.qc_rows(db, entity_type="organism", entity_id="ORG_000001")) == 1
    columns, rows = reports.qc_wide(db, "organism")
    assert "m" in columns and rows[0]["m"] == 1.0
    assert "metric_name" in reports.print_qc_table(db)
    assert "ORG_000001" in reports.print_status(db)
    wide = reports.export_qc_tsv(db, project, "organism")
    assert wide.is_file()
    output = reports.export_metadata_report(db, project, project.root / "custom-report")
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["tables"]["organisms.tsv"]["row_count"] == 1


def test_filesystem_identity_and_opening_paths(tmp_path):
    with pytest.raises(NotADirectoryError):
        list(utils.iter_directory_entries(tmp_path / "missing"))
    root = tmp_path / "tree"
    root.mkdir()
    (root / "empty").mkdir()
    (root / "file").write_text("abc", encoding="utf-8")
    (root / "link").symlink_to("file")
    entries = [p.relative_to(root).as_posix() for p in utils.iter_directory_entries(root)]
    assert entries == ["empty", "file", "link"]
    assert utils.sha256_path(root) == utils.sha256_directory(root)
    assert utils.sha256_path(root / "file") == utils.sha256_file(root / "file")
    assert utils.path_size_bytes(root) == 3
    assert utils.path_size_bytes(root / "file") == 3
    with pytest.raises(FileNotFoundError):
        utils.sha256_path(tmp_path / "missing")
    with pytest.raises(FileNotFoundError):
        utils.path_size_bytes(tmp_path / "missing")
    assert utils.path_is_nonempty(root)
    assert utils.path_is_nonempty(root / "empty") is False
    assert utils.path_is_nonempty(root / "file") is True
    assert utils.path_is_nonempty(tmp_path / "missing") is False

    plain = tmp_path / "plain"
    plain.write_text("text", encoding="utf-8")
    compressed = tmp_path / "compressed"
    with gzip.open(compressed, "wt") as handle:
        handle.write("gzip")
    assert utils.is_gzip_path(compressed)
    with utils.open_maybe_gzip(compressed) as handle:
        assert handle.read() == "gzip"
    with utils.open_maybe_gzip(plain, "r") as handle:
        assert handle.read() == "text"
    with pytest.raises(ValueError, match="text reading only"):
        utils.open_maybe_gzip(plain, "wb")


def test_atomic_helpers_cleanup_temporary_files(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "text"
    utils.atomic_write_text(target, "ok")
    assert target.read_text() == "ok"
    copied = tmp_path / "copied"
    utils.atomic_copy(target, copied)
    assert copied.read_text() == "ok"
    tree_copy = tmp_path / "tree-copy"
    utils.atomic_copytree(target.parent, tree_copy)
    assert (tree_copy / "text").read_text() == "ok"
    with pytest.raises(NotADirectoryError):
        utils.atomic_copytree(target, tmp_path / "bad")

    real_replace = os.replace
    monkeypatch.setattr(utils.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("replace")))
    with pytest.raises(OSError):
        utils.atomic_write_text(tmp_path / "failed-text", "x")
    with pytest.raises(OSError):
        utils.atomic_copy(target, tmp_path / "failed-copy")
    with pytest.raises(OSError):
        utils.atomic_copytree(target.parent, tmp_path / "failed-tree")
    monkeypatch.setattr(utils.os, "replace", real_replace)
    assert not list(tmp_path.glob(".failed-*"))


def test_table_and_numeric_utility_edges():
    assert "header" in utils.format_table(["header"], [])
    assert "" in utils.format_table(["a"], [[None]])
    assert utils.parse_key_values(["--a=1", "b=two=parts"]) == {"a": "1", "b": "two=parts"}
    with pytest.raises(ValueError, match="expected key=value"):
        utils.parse_key_values(["bad"])
    with pytest.raises(ValueError, match="empty field name"):
        utils.parse_key_values(["--=x"])
    assert list(utils.chunked(range(5), 2)) == [[0, 1], [2, 3], [4]]
    assert utils.median([]) == 0
    assert utils.median([3, 1, 2]) == 2
    assert utils.median([1, 3]) == 2
    assert utils.pct(1, 4) == 25
    assert utils.pct(1, 0) == 0
