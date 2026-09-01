"""Executable contracts for the advertised Python 3.10+ support window."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = sorted((ROOT / "operon").rglob("*.py"))
BUILD_SOURCE_FILES = [ROOT / "setup.py", ROOT / "tools" / "build.py"]


@pytest.mark.compatibility
def test_project_metadata_declares_python_310_or_newer() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^requires-python\s*=\s*">=3\.10"$', pyproject, re.MULTILINE)


@pytest.mark.compatibility
@pytest.mark.parametrize("source_path", SOURCE_FILES, ids=lambda path: str(path.relative_to(ROOT)))
def test_source_parses_with_python_310_grammar(source_path: Path) -> None:
    source = source_path.read_text(encoding="utf-8")
    ast.parse(source, filename=str(source_path), feature_version=(3, 10))


@pytest.mark.compatibility
@pytest.mark.parametrize(
    "source_path",
    BUILD_SOURCE_FILES,
    ids=lambda path: str(path.relative_to(ROOT)),
)
def test_build_source_parses_with_python_310_grammar(source_path: Path) -> None:
    source = source_path.read_text(encoding="utf-8")
    ast.parse(source, filename=str(source_path), feature_version=(3, 10))


@pytest.mark.compatibility
def test_runtime_is_within_supported_window() -> None:
    assert sys.version_info >= (3, 10)


@pytest.mark.compatibility
def test_cli_imports_on_supported_runtime() -> None:
    from operon.cli import main

    assert callable(main)
