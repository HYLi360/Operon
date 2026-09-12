"""Cross-cutting contracts that the per-module suites leave uncovered.

Each test here asserts an observable outcome (returned value, error message,
file present/absent, database row) for a real contract: atomic publish and
staging cleanup, unsupported directory entries, retired-entity visibility,
release/export staging recovery, fanout unit selection, coverage rounding,
schema NULL literals, and environment probe fallbacks.
"""

from __future__ import annotations

import csv
import json
import os
import signal
import sqlite3
import sys
import types
import zipfile
from pathlib import Path

import pytest
import yaml

import operon
import operon.shutdown as shutdown
from operon import backup, coverage, entity_view, environment, lineage, release
from operon import sequence_tools, taxonomy, utils
from operon.classify import _extra_fields, _row_context, validate_classification_profile
from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.errors import ChecksumError, ConflictError, ValidationError
from operon.export import _export_files_in_workspace, export_files
from operon.fanout import fanout_units
from operon.files import ingest_file
from operon.import_wizard import _ask_files, _commit, _preflight, _summary
from operon.lifecycle import entity_subtree, lifecycle_plan, list_retired_entities
from operon.lineage import (
    _normalize_derived_from,
    adopt_files,
    load_adopt_manifest,
    normalize_adopt_item,
)
from operon.schema import Schema
from operon.table_import import apply_table_import, preview_table_import, read_table_file
from operon.workflow import set_state
from operon.utils import (
    atomic_copy,
    atomic_copytree,
    atomic_write_text,
    chunked,
    sha256_directory,
    sha256_file,
    sha256_path,
)


