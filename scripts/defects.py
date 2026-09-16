#!/usr/bin/env python3
"""Append to and query the Operon defect registry."""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Sequence, TextIO

import yaml


ROOT = Path(__file__).resolve().parents[1]
SEVERITIES = ("low", "medium", "high", "critical")
STATUSES = ("open", "confirmed", "fixed", "verified", "wontfix", "duplicate")
HEADER = """# Operon defect registry (schema 1).
#
# One record per confirmed defect, appended in ID order. Records are loaded
# from this file plus any `defects/*.yml` shards (sorted by name), so this
# file can be split when it grows. Use `scripts/defects.sh` to append or
# query records; `tests/unit/test_defect_registry.py` validates the schema
# and the regression-test closure in both directions.
# See docs/en/contributor/defect-tracking.md for the process.
"""

RESET = "\033[0m"
BOLD = "\033[1m"
COLORS = {
    "low": "\033[36m",
    "medium": "\033[33m",
    "high": "\033[31m",
    "critical": "\033[1;31m",
    "open": "\033[31m",
    "confirmed": "\033[33m",
    "fixed": "\033[32m",
    "verified": "\033[36m",
    "wontfix": "\033[2m",
    "duplicate": "\033[2m",
}


class RegistryError(Exception):
    """A defect registry cannot be read or updated."""


