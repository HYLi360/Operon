"""Shared widgets, styles, and helpers for the Operon TUI screens."""

from __future__ import annotations

from typing import Any, Callable, Iterable

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, Static

ENTITY_TYPE_OPTIONS = [
    (entity_type, entity_type)
    for entity_type in ("organism", "sample", "run", "assembly", "annotation")
]

RUN_STATUS_STYLES = {
    "completed": "green",
    "running": "blue",
    "failed": "red",
    "interrupted": "yellow",
    "adopted": "cyan",
    "planned": "dim",
}

DECISION_STYLES = {
    "PASS": "green",
    "PASS_WITH_WARNINGS": "green_yellow",
    "ACCEPT_WITH_WARNING": "green_yellow",
    "REVIEW": "yellow",
    "FAIL": "red",
}

FILE_STATUS_STYLES = {
    "CHECKSUM_VERIFIED": "green",
    "STANDARDIZED": "green",
    "REMOTE_ONLY": "blue",
    "MISSING": "red",
    "CHECKSUM_FAILED": "red",
    "REMOTE_UNVERIFIED": "yellow",
}


def human_size(size_bytes: Any) -> str:
    """Render a byte count in human-readable units."""
    try:
        value = float(size_bytes)
    except (TypeError, ValueError):
        return "-"
    unit = "B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} B"
    return f"{value:.1f} {unit}"


def styled(value: Any, styles: dict[str, str]) -> Text:
    """Render a status/decision value with its color style."""
    text = "-" if value in (None, "") else str(value)
    return Text(text, style=styles.get(text, "dim"))


def styled_status(value: Any) -> Text:
    return styled(value, RUN_STATUS_STYLES)


def styled_decision(value: Any) -> Text:
    return styled(value, DECISION_STYLES)


def styled_file_status(value: Any) -> Text:
    return styled(value, FILE_STATUS_STYLES)


def format_duration(record: dict[str, Any]) -> str:
    """Render a workflow run duration like the CLI does."""
    duration = record.get("duration_seconds")
    if duration is None:
        return "-"
    return f"{float(duration):.3f}s"


def entity_label(record: dict[str, Any]) -> str:
    """Render the ``entity_type:entity_id`` label used in run listings."""
    return ":".join(
        part for part in (record.get("entity_type"), record.get("entity_id")) if part
    ) or "-"


def capture_table_view(table: DataTable) -> tuple[int, Offset]:
    """Capture cursor row and scroll offset before rebuilding a table."""
    return table.cursor_row, table.scroll_offset


def restore_table_view(table: DataTable, state: tuple[int, Offset], row_count: int) -> None:
    """Restore cursor row and scroll offset after repopulating a table.

    ``DataTable.clear()`` resets both to the origin; restoring them keeps an
    auto-refreshed table from jumping back to the top.  Runs after refresh so
    the new rows are laid out, and ``scroll_to`` is posted after the cursor
    watcher's scroll-into-view so the saved offset wins.
    """
    cursor_row, scroll_offset = state

    def restore() -> None:
        if row_count:
            table.move_cursor(row=min(max(cursor_row, 0), row_count - 1), animate=False)
        table.scroll_to(x=scroll_offset.x, y=scroll_offset.y, animate=False)

    table.call_after_refresh(restore)


class Panel(VerticalScroll):
    """Base class for the four main panels.

    Data loads happen in short-lived worker threads against
    :mod:`operon.tui.data`; results and errors are marshalled back to the UI
    thread.  A failing load must never crash the app: the error is rendered
    inside the panel instead.
    """

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        self._load()

    @work(thread=True)
    def _load(self) -> None:
        try:
            payload = self._fetch()
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel
            payload = exc
        app = self.app
        if not app.is_running:  # pragma: no cover - shutdown race guard
            return
        try:
            app.call_from_thread(self._apply, payload)
        except RuntimeError:  # pragma: no cover - app is shutting down
            pass

    def _apply(self, payload: Any) -> None:
        if isinstance(payload, BaseException):
            self.show_error(payload)
        else:
            self.render_data(payload)

    def _fetch(self) -> Any:  # pragma: no cover - abstract stub
        raise NotImplementedError

    def render_data(self, payload: Any) -> None:  # pragma: no cover - abstract stub
        raise NotImplementedError

    def show_error(self, exc: BaseException) -> None:  # pragma: no cover - abstract stub
        raise NotImplementedError


class WriteModal(ModalScreen):
    """Base class for phase-2 write-operation modals.

    Every write flow looks and behaves the same: a title, a form/preview
    body (``compose_form``), the equivalent CLI command line, an inline
    error area, and Confirm/Cancel buttons plus an ``esc`` binding.  The
    actual mutation runs in a thread worker inside the subclass and always
    goes through :mod:`operon.tui.actions`; on success the modal notifies
    and dismisses with a truthy result, on failure the exception message is
    shown inline and the modal stays open.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, title: str) -> None:
        super().__init__()
        self.modal_title = title

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self.modal_title, id="modal-title")
            yield from self.compose_form()
            yield Static("", id="modal-command")
            yield Static("", id="modal-error")
            with Horizontal(id="modal-buttons"):
                yield Button("Confirm", id="confirm", variant="primary")
                yield Button("Cancel", id="cancel")

    def compose_form(self) -> Iterable[Any]:
        return []

    def command_text(self) -> str:
        """The equivalent ``operon`` CLI command for the current inputs."""
        return ""

    def on_mount(self) -> None:
        self.refresh_command()

    def refresh_command(self) -> None:
        command = self.command_text()
        view = self.query_one("#modal-command", Static)
        view.update(Text(f"$ {command}", style="dim") if command else Text(""))

    def show_error(self, exc: BaseException | str) -> None:
        self.query_one("#modal-error", Static).update(Text(str(exc), style="red"))

    def clear_error(self) -> None:
        self.query_one("#modal-error", Static).update("")

    def set_confirm_enabled(self, enabled: bool) -> None:
        self.query_one("#confirm", Button).disabled = not enabled

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "confirm":
            self.confirm()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def confirm(self) -> None:  # pragma: no cover - abstract stub
        raise NotImplementedError

    def run_action(self, fn: Callable[[], Any]) -> None:
        """Run one write action in a thread worker with uniform handling.

        ``fn`` is a zero-argument callable invoking :mod:`operon.tui.actions`.
        Success routes to ``on_action_success``; any exception is shown in
        the inline error area and the modal stays open.
        """
        self.set_confirm_enabled(False)
        self.clear_error()
        self._execute(fn)

    @work(thread=True)
    def _execute(self, fn: Callable[[], Any]) -> None:
        try:
            payload: Any = fn()
        except Exception as exc:  # noqa: BLE001 - surfaced in the modal
            payload = exc
        app = self.app
        if not app.is_running:  # pragma: no cover - shutdown race guard
            return
        try:
            app.call_from_thread(self._action_done, payload)
        except RuntimeError:  # pragma: no cover - app is shutting down
            pass

    def _action_done(self, payload: Any) -> None:
        self.set_confirm_enabled(True)
        if isinstance(payload, BaseException):
            self.show_error(payload)
            return
        self.on_action_success(payload)

    def on_action_success(self, payload: Any) -> None:
        self.dismiss(payload)


class ErrorDialog(ModalScreen):
    """Simple modal showing an operation result/error with an OK button."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("enter", "dismiss", "Close"),
    ]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.dialog_title = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-box"):
            yield Label(self.dialog_title, id="modal-title")
            yield Static(Text(self.message, style="red"), id="error-dialog-body")
            with Horizontal(id="modal-buttons"):
                yield Button("OK", id="cancel", variant="primary")