@pytest.fixture
def project_db(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    try:
        yield project, db
    finally:
        db.close()


def _insert_graph(db: Database) -> None:
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Graphus"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("runs", {"run_id": "RUN_000001", "sample_id": "SMP_000001"})
    db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
    db.insert_row("annotations", {"annotation_id": "ANN_000001", "assembly_id": "ASM_000001"})


# ---------------------------------------------------------------------------
# operon/__init__.py, operon/__main__.py
# ---------------------------------------------------------------------------


def test_package_version_falls_back_when_distribution_is_absent(monkeypatch):
    """An uninstalled source checkout must still import with a version string."""
    import importlib.metadata as real_metadata

    fake = types.ModuleType("importlib.metadata")
    fake.PackageNotFoundError = real_metadata.PackageNotFoundError

    def missing(name):
        raise real_metadata.PackageNotFoundError(name)

    fake.version = missing
    monkeypatch.setitem(sys.modules, "importlib.metadata", fake)
    namespace: dict[str, object] = {}
    source = Path(operon.__file__).read_text(encoding="utf-8")
    exec(compile(source, operon.__file__, "exec"), namespace)  # noqa: S102 - module source under test
    assert namespace["__version__"] == "0+unknown"
    # The installed distribution keeps reporting the real version (no pollution).
    assert operon.__version__ != "0+unknown"


def test_main_module_exposes_the_cli_entry_point():
    import importlib

    module = importlib.import_module("operon.__main__")
    assert module.main is main


# ---------------------------------------------------------------------------
# operon/utils.py
# ---------------------------------------------------------------------------


def test_sha256_directory_rejects_a_non_directory(tmp_path):
    with pytest.raises(NotADirectoryError):
        sha256_directory(tmp_path / "missing")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO support")
def test_sha256_directory_rejects_unsupported_entry_types(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "regular.txt").write_text("x", encoding="utf-8")
    os.mkfifo(tree / "pipe")
    with pytest.raises(OSError, match="unsupported directory entry type"):
        sha256_directory(tree)
    # A FIFO cannot be hashed, so no digest is ever published for that tree.
    with pytest.raises(OSError, match="unsupported directory entry type"):
        sha256_path(tree)


def test_atomic_write_text_keeps_staging_on_failed_unlink(tmp_path):
    target = tmp_path / "out.txt"
    real_replace, real_unlink = os.replace, os.unlink

    def failing_replace(_source, _target):
        raise RuntimeError("replace failed")

    def failing_unlink(_path):
        raise OSError("unlink failed")

    os.replace, os.unlink = failing_replace, failing_unlink
    try:
        with pytest.raises(RuntimeError, match="replace failed"):
            atomic_write_text(target, "payload")
    finally:
        os.replace, os.unlink = real_replace, real_unlink
    assert not target.exists()
    # The temporary file is the only trace and stays readable for debugging.
    staging = list(tmp_path.glob(".out.txt.*"))
    assert len(staging) == 1
    assert staging[0].read_text(encoding="utf-8") == "payload"
    staging[0].unlink()


def test_atomic_copy_keeps_staging_on_failed_unlink(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    target = tmp_path / "copy" / "target.bin"
    real_replace, real_unlink = os.replace, os.unlink

    def failing_replace(_source, _target):
        raise RuntimeError("replace failed")

    def failing_unlink(_path):
        raise OSError("unlink failed")

    os.replace, os.unlink = failing_replace, failing_unlink
    try:
        with pytest.raises(RuntimeError, match="replace failed"):
            atomic_copy(source, target)
    finally:
        os.replace, os.unlink = real_replace, real_unlink
    assert not target.exists()
    staging = list(target.parent.glob(".target.bin.*"))
    assert len(staging) == 1
    assert staging[0].read_bytes() == b"payload"
    staging[0].unlink()


def test_atomic_copytree_failure_before_staging_leaves_no_leftovers(tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    target = tmp_path / "dst"

    def failing_copytree(*_args, **_kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(utils.shutil, "copytree", failing_copytree)
    with pytest.raises(OSError, match="copy failed"):
        atomic_copytree(source, target)
    assert not target.exists()
    assert list(tmp_path.glob(".dst.*")) == []


def test_chunked_batches_an_exact_multiple_without_an_empty_tail():
    assert list(chunked([1, 2, 3, 4], 2)) == [[1, 2], [3, 4]]
    assert list(chunked([], 3)) == []
    assert list(chunked([1], 3)) == [[1]]


# ---------------------------------------------------------------------------
# operon/entity_view.py and retired-entity visibility
# ---------------------------------------------------------------------------


def test_entity_graph_rejects_an_unknown_scope(project_db):
    _project, db = project_db
    _insert_graph(db)
    with pytest.raises(ValidationError, match="unknown entity graph scope 'everything'"):
        entity_view.entity_graph(db, "ORG_000001", scope="everything")
    assert entity_view.entity_graph(db, "ORG_000001")["scope"] == "matched"


def test_entity_graph_on_a_pre_lifecycle_database_reports_no_retirements(project_db):
    """Databases without the 2.7 lifecycle views stay readable."""
    _project, db = project_db
    _insert_graph(db)
    db.conn.execute("DROP VIEW effective_retired_entities")
    db.conn.commit()
    assert db.lifecycle_schema_available() is False
    graph = entity_view.entity_graph(db, "ORG_000001", scope="organism")
    assert graph["retirements"] == []
    assert [row["sample_id"] for row in graph["samples"]] == ["SMP_000001"]


# ---------------------------------------------------------------------------
# operon/lifecycle.py
# ---------------------------------------------------------------------------


def test_entity_subtree_for_a_sample_lists_its_own_descendants(project_db):
    _project, db = project_db
    _insert_graph(db)
    assert entity_subtree(db, "sample", "SMP_000001") == {
        "organism": [], "sample": ["SMP_000001"], "run": ["RUN_000001"],
        "assembly": ["ASM_000001"], "annotation": ["ANN_000001"],
    }


def test_lifecycle_plan_rejects_an_unsupported_action(project_db):
    _project, db = project_db
    _insert_graph(db)
    with pytest.raises(ValidationError, match="unsupported lifecycle action 'DESTROY'"):
        lifecycle_plan(db, "ORG_000001", action="DESTROY")


def test_lifecycle_requires_schema_2_7(project_db):
    _project, db = project_db
    _insert_graph(db)
    db.conn.execute("DROP VIEW effective_retired_entities")
    db.conn.commit()
    with pytest.raises(ValidationError, match="requires database schema 2.7"):
        lifecycle_plan(db, "ORG_000001", action="RETIRE")
    with pytest.raises(ValidationError, match="requires database schema 2.7"):
        list_retired_entities(db)


def test_lifecycle_plan_without_files_reports_zero_file_references(project_db):
    _project, db = project_db
    _insert_graph(db)
    plan = lifecycle_plan(db, "ORG_000001", action="RETIRE")
    assert plan["reference_counts"]["files"] == 0
    assert plan["reference_counts"]["remote_locations"] == 0
    assert plan["reference_counts"]["release_members"] == 0
    assert plan["historical_release_versions"] == []
    assert plan["files"] == []


def test_lifecycle_plan_counts_files_and_release_members(project_db, tmp_path):
    project, db = project_db
    _insert_graph(db)
    genome = tmp_path / "genome.fa"
    genome.write_text(">ctg1\nACGT\n", encoding="utf-8")
    record = ingest_file(db, project, genome, "assembly", "ASM_000001", "genome_fasta")
    db.conn.execute(
        "INSERT INTO releases(version, created_at, profile, path, manifest_sha256, summary) "
        "VALUES(?,?,?,?,?,?)", ("v1", "now", "p", "releases/v1", "x", "{}"),
    )
    db.conn.execute(
        "INSERT INTO release_members(release_version, file_id, entity_type, entity_id, "
        "release_path, sha256, size_bytes) VALUES(?,?,?,?,?,?,?)",
        ("v1", record["file_id"], "assembly", "ASM_000001", "data/x.fa",
         record["sha256"], record["size_bytes"]),
    )
    db.conn.commit()
    plan = lifecycle_plan(db, "ASM_000001", action="RETIRE")
    assert plan["reference_counts"]["files"] == 1
    assert plan["historical_release_versions"] == ["v1"]


# ---------------------------------------------------------------------------
# operon/schema.py
# ---------------------------------------------------------------------------


def test_unique_combinations_ignore_missing_values(project_db, tmp_path):
    """A unique tuple with a missing half is not a duplicate (NULL semantics)."""
    project, _db = project_db
    document = yaml.safe_load(project.schema_path.read_text(encoding="utf-8"))
    document["tables"]["samples"]["unique"] = [["strain", "isolate"]]
    custom = tmp_path / "custom-schema.yaml"
    custom.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    schema = Schema.from_file(custom)

    rows, errors = schema.validate_and_normalize("samples", [
        {"sample_id": "SMP_000001", "organism_id": "ORG_000001"},
        {"sample_id": "SMP_000002", "organism_id": "ORG_000001"},
    ])
    assert errors == []
    assert [row["sample_id"] for row in rows] == ["SMP_000001", "SMP_000002"]
    assert rows[0]["strain"] is None

    with pytest.raises(ValidationError, match="duplicate unique combination"):
        schema.validate_and_normalize("samples", [
            {"sample_id": "SMP_000001", "organism_id": "ORG_000001",
             "strain": "A", "isolate": "B"},
            {"sample_id": "SMP_000002", "organism_id": "ORG_000001",
             "strain": "A", "isolate": "B"},
        ])


# ---------------------------------------------------------------------------
# operon/classify.py
# ---------------------------------------------------------------------------


def test_alignment_rows_without_extra_json_still_classify():
    context = _row_context({"query_id": "g1 description", "query_start": 5,
                            "query_end": 9, "extra_json": None})
    assert context["seqid"] == "g1"
    assert context["span"] == 5
    assert _extra_fields(None) == {}
    assert _extra_fields("") == {}


def test_profile_accepts_any_and_not_condition_groups():
    profile = {
        "kind": "sequence_classification",
        "version": 1,
        "applies_to": {"entity_type": "annotation", "file_role": "protein_fasta"},
        "sources": {"core": {"analysis": "cdd", "filter": [
            {"any": [{"field": "hit_type", "operator": "==", "value": "Specific"},
                     {"field": "hit_type", "operator": "==", "value": "Motif"}]},
            {"not": {"field": "short_name", "operator": "like", "value": "bhlh_%"}},
        ]}},
        "rules": [
            {"label": "HIT", "source": "core", "when": [
                {"not": {"field": "incomplete", "operator": "==", "value": "NC"}}]},
            {"label": "NONE", "default": True},
        ],
    }
    spec = validate_classification_profile(profile, "tier")
    assert spec["sources"]["core"]["filter"] == profile["sources"]["core"]["filter"]
    assert spec["rules"][0]["when"] == profile["rules"][0]["when"]


# ---------------------------------------------------------------------------
# operon/environment.py
# ---------------------------------------------------------------------------


def test_local_home_falls_back_to_the_home_variable(monkeypatch):
    monkeypatch.setenv("HOME", "/probe/home")

    def unresolvable(_cls):
        raise RuntimeError("no home directory")

    monkeypatch.setattr(Path, "home", classmethod(unresolvable))
    assert environment._local_home() == "/probe/home"


def test_local_home_falls_back_to_empty_without_the_variable(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)

    def unresolvable(_cls):
        raise RuntimeError("no home directory")

    monkeypatch.setattr(Path, "home", classmethod(unresolvable))
    assert environment._local_home() == ""


# ---------------------------------------------------------------------------
# operon/shutdown.py
# ---------------------------------------------------------------------------


def test_force_exit_uses_the_posix_signal_convention(monkeypatch):
    exits: list[int] = []
    monkeypatch.setattr(shutdown.os, "_exit", exits.append)
    shutdown._force_exit(signal.SIGTERM)
    shutdown._force_exit(signal.SIGINT)
    assert exits == [128 + signal.SIGTERM, 128 + signal.SIGINT]


def test_first_signal_reports_and_requests_a_graceful_shutdown(capsys):
    with shutdown.graceful_shutdown():
        with pytest.raises(shutdown.ShutdownRequested) as caught:
            shutdown._handler(signal.SIGTERM, None)
    assert caught.value.signum == signal.SIGTERM
    assert "SIGTERM" in str(caught.value)
    assert isinstance(caught.value, KeyboardInterrupt)
    assert "shutting down gracefully" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# operon/backup.py: standardized-link rebasing and verify_backup faults
# ---------------------------------------------------------------------------


def _make_project(root: Path) -> tuple[object, Database]:
    root.mkdir(parents=True, exist_ok=True)
    assert main(["--project", str(root), "init", str(root)]) == 0
    project = load_project(root)
    return project, Database(project.db_path)


def test_backup_rebases_only_project_internal_absolute_links(tmp_path):
    project, db = _make_project(tmp_path / "project")
    try:
        db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "B"})
        views = project.root / "standardized" / "assemblies" / "ASM_000001"
        views.mkdir(parents=True)
        archived = project.root / "raw" / "assemblies" / "ASM_000001" / "genome.fasta"
        archived.parent.mkdir(parents=True)
        archived.write_text(">ctg1\nACGT\n", encoding="utf-8")
        (views / "relative.fasta").symlink_to(
            Path("../../raw/assemblies/ASM_000001/genome.fasta"))
        (views / "internal.fasta").symlink_to(archived)
        external = tmp_path / "external.txt"
        external.write_text("outside\n", encoding="utf-8")
        (views / "external.fasta").symlink_to(external)

        destination = tmp_path / "backup"
        result = backup.create_backup(db, project, destination, scope="full")
        assert result["scope"] == "full"

        copied = destination / "standardized" / "assemblies" / "ASM_000001"
        # A portable relative link is preserved verbatim.
        assert os.readlink(copied / "relative.fasta") == (
            "../../raw/assemblies/ASM_000001/genome.fasta")
        # An absolute link into the project is rebased so the backup is movable.
        rebased = os.readlink(copied / "internal.fasta")
        assert not os.path.isabs(rebased)
        assert (copied / "internal.fasta").resolve() == (
            destination / "raw" / "assemblies" / "ASM_000001" / "genome.fasta").resolve()
        # An external target keeps its original link text and is never followed.
        assert os.readlink(copied / "external.fasta") == str(external)
        assert backup.verify_backup(destination)["ok"] is True
    finally:
        db.close()


def test_verify_backup_reports_missing_and_unexpected_symlinks(tmp_path):
    project, db = _make_project(tmp_path / "project")
    try:
        db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "B"})
        views = project.root / "standardized" / "assemblies" / "ASM_000001"
        views.mkdir(parents=True)
        archived = project.root / "raw" / "assemblies" / "ASM_000001" / "genome.fasta"
        archived.parent.mkdir(parents=True)
        archived.write_text(">ctg1\nACGT\n", encoding="utf-8")
        (views / "internal.fasta").symlink_to(archived)

        destination = tmp_path / "backup"
        backup.create_backup(db, project, destination, scope="full")
        assert backup.verify_backup(destination)["ok"] is True

        link = destination / "standardized" / "assemblies" / "ASM_000001" / "internal.fasta"
        link.unlink()
        report = backup.verify_backup(destination)
        assert report["ok"] is False
        assert {"relative_path": link.relative_to(destination).as_posix(),
                "error": "missing symlink"} in report["failures"]

        # Restore the link, then swap a manifest regular file for a symlink.
        link.symlink_to(os.path.relpath(
            destination / "raw" / "assemblies" / "ASM_000001" / "genome.fasta",
            link.parent))
        assert backup.verify_backup(destination)["ok"] is True
        swap = destination / "project.yaml"
        swap.unlink()
        swap.symlink_to("operon.sqlite")
        report = backup.verify_backup(destination)
        assert report["ok"] is False
        assert {"relative_path": "project.yaml",
                "error": "unexpected symlink"} in report["failures"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# operon/export.py: staging cleanup, workspace publish and link kinds
# ---------------------------------------------------------------------------


def _ingest(db: Database, project, tmp_path: Path, name: str, text: str,
            entity_type: str, entity_id: str, role: str) -> dict:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return ingest_file(db, project, path, entity_type, entity_id, role)


@pytest.fixture
def export_project(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    for suffix in ("1", "2"):
        db.insert_row("assemblies", {
            "assembly_id": f"ASM_00000{suffix}", "sample_id": "SMP_000001"})
    files = {
        "genome1": _ingest(db, project, tmp_path, "genome1.fa", ">ctg1\nACGTACGT\n",
                           "assembly", "ASM_000001", "genome_fasta"),
        "genome2": _ingest(db, project, tmp_path, "genome2.fa", ">ctg2\nTTTTGGGG\n",
                           "assembly", "ASM_000002", "genome_fasta"),
        "proteins1": _ingest(db, project, tmp_path, "proteins.faa", ">p1\nMAAA\n",
                             "assembly", "ASM_000001", "protein_fasta"),
        "table1": _ingest(db, project, tmp_path, "genes.tsv", "gene\tvalue\na\t1\n",
                          "assembly", "ASM_000001", "annotation_table"),
    }
    try:
        yield project, db, files
    finally:
        db.close()


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def test_export_skips_taxa_inference_for_taxonomy_snapshot_members(export_project, tmp_path):
    """Snapshot packages export as opaque members: no organism can be traced."""
    project, db, _files = export_project
    snapshot = project.root / "taxonomy" / "TAX_000001"
    snapshot.mkdir(parents=True)
    (snapshot / "nodes.dmp").write_text("1\t|\tno rank\t|\n", encoding="utf-8")
    db.insert_row("files", {
        "file_id": "FIL_TAX_000001", "entity_type": "taxonomy_snapshot",
        "entity_id": "TAX_000001", "file_role": "taxonomy_package",
        "format": "directory", "compression": "none",
        "relative_path": "taxonomy/TAX_000001", "size_bytes": 1,
        "sha256": sha256_directory(snapshot), "status": "CHECKSUM_VERIFIED",
    })
    out = tmp_path / "snapshot-export"
    summary = export_files(db, project, output_dir=out, entity_type="taxonomy_snapshot")
    assert summary["file_count"] == 1
    manifest = _read_tsv(out / "manifest.tsv")
    assert [row["file_id"] for row in manifest] == ["FIL_TAX_000001"]
    assert (out / manifest[0]["export_relative_path"] / "nodes.dmp").is_file()
    # taxa.tsv keeps only its header: filenames never invent taxonomy.
    assert _read_tsv(out / "taxa.tsv") == []


def test_export_selection_by_entity_ids_format_and_entity_state(export_project, tmp_path):
    project, db, files = export_project
    db.set_entity_state("assembly", "ASM_000002", "METADATA_VALIDATED", "test")

    by_ids = tmp_path / "by-ids"
    summary = export_files(db, project, output_dir=by_ids, entity_ids=["ASM_000001"])
    rows = _read_tsv(by_ids / "manifest.tsv")
    assert summary["file_count"] == 3
    assert {row["entity_id"] for row in rows} == {"ASM_000001"}

    by_format = tmp_path / "by-format"
    summary = export_files(db, project, output_dir=by_format, fmt="tsv")
    rows = _read_tsv(by_format / "manifest.tsv")
    assert summary["file_count"] == 1
    assert [row["file_id"] for row in rows] == [files["table1"]["file_id"]]

    # Entity-state selection is case-insensitive on the caller's side.
    by_state = tmp_path / "by-state"
    summary = export_files(db, project, output_dir=by_state, state="metadata_validated")
    rows = _read_tsv(by_state / "manifest.tsv")
    assert summary["file_count"] == 1
    assert [row["entity_id"] for row in rows] == ["ASM_000002"]


def test_export_failure_leaves_an_occupied_workspace_empty(export_project, tmp_path):
    """A failed export must not leave a half-materialized member set behind."""
    project, db, files = export_project
    (project.root / files["genome2"]["relative_path"]).write_text(
        ">ctg2\ntampered\n", encoding="utf-8")
    out = tmp_path / "empty-target"
    out.mkdir()
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        export_files(db, project, output_dir=out,
                     entity_ids=["ASM_000001", "ASM_000002"])
    assert out.is_dir()
    assert list(out.iterdir()) == []


def test_export_publish_failure_removes_every_staged_file(export_project, tmp_path):
    project, db, _files = export_project
    out = tmp_path / "target"
    out.mkdir()
    db.conn.execute(
        "CREATE TRIGGER fail_export_run BEFORE INSERT ON workflow_runs "
        "BEGIN SELECT RAISE(ABORT, 'injected export failure'); END;"
    )
    db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected export failure"):
        export_files(db, project, output_dir=out, entity_type="assembly")
    assert list(out.iterdir()) == []


def test_export_workspace_publish_contract(export_project, tmp_path):
    project, db, _files = export_project
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "stale.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError, match="not empty"):
        _export_files_in_workspace(db, project, output_dir=occupied,
                                   entity_type="assembly")
    assert (occupied / "stale.txt").read_text(encoding="utf-8") == "x"

    fresh = tmp_path / "fresh"
    summary = _export_files_in_workspace(
        db, project, output_dir=fresh, entity_type="assembly", include_qc=False)
    assert summary["output_dir"] == str(fresh)
    assert (fresh / "manifest.tsv").is_file()
    assert not (fresh / "qc.tsv").exists()


def test_export_remote_only_member_requires_hydration(export_project, tmp_path):
    project, db, files = export_project
    db.conn.execute("UPDATE files SET status='REMOTE_ONLY' WHERE file_id=?",
                    (files["genome1"]["file_id"],))
    db.conn.commit()
    (project.root / files["genome1"]["relative_path"]).unlink()
    with pytest.raises(FileNotFoundError, match="remote-only"):
        export_files(db, project, output_dir=tmp_path / "out",
                     file_ids=[files["genome1"]["file_id"]])


def test_export_directory_artifact_copy_and_hardlink(export_project, tmp_path):
    project, db, _files = export_project
    tree = tmp_path / "pangenome"
    tree.mkdir()
    (tree / "gene_presence_absence.tsv").write_text("gene\na\n", encoding="utf-8")
    adopted = lineage.adopt_files(db, project, items=[{
        "path": str(tree), "entity_type": "assembly", "entity_id": "ASM_000001",
        "role": "pangenome_dir", "format": "directory", "compression": "none",
        "derived_from": [_files["genome1"]["file_id"]],
    }])[0]

    copied = tmp_path / "dir-copy"
    export_files(db, project, output_dir=copied, file_ids=[adopted["file_id"]],
                 include_qc=False)
    row = _read_tsv(copied / "manifest.tsv")[0]
    exported = copied / row["export_relative_path"]
    assert exported.is_dir() and (exported / "gene_presence_absence.tsv").is_file()
    assert not exported.is_symlink()
    assert sha256_file(exported / "gene_presence_absence.tsv") == sha256_file(
        tree / "gene_presence_absence.tsv")

    linked = tmp_path / "dir-link"
    export_files(db, project, output_dir=linked, file_ids=[adopted["file_id"]],
                 link_kind="hardlink", include_qc=False)
    row = _read_tsv(linked / "manifest.tsv")[0]
    assert (linked / row["export_relative_path"] / "gene_presence_absence.tsv").is_file()


def test_export_hardlink_falls_back_to_a_copy(export_project, tmp_path, monkeypatch):
    project, db, files = export_project

    def unsupported(*_args, **_kwargs):
        raise OSError("cross-device link")

    monkeypatch.setattr("operon.export.os.link", unsupported)
    out = tmp_path / "hard-fallback"
    export_files(db, project, output_dir=out, file_ids=[files["genome1"]["file_id"]],
                 link_kind="hardlink", include_qc=False)
    row = _read_tsv(out / "manifest.tsv")[0]
    exported = out / row["export_relative_path"]
    source = project.root / row["original_relative_path"]
    assert exported.read_bytes() == source.read_bytes()
    assert exported.stat().st_ino != source.stat().st_ino


# ---------------------------------------------------------------------------
# operon/release.py: preflight edge rules and hardlinked directories
# ---------------------------------------------------------------------------


def _write_qc_profile(project, name: str, applies_to) -> None:
    (project.profiles_dir / f"{name}.yaml").write_text(
        yaml.safe_dump({"kind": "qc", "version": 1, "applies_to": applies_to},
                       sort_keys=False),
        encoding="utf-8",
    )


def test_release_preflight_accepts_empty_and_unknown_applies_to(project_db):
    project, db = project_db
    _insert_graph(db)
    _write_qc_profile(project, "empty_scope", [])
    empty = release.create_release(db, project, "v-empty", "empty_scope")
    assert empty["accepted_file_count"] == 0

    _write_qc_profile(project, "unknown_scope", ["taxonomy_snapshot"])
    unknown = release.create_release(db, project, "v-unknown", "unknown_scope")
    assert unknown["accepted_file_count"] == 0


def test_release_preflight_falls_back_when_observed_json_is_unreadable(project_db, tmp_path):
    project, db = project_db
    _insert_graph(db)
    _write_qc_profile(project, "release_observed", ["assembly"])
    _ingest(db, project, tmp_path, "genome.fa", ">ctg1\nACGT\n",
            "assembly", "ASM_000001", "genome_fasta")
    db.upsert_decision({
        "entity_type": "assembly", "entity_id": "ASM_000001",
        "profile": "release_observed", "profile_version": 1, "decision": "FAIL",
        "reason_codes": json.dumps(["LOW_QUALITY"]), "observed": "not-json",
        "thresholds": "{}", "evaluated_at": "2099-01-01T00:00:00+00:00",
    })
    summary = release.create_release(db, project, "v-observed", "release_observed")
    # The unreadable watermark degrades to the timestamp comparison, so the
    # decision itself is the only exclusion.
    assert summary["accepted_file_count"] == 0
    assert summary["excluded_entity_count"] == 1
    excluded = _read_tsv(Path(summary["path"]) / "exclusions.tsv")
    assert [(row["entity_id"], row["exclusion_reason"]) for row in excluded] == [
        ("ASM_000001", "DECISION")]


def test_release_preflight_summarizes_more_than_ten_problem_entities(project_db, tmp_path):
    project, db = project_db
    _write_qc_profile(project, "release_many", ["assembly"])
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    for number in range(1, 12):
        entity_id = f"ASM_{number:06d}"
        db.insert_row("assemblies", {"assembly_id": entity_id, "sample_id": "SMP_000001"})
        _ingest(db, project, tmp_path, f"genome{number}.fa", f">ctg{number}\nACGT\n",
                "assembly", entity_id, "genome_fasta")
    with pytest.raises(ValidationError) as caught:
        release.create_release(db, project, "v-many", "release_many")
    message = str(caught.value)
    assert "11 unevaluated/stale entity/entities" in message
    assert "ASM_000010, ..." in message
    assert "ASM_000011" not in message
    assert list(project.releases_root.glob(".v-many.operon-release-*")) == []


def test_release_hardlinks_a_directory_member(project_db, tmp_path, monkeypatch):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X"})
    tree = project.root / "raw" / "tree"
    tree.mkdir(parents=True)
    (tree / "x.txt").write_text("x", encoding="utf-8")
    member = {
        "file_id": "FIL_000001", "entity_type": "organism", "entity_id": "ORG_000001",
        "file_role": "pangenome_dir", "format": "directory", "compression": "none",
        "relative_path": "raw/tree", "source_url": None, "size_bytes": 1,
        "sha256": sha256_path(tree), "status": "CHECKSUM_VERIFIED",
        "effective_decision": "PASS",
    }
    monkeypatch.setattr(release, "release_files_for", lambda *_a: [member])
    summary = release.create_release(db, project, "v-dir", "p", copy_files=False,
                                     link_kind="hardlink")
    published = Path(summary["path"]) / "data" / "organism" / "ORG_000001" / "tree"
    assert (published / "x.txt").read_text(encoding="utf-8") == "x"


# ---------------------------------------------------------------------------
# operon/coverage.py: real lineage SQL, release scopes and publish cleanup
# ---------------------------------------------------------------------------


def _import_taxonomy(project, db, tmp_path: Path, *, ranks, thresholds, root_taxids):
    source = tmp_path / "taxonomy.jsonl"
    records = [
        {"taxId": 1, "rank": "no rank", "taxName": "root"},
        {"taxId": 10, "parents": [1], "rank": "family", "taxName": "Fam"},
        {"taxId": 20, "parents": [10], "rank": "genus", "taxName": "Gen"},
        {"taxId": 22, "parents": [10], "rank": "genus", "taxName": "Gen2"},
        {"taxId": 23, "parents": [10], "rank": "genus", "taxName": "Gen3"},
    ]
    source.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    taxonomy.import_ncbi_taxonomy(db, project, source, "cov.1")
    profile = {
        "kind": "taxonomy_coverage",
        "version": 1,
        "name": "cov",
        "taxonomy": {"source": "NCBI"},
        "scope": {"root_taxids": root_taxids},
        "targets": {"ranks": ranks},
        "thresholds": thresholds,
    }
    (project.profiles_dir / "cov.yaml").write_text(
        yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
    return taxonomy.compile_reference_set(db, project, "cov", "cov.1")


def test_metadata_coverage_rounds_percentages_and_scopes_every_rank(project_db, tmp_path):
    project, db = project_db
    reference = _import_taxonomy(
        project, db, tmp_path, ranks=["genus"],
        thresholds={"genus": {"min_coverage_percent": 50}},
        root_taxids=[1],
    )
    for organism_id, name, taxid in (("ORG_000001", "Covered", 20),):
        db.insert_row("organisms", {
            "organism_id": organism_id, "scientific_name": name,
            "taxon_id": taxid, "taxonomy_source": "NCBI",
        })
    report = coverage.report_coverage(db, project, reference["reference_set_id"])
    metrics = {metric["rank"]: metric for metric in report["metrics"]}
    assert metrics["genus"]["numerator"] == 1
    assert metrics["genus"]["denominator"] == 3
    assert metrics["genus"]["coverage_percent"] == 33.3333
    assert metrics["genus"]["decision"] == "FAIL"
    assert report["decision"] == "FAIL"
    assert report["exit_code"] == 1
    assert report["reason_codes"] == ["GENUS_COVERAGE_BELOW_THRESHOLD"]
    summary = _read_tsv(Path(report["path"]) / "coverage_summary.tsv")
    assert summary[0]["coverage_percent"] == "33.3333"
    observations = _read_tsv(Path(report["path"]) / "coverage_observations.tsv")
    assert [row["organism_id"] for row in observations] == ["ORG_000001"]
    assert {row["family_taxid"] for row in observations} == {"10"}
    missing = _read_tsv(Path(report["path"]) / "coverage_missing.tsv")
    assert {row["taxid"] for row in missing} == {"22", "23"}

    # An unchanged re-run reuses the frozen report instead of rewriting it.
    reused = coverage.report_coverage(db, project, reference["reference_set_id"])
    assert reused["reused"] is True
    assert reused["report_id"] == report["report_id"]
    assert reused["exit_code"] == 1
    assert reused["path"] == report["path"]


def test_release_scope_coverage_uses_a_relative_release_path(project_db, tmp_path):
    project, db = project_db
    _insert_graph(db)
    _import_taxonomy(
        project, db, tmp_path, ranks=["genus"],
        thresholds={"genus": {"min_coverage_percent": 0}}, root_taxids=[1],
    )
    db.conn.execute("UPDATE organisms SET taxon_id=20, taxonomy_source='NCBI' "
                    "WHERE organism_id='ORG_000001'")
    db.conn.commit()
    _write_qc_profile(project, "release_coverage", ["assembly"])
    _ingest(db, project, tmp_path, "genome.fa", ">ctg1\nACGT\n",
            "assembly", "ASM_000001", "genome_fasta")
    db.upsert_decision({
        "entity_type": "assembly", "entity_id": "ASM_000001",
        "profile": "release_coverage", "profile_version": 1, "decision": "PASS",
        "reason_codes": "[]", "observed": "{}", "thresholds": "{}",
        "evaluated_at": "2099-01-01T00:00:00+00:00",
    })
    for state in ("STANDARDIZED", "QC_RUNNING", "QC_COMPLETE", "ACCEPTED"):
        set_state(db, "assembly", "ASM_000001", state, "release fixture")
    summary = release.create_release(db, project, "v1", "release_coverage")
    assert summary["accepted_file_count"] == 1
    # Releases can be recorded with a project-relative path.
    db.conn.execute("UPDATE releases SET path=? WHERE version='v1'",
                    (f"releases/v1",))
    db.conn.commit()
    reference = db.query(
        "SELECT reference_set_id FROM taxonomy_reference_sets")[0]["reference_set_id"]
    report = coverage.report_coverage(db, project, reference, release_version="v1")
    assert report["scope_kind"] == "release"
    assert report["scope_value"] == "v1"
    assert report["decision"] == "PASS"
    observations = _read_tsv(Path(report["path"]) / "coverage_observations.tsv")
    assert [row["organism_id"] for row in observations] == ["ORG_000001"]
    assert observations[0]["release_version"] == "v1"
    assert observations[0]["file_ids"]

    # A tampered frozen metadata snapshot is rejected before any report row.
    organisms_tsv = Path(summary["path"]) / "organisms.tsv"
    organisms_tsv.write_text(organisms_tsv.read_text(encoding="utf-8") + "\n",
                             encoding="utf-8")
    with pytest.raises(ValidationError, match="metadata checksum mismatch"):
        coverage.report_coverage(db, project, reference, release_version="v1")


def test_coverage_publish_failure_leaves_no_partial_report(project_db, tmp_path):
    project, db = project_db
    reference = _import_taxonomy(
        project, db, tmp_path, ranks=["genus"],
        thresholds={"genus": {"min_coverage_percent": 0}}, root_taxids=[1],
    )
    db.insert_row("organisms", {
        "organism_id": "ORG_000001", "scientific_name": "Covered",
        "taxon_id": 20, "taxonomy_source": "NCBI",
    })
    db.conn.execute(
        "CREATE TRIGGER fail_coverage_insert BEFORE INSERT ON coverage_reports "
        "BEGIN SELECT RAISE(ABORT, 'injected coverage failure'); END;"
    )
    db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected coverage failure"):
        coverage.report_coverage(db, project, reference["reference_set_id"])
    reports_root = project.reports_root / "coverage"
    assert not reports_root.exists() or list(reports_root.iterdir()) == []
    assert db.query("SELECT COUNT(*) AS n FROM coverage_reports")[0]["n"] == 0
    failure = db.query(
        "SELECT status, error FROM workflow_runs WHERE step='coverage_report'")[0]
    assert failure["status"] == "failed"
    assert "injected coverage failure" in failure["error"]


# ---------------------------------------------------------------------------
# operon/import_wizard.py
# ---------------------------------------------------------------------------


def _clean_draft(genome: Path) -> dict:
    return {
        "source": {
            "source_type": "insdc", "database_name": "NCBI", "provider": "NCBI",
            "record_url": "https://example.invalid/dataset/1",
        },
        "organism": {"action": "create", "id": "ORG_000001", "row": {
            "organism_id": "ORG_000001", "scientific_name": "Wizardus testii",
            "taxon_id": 1, "taxonomy_source": "NCBI"}},
        "sample": {"action": "create", "id": "SMP_000001", "row": {
            "sample_id": "SMP_000001", "organism_id": "ORG_000001"}},
        "run": {"action": "create", "id": "RUN_000001", "row": {
            "run_id": "RUN_000001", "sample_id": "SMP_000001",
            "library_strategy": "WGS"}},
        "assembly": {"action": "create", "id": "ASM_000001", "row": {
            "assembly_id": "ASM_000001", "sample_id": "SMP_000001",
            "assembly_version": "1"}},
        "annotation": None,
        "files": [{"label": "Genome FASTA", "role": "genome_fasta",
                   "entity_type": "assembly", "path": str(genome)}],
    }


def test_ask_files_without_annotation_or_run_only_asks_for_the_genome(tmp_path, monkeypatch):
    asked: list[str] = []

    def record(label, current=""):
        asked.append(label)
        return ""

    monkeypatch.setattr("operon.import_wizard._ask_path", record)
    draft: dict = {"files": []}
    _ask_files(None, draft)
    assert asked == ["Genome FASTA"]
    assert draft["files"] == []


def test_summary_renders_run_details_and_no_warnings(tmp_path, project_db):
    _project, db = project_db
    genome = tmp_path / "genome.fna"
    genome.write_text(">x\nACGT\n", encoding="utf-8")
    draft = _clean_draft(genome)
    summary = _summary(db, draft)
    assert "[4] Sequencing" in summary
    assert "    library_strategy: WGS" in summary
    assert "Warnings" in summary
    assert summary.rstrip().endswith("None")


def test_preflight_blocks_a_reused_sample_from_another_organism(project_db):
    project, db = project_db
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "One"})
    db.insert_row("organisms", {"organism_id": "ORG_000002", "scientific_name": "Two"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    draft = {
        "source": {"source_type": "insdc", "database_name": "NCBI", "provider": "NCBI"},
        "organism": {"action": "reuse", "id": "ORG_000002"},
        "sample": {"action": "reuse", "id": "SMP_000001"},
        "files": [],
    }
    with pytest.raises(ValidationError, match="does not belong to the selected organism"):
        _preflight(db, project, draft)


def test_commit_reuses_an_archived_target_without_new_files(project_db, tmp_path):
    project, db = project_db
    genome = tmp_path / "genome.fna"
    genome.write_text(">x\nACGT\n", encoding="utf-8")
    first = _commit(db, project, _clean_draft(genome))
    assert len(first["files"]) == 1
    archived = project.raw_root / "assemblies" / "ASM_000001"
    targets = [path for path in archived.iterdir() if path.is_file()]
    assert len(targets) == 1

    reuse = _clean_draft(genome)
    for entity_type in ("organism", "sample", "run", "assembly"):
        reuse[entity_type] = {"action": "reuse", "id": reuse[entity_type]["id"]}
    second = _commit(db, project, reuse)
    assert [row["file_id"] for row in second["files"]] == [
        row["file_id"] for row in first["files"]]
    assert [path for path in archived.iterdir() if path.is_file()] == targets
    assert db.query("SELECT COUNT(*) AS n FROM files")[0]["n"] == 1


def test_commit_removes_a_directory_target_when_bookkeeping_fails(project_db, tmp_path):
    project, db = project_db
    bundle = tmp_path / "annotation_bundle"
    bundle.mkdir()
    (bundle / "genes.gff3").write_text("##gff-version 3\n", encoding="utf-8")
    draft = _clean_draft(tmp_path / "genome.fna")
    draft["files"][0] = {"label": "Annotation bundle", "role": "annotation_bundle",
                         "entity_type": "assembly", "path": str(bundle)}
    (tmp_path / "genome.fna").write_text(">x\nACGT\n", encoding="utf-8")
    db.conn.execute(
        "CREATE TRIGGER fail_import_run BEFORE INSERT ON workflow_runs "
        "BEGIN SELECT RAISE(ABORT, 'injected import failure'); END;"
    )
    db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected import failure"):
        _commit(db, project, draft)
    # Nothing survives: no archived bundle, no metadata rows, no run log.
    assert not (project.raw_root / "assemblies" / "ASM_000001"
                / "ASM_000001.annotation_bundle.dir").exists()
    assert not any(path.is_file() for path in project.raw_root.rglob("*"))
    assert db.query("SELECT COUNT(*) AS n FROM organisms")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM files")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM workflow_runs")[0]["n"] == 0


# ---------------------------------------------------------------------------
# operon/table_import.py
# ---------------------------------------------------------------------------


def _xlsx(path: Path, workbook: str, sheet: str) -> Path:
    rels = ("<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/"
            "relationships\"><Relationship Id=\"rId1\" "
            "Target=\"worksheets/sheet1.xml\"/></Relationships>")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


def test_xlsx_with_a_headerless_empty_sheet_reads_as_no_rows(tmp_path):
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    workbook = (f'<workbook xmlns="{ns}" xmlns:r="{rel}">'
                f'<sheets><sheet name="data" r:id="rId1"/></sheets></workbook>')
    sheet = f'<worksheet xmlns="{ns}"><sheetData/></worksheet>'
    path = _xlsx(tmp_path / "headerless.xlsx", workbook, sheet)
    assert read_table_file(path) == []


def test_accession_table_preview_validates_targets_and_applies_updates(project_db, tmp_path):
    project, db = project_db
    schema = Schema.from_file(project.schema_path)
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "O"})
    db.insert_row("accessions", {
        "internal_type": "organism", "internal_id": "ORG_000001",
        "namespace": "LAB", "accession": "A1", "version": "1",
    })
    source = tmp_path / "accessions.csv"
    with open(source, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["namespace", "accession", "internal_type",
                                "internal_id", "version"])
        writer.writeheader()
        writer.writerow({"namespace": "LAB", "accession": "A1",
                         "internal_type": "organism", "internal_id": "ORG_000001",
                         "version": "2"})
    preview = preview_table_import(db, schema, "accessions", source)
    assert (preview["insert"], preview["update"], preview["unchanged"]) == (0, 1, 0)
    assert preview["items"][0]["differences"] == ["version"]

    result = apply_table_import(db, schema, preview, on_conflict="update")
    assert result == {"inserted": 0, "updated": 1, "unchanged": 0, "skipped": 0}
    assert db.query("SELECT version FROM accessions WHERE accession='A1'")[0]["version"] == "2"
    audit = db.query(
        "SELECT field, old_value, new_value FROM changes "
        "WHERE object_type='accessions' AND reason='table import update'")
    assert [(row["field"], row["old_value"], row["new_value"]) for row in audit] == [
        ("version", "1", "2")]


