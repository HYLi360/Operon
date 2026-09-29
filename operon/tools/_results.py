"""Result parsers and write-back for tool output.

BLAST tabular, HMMER tblout/domtblout, RPS-BLAST/rpsbproc tabular and
BUSCO JSON parsing, plus the summary/hits/alignment synchronization into
SQLite.  These per-software parsers are the software-specific boundary the
plugin plan moves into plugin libraries."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from operon.config import Project
from operon.database import Database
from operon.errors import ExternalToolError, ValidationError
from operon.utils import now_iso

from ._config import Recipe, ToolSpec

_ALIGNMENT_FIELD_KEYS = (
    ("qstart", "qstart_column", ("qstart",), True),
    ("qend", "qend_column", ("qend",), True),
    ("sstart", "sstart_column", ("sstart",), True),
    ("send", "send_column", ("send",), True),
    ("evalue", "evalue_column", ("evalue",), False),
    ("bitscore", "bitscore_column", ("bitscore",), False),
    ("pident", "pident_column", ("pident",), False),
)


_HMMER_PROGRAM_HEADER = re.compile(r"^#\s*(\S+)\s*::")


def parse_and_store_results(
    db: Database,
    project: Project,
    recipe: Recipe,
    tool: ToolSpec,
    tool_version: str,
    file_record: dict[str, Any],
    job_id: int,
    output_path: Path,
    output_sha: str,
    runtime_parameters: dict[str, str] | None = None,
) -> tuple[int, int, int, int, int]:
    """Parse tool output and synchronize summary + top hits + alignments into SQLite."""
    hits, alignments = parse_hits(output_path, recipe)
    metrics: list[dict[str, Any]] = []
    if recipe.result_parser == "busco_json":
        metrics = _parse_busco_json(output_path, recipe)
    elif recipe.result_parser != "none":
        queries = sorted({h["query_id"] for h in hits})
        query_with_hit = sorted({h["query_id"] for h in hits if h.get("rank") == 1})
        hit_pairs = sorted({(h["query_id"], h["subject_id"]) for h in hits})
        metrics = [
            _result_metric("query_count", len(queries)),
            _result_metric("query_with_hit_count", len(query_with_hit)),
            _result_metric("hit_count", len(hit_pairs)),
        ]
        best_evalue = None
        for hit in hits:
            if (
                hit["metric_name"] in {"evalue", "E-value"}
                and hit["metric_numeric"] is not None
            ):
                best_evalue = (
                    hit["metric_numeric"]
                    if best_evalue is None
                    else min(best_evalue, hit["metric_numeric"])
                )
        if best_evalue is not None:
            metrics.append(_result_metric("best_evalue", best_evalue))

    qc_stage = f"analysis:{recipe.name}"
    if runtime_parameters:
        suffix = ",".join(
            f"{name}={runtime_parameters[name]}" for name in sorted(runtime_parameters)
        )
        qc_stage = f"{qc_stage}:{suffix}"

    with db.transaction() as conn:
        conn.execute("DELETE FROM analysis_results WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM analysis_hits WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM analysis_alignments WHERE job_id=?", (job_id,))
    for hit in hits:
        db.conn.execute(
            "INSERT INTO analysis_hits(job_id, entity_type, entity_id, file_id, analysis_name, "
            "query_id, subject_id, metric_name, metric_value, metric_numeric, metric_unit, hit_rank) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                file_record["entity_type"],
                file_record["entity_id"],
                file_record["file_id"],
                recipe.name,
                hit["query_id"],
                hit["subject_id"],
                hit["metric_name"],
                str(hit["metric_value"]),
                hit["metric_numeric"],
                hit.get("metric_unit"),
                hit["rank"],
            ),
        )
    for alignment in alignments:
        db.conn.execute(
            "INSERT INTO analysis_alignments(job_id, entity_type, entity_id, file_id, analysis_name, "
            "query_id, subject_id, hit_rank, query_start, query_end, subject_start, subject_end, "
            "evalue, bitscore, percent_identity, extra_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                file_record["entity_type"],
                file_record["entity_id"],
                file_record["file_id"],
                recipe.name,
                alignment["query_id"],
                alignment["subject_id"],
                alignment["rank"],
                alignment["qstart"],
                alignment["qend"],
                alignment["sstart"],
                alignment["send"],
                alignment["evalue"],
                alignment["bitscore"],
                alignment["pident"],
                json.dumps(alignment["extra"], ensure_ascii=False, sort_keys=True)
                if alignment["extra"]
                else None,
            ),
        )
    db.conn.commit()

    with db.transaction() as conn:
        for metric in metrics:
            conn.execute(
                "INSERT INTO analysis_results(job_id, entity_type, entity_id, file_id, analysis_name, "
                "metric_name, metric_value, metric_numeric, metric_unit) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    file_record["entity_type"],
                    file_record["entity_id"],
                    file_record["file_id"],
                    recipe.name,
                    metric["metric_name"],
                    metric["metric_value"],
                    metric["metric_numeric"],
                    metric.get("metric_unit"),
                ),
            )
            db.insert_qc_result(
                {
                    "entity_type": file_record["entity_type"],
                    "entity_id": file_record["entity_id"],
                    "file_id": file_record["file_id"],
                    "file_sha256": file_record["sha256"],
                    "qc_stage": qc_stage,
                    "metric_name": metric["metric_name"],
                    "metric_value": metric["metric_value"],
                    "metric_numeric": metric["metric_numeric"],
                    "metric_unit": metric.get("metric_unit"),
                    "tool": tool.name,
                    "tool_version": tool_version,
                    "parameter_set": f"{recipe.name}:{output_sha[:16]}",
                    "evaluated_at": now_iso(),
                }
            )
    queries = {h["query_id"] for h in hits}
    query_with_hit = {h["query_id"] for h in hits if h.get("rank") == 1}
    hit_pairs = {(h["query_id"], h["subject_id"]) for h in hits}
    return (
        len(hit_pairs),
        len(queries),
        len(query_with_hit),
        len(metrics),
        len(alignments),
    )


def _result_metric(name: str, value: Any, unit: str | None = None) -> dict[str, Any]:
    numeric: float | None = None
    if not isinstance(value, bool):
        try:
            candidate = float(value)
            if math.isfinite(candidate):
                numeric = candidate
        except (TypeError, ValueError):
            pass
    if value is None:
        rendered = ""
    elif isinstance(value, (dict, list)):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        rendered = str(value)
    return {
        "metric_name": name,
        "metric_value": rendered,
        "metric_numeric": numeric,
        "metric_unit": unit,
    }


def parse_hits(
    output_path: Path, recipe: Recipe
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse tool output into (EAV hit rows, structured alignment rows)."""
    parser = recipe.result_parser
    if parser in {"none", "busco_json"}:
        return [], []
    if parser == "blast_tabular":
        return _parse_blast_tabular(output_path, recipe)
    if parser == "hmmer_tblout":
        return _parse_hmmer_tblout(output_path, recipe), []
    if parser == "hmmer_domtblout":
        return _parse_hmmer_domtblout(output_path, recipe)
    if parser == "rpsbproc_tabular":
        return _parse_rpsbproc_tabular(output_path, recipe)
    raise ExternalToolError(f"unsupported result_parser {parser!r} for {recipe.name}")


