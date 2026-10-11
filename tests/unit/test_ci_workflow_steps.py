"""Guard: every CI step is named, and its name identifies one step.

A red run is triaged from the step list GitHub reports, so each step has to say
which command it ran.  The failure this guards against was structural: one step
held ``pip install --upgrade pip``, ``pip install -e '.[test]'`` and
``python -m pytest``, and the run reported it as ``Upgrade pip`` — a name that
reads like infrastructure flakiness while the step was in fact reporting failing
tests.  Splitting a step fixes the instance; keeping every step named and
uniquely named keeps the instance from coming back.

The check parses the workflow files instead of importing anything: no CI service
is reachable from the test run, and the files are the whole surface.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))

Step = dict[str, Any]


def _jobs() -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Yield ``(workflow file, job name, job)`` for every job of every workflow."""

    for path in WORKFLOWS:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in workflow["jobs"].items():
            yield path.name, job_name, job


def _steps() -> Iterator[tuple[str, str, int, Step]]:
    """Yield ``(workflow file, job name, step number, step)`` for every step."""

    for file_name, job_name, job in _jobs():
        for number, step in enumerate(job.get("steps", []), start=1):
            yield file_name, job_name, number, step


def test_both_workflows_are_checked() -> None:
    """The guard is worthless if it quietly finds no workflow to check."""

    found = sorted({file_name for file_name, _, _ in _jobs()})
    assert found == ["publish.yml", "test.yml"], f"unexpected workflows: {found}"


def test_every_step_is_named() -> None:
    """An unnamed step is reported by its position, which triages nothing."""

    unnamed = [
        f"{file_name}: {job}#{number}"
        for file_name, job, number, step in _steps()
        if not str(step.get("name", "")).strip()
    ]
    assert unnamed == [], (
        f"every step needs a name: {unnamed} report no command to the run's step list"
    )


def test_step_names_are_unique_inside_their_job() -> None:
    """A reported name has to identify exactly one step of the job."""

    for file_name, job_name, job in _jobs():
        seen: dict[str, int] = {}
        for step in job.get("steps", []):
            name = str(step.get("name", "")).strip()
            seen[name] = seen.get(name, 0) + 1
        duplicated = sorted(name for name, count in seen.items() if count > 1)
        assert duplicated == [], (
            f"{file_name}: {job_name} repeats the step name(s) {duplicated}"
        )


@pytest.mark.parametrize("file_name", ["test.yml", "publish.yml"])
def test_a_step_never_bundles_the_test_suite_with_its_install(file_name: str) -> None:
    """pytest stays in a step of its own, named as the test run."""

    for _, job_name, _, step in _steps():
        run = step.get("run", "")
        if "pytest" not in run:
            continue
        assert "pip install" not in run, (
            f"{file_name}: {job_name} step {step.get('name')!r} installs and "
            "runs pytest in one step; a failure inside it is reported under the "
            "install's name"
        )
        assert "pytest" in str(step.get("name", "")).lower(), (
            f"{file_name}: {job_name} runs pytest in a step named "
            f"{step.get('name')!r}, which does not say it ran the tests"
        )
