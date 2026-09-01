"""Integration test for tools/collect_licenses.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "collect_licenses.py"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def collected(tmp_path_factory):
    output = tmp_path_factory.mktemp("licenses")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return output


def test_notices_lists_runtime_dependencies(collected):
    text = (collected / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").lower()
    for package in ("pyyaml", "requests", "aiohttp", "biopython", "paramiko", "questionary"):
        assert package in text


def test_transitive_dependencies_are_included(collected):
    text = (collected / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").lower()
    for package in ("urllib3", "certifi", "numpy", "prompt_toolkit", "cryptography"):
        assert package in text


def test_license_files_are_copied(collected):
    for package in ("pyyaml", "requests", "aiohttp"):
        package_dir = collected / package
        assert package_dir.is_dir()
        assert any(package_dir.rglob("*"))


def test_build_and_test_tooling_is_excluded(collected):
    for package in ("pytest", "coverage", "cx-freeze"):
        assert not (collected / package).exists()
    table_rows = [
        line
        for line in (collected / "THIRD_PARTY_NOTICES.md")
        .read_text(encoding="utf-8")
        .lower()
        .splitlines()
        if line.startswith("| ")
    ]
    for package in ("pytest", "coverage", "cx-freeze", "cx_freeze"):
        assert all(package not in row for row in table_rows)
