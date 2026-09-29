"""Guard: ruff format must not be able to orphan a line suppression.

``ruff format`` wraps an over-long statement and leaves a trailing
``# noqa`` / ``# type: ignore`` / ``# pragma`` on the new closing line.
``ruff check --fix`` then reads that relocated comment as an unused
suppression and deletes it, so the suppression disappears without a
failing lint. A suppression that already sits where the formatter leaves
it cannot be lost that way.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {".venv", ".matrix", "build", ".git", "__pycache__", "others", "reports"}
SUPPRESSION_MARKERS = ("# noqa", "# type: ignore", "# pragma:")


def _python_files() -> list[Path]:
    return [
        path
        for path in sorted(ROOT.rglob("*.py"))
        if not SKIP_DIRS.intersection(path.relative_to(ROOT).parts)
    ]


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
        text = path.read_text(encoding="utf-8")
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
