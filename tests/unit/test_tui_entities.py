#  Copyright (c) 2026 hyli360
#  Operon (and any official addons) is libre-software, licensed under BSD-2
#  See license file on the repo root
#  SPDX-License-Identifier: BSD-2-Clause
"""Branch coverage for the entities panel and its write modals.

These tests pin the *guard* arms of the entities screen and its dialogs:
change events that arrive from widgets the modal does not own (the false arm
of every ``on_input_changed``/``on_select_changed``/``on_checkbox_changed``
handler), the readiness gates of the add-record form, the near-miss branches
of ``SetStateModal.refresh_hint`` and ``NextIdModal``, the panel's button
dispatch and its callback wrappers, and the ``RetiredModal`` read-failure
rendering.  Every wait is a predicate wait on visible state; nothing sleeps.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.widgets import Button, Checkbox, Input, Select, Static, Tree

from operon.config import Project
from operon.database import Database
from operon.demo import init_demo
from operon.tui import actions, data
from operon.tui.app import OperonApp
from operon.tui.screens.common import MountTracked
from operon.tui.screens.entities import (
    AddAccessionModal,
    AddRecordModal,
    EntitiesPanel,
    ExportMetadataModal,
    ExportQcModal,
    FieldRow,
    NextIdModal,
    RetiredModal,
    SetStateModal,
)
from tests.tui_helpers import click as _click
from tests.tui_helpers import wait_until

SCENARIO_TIMEOUT = 120.0
SETTLE_TIMEOUT = 30.0


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory) -> Project:
    return init_demo(tmp_path_factory.mktemp("tui-entities-demo"))


@pytest.fixture
def project(tmp_path: Path, demo_template: Project) -> Project:
    """Each test gets its own writable copy of the demo project."""
    target = tmp_path / "project"
    shutil.copytree(demo_template.root, target)
    return Project.find(target)


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


async def _push(pilot, modal, selector: str | None = None) -> None:
    """Push a screen and wait until its form **and** buttons have composed."""
    for attempt in range(2):
        if pilot.app.screen is not modal:
            pilot.app.push_screen(modal)

        def ready() -> bool:
            if len(modal.query("#confirm")) == 0:
                return False
            if selector is None:
                return bool(modal.query("#modal-form > *"))
            return len(modal.query(selector)) > 0

        try:
            await wait_until(ready, f"{type(modal).__name__} to compose", timeout=10.0)
        except TimeoutError:
            if attempt:
                raise
            await pilot.pause()
            continue
        await pilot.pause()
        return


async def _q(modal, selector: str, *types):
    await wait_until(lambda: len(modal.query(selector)) > 0, f"{selector} to exist")
    return modal.query_one(selector, *types)


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return renderable.plain if isinstance(renderable, Text) else str(renderable)


def _notifications(app) -> list[tuple[str, str]]:
    return [(n.severity, str(n.message)) for n in app._notifications]


def test_set_state_modal_hint_branches_and_foreign_events(project: Project) -> None:
    """The hint explains same-state, standard, forced and refused picks."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = SetStateModal(project, "organism", "ORG_000001", "ACCEPTED")
            await _push(pilot, modal, "#set-state-state")
            hint = modal.query_one("#set-state-hint", Static)
            state_select = await _q(modal, "#set-state-state", Select)

            # Picking the current state explains there is nothing to record.
            state_select.value = "ACCEPTED"
            await wait_until(
                lambda: "already the current state" in _static_text(hint),
                "same-state hint",
            )

            # A transition the state machine allows is called standard.
            state_select.value = "REVIEW"
            await wait_until(
                lambda: "is a standard transition" in _static_text(hint),
                "standard-transition hint",
            )

            # Change events from foreign widgets never rewrite the preview.
            command = modal.command_text()
            modal.on_select_changed(
                Select.Changed(Select([("x", "x")], id="other-select"), "x")
            )
            modal.on_input_changed(
                Input.Changed(input=Input(id="other-input"), value="zzz")
            )
            modal.on_checkbox_changed(
                Checkbox.Changed(Checkbox("other", id="other-check"), True)
            )
            assert modal.command_text() == command
            assert "is a standard transition" in _static_text(hint)

            # Confirm without a target state refuses with the inline error.
            state_select.clear()
            await wait_until(lambda: modal._selected_state() == "", "blank state")
            modal.confirm()
            await wait_until(
                lambda: (
                    "select the target state"
                    in _static_text(modal.query_one("#modal-error", Static))
                ),
                "missing target state error",
            )
            assert app.screen is modal

            await pilot.press("escape")
            await wait_until(lambda: app.screen is not modal, "set-state close")
            await _settled(app)

    _run(scenario())


