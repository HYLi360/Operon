"""Deterministic bundle, shared rendering and publication failure contracts."""

import json
from contextlib import closing

import pytest

from operon.cli import main
from operon.config import load_project
from operon.database import Database
from operon.demo import init_demo
from operon.errors import ConflictError, ValidationError
from operon.utils import sha256_file
from operon.view_bundle import export_view_bundle, render_qc_report


@pytest.fixture
def project(tmp_path):
    init_demo(tmp_path / "project")
    return load_project(tmp_path / "project")


def test_view_bundle_is_read_only_deterministic_and_matches_qc(
    project, tmp_path, capsys
):
    out = tmp_path / "view"
    before = project.db_path.read_bytes()
    assert (
        main(["--project", str(project.root), "report", "view", "--out", str(out)]) == 0
    )
    capsys.readouterr()
    assert project.db_path.read_bytes() == before
    snapshot = {
        p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()
    }
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["bundle_schema_version"] == 1
    for name, member in manifest["members"].items():
        assert sha256_file(out / name) == member["sha256"]
    assert (
        main(
            [
                "--project",
                str(project.root),
                "report",
                "qc",
                "--wide",
                "--format",
                "tsv",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.encode() == (out / "qc_wide.tsv").read_bytes()
    with closing(Database(project.db_path, read_only=True)) as db:
        assert export_view_bundle(db, out) == out
        wide_path = project.qc_root / "aggregate" / "qc_results.wide.tsv"
        from operon.reports import export_qc_tsv

        export_qc_tsv(db, project)
        assert wide_path.read_bytes() == (out / "qc_wide.tsv").read_bytes()
    assert {
        p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()
    } == snapshot


def test_bundle_failure_leaves_no_destination(project, tmp_path, monkeypatch):
    import operon.view_bundle as bundles

    out = tmp_path / "view"

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(bundles, "write_tsv", fail)
    with (
        closing(Database(project.db_path, read_only=True)) as db,
        pytest.raises(OSError, match="disk full"),
    ):
        export_view_bundle(db, out)
    assert not out.exists()
    assert not list(tmp_path.glob(".view.*"))


def test_bundle_conflicts_and_missing_tables(project, tmp_path, monkeypatch):
    out = tmp_path / "view"
    with closing(Database(project.db_path, read_only=True)) as db:
        export_view_bundle(db, out)
        (out / "files.tsv").write_text("different")
        with pytest.raises(ConflictError):
            export_view_bundle(db, out)
        assert (out / "files.tsv").read_text() == "different"
        regular_file = tmp_path / "file"
        regular_file.write_text("keep")
        with pytest.raises(ConflictError):
            export_view_bundle(db, regular_file)
        monkeypatch.setattr(db, "table_columns", lambda table: [])
        with pytest.raises(ValidationError, match="migrate"):
            export_view_bundle(db, tmp_path / "missing")


@pytest.mark.parametrize("fmt,wide", [("tsv", False), ("json", True), ("text", True)])
def test_qc_report_formats_and_filters(project, tmp_path, fmt, wide):
    out = tmp_path / "qc.txt"
    args = [
        "--project",
        str(project.root),
        "report",
        "qc",
        "--format",
        fmt,
        "--out",
        str(out),
        "--include-retired",
        "--entity-type",
        "assembly",
        "--entity-id",
        "ASM_000001",
    ]
    if wide:
        args.append("--wide")
    assert main(args) == 0
    with closing(Database(project.db_path, read_only=True)) as db:
        assert out.read_text() == render_qc_report(
            db,
            fmt,
            wide=wide,
            entity_type="assembly",
            entity_id="ASM_000001",
            include_retired=True,
        )