class Registry:
    def __init__(self, root: Path = ROOT):
        self.root = root

    def sources(self) -> list[Path]:
        files = []
        root_file = self.root / "defects.yml"
        if root_file.exists():
            files.append(root_file)
        shard_dir = self.root / "defects"
        if shard_dir.is_dir():
            files.extend(sorted(shard_dir.glob("*.yml")))
        if not files:
            raise RegistryError(
                "no defect registry found (defects.yml or defects/*.yml)"
            )
        return files

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RegistryError(f"cannot read {path}: {exc}") from exc
        if not isinstance(document, dict):
            raise RegistryError(f"{path}: expected a YAML mapping")
        records = document.get("defects") or []
        if not isinstance(records, list) or not all(
            isinstance(record, dict) for record in records
        ):
            raise RegistryError(f"{path}: 'defects' must be a list of mappings")
        return document

    def load_all(self) -> list[dict]:
        records = []
        for path in self.sources():
            records.extend(self._read(path).get("defects") or [])
        return records

    def find(self, defect_id: str) -> dict | None:
        wanted = defect_id.upper()
        return next(
            (record for record in self.load_all() if record.get("id") == wanted),
            None,
        )

    def append(self, options: argparse.Namespace) -> tuple[str, Path]:
        existing = self.load_all()
        try:
            highest = max(
                (int(record["id"][4:]) for record in existing), default=0
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistryError("registry contains a malformed defect id") from exc
        new_id = f"ODR-{highest + 1:04d}"
        if any(record.get("id") == new_id for record in existing):
            raise RegistryError(f"id collision on {new_id}")

        record = {
            "id": new_id,
            "title": options.title,
            "reported": options.reported,
            "introduced_in": None,
            "affected": options.affected,
            "severity": options.severity,
            "component": options.component,
            "status": "open",
            "reproduction": options.reproduction,
            "disposition": options.disposition,
            "fix_commit": None,
            "fixed_in": None,
            "regression_tests": [],
        }

        target = self.root / "defects.yml"
        document = self._read(target) if target.exists() else None
        if not document:
            shard_dir = self.root / "defects"
            shards = sorted(shard_dir.glob("*.yml")) if shard_dir.is_dir() else []
            if shards:
                target = shards[-1]
                document = self._read(target)
            else:
                document = {"schema": 1, "defects": []}
        document.setdefault("defects", []).append(record)
        try:
            target.write_text(
                HEADER
                + yaml.safe_dump(
                    document, sort_keys=False, allow_unicode=True, width=4096
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            raise RegistryError(f"cannot write {target}: {exc}") from exc
        return new_id, target


def _use_color(stream: TextIO) -> bool:
    return (
        hasattr(stream, "isatty")
        and stream.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM") != "dumb"
    )


def _style(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def _terminal_width(stream: TextIO) -> int:
    if hasattr(stream, "isatty") and stream.isatty():
        return max(24, shutil.get_terminal_size(fallback=(100, 24)).columns)
    return 100


def _wrap(
    text: object, width: int, *, initial: str = "", subsequent: str = ""
) -> list[str]:
    value = "-" if text is None or text == "" else str(text)
    lines = textwrap.wrap(
        value,
        width=max(1, width),
        initial_indent=initial,
        subsequent_indent=subsequent,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=True,
    )
    return lines or [initial.rstrip()]


def _colored_cell(value: object, width: int, color: bool) -> str:
    text = str(value or "?")
    return _style(f"{text:<{width}}", COLORS.get(text.lower(), ""), color)


def render_list(records: Sequence[dict], width: int, *, color: bool = False) -> str:
    if not records:
        return "(no matching defect records)"
    if width < 72:
        return _render_compact_list(records, width, color=color)

    id_width = max(len("ID"), max(len(str(record.get("id", "?"))) for record in records))
    status_width = max(
        len("STATUS"), max(len(str(record.get("status", "?"))) for record in records)
    )
    severity_width = max(
        len("SEVERITY"),
        max(len(str(record.get("severity", "?"))) for record in records),
    )
    component_width = min(
        20,
        max(
            len("COMPONENT"),
            max(len(str(record.get("component", "?"))) for record in records),
        ),
    )
    prefix_width = id_width + status_width + severity_width + component_width + 8
    title_width = max(12, width - prefix_width)
    header = (
        f"{'ID':<{id_width}}  {'STATUS':<{status_width}}  "
        f"{'SEVERITY':<{severity_width}}  {'COMPONENT':<{component_width}}  TITLE"
    )
    output = [_style(header, BOLD, color)]
    blank_prefix = " " * prefix_width
    for record in records:
        title_lines = _wrap(record.get("title"), title_width)
        component = str(record.get("component", "?"))
        if len(component) > component_width:
            component = component[: component_width - 1] + "~"
        first = (
            f"{str(record.get('id', '?')):<{id_width}}  "
            f"{_colored_cell(record.get('status'), status_width, color)}  "
            f"{_colored_cell(record.get('severity'), severity_width, color)}  "
            f"{component:<{component_width}}  {title_lines[0]}"
        )
        output.append(first.rstrip())
        output.extend((blank_prefix + line).rstrip() for line in title_lines[1:])
    return "\n".join(output)


def _render_compact_list(
    records: Sequence[dict], width: int, *, color: bool
) -> str:
    output = []
    for index, record in enumerate(records):
        status = str(record.get("status", "?")).upper()
        severity = str(record.get("severity", "?")).upper()
        heading = f"{record.get('id', '?')}  {status} / {severity}"
        output.append(_style(heading, BOLD, color))
        component = str(record.get("component", "?"))
        title = f"[{component}] {record.get('title', '')}"
        output.extend(_wrap(title, width, initial="  ", subsequent="  "))
        if index != len(records) - 1:
            output.append("")
    return "\n".join(output)


def _append_field(output: list[str], label: str, value: object, width: int) -> None:
    label_width = 14
    output.extend(
        _wrap(
            value,
            width,
            initial=f"{label:<{label_width}}",
            subsequent=" " * label_width,
        )
    )


def _append_section(output: list[str], title: str, value: object, width: int) -> None:
    output.extend(("", title))
    if isinstance(value, list):
        if not value:
            output.append("  -")
        for item in value:
            output.extend(_wrap(item, width, initial="  - ", subsequent="    "))
        return
    if value is None or value == "":
        output.append("  -")
        return
    for index, paragraph in enumerate(str(value).split("\n\n")):
        if index:
            output.append("")
        output.extend(
            _wrap(" ".join(paragraph.split()), width, initial="  ", subsequent="  ")
        )


def render_detail(record: dict, width: int, *, color: bool = False) -> str:
    content_width = max(18, width - 2)
    status = str(record.get("status", "?")).upper()
    severity = str(record.get("severity", "?")).upper()
    heading = f"{record.get('id', '?')}  {status} / {severity}"
    output = [_style(heading, BOLD, color)]
    output.extend(_wrap(record.get("title"), width))
    output.append("")
    _append_field(output, "Reported", record.get("reported"), width)
    _append_field(output, "Component", record.get("component"), width)
    _append_field(output, "Introduced in", record.get("introduced_in"), width)
    _append_field(output, "Fixed in", record.get("fixed_in"), width)
    _append_field(output, "Fix commit", record.get("fix_commit"), width)
    _append_section(output, "Affected", record.get("affected"), content_width)
    _append_section(output, "Reproduction", record.get("reproduction"), content_width)
    _append_section(output, "Disposition", record.get("disposition"), content_width)
    _append_section(
        output, "Regression tests", record.get("regression_tests") or [], content_width
    )
    return "\n".join(output)


def _iso_date(value: str) -> str:
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date (YYYY-MM-DD)") from exc
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append to and query the Operon defect registry."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list", help="list defect records")
    list_parser.add_argument("--status", choices=STATUSES)
    list_parser.add_argument("--component")

    show_parser = commands.add_parser("show", help="show one defect record")
    show_parser.add_argument("id", metavar="ODR-XXXX")

    add_parser = commands.add_parser("add", help="append an open defect record")
    add_parser.add_argument("--title", required=True)
    add_parser.add_argument("--severity", required=True, choices=SEVERITIES)
    add_parser.add_argument("--component", required=True)
    add_parser.add_argument(
        "--reported", type=_iso_date, default=datetime.date.today().isoformat()
    )
    add_parser.add_argument("--affected")
    add_parser.add_argument("--reproduction")
    add_parser.add_argument("--disposition")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path = ROOT,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    width: int | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    registry = Registry(root)
    try:
        if args.command == "list":
            records = [
                record
                for record in registry.load_all()
                if (args.status is None or record.get("status") == args.status)
                and (
                    args.component is None
                    or record.get("component") == args.component
                )
            ]
            print(
                render_list(
                    records,
                    width or _terminal_width(stdout),
                    color=_use_color(stdout),
                ),
                file=stdout,
            )
        elif args.command == "show":
            record = registry.find(args.id)
            if record is None:
                raise RegistryError(f"no defect record with id {args.id.upper()}")
            print(
                render_detail(
                    record,
                    width or _terminal_width(stdout),
                    color=_use_color(stdout),
                ),
                file=stdout,
            )
        else:
            new_id, target = registry.append(args)
            print(f"appended {new_id} to {target.relative_to(root)}", file=stdout)
    except RegistryError as exc:
        print(f"error: {exc}", file=stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
