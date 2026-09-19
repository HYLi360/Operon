"""Shared widgets, styles, and helpers for the Operon TUI screens."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.await_complete import AwaitComplete
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.widget import MountError
from textual.widgets import Button, DataTable, Label, Select, Static
from textual.widgets._select import SelectOverlay

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


_SCIENTIFIC_NAME_RANK = re.compile(
    r"(?<!\S)(?:subsp\.|ssp\.|var\.|subvar\.|f\.|subf\.|x)(?=\s|$)"
)


def styled_scientific_name(value: Any) -> Text:
    """Italicize scientific names, keeping standalone rank abbreviations upright.

    Preserve the input verbatim; this is a display helper, not a taxonomic
    parser. Unknown name components retain the default italic styling.
    """
    text = Text(str(value), style="italic")
    for match in _SCIENTIFIC_NAME_RANK.finditer(text.plain):
        text.stylize("not italic", match.start(), match.end())
    return text


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


# -- execution backends (shared by the analyze and run-external modals) -------


def project_default_backend(project: Any) -> str:
    """The ``execution.backend`` a CLI command uses when ``--backend`` is omitted."""
    execution = getattr(project, "config", {}) or {}
    name = str((execution.get("execution", {}) or {}).get("backend") or "local")
    return name.strip().lower() or "local"


def backend_select_options(project: Any) -> list[tuple[str, str]]:
    """Select options mirroring ``--backend``: the project default, then names."""
    return [
        (f"project default ({project_default_backend(project)})", ""),
        ("local", "local"),
        ("slurm", "slurm"),
        ("ssh", "ssh"),
    ]


def selected_backend(screen: Any, widget_id: str) -> str:
    """Read a backend Select ("" = project default); safe before the form mounts."""
    try:
        widget = screen.query_one(f"#{widget_id}", Select)
    except NoMatches:
        return ""
    return "" if widget.value is Select.NULL else str(widget.value)


class FittingSelect(Select):
    """A ``Select`` whose dropdown is as wide as its longest option.

    Textual sizes the overlay to the control (``width: 1fr``), so an option
    label longer than the control wraps onto a second line and stretches the
    row — the panel then looks ragged next to single-line neighbours.  This
    subclass sizes the overlay to its content (the longest label plus border
    and padding) when it expands, so every option renders on one line, left
    aligned; the width may therefore differ from the control's, capped at
    ``overlay_max_share`` of the terminal.  The collapsed control keeps its own
    width: ``app.tcss`` holds it to one line with an ellipsis.

    It also shields Textual's own mount-time lookups: ``Select._on_mount``
    queries ``SelectOverlay`` and ``SelectCurrent``'s label before its compose
    children exist when the widget is mounted through a deferred chain
    (``remount`` mounts a whole editor a message-loop turn later), which raised
    ``NoMatches`` from inside Textual and failed the app (ODR-0023).  The work
    is retried a turn at a time until the children are there.
    """

    overlay_max_share = 0.8
    _mount_retry_limit = 50

    def _watch_expanded(self, expanded: bool) -> None:
        super()._watch_expanded(expanded)
        if expanded:
            self.call_after_refresh(self._fit_overlay)

    def _fit_overlay(self) -> None:
        try:
            overlay = self.query_one(SelectOverlay)
        except NoMatches:
            return
        widest = max((len(str(label)) for label, _value in self._options), default=0)
        cap = max(8, int(self.app.size.width * self.overlay_max_share))
        overlay.styles.width = min(widest + 4, cap)

    def _on_mount(self, event: Any) -> None:
        try:
            super()._on_mount(event)
        except NoMatches:  # the overlay/label are not composed yet (ODR-0023)
            self._init_options_when_composed(attempt=0)

    def _init_options_when_composed(self, attempt: int) -> None:
        try:
            self._setup_options_renderables()
            self._init_selected_option(self._value)
        except NoMatches:
            if attempt < self._mount_retry_limit:
                self.call_after_refresh(self._init_options_when_composed, attempt + 1)


class MountTracked(Vertical):
    """A container that fills itself from ``on_mount`` and reports when it did.

    ``mount()`` lands a message-loop turn later, and the classification editor
    does this at every nesting level (a rule mounts its when-conditions, a
    source its filter editors, a condition editor its body rows), so "the
    panel rendered" is not the same as "the form can be read": a reader that
    runs in between sees an empty container, which either raises ``NoMatches``
    or silently drops the rows it cannot see (ODR-0023).  Readers ask
    :attr:`mounts_settled` first; the class carries the CSS class
    ``mount-tracked`` so a panel can look its tracked containers up in one
    query instead of naming every one of them.

    The answer is evaluated lazily instead of scheduled: a callback registered
    with ``call_after_refresh`` is posted to the widget's own message queue,
    which a container that is still being mounted never pumps, and it would
    leave the container waiting forever.  The check latches once it holds, so
    removing a row later (a user deleting a when-condition) does not re-arm it.
    """

    _waiting = False
    _wait_selector: str | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        classes = str(kwargs.pop("classes", "") or "")
        super().__init__(*args, classes=f"{classes} mount-tracked".strip(), **kwargs)

    def expect_mounts(self, when_present: str | None = None) -> None:
        """Mark the container as waiting for rows that are on their way.

        *when_present* is a selector the mounted rows must match (``None`` means
        "any child").  The check reads *this* container instead of a value
        captured by the caller: two calls that share a local variable would
        otherwise wait on the wrong container, and the container would never
        settle.
        """
        self._waiting = True
        self._wait_selector = when_present

    def mount_later(self, *widgets: Any, when_present: str | None = None) -> None:
        """Mount *widgets* now and report settled once *when_present* is matched."""
        self.expect_mounts(when_present)
        self.mount(*widgets)

    @property
    def mounts_settled(self) -> bool:
        """False while the rows this container mounts are not there yet."""
        if not self._waiting:
            return True
        selector = self._wait_selector
        present = bool(self.children) if selector is None else bool(self.query(selector))
        if present:
            self._waiting = False
            return True
        return False


def remount(container: Any, *widgets: Any) -> None:
    """Replace a container's children with ``widgets`` (atomically, from the UI).

    ``Widget.remove_children()`` completes asynchronously, so mounting in the
    same turn can be undone by the pending removal — the freshly mounted
    children vanish when the removal lands.  Deferring the mount past the next
    refresh keeps the replacement in order, and a :class:`MountTracked`
    container reports that the rows are on their way (ODR-0023).
    """
    container.remove_children()
    if not widgets:
        return
    if isinstance(container, MountTracked):
        container.expect_mounts()
    container.call_after_refresh(container.mount, *widgets)


class ComposedRows:
    """A row that reports when its own ``compose`` has landed.

    Textual composes a widget's whole subtree while mounting it, so a row that
    is already in the tree may still be missing the inputs — and the inputs'
    own children — that a reader queries; only the row's ``on_mount`` runs once
    that subtree exists.  Rows latch a flag there and readers ask
    :attr:`form_ready` (ODR-0023).  The latch is one-way on purpose: removing a
    row later (a user deleting a condition) must not make the form look like it
    is still mounting.
    """

    _form_ready = False

    def mark_form_ready(self) -> None:
        """Record that this row's composed subtree is in the tree."""

        self._form_ready = True

    @property
    def form_ready(self) -> bool:
        """True once this row's ``on_mount`` has run."""

        return self._form_ready


