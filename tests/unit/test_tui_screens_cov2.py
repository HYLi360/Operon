"""Coverage close-out for the TUI guards a passing suite never reaches.

Three families of lines are exercised here, all of them guard arms rather than
happy paths: the sequence-label modal's summary-only and failing-read states,
the shared mounts' defensive branches (``FittingSelect``'s swallowed base
lookups — ODR-38/ODR-26/ODR-50 — and ``MountTracked``'s superseded-generation
drop — ODR-30), the classification rows' fall-through button handlers and
pre-compose source refresh, and the import wizard's refusal to navigate while
a commit is in flight.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual import events
from textual.app import App
from textual.css.query import NoMatches
from textual.widgets import Button, ContentSwitcher, DataTable, Input, Select, Static

from operon.config import Project
from operon.demo import init_demo
from operon.tui.app import OperonApp
from operon.tui.screens import import_wizard as wizard_screen
from operon.tui.screens import labels as labels_module
from operon.tui.screens.common import FittingSelect, MountTracked
from operon.tui.screens.config_classification import (
    BestByRow,
    ClassificationRuleRow,
    ConditionEditor,
    ConditionRow,
    SourceRow,
)
from operon.tui.screens.import_wizard import ImportWizardScreen
from operon.tui.screens.labels import SequenceLabelsModal
from tests import helpers
from tests.tui_helpers import click as _click
from tests.tui_helpers import wait_until

SCENARIO_TIMEOUT = 180.0
SETTLE_TIMEOUT = 30.0
#: Budget for a worker result crossing back from its thread to the UI (ODR-46),
#: matching the other TUI modules.
HANDOFF_TIMEOUT = 120.0


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-screens-cov2-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    """Each test gets its own writable copy of the demo project."""
    return Project.find(
        helpers.copy_project_tree(demo_template.root, tmp_path / "project")
    )


def _run(coroutine) -> None:
    asyncio.run(asyncio.wait_for(coroutine, timeout=SCENARIO_TIMEOUT))


async def _settled(app, timeout: float = SETTLE_TIMEOUT) -> None:
    """Wait until no workers are running, with a diagnostic timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while app.workers or getattr(app, "_starting", False):
        if loop.time() > deadline:
            states = [worker.state.name for worker in app.workers]
            raise TimeoutError(f"workers did not finish within {timeout}s: {states}")
        await asyncio.sleep(0.05)


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


# ---------------------------------------------------------------------------
# labels.py: the summary-only view and its error surface
# ---------------------------------------------------------------------------


def test_sequence_labels_modal_summary_only_view(project: Project) -> None:
    """``file_id=None`` composes no per-file table and reports the summary."""
    dismissed: list = []

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            modal = SequenceLabelsModal(project)
            app.push_screen(modal, dismissed.append)
            await wait_until(
                lambda: bool(modal.query("#labels-table")), "the modal to compose"
            )

            status = modal.query_one("#labels-status", Static)
            await wait_until(
                lambda: "label/profile group(s) in the project" in _static_text(status),
                "the project-wide summary to render",
            )
            assert modal.file_id is None
            assert not modal.query("#labels-file-table")
            assert modal.file_labels == []
            summary_table = modal.query_one("#labels-table", DataTable)
            assert summary_table.row_count == len(modal.summary)

            # The Close button dismisses this read-only view.
            await _click(pilot, "#cancel")
            await wait_until(lambda: bool(dismissed), "the modal to close")

    _run(scenario())


def test_sequence_labels_modal_surfaces_worker_errors(
    project: Project, monkeypatch
) -> None:
    """A failing read is rendered inline instead of killing the worker."""
    calls: list[int] = []

    def broken_summary(*_args: Any, **_kwargs: Any) -> Any:
        calls.append(1)
        raise RuntimeError("labels backend offline")

    monkeypatch.setattr(labels_module.data, "label_summary", broken_summary)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 55)) as pilot:
            await _settled(app)
            modal = SequenceLabelsModal(project)
            app.push_screen(modal)
            await wait_until(
                lambda: bool(modal.query("#labels-table")), "the modal to compose"
            )

            status = modal.query_one("#labels-status", Static)
            await wait_until(
                lambda: "error:" in _static_text(status),
                "the load failure to render",
            )
            assert calls, "the failing read really ran"
            assert "labels backend offline" in _static_text(status)
            assert app.screen is modal, "the failure stays inside the modal"

            await pilot.press("escape")
            await wait_until(lambda: app.screen is not modal, "the modal to close")

    _run(scenario())


# ---------------------------------------------------------------------------
# common.py: the FittingSelect mount guards
# ---------------------------------------------------------------------------


