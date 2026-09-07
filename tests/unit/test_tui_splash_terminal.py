"""Terminal capability fallback and Kitty protocol lifecycle regressions."""

import asyncio
import base64
from importlib.resources import files
from types import SimpleNamespace

import pytest

from operon.tui.splash_terminal import (
    kitty_delete, kitty_place, kitty_upload, splash_mode, text_brand,
)


@pytest.mark.parametrize("env, expected", [
    ({}, "text"), ({"TERM": "linux", "COLORTERM": "truecolor"}, "text"),
    ({"TERM": "dumb"}, "text"), ({"TERM": "vt100", "SSH_CONNECTION": "yes"}, "text"),
    ({"TERM": "xterm"}, "text"), ({"TERM": "xterm-256color"}, "blocks"),
    ({"TERM": "xterm", "COLORTERM": "truecolor"}, "blocks"),
    ({"TERM": "xterm-kitty", "SSH_CONNECTION": "yes"}, "kitty"),
    ({"TERM": "xterm-kitty", "NO_COLOR": ""}, "text"),
    ({"TERM": "xterm-kitty", "TMUX": "yes"}, "text"),
    ({"TERM": "screen-256color", "STY": "yes"}, "blocks"),
    ({"TERM": "xterm-kitty", "OPERON_SPLASH": "text"}, "text"),
    ({"TERM": "xterm", "OPERON_SPLASH": "kitty"}, "kitty"),
    ({"TERM": "linux", "OPERON_SPLASH": "blocks"}, "blocks"),
    ({"TERM": "linux", "OPERON_SPLASH": "invalid"}, "text"),
])
def test_terminal_selection(env, expected):
    assert splash_mode(env) == expected


@pytest.mark.parametrize("width,height", [(0, 0), (1, 1), (20, 4), (35, 7), (80, 24)])
def test_brand_fits_small_terminals(width, height):
    for unicode in (True, False):
        rendered = text_brand(width, height, unicode=unicode, color=False)
        assert len(rendered.plain.splitlines()) <= height
        assert all(len(line) <= width for line in rendered.plain.splitlines())
        if not unicode:
            assert rendered.plain.isascii()
    assert "█" in text_brand(80, 24).plain


def test_kitty_png_roundtrip_and_chunk_limit():
    png = files("operon.tui").joinpath("assets/splash.png").read_bytes()
    wire = kitty_upload(png, 42)
    packets = wire.split("\x1b\\")[:-1]
    assert len(packets) > 1
    chunks = []
    for index, packet in enumerate(packets):
        controls, payload = packet.removeprefix("\x1b_G").split(";", 1)
        assert len(payload) <= 4096
        assert len(payload) % 4 == 0
        assert f"m={int(index < len(packets) - 1)}" in controls
        assert "q=2" in controls
        chunks.append(payload)
    assert "a=t,t=d,f=100,i=42" in packets[0]
    assert base64.b64decode("".join(chunks)) == png
    assert "d=I,i=42" in kitty_delete(42)
    assert "d=i,i=42" in kitty_delete(42, free=False)
    place = kitty_place(42, 0, 0, 80, 23)
    assert "c=61,r=22,C=1" in place  # Leaves the footer outside the placement.
    assert place.startswith("\x1b7") and place.endswith("\x1b8")


def test_kitty_widget_lifecycle(monkeypatch):
    """Exercise transfer, resize, suspend/resume, and deletion via a fake wire."""
    from textual.app import App
    from operon.tui.splash import LakeArt, SplashScreen

    monkeypatch.setenv("OPERON_SPLASH", "kitty")
    output = []
    wire = SimpleNamespace(is_headless=False, write=output.append)

    class Preview(App):
        async def on_mount(self):
            await self.push_screen(SplashScreen())

    async def scenario():
        app = Preview()
        async with app.run_test(size=(80, 24)) as pilot:
            art = app.screen.query_one(LakeArt)
            real_driver = app._driver
            def display():
                app._driver = wire
                try:
                    art.display_image()
                finally:
                    app._driver = real_driver
            display()
            assert art._uploaded
            assert "a=t,t=d,f=100" in output[0]
            count = len(output)
            display()
            assert len(output) == count  # No retransmission on status updates.
            await pilot.resize_terminal(100, 30)
            display()
            assert len(output) == count + 1  # Placement only.
            app._driver = wire
            try:
                art.hide_image()
            finally:
                app._driver = real_driver
            assert f"d=I,i={art._image_id}" in output[-1]
            assert not art._uploaded
            display()
            assert art._uploaded
            # Covering the splash removes graphics, resuming can transmit again.
            app._driver = wire
            try:
                app.screen.on_screen_suspend()
            finally:
                app._driver = real_driver
            assert not art._uploaded
            display()
            assert art._uploaded
            app.pop_screen()
            await pilot.pause()
            assert not art._uploaded

    asyncio.run(scenario())


def test_kitty_output_failure_preserves_usable_text(monkeypatch):
    from textual.app import App
    from operon.tui.splash import LakeArt, SplashScreen
    monkeypatch.setenv("OPERON_SPLASH", "kitty")

    def disconnected(data):
        raise OSError("terminal disconnected")

    async def scenario():
        app = App()
        async with app.run_test() as pilot:
            await app.push_screen(SplashScreen())
            await pilot.pause()
            art = app.screen.query_one(LakeArt)
            driver = app._driver
            app._driver = SimpleNamespace(is_headless=False, write=disconnected)
            try:
                art.display_image()
            finally:
                app._driver = driver
            assert art.mode == "text"
            assert not art._uploaded
            assert "D" in art.render().plain
    asyncio.run(scenario())


def test_text_screen_restores_palette(monkeypatch):
    from textual.app import App
    from operon.tui.splash import SplashScreen
    monkeypatch.setenv("OPERON_SPLASH", "text")

    async def scenario():
        app = App()
        async with app.run_test() as pilot:
            previous = app.ansi_color
            await app.push_screen(SplashScreen())
            await pilot.pause()
            assert app.ansi_color is True
            app.pop_screen()
            await pilot.pause()
            assert app.ansi_color == previous
    asyncio.run(scenario())