class WorkerResults:
    """Worker results for a widget that can disappear while they are in flight.

    A modal or a pushed screen can be dismissed while its worker still runs,
    and Textual then reports any widget lookup on the torn-down widget as
    ``NoMatches`` — raised inside the worker thread, where it escapes as
    ``WorkerFailed`` and fails the whole application (ODR-0022).  Workers of
    such a widget hand their result to :meth:`post_to_ui` instead of
    ``app.call_from_thread``; the result is rendered through
    :meth:`apply_from_worker` and dropped when there is nothing left to render.
    """

    #: Supplied by the concrete widget (``Widget.app``).
    app: Any

    def post_to_ui(self, callback: Callable[..., None], *args: Any) -> None:
        """Hand a worker result to the UI thread (call it from the worker)."""

        app = self.app
        if not app.is_running:  # pragma: no cover - shutdown race guard
            return
        try:
            app.call_from_thread(self.apply_from_worker, callback, *args)
        except RuntimeError:  # pragma: no cover - app is shutting down
            pass

    def apply_from_worker(self, callback: Callable[..., None], *args: Any) -> None:
        """Render a worker result, or drop it once the widget is gone."""

        try:
            callback(*args)
        except (MountError, NoMatches):
            return


class Panel(WorkerResults, VerticalScroll):
    """Base class for the four main panels.

    Data loads happen in short-lived worker threads against
    :mod:`operon.tui.data`; results and errors are marshalled back to the UI
    thread.  A failing load must never crash the app: the error is rendered
    inside the panel instead.
    """

    initial_load_complete = False
    initial_load_failed = False

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
        self.post_to_ui(self._apply, payload)

    def _apply(self, payload: Any) -> None:
        # The result can arrive while the panel is being torn down (the user
        # quit during the initial load) or before its children finished
        # mounting.  There is nowhere to render it then, and Textual reports
        # that as MountError/NoMatches; dropping the result is correct, while
        # letting it escape would fail the whole app from a worker thread.
        try:
            if isinstance(payload, BaseException):
                self.show_error(payload)
            else:
                self.render_data(payload)
        except (MountError, NoMatches):
            return
        if not self.initial_load_complete:
            self.initial_load_failed = isinstance(payload, BaseException)
            self.initial_load_complete = True

    def _fetch(self) -> Any:  # pragma: no cover - abstract stub
        raise NotImplementedError

    def render_data(self, payload: Any) -> None:  # pragma: no cover - abstract stub
        raise NotImplementedError

    def show_error(self, exc: BaseException) -> None:  # pragma: no cover - abstract stub
        raise NotImplementedError


class DismissOnce:
    """Mixin that makes ``Screen.dismiss()`` idempotent.

    Textual's ``dismiss()`` pops the screen stack unconditionally, so a
    second activation of the same close path — a double-clicked Cancel
    button, or ``escape`` racing a worker's completion callback — raises
    ``ScreenStackError`` and crashes the whole app.  The first call wins;
    later calls are no-ops.
    """

    _dismissed = False

    def dismiss(self, result: Any = None) -> AwaitComplete:
        if self._dismissed:
            return AwaitComplete.nothing()
        self._dismissed = True
        return super().dismiss(result)


class WriteModal(DismissOnce, WorkerResults, ModalScreen):
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
            # The form scrolls so the buttons below stay visible even when
            # the content (e.g. a wrapped long command line) exceeds the box.
            with VerticalScroll(id="modal-form"):
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
        self.post_to_ui(self._action_done, payload)

    def _action_done(self, payload: Any) -> None:
        self.set_confirm_enabled(True)
        if isinstance(payload, BaseException):
            self.show_error(payload)
            return
        self.on_action_success(payload)

    def on_action_success(self, payload: Any) -> None:
        self.dismiss(payload)


class ErrorDialog(DismissOnce, ModalScreen):
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
            with VerticalScroll(id="modal-form"):
                yield Static(Text(self.message, style="red"), id="error-dialog-body")
            with Horizontal(id="modal-buttons"):
                yield Button("OK", id="cancel", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
