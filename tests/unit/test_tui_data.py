#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Read-only data layer edge cases: tool-document guards and filtered run queries.

``operon/tui/data.py`` is the only place the TUI reads the database.  The happy
paths are exercised through the screens; this module pins the guard rails a
caller can hit directly: the two ``get_tool_document`` rejections (an unknown
tool, a tool entry that is not a mapping), and the ``step``/``entity``
substring branch of ``list_workflow_runs`` where the exact-match column filters
(``run_id`` and friends) are applied to the custom query.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from operon.config import Project
from operon.database import Database
from operon.errors import ValidationError
from operon.tui import data


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project.init(tmp_path / "data-project")


def test_get_tool_document_rejects_unknown_tool(project: Project) -> None:
    """A name that is not in tools.yaml names itself and the config path."""
    with pytest.raises(ValidationError, match="unknown tool 'no_such_tool'"):
        data.get_tool_document(project, "no_such_tool")


def test_get_tool_document_rejects_a_non_mapping_entry(project: Project) -> None:
    """A scalar under ``tools:`` cannot be edited as a document."""
    project.tools_config_path.parent.mkdir(parents=True, exist_ok=True)
    project.tools_config_path.write_text(
        "tools:\n  broken: just a string\n", encoding="utf-8"
    )
    with pytest.raises(ValidationError, match="tool 'broken' .* must be a mapping"):
        data.get_tool_document(project, "broken")


def _insert_runs(project: Project) -> None:
    db = Database(project.db_path)
    try:
        db.insert_row(
            "workflow_runs",
            {
                "run_id": "RUN_000001",
                "entity_type": "organism",
                "entity_id": "ORG_000001",
                "step": "import",
                "status": "completed",
                "started_at": "2025-01-01T00:00:00+00:00",
                "tool": "samtools",
                "executor": "local",
            },
        )
        db.insert_row(
            "workflow_runs",
            {
                "run_id": "RUN_000002",
                "entity_type": "sample",
                "entity_id": "SMP_000001",
                "step": "qc",
                "status": "completed",
                "started_at": "2025-01-02T00:00:00+00:00",
                "tool": "fastqc",
                "executor": "local",
            },
        )
    finally:
        db.close()


def test_list_workflow_runs_step_and_entity_filters(project: Project) -> None:
    """``step``/``entity`` switch to the substring query; column filters combine.

    ``step`` alone exercises the loop with every exact-match column unset (the
    filters simply do not enter the SQL); pairing it with ``tool``/``run_id``
    exercises the arms that append ``<column>=?`` clauses.
    """
    _insert_runs(project)

    step_only = data.list_workflow_runs(project, step="import")
    assert [row["run_id"] for row in step_only] == ["RUN_000001"]

    tool_filtered = data.list_workflow_runs(project, step="import", tool="samtools")
    assert [row["run_id"] for row in tool_filtered] == ["RUN_000001"]
    assert data.list_workflow_runs(project, step="import", tool="fastqc") == []

    run_filtered = data.list_workflow_runs(
        project, entity="ORG_000001", run_id="RUN_000001", executor="local"
    )
    assert [row["run_id"] for row in run_filtered] == ["RUN_000001"]

    no_match = data.list_workflow_runs(project, step="import", run_id="RUN_999999")
    assert no_match == []

    # limit=0 means "no row limit", so the LIMIT clause is omitted.
    unlimited = data.list_workflow_runs(project, step="import", limit=0)
    assert [row["run_id"] for row in unlimited] == ["RUN_000001"]
