"""Portable night-lake splash; no terminal image protocol or imaging dependency."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
import zlib

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static

from operon import __version__


@lru_cache(maxsize=1)
def lake_pixels() -> bytes:
    """Load the pre-sampled RGB companion to the 1024 × 768 source image."""
    pixels = zlib.decompress(
        files("operon.tui").joinpath("assets/splash.rgb.z").read_bytes()
    )
    if len(pixels) != 256 * 192 * 3:
        raise ValueError("Invalid splash pixel data")
    return pixels


@lru_cache(maxsize=8)
def lake_text(width: int, height: int) -> Text:
    """Fit 4:3 art using two square color samples per terminal cell.

    A terminal cell is assumed to be twice as tall as it is wide. Rich
    handles terminal color reduction; no escape sequences bypass Textual.
    """
    result = Text(no_wrap=True, overflow="crop")
    if width < 1 or height < 1:
        return result
    try:
        pixels = lake_pixels()
    except (OSError, ValueError, zlib.error):
        return Text("OPERON\nThe Database System", style="bold #bce9ed on #061522")
    columns = min(width, max(1, height * 8 // 3))
    rows = min(height, max(1, columns * 3 // 8))
    left = (width - columns) // 2
    top = (height - rows) // 2
    result.append("\n" * top)
    caption = "The Database System"
    target = min(columns, max(len(caption), int(columns * 0.46)))
    extra, remainder = divmod(max(0, target - len(caption)), len(caption) - 1)
    caption = "".join(
        char + (" " * (extra + (index < remainder)) if index < len(caption) - 1 else "")
        for index, char in enumerate(caption)
    )
    caption_left = (columns - target) // 2
    for row in range(rows):
        result.append(" " * left)
        for column in range(columns):
            x = min(255, int((column + 0.5) * 256 / columns))
            # Replace the raster subtitle with readable terminal text, sampling
            # nearby lake-sky colors so the caption has no full-width banner.
            if (columns >= 19 and caption_left <= column < caption_left + target
                    and int(rows * 0.50) <= row <= int(rows * 0.54)):
                offset = (int(192 * 0.55) * 256 + x) * 3
                background = "#" + pixels[offset:offset + 3].hex()
                char = caption[column - caption_left] if row == int(rows * 0.52) else " "
                result.append(char, Style(color="#bce9ed", bgcolor=background))
                continue
            colors = []
            for half in range(2):
                y = min(191, int((row * 2 + half + 0.5) * 192 / (rows * 2)))
                offset = (y * 256 + x) * 3
                colors.append("#" + pixels[offset:offset + 3].hex())
            result.append("▀", Style(color=colors[0], bgcolor=colors[1]))
        if row < rows - 1:
            result.append("\n")
    return result


class LakeArt(Static):
    """Re-render at the actual terminal size, including live resizes."""

    def render(self) -> Text:
        return lake_text(self.size.width, self.size.height)


class SplashScreen(ModalScreen):
    """Block navigation while initial reads finish; quitting stays available."""

    BINDINGS = [Binding("q", "app.quit", "Quit", priority=True)]
    DEFAULT_CSS = """
    SplashScreen { background: #000000; layout: vertical; }
    LakeArt { width: 1fr; height: 1fr; background: #000000; overflow: hidden; }
    #splash-footer { height: 1; background: #000000; color: #ffffff; }
    #splash-status { width: 1fr; color: #ffffff; background: #000000; }
    #splash-version { width: auto; color: #ffffff; background: #000000; }
    """

    def compose(self) -> ComposeResult:
        yield LakeArt()
        with Horizontal(id="splash-footer"):
            yield Static("Loading project data...", id="splash-status", markup=False)
            yield Static(f"Version {__version__}", id="splash-version", markup=False)

    def set_status(self, status: str) -> None:
        self.query_one("#splash-status", Static).update(status)
