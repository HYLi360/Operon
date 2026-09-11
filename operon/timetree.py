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

    def query_command(name, help_text):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--format", choices=["text", "json"], default="text")
        command.add_argument("--refresh", action="store_true",
                             help="ignore cached responses and query TimeTree again")
        command.add_argument("--timeout", type=float, default=30)
        command.add_argument("--retries", type=int, default=3)
        command.add_argument("--delay", type=float, default=0.5,
                             help="pause between real network requests (seconds)")
        return command

    taxon = query_command("taxon", "resolve a scientific name to TimeTree/NCBI taxonomy candidates")
    taxon.add_argument("--name", required=True)

    def taxon_selection(command, *, multiple):
        command.add_argument("--taxon", action="append", default=[],
                             help="scientific name; repeatable" if multiple else "scientific name")
        command.add_argument("--taxon-id", type=int, action="append", default=[],
                             help="NCBI taxonomy ID; skips name resolution" if multiple
                             else "NCBI taxonomy ID; skips name resolution")

    pairwise_command = query_command("pairwise", "divergence-time summary for two taxa")
    taxon_selection(pairwise_command, multiple=True)
    mrca_command = query_command("mrca", "MRCA divergence-time summary for N taxa")
    taxon_selection(mrca_command, multiple=True)
    mrca_command.add_argument("--taxa", default="",
                              help="comma-separated scientific names (alternative to repeated --taxon)")
    timeline_command = query_command("timeline", "node timetable from a taxon to the last universal ancestor")
    taxon_selection(timeline_command, multiple=True)
    calibrations = query_command(
        "calibrations", "build a calibration prior table (MRCA or per-pair) for MCMCTree")
    taxon_selection(calibrations, multiple=True)
    calibrations.add_argument("--taxa", default="",
                              help="comma-separated scientific names (alternative to repeated --taxon)")
    calibrations.add_argument("--pairs", action="store_true",
                              help="query every pair instead of one whole-set MRCA")
    calibrations.add_argument("--out", help="also write the calibration table to this TSV file")


def run_cli(args):
    if args.timetree_command in _QUERY_COMMANDS:
        return _run_query_cli(args)
    if args.timetree_command == "fetch":
        result = fetch_snapshot(args.pairs, args.output, timeout=args.timeout, retries=args.retries)
    else:
        result = calibrate_tree(args.snapshot, args.tree, args.taxa, args.constraints, args.output, unit_ma=args.unit_ma)
    print(json.dumps(result, indent=2))
    return 0


_QUERY_COMMANDS = ("taxon", "pairwise", "mrca", "timeline", "calibrations")


def _print_citation(stream=None):
    from operon.adapters.timetree import TIMETREE_CITATION
    print(f"cite: {TIMETREE_CITATION}", file=stream or sys.stderr)


def _print_payload(args, headers, rows, payload):
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_table(headers, rows))


def _log_timetree_run(project, db, subcommand, command_text, *, started_at, details):
    from operon.workflow import log_run
    log_run(db, project, {
        "step": f"timetree:{subcommand}",
        "status": "completed",
        "started_at": started_at,
        "finished_at": now_iso(),
        "command": command_text,
        "tool": "operon",
        "execution_details": json.dumps(details, ensure_ascii=False, sort_keys=True, default=str),
    })


def _query_client(args, project):
    from operon.adapters.timetree import TimeTreeClient
    return TimeTreeClient(
        project.root / "adapters_cache" / "timetree",
        timeout=args.timeout, retries=args.retries, delay=args.delay,
        refresh=args.refresh,
    )


def _resolve_cli_taxa(args, client, *, minimum):
    """Resolve every --taxon name to exactly one ID; never auto-pick among ties."""
    requested = list(args.taxon)
    if getattr(args, "taxa", ""):
        requested.extend(part.strip() for part in args.taxa.split(",") if part.strip())
    resolved = [(str(taxon_id), int(taxon_id)) for taxon_id in args.taxon_id]
    for name in requested:
        candidates = client.resolve_taxon(name)
        if len(candidates) > 1:
            listing = "; ".join(
                f"{item['scientific_name']} (id {item['taxon_id']}"
                + (f", {item['rank']}" if item["rank"] else "") + ")"
                for item in candidates
            )
            raise ValidationError(
                f"TimeTree name {name!r} is ambiguous; re-run with --taxon-id: {listing}"
            )
        candidate = candidates[0]
        resolved.append((candidate["scientific_name"], candidate["taxon_id"]))
    unique = []
    seen = set()
    for item in resolved:
        if item[1] not in seen:
            seen.add(item[1])
            unique.append(item)
    if len(unique) < minimum:
        raise ValidationError(f"this command needs at least {minimum} distinct taxa")
    return unique