# ---------------------------------------------------------------------------
# operon/fanout.py: unit selection guards and batch cleanup
# ---------------------------------------------------------------------------


@pytest.fixture
def fanout_project(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path)]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "X"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
    db.insert_row("annotations", {
        "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
        "annotation_source": "test", "annotation_version": 1,
    })
    fasta = tmp_path / "proteins.faa"
    fasta.write_text(">p1\nMPEPTIDE\n>p2\nAAAAAA\n>p3\nCCCCCC\n", encoding="utf-8")
    source = ingest_file(db, project, fasta, "annotation", "ANN_000001", "protein_fasta")
    for seqid, length in (("p1", 8), ("p2", 6), ("p3", 6)):
        db.insert_row("sequences", {
            "file_id": source["file_id"], "file_sha256": source["sha256"],
            "entity_type": "annotation", "entity_id": "ANN_000001",
            "seqid": seqid, "length": length,
        })
    assignments_path = tmp_path / "assign.tsv"
    assignments_path.write_text("unit\tseqid\nSF01\tp1\nSF02\tp2\n", encoding="utf-8")
    assignments = ingest_file(
        db, project, assignments_path, "annotation", "ANN_000001",
        "subfamily_assignments", fmt="tsv", compression="none")
    try:
        yield project, db, source, assignments
    finally:
        db.close()


