"""TUI/CLI parity tests (Layers 1 and 2).

Layer 1 — command coverage: every CLI leaf command appears exactly once in
``operon.tui.parity.REGISTRY``, every registry entry names a real CLI
command, and every entry is internally consistent (notes, resolvable
actions, importable modals).

Layer 2 — parameter mapping: for each ``implemented`` entry, every optional
CLI flag of the command is either mapped to a TUI widget/context in
``params`` or excused with a reason in ``waived``; stale keys that do not
correspond to a real CLI flag are errors.

These layers are pure argparse introspection and run without Textual.  Only
the modal-resolution check needs the ``tui`` extra and skips without it.
"""

from __future__ import annotations

import pytest

from operon.tui import parity
from operon.tui.parity import (
    REGISTRY,
    STATUS_CLI_ONLY,
    STATUS_IMPLEMENTED,
    STATUS_PLANNED,
    STATUSES,
)


def _cli_leaves() -> dict[tuple[str, ...], object]:
    return dict(parity.iter_cli_commands())


def _registry_commands() -> list[tuple[str, ...]]:
    return [entry.command for entry in REGISTRY]


# -- Layer 1: command coverage -----------------------------------------------


def test_every_cli_leaf_command_is_registered_exactly_once() -> None:
    leaves = _cli_leaves()
    registered = _registry_commands()
    duplicates = sorted({cmd for cmd in registered if registered.count(cmd) > 1})
    assert not duplicates, "duplicate TUI parity entries for: " + ", ".join(
        " ".join(cmd) for cmd in duplicates
    )
    missing = sorted(set(leaves) - set(registered))
    assert not missing, (
        "CLI command(s) without a TUI parity entry: "
        + ", ".join(" ".join(cmd) for cmd in missing)
        + " — implement the command in the TUI or register it as "
        "cli-only/planned with a reason in operon/tui/parity.py"
    )


def test_every_registry_entry_exists_in_the_cli_tree() -> None:
    leaves = _cli_leaves()
    stale = sorted(set(_registry_commands()) - set(leaves))
    assert not stale, (
        "parity registry entries with no matching CLI command (TUI ahead of "
        "CLI or stale entry): " + ", ".join(" ".join(cmd) for cmd in stale)
    )


def test_registry_covers_the_same_command_set_as_the_cli() -> None:
    assert set(_registry_commands()) == set(_cli_leaves())


def test_entry_statuses_are_valid() -> None:
    for entry in REGISTRY:
        assert entry.status in STATUSES, (
            f"{entry.command_text}: unknown status {entry.status!r}"
        )


def test_cli_only_and_planned_entries_have_a_reason() -> None:
    for entry in REGISTRY:
        if entry.status in (STATUS_CLI_ONLY, STATUS_PLANNED):
            assert entry.note.strip(), (
                f"{entry.command_text}: {entry.status} entries must record "
                "a reason / milestone in `note`"
            )


def test_implemented_entries_resolve_their_action() -> None:
    for entry in REGISTRY:
        if entry.status != STATUS_IMPLEMENTED:
            continue
        assert entry.actions, (
            f"{entry.command_text}: implemented entries must name an "
            "actions.* or data.* function"
        )
        assert entry.actions.split(".")[0] in ("actions", "data"), (
            f"{entry.command_text}: actions must reference the actions or "
            f"data module, got {entry.actions!r}"
        )
        assert callable(parity.resolve_action(entry)), (
            f"{entry.command_text}: {entry.actions} is not callable"
        )


def test_implemented_entries_resolve_their_modal() -> None:
    pytest.importorskip("textual")
    for entry in REGISTRY:
        if entry.status != STATUS_IMPLEMENTED or not entry.modal:
            continue
        modal = parity.resolve_modal(entry)
        assert isinstance(modal, type), (
            f"{entry.command_text}: {entry.modal} did not resolve to a class"
        )


def test_strict_mode_flags_planned_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    planned = [
        entry.command_text for entry in REGISTRY if entry.status == STATUS_PLANNED
    ]
    assert planned, "registry sanity check: expected at least one planned entry"

    monkeypatch.delenv("OPERON_PARITY_STRICT", raising=False)
    assert parity.strict_violations() == []

    monkeypatch.setenv("OPERON_PARITY_STRICT", "1")
    assert sorted(parity.strict_violations()) == sorted(planned)

    monkeypatch.setenv("OPERON_PARITY_STRICT", "0")
    assert parity.strict_violations() == []


# -- Layer 2: parameter mapping ------------------------------------------------
#
# Mapping convention (see operon/tui/parity.py): ``params``/``waived`` are
# keyed by argparse dest (``entity_type`` for ``--entity-type``).  Positional
# arguments are excluded — they arrive from screen context (selected row,
# wizard flow) rather than a flag — and so is ``-h/--help``, which is
# argparse plumbing with no TUI meaning.


def _implemented_entries() -> list[parity.ParityEntry]:
    return [entry for entry in REGISTRY if entry.status == STATUS_IMPLEMENTED]


def test_implemented_entries_cover_every_cli_flag() -> None:
    problems: list[str] = []
    for entry in _implemented_entries():
        options = parity.cli_options(entry.command)
        uncovered = sorted(set(options) - set(entry.params) - set(entry.waived))
        if uncovered:
            problems.append(
                f"{entry.command_text}: CLI flag(s) {uncovered} are neither "
                "mapped in params nor excused in waived"
            )
    assert not problems, "\n".join(problems)


def test_implemented_entries_have_no_stale_mapping_keys() -> None:
    problems: list[str] = []
    for entry in _implemented_entries():
        options = parity.cli_options(entry.command)
        stale_params = sorted(set(entry.params) - set(options))
        stale_waived = sorted(set(entry.waived) - set(options))
        if stale_params:
            problems.append(
                f"{entry.command_text}: params key(s) {stale_params} do not "
                "match any CLI flag dest"
            )
        if stale_waived:
            problems.append(
                f"{entry.command_text}: waived key(s) {stale_waived} do not "
                "match any CLI flag dest"
            )
    assert not problems, "\n".join(problems)


def test_waived_flags_have_reasons_and_params_have_targets() -> None:
    for entry in _implemented_entries():
        for dest, reason in entry.waived.items():
            assert reason.strip(), (
                f"{entry.command_text}: waived flag {dest!r} needs a reason"
            )
        for dest, target in entry.params.items():
            assert target.strip(), (
                f"{entry.command_text}: params flag {dest!r} needs a widget "
                "id or a 'context: …' target"
            )
