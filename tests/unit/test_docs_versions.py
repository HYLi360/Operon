"""Guard: documentation must not hardcode the current version markers.

Current versions in the Markdown sources are rendered through
``myst_substitutions`` (see ``docs/conf.py``): ``{{ operon_version }}``,
``{{ db_schema }}``, and ``{{ metadata_schema }}``. A literal occurrence of a
current version therefore means somebody bypassed the substitution — fail the
build instead of drifting. Historical version mentions are facts and stay
literal, either on an allowlisted era-pinned page or on a line carrying the
``<!-- version-pin -->`` marker.
"""

from __future__ import annotations

import re
from importlib.metadata import version as dist_version
from pathlib import Path

from operon.database import SCHEMA_VERSION
from operon.schema import METADATA_SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"

PIN_MARKER = "<!-- version-pin -->"

# Era-pinned pages: the historical migration inventory and the legacy-database
# recovery runbook. Their version numbers describe those eras and are revised
# by hand together with the page itself.
ALLOWLISTED_PAGES = frozenset(
    {
        "en/operations/database-compatibility.md",
        "zh/operations/database-compatibility.md",
        "en/operations/ncbi-recovery-migration.md",
        "zh/operations/ncbi-recovery-migration.md",
    }
)


def _current_markers() -> dict[str, str]:
    return {
        "operon": dist_version("OperonDBS"),
        "database schema": SCHEMA_VERSION,
        "metadata schema": METADATA_SCHEMA_VERSION,
    }


def _contains_version(value: str, line: str) -> bool:
    return re.search(rf"(?<![\d.]){re.escape(value)}(?![\d.])", line) is not None


def test_docs_do_not_hardcode_current_versions():
    markers = _current_markers()
    offenders = []
    for path in sorted(DOCS_DIR.rglob("*.md")):
        relative = path.relative_to(DOCS_DIR).as_posix()
        if relative in ALLOWLISTED_PAGES or relative.startswith("_build/"):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PIN_MARKER in line:
                continue
            for label, value in markers.items():
                if _contains_version(value, line):
                    offenders.append(
                        f"{relative}:{lineno}: hardcoded {label} version {value!r}; "
                        "use the matching myst_substitutions reference "
                        "({{ operon_version }} / {{ db_schema }} / {{ metadata_schema }}) "
                        f"or append {PIN_MARKER!r} if the literal pin is intentional"
                    )
    assert not offenders, "\n".join(offenders)
