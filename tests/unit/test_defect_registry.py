"""Validate the defect registry (defects.yml + defects/*.yml) and its test closure.

Every ``@pytest.mark.bug("ODR-XXXX")`` in the suite must name a registry
record, and every registry record whose status is ``fixed``/``verified``
must list at least one existing regression test carrying its marker.
"""

from __future__ import annotations

import ast
import datetime
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_DEFECTS_SPEC = importlib.util.spec_from_file_location(
    "operon_defects_format", ROOT / "scripts" / "defects.py"
)
assert _DEFECTS_SPEC and _DEFECTS_SPEC.loader
defects_format = importlib.util.module_from_spec(_DEFECTS_SPEC)
_DEFECTS_SPEC.loader.exec_module(defects_format)

ID_PATTERN = re.compile(r"^ODR-\d+$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_FIELDS = {
    "id",
    "title",
    "reported",
    "introduced_in",
    "affected",
    "severity",
    "component",
    "status",
    "reproduction",
    "disposition",
    "fix_commit",
    "fixed_in",
    "regression_tests",
}
SEVERITIES = {"low", "medium", "high", "critical"}
STATUSES = {"open", "confirmed", "fixed", "verified", "wontfix", "duplicate"}


def _registry_sources() -> list[Path]:
    sources = []
    root_file = ROOT / "defects.yml"
    if root_file.exists():
        sources.append(root_file)
    shard_dir = ROOT / "defects"
    if shard_dir.is_dir():
        sources.extend(sorted(shard_dir.glob("*.yml")))
    return sources


def _load_registry() -> list[dict]:
    defects: list[dict] = []
    for path in _registry_sources():
        doc = defects_format.load_document(path.read_text(encoding="utf-8"))
        for record in doc.get("defects") or []:
            record["_source"] = path.name
            defects.append(record)
    return defects


def _bug_markers() -> dict[str, set[str]]:
    """Map "path::qualified_test_name" -> defect ids from @pytest.mark.bug."""
    marked: dict[str, set[str]] = {}
    for path in sorted((ROOT / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            ids = set()
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "bug"
                    and isinstance(decorator.func.value, ast.Attribute)
                    and decorator.func.value.attr == "mark"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)
                ):
                    ids.add(decorator.args[0].value)
            if ids:
                marked[f"{path.relative_to(ROOT)}::{node.name}"] = ids
    return marked


def test_registry_schema():
    defects = _load_registry()
    assert defects, "defect registry is missing or empty"
    seen: set[str] = set()
    for record in defects:
        where = f"{record['_source']}:{record.get('id')!r}"
        missing = REQUIRED_FIELDS - set(record)
        assert not missing, f"{where}: missing fields {sorted(missing)}"
        assert ID_PATTERN.match(record["id"]), f"{where}: malformed id"
        assert record["id"] not in seen, f"{where}: duplicate id"
        seen.add(record["id"])
        datetime.date.fromisoformat(str(record["reported"]))
        assert record["severity"] in SEVERITIES, f"{where}: bad severity"
        assert record["status"] in STATUSES, f"{where}: bad status"
        commit = record["fix_commit"]
        assert commit is None or COMMIT_PATTERN.match(str(commit)), (
            f"{where}: fix_commit must be null or a 40-hex commit id"
        )
        if record["status"] in {"fixed", "verified"}:
            assert record["regression_tests"], (
                f"{where}: status {record['status']} requires regression tests"
            )
        if record["status"] == "verified":
            assert commit is not None, f"{where}: verified requires fix_commit"
            assert record["fixed_in"], f"{where}: verified requires fixed_in"


def test_registry_storage_format():
    """The on-disk registry is exactly what the formatter would write.

    That pins the rules the formatter enforces: ISO dates stay single-quoted
    strings, long text is folded at one width, and a paragraph break is one
    blank line rather than two. A hand edit that drifts from the format fails
    here instead of landing silently.
    """
    for path in _registry_sources():
        text = path.read_text(encoding="utf-8")
        rendered = defects_format.render_document(defects_format.load_document(text))
        assert text == rendered, (
            f"{path.name} is not in the canonical registry format; "
            "rewrite it with scripts/defects.py's render_document"
        )


def test_registry_format_quotes_dates_and_folds_long_text():
    raw = (
        "schema: 1\n"
        "defects:\n"
        "- id: ODR-1\n"
        "  reported: 2026-09-24\n"
        "  title: " + ("word " * 40) + "\n"
        "  reproduction: |\n"
        "    a paragraph\n"
        "\n"
        "\n"
        "    that was split by a double break\n"
        "  note: >\n"
        "    kept\n"
        "\n"
        "    apart\n"
    )
    rendered = defects_format.render_document(defects_format.load_document(raw))

    assert "reported: '2026-09-24'" in rendered
    assert not any(
        line.startswith("  reported:") and "'" not in line
        for line in rendered.splitlines()
    )
    assert "\n\n\n" not in rendered
    assert "a paragraph\n    \n    that was split" in rendered
    assert max(len(line) for line in rendered.splitlines()) <= defects_format.LINE_WIDTH
    assert (
        defects_format.render_document(defects_format.load_document(rendered))
        == rendered
    )


def test_regression_test_closure():
    defects = {record["id"]: record for record in _load_registry()}
    marked = _bug_markers()
    for node, ids in marked.items():
        for defect_id in ids:
            assert defect_id in defects, (
                f"{node} is marked with {defect_id}, which is not in the registry"
            )
    for defect_id, record in defects.items():
        for node in record["regression_tests"]:
            file_part, _, test_part = node.partition("::")
            assert (ROOT / file_part).is_file(), (
                f"{defect_id}: regression test file does not exist: {file_part}"
            )
            test_name = test_part.split("::")[-1]
            key = f"{file_part}::{test_name}"
            assert key in marked and defect_id in marked[key], (
                f'{defect_id}: {key} must carry @pytest.mark.bug("{defect_id}")'
            )