def _cmd_timetree_taxon(args, project, db, started_at):
    with _query_client(args, project) as client:
        candidates = client.resolve_taxon(args.name)
    _print_payload(
        args,
        ["taxon_id", "scientific_name", "rank"],
        [[item["taxon_id"], item["scientific_name"], item["rank"]] for item in candidates],
        {"name": args.name, "candidates": candidates},
    )
    _print_citation()
    _log_timetree_run(project, db, "taxon", f"operon timetree taxon --name {args.name!r}",
                      started_at=started_at, details={"name": args.name, "candidates": candidates})
    return 0


def _summary_rows(summaries):
    rows = []
    for summary in summaries:
        rows.append([
            summary["label"],
            ",".join(str(value) for value in summary["taxon_ids"]),
            ",".join(summary["scientific_names"]),
            summary["age_median"], summary["ci_low"], summary["ci_high"],
            summary["study_count"],
            "yes" if summary["from_cache"] else "no",
        ])
    return rows


def _cmd_timetree_divergence(args, project, db, started_at, *, subcommand):
    minimum = 2
    with _query_client(args, project) as client:
        taxa = _resolve_cli_taxa(args, client, minimum=minimum)
        if subcommand == "pairwise":
            if len(taxa) != 2:
                raise ValidationError("pairwise needs exactly two distinct taxa")
            summaries = [client.pairwise(taxa[0][1], taxa[1][1])]
        else:
            summaries = [client.mrca([taxon_id for _, taxon_id in taxa])]
    _print_payload(
        args,
        ["node", "taxon_ids", "names", "age_median", "ci_low", "ci_high", "study_count", "cached"],
        _summary_rows(summaries),
        {"taxa": taxa, "summaries": summaries},
    )
    _print_citation()
    names = " ".join(f"--taxon {name!r}" for name, _ in taxa)
    _log_timetree_run(project, db, subcommand, f"operon timetree {subcommand} {names}",
                      started_at=started_at,
                      details={"taxa": taxa, "summaries": summaries})
    return 0


def _cmd_timetree_timeline(args, project, db, started_at):
    with _query_client(args, project) as client:
        taxa = _resolve_cli_taxa(args, client, minimum=1)
        if len(taxa) != 1:
            raise ValidationError("timeline needs exactly one taxon")
        rows = client.timeline(taxa[0][1])
    headers = [key for key in rows[0] if not key.startswith("_")]
    _print_payload(args, headers, [[row.get(key, "") for key in headers] for row in rows],
                   {"taxon": taxa[0], "nodes": rows})
    _print_citation()
    _log_timetree_run(project, db, "timeline",
                      f"operon timetree timeline --taxon {taxa[0][0]!r}",
                      started_at=started_at,
                      details={"taxon": taxa[0], "node_count": len(rows)})
    return 0


def _cmd_timetree_calibrations(args, project, db, started_at):
    from operon.adapters.timetree import CALIBRATION_COLUMNS
    with _query_client(args, project) as client:
        taxa = _resolve_cli_taxa(args, client, minimum=2)
        rows = client.build_calibrations(taxa, pairs=args.pairs)
    if args.out:
        write_tsv(args.out, CALIBRATION_COLUMNS, rows)
    _print_payload(
        args,
        CALIBRATION_COLUMNS,
        [["" if row.get(column) is None else row.get(column) for column in CALIBRATION_COLUMNS]
         for row in rows],
        {"taxa": taxa, "calibrations": rows},
    )
    _print_citation()
    if args.out:
        print(f"wrote {args.out}; adopt it into the project with `operon adopt` if it should be archived",
              file=sys.stderr)
    _log_timetree_run(project, db, "calibrations",
                      f"operon timetree calibrations --taxa "
                      f"{','.join(name for name, _ in taxa)!r}"
                      + (" --pairs" if args.pairs else "")
                      + (f" --out {args.out}" if args.out else ""),
                      started_at=started_at,
                      details={"taxa": taxa, "pairs": args.pairs, "out": args.out,
                               "calibrations": rows})
    return 0


def _run_query_cli(args):
    from operon.config import load_project
    from operon.database import Database
    started_at = now_iso()
    project = load_project(args.project)
    db = Database(project.db_path)
    try:
        handlers = {
            "taxon": lambda: _cmd_timetree_taxon(args, project, db, started_at),
            "pairwise": lambda: _cmd_timetree_divergence(
                args, project, db, started_at, subcommand="pairwise"),
            "mrca": lambda: _cmd_timetree_divergence(
                args, project, db, started_at, subcommand="mrca"),
            "timeline": lambda: _cmd_timetree_timeline(args, project, db, started_at),
            "calibrations": lambda: _cmd_timetree_calibrations(args, project, db, started_at),
        }
        return handlers[args.timetree_command]()
    finally:
        db.close()
