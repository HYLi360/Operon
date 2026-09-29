"""Guard: ruff format must not be able to orphan a line suppression.

``ruff format`` wraps an over-long statement and leaves a trailing
``# noqa`` / ``# type: ignore`` / ``# pragma`` on the new closing line.
``ruff check --fix`` then reads that relocated comment as an unused
suppression and deletes it, so the suppression disappears without a
failing lint. A suppression that already sits where the formatter leaves
it cannot be lost that way.

The guard checks the checkout's **tracked** Python sources. Distribution
artifacts a build stages inside the tree (setuptools copies an sdist as
``<name>-<version>/`` before it writes the archive) are not source, and
listing them raced the build that removes them again (ODR-52).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Directories that can never hold project source: build outputs, editor and
# tool caches, and the data directories a managed project keeps at its root.
SKIP_DIRS = {
    ".git",
    ".idea",
    ".matrix",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "others",
    "reports",
}
SUPPRESSION_MARKERS = ("# noqa", "# type: ignore", "# pragma:")


def _tracked_python_files(root: Path) -> list[Path] | None:
    """The checkout's tracked Python files, or ``None`` outside a git checkout.

    ``setuptools`` stages an sdist archive as ``<name>-<version>/`` inside the
    project root while it builds (see
    ``tests/integration/test_python_packaging.py``), so under pytest-xdist this
    guard can list staging files that the build removes before they are read.
    The tracked set is exact — artifacts are untracked — and cannot race.
    """
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--", "*.py"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [root / name for name in listing.stdout.split("\0") if name]


def _walk_python_files(root: Path) -> list[Path]:
    """Fallback for checkouts without git: skip artifacts by their shape.

    ``PKG-INFO`` marks an unpacked distribution (an sdist staging tree or an
    ``*.egg-info`` directory), and a path that stopped being a regular file is
    not project source either.
    """
    artifacts = {
        child.name
        for child in root.iterdir()
        if child.is_dir() and (child / "PKG-INFO").is_file()
    }
    files: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if SKIP_DIRS.intersection(parts) or parts[0] in artifacts:
            continue
        if any(part.endswith(".egg-info") for part in parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def _python_files(root: Path = ROOT) -> list[Path]:
    return _tracked_python_files(root) or _walk_python_files(root)


def _suppressions(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if any(marker in line for marker in SUPPRESSION_MARKERS)
    ]


def test_ruff_format_does_not_move_line_suppressions():
    """Formatting the tree must leave every suppression comment where it is.

    Comparing the suppressing lines before and after a whole-file format
    catches the wrap that detaches a comment from the statement it covers.
    A comment ruff format rewrites is one ``ruff check --fix`` can delete.
    """
    displaced = []
    for path in _python_files():
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # A build could still remove an untracked staging file between the
            # listing and this read; tracked sources do not vanish.
            continue
        before = _suppressions(text)
        if not before:
            continue
        formatted = subprocess.run(
            ["ruff", "format", "--stdin-filename", str(path), "-"],
            input=text,
            text=True,
            capture_output=True,
            check=False,
        )
        if formatted.returncode != 0:
            displaced.append(
                f"{path.relative_to(ROOT)}: ruff format failed: "
                f"{formatted.stderr.strip()}"
            )
            continue
        after = _suppressions(formatted.stdout)
        if after != before:
            displaced.append(
                f"{path.relative_to(ROOT)}:\n    before: {before}\n    after:  {after}"
            )
    assert not displaced, (
        "ruff format rewrites line suppressions; a following "
        "ruff check --fix can delete the relocated comment as unused:\n"
        + "\n".join(displaced)
    )


def test_ruff_check_fix_drops_a_suppression_the_formatter_relocated():
    """Pin the failure mode: format relocates the noqa, then fix deletes it."""
    source = (
        "def function():\n"
        "    from aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbb import name"
        "  # noqa: PLC0415 - must stay attached to this import\n"
        "    return name\n"
    )
    formatted = subprocess.run(
        ["ruff", "format", "--stdin-filename", "sample.py", "-"],
        input=source,
        text=True,
        capture_output=True,
        check=False,
    )
    assert formatted.returncode == 0
    assert "# noqa" in formatted.stdout
    assert formatted.stdout != source

    fixed = subprocess.run(
        [
            "ruff",
            "check",
            "--fix",
            "--isolated",
            "--select",
            "RUF100",
            "--stdin-filename",
            "sample.py",
            "-",
        ],
        input=formatted.stdout,
        text=True,
        capture_output=True,
        check=False,
    )
    assert fixed.returncode == 0
    assert "# noqa" not in fixed.stdout


@pytest.mark.bug("ODR-52")
def test_python_files_skips_distribution_artifacts_without_git(tmp_path):
    """The no-git fallback must not list what a build leaves in the tree.

    A ``PKG-INFO`` staging tree, an ``*.egg-info`` directory and a path that
    stopped being a regular file are the shapes setuptools produces inside the
    project root; the guard crashed on the third one (ODR-52).
    """
    source = tmp_path / "operon"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    staging = tmp_path / "operondbs-0.9.0" / "docs"
    staging.mkdir(parents=True)
    (tmp_path / "operondbs-0.9.0" / "PKG-INFO").write_text(
        "Name: operondbs\n", encoding="utf-8"
    )
    (staging / "conf_common.py").write_text("VALUE = 2\n", encoding="utf-8")
    egg_info = tmp_path / "OperonDBS.egg-info"
    egg_info.mkdir()
    (egg_info / "metadata.py").write_text("VALUE = 3\n", encoding="utf-8")
    (tmp_path / "removed.py").symlink_to(tmp_path / "gone.py")

    assert [path.name for path in _walk_python_files(tmp_path)] == ["module.py"]


@pytest.mark.bug("ODR-52")
def test_python_files_in_a_checkout_are_the_tracked_set(tmp_path):
    """In a git checkout the listing is exactly the tracked files, so a tree a
    build stages in the project root is never listed — even while it exists.
    """
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    (tmp_path / "operon").mkdir()
    (tmp_path / "operon" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    init = ["git", "init", "-q"]
    add = ["git", "add", "operon/module.py"]
    for command in (init, add):
        subprocess.run(command, cwd=tmp_path, capture_output=True, check=True)
    staging = tmp_path / "operondbs-0.9.0"
    staging.mkdir()
    (staging / "conf_common.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert [path.name for path in _python_files(tmp_path)] == ["module.py"]