def _fanout(db, project, source, assignments, **overrides):
    kwargs = {
        "assignments_file_id": assignments["file_id"],
        "source_file_ids": [source["file_id"]],
        "entity_type": "annotation",
        "entity_id": "ANN_000001",
        "role_prefix": "subfamily_alignment",
        "command": "operon fanout",
    }
    kwargs.update(overrides)
    return fanout_units(db, project, **kwargs)


def test_fanout_rejects_duplicate_source_files(fanout_project):
    project, db, source, assignments = fanout_project
    with pytest.raises(ValidationError, match="duplicate --source-file"):
        _fanout(db, project, source, assignments,
                source_file_ids=[source["file_id"], source["file_id"]])
    assert db.query("SELECT COUNT(*) AS n FROM workflow_runs WHERE step='fanout'")[0]["n"] == 0


def test_fanout_rejects_remote_only_and_missing_manifest_bytes(fanout_project):
    project, db, source, assignments = fanout_project
    db.conn.execute("UPDATE files SET status='REMOTE_ONLY' WHERE file_id=?",
                    (source["file_id"],))
    db.conn.commit()
    with pytest.raises(ValidationError, match="REMOTE_ONLY"):
        _fanout(db, project, source, assignments)
    db.conn.execute("UPDATE files SET status='CHECKSUM_VERIFIED' WHERE file_id=?",
                    (source["file_id"],))
    db.conn.commit()
    (project.root / source["relative_path"]).unlink()
    with pytest.raises(ValidationError, match="bytes are missing"):
        _fanout(db, project, source, assignments)


