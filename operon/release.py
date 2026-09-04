"""Immutable dataset releases.

A release is a directory with manifest, metadata snapshots, QC summary,
exclusion report, provenance and checksums. Files are copied by default so a
release cannot share writable inodes with raw/ or standardized/.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from operon import __version__
from operon.config import Project
from operon.database import Database
from operon.schema import write_tsv
from operon.utils import atomic_copy, atomic_copytree, now_iso, sha256_file, sha256_path

ACCEPTED = {"PASS", "PASS_WITH_WARNINGS", "ACCEPT_WITH_WARNING"}


def release_files_for(db: Database, profile: str) -> list[dict[str, Any]]:
    rows = db.conn.execute(
        """
        SELECT f.*, COALESCE(d.curated_decision, d.decision) AS effective_decision
        FROM files f
        JOIN current_decisions d ON d.entity_type=f.entity_type AND d.entity_id=f.entity_id
        WHERE d.profile=? AND COALESCE(d.curated_decision, d.decision) IN ('PASS','PASS_WITH_WARNINGS','ACCEPT_WITH_WARNING')
          AND NOT EXISTS (
              SELECT 1 FROM entity_supersessions s
              WHERE s.object_type=f.entity_type AND s.object_id=f.entity_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM effective_retired_entities r
              WHERE r.entity_type=f.entity_type AND r.entity_id=f.entity_id
          )
        ORDER BY f.file_id
        """,
        (profile,),
    ).fetchall()
    return [dict(r) for r in rows]


def release_exclusions_for(db: Database, profile: str) -> list[dict[str, Any]]:
    """Return decision and retirement exclusions for a new release."""
    decisions = [dict(row) for row in db.conn.execute(
        "SELECT entity_type, entity_id, COALESCE(curated_decision, decision) "
        "AS effective_decision, reason_codes, evaluated_at "
        "FROM current_decisions WHERE profile=? ORDER BY entity_type, entity_id",
        (profile,),
    ).fetchall()]
    retirement_rows = db.conn.execute(
        "SELECT entity_type, entity_id, retired_by_type, retired_by_id, "
        "reason_code, reason FROM effective_retired_entities "
        "ORDER BY entity_type, entity_id, event_id"
    ).fetchall()
    retirements: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in retirement_rows:
        retirements.setdefault(
            (str(row["entity_type"]), str(row["entity_id"])), []
        ).append(dict(row))

    excluded: list[dict[str, Any]] = []
    for decision in decisions:
        key = (str(decision["entity_type"]), str(decision["entity_id"]))
        roots = retirements.get(key, [])
        if not roots and decision["effective_decision"] in ACCEPTED:
            continue
        excluded.append({
            **decision,
            "exclusion_reason": "RETIRED" if roots else "DECISION",
            "retired_by": json.dumps(
                [f"{row['retired_by_type']}:{row['retired_by_id']}" for row in roots],
                ensure_ascii=False,
            ),
            "retirement_reason_codes": json.dumps(
                [row["reason_code"] for row in roots], ensure_ascii=False,
            ),
            "retirement_reasons": json.dumps(
                [row["reason"] for row in roots], ensure_ascii=False,
            ),
        })
    return excluded


def create_release(db: Database, project: Project, version: str, profile: str,
                   copy_files: bool | None = None, link_kind: str = "copy") -> dict[str, Any]:
    if copy_files:
        link_kind = "copy"
    if link_kind not in {"copy", "hardlink"}:
        raise ValueError(f"unsupported release link kind {link_kind!r}")
    release_root = project.releases_root / version
    if release_root.exists():
        raise FileExistsError(f"release {version} already exists: {release_root}")
    release_root.mkdir(parents=True, exist_ok=False)

    # Metadata snapshots (small, copied).
    metadata_tables = [
        "organisms", "samples", "runs", "assemblies", "annotations", "accessions",
        "data_sources", "source_links",
    ]
    for table in metadata_tables:
        columns = db.table_columns(table)
        rows = db.export_active_rows(table, columns)
        write_tsv(release_root / f"{table}.tsv", columns, rows)
    metadata_sha256 = {
        f"{table}.tsv": sha256_file(release_root / f"{table}.tsv")
        for table in metadata_tables
    }

    members = release_files_for(db, profile)
    manifest_rows: list[dict[str, Any]] = []
    for member in members:
        source = project.root / member["relative_path"]
        if not source.exists():
            if member.get("status") == "REMOTE_ONLY":
                raise FileNotFoundError(
                    f"release member {member['file_id']} is remote-only; hydrate it with "
                    f"`operon pull --remote NAME --file-id {member['file_id']}` before release"
                )
            raise FileNotFoundError(f"release member missing: {source}")
        if sha256_path(source) != member["sha256"]:
            raise RuntimeError(f"release member checksum mismatch: {source}")
        release_rel = f"data/{member['entity_type']}/{member['entity_id']}/{Path(member['relative_path']).name}"
        target = release_root / release_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if link_kind == "copy":
            if source.is_dir():
                atomic_copytree(source, target)
            else:
                atomic_copy(source, target)
        else:
            if source.is_dir():
                atomic_copytree(source, target)
            else:
                try:
                    os.link(source, target)
                except OSError:
                    atomic_copy(source, target)
        target_sha = sha256_path(target)
        manifest_rows.append({
            "file_id": member["file_id"],
            "entity_type": member["entity_type"],
            "entity_id": member["entity_id"],
            "file_role": member["file_role"],
            "format": member["format"],
            "compression": member["compression"],
            "release_relative_path": release_rel,
            "original_relative_path": member["relative_path"],
            "source_url": member["source_url"],
            "size_bytes": member["size_bytes"],
            "sha256": target_sha,
            "effective_decision": member["effective_decision"],
        })

    manifest_cols = [
        "file_id", "entity_type", "entity_id", "file_role", "format", "compression",
        "release_relative_path", "original_relative_path", "source_url", "size_bytes",
        "sha256", "effective_decision",
    ]
    write_tsv(release_root / "manifest.tsv", manifest_cols, manifest_rows)

    # QC summary and decisions.
    qc_columns = ["entity_type", "entity_id", "file_id", "file_sha256", "input_identity", "qc_stage", "metric_name",
                  "metric_value", "metric_numeric", "metric_unit", "tool", "tool_version", "parameter_set",
                  "evaluated_at"]
    qc_rows = db.conn.execute(
        "SELECT q.* FROM qc_results q WHERE NOT EXISTS ("
        "SELECT 1 FROM effective_retired_entities r "
        "WHERE r.entity_type=q.entity_type AND r.entity_id=q.entity_id) "
        "ORDER BY q.entity_type, q.entity_id, q.qc_stage, q.metric_name"
    ).fetchall()
    write_tsv(release_root / "qc_summary.tsv", qc_columns, [dict(r) for r in qc_rows])
    decision_cols = ["decision_id", "entity_type", "entity_id", "profile", "profile_version", "profile_snapshot_id",
                     "profile_sha256", "decision", "curated_decision", "reason_codes", "evaluated_at", "curated_by",
                     "curated_reason", "curated_evidence", "curated_at"]
    decision_rows = db.conn.execute(
        "SELECT d.* FROM current_decisions d WHERE d.profile=? AND NOT EXISTS ("
        "SELECT 1 FROM effective_retired_entities r "
        "WHERE r.entity_type=d.entity_type AND r.entity_id=d.entity_id) "
        "ORDER BY d.entity_type, d.entity_id",
        (profile,),
    ).fetchall()
    write_tsv(release_root / "decisions.tsv", decision_cols, [dict(r) for r in decision_rows])
    profile_cols = ["profile_snapshot_id", "profile_name", "profile_version", "profile_sha256", "profile_document",
                    "recorded_at"]
    profile_rows = db.conn.execute(
        "SELECT * FROM qc_profiles WHERE profile_name=? ORDER BY profile_snapshot_id", (profile,)
    ).fetchall()
    write_tsv(release_root / "profile_history.tsv", profile_cols, [dict(r) for r in profile_rows])

    # Exclusions preserve both failed decisions and retirement provenance.
    excluded = release_exclusions_for(db, profile)
    exclusion_cols = [
        "entity_type", "entity_id", "effective_decision", "reason_codes",
        "evaluated_at", "exclusion_reason", "retired_by",
        "retirement_reason_codes", "retirement_reasons",
    ]
    write_tsv(release_root / "exclusions.tsv", exclusion_cols, excluded)

    software = [
        {"tool": "operon", "version": __version__, "role": "file database, built-in QC and rule engine"},
        {"tool": "python", "version": _python_version(), "role": "runtime"},
    ]
    write_tsv(release_root / "software_versions.tsv", ["tool", "version", "role"], software)

    provenance = {
        "schema": "operon-2.0",
        "project_id": project.project_id,
        "release_version": version,
        "created_at": now_iso(),
        "profile": profile,
        "file_count": len(manifest_rows),
        "excluded_count": len(excluded),
        "created_by": "operon.release",
        "package_version": __version__,
        "storage_mode": link_kind,
        "metadata_sha256": metadata_sha256,
    }
    (release_root / "provenance.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
                                                  encoding="utf-8")

    checksum_lines = []
    for manifest_row in manifest_rows:
        checksum_lines.append(f"{manifest_row['sha256']}  {manifest_row['release_relative_path']}")
    (release_root / "checksums.sha256").write_text("\n".join(checksum_lines) + ("\n" if checksum_lines else ""),
                                                   encoding="utf-8")

    readme = (
        f"# Operon release {version}\n\n"
        f"- project: {project.project_id}\n"
        f"- QC profile: {profile}\n"
        f"- accepted files: {len(manifest_rows)}\n"
        f"- excluded/review entities: {len(excluded)}\n"
        f"- created by: operon {__version__}\n"
        f"- provenance: provenance.json\n"
        f"- checksums: checksums.sha256\n\n"
        "Raw files are immutable in this system. Verify on Linux with:\n\n"
        "    sha256sum -c checksums.sha256\n\n"
        "Or on macOS with:\n\n"
        "    shasum -a 256 -c checksums.sha256\n"
    )
    (release_root / "README.md").write_text(readme, encoding="utf-8")

    manifest_path = release_root / "manifest.tsv"
    summary = {
        "version": version,
        "profile": profile,
        "created_at": now_iso(),
        "accepted_file_count": len(manifest_rows),
        "excluded_entity_count": len(excluded),
        "manifest_sha256": sha256_file(manifest_path),
        "metadata_sha256": metadata_sha256,
    }
    db.conn.execute(
        "INSERT INTO releases(version, created_at, profile, path, manifest_sha256, summary) VALUES(?,?,?,?,?,?)",
        (version, now_iso(), profile, str(release_root), summary["manifest_sha256"], json.dumps(summary)),
    )
    db.conn.executemany(
        "INSERT OR REPLACE INTO release_members(release_version, file_id, entity_type, entity_id, release_path, sha256, size_bytes) "
        "VALUES(?,?,?,?,?,?,?)",
        [(version, m["file_id"], m["entity_type"], m["entity_id"], m["release_relative_path"], m["sha256"],
          m["size_bytes"]) for m in manifest_rows],
    )
    db.conn.commit()
    for member in members:
        # Release state is applied to accepted members.
        db.set_entity_state(member["entity_type"], member["entity_id"], "RELEASED", f"released in {version}")
    return {**summary, "path": str(release_root)}


def _python_version() -> str:
    import platform
    return platform.python_version()
