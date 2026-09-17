"""Tests for the standalone defect registry CLI."""

from __future__ import annotations

import argparse
import importlib.util
import io
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "operon_defects_cli", ROOT / "scripts" / "defects.py"
)
assert SPEC and SPEC.loader
defects_cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(defects_cli)


def _record(**overrides):
    record = {
        "id": "ODR-0001",
        "title": "A long defect title that needs to wrap cleanly on narrow terminals",
        "reported": "2026-09-16",
        "introduced_in": None,
        "affected": "All releases when a particular sequence of operations is used.",
        "severity": "high",
        "component": "database",
        "status": "fixed",
        "reproduction": "Run the first operation and then repeat the second operation.",
        "disposition": "The operation now preserves the original state.",
        "fix_commit": "a" * 40,
        "fixed_in": None,
        "regression_tests": [
            "tests/regression/test_example.py::test_a_very_long_regression_name"
        ],
    }
    record.update(overrides)
    return record


def _write_registry(root: Path, records: list[dict]) -> None:
    (root / "defects.yml").write_text(
        yaml.safe_dump({"schema": 1, "defects": records}, sort_keys=False),
        encoding="utf-8",
    )


def test_list_uses_a_table_on_wide_terminals_and_wraps_the_title():
    rendered = defects_cli.render_list([_record()], 80)

    assert rendered.splitlines()[0].startswith("ID")
    assert "ODR-0001" in rendered
    assert "cleanly on narrow" in rendered.replace("\n", " ")
    assert max(map(len, rendered.splitlines())) <= 80


def test_list_uses_a_compact_layout_on_narrow_terminals():
    rendered = defects_cli.render_list([_record()], 48)

    assert rendered.splitlines()[0] == "ODR-0001  FIXED / HIGH"
    assert "[database]" in rendered
    assert max(map(len, rendered.splitlines())) <= 48


def test_show_formats_sections_and_wraps_long_values():
    rendered = defects_cli.render_detail(_record(), 58)

    assert "Reported      2026-09-16" in rendered
    assert "Reproduction\n" in rendered
    assert "Regression tests\n" in rendered
    assert max(map(len, rendered.splitlines())) <= 58


def test_main_keeps_list_and_case_insensitive_show_interfaces(tmp_path):
    _write_registry(tmp_path, [_record()])
    output = io.StringIO()

    result = defects_cli.main(
        ["list", "--status", "fixed"], root=tmp_path, stdout=output
    )
    assert result == 0
    assert "ODR-0001" in output.getvalue()

    output = io.StringIO()
    result = defects_cli.main(
        ["show", "odr-0001"], root=tmp_path, stdout=output
    )
    assert result == 0
    assert output.getvalue().startswith("ODR-0001  FIXED / HIGH")


def test_add_appends_the_next_id_with_the_existing_schema(tmp_path):
    _write_registry(tmp_path, [_record()])
    options = argparse.Namespace(
        title="New defect",
        reported="2026-09-17",
        affected=None,
        severity="medium",
        component="files",
        reproduction="Run it twice.",
        disposition=None,
    )

    new_id, target = defects_cli.Registry(tmp_path).append(options)

    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert new_id == "ODR-0002"
    assert document["defects"][-1] == {
        "id": "ODR-0002",
        "title": "New defect",
        "reported": "2026-09-17",
        "introduced_in": None,
        "affected": None,
        "severity": "medium",
        "component": "files",
        "status": "open",
        "reproduction": "Run it twice.",
        "disposition": None,
        "fix_commit": None,
        "fixed_in": None,
        "regression_tests": [],
    }


def test_add_uses_last_shard_when_root_registry_is_empty(tmp_path):
    (tmp_path / "defects.yml").write_text("", encoding="utf-8")
    shard_dir = tmp_path / "defects"
    shard_dir.mkdir()
    target = shard_dir / "2026.yml"
    target.write_text(
        yaml.safe_dump({"schema": 1, "defects": [_record()]}), encoding="utf-8"
    )
    options = argparse.Namespace(
        title="New defect",
        reported="2026-09-17",
        affected=None,
        severity="low",
        component="files",
        reproduction=None,
        disposition=None,
    )

    new_id, written_to = defects_cli.Registry(tmp_path).append(options)

    assert new_id == "ODR-0002"
    assert written_to == target
    assert (tmp_path / "defects.yml").read_text(encoding="utf-8") == ""