def _select_busco_json(output_path: Path, recipe: Recipe) -> Path:
    if output_path.is_file():
        return output_path
    result_glob = str(
        recipe.raw.get("result_glob", "short_summary*.json") or "short_summary*.json"
    )
    if Path(result_glob).is_absolute() or ".." in Path(result_glob).parts:
        raise ValidationError(
            f"{recipe.name}: result_glob must stay within the output directory"
        )
    candidates = sorted(p for p in output_path.glob(result_glob) if p.is_file())
    if not candidates:
        raise ExternalToolError(
            f"{recipe.name}: no BUSCO JSON matched {result_glob!r} under {output_path}"
        )
    if len(candidates) == 1:
        return candidates[0]
    specific = [p for p in candidates if ".specific." in p.name]
    if len(specific) == 1:
        return specific[0]
    names = ", ".join(p.relative_to(output_path).as_posix() for p in candidates)
    raise ExternalToolError(
        f"{recipe.name}: result_glob matched multiple ambiguous BUSCO JSON files: {names}; "
        "narrow result_glob to the final specific summary"
    )


def _first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _parse_busco_json(output_path: Path, recipe: Recipe) -> list[dict[str, Any]]:
    summary_path = _select_busco_json(output_path, recipe)
    try:
        with open(summary_path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ExternalToolError(
            f"{recipe.name}: invalid BUSCO JSON {summary_path}: {exc}"
        ) from exc
    if not isinstance(document, dict) or not isinstance(document.get("results"), dict):
        raise ExternalToolError(
            f"{recipe.name}: BUSCO JSON has no results object: {summary_path}"
        )

    results = document["results"]
    parameters = (
        document.get("parameters")
        if isinstance(document.get("parameters"), dict)
        else {}
    )
    lineage = (
        document.get("lineage_dataset")
        if isinstance(document.get("lineage_dataset"), dict)
        else {}
    )
    versions = (
        document.get("versions") if isinstance(document.get("versions"), dict) else {}
    )
    metrics: list[dict[str, Any]] = []

    result_fields = [
        ("busco_complete_percent", ("Complete percentage",), "percent"),
        ("busco_complete_count", ("Complete BUSCOs",), "count"),
        (
            "busco_single_copy_percent",
            ("Single copy percentage", "Single-copy percentage"),
            "percent",
        ),
        (
            "busco_single_copy_count",
            ("Single copy BUSCOs", "Single-copy BUSCOs"),
            "count",
        ),
        (
            "busco_duplicated_percent",
            ("Multi copy percentage", "Duplicated percentage"),
            "percent",
        ),
        ("busco_duplicated_count", ("Multi copy BUSCOs", "Duplicated BUSCOs"), "count"),
        ("busco_fragmented_percent", ("Fragmented percentage",), "percent"),
        ("busco_fragmented_count", ("Fragmented BUSCOs",), "count"),
        ("busco_missing_percent", ("Missing percentage",), "percent"),
        ("busco_missing_count", ("Missing BUSCOs",), "count"),
        ("busco_n_markers", ("n_markers",), "count"),
        ("busco_domain", ("domain",), None),
        ("busco_one_line_summary", ("one_line_summary",), None),
    ]
    for metric_name, keys, unit in result_fields:
        value = _first_value(results, *keys)
        if value is not None:
            metrics.append(_result_metric(metric_name, value, unit))

    metadata_fields = [
        ("busco_lineage_dataset", lineage, ("name",), None),
        ("busco_dataset_creation_date", lineage, ("creation_date",), None),
        ("busco_dataset_buscos", lineage, ("number_of_buscos",), "count"),
        ("busco_dataset_species", lineage, ("number_of_species",), "count"),
        ("busco_datasets_version", parameters, ("datasets_version",), None),
        ("busco_orthodb_version", parameters, ("orthodb_version",), None),
        ("busco_dataset_version", parameters, ("dataset_version",), None),
        ("busco_ncbi_taxid", parameters, ("ncbi_taxid",), None),
        ("busco_reported_version", versions, ("busco",), None),
    ]
    for metric_name, source, keys, unit in metadata_fields:
        value = _first_value(source, *keys)
        if value is not None:
            metrics.append(_result_metric(metric_name, value, unit))

    metric_names = {m["metric_name"] for m in metrics}
    required = {"busco_complete_percent", "busco_n_markers"}
    missing = sorted(required - metric_names)
    if missing:
        raise ExternalToolError(
            f"{recipe.name}: BUSCO JSON {summary_path} is missing required metrics: {', '.join(missing)}"
        )
    return metrics


def _alignment_number(raw: Any, integer: bool = False) -> int | float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return int(value) if integer else value


def _parse_blast_tabular(
    path: Path, recipe: Recipe
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    columns = [str(c) for c in recipe.raw.get("result_columns", [])]
    if len(columns) < 2:
        raise ValidationError(
            f"{recipe.name}: result_columns must contain at least query and subject"
        )
    metric_columns = [str(c) for c in recipe.raw.get("hit_metric_columns", columns[2:])]
    numeric_columns = {
        str(c) for c in recipe.raw.get("numeric_columns", metric_columns)
    }
    query_index = columns.index(recipe.raw.get("query_column", columns[0]))
    subject_index = columns.index(recipe.raw.get("subject_column", columns[1]))
    metric_indexes = [columns.index(c) for c in metric_columns if c in columns]
    alignment_indexes: dict[str, tuple[int, bool]] = {}
    for field, recipe_key, common_names, integer in _ALIGNMENT_FIELD_KEYS:
        declared = recipe.raw.get(recipe_key)
        if declared is not None and str(declared) in columns:
            alignment_indexes[field] = (columns.index(str(declared)), integer)
            continue
        for name in common_names:
            if name in columns:
                alignment_indexes[field] = (columns.index(name), integer)
                break
    structured = {index for index, _ in alignment_indexes.values()} | {
        query_index,
        subject_index,
    }
    rank: dict[str, int] = {}
    hits: list[dict[str, Any]] = []
    alignments: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n\r")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) != len(columns):
                continue
            query_id = fields[query_index].strip()
            subject_id = fields[subject_index].strip()
            if not query_id or not subject_id:
                continue
            rank[query_id] = rank.get(query_id, 0) + 1
            alignment: dict[str, Any] = {
                "query_id": query_id,
                "subject_id": subject_id,
                "rank": rank[query_id],
                "extra": {
                    columns[i]: fields[i].strip()
                    for i in range(len(columns))
                    if i not in structured
                },
            }
            for field in (
                "qstart",
                "qend",
                "sstart",
                "send",
                "evalue",
                "bitscore",
                "pident",
            ):
                mapped = alignment_indexes.get(field)
                alignment[field] = (
                    _alignment_number(fields[mapped[0]], integer=mapped[1])
                    if mapped is not None
                    else None
                )
            alignments.append(alignment)
            if rank[query_id] > recipe.max_hits_per_query:
                continue
            for metric_index, metric_name in zip(
                metric_indexes, [columns[i] for i in metric_indexes], strict=True
            ):
                raw_value = fields[metric_index].strip()
                if raw_value == "":
                    continue
                try:
                    numeric = float(raw_value)
                except ValueError:
                    numeric = None
                hits.append(
                    {
                        "query_id": query_id,
                        "subject_id": subject_id,
                        "metric_name": metric_name,
                        "metric_value": raw_value,
                        "metric_numeric": numeric
                        if metric_name in numeric_columns
                        else None,
                        "metric_unit": None,
                        "rank": rank[query_id],
                    }
                )
    return hits, alignments


def _hmmer_swap(path: Path, recipe: Recipe) -> bool:
    """Whether to swap query/subject: True normalizes hmmsearch output so
    query_id is the searched sequence and subject_id the HMM profile.

    An explicit recipe ``hmmer_mode`` wins; otherwise the program name in the
    first ``# <program> ::`` header line decides; files without either keep
    the hmmscan mapping.
    """
    mode = recipe.raw.get("hmmer_mode")
    if mode:
        if mode == "hmmsearch":
            return True
        if mode == "hmmscan":
            return False
        raise ValidationError(
            f"{recipe.name}: hmmer_mode must be 'hmmsearch' or 'hmmscan', got {mode!r}"
        )
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n\r")
            if not line.startswith("#"):
                break
            match = _HMMER_PROGRAM_HEADER.match(line)
            if match:
                return match.group(1) == "hmmsearch"
    return False


def _parse_hmmer_tblout(path: Path, recipe: Recipe) -> list[dict[str, Any]]:
    swap = _hmmer_swap(path, recipe)
    rank: dict[str, int] = {}
    hits: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n\r")
            if not line.strip() or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) < 6:
                continue
            target_name = fields[0]
            query_name = fields[2]
            if swap:
                target_name, query_name = query_name, target_name
            if not target_name or not query_name:
                continue
            rank[query_name] = rank.get(query_name, 0) + 1
            if rank[query_name] > recipe.max_hits_per_query:
                continue
            for metric_name, raw_value in (("evalue", fields[4]), ("score", fields[5])):
                try:
                    numeric = float(raw_value)
                except ValueError:
                    numeric = None
                hits.append(
                    {
                        "query_id": query_name,
                        "subject_id": target_name,
                        "metric_name": metric_name,
                        "metric_value": raw_value,
                        "metric_numeric": numeric,
                        "metric_unit": None,
                        "rank": rank[query_name],
                    }
                )
    return hits


def _parse_hmmer_domtblout(
    path: Path, recipe: Recipe
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    swap = _hmmer_swap(path, recipe)
    rank: dict[str, int] = {}
    hits: list[dict[str, Any]] = []
    alignments: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n\r")
            if not line.strip() or line.startswith("#"):
                continue
            # domtblout has 22 fixed whitespace-separated columns; the
            # description column may contain spaces, so cap the split.
            fields = line.split(None, 22)
            if len(fields) < 22:
                continue
            target_name = fields[0]
            query_name = fields[3]
            if swap:
                target_name, query_name = query_name, target_name
            if not target_name or not query_name:
                continue
            rank[query_name] = rank.get(query_name, 0) + 1
            try:
                evalue_numeric: float | None = float(fields[12])
            except ValueError:
                evalue_numeric = None
            try:
                score_numeric: float | None = float(fields[13])
            except ValueError:
                score_numeric = None
            alignments.append(
                {
                    "query_id": query_name,
                    "subject_id": target_name,
                    "rank": rank[query_name],
                    "qstart": _alignment_number(fields[17], integer=True),
                    "qend": _alignment_number(fields[18], integer=True),
                    "sstart": None,
                    "send": None,
                    "evalue": evalue_numeric,
                    "bitscore": score_numeric,
                    "pident": None,
                    "extra": {
                        "hmm_from": fields[15],
                        "hmm_to": fields[16],
                        "env_from": fields[19],
                        "env_to": fields[20],
                    },
                }
            )
            if rank[query_name] > recipe.max_hits_per_query:
                continue
            for metric_name, raw_value, numeric in (
                ("evalue", fields[12], evalue_numeric),
                ("score", fields[13], score_numeric),
            ):
                hits.append(
                    {
                        "query_id": query_name,
                        "subject_id": target_name,
                        "metric_name": metric_name,
                        "metric_value": raw_value,
                        "metric_numeric": numeric,
                        "metric_unit": None,
                        "rank": rank[query_name],
                    }
                )
    return hits, alignments


def _parse_rpsbproc_tabular(
    path: Path, recipe: Recipe
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse rpsbproc tabular output (DATA/SESSION/QUERY/DOMAINS blocks).

    Domain rows are attributed to their enclosing QUERY block; the alignment
    query_id is the QUERY definition line. SITES/MOTIFS blocks and anything
    after ENDDATA are ignored.
    """
    session = ""
    query_id = ""
    definition = ""
    mode = ""
    query_keys: set[tuple[str, str]] = set()
    rank: dict[tuple[str, str], int] = {}
    hits: list[dict[str, Any]] = []
    alignments: list[dict[str, Any]] = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for lineno, raw_line in enumerate(handle, 1):
            line = raw_line.rstrip("\n\r")
            if not line.strip() or line.startswith("#"):
                continue
            if line == "ENDDATA":
                break
            if line == "DATA":
                continue
            if line.startswith("SESSION"):
                parts = line.split("\t")
                session = parts[1].strip() if len(parts) > 1 else ""
                mode = ""
                continue
            if line.startswith("QUERY\t"):
                parts = line.split("\t", 4)
                if len(parts) < 5:
                    raise ExternalToolError(
                        f"{recipe.name}: malformed rpsbproc QUERY record at {path}:{lineno}"
                    )
                query_id = parts[1].strip()
                definition = parts[4]
                key = (session, query_id)
                if key in query_keys:
                    raise ExternalToolError(
                        f"{recipe.name}: duplicate rpsbproc query "
                        f"(session={session!r}, query={query_id!r}) at {path}:{lineno}"
                    )
                query_keys.add(key)
                mode = ""
                continue
            if line == "DOMAINS":
                mode = "domains"
                continue
            if line == "SITES":
                mode = "sites"
                continue
            if line == "MOTIFS":
                mode = "motifs"
                continue
            if line.startswith("END"):
                mode = ""
                continue
            if mode != "domains":
                continue
            fields = line.split("\t")
            if len(fields) < 12:
                raise ExternalToolError(
                    f"{recipe.name}: malformed rpsbproc domain record at {path}:{lineno}"
                )
            if not query_id:
                raise ExternalToolError(
                    f"{recipe.name}: rpsbproc domain record without a preceding QUERY "
                    f"at {path}:{lineno}"
                )
            key = (session, query_id)
            rank[key] = rank.get(key, 0) + 1
            evalue_numeric = _alignment_number(fields[6])
            bitscore_numeric = _alignment_number(fields[7])
            alignments.append(
                {
                    "query_id": definition,
                    "subject_id": fields[8].strip(),
                    "rank": rank[key],
                    "qstart": _alignment_number(fields[4], integer=True),
                    "qend": _alignment_number(fields[5], integer=True),
                    "sstart": None,
                    "send": None,
                    "evalue": evalue_numeric,
                    "bitscore": bitscore_numeric,
                    "pident": None,
                    "extra": {
                        "hit_type": fields[2].strip(),
                        "pssm_id": fields[3].strip(),
                        "short_name": fields[9].strip(),
                        "incomplete": fields[10].strip(),
                        "superfamily_pssm": fields[11].strip(),
                        "session": session,
                        "rps_query_id": fields[1].strip(),
                    },
                }
            )
            if rank[key] > recipe.max_hits_per_query:
                continue
            for metric_name, raw_value, numeric in (
                ("evalue", fields[6].strip(), evalue_numeric),
                ("bitscore", fields[7].strip(), bitscore_numeric),
            ):
                hits.append(
                    {
                        "query_id": definition,
                        "subject_id": fields[8].strip(),
                        "metric_name": metric_name,
                        "metric_value": raw_value,
                        "metric_numeric": numeric,
                        "metric_unit": None,
                        "rank": rank[key],
                    }
                )
    return hits, alignments
