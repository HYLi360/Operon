"""Shared widgets, styles, and helpers for the Operon TUI screens."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import work
from textual.containers import VerticalScroll

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