@pytest.mark.bug("ODR-38")
def test_fitting_select_mount_guard_swallows_a_base_lookup_failure() -> None:
    """Textual's own ``_on_mount`` raising NoMatches must not kill the mount."""

    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            select = FittingSelect([("short", "x")], value="x")
            await app.screen.mount(select)
            await pilot.pause()
            await wait_until(lambda: select.options_ready, "the control to mount")
            select._options_ready = False

            def base_raises(self: Select, _event: events.Mount) -> None:
                raise NoMatches("Textual reached for something not composed yet")

            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(Select, "_on_mount", base_raises)
                # The direct, dispatch-free call: the base handler raises and
                # only the guard's except arm is exercised.
                select._on_mount(events.Mount())
            assert select.options_ready, (
                "the failed lookup is absorbed, readiness latches"
            )

    _run(scenario())


def test_fitting_select_init_selected_option_swallows_a_base_lookup_failure() -> None:
    """A failing base ``_init_selected_option`` is swallowed, not raised."""
    select = FittingSelect([("short", "x")])

    def base_raises(self: Select, hint: Any = Select.NULL) -> None:
        raise NoMatches("the overlay is not composed yet")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Select, "_init_selected_option", base_raises)
        select._init_selected_option()
    assert select.value is Select.NULL, "the swallowed failure changed nothing"


@pytest.mark.bug("ODR-50")
def test_fitting_select_adopted_value_survives_a_paint_that_cannot_find_its_label() -> (
    None
):
    """``_watch_value``'s paint may fail while the stored value still stands."""
    select = FittingSelect([("short", "x"), ("longer", "y")], allow_blank=False)
    select._value = Select.NULL
    paints: list[tuple[Any, int]] = []

    def unpaintable(self: FittingSelect, value: Any, attempt: int) -> None:
        paints.append((value, attempt))
        raise NoMatches("the collapsed label has not composed yet")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FittingSelect, "_paint_value_when_composed", unpaintable)
        # Adopting the first option assigns the value, whose paint then fails;
        # Textual stores the value before painting, so the guard's except arm
        # leaves it in place.
        select._adopt_value_before_overlay()
    assert paints == [("x", 0)], "the failed paint really ran"
    assert select.value == "x", "the stored value survives the failed paint"


# ---------------------------------------------------------------------------
# common.py: MountTracked drops a superseded replacement (ODR-30)
# ---------------------------------------------------------------------------


class _GatedRemoval:
    """An awaitable removal that lands only when the test releases it."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.awaited = False
        self.resumed = False
        self._release = asyncio.Event()

    def __await__(self):
        self.awaited = True
        yield from self._wait().__await__()

    async def _wait(self) -> None:
        await self._release.wait()
        self.resumed = True
        await self.inner

    def release(self) -> None:
        self._release.set()


class _GatedMountTracked(MountTracked):
    """A MountTracked whose pending removal the test can park and release."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.gates: list[_GatedRemoval] = []

    def remove_children(self):
        gate = _GatedRemoval(super().remove_children())
        self.gates.append(gate)
        return gate


@pytest.mark.bug("ODR-30")
def test_mount_tracked_replace_children_drops_a_superseded_generation() -> None:
    """A replacement parked on its removal must not mount beside newer rows."""

    async def scenario() -> None:
        app = App()
        async with app.run_test(size=(80, 24)):
            container = _GatedMountTracked(id="gen-container")
            await app.screen.mount(container)
            await container.mount(Static("old", id="row-old"))

            # The replacements are flushed by the screen's own callback turn;
            # the plain sleep pump of ``wait_until`` lets those turns run.
            try:
                container.replace_children(Static("one", id="row-one"))
                await wait_until(
                    lambda: bool(container.gates) and container.gates[0].awaited,
                    "the first replacement to park on its removal",
                )

                # A newer selection supersedes it while it waits.  The second
                # replacement queues behind the parked first one on the
                # screen's callback turn, so it cannot start yet.
                container.replace_children(Static("two", id="row-two"))

                # Releasing the first removal lets the superseded replacement
                # wake up and drop itself instead of mounting beside the rows
                # of the generation that replaced it.
                container.gates[0].release()
                await wait_until(
                    lambda: container.gates[0].resumed,
                    "the parked replacement to wake",
                )
                await wait_until(
                    lambda: len(container.gates) == 2 and container.gates[1].awaited,
                    "the current replacement to take its turn",
                )

                container.gates[1].release()
                await wait_until(
                    lambda: bool(container.query("#row-two")),
                    "the current rows to mount",
                )

                assert not container.query("#row-one"), "the superseded rows never land"
                assert not container.query("#row-old"), "the retired rows are gone"
            finally:
                # Never leave a parked callback behind: a wait that fails
                # above would otherwise keep the screen's callback turn busy
                # and hang the app's shutdown too.
                for gate in container.gates:
                    gate.release()

    _run(scenario())


# ---------------------------------------------------------------------------
# config_classification.py: fall-through handlers and pre-compose refresh
# ---------------------------------------------------------------------------


