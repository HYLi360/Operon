"""Integration tests for the PyPI packaging contract (sdist and metadata)."""

from __future__ import annotations

import os
import re
import tarfile
from pathlib import Path

import pytest

import operon

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"

pytestmark = pytest.mark.integration


def _load_pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _build_sdist(output: Path) -> Path:
    from setuptools import build_meta

    output.mkdir(parents=True, exist_ok=True)
    previous_cwd = Path.cwd()
    try:
        os.chdir(ROOT)
        filename = build_meta.build_sdist(os.fspath(output))
    finally:
        os.chdir(previous_cwd)
    return output / filename


def test_sdist_contains_complete_project_source(tmp_path):
    pyproject = _load_pyproject()
    name = re.sub(r"[-_.]+", "-", pyproject["project"]["name"]).lower()
    version = pyproject["project"]["version"]
    archive = _build_sdist(tmp_path)
    assert archive.name == f"{name}-{version}.tar.gz"
    prefix = f"{name}-{version}/"
    required = {
        f"{prefix}LICENSE",
        f"{prefix}MANIFEST.in",
        f"{prefix}README.md",
        f"{prefix}pyproject.toml",
        f"{prefix}setup.py",
        f"{prefix}operon/cli.py",
        f"{prefix}operon/qc/_parsers.pyx",
        f"{prefix}operon/tui/app.tcss",
        f"{prefix}operon/tui/assets/splash.png",
        f"{prefix}operon/tui/assets/splash.rgb.z",
        f"{prefix}.readthedocs.yaml",
        f"{prefix}docs/conf.py",
        f"{prefix}docs/requirements.txt",
        f"{prefix}docs/zh/architecture/index.md",
        f"{prefix}docs/en/architecture/index.md",
        f"{prefix}docs/_templates/layout.html",
        f"{prefix}docs/_static/operon.css",
        f"{prefix}docs/_static/language-switcher.js",
    }
    with tarfile.open(archive, "r:gz") as source_tar:
        names = set(source_tar.getnames())

    assert required <= names
    assert not any(
        name.endswith((".so", ".pyd", ".pyc", "_parsers.c"))
        or "/__pycache__/" in name
        or "/docs/_build/" in name
        or name.startswith(f"{prefix}tools/")
        for name in names
    )


def test_python_package_build_uses_only_setuptools_and_cython():
    pyproject = _load_pyproject()
    assert not any(
        requirement.startswith("cx-Freeze")
        for requirement in pyproject["build-system"]["requires"]
    )
    assert not any(
        requirement.startswith(("cx-Freeze", "Cython"))
        for requirement in pyproject["project"]["dependencies"]
    )


def test_no_extra_references_cxfreeze():
    pyproject = _load_pyproject()
    optional = pyproject["project"].get("optional-dependencies", {})
    for extra, requirements in optional.items():
        assert not any(
            requirement.lower().startswith("cx-freeze")
            for requirement in requirements
        ), f"extra {extra!r} still references cx-Freeze"


def test_cython_is_available_to_test_and_dev_tooling():
    pyproject = _load_pyproject()
    optional = pyproject["project"]["optional-dependencies"]
    for extra in ("test", "dev"):
        assert any(
            requirement.lower().startswith("cython>=3.0")
            for requirement in optional[extra]
        )


def test_python_310_tomli_is_an_explicit_docs_and_dev_dependency():
    pyproject = _load_pyproject()
    optional = pyproject["project"]["optional-dependencies"]
    for extra in ("docs", "dev"):
        assert any(
            requirement.startswith("tomli>=2.0")
            and "python_version < '3.11'" in requirement
            for requirement in optional[extra]
        )


def test_pyproject_is_the_single_application_version_source():
    pyproject = _load_pyproject()
    assert pyproject["project"]["version"] == operon.__version__
    assert 'version = "' not in (ROOT / "operon" / "__init__.py").read_text(
        encoding="utf-8"
    )
