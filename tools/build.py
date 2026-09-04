#!/usr/bin/env python3
"""Build a complete, versioned standalone Operon application release.

This is the single entry point for application releases. It compiles the
required Cython parser extension, strictly builds the Sphinx documentation,
collects third-party license texts, builds an sdist containing the
corresponding project source, freezes the executable, assembles the release
directory, and verifies the finished artifact.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from importlib import machinery, metadata
from pathlib import Path
from typing import Any, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        sys.exit(
            "Python 3.10 release builds require the 'tomli' package; "
            "install the project build extra with: "
            "python -m pip install -e '.[build]'"
        )

try:
    from packaging.requirements import Requirement
except ModuleNotFoundError:  # packaging normally ships with pip
    Requirement = None


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
DEFAULT_RELEASE_ROOT = ROOT / "build" / "release"
DEFAULT_BUNDLED_EXTRAS = ("docs",)

_LICENSE_FILE_RE = re.compile(
    r"^(licen[cs]e|copying|notice)(\..*)?$",
    re.IGNORECASE,
)


def _load_pyproject(pyproject: Path = PYPROJECT) -> dict[str, Any]:
    with pyproject.open("rb") as fh:
        return tomllib.load(fh)


def project_version(pyproject: Path = PYPROJECT) -> str:
    """Return the sole literal application version from pyproject.toml."""
    version = str(_load_pyproject(pyproject)["project"]["version"]).strip()
    if not version:
        raise RuntimeError(f"project.version is empty in {pyproject}")
    return version


def project_distribution_name(pyproject: Path = PYPROJECT) -> str:
    """Return the normalized Python distribution name."""
    name = str(_load_pyproject(pyproject)["project"]["name"]).strip()
    if not name:
        raise RuntimeError(f"project.name is empty in {pyproject}")
    return _normalize(name)


def source_distribution_filename(
    version: str,
    pyproject: Path = PYPROJECT,
) -> str:
    """Return the PEP 625 source-distribution filename."""
    return f"{project_distribution_name(pyproject)}-{version}.tar.gz"


def _normalize(name: str) -> str:
    """Normalize a distribution name per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str, extra: str) -> str | None:
    """Return a requirement's name when its environment marker applies."""
    if Requirement is not None:
        req = Requirement(requirement)
        if req.marker is not None and not req.marker.evaluate({"extra": extra}):
            return None
        return req.name
    if "extra ==" in requirement and f'extra == "{extra}"' not in requirement:
        return None
    return re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0]


def _load_dependency_roots(
    pyproject: Path,
    extras: Sequence[str],
) -> list[str]:
    project = _load_pyproject(pyproject)["project"]
    roots: list[str] = []
    for requirement in project.get("dependencies", []):
        if (name := _requirement_name(requirement, "")) is not None:
            roots.append(_normalize(name))
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        for requirement in optional.get(extra, []):
            if (name := _requirement_name(requirement, extra)) is not None:
                roots.append(_normalize(name))
    return roots


def _resolve_dependency_closure(
    roots: list[str],
) -> dict[str, metadata.Distribution]:
    """Resolve installed distributions reachable from the bundled roots."""
    seen: dict[str, metadata.Distribution] = {}
    queue = list(roots)
    missing_roots: list[str] = []
    root_names = set(roots)

    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            if name in root_names:
                missing_roots.append(name)
            else:
                print(
                    f"warning: transitive dependency '{name}' is not installed; "
                    "its license was skipped",
                    file=sys.stderr,
                )
            continue
        seen[name] = dist
        for requirement in metadata.requires(name) or []:
            if (dependency := _requirement_name(requirement, "")) is not None:
                queue.append(_normalize(dependency))

    if missing_roots:
        packages = ", ".join(sorted(set(missing_roots)))
        raise RuntimeError(
            f"bundled dependencies are not installed: {packages}; "
            "install the project build extra first"
        )
    return seen


def _license_sources(dist: metadata.Distribution) -> list[Path]:
    """Locate PEP 639 or legacy license files for one distribution."""
    dist_info = Path(dist._path)  # type: ignore[attr-defined]
    pep639_dir = dist_info / "licenses"
    if pep639_dir.is_dir():
        return [pep639_dir]
    return sorted(
        path
        for path in dist_info.iterdir()
        if path.is_file() and _LICENSE_FILE_RE.match(path.name)
    )


def _metadata_value(dist: metadata.Distribution, *keys: str) -> str:
    for key in keys:
        value = dist.metadata.get(key)
        if value:
            return value.strip()
    return ""