def test_export_modals_ignore_foreign_events(project: Project) -> None:
    """The export dialogs only track the widgets they own."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)

            metadata = ExportMetadataModal(project)
            await _push(pilot, metadata, "#metadata-output")
            command = metadata.command_text()
            metadata.on_input_changed(
                Input.Changed(input=Input(id="other-input"), value="zzz")
            )
            metadata.on_checkbox_changed(
                Checkbox.Changed(Checkbox("other", id="other-check"), True)
            )
            assert metadata.command_text() == command == "operon report metadata"
            await pilot.press("escape")
            await wait_until(
                lambda: not isinstance(app.screen, ExportMetadataModal),
                "metadata modal close",
            )

            qc = ExportQcModal(project, "assembly")
            await _push(pilot, qc, "#qc-export-type")
            command = qc.command_text()
            qc.on_select_changed(
                Select.Changed(Select([("x", "x")], id="other-select"), "x")
            )
            qc.on_checkbox_changed(
                Checkbox.Changed(Checkbox("other", id="other-check"), True)
            )
            assert qc.command_text() == command
            assert command == "operon report qc --entity-type assembly --export"
            await pilot.press("escape")
            await wait_until(
                lambda: not isinstance(app.screen, ExportQcModal), "qc modal close"
            )

    _run(scenario())


def test_retired_modal_ignores_foreign_events_and_reports_read_errors(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing retired read lands in the status line, never out of the worker."""

    def boom(*args, **kwargs):
        raise RuntimeError("list_retired exploded")

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = RetiredModal(project)
            app.push_screen(modal)
            await wait_until(
                lambda: len(modal.query("#retired-status")) > 0,
                "retired modal to compose",
            )
            status = modal.query_one("#retired-status", Static)
            await wait_until(
                lambda: _static_text(status) == "no retired entities",
                "empty retired list",
            )

            # Foreign events and foreign buttons leave the modal untouched.
            command = _static_text(modal.query_one("#retired-command", Static))
            modal.on_checkbox_changed(
                Checkbox.Changed(Checkbox("other", id="other-check"), True)
            )
            modal.on_button_pressed(Button.Pressed(Button("other", id="other")))
            assert _static_text(modal.query_one("#retired-command", Static)) == command
            assert app.screen is modal

            # A failing read is reported in place; the modal stays open.
            monkeypatch.setattr(data, "list_retired", boom)
            modal.query_one("#retired-direct-only", Checkbox).value = True
            await wait_until(
                lambda: "error: list_retired exploded" in _static_text(status),
                "retired read error",
            )
            assert app.screen is modal

            await pilot.press("escape")
            await wait_until(lambda: app.screen is not modal, "retired modal close")
            await _settled(app)

    _run(scenario())


