#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Guard: the remote smoke-run checklist names the record each field actually lives in.

A remote run splits its provenance between two records: the ``workflow_runs`` row for the run,
whose ``scheduler_job_id`` and ``executor`` columns carry the batch job id and the ssh target,
and the ``execution_details`` JSON document beside it, which holds the elapsed seconds, the host
and the staged script path (with the per-step argv, version and exit code inside its ``steps``
list). The verification table in the remote-execution guide is what a maintainer follows to
validate a deployment, so a row naming a field in the wrong document sends them looking where it
was never written — which is how the checklist came to promise ``scheduler_job_id`` inside
``execution_details`` (ODR-74).

Everything here is checked by parsing files instead of importing them, in the style of the other
documentation guards, and the last test keeps the guide honest against the schema it describes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUIDES = {
    "en": REPO_ROOT / "docs" / "en" / "guides" / "remote-execution.md",
    "zh": REPO_ROOT / "docs" / "zh" / "guides" / "remote-execution.md",
}
SCHEMA_MODULE = REPO_ROOT / "operon" / "database.py"

#: Header cell of the "check both planes" verification table, per language tree.
CHECKLIST_HEADERS = {"en": "Where", "zh": "位置"}


def _checklist_rows(guide: Path) -> dict[str, str]:
    """Return the verification table's rows keyed by their first cell, backticks stripped."""

    lines = guide.read_text(encoding="utf-8").splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if re.match(r"^\|\s*(Where|位置)\s*\|", line)
    )
    rows: dict[str, str] = {}
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= {"-", " ", ":"}:
            continue
        rows[cells[0].strip("`")] = cells[1]
    return rows


@pytest.mark.parametrize("directory", sorted(GUIDES))
@pytest.mark.bug("ODR-74")
def test_checklist_keeps_the_scheduler_job_id_out_of_execution_details(
    directory: str,
) -> None:
    rows = _checklist_rows(GUIDES[directory])
    assert "execution_details" in rows
    assert "scheduler_job_id" not in rows["execution_details"]


@pytest.mark.parametrize("directory", sorted(GUIDES))
@pytest.mark.bug("ODR-74")
def test_checklist_names_the_workflow_runs_columns_that_hold_the_job_id(
    directory: str,
) -> None:
    rows = _checklist_rows(GUIDES[directory])
    assert "workflow_runs" in rows, (
        "the checklist must say which record carries the scheduler job id"
    )
    assert "scheduler_job_id" in rows["workflow_runs"]
    assert "executor" in rows["workflow_runs"]


@pytest.mark.bug("ODR-74")
def test_workflow_runs_declares_the_columns_the_checklist_points_at() -> None:
    schema = SCHEMA_MODULE.read_text(encoding="utf-8")
    block = re.search(
        r"CREATE TABLE IF NOT EXISTS workflow_runs \((.*?)\n\);", schema, re.DOTALL
    )
    assert block is not None, "workflow_runs DDL not found in operon/database.py"
    columns = block.group(1)
    assert re.search(r"^\s*scheduler_job_id\s", columns, re.MULTILINE)
    assert re.search(r"^\s*executor\s", columns, re.MULTILINE)
