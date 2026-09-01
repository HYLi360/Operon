"""Integration tests for the complete application-release builder."""

from __future__ import annotations

import importlib.util
import tarfile
from pathlib import Path

import pytest

import operon

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "build.py"

spec = importlib.util.spec_from_file_location("operon_application_build", SCRIPT)
assert spec is not None and spec.loader is not None
application_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(application_build)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def collected(tmp_path_factory):
    output = tmp_path_factory.mktemp("licenses")
    application_build.collect_licenses(
        ROOT / "pyproject.toml",
        output,
    )
    return output


def test_notices_lists_runtime_dependencies(collected):
    text = (collected / "THIRD_PARTY_NOTICES.md").read_text(
        encoding="utf-8"
    ).lower()
    for package in (
        "pyyaml",
        "requests",
        "aiohttp",
        "biopython",
        "paramiko",
        "questionary",
    ):
        assert package in text


def test_transitive_dependencies_are_included(collected):
    text = (collected / "THIRD_PARTY_NOTICES.md").read_text(
        encoding="utf-8"
    ).lower()
    for package in (
        "urllib3",
        "certifi",
        "numpy",
        "prompt_toolkit",
        "cryptography",
    ):
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


def test_pyproject_is_the_single_application_version_source():
    assert application_build.project_version() == operon.__version__
    assert 'version = "' not in (ROOT / "operon" / "__init__.py").read_text(
        encoding="utf-8"
    )


def test_python_310_tomli_is_an_explicit_build_dependency():
    project = application_build._load_pyproject()["project"]
    for extra in ("build", "dev"):
        requirements = project["optional-dependencies"][extra]
        assert any(
            requirement.startswith("tomli>=2.0")
            and "python_version < '3.11'" in requirement
            for requirement in requirements
        )


def test_source_distribution_contains_corresponding_source(tmp_path):
    version = application_build.project_version()
    archive = application_build.build_source_distribution(tmp_path, version)
    prefix = f"operon-{version}/"
    required = {
        f"{prefix}LICENSE",
        f"{prefix}MANIFEST.in",
        f"{prefix}README.md",
        f"{prefix}pyproject.toml",
        f"{prefix}setup.py",
        f"{prefix}operon/cli.py",
        f"{prefix}operon/qc_module/_parsers.pyx",
        f"{prefix}tools/build.py",
        f"{prefix}tests/integration/test_application_build.py",
        f"{prefix}docs/architecture.md",
    }
    with tarfile.open(archive, "r:gz") as source_tar:
        names = set(source_tar.getnames())

    assert required <= names
    assert not any(
        name.endswith((".so", ".pyd", ".pyc", "_parsers.c"))
        or "/__pycache__/" in name
        for name in names
    )


def test_existing_version_is_not_silently_overwritten(tmp_path):
    existing = tmp_path / f"v{application_build.project_version()}"
    existing.mkdir()
    with pytest.raises(RuntimeError, match="release already exists"):
        application_build.build_release(tmp_path)


def test_force_preserves_existing_release_when_rebuild_fails(tmp_path, monkeypatch):
    existing = tmp_path / f"v{application_build.project_version()}"
    existing.mkdir()
    marker = existing / "previous-release.txt"
    marker.write_text("keep me", encoding="utf-8")

    def fail_build():
        raise RuntimeError("synthetic build failure")

    monkeypatch.setattr(application_build, "build_cython_extension", fail_build)
    with pytest.raises(RuntimeError, match="synthetic build failure"):
        application_build.build_release(tmp_path, force=True)

    assert marker.read_text(encoding="utf-8") == "keep me"
