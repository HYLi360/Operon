"""The shared core behind ``operon run-pipeline`` (CLI and TUI).

A pipeline run takes one source file through the four stages, each of which
keeps its own provenance:

1. ``ingest`` — archive the source into ``raw/`` and register the manifest row;
2. ``standardize`` — stage a verified, independent copy into ``standardized/``;
3. ``qc`` — run every applicable built-in QC stage and write ``qc_results``;
4. ``evaluate`` — apply the QC profile and record the decision.

``plan_pipeline`` answers what a run would do — the resolved profile, the
entity, the steps, and whether evaluation would **reuse an existing curated
decision** — without writing anything, which is what the TUI dialog previews
before its Confirm.  The curated-decision gate itself stays with the caller:
the CLI prompts on a tty (``--yes`` bypasses the prompt, a non-tty raises) and
the TUI substitutes an explicit checkbox.

``run_pipeline`` performs the stages, reports each one through an optional
``progress`` callback (the CLI passes ``print``, so its trace is unchanged),
and stops before evaluation when QC fails — exactly like the CLI, which turns
that into exit code 1.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from operon.database import Database
from operon.errors import ValidationError
from operon.files import ingest_file, standardize_file
from operon.qc import qc_file
from operon.rules import evaluate_entity

#: The stage names a pipeline run reports, in execution order.
PIPELINE_STEPS = ("ingest", "standardize", "qc", "evaluate")


def resolve_profile(project, profile_name: str | None) -> str:
    """``--profile`` when given, the project's default QC profile otherwise."""
    return profile_name or project.config["qc"]["default_profile"]


def reason_list(reason_codes: Any) -> list[str]:
    """Normalize a decision's ``reason_codes`` (a list or a JSON string)."""
    if isinstance(reason_codes, list):
        return [str(item) for item in reason_codes]
    if isinstance(reason_codes, str):
        try:
            value = json.loads(reason_codes)
        except json.JSONDecodeError:
            return [reason_codes]
        return [str(item) for item in value] if isinstance(value, list) else [reason_codes]
    return []


def curated_targets(db: Database, entity_type: str, entity_id: str,
                    profile_name: str) -> list[tuple[str, str]]:
    """Entities whose curated decision a re-evaluation would overwrite."""
    current = db.conn.execute(
        "SELECT curated_decision FROM current_decisions "
        "WHERE entity_type=? AND entity_id=? AND profile=?",
        (entity_type, entity_id, profile_name),
    ).fetchone()
    if current is not None and current["curated_decision"] is not None:
        return [(entity_type, entity_id)]
    return []


def plan_pipeline(
        db: Database,
        project,
        *,
        source: str | Path,
        entity_type: str,
        entity_id: str,
        role: str,
        profile: str | None = None,
        fmt: str | None = None,
        compression: str | None = None,
        source_url: str | None = None,
) -> dict[str, Any]:
    """Validate the pipeline's own preconditions without writing anything.

    Each stage keeps its own deeper validation (``ingest`` checks the source
    for real, QC checks the artifact); this covers what the pipeline itself
    decides: the source/entity/role are present, the entity is actionable, the
    profile resolves to a loadable ``qc`` profile, and whether evaluation would
    overwrite a curated decision — the plan the TUI previews before Confirm.
    """
    from operon.profiles import load_profile

    source_text = str(source).strip()
    if not source_text:
        raise ValidationError("a source path is required (--source)")
    if not str(entity_id).strip():
        raise ValidationError("an entity id is required (--entity-id)")
    if not str(role).strip():
        raise ValidationError("a role is required (--role)")
    db.require_active_entity(entity_type, entity_id)
    profile_name = resolve_profile(project, profile)
    load_profile(project.profiles_dir, profile_name, expected_kind="qc")
    targets = curated_targets(db, entity_type, entity_id, profile_name)
    return {
        "source": source_text,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "role": role,
        "fmt": fmt,
        "compression": compression,
        "source_url": source_url,
        "profile": profile_name,
        "default_profile": project.config["qc"]["default_profile"],
        "curated_targets": targets,
        "steps": list(PIPELINE_STEPS),
        "source_exists": (
            None if source_text.startswith(("sftp://", "remote://"))
            else Path(source_text).exists()
        ),
    }


def run_pipeline(
        db: Database,
        project,
        *,
        source: str | Path,
        entity_type: str,
        entity_id: str,
        role: str,
        profile: str | None = None,
        fmt: str | None = None,
        compression: str | None = None,
        source_url: str | None = None,
        progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the four stages in order; a QC failure stops before evaluation."""
    emit = progress if progress is not None else (lambda _line: None)
    profile_name = resolve_profile(project, profile)

    emit(f"[1/4] ingest {source}")
    import_row = ingest_file(db, project, str(source), entity_type, entity_id, role,
                             fmt=fmt, compression=compression, source_url=source_url)
    file_id = import_row["file_id"]
    emit(f"       -> {file_id} {import_row['sha256'][:16]}...")

    emit(f"[2/4] standardize {file_id}")
    staged = standardize_file(db, project, file_id)
    emit(f"       -> {staged['target']}")

    emit(f"[3/4] QC {file_id}")
    qc_result = qc_file(db, project, file_id)
    decision: dict[str, Any] | None = None
    reasons: list[str] = []
    if qc_result["ok"]:
        emit("       -> metrics written to qc_results")
        emit(f"[4/4] evaluate with profile {profile_name}")
        decision = evaluate_entity(db, project, entity_type, entity_id, profile)
        reasons = reason_list(decision.get("reason_codes"))
        emit(f"       -> {decision['decision']}: {', '.join(reasons) or 'no issues'}")

    return {
        "source": str(source),
        "entity_type": entity_type,
        "entity_id": entity_id,
        "role": role,
        "profile": profile_name,
        "file_id": file_id,
        "sha256": import_row["sha256"],
        "target": staged["target"],
        "qc_ok": bool(qc_result["ok"]),
        "qc_error": qc_result.get("error"),
        "decision": decision["decision"] if decision else None,
        "reason_codes": reasons,
        "steps": list(PIPELINE_STEPS),
    }
