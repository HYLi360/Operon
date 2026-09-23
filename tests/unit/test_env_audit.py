"""Environment reads are audited: user settings live in the XDG user configuration.

The user configuration (:mod:`operon.config`) owns every user-level setting;
environment variables stay supported and *outrank* the file, but the set of
variables ``operon`` reads directly must stay small, explicit and reasoned
about — a new ``os.environ`` read has to be annotated with ``# env-audit:``
and, when it reads a literal name, added to :data:`AUDITED_VARIABLES` here.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "operon"

ENV_MARKER = "# env-audit:"
_ENV_READ = re.compile(r"\bos\.(?:environ|getenv)\b")
_ENV_LITERAL = re.compile(
    r"""os\.(?:environ|getenv)\s*(?:\.get\(\s*|\[\s*|\(\s*)["']([A-Za-z0-9_]+)["']"""
)

AUDITED_VARIABLES: dict[str, str] = {
    "HOME": "environment capture: the home path is part of environment_id",
    "OPERON_PARITY_STRICT": "developer switch for the TUI/CLI parity gate",
}
"""Literal environment variables read through ``os.environ`` in the package.

Everything else the user can set (``OPERON_ACTOR``, ``USER``, ``LOGNAME``,
``USERNAME``, ``NCBI_EMAIL``, ``NCBI_API_KEY``, ``OPERON_SPLASH``,
``XDG_CONFIG_HOME``) is read through the ``environ`` mapping that
:func:`operon.config._environ` and :mod:`operon.secrets` take as a parameter,
so it stays injectable in tests and documented in one place per setting.
"""


def _python_files() -> list[Path]:
    return sorted(path for path in PACKAGE_ROOT.rglob("*.py"))


def test_every_environment_read_carries_an_audit_note() -> None:
    offenders: list[str] = []
    for path in _python_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _ENV_READ.search(line) and ENV_MARKER not in line:
                offenders.append(f"{path.relative_to(PACKAGE_ROOT.parent)}:{number}: {line.strip()}")
    assert not offenders, (
        "unaudited environment read(s) found — route the value through "
        "operon.config (or operon.secrets for credentials), or annotate the "
        "line with '# env-audit: <reason>':\n" + "\n".join(offenders)
    )


def test_literal_environment_variables_stay_on_the_audited_list() -> None:
    found: set[str] = set()
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        found.update(_ENV_LITERAL.findall(text))
    assert found == set(AUDITED_VARIABLES), (
        "the set of literal environment variables read through os.environ "
        f"changed: new {sorted(found - set(AUDITED_VARIABLES))}, gone "
        f"{sorted(set(AUDITED_VARIABLES) - found)} — update AUDITED_VARIABLES "
        "and document the variable before merging"
    )


def test_user_configuration_owns_no_secret_and_no_project_key() -> None:
    """The user file and the project file must not overlap or hold secrets."""
    from operon.config import USER_CONFIG_TYPES, has_secret_like_keys

    assert has_secret_like_keys({key: "x" for key in USER_CONFIG_TYPES}) == []
    assert not any(key.startswith(("storage.", "database.", "resources.", "execution."))
                   for key in USER_CONFIG_TYPES)
