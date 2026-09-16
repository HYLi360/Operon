#!/usr/bin/env bash

# Append to and query the Operon defect registry (defects.yml + defects/*.yml).
#
# Usage:
#   scripts/defects.sh list [--status STATUS] [--component NAME]
#   scripts/defects.sh show ODR-0001
#   scripts/defects.sh add --title "..." --severity low|medium|high|critical \
#       --component NAME [--reported YYYY-MM-DD] [--affected "..."] \
#       [--reproduction "..."] [--disposition "..."]
#
# `add` allocates the next ODR id and appends a status=open record.

set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

exec "$PYTHON" - "$@" <<'PYEOF'
import datetime
import sys
from pathlib import Path

import yaml

ROOT = Path(".")
SEVERITIES = ("low", "medium", "high", "critical")
HEADER = """# Operon defect registry (schema 1).
#
# One record per confirmed defect, appended in ID order. Records are loaded
# from this file plus any `defects/*.yml` shards (sorted by name), so this
# file can be split when it grows. Use `scripts/defects.sh` to append or
# query records; `tests/unit/test_defect_registry.py` validates the schema
# and the regression-test closure in both directions.
# See docs/en/contributor/defect-tracking.md for the process.
"""


def sources():
    files = []
    root_file = ROOT / "defects.yml"
    if root_file.exists():
        files.append(root_file)
    shard_dir = ROOT / "defects"
    if shard_dir.is_dir():
        files.extend(sorted(shard_dir.glob("*.yml")))
    if not files:
        sys.exit("no defect registry found (defects.yml or defects/*.yml)")
    return files


def load_all():
    defects = []
    for path in sources():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        defects.extend(doc.get("defects") or [])
    return defects


def die(message):
    sys.exit(f"error: {message}")


def cmd_list(args):
    status = component = None
    while args:
        if args[0] == "--status":
            status = args[1]; args = args[2:]
        elif args[0] == "--component":
            component = args[1]; args = args[2:]
        else:
            die(f"unknown list option: {args[0]}")
    rows = [d for d in load_all()
            if (status is None or d.get("status") == status)
            and (component is None or d.get("component") == component)]
    if not rows:
        print("(no matching defect records)")
        return
    print(f"{'id':<10} {'status':<10} {'severity':<9} {'component':<16} title")
    for d in rows:
        print(f"{d['id']:<10} {d.get('status', '?'):<10} {d.get('severity', '?'):<9} "
              f"{str(d.get('component', '?')):<16} {d.get('title', '')}")


def cmd_show(args):
    if len(args) != 1:
        die("usage: defects.sh show ODR-XXXX")
    for path in sources():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for d in doc.get("defects") or []:
            if d.get("id") == args[0]:
                print(yaml.safe_dump(d, sort_keys=False, allow_unicode=True,
                                     width=4096).rstrip())
                return
    die(f"no defect record with id {args[0]}")


def cmd_add(args):
    opts = {"reported": datetime.date.today().isoformat(), "affected": None,
            "reproduction": None, "disposition": None}
    while args:
        key = args[0]
        if key not in ("--title", "--severity", "--component", "--reported",
                       "--affected", "--reproduction", "--disposition"):
            die(f"unknown add option: {key}")
        opts[key[2:]] = args[1]
        args = args[2:]
    for required in ("title", "severity", "component"):
        if not opts.get(required):
            die(f"add requires --{required}")
    if opts["severity"] not in SEVERITIES:
        die(f"--severity must be one of {SEVERITIES}")
    datetime.date.fromisoformat(opts["reported"])  # validated, raises on bad input
    existing = load_all()
    highest = max((int(d["id"][4:]) for d in existing), default=0)
    new_id = f"ODR-{highest + 1:04d}"
    if any(d["id"] == new_id for d in existing):
        die(f"id collision on {new_id}")
    record = {
        "id": new_id,
        "title": opts["title"],
        "reported": opts["reported"],
        "introduced_in": None,
        "affected": opts["affected"],
        "severity": opts["severity"],
        "component": opts["component"],
        "status": "open",
        "reproduction": opts["reproduction"],
        "disposition": opts["disposition"],
        "fix_commit": None,
        "fixed_in": None,
        "regression_tests": [],
    }
    target = ROOT / "defects.yml"
    doc = yaml.safe_load(target.read_text(encoding="utf-8")) if target.exists() else None
    if doc is None:
        shards = sorted((ROOT / "defects").glob("*.yml")) if (ROOT / "defects").is_dir() else []
        if not shards:
            doc = {"schema": 1, "defects": []}
            target = ROOT / "defects.yml"
        else:
            target = shards[-1]
            doc = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    doc.setdefault("defects", []).append(record)
    target.write_text(HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True,
                                              width=4096),
                      encoding="utf-8")
    print(f"appended {new_id} to {target}")


def main(argv):
    if not argv:
        die("usage: defects.sh list|show|add ... (see script header)")
    command, args = argv[0], argv[1:]
    if command == "list":
        cmd_list(args)
    elif command == "show":
        cmd_show(args)
    elif command == "add":
        cmd_add(args)
    else:
        die(f"unknown subcommand: {command}")


main(sys.argv[1:])
PYEOF
