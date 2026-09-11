"""Alignment-driven sequence extraction and selection.

Both commands read query intervals stored in ``analysis_alignments`` (only
jobs with ``status='completed'`` count) and materialize FASTA subsets of one
manifest sequence file.  They never mutate metadata beyond a single
``workflow_runs`` provenance record; the outputs re-enter the manifest
through ``operon adopt``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.qc_module._parsers import iter_fasta
from operon.schema import read_tsv
from operon.utils import atomic_write_text, sha256_file
from operon.workflow import log_run

FASTA_LINE_WIDTH = 60

EXTRACT_MANIFEST_COLUMNS = [
    "seqid", "source_file_id", "analysis_name", "subject_id",
    "region_start", "region_end", "extracted_start", "extracted_end",
    "length", "evalue", "excluded_reason",
]

SELECT_MANIFEST_COLUMNS = [
    "seqid", "selected", "matched_analysis", "best_evalue", "best_subject", "hit_count",
]


def _like_match(pattern: str, value: str | None) -> bool:
    """Case-insensitive SQL LIKE semantics (``%`` and ``_`` wildcards)."""
    if value is None:
        return False
    regex = "".join(
        ".*" if char == "%" else "." if char == "_" else re.escape(char)
        for char in pattern
    )
    return re.fullmatch(regex, value, flags=re.IGNORECASE | re.DOTALL) is not None


def _extra_fields(extra_json: str | None) -> dict[str, Any]:
    if not extra_json:
        return {}
    try:
        parsed = json.loads(extra_json)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _source_fasta(db: Database, project: Project, file_id: str) -> tuple[dict[str, Any], Path]:
    row = db.conn.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
    if not row:
        raise ValidationError(f"file {file_id} does not exist in the manifest")
    record = dict(row)
    if record["status"] == "REMOTE_ONLY":
        raise ValidationError(
            f"file {file_id} is REMOTE_ONLY (local bytes were evicted; only remote "
            "mirror copies remain); restore it with 'operon pull' and re-run"
        )
    path = project.root / record["relative_path"]
    if not path.is_file():
        raise ValidationError(
            f"file {file_id} bytes are missing at {record['relative_path']}; "
            "run 'operon verify' to inspect the manifest state"
        )
    db.require_active_entity(record["entity_type"], record["entity_id"])
    return record, path


def _subject_matches(row: dict[str, Any], subject_like: str | None) -> bool:
    if subject_like is None:
        return True
    if _like_match(subject_like, row.get("subject_id")):
        return True
    return _like_match(subject_like, _extra_fields(row.get("extra_json")).get("short_name"))


def _alignment_rows(
        db: Database, file_id: str, *,
        analyses: Iterable[str] = (),
        evalue_max: float | None = None,
        min_span: int | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
) -> list[dict[str, Any]]:
    conditions = ["a.file_id = ?", "j.status = 'completed'"]
    parameters: list[Any] = [file_id]
    analyses = list(analyses)
    if analyses:
        conditions.append(f"a.analysis_name IN ({', '.join('?' for _ in analyses)})")
        parameters.extend(analyses)
    if evalue_max is not None:
        conditions.append("a.evalue IS NOT NULL AND a.evalue <= ?")
        parameters.append(evalue_max)
    if min_span is not None:
        conditions.append(
            "a.query_start IS NOT NULL AND a.query_end IS NOT NULL "
            "AND (a.query_end - a.query_start + 1) >= ?"
        )
        parameters.append(min_span)
    if entity_type is not None:
        conditions.append("a.entity_type = ?")
        parameters.append(entity_type)
    if entity_id is not None:
        conditions.append("a.entity_id = ?")
        parameters.append(entity_id)
    sql = (
        "SELECT a.analysis_name, a.query_id, a.subject_id, a.hit_rank, "
        "a.query_start, a.query_end, a.evalue, a.extra_json "
        "FROM analysis_alignments a "
        "JOIN analysis_jobs j ON j.job_id = a.job_id "
        f"WHERE {' AND '.join(conditions)}"
    )
    return [dict(row) for row in db.conn.execute(sql, parameters).fetchall()]


def _format_fasta(records: list[tuple[str, str]]) -> str:
    if not records:
        return ""
    lines: list[str] = []
    for header, sequence in records:
        lines.append(f">{header}")
        lines.extend(
            sequence[i:i + FASTA_LINE_WIDTH]
            for i in range(0, len(sequence), FASTA_LINE_WIDTH)
        )
    return "\n".join(lines) + "\n"


def _format_manifest(columns: list[str], rows: list[dict[str, Any]]) -> str:
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join("" if row.get(c) is None else str(row.get(c)) for c in columns))
    return "\n".join(lines) + "\n"


def _evalue_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    evalue = row.get("evalue")
    return (
        1 if evalue is None else 0,
        float(evalue) if evalue is not None else 0.0,
        int(row.get("hit_rank") or 0),
        int(row.get("query_start") or 0),
        int(row.get("query_end") or 0),
        str(row.get("subject_id") or ""),
    )


def extract_domains(
        db: Database, project: Project, *,
        file_id: str,
        out: str | Path,
        command: str,
        analysis: str | None = None,
        regions_tsv: str | Path | None = None,
        flank: int = 5,
        min_length: int = 30,
        best_only: bool = True,
        subject_like: str | None = None,
        evalue_max: float | None = None,
        manifest: str | Path | None = None,
) -> dict[str, Any]:
    """Extract flanked query regions from a manifest FASTA as a new FASTA."""
    if (analysis is None) == (regions_tsv is None):
        raise ValidationError("exactly one of --analysis or --regions-tsv is required")
    if regions_tsv is not None and (subject_like is not None or evalue_max is not None):
        raise ValidationError("--subject-like/--evalue-max only apply to --analysis regions")
    record, path = _source_fasta(db, project, file_id)

    candidates: list[dict[str, Any]] = []
    if analysis is not None:
        for row in _alignment_rows(db, file_id, analyses=[analysis], evalue_max=evalue_max):
            if not _subject_matches(row, subject_like):
                continue
            candidates.append({
                "seqid": str(row["query_id"]).split()[0],
                "start": row["query_start"],
                "end": row["query_end"],
                "subject_id": row["subject_id"],
                "evalue": row["evalue"],
                "analysis_name": row["analysis_name"],
                "hit_rank": row["hit_rank"],
            })
    else:
        for line_number, row in enumerate(
                read_tsv(regions_tsv, required_header=["seqid", "start", "end"]), start=2):
            seqid = str(row.get("seqid") or "").strip()
            if not seqid:
                raise ValidationError(f"{regions_tsv}: line {line_number}: empty seqid")
            try:
                start = int(str(row.get("start") or ""))
                end = int(str(row.get("end") or ""))
            except ValueError as exc:
                raise ValidationError(
                    f"{regions_tsv}: line {line_number}: start/end must be integers"
                ) from exc
            if start < 1 or end < start:
                raise ValidationError(
                    f"{regions_tsv}: line {line_number}: require 1 <= start <= end"
                )
            evalue_raw = str(row.get("evalue") or "").strip()
            try:
                evalue = float(evalue_raw) if evalue_raw else None
            except ValueError as exc:
                raise ValidationError(
                    f"{regions_tsv}: line {line_number}: evalue must be numeric"
                ) from exc
            candidates.append({
                "seqid": seqid,
                "start": start,
                "end": end,
                "subject_id": str(row.get("subject") or "").strip() or None,
                "evalue": evalue,
                "analysis_name": None,
                "hit_rank": 0,
            })

    usable: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["start"] is None or candidate["end"] is None:
            candidate["excluded_reason"] = "missing_coordinates"
            excluded.append(candidate)
        elif int(candidate["end"]) - int(candidate["start"]) + 1 < min_length:
            candidate["excluded_reason"] = "below_min_length"
            excluded.append(candidate)
        else:
            usable.append(candidate)

    if best_only:
        chosen: dict[str, dict[str, Any]] = {}
        for candidate in usable:
            current = chosen.get(candidate["seqid"])
            if current is None or _evalue_sort_key(candidate) < _evalue_sort_key(current):
                chosen[candidate["seqid"]] = candidate
        selected = sorted(
            chosen.values(),
            key=lambda c: (c["seqid"], int(c["start"]), int(c["end"]),
                           str(c.get("subject_id") or "")),
        )
    else:
        selected = sorted(
            usable,
            key=lambda c: (c["seqid"], int(c["start"]), int(c["end"]),
                           str(c.get("subject_id") or "")),
        )

    needed = {candidate["seqid"] for candidate in selected}
    sequences: dict[str, str] = {}
    for seqid, sequence in iter_fasta(path):
        if seqid in needed and seqid not in sequences:
            sequences[seqid] = sequence

    records: list[tuple[str, str]] = []
    manifest_rows: list[dict[str, Any]] = []
    for candidate in selected:
        sequence = sequences.get(candidate["seqid"])
        manifest_row = {
            "seqid": candidate["seqid"],
            "source_file_id": file_id,
            "analysis_name": candidate["analysis_name"],
            "subject_id": candidate.get("subject_id"),
            "region_start": candidate["start"],
            "region_end": candidate["end"],
            "extracted_start": None,
            "extracted_end": None,
            "length": None,
            "evalue": candidate.get("evalue"),
            "excluded_reason": None,
        }
        if sequence is None:
            manifest_row["excluded_reason"] = "seqid_not_in_fasta"
        elif int(candidate["start"]) > len(sequence):
            manifest_row["excluded_reason"] = "region_outside_sequence"
        else:
            extracted_start = max(1, int(candidate["start"]) - flank)
            extracted_end = min(len(sequence), int(candidate["end"]) + flank)
            subsequence = sequence[extracted_start - 1:extracted_end]
            manifest_row.update(
                extracted_start=extracted_start,
                extracted_end=extracted_end,
                length=len(subsequence),
            )
            if best_only:
                header = candidate["seqid"]
            else:
                header = f"{candidate['seqid']}|region:{extracted_start}-{extracted_end}"
            records.append((header, subsequence))
        manifest_rows.append(manifest_row)
    for candidate in excluded:
        manifest_rows.append({
            "seqid": candidate["seqid"],
            "source_file_id": file_id,
            "analysis_name": candidate["analysis_name"],
            "subject_id": candidate.get("subject_id"),
            "region_start": candidate["start"],
            "region_end": candidate["end"],
            "extracted_start": None,
            "extracted_end": None,
            "length": None,
            "evalue": candidate.get("evalue"),
            "excluded_reason": candidate["excluded_reason"],
        })
    manifest_rows.sort(key=lambda r: (
        str(r["seqid"]),
        r["region_start"] if r["region_start"] is not None else -1,
        r["region_end"] if r["region_end"] is not None else -1,
        str(r.get("subject_id") or ""),
    ))

    atomic_write_text(out, _format_fasta(records))
    if manifest is not None:
        atomic_write_text(manifest, _format_manifest(EXTRACT_MANIFEST_COLUMNS, manifest_rows))

    extracted_count = sum(1 for row in manifest_rows if not row["excluded_reason"])
    excluded_count = len(manifest_rows) - extracted_count
    log_run(db, project, {
        "entity_type": record["entity_type"],
        "entity_id": record["entity_id"],
        "step": "extract-domains",
        "status": "completed",
        "command": command,
        "tool": "operon",
        "input_sha256": record["sha256"],
        "output_sha256": sha256_file(out),
        "execution_details": json.dumps({
            "file_id": file_id,
            "analysis": analysis,
            "regions_tsv": str(regions_tsv) if regions_tsv is not None else None,
            "flank": flank,
            "min_length": min_length,
            "mode": "best-only" if best_only else "all-regions",
            "subject_like": subject_like,
            "evalue_max": evalue_max,
            "extracted": extracted_count,
            "excluded": excluded_count,
            "output": str(out),
            "manifest": str(manifest) if manifest is not None else None,
        }, ensure_ascii=False, sort_keys=True),
    })
    return {
        "extracted": extracted_count,
        "excluded": excluded_count,
        "output": str(out),
        "manifest": str(manifest) if manifest is not None else None,
    }


def select_sequences(
        db: Database, project: Project, *,
        file_id: str,
        out: str | Path,
        command: str,
        analyses: Iterable[str] = (),
        subject_like: str | None = None,
        evalue_max: float | None = None,
        min_span: int | None = None,
        hit_type: str | None = None,
        require_hit: bool = True,
        entity_type: str | None = None,
        entity_id: str | None = None,
        manifest: str | Path | None = None,
) -> dict[str, Any]:
    """Write the subset of a manifest FASTA with (or without) matching hits."""
    analyses = list(analyses)
    if not analyses and subject_like is None and evalue_max is None \
            and min_span is None and hit_type is None:
        raise ValidationError(
            "no hit criteria given; pass at least one of --analysis, --subject-like, "
            "--evalue-max, --min-span or --hit-type"
        )
    record, path = _source_fasta(db, project, file_id)

    rows = _alignment_rows(
        db, file_id,
        analyses=analyses, evalue_max=evalue_max, min_span=min_span,
        entity_type=entity_type, entity_id=entity_id,
    )
    matched: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not _subject_matches(row, subject_like):
            continue
        if hit_type is not None \
                and _extra_fields(row.get("extra_json")).get("hit_type") != hit_type:
            continue
        seqid = str(row["query_id"]).split()[0]
        entry = matched.setdefault(seqid, {"hits": 0, "best": None, "analyses": set()})
        entry["hits"] += 1
        entry["analyses"].add(row["analysis_name"])
        if entry["best"] is None or _evalue_sort_key(row) < _evalue_sort_key(entry["best"]):
            entry["best"] = row

    universe = [
        str(row["seqid"]) for row in db.conn.execute(
            "SELECT seqid FROM sequences WHERE file_id=? ORDER BY seqid", (file_id,),
        ).fetchall()
    ]
    if not universe:
        universe = sorted(seqid for seqid, _sequence in iter_fasta(path))
    universe_set = set(universe)

    if require_hit:
        selected_ids = sorted(seqid for seqid in matched if seqid in universe_set)
    else:
        selected_ids = sorted(universe_set - set(matched))
    selected_set = set(selected_ids)

    sequences: dict[str, str] = {}
    for seqid, sequence in iter_fasta(path):
        if seqid in selected_set and seqid not in sequences:
            sequences[seqid] = sequence
    records = [
        (seqid, sequences[seqid]) for seqid in selected_ids if seqid in sequences
    ]
    atomic_write_text(out, _format_fasta(records))

    manifest_rows: list[dict[str, Any]] = []
    for seqid in sorted(universe_set):
        entry = matched.get(seqid)
        best = entry["best"] if entry else None
        manifest_rows.append({
            "seqid": seqid,
            "selected": 1 if seqid in selected_set else 0,
            "matched_analysis": ",".join(sorted(entry["analyses"])) if entry else None,
            "best_evalue": best["evalue"] if best else None,
            "best_subject": best["subject_id"] if best else None,
            "hit_count": entry["hits"] if entry else 0,
        })
    if manifest is not None:
        atomic_write_text(manifest, _format_manifest(SELECT_MANIFEST_COLUMNS, manifest_rows))

    stats = {
        "total": len(universe_set),
        "selected": len(selected_set),
        "excluded": len(universe_set) - len(selected_set),
        "matched_not_in_fasta": len(set(matched) - universe_set),
    }
    log_run(db, project, {
        "entity_type": record["entity_type"],
        "entity_id": record["entity_id"],
        "step": "select-sequences",
        "status": "completed",
        "command": command,
        "tool": "operon",
        "input_sha256": record["sha256"],
        "output_sha256": sha256_file(out),
        "execution_details": json.dumps({
            "file_id": file_id,
            "analyses": analyses,
            "subject_like": subject_like,
            "evalue_max": evalue_max,
            "min_span": min_span,
            "hit_type": hit_type,
            "mode": "require-hit" if require_hit else "require-no-hit",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "output": str(out),
            "manifest": str(manifest) if manifest is not None else None,
            **stats,
        }, ensure_ascii=False, sort_keys=True),
    })
    return {
        "total": stats["total"],
        "selected": stats["selected"],
        "excluded": stats["excluded"],
        "output": str(out),
        "manifest": str(manifest) if manifest is not None else None,
    }
