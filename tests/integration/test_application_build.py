"""Integration tests for the complete application-release builder."""

from __future__ import annotations

import importlib.util
import tarfile
from pathlib import Path
from types import SimpleNamespace

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
        extras=("remote",),
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


def test_release_license_scope_includes_rendered_documentation():
    assert application_build.DEFAULT_BUNDLED_EXTRAS == ("remote", "docs")


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
    assert application_build.project_distribution_name() == "operondbs"
    assert 'version = "' not in (ROOT / "operon" / "__init__.py").read_text(
        encoding="utf-8"
    )


def test_freeze_includes_importlib_metadata_email_dependency():
    cxfreeze = application_build._load_pyproject()["tool"]["cxfreeze"]
    assert "email" in cxfreeze["build_exe"]["packages"]


def test_freeze_copies_distribution_metadata(tmp_path, monkeypatch):
    output = tmp_path / "application"
    (output / "lib").mkdir(parents=True)
    distribution_info = tmp_path / "operondbs-0.6.2.dist-info"
    distribution_info.mkdir()
    (distribution_info / "METADATA").write_text(
        "Name: OperonDBS\nVersion: 0.6.2\n",
        encoding="utf-8",
    )
    observed = []

    monkeypatch.setattr(
        application_build,
        "_run",
        lambda args, **kwargs: observed.append((args, kwargs)),
    )
    monkeypatch.setattr(
        application_build.metadata,
        "distribution",
        lambda name: SimpleNamespace(_path=distribution_info),
    )

    application_build.freeze_application(output)

    assert (output / "lib" / distribution_info.name / "METADATA").is_file()
    assert observed == [
        (
            [
                application_build.sys.executable,
                "-m",
                "cx_Freeze",
                "build_exe",
                f"--build-exe={output}",
            ],
            {},
        )
    ]


def test_freeze_inputs_use_rendered_documentation_only():
    cxfreeze = application_build._load_pyproject()["tool"]["cxfreeze"]
    include_files = cxfreeze["build_exe"]["include_files"]
    assert ["README_ZH.md", "share/doc/operon/README_ZH.md"] in include_files
    assert not any(source == "docs" for source, _ in include_files)


def test_build_extra_contains_documentation_toolchain():
    project = application_build._load_pyproject()["project"]
    requirements = project["optional-dependencies"]["build"]
    for package in ("Sphinx", "myst-parser", "sphinx-rtd-theme"):
        assert any(requirement.startswith(package) for requirement in requirements)


def test_python_package_build_is_independent_of_cxfreeze():
    pyproject = application_build._load_pyproject()
    assert not any(
        requirement.startswith("cx-Freeze")
        for requirement in pyproject["build-system"]["requires"]
    )
    assert not any(
        requirement.startswith(("cx-Freeze", "Cython"))
        for requirement in pyproject["project"]["dependencies"]
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


def test_documentation_build_is_strict_and_checks_outputs(tmp_path, monkeypatch):
    output = tmp_path / "html"
    stale = output / "stale.txt"
    stale.parent.mkdir()
    stale.write_text("stale", encoding="utf-8")
    observed = []

    def fake_run(args, **kwargs):
        observed.append((args, kwargs))
        for relative in (
            "index.html",
            "zh/index.html",
            "en/index.html",
            "_static/language-switcher.js",
        ):
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(relative, encoding="utf-8")
        (output / ".buildinfo").write_text("cache", encoding="utf-8")
        doctree = output.with_name(".html-doctrees") / "index.doctree"
        doctree.parent.mkdir()
        doctree.write_text("cache", encoding="utf-8")
        (output / "_sources").mkdir()

    monkeypatch.setattr(application_build, "_run", fake_run)

    assert application_build.build_documentation(output) == output
    assert not stale.exists()
    assert not (output / ".buildinfo").exists()
    assert not output.with_name(".html-doctrees").exists()
    assert not (output / "_sources").exists()
    args, kwargs = observed[0]
    assert args == [
        application_build.sys.executable,
        "-m",
        "sphinx",
        "-W",
        "--keep-going",
        "-b",
        "html",
        "-d",
        output.with_name(".html-doctrees"),
        "-D",
        "html_copy_source=0",
        "-D",
        "html_show_sourcelink=0",
        application_build.ROOT / "docs",
        output,
    ]
    assert kwargs == {}


def test_source_distribution_contains_corresponding_source(tmp_path):
    version = application_build.project_version()
    archive = application_build.build_source_distribution(tmp_path, version)
    prefix = f"{application_build.project_distribution_name()}-{version}/"
    required = {
        f"{prefix}LICENSE",
        f"{prefix}MANIFEST.in",
        f"{prefix}README.md",
        f"{prefix}pyproject.toml",
        f"{prefix}setup.py",
        f"{prefix}operon/cli.py",
        f"{prefix}operon/qc_module/_parsers.pyx",
        f"{prefix}operon/tui/app.tcss",
        f"{prefix}tools/build.py",
        f"{prefix}tests/integration/test_application_build.py",
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