def test_fanout_rejects_tampered_source_bytes(fanout_project):
    project, db, source, assignments = fanout_project
    (project.root / source["relative_path"]).write_text(
        ">p1\nTAMPERED\n>p2\nAAAAAA\n>p3\nCCCCCC\n", encoding="utf-8")
    with pytest.raises(ChecksumError, match="failed manifest verification"):
        _fanout(db, project, source, assignments)


def test_fanout_rejects_a_stale_sequence_registry(fanout_project):
    project, db, source, assignments = fanout_project
    db.conn.execute("UPDATE sequences SET file_sha256='stale' WHERE file_id=? AND seqid='p2'",
                    (source["file_id"],))
    db.conn.commit()
    with pytest.raises(ValidationError, match="predates the current file bytes"):
        _fanout(db, project, source, assignments)


def test_fanout_rejects_a_seqid_absent_from_the_fasta(fanout_project):
    project, db, source, assignments = fanout_project
    db.insert_row("sequences", {
        "file_id": source["file_id"], "file_sha256": source["sha256"],
        "entity_type": "annotation", "entity_id": "ANN_000001",
        "seqid": "ghost", "length": 10,
    })
    assignments_path = project.root / assignments["relative_path"]
    assignments_path.write_text("unit\tseqid\nSF01\tghost\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="absent from the source FASTA bytes"):
        _fanout(db, project, source, assignments)


def test_fanout_rejects_an_occupied_target_with_different_content(fanout_project):
    project, db, source, assignments = fanout_project
    target = (project.analysis_root / "derived" / "ANN_000001"
              / "ANN_000001.subfamily_alignment:SF01.fasta")
    target.parent.mkdir(parents=True)
    target.write_text(">p1\nDIFFERENT\n", encoding="utf-8")
    with pytest.raises(ConflictError, match="occupied by different content"):
        _fanout(db, project, source, assignments)
    assert target.read_text(encoding="utf-8") == ">p1\nDIFFERENT\n"


def test_fanout_batch_failure_removes_created_targets_and_records_it(fanout_project):
    project, db, source, assignments = fanout_project
    db.conn.execute(
        "CREATE TRIGGER fail_lineage BEFORE INSERT ON file_lineage "
        "BEGIN SELECT RAISE(ABORT, 'injected lineage failure'); END;"
    )
    db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected lineage failure"):
        _fanout(db, project, source, assignments)
    derived = project.analysis_root / "derived" / "ANN_000001"
    assert not derived.exists() or list(derived.iterdir()) == []
    assert db.query("SELECT COUNT(*) AS n FROM files WHERE relative_path LIKE 'analysis/%'")[0]["n"] == 0
    runs = db.query("SELECT status, exit_code, error FROM workflow_runs WHERE step='fanout'")
    assert [(row["status"], row["exit_code"]) for row in runs] == [("failed", 1)]
    assert "injected lineage failure" in runs[0]["error"]


# ---------------------------------------------------------------------------
# operon/lineage.py: adopt preflight, path safety and idempotent targets
# ---------------------------------------------------------------------------


def test_derived_from_must_be_a_list_or_string(tmp_path):
    assert _normalize_derived_from(" a , b ", 1) == ["a", "b"]
    assert _normalize_derived_from(["a", " b "], 1) == ["a", "b"]
    with pytest.raises(ValidationError, match="derived_from must be a list"):
        _normalize_derived_from(42, 3)
    with pytest.raises(ValidationError, match="requires at least one file_id"):
        _normalize_derived_from(" , ", 3)
    with pytest.raises(ValidationError, match="must be a mapping"):
        normalize_adopt_item(["not-a-mapping"], 2)


def test_load_adopt_manifest_rejects_broken_json(tmp_path):
    broken = tmp_path / "adopt.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValidationError, match="invalid JSON adopt manifest"):
        load_adopt_manifest(broken)
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps({"path": "a"}), encoding="utf-8")
    with pytest.raises(ValidationError, match="must be a list of items"):
        load_adopt_manifest(mapping)