def test_classification_row_handlers_ignore_foreign_buttons() -> None:
    """A press from a button the row does not own falls through the whole chain."""
    rows = [
        ConditionRow({"field": "assembly_level"}),
        ConditionEditor({"field": "assembly_level"}),
        BestByRow({"field": "hit_rank"}),
        SourceRow("pfam", {"analysis": "pfam"}),
        ClassificationRuleRow({"label": "PFAM"}, ["pfam"]),
    ]
    for row in rows:
        row.on_button_pressed(Button.Pressed(Button("unrelated")))
        assert list(row.children) == [], (
            f"{type(row).__name__} reacted to a foreign press"
        )


def test_classification_rule_row_keeps_sources_until_it_composes() -> None:
    """``set_sources`` before compose stores the list for ``on_mount`` to read."""
    row = ClassificationRuleRow({"label": "PFAM", "source": "pfam"}, ["pfam"])
    row.set_sources(["pfam", "rgi"])
    assert row.source_names == ["pfam", "rgi"]
    assert ("rgi", "rgi") in row._source_options()


# ---------------------------------------------------------------------------
# import_wizard.py: navigation is refused while a commit runs
# ---------------------------------------------------------------------------


@pytest.mark.bug("ODR-54")
def test_import_wizard_refuses_back_while_executing(
    project: Project, tmp_path: Path, monkeypatch
) -> None:
    """A walkthrough commit parks the screen; Back must not navigate from it."""
    fasta = tmp_path / "walk.fasta"
    fasta.write_text(">ctg1\n" + "ACGT" * 250 + "\n", encoding="utf-8")
    released = threading.Event()
    started = threading.Event()
    dismissed: list = []

    def blocking_import(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        started.set()
        released.wait(HANDOFF_TIMEOUT)
        return {
            "entities": {"organism": "ORG_000003"},
            "files": [],
            "source_id": "SRC_1",
        }

    monkeypatch.setattr(wizard_screen.actions, "import_dataset", blocking_import)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)) as pilot:
            await _settled(app)
            wizard = ImportWizardScreen(project)
            app.push_screen(wizard, dismissed.append)
            await wait_until(
                lambda: bool(wizard.reserved_ids), "the wizard startup worker"
            )
            pages = wizard.query_one("#wizard-pages", ContentSwitcher)
            assert pages.current == "page-source"

            wizard.query_one("#iw-database-name", Input).value = "TestDB"
            wizard.query_one("#iw-provider", Input).value = "TestProvider"
            await _click(pilot, "#wizard-next")
            await wait_until(
                lambda: pages.current == "page-organism", "the organism page"
            )

            wizard.query_one("#iw-organism-name", Input).value = "Syntheticus gamma"
            wizard.query_one("#iw-organism-taxon-id", Input).value = "12345"
            await _click(pilot, "#wizard-next")
            await wait_until(lambda: pages.current == "page-sample", "the sample page")

            wizard.query_one("#iw-sample-strain", Input).value = "T1"
            await _click(pilot, "#wizard-next")
            await wait_until(
                lambda: pages.current == "page-sequencing", "the sequencing page"
            )
            # The sequencing section stays skipped: the checkbox is off.
            await _click(pilot, "#wizard-next")
            await wait_until(
                lambda: pages.current == "page-assembly", "the assembly page"
            )

            wizard.query_one("#iw-assembly-name", Input).value = "gamma-asm"
            await _click(pilot, "#wizard-next")
            await wait_until(
                lambda: pages.current == "page-annotation", "the annotation page"
            )
            await _click(pilot, "#wizard-next")
            await wait_until(lambda: pages.current == "page-files", "the files page")

            wizard.query_one("#iw-file-genome-fasta", Input).value = str(fasta)
            await _click(pilot, "#wizard-next")
            await wait_until(
                lambda: pages.current == "page-summary", "the summary page"
            )
            await wait_until(
                lambda: wizard.query_one("#wizard-execute", Button).display is True,
                "the execute button to show",
            )

            await _click(pilot, "#wizard-execute")
            await wait_until(
                lambda: wizard._executing and started.is_set(),
                "the commit to start",
                timeout=HANDOFF_TIMEOUT,
            )

            # Back is enabled but refused while the commit runs: navigating
            # away from the summary now would leave the page being committed.
            assert wizard.query_one("#wizard-back", Button).disabled is False
            await _click(pilot, "#wizard-back")
            await wait_until(
                lambda: pages.current == "page-summary" and wizard._executing,
                "the refused Back press to leave the summary alone",
            )
            assert wizard.query_one("#wizard-execute", Button).display is True

            released.set()
            await wait_until(
                lambda: app.screen is not wizard,
                "the wizard to close after the commit",
                timeout=HANDOFF_TIMEOUT,
            )
            await _settled(app)

    try:
        _run(scenario())
    finally:
        released.set()
    assert dismissed and dismissed[0]["source_id"] == "SRC_1"
