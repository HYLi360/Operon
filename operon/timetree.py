"""Frozen TimeTree evidence and explicitly reviewed secondary calibrations.

These helpers are project-independent. HPC workers consume immutable snapshots;
only the normal ingest/adopt interfaces register their products in a project.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from operon.errors import ValidationError
from operon.schema import read_tsv, write_tsv
from operon.utils import format_table, now_iso, sha256_file

API = "https://timetree.temple.edu/api"
CITATION = "https://doi.org/10.1093/molbev/msac174"


@contextmanager
def new_directory(destination):
    """Publish a complete standalone artifact, never overwrite a prior run."""
    destination = Path(destination)
    if destination.exists():
        raise ValidationError(f"output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        yield temporary
        if destination.exists():
            raise ValidationError(f"output already exists: {destination}")
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def positive(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValidationError(f"{label} must be a positive finite number")
    return number


def pairs_from_tsv(path):
    rows = read_tsv(path, required_header=["taxon_a", "taxon_b"])
    pairs = set()
    for row in rows:
        try:
            a, b = int(row["taxon_a"]), int(row["taxon_b"])
        except ValueError as exc:
            raise ValidationError("TimeTree pairs require NCBI taxonomy integer IDs") from exc
        if min(a, b) <= 0 or a == b:
            raise ValidationError("TimeTree pairs require two different positive NCBI IDs")
        pairs.add(tuple(sorted((a, b))))
    if not pairs:
        raise ValidationError("TimeTree pairs table is empty")
    return sorted(pairs)


def fetch_snapshot(pairs_file, output, *, timeout=30, retries=3, delay=1.0):
    """Save raw summary + study evidence for selected pairs, with bounded retries.

    The public endpoint does not report a release number. Do not infer one from
    the current website or the API publication year.
    """
    import requests

    pairs = pairs_from_tsv(pairs_file)
    positive(timeout, "timeout")
    if not 1 <= retries <= 10 or not math.isfinite(delay) or delay < 0:
        raise ValidationError("retries must be 1..10 and delay must be finite and nonnegative")
    records = []
    with new_directory(output) as work, requests.Session() as session:
        session.headers["User-Agent"] = "Operon-TimeTree-snapshot (research; selected taxon pairs)"
        raw_dir = work / "raw"
        raw_dir.mkdir()
        for a, b in pairs:
            record = {"taxon_a": a, "taxon_b": b, "responses": {}}
            for flag in ("summaryjson", "json"):
                url = f"{API}/pairwise/{a}/{b}/{flag}"
                response = None
                for attempt in range(retries):
                    try:
                        response = session.get(url, timeout=timeout)
                        response.raise_for_status()
                        data = response.json()
                        if not isinstance(data, dict):
                            raise ValueError("expected a JSON object")
                        break
                    except (requests.RequestException, ValueError) as exc:
                        if attempt == retries - 1:
                            raise ValidationError(f"TimeTree request failed: {url}: {exc}") from exc
                        time.sleep(delay * (2 ** attempt))
                if flag == "summaryjson":
                    if (data.get("taxon_a_id"), data.get("taxon_b_id")) != (a, b):
                        raise ValidationError(f"TimeTree returned different/missing taxon IDs for {a}/{b}")
                    positive(data.get("precomputed_age"), f"TimeTree age for {a}/{b}")
                elif not isinstance(data.get("hit_records"), list) or not data["hit_records"]:
                    raise ValidationError(f"TimeTree returned no study evidence for {a}/{b}")
                name = f"raw/{a}_{b}.{flag}.json"
                (work / name).write_bytes(response.content)
                record["responses"][flag] = {
                    "path": name, "sha256": sha256_file(work / name),
                    "url": url, "response_url": response.url, "retrieved_at": now_iso(),
                }
                time.sleep(delay)
            records.append(record)
        manifest = {
            "schema": "operon-timetree-snapshot-1", "source": "TimeTree",
            "source_version": "unreported-by-api", "api_publication": CITATION,
            "age_unit": "Ma", "calibration_type": "secondary",
            "pairs_sha256": sha256_file(pairs_file), "records": records,
        }
        (work / "snapshot.json").write_text(json.dumps(manifest, indent=2) + "\n")
        candidates = []
        for record in records:
            summary = json.loads((work / record["responses"]["summaryjson"]["path"]).read_text())
            candidates.append({
                "taxon_a": record["taxon_a"], "taxon_b": record["taxon_b"],
                "name_a": summary.get("scientific_name_a", ""),
                "name_b": summary.get("scientific_name_b", ""),
                "age_ma": summary["precomputed_age"],
                "reported_ci_low_ma": summary.get("precomputed_ci_low", ""),
                "reported_ci_high_ma": summary.get("precomputed_ci_high", ""),
                "adjusted_age": summary.get("adjusted_age", ""),
                "studies": summary.get("all_total", ""),
                "calibration_type": "secondary", "approved": "no",
            })
        write_tsv(work / "candidates.tsv", list(candidates[0]), candidates)
    return {"output": str(output), "pairs": len(records), "snapshot_sha256": sha256_file(Path(output) / "snapshot.json")}


def load_snapshot(path):
    root = Path(path).resolve()
    document = json.loads((root / "snapshot.json").read_text())
    if document.get("schema") != "operon-timetree-snapshot-1" or document.get("age_unit") != "Ma":
        raise ValidationError("unsupported TimeTree snapshot schema or time unit")
    pairs = set()
    for record in document["records"]:
        for flag in ("summaryjson", "json"):
            item = record["responses"][flag]
            source = (root / item["path"]).resolve()
            if not source.is_relative_to(root) or sha256_file(source) != item["sha256"]:
                raise ValidationError("TimeTree snapshot checksum mismatch or path escape")
        pairs.add(tuple(sorted((record["taxon_a"], record["taxon_b"]))))
    return document, pairs


def calibrate_tree(snapshot, tree_file, taxa_file, constraints_file, output, *, unit_ma=100):
    """Compile reviewed soft bounds, never turn a summary CI into fossil bounds.

    Taxa: leaf,taxon_id. Constraints: taxon_a,taxon_b,members,min_ma,max_ma,
    approved,rationale. members is the exact comma-separated target clade.
    All children of the rooted target tree are retained and branch lengths are
    removed; MCMCTree estimates times on this topology.
    """
    from Bio import Phylo

    unit_ma = positive(unit_ma, "unit_ma")
    _, available = load_snapshot(snapshot)
    tree = Phylo.read(tree_file, "newick")
    leaves = [leaf.name for leaf in tree.get_terminals()]
    if not leaves or len(set(leaves)) != len(leaves) or any(not name for name in leaves):
        raise ValidationError("dating tree must have unique nonempty leaf labels")
    if any(len(node.clades) != 2 for node in tree.get_nonterminals()):
        raise ValidationError("dating tree must be rooted and strictly bifurcating")
    taxa = read_tsv(taxa_file, required_header=["leaf", "taxon_id"])
    mapping = {}
    for row in taxa:
        if row["leaf"] in mapping:
            raise ValidationError("duplicate leaf in taxa table")
        mapping[row["leaf"]] = int(row["taxon_id"])
    if set(mapping) != set(leaves) or len(set(mapping.values())) != len(mapping) or min(mapping.values()) <= 0:
        raise ValidationError("taxa table must map every tree leaf to one unique positive NCBI ID")
    reverse = {value: key for key, value in mapping.items()}
    rows = read_tsv(constraints_file, required_header=[
        "taxon_a", "taxon_b", "members", "min_ma", "max_ma", "approved", "rationale",
    ])
    bounds = {}
    for row in rows:
        pair = tuple(sorted((int(row["taxon_a"]), int(row["taxon_b"]))))
        if pair not in available or any(taxon not in reverse for taxon in pair):
            raise ValidationError("constraint pair is absent from snapshot or target taxa")
        if row["approved"].lower() != "yes" or not row["rationale"].strip():
            raise ValidationError("each constraint requires approved=yes and a scientific rationale")
        node = tree.common_ancestor(reverse[pair[0]], reverse[pair[1]])
        members = {part.strip() for part in row["members"].split(",") if part.strip()}
        if members != {leaf.name for leaf in node.get_terminals()}:
            raise ValidationError("constraint members do not equal the target MRCA clade")
        if node in bounds:
            raise ValidationError("multiple constraints map to the same MRCA; review and consolidate them")
        low, high = positive(row["min_ma"], "min_ma"), positive(row["max_ma"], "max_ma")
        if low >= high:
            raise ValidationError("min_ma must be less than max_ma")
        bounds[node] = (low, high)
    if not bounds:
        raise ValidationError("at least one reviewed calibration is required")
    for ancestor, (_, high) in bounds.items():
        descendants = set(ancestor.find_clades()) - {ancestor}
        if any(low >= high for descendant, (low, _) in bounds.items() if descendant in descendants):
            raise ValidationError("calibration bounds contradict ancestor/descendant time ordering")

    def encode(node):
        if node.is_terminal():
            # PAML sequence/tree labels must be simple and identical.
            import re
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", node.name):
                raise ValidationError(f"unsafe PAML leaf label: {node.name}")
            return node.name
        text = "(" + ",".join(encode(child) for child in node.clades) + ")"
        if node in bounds:
            low, high = bounds[node]
            text += f"'B({low / unit_ma:.12g},{high / unit_ma:.12g})'"
        return text

    newick = encode(tree.root) + ";\n"
    with new_directory(output) as work:
        (work / "calibrated.tree").write_text(f"{len(leaves)} 1\n" + newick)
        shutil.copyfile(constraints_file, work / "constraints.tsv")
        evidence = {
            "calibration_type": "secondary", "unit_ma": unit_ma,
            "snapshot_sha256": sha256_file(Path(snapshot) / "snapshot.json"),
            "tree_sha256": sha256_file(tree_file), "taxa_sha256": sha256_file(taxa_file),
            "constraints_sha256": sha256_file(constraints_file),
            "calibrated_tree_sha256": sha256_file(work / "calibrated.tree"),
        }
        (work / "provenance.json").write_text(json.dumps(evidence, indent=2) + "\n")
    return {"output": str(output), "calibrations": len(bounds), "unit_ma": unit_ma}


def add_parser(sub):
    parser = sub.add_parser("timetree", help="freeze TimeTree evidence and compile reviewed secondary calibrations")
    commands = parser.add_subparsers(dest="timetree_command", required=True)
    fetch = commands.add_parser("fetch", help="download selected NCBI taxon pairs into a new immutable snapshot")
    fetch.add_argument("--pairs", required=True)
    fetch.add_argument("--output", required=True)
    fetch.add_argument("--timeout", type=float, default=30)
    fetch.add_argument("--retries", type=int, default=3)
    compile_command = commands.add_parser("calibrate", help="compile reviewed bounds for a rooted species tree")
    for name in ("snapshot", "tree", "taxa", "constraints", "output"):
        compile_command.add_argument(f"--{name}", required=True)
    compile_command.add_argument("--unit-ma", type=float, default=100)


def run_cli(args):
    if args.timetree_command == "fetch":
        result = fetch_snapshot(args.pairs, args.output, timeout=args.timeout, retries=args.retries)
    else:
        result = calibrate_tree(args.snapshot, args.tree, args.taxa, args.constraints, args.output, unit_ma=args.unit_ma)
    print(json.dumps(result, indent=2))
    return 0