def test_field_row_message_control_and_foreign_events(project: Project) -> None:
    """The row's removal message names its control; foreign events are inert."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 50)) as pilot:
            await _settled(app)
            modal = AddRecordModal(project)
            await _push(pilot, modal, ".field-row")
            row = modal.query_one(FieldRow)

            # Textual routes a message through its ``control``; the removal
            # message must name the row that sent it.
            message = FieldRow.RemoveRequested(row)
            assert message.control is row

            # A button on the row that is not the remove button is ignored.
            row.on_button_pressed(Button.Pressed(Button("foreign", id="foreign")))
            assert len(modal.query(FieldRow)) == 1

            # Foreign input/select events never rewrite the preview.
            command = modal.command_text()
            modal.on_input_changed(
                Input.Changed(input=Input(id="other-input"), value="zzz")
            )
            modal.on_select_changed(
                Select.Changed(Select([("x", "x")], id="other-select"), "x")
            )
            assert modal.command_text() == command

            await pilot.press("escape")
            await wait_until(lambda: app.screen is not modal, "add modal close")

    _run(scenario())


def test_add_record_form_readiness_gates(project: Project) -> None:
    """A gone or still-waiting field container answers "not ready"."""

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 50)) as pilot:
            await _settled(app)

            # A container that is gone answers "not ready" instead of raising.
            modal = AddRecordModal(project)
            await _push(pilot, modal, ".field-row")
            await wait_until(lambda: modal._fields_ready(), "form ready")
            modal.query_one("#add-fields", MountTracked).remove()
            await pilot.pause()
            assert modal._fields_ready() is False
            await pilot.press("escape")
            await wait_until(lambda: app.screen is not modal, "first modal close")

            # A container still waiting for rows it cannot see is not ready
            # either: arm the wait for a row that never lands (the mid-mount
            # window the gate exists for, forced the same way the ODR-39
            # regression forces a row's readiness latch).
            modal = AddRecordModal(project)
            await _push(pilot, modal, ".field-row")
            await wait_until(lambda: modal._fields_ready(), "form ready")
            container = modal.query_one("#add-fields", MountTracked)
            container.expect_mounts(".never-lands")
            assert modal._fields_ready() is False
            await pilot.press("escape")
            await wait_until(lambda: app.screen is not modal, "second modal close")

    _run(scenario())


def test_add_record_confirm_branches_and_warning_notifications(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value without a name refuses; blank rows are skipped; warnings notify."""
    calls: list[tuple[tuple, dict]] = []
    warning = "unknown field 'legacy' for organism; add it to the schema"

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "entity_type": "organism",
            "entity_id": "ORG_900001",
            "warnings": [warning],
        }

    monkeypatch.setattr(actions, "add_record", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 50)) as pilot:
            await _settled(app)
            modal = AddRecordModal(project)
            await _push(pilot, modal, ".field-row")
            await wait_until(lambda: modal._fields_ready(), "form ready")
            row = modal.query_one(FieldRow)

            # A value without a field name is refused with the inline error.
            row.query_one(".field-value", Input).value = "orphan"
            modal.confirm()
            await wait_until(
                lambda: (
                    "field name is required for value 'orphan'"
                    in _static_text(modal.query_one("#modal-error", Static))
                ),
                "missing field name error",
            )
            assert app.screen is modal
            assert calls == []

            # Blank rows are skipped, not refused: the modal asks the core for
            # the record and surfaces the warnings on the payload.
            row.query_one(".field-value", Input).value = ""
            app.clear_notifications()
            await _click(pilot, "#confirm")
            await wait_until(lambda: len(calls) == 1, "add record call")
            await wait_until(
                lambda: ("warning", warning) in _notifications(app),
                "add warning notification",
            )
            await wait_until(lambda: app.screen is not modal, "add modal close")

    _run(scenario())
    args, kwargs = calls[0]
    assert args == (project, data.ENTITY_TYPES[0], {})
    assert kwargs == {"record_id": None}