def _write_third_party_notices(
    output: Path,
    entries: list[tuple[str, metadata.Distribution]],
    missing: list[str],
) -> None:
    lines = [
        "# Third-Party Notices",
        "",
        "Operon itself is licensed under the GNU Affero General Public License,",
        "version 3 or later (see the `LICENSE` file at the top of this",
        "distribution).",
        "",
        "This standalone bundle also contains the third-party Python packages",
        "listed below. Each package remains under its own license; the full",
        "license texts are collected in the per-package directories next to",
        "this file.",
        "",
        "| Package | Version | License |",
        "| --- | --- | --- |",
    ]
    for name, dist in entries:
        display = _metadata_value(dist, "Name") or name
        license_expression = (
            _metadata_value(dist, "License-Expression", "License") or "unknown"
        )
        lines.append(f"| {display} | {dist.version} | {license_expression} |")
    if missing:
        lines += [
            "",
            "The following packages ship no license file upstream; their",
            "license is stated in the package metadata shown above:",
            "",
        ]
        lines += [f"- {name}" for name in missing]
    lines += [
        "",
        "The bundle further includes the CPython interpreter and its standard",
        "library, distributed under the Python Software Foundation License",
        "Version 2 (<https://docs.python.org/3/license.html>), and system",
        "shared libraries copied by cx_Freeze (for example OpenSSL, zlib and",
        "libffi), each of which remains under its own license as provided by",
        "the operating system.",
        "",
    ]
    (output / "THIRD_PARTY_NOTICES.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def collect_licenses(
    pyproject: Path,
    output: Path,
    extras: Sequence[str] = DEFAULT_BUNDLED_EXTRAS,
) -> None:
    """Collect licenses for runtime packages and rendered-doc assets."""
    roots = _load_dependency_roots(pyproject, extras)
    closure = _resolve_dependency_closure(roots)

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    entries: list[tuple[str, metadata.Distribution]] = []
    missing: list[str] = []
    for name, dist in sorted(closure.items()):
        sources = _license_sources(dist)
        if not sources:
            missing.append(_metadata_value(dist, "Name") or name)
            entries.append((name, dist))
            continue
        destination = output / name
        destination.mkdir()
        for source in sources:
            if source.is_dir():
                for item in sorted(source.rglob("*")):
                    if item.is_file():
                        target = destination / item.relative_to(source)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, target)
            else:
                shutil.copy2(source, destination / source.name)
        entries.append((name, dist))

    _write_third_party_notices(output, entries, missing)
    copied = len(entries) - len(missing)
    print(f"\033[32mCollected license files for {copied} distributions -> {output}\033[0m")
    for name in missing:
        print(
            f"\033[33mwarning: no license file found for '{name}' (metadata only)\033[0m",
            file=sys.stderr,
        )


def _run(
    args: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path = ROOT,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [os.fspath(arg) for arg in args]
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=capture_output,
        text=True,
    )


def build_cython_extension() -> Path:
    """Compile the production parser backend in place and return its path."""
    _run([sys.executable, "setup.py", "build_ext", "--inplace"])
    extension_dir = ROOT / "operon" / "qc_module"
    for suffix in machinery.EXTENSION_SUFFIXES:
        candidate = extension_dir / f"_parsers{suffix}"
        if candidate.is_file():
            return candidate
    raise RuntimeError("Cython parser extension was not produced")


