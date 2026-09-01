#!/usr/bin/env python3
"""Collect third-party license texts for the standalone (cx_Freeze) release.

The release bundle ships not only Operon itself (AGPL-3.0-or-later, see the
top-level LICENSE file) but also every Python distribution that cx_Freeze
freezes into it. Most licenses require the license text to accompany the
software, so this script:

1. reads the runtime dependency set from ``pyproject.toml`` (core
   dependencies plus the optional extra that is bundled, ``remote``),
2. resolves the full transitive closure against the *current* environment,
3. copies each distribution's license files (the PEP 639 ``licenses/``
   directory, or legacy ``LICENSE``/``COPYING``/``NOTICE`` files) into a
   staging directory, and
4. writes a ``THIRD_PARTY_NOTICES.md`` summary next to them.

Run it before ``python -m cx_Freeze build``; ``pyproject.toml`` then packs
the staging directory into the release via ``include_files``.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    sys.exit("tools/collect_licenses.py requires Python >= 3.11 (tomllib).")

try:
    from packaging.requirements import Requirement
except ModuleNotFoundError:  # packaging ships with pip; fall back if absent
    Requirement = None

DEFAULT_OUTPUT = Path("build/licenses")
DEFAULT_EXTRA = "remote"

_LICENSE_FILE_RE = re.compile(r"^(licen[cs]e|copying|notice)(\..*)?$", re.IGNORECASE)


def _normalize(name: str) -> str:
    """Normalize a distribution name per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str, extra: str) -> str | None:
    """Return the distribution name of a requirement, or None if its marker
    excludes it from the bundled set (e.g. an unselected ``extra``)."""
    if Requirement is not None:
        req = Requirement(requirement)
        if req.marker is not None and not req.marker.evaluate({"extra": extra}):
            return None
        return req.name
    if "extra ==" in requirement and f'extra == "{extra}"' not in requirement:
        return None
    return re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0]


def _load_roots(pyproject: Path, extra: str) -> list[str]:
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    project = data["project"]
    deps = list(project.get("dependencies", []))
    deps.extend(project.get("optional-dependencies", {}).get(extra, []))
    return [
        _normalize(name)
        for req in deps
        if (name := _requirement_name(req, extra)) is not None
    ]


def _resolve_closure(roots: list[str], extra: str) -> dict[str, metadata.Distribution]:
    """Walk the dependency graph and return {normalized_name: Distribution}."""
    seen: dict[str, metadata.Distribution] = {}
    queue = list(roots)
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            print(
                f"warning: declared dependency '{name}' is not installed; skipped",
                file=sys.stderr,
            )
            continue
        seen[name] = dist
        for requirement in metadata.requires(name) or []:
            if (dep := _requirement_name(requirement, extra)) is not None:
                queue.append(_normalize(dep))
    return seen


def _license_sources(dist: metadata.Distribution) -> list[Path]:
    """Locate a distribution's license files: the PEP 639 ``licenses/``
    directory when present, else legacy top-level LICENSE/COPYING/NOTICE
    files inside ``*.dist-info``."""
    dist_info = Path(dist._path)  # distribution-locating path of the dist-info
    pep639_dir = dist_info / "licenses"
    if pep639_dir.is_dir():
        return [pep639_dir]
    return sorted(
        p
        for p in dist_info.iterdir()
        if p.is_file() and _LICENSE_FILE_RE.match(p.name)
    )


def _metadata_value(dist: metadata.Distribution, *keys: str) -> str:
    for key in keys:
        value = dist.metadata.get(key)
        if value:
            return value.strip()
    return ""


def _write_notices(
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
        license_expr = (
            _metadata_value(dist, "License-Expression", "License") or "unknown"
        )
        lines.append(f"| {display} | {dist.version} | {license_expr} |")
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
    (output / "THIRD_PARTY_NOTICES.md").write_text("\n".join(lines), encoding="utf-8")


def collect(pyproject: Path, output: Path, extra: str) -> int:
    roots = _load_roots(pyproject, extra)
    closure = _resolve_closure(roots, extra)

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
        dest = output / name
        dest.mkdir()
        for src in sources:
            if src.is_dir():
                for item in sorted(src.rglob("*")):
                    if item.is_file():
                        target = dest / item.relative_to(src)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, target)
            else:
                shutil.copy2(src, dest / src.name)
        entries.append((name, dist))

    _write_notices(output, entries, missing)

    print(f"Collected license files for {len(entries) - len(missing)} distributions -> {output}")
    for name in missing:
        print(f"warning: no license file found for '{name}' (metadata only)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        help="path to pyproject.toml (default: ./pyproject.toml)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"staging directory for collected licenses (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--extra",
        default=DEFAULT_EXTRA,
        help=f"optional-dependency extra bundled into the release (default: {DEFAULT_EXTRA})",
    )
    args = parser.parse_args(argv)
    return collect(args.pyproject, args.output, args.extra)


if __name__ == "__main__":
    sys.exit(main())