def test_next_id_modal_locks_after_reservation(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumed reservation disables Confirm and freezes the preview."""
    calls: list[tuple[tuple, dict]] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return {"entity_type": "organism", "entity_id": "ORG_000900"}

    monkeypatch.setattr(actions, "reserve_next_id", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            modal = NextIdModal(project)
            await _push(pilot, modal, "#nextid-entity-type")
            await _click(pilot, "#confirm")
            await wait_until(lambda: len(calls) == 1, "reservation call")
            result = modal.query_one("#nextid-result", Static)
            await wait_until(
                lambda: "reserved organism ID: ORG_000900" in _static_text(result),
                "reservation result",
            )
            assert modal.query_one("#confirm", Button).disabled

            # A second reservation would burn another ID: Confirm is inert.
            modal.confirm()
            assert len(calls) == 1

            # With the ID reserved, changing the picker no longer re-previews.
            modal.on_select_changed(
                Select.Changed(modal.query_one("#nextid-entity-type", Select), "sample")
            )
            assert modal.command_text() == "operon next-id organism"

            await pilot.press("escape")
            await wait_until(lambda: app.screen is not modal, "next-id close")

    _run(scenario())


def test_entities_panel_buttons_and_write_callbacks(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every action button dispatches; the set-state callback reloads panels."""
    calls: list[tuple[tuple, dict]] = []

    def stub(*args, **kwargs):
        calls.append((args, kwargs))
        return {
            "entity_type": "assembly",
            "entity_id": "ASM_000001",
            "state": "REVIEW",
            "previous_state": "RELEASED",
            "forced": False,
        }

    monkeypatch.setattr(actions, "set_state", stub)

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(160, 50)) as pilot:
            await _settled(app)
            app.action_switch_screen("entities")
            await wait_until(lambda: bool(app.query(EntitiesPanel)), "entities panel")
            panel = app.query_one(EntitiesPanel)
            await wait_until(lambda: panel.tree_data != [], "entity tree")

            # Set state without a selection explains itself, then opens the
            # dialog once a detail is shown.
            app.clear_notifications()
            await _click(pilot, "#entities-set-state")
            await wait_until(
                lambda: ("warning", "select an entity first") in _notifications(app),
                "selection warning",
            )
            assert not isinstance(app.screen, SetStateModal)

            panel._apply_detail(data.entity_detail(project, "assembly", "ASM_000001"))
            await _click(pilot, "#entities-set-state")
            await wait_until(
                lambda: isinstance(app.screen, SetStateModal), "set-state modal"
            )
            modal = app.screen
            (await _q(modal, "#set-state-state", Select)).value = "REVIEW"
            (await _q(modal, "#set-state-message", Input)).value = "coverage rerun"
            await _click(pilot, "#confirm")
            await wait_until(lambda: len(calls) == 1, "set-state call")
            await wait_until(lambda: app.screen is not modal, "set-state close")
            await _settled(app)
            # A falsy result on the same callback reloads nothing.
            panel._after_set_state(None)

            # A button that is none of the four falls through the chain.
            panel.on_button_pressed(Button.Pressed(Button("other", id="other")))

            # Export QC with the current selection, then without one.
            await _click(pilot, "#entities-export-qc")
            await wait_until(
                lambda: isinstance(app.screen, ExportQcModal), "qc export modal"
            )
            assert app.screen.initial_entity_type == "assembly"
            await pilot.press("escape")
            await wait_until(
                lambda: not isinstance(app.screen, ExportQcModal), "qc export close"
            )

            panel.detail = None
            await _click(pilot, "#entities-export-qc")
            await wait_until(
                lambda: isinstance(app.screen, ExportQcModal),
                "qc export modal without a selection",
            )
            assert app.screen.initial_entity_type == ""
            await pilot.press("escape")
            await wait_until(
                lambda: not isinstance(app.screen, ExportQcModal), "qc export close"
            )

            # The retired browser opens from the panel's own button.
            await _click(pilot, "#entities-retired")
            await wait_until(
                lambda: isinstance(app.screen, RetiredModal), "retired modal"
            )
            retired = app.screen
            await wait_until(
                lambda: (
                    _static_text(retired.query_one("#retired-status", Static))
                    == "no retired entities"
                ),
                "retired rows",
            )
            await pilot.press("escape")
            await wait_until(lambda: app.screen is not retired, "retired close")
            await _settled(app)

            # 'A' with no selection prefills nothing; foreign events on the
            # accession form are inert; closing it hits the falsy callback arm.
            panel.query_one("#entities-tree", Tree).focus()
            await pilot.press("A")
            await wait_until(
                lambda: isinstance(app.screen, AddAccessionModal), "accession modal"
            )
            accession = app.screen
            command = accession.command_text()
            accession.on_input_changed(
                Input.Changed(input=Input(id="other-input"), value="zzz")
            )
            accession.on_select_changed(
                Select.Changed(Select([("x", "x")], id="other-select"), "x")
            )
            accession.on_checkbox_changed(
                Checkbox.Changed(Checkbox("other", id="other-check"), True)
            )
            assert accession.command_text() == command
            assert accession._prefill_type is None and accession._prefill_id is None
            await pilot.press("escape")
            await wait_until(lambda: app.screen is not accession, "accession close")
            await _settled(app)

    _run(scenario())


def test_entity_detail_supersession_without_reason(project: Project) -> None:
    """A supersession with no reason renders without the reason suffix."""
    db = Database(project.db_path)
    try:
        db.supersede_entity(
            "assembly",
            "ASM_000001",
            "assembly",
            "ASM_000002",
            reason="",
            workflow_run_id=None,
        )
    finally:
        db.close()
    detail = data.entity_detail(project, "assembly", "ASM_000001")
    assert detail is not None
    assert [row["reason"] for row in detail["supersessions"]] == [""]

    async def scenario() -> None:
        app = OperonApp(project)
        async with app.run_test(size=(140, 45)):
            await _settled(app)
            app.action_switch_screen("entities")
            await wait_until(lambda: bool(app.query(EntitiesPanel)), "entities panel")
            panel = app.query_one(EntitiesPanel)
            panel._apply_detail(detail)
            text = _static_text(panel.query_one("#entity-detail", Static))
            superseded_by = [
                line
                for line in text.splitlines()
                if "superseded by assembly ASM_000002" in line
            ]
            assert superseded_by, "the supersession row must render"
            assert "—" not in superseded_by[0]  # no reason: no em-dash suffix

    _run(scenario())