def test_adopt_resolves_relative_paths_and_rejects_incompatible_shapes(fanout_project, tmp_path):
    project, db, source, _assignments = fanout_project
    derived = project.root / "inputs" / "matrix.tsv"
    derived.parent.mkdir()
    derived.write_text("id\tvalue\nctg1\t1\n", encoding="utf-8")
    item = {
        "path": "inputs/matrix.tsv", "entity_type": "annotation", "entity_id": "ANN_000001",
        "role": "pangenome_matrix", "format": "tsv", "compression": "none",
        "derived_from": [source["file_id"]],
    }
    record = adopt_files(db, project, items=[item])[0]
    assert record["format"] == "tsv"
    assert (project.root / record["relative_path"]).is_file()

    tree = tmp_path / "roary_out"
    tree.mkdir()
    (tree / "gene.txt").write_text("gene\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="incompatible format/compression"):
        adopt_files(db, project, items=[{
            **item, "path": str(tree), "role": "bad_bundle",
            "format": "tsv", "compression": "none",
        }])


def test_adopt_rejects_a_manifest_path_escaping_the_project(fanout_project, tmp_path):
    project, db, source, _assignments = fanout_project
    outside = tmp_path / "outside.tsv"
    outside.write_text("id\tvalue\nctg1\t1\n", encoding="utf-8")
    db.insert_row("files", {
        "file_id": "FIL_ESCAPE", "entity_type": "annotation", "entity_id": "ANN_000001",
        "file_role": "escaping_matrix", "format": "tsv", "compression": "none",
        "relative_path": "../outside.tsv", "size_bytes": outside.stat().st_size,
        "sha256": sha256_file(outside), "status": "CHECKSUM_VERIFIED",
    })
    with pytest.raises(ValidationError, match="escapes the project"):
        adopt_files(db, project, items=[{
            "path": str(outside), "entity_type": "annotation",
            "entity_id": "ANN_000001", "role": "escaping_matrix",
            "format": "tsv", "compression": "none",
            "derived_from": [source["file_id"]],
        }])


def test_adopt_registers_a_preexisting_identical_target(fanout_project, tmp_path):
    project, db, source, _assignments = fanout_project
    derived = tmp_path / "matrix.tsv"
    derived.write_text("id\tvalue\nctg1\t1\n", encoding="utf-8")
    target = (project.analysis_root / "adopted" / "ANN_000001"
              / "ANN_000001.pangenome_matrix.tsv")
    target.parent.mkdir(parents=True)
    target.write_bytes(derived.read_bytes())
    record = adopt_files(db, project, items=[{
        "path": str(derived), "entity_type": "annotation", "entity_id": "ANN_000001",
        "role": "pangenome_matrix", "format": "tsv", "compression": "none",
        "derived_from": [source["file_id"]],
    }])[0]
    assert record["relative_path"] == "analysis/adopted/ANN_000001/ANN_000001.pangenome_matrix.tsv"
    assert target.read_text(encoding="utf-8") == derived.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# operon/sequence_tools.py: region validation and selection criteria
# ---------------------------------------------------------------------------


@pytest.fixture
def seq_project(tmp_path: Path):
    assert main(["--project", str(tmp_path), "init", str(tmp_path),
                 "--project-id", "PRJ_SEQ_9"]) == 0
    project = load_project(tmp_path)
    db = Database(project.db_path)
    db.insert_row("organisms", {"organism_id": "ORG_000001", "scientific_name": "Testus"})
    db.insert_row("samples", {"sample_id": "SMP_000001", "organism_id": "ORG_000001"})
    db.insert_row("assemblies", {"assembly_id": "ASM_000001", "sample_id": "SMP_000001"})
    db.insert_row("annotations", {
        "annotation_id": "ANN_000001", "assembly_id": "ASM_000001",
        "annotation_source": "test", "annotation_version": 1,
    })
    fasta = tmp_path / "proteins.faa"
    fasta.write_text(
        ">p1\n" + "M" * 50 + "\n>p2\n" + "C" * 40 + "\n>p3\n" + "G" * 20 + "\n",
        encoding="utf-8")
    record = ingest_file(db, project, fasta, "annotation", "ANN_000001", "protein_fasta")
    for seqid, length in (("p1", 50), ("p2", 40), ("p3", 20)):
        db.insert_row("sequences", {
            "file_id": record["file_id"], "file_sha256": record["sha256"],
            "entity_type": "annotation", "entity_id": "ANN_000001",
            "seqid": seqid, "length": length,
        })
    db.insert_row("analysis_jobs", {
        "analysis_name": "cdd", "entity_type": "annotation", "entity_id": "ANN_000001",
        "file_id": record["file_id"], "tool": "faketool", "tool_version": "1.0",
        "parameter_set": "default", "parameter_sha256": "p" * 64,
        "input_sha256": record["sha256"], "database_identity": "db",
        "status": "completed", "started_at": "2026-01-01T00:00:00+00:00",
    })
    job = db.query("SELECT max(job_id) AS j FROM analysis_jobs")[0]["j"]
    alignments = [
        # No coordinates at all: excluded as missing_coordinates.
        {"query_id": "p1", "subject_id": "s1", "hit_rank": 1,
         "query_start": None, "query_end": None, "evalue": 1e-5},
        # Coordinates beyond the sequence: excluded as region_outside_sequence.
        {"query_id": "p3", "subject_id": "s2", "hit_rank": 1,
         "query_start": 50, "query_end": 90, "evalue": 1e-6},
        # Usable centered region on p2.
        {"query_id": "p2 description", "subject_id": "s3", "hit_rank": 1,
         "query_start": 10, "query_end": 39, "evalue": 1e-8},
    ]
    for alignment in alignments:
        db.insert_row("analysis_alignments", {
            "job_id": job, "entity_type": "annotation", "entity_id": "ANN_000001",
            "file_id": record["file_id"], "analysis_name": "cdd",
            "subject_id": alignment["subject_id"], "hit_rank": alignment["hit_rank"],
            "query_id": alignment["query_id"], "query_start": alignment["query_start"],
            "query_end": alignment["query_end"], "evalue": alignment["evalue"],
            "extra_json": None,
        })
    try:
        yield project, db, record
    finally:
        db.close()


def test_extract_domains_requires_exactly_one_region_source(seq_project, tmp_path):
    project, db, record = seq_project
    out = tmp_path / "out.faa"
    with pytest.raises(ValidationError, match="exactly one of --analysis or --regions-tsv"):
        sequence_tools.extract_domains(db, project, file_id=record["file_id"],
                                       out=out, command="extract")
    regions = tmp_path / "regions.tsv"
    regions.write_text("seqid\tstart\tend\np1\t1\t10\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="only apply to --analysis regions"):
        sequence_tools.extract_domains(
            db, project, file_id=record["file_id"], out=out, command="extract",
            regions_tsv=regions, subject_like="bhlh%")
    assert not out.exists()


def test_extract_domains_rejects_malformed_region_rows(seq_project, tmp_path):
    project, db, record = seq_project
    out = tmp_path / "out.faa"
    cases = [
        ("seqid\tstart\tend\n\t1\t10\n", "empty seqid"),
        ("seqid\tstart\tend\np1\tx\t10\n", "start/end must be integers"),
        ("seqid\tstart\tend\np1\t0\t10\n", r"require 1 <= start <= end"),
        ("seqid\tstart\tend\np1\t20\t10\n", r"require 1 <= start <= end"),
        ("seqid\tstart\tend\tevalue\np1\t1\t10\tabc\n", "evalue must be numeric"),
    ]
    for index, (text, message) in enumerate(cases):
        regions = tmp_path / f"regions{index}.tsv"
        regions.write_text(text, encoding="utf-8")
        with pytest.raises(ValidationError, match=message):
            sequence_tools.extract_domains(
                db, project, file_id=record["file_id"], out=out, command="extract",
                regions_tsv=regions)
    assert not out.exists()
    assert db.query(
        "SELECT COUNT(*) AS n FROM workflow_runs WHERE step='extract-domains'")[0]["n"] == 0


def test_extract_domains_reports_every_exclusion_reason(seq_project, tmp_path):
    project, db, record = seq_project
    out = tmp_path / "domains.faa"
    manifest = tmp_path / "domains.tsv"
    result = sequence_tools.extract_domains(
        db, project, file_id=record["file_id"], out=out, command="extract",
        analysis="cdd", manifest=manifest)
    assert (result["extracted"], result["excluded"]) == (1, 2)
    rows = {row["seqid"]: row for row in _read_tsv(manifest)}
    assert rows["p1"]["excluded_reason"] == "missing_coordinates"
    assert rows["p3"]["excluded_reason"] == "region_outside_sequence"
    assert rows["p2"]["excluded_reason"] == ""
    assert rows["p2"]["extracted_start"] == "5" and rows["p2"]["extracted_end"] == "40"
    assert out.read_text(encoding="utf-8").startswith(">p2\n")
    run = db.query("SELECT * FROM workflow_runs WHERE step='extract-domains'")[0]
    details = json.loads(run["execution_details"])
    assert (details["extracted"], details["excluded"], details["mode"]) == (1, 2, "best-only")


def test_select_sequences_accepts_evalue_only_and_entity_narrowing(seq_project, tmp_path):
    project, db, record = seq_project
    out = tmp_path / "selected.faa"
    # evalue-max alone exercises the criteria builder without an analysis filter.
    result = sequence_tools.select_sequences(
        db, project, file_id=record["file_id"], out=out, command="select",
        evalue_max=1e-7)
    assert (result["total"], result["selected"]) == (3, 1)
    assert out.read_text(encoding="utf-8").startswith(">p2\n")

    narrow = tmp_path / "narrow.faa"
    result = sequence_tools.select_sequences(
        db, project, file_id=record["file_id"], out=narrow, command="select",
        analyses=["cdd"], entity_type="annotation", entity_id="ANN_000001")
    assert (result["total"], result["selected"]) == (3, 3)
    no_match = tmp_path / "no-match.faa"
    result = sequence_tools.select_sequences(
        db, project, file_id=record["file_id"], out=no_match, command="select",
        analyses=["cdd"], entity_type="annotation", entity_id="ANN_999999")
    assert result["selected"] == 0
    assert no_match.read_text(encoding="utf-8") == ""


def test_sequence_tools_reject_unknown_and_missing_source_files(seq_project, tmp_path):
    project, db, record = seq_project
    out = tmp_path / "out.faa"
    with pytest.raises(ValidationError, match="does not exist in the manifest"):
        sequence_tools.select_sequences(
            db, project, file_id="FIL_999999", out=out, command="select",
            analyses=["cdd"])
    sequence_path = project.root / record["relative_path"]
    sequence_path.unlink()
    with pytest.raises(ValidationError, match="bytes are missing"):
        sequence_tools.select_sequences(
            db, project, file_id=record["file_id"], out=out, command="select",
            analyses=["cdd"])
    assert not out.exists()


def test_coverage_publish_failure_before_rename_removes_the_staging_tree(
        project_db, tmp_path, monkeypatch):
    project, db = project_db
    reference = _import_taxonomy(
        project, db, tmp_path, ranks=["genus"],
        thresholds={"genus": {"min_coverage_percent": 0}}, root_taxids=[1],
    )
    db.insert_row("organisms", {
        "organism_id": "ORG_000001", "scientific_name": "Covered",
        "taxon_id": 20, "taxonomy_source": "NCBI",
    })

    def failing_replace(_source, _target):
        raise OSError("publish failed")

    monkeypatch.setattr(coverage.os, "replace", failing_replace)
    with pytest.raises(OSError, match="publish failed"):
        coverage.report_coverage(db, project, reference["reference_set_id"])
    reports_root = project.reports_root / "coverage"
    assert not reports_root.exists() or list(reports_root.iterdir()) == []
    assert db.query("SELECT COUNT(*) AS n FROM coverage_reports")[0]["n"] == 0


def test_published_coverage_report_survives_a_provenance_log_failure(project_db, tmp_path):
    project, db = project_db
    reference = _import_taxonomy(
        project, db, tmp_path, ranks=["genus"],
        thresholds={"genus": {"min_coverage_percent": 0}}, root_taxids=[1],
    )
    db.insert_row("organisms", {
        "organism_id": "ORG_000001", "scientific_name": "Covered",
        "taxon_id": 20, "taxonomy_source": "NCBI",
    })
    db.conn.execute(
        "CREATE TRIGGER fail_coverage_run BEFORE INSERT ON workflow_runs "
        "BEGIN SELECT RAISE(ABORT, 'injected log failure'); END;"
    )
    db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected log failure"):
        coverage.report_coverage(db, project, reference["reference_set_id"])
    row = db.query("SELECT relative_path FROM coverage_reports")[0]
    published = project.root / row["relative_path"]
    assert (published / "coverage_summary.tsv").is_file()
    assert (published / "provenance.json").is_file()

    # Once the log path works again the frozen report is reused, not rebuilt.
    db.conn.execute("DROP TRIGGER fail_coverage_run")
    db.conn.commit()
    reused = coverage.report_coverage(db, project, reference["reference_set_id"])
    assert reused["reused"] is True
    assert reused["path"] == str(published)


def test_subject_matching_tolerates_missing_and_malformed_hit_metadata(seq_project, tmp_path):
    """Hits without matching subject metadata never match, and never raise."""
    project, db, record = seq_project
    job = db.query("SELECT max(job_id) AS j FROM analysis_jobs")[0]["j"]
    for query_id, subject_id, extra_json in (
            ("p1", "nope", "{broken"),   # unparsable extra JSON
            ("p3", "zzz", None),         # no extra JSON at all
    ):
        db.insert_row("analysis_alignments", {
            "job_id": job, "entity_type": "annotation", "entity_id": "ANN_000001",
            "file_id": record["file_id"], "analysis_name": "cdd",
            "query_id": query_id, "subject_id": subject_id, "hit_rank": 1,
            "query_start": 1, "query_end": 20, "evalue": 1e-9,
            "extra_json": extra_json,
        })
    out = tmp_path / "matched.faa"
    result = sequence_tools.select_sequences(
        db, project, file_id=record["file_id"], out=out, command="select",
        subject_like="s3")
    assert (result["total"], result["selected"]) == (3, 1)
    assert out.read_text(encoding="utf-8").startswith(">p2\n")


def test_fanout_adopts_a_preexisting_identical_unit_target(fanout_project, tmp_path):
    """An unregistered target with identical bytes is registered, not refused."""
    project, db, source, assignments = fanout_project
    target = (project.analysis_root / "derived" / "ANN_000001"
              / "ANN_000001.subfamily_alignment:SF01.fasta")
    target.parent.mkdir(parents=True)
    target.write_text(">p1\nMPEPTIDE\n", encoding="utf-8")
    result = _fanout(db, project, source, assignments)
    unit = {row["unit"]: row for row in result["units"]}["SF01"]
    assert unit["status"] == "created"
    assert (project.root / unit["relative_path"]).read_text(encoding="utf-8") == (
        ">p1\nMPEPTIDE\n")
    assert db.query("SELECT COUNT(*) AS n FROM files WHERE file_role=?",
                    (unit["role"],))[0]["n"] == 1