def build_documentation(output: Path) -> Path:
    """Strictly build the bilingual Sphinx HTML documentation."""
    doctrees = output.with_name(f".{output.name}-doctrees")
    if output.exists():
        shutil.rmtree(output)
    if doctrees.exists():
        shutil.rmtree(doctrees)
    _run(
        [
            sys.executable,
            "-m",
            "sphinx",
            "-W",
            "--keep-going",
            "-b",
            "html",
            "-d",
            doctrees,
            "-D",
            "html_copy_source=0",
            "-D",
            "html_show_sourcelink=0",
            ROOT / "docs",
            output,
        ]
    )
    required = [
        output / "index.html",
        output / "zh" / "index.html",
        output / "en" / "index.html",
        output / "_static" / "language-switcher.js",
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        details = "\n".join(f"- {path}" for path in missing)
        raise RuntimeError(f"documentation build is incomplete:\n{details}")
    shutil.rmtree(doctrees, ignore_errors=True)
    shutil.rmtree(output / "_sources", ignore_errors=True)
    (output / ".buildinfo").unlink(missing_ok=True)
    return output


def build_source_distribution(output: Path, version: str) -> Path:
    """Create the corresponding-source sdist using the configured backend."""
    from setuptools import build_meta

    output.mkdir(parents=True, exist_ok=True)
    previous_cwd = Path.cwd()
    try:
        os.chdir(ROOT)
        filename = build_meta.build_sdist(os.fspath(output))
    finally:
        os.chdir(previous_cwd)
    archive = output / filename
    expected = output / source_distribution_filename(version)
    if archive != expected or not archive.is_file():
        raise RuntimeError(
            f"source distribution mismatch: expected {expected}, produced {archive}"
        )
    return archive


def freeze_application(output: Path) -> None:
    """Run cx_Freeze with a dynamically versioned output location."""
    _run(
        [
            sys.executable,
            "-m",
            "cx_Freeze",
            "build_exe",
            f"--build-exe={output}",
        ]
    )
    library = output / "lib"
    if not library.is_dir():
        raise RuntimeError(f"cx_Freeze output has no library directory: {library}")
    distribution = metadata.distribution(project_distribution_name())
    distribution_info = Path(distribution._path)  # type: ignore[attr-defined]
    shutil.copytree(
        distribution_info,
        library / distribution_info.name,
        dirs_exist_ok=True,
    )


def _executable_path(release: Path) -> Path:
    return release / ("operon.exe" if os.name == "nt" else "operon")


def verify_release(release: Path, version: str) -> None:
    """Check required artifacts and run the frozen executable smoke test."""
    executable = _executable_path(release)
    documentation = release / "share" / "doc" / "operon" / "html"
    required = [
        executable,
        release / "LICENSE",
        release / "licenses" / "THIRD_PARTY_NOTICES.md",
        release / "source" / source_distribution_filename(version),
        release / "share" / "doc" / "operon" / "README.md",
        release / "share" / "doc" / "operon" / "README_ZH.md",
        documentation / "index.html",
        documentation / "zh" / "index.html",
        documentation / "en" / "index.html",
        documentation / "_static" / "language-switcher.js",
    ]
    missing = [path for path in required if not path.exists()]
    if missing:
        details = "\n".join(f"- {path}" for path in missing)
        raise RuntimeError(f"release is incomplete:\n{details}")

    forbidden = [
        documentation / ".doctrees",
        documentation / ".buildinfo",
        documentation / "_sources",
        release / "share" / "doc" / "operon" / "docs",
    ]
    leaked = [path for path in forbidden if path.exists()]
    leaked.extend(documentation.rglob("*.pickle"))
    if leaked:
        details = "\n".join(f"- {path}" for path in leaked)
        raise RuntimeError(f"release contains documentation build cache:\n{details}")

    result = subprocess.run(
        [os.fspath(executable), "--version"],
        cwd=release,
        check=False,
        capture_output=True,
        text=True,
    )
    observed = f"{result.stdout}\n{result.stderr}".strip()
    expected = f"operon {version}"
    if result.returncode != 0:
        raise RuntimeError(
            f"frozen executable smoke test exited with {result.returncode}: "
            f"{observed or '<no output>'}"
        )
    if expected not in observed:
        raise RuntimeError(
            f"frozen executable reported an unexpected version: {observed!r}; "
            f"expected {expected!r}"
        )


def build_release(
    release_root: Path = DEFAULT_RELEASE_ROOT,
    *,
    force: bool = False,
) -> Path:
    """Build, assemble, verify, and publish one versioned release directory."""
    version = project_version()
    final_output = release_root / f"v{version}"
    if final_output.exists():
        if not force:
            raise RuntimeError(
                f"release already exists: {final_output}; rerun with --force "
                "to replace that exact version"
            )


    release_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".v{version}-",
        dir=release_root,
    ) as temporary:
        staging = Path(temporary)
        application = staging / "application"
        licenses = staging / "licenses"
        sources = staging / "sources"
        documentation = staging / "documentation"

        extension = build_cython_extension()
        print(f"\033[34mBuilt Cython parser extension -> {extension}\033[0m")
        build_documentation(documentation)
        print(f"\033[34mBuilt Sphinx documentation -> {documentation}\033[0m")
        collect_licenses(PYPROJECT, licenses)
        source_archive = build_source_distribution(sources, version)
        print(f"\033[34mBuilt corresponding source archive -> {source_archive}\033[0m")
        freeze_application(application)

        shutil.copytree(licenses, application / "licenses")
        source_output = application / "source"
        source_output.mkdir()
        shutil.copy2(source_archive, source_output / source_archive.name)
        documentation_output = application / "share" / "doc" / "operon" / "html"
        shutil.copytree(documentation, documentation_output)

        verify_release(application, version)

        previous_release = staging / "previous-release"
        if final_output.exists():
            os.replace(final_output, previous_release)
        try:
            os.replace(application, final_output)
        except BaseException:
            if previous_release.exists() and not final_output.exists():
                os.replace(previous_release, final_output)
            raise

    print(f"\033[32mComplete Operon release -> \033[35m{final_output}\033[0m")
    return final_output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--release-root",
        type=Path,
        default=DEFAULT_RELEASE_ROOT,
        help="parent directory for versioned releases "
             f"(default: build/release/{project_version()})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing release of this exact project version",
    )
    args = parser.parse_args(argv)
    build_release(args.release_root.resolve(), force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
