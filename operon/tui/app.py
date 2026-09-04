"""Textual application shell for the Operon TUI.

The app is strictly read-only: it never opens a writable ``Database``.  Each
panel loads data through short-lived read-only connections in
:mod:`operon.tui.data`, so the TUI is safe to leave open while CLI commands
run against the same project.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher, Footer, Header, Label, ListItem, ListView, Static

from operon.config import Project
from operon.tui.screens.common import Panel
from operon.tui.screens.entities import EntitiesPanel
from operon.tui.screens.files import FilesPanel
from operon.tui.screens.home import HomePanel
from operon.tui.screens.runs import RunsPanel

SCREENS = ("home", "entities", "files", "runs")
NAV_LABELS = {
    "home": "1  Home",
    "entities": "2  Entities",
    "files": "3  Files",
    "runs": "4  Runs",
}


class HelpScreen(ModalScreen):
    """Modal listing the global key bindings."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(
            "Operon TUI — keys\n"
            "\n"
            "  1  Home dashboard\n"
            "  2  Entities browser\n"
            "  3  Files browser\n"
            "  4  Workflow runs\n"
            "  r  refresh current screen\n"
            "  t  toggle retired entities (Entities screen)\n"
            "  enter  open selected run (Runs screen)\n"
            "  esc  back / close\n"
            "  q  quit\n"
            "\n"
            "The TUI is strictly read-only; it never writes to the project.",
            id="help-body",
        )


class OperonApp(App):
    """Read-only TUI for an Operon project."""

    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "switch_screen('home')", "Home"),
        Binding("2", "switch_screen('entities')", "Entities"),
        Binding("3", "switch_screen('files')", "Files"),
        Binding("4", "switch_screen('runs')", "Runs"),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self.title = f"Operon — {project.config['project'].get('name') or project.project_id}"
        self.sub_title = f"{project.project_id} · {project.db_path}"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="app-layout"):
            yield ListView(
                *(ListItem(Label(label), id=f"nav-{name}") for name, label in NAV_LABELS.items()),
                id="nav",
            )
            with ContentSwitcher(initial="home", id="main"):
                yield HomePanel(self.project)
                yield EntitiesPanel(self.project)
                yield FilesPanel(self.project)
                yield RunsPanel(self.project)
        yield Footer()

    def action_switch_screen(self, name: str) -> None:
        if name not in SCREENS:
            return
        self.query_one("#main", ContentSwitcher).current = name
        nav = self.query_one("#nav", ListView)
        nav.index = SCREENS.index(name)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if item_id.startswith("nav-"):
            self.action_switch_screen(item_id[4:])

    def current_panel(self) -> Panel:
        switcher = self.query_one("#main", ContentSwitcher)
        return switcher.get_child_by_id(switcher.current or "home")

    def action_refresh(self) -> None:
        panel = self.current_panel()
        if isinstance(panel, Panel):
            panel.reload()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
