"""Textual application shell for the Operon TUI.

Read access goes through short-lived read-only connections in
:mod:`operon.tui.data`, so the TUI is safe to leave open while CLI commands
run against the same project.  Write operations (evaluate, curate,
retire/restore, ingest, verify, QC) go through :mod:`operon.tui.actions`,
which calls the same core functions as the CLI — every mutation follows
preview/form → explicit confirm → audited apply, and every dialog shows the
equivalent CLI command.
"""

from __future__ import annotations

import asyncio
from time import monotonic

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import ModalScreen, Screen
from textual.widgets import ContentSwitcher, Footer, Header, Label, ListItem, ListView, Static

from operon.config import Project
from operon.tui.screens.common import Panel
from operon.tui.screens.config import ConfigPanel
from operon.tui.screens.coverage import CoveragePanel
from operon.tui.screens.decisions import DecisionsPanel
from operon.tui.screens.entities import EntitiesPanel
from operon.tui.screens.files import FilesPanel
from operon.tui.screens.home import HomePanel
from operon.tui.screens.import_wizard import ImportWizardScreen
from operon.tui.screens.publish import PublishPanel
from operon.tui.screens.runs import RunsPanel
from operon.tui.splash import SplashScreen

SCREENS = ("home", "entities", "files", "runs", "decisions", "config", "publish", "coverage")
NAV_LABELS = {
    "home": "1  Home",
    "entities": "2  Entities",
    "files": "3  Files",
    "runs": "4  Tasks",
    "decisions": "5  Decisions",
    "config": "6  Config",
    "publish": "7  Publish",
    "coverage": "8  Coverage",
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
            "  4  Tasks — workflow-run monitor\n"
            "  5  Decisions\n"
            "  6  Config — QC profiles and tools/recipes editors\n"
            "  7  Publish — release builder and selective export builder\n"
            "  8  Coverage — taxonomy snapshots, reference sets, coverage reports\n"
            "  r  refresh current screen\n"
            "  i  import dataset wizard (except on the Files screen, where it ingests)\n"
            "  t  show/hide retired entities (Entities screen; shown dimmed by default)\n"
            "  x  retire/restore selected entity (Entities screen)\n"
            "  i  ingest file (Files screen)\n"
            "  v  verify files (Files screen)\n"
            "  q  run QC (Files screen; elsewhere: quit)\n"
            "  e  evaluate decisions (Decisions screen)\n"
            "  c  curate selected decision (Decisions screen)\n"
            "  enter  open selected run (Tasks screen)\n"
            "  esc  back / close\n"
            "  q  quit\n"
            "\n"
            "Write operations run the same audited core functions as the CLI:\n"
            "every change shows a preview, the equivalent CLI command, and an\n"
            "explicit Confirm before anything is written.",
            id="help-body",
        )


class OperonApp(App):
    """TUI for an Operon project (reads plus audited write operations)."""

    CSS_PATH = "app.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("1", "switch_screen('home')", "Home"),
        Binding("2", "switch_screen('entities')", "Entities"),
        Binding("3", "switch_screen('files')", "Files"),
        Binding("4", "switch_screen('runs')", "Tasks"),
        Binding("5", "switch_screen('decisions')", "Decisions"),
        Binding("6", "switch_screen('config')", "Config"),
        Binding("7", "switch_screen('publish')", "Publish"),
        Binding("8", "switch_screen('coverage')", "Coverage"),
        Binding("i", "import_dataset", "Import dataset"),
        Binding("r", "refresh", "Refresh"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self._starting = True
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
                yield DecisionsPanel(self.project)
                yield ConfigPanel(self.project)
                yield PublishPanel(self.project)
                yield CoveragePanel(self.project)
        yield Footer()

    async def on_mount(self) -> None:
        splash = SplashScreen()
        await self.push_screen(splash)
        # Start the clock only after the splash has actually been painted.
        splash.call_after_refresh(self._finish_startup, splash)

    @work
    async def _finish_startup(self, splash: SplashScreen) -> None:
        started = monotonic()
        panels = list(self.query(Panel))
        while self.is_running:
            pending = [panel for panel in panels if not panel.initial_load_complete]
            if pending:
                names = ", ".join((panel.id or "data").title() for panel in pending)
                splash.set_status(f"Loading {names}...")
            else:
                failed = sum(panel.initial_load_failed for panel in panels)
                splash.set_status("Loaded with errors" if failed else "Ready")
                if monotonic() - started >= 2.0:
                    self._starting = False
                    self.pop_screen()
                    if failed:
                        self.notify(
                            "Some panels could not load. See panel errors; press r to retry.",
                            severity="error",
                        )
                    return
            await asyncio.sleep(0.05)

    def action_switch_screen(self, name: str) -> None:
        if self._starting or name not in SCREENS:
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
        if self._starting:
            return
        panel = self.current_panel()
        if isinstance(panel, Panel):
            panel.reload()

    def reload_after_write(self) -> None:
        """Reload every panel after a successful audited write."""
        for panel in self.query(Panel):
            panel.reload()

    def action_help(self) -> None:
        if self._starting:
            return
        self.push_screen(HelpScreen())

    def action_import_dataset(self) -> None:
        # Only from the base screen: modals and the wizard itself keep `i` as
        # plain input, and the Files panel's own `i` binding (ingest) wins
        # whenever focus is inside that panel.
        if self._starting or type(self.screen) is not Screen:
            return
        self.push_screen(ImportWizardScreen(self.project))
