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


def _requirement_lists(pyproject: dict, section: str) -> list[str]:
    if section == "build-system":
        return list(pyproject["build-system"]["requires"])
    if section == "dependencies":
        return list(pyproject["project"]["dependencies"])
    return list(pyproject["project"]["optional-dependencies"][section])


def _package_name(requirement: str) -> str:
    return re.split(r"[<>=!;\s\[]", requirement, maxsplit=1)[0].lower()


@pytest.mark.parametrize(
    ("section", "required", "marker", "forbidden", "only"),
    [
        # Building the Cython extension needs setuptools + Cython, nothing else.
        ("build-system", ("cython>=3.0", "setuptools>="), None, (), ("cython", "setuptools")),
        # Cython is a build/test tool: it must never become a runtime dependency.
        ("dependencies", (), None, ("cython",), ()),
        ("test", ("cython>=3.0",), None, (), ()),
        ("dev", ("cython>=3.0", "tomli>=2.0"), "python_version < '3.11'", (), ()),
        ("docs", ("tomli>=2.0",), "python_version < '3.11'", (), ()),
    ],
    ids=["build-system", "runtime-dependencies", "test-extra", "dev-extra", "docs-extra"],
)
def test_pyproject_dependency_contract(section, required, marker, forbidden, only):
    """The requirement sets that keep the package buildable and installable.

    Every case reads the same pyproject requirement lists, so they share one
    parametrized check instead of four near-identical test bodies.
    """
    requirements = _requirement_lists(_load_pyproject(), section)
    lowered = [requirement.lower() for requirement in requirements]
    for prefix in required:
        assert any(requirement.startswith(prefix) for requirement in lowered), \
            f"{section} must require {prefix}: {requirements}"
    for prefix in forbidden:
        assert not any(requirement.startswith(prefix) for requirement in lowered), \
            f"{section} must not require {prefix}: {requirements}"
    if only:
        assert sorted(_package_name(requirement) for requirement in requirements) == sorted(only), \
            requirements
    if marker:
        # Python 3.10 has no stdlib tomllib, so the conditional pin must stay
        # attached to the requirement instead of installing it unconditionally.
        conditional = [r for r in requirements if _package_name(r) == "tomli"]
        assert conditional and all(marker in requirement for requirement in conditional), requirements


def test_pyproject_is_the_single_application_version_source():
    pyproject = _load_pyproject()
    assert pyproject["project"]["version"] == operon.__version__
    assert 'version = "' not in (ROOT / "operon" / "__init__.py").read_text(
        encoding="utf-8"
    )
