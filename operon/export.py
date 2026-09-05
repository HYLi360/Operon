"""Selective file export with release-style provenance.

An export materializes a filtered subset of manifest files into a standalone
directory with manifest, QC snapshot, checksums and provenance, so downstream
analysis can consume a stable, verifiable file set without copying the whole
project.  Like a release, an export never overwrites an existing directory and
re-verifies every source against the manifest before materializing it.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable

from operon import __version__
from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.schema import write_tsv
from operon.utils import atomic_copy, atomic_copytree, now_iso, sha256_file, sha256_path
from operon.workflow import log_run

LINK_KINDS = {"copy", "hardlink", "symlink"}

MANIFEST_COLUMNS = [
    "file_id", "entity_type", "entity_id", "file_role", "format", "compression",
    "export_relative_path", "original_relative_path", "source_url", "size_bytes",
    "sha256",
]

QC_COLUMNS = [
    "entity_type", "entity_id", "file_id", "file_sha256", "input_identity",
    "qc_stage", "metric_name", "metric_value", "metric_numeric", "metric_unit",
    "tool", "tool_version", "parameter_set", "evaluated_at",
]


def _select_files(
        db: Database,
        *,
        entity_type: str | None,
        entity_ids: Iterable[str],
        file_ids: Iterable[str],
        file_role: str | None,
        fmt: str | None,
        state: str | None,
        decision: str | None,
        profile: str | None,
) -> list[dict[str, Any]]:
    entity_ids = list(entity_ids)
    file_ids = list(file_ids)
    if not any([entity_type, entity_ids, file_ids, file_role, fmt, state, decision]):
        raise ValidationError(
            "export requires at least one selection criterion "
            "(--entity-type/--entity-id/--file-id/--file-role/--format/--state/--decision)"
        )
    if decision and not profile:
        raise ValidationError("--decision requires --profile")

    clauses: list[str] = []
    params: list[Any] = []
    joins = ""
    if entity_type:
        clauses.append("f.entity_type=?")
        params.append(entity_type)
    if entity_ids:
        clauses.append("f.entity_id IN (" + ", ".join("?" for _ in entity_ids) + ")")
        params.extend(entity_ids)
    if file_ids:
        clauses.append("f.file_id IN (" + ", ".join("?" for _ in file_ids) + ")")
        params.extend(file_ids)
    if file_role:
        clauses.append("f.file_role=?")
        params.append(file_role)
    if fmt:
        clauses.append("f.format=?")
        params.append(fmt)
    if state:
        clauses.append(
            "EXISTS (SELECT 1 FROM entity_state es "
            "WHERE es.entity_type=f.entity_type AND es.entity_id=f.entity_id "
            "AND es.state=?)"
        )
        params.append(state.upper())
    if decision:
        joins += (
            " JOIN current_decisions d"
            " ON d.entity_type=f.entity_type AND d.entity_id=f.entity_id"
        )
        clauses.append("d.profile=?")
        params.append(profile)
        clauses.append("COALESCE(d.curated_decision, d.decision)=?")
        params.append(decision.upper())
    clauses.append(
        "NOT EXISTS (SELECT 1 FROM effective_retired_entities r "
        "WHERE r.entity_type=f.entity_type AND r.entity_id=f.entity_id)"
    )
    rows = db.conn.execute(
        f"SELECT f.* FROM files f{joins} WHERE {' AND '.join(clauses)} ORDER BY f.file_id",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def export_files(
        db: Database,
        project: Project,
        *,
        output_dir: str | Path,
        entity_type: str | None = None,
        entity_ids: Iterable[str] = (),
        file_ids: Iterable[str] = (),
        file_role: str | None = None,
        fmt: str | None = None,
        state: str | None = None,
        decision: str | None = None,
        profile: str | None = None,
        link_kind: str = "copy",
        include_qc: bool = True,
) -> dict[str, Any]:
    """Materialize an export in a recoverable workspace before publishing it."""
    requested = Path(output_dir)
    if requested.exists():
        if not requested.is_dir() or any(requested.iterdir()):
            raise FileExistsError(f"export output directory is not empty: {requested}")
        workspace = requested
        temporary = False
    else:
        requested.parent.mkdir(parents=True, exist_ok=True)
        workspace = Path(tempfile.mkdtemp(
            prefix=f".{requested.name}.operon-export-", dir=str(requested.parent),
        ))
        temporary = True
    try:
        summary = _export_files_in_workspace(
            db, project, output_dir=workspace,
            output_label=requested,
            entity_type=entity_type, entity_ids=entity_ids, file_ids=file_ids,
            file_role=file_role, fmt=fmt, state=state, decision=decision,
            profile=profile, link_kind=link_kind, include_qc=include_qc,
        )
        if temporary:
            os.replace(workspace, requested)
        summary["output_dir"] = str(requested)
        return summary
    except BaseException:
        if temporary:
            shutil.rmtree(workspace, ignore_errors=True)
        else:
            for child in list(workspace.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        raise


def _export_files_in_workspace(
        db: Database,
        project: Project,
        *,
        output_dir: str | Path,
        output_label: str | Path | None = None,
        entity_type: str | None = None,
        entity_ids: Iterable[str] = (),
        file_ids: Iterable[str] = (),
        file_role: str | None = None,
        fmt: str | None = None,
        state: str | None = None,
        decision: str | None = None,
        profile: str | None = None,
        link_kind: str = "copy",
        include_qc: bool = True,
) -> dict[str, Any]:
    """Materialize selected manifest files into an already isolated workspace.

    The output directory must not exist or must be empty; exports never
    overwrite.  Every source is re-hashed against the manifest before it is
    copied, hardlinked or symlinked.  Returns a summary dict.
    """
    if link_kind not in LINK_KINDS:
        raise ValidationError(f"unsupported export link kind {link_kind!r}")
    entity_ids = list(entity_ids)
    file_ids = list(file_ids)
    selection = {
        "entity_type": entity_type,
        "entity_ids": entity_ids,
        "file_ids": file_ids,
        "file_role": file_role,
        "format": fmt,
        "state": state,
        "decision": decision,
        "profile": profile,
    }
    members = _select_files(
        db, entity_type=entity_type, entity_ids=entity_ids, file_ids=file_ids,
        file_role=file_role, fmt=fmt, state=state, decision=decision, profile=profile,
    )

    output_root = Path(output_dir)
    output_label = Path(output_label) if output_label is not None else output_root
    if output_root.exists():
        if not output_root.is_dir() or any(output_root.iterdir()):
            raise FileExistsError(f"export output directory is not empty: {output_root}")
    else:
        output_root.mkdir(parents=True, exist_ok=False)

    manifest_rows: list[dict[str, Any]] = []
    for member in members:
        source = project.root / member["relative_path"]
        if not source.exists():
            if member.get("status") == "REMOTE_ONLY":
                raise FileNotFoundError(
                    f"export member {member['file_id']} is remote-only; hydrate it with "
                    f"`operon pull --remote NAME --file-id {member['file_id']}` before export"
                )
            raise FileNotFoundError(f"export member missing: {source}")
        if sha256_path(source) != member["sha256"]:
            raise RuntimeError(f"export member checksum mismatch: {source}")
        export_rel = (
            f"data/{member['entity_type']}/{member['entity_id']}/"
            f"{Path(member['relative_path']).name}"
        )
        target = output_root / export_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if link_kind == "copy":
            if source.is_dir():
                atomic_copytree(source, target)
            else:
                atomic_copy(source, target)
        elif link_kind == "hardlink":
            if source.is_dir():
                atomic_copytree(source, target)
            else:
                try:
                    os.link(source, target)
                except OSError:
                    atomic_copy(source, target)
        else:  # symlink
            os.symlink(source.resolve(), target)
        manifest_rows.append({
            "file_id": member["file_id"],
            "entity_type": member["entity_type"],
            "entity_id": member["entity_id"],
            "file_role": member["file_role"],
            "format": member["format"],
            "compression": member["compression"],
            "export_relative_path": export_rel,
            "original_relative_path": member["relative_path"],
            "source_url": member["source_url"],
            "size_bytes": member["size_bytes"],
            "sha256": sha256_path(target),
        })

    write_tsv(output_root / "manifest.tsv", MANIFEST_COLUMNS, manifest_rows)

    if include_qc:
        pairs = sorted({(row["entity_type"], row["entity_id"]) for row in manifest_rows})
        qc_rows: list[dict[str, Any]] = []
        if pairs:
            clause = " OR ".join("(q.entity_type=? AND q.entity_id=?)" for _ in pairs)
            params = [value for pair in pairs for value in pair]
            qc_rows = [dict(row) for row in db.conn.execute(
                f"SELECT q.* FROM qc_results q WHERE ({clause}) "
                "ORDER BY q.entity_type, q.entity_id, q.qc_stage, q.metric_name",
                params,
            ).fetchall()]
        write_tsv(output_root / "qc.tsv", QC_COLUMNS, qc_rows)

    checksum_lines = [
        f"{row['sha256']}  {row['export_relative_path']}" for row in manifest_rows
    ]
    (output_root / "checksums.sha256").write_text(
        "\n".join(checksum_lines) + ("\n" if checksum_lines else ""), encoding="utf-8",
    )

    created_at = now_iso()
    manifest_sha256 = sha256_file(output_root / "manifest.tsv")
    provenance = {
        "schema": "operon-2.0",
        "project_id": project.project_id,
        "created_at": created_at,
        "selection": selection,
        "file_count": len(manifest_rows),
        "created_by": "operon.export",
        "package_version": __version__,
        "link_kind": link_kind,
        "manifest_sha256": manifest_sha256,
    }
    (output_root / "provenance.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )

    log_run(db, project, {
        "entity_type": entity_type,
        "step": "export",
        "status": "completed",
        "command": f"operon export --output {output_root}",
        "output_sha256": manifest_sha256,
        "execution_details": json.dumps({
            "selection": selection,
            "output_dir": str(output_label),
            "link_kind": link_kind,
            "file_count": len(manifest_rows),
        }, ensure_ascii=False, sort_keys=True),
    })
    return {
        "file_count": len(manifest_rows),
        "output_dir": str(output_label),
        "manifest_sha256": manifest_sha256,
        "link_kind": link_kind,
        "created_at": created_at,
    }
