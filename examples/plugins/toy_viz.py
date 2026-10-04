"""A standalone checksummed-bundle consumer producing a small SVG state chart."""

import argparse
import csv
import hashlib
import html
import json
from collections import Counter
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--bundle", required=True)
parser.add_argument("--out", required=True)
args = parser.parse_args()
bundle = Path(args.bundle)
manifest = json.loads((bundle / "manifest.json").read_text())
if (
    type(manifest.get("bundle_schema_version")) is not int
    or manifest["bundle_schema_version"] != 1
):
    parser.error("unsupported bundle_schema_version")
for name, member in manifest["members"].items():
    path = (bundle / name).resolve()
    if not path.is_relative_to(bundle.resolve()):
        parser.error("bundle member escapes the bundle directory")
    if hashlib.sha256(path.read_bytes()).hexdigest() != member["sha256"]:
        parser.error(f"checksum mismatch: {name}")
with (bundle / "entities.tsv").open(encoding="utf-8", newline="") as handle:
    states = Counter(
        row["state"] or "UNKNOWN" for row in csv.DictReader(handle, delimiter="\t")
    )
parts = [
    f'<svg xmlns="http://www.w3.org/2000/svg" width="560" height="{60 + 32 * len(states)}">',
    '<rect width="100%" height="100%" fill="white"/>',
    '<text x="16" y="28" font-family="sans-serif">Toy entity states</text>',
]
for index, (state, count) in enumerate(sorted(states.items())):
    y = 58 + 32 * index
    parts.extend(
        [
            f'<text x="16" y="{y}" font-family="sans-serif">{html.escape(state)}</text>',
            f'<rect x="170" y="{y - 16}" width="{count * 32}" height="22" fill="#2563eb"/>',
            f'<text x="{180 + count * 32}" y="{y}" font-family="sans-serif">{count}</text>',
        ]
    )
parts.append("</svg>")
Path(args.out).write_text("\n".join(parts) + "\n", encoding="utf-8")
