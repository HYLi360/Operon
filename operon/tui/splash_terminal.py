"""Terminal selection, text branding, and the dependency-free Kitty wire format."""

from __future__ import annotations

import base64
from collections.abc import Mapping

from rich.text import Text


def splash_mode(env: Mapping[str, str]) -> str:
    """Conservatively select graphics from terminal hints, never from SSH alone."""
    requested = env.get("OPERON_SPLASH", "auto").lower()
    if requested in {"text", "blocks", "kitty"}:
        return requested
    term = env.get("TERM", "").lower()
    if "NO_COLOR" in env or term in {"", "dumb", "linux"}:
        return "text"
    multiplexed = bool(env.get("TMUX") or env.get("STY")) or term.startswith(("screen", "tmux"))
    if term == "xterm-kitty" and not multiplexed:
        return "kitty"
    if "256color" in term or env.get("COLORTERM", "").lower() in {"truecolor", "24bit"}:
        return "blocks"
    return "text"


# Five-by-five rounded-square glyphs: readable even on an 80-column console.
_GLYPHS = {
    "O": ("01110", "11011", "11011", "11011", "01110"),
    "P": ("11110", "11011", "11110", "11000", "11000"),
    "E": ("11111", "11000", "11110", "11000", "11111"),
    "R": ("11110", "11011", "11110", "11011", "11011"),
    "N": ("10001", "11001", "10101", "10011", "10001"),
}


def text_brand(width: int, height: int, *, unicode: bool = True, color: bool = True) -> Text:
    """Centered cyan/white branding, with ASCII and narrow-terminal fallbacks."""
    result = Text(no_wrap=True, overflow="crop")
    if width <= 0 or height <= 0:
        return result
    block = "█" if unicode else "#"
    if width >= 35 and height >= 7:
        lines = [" ".join(_GLYPHS[letter][row] for letter in "OPERON")
                 .replace("1", block).replace("0", " ") for row in range(5)]
        lines += ["", "T h e  D a t a b a s e  S y s t e m"]
    else:
        lines = ["OPERON", "The Database System"][:height]
    result.append("\n" * max(0, (height - len(lines)) // 2))
    for index, line in enumerate(lines):
        result.append(line[:width].center(width), style="bold cyan" if color else "bold")
        if index < len(lines) - 1:
            result.append("\n")
    return result


def kitty_command(control: str, payload: str = "") -> str:
    return f"\x1b_G{control};{payload}\x1b\\"


def kitty_upload(png: bytes, image_id: int) -> str:
    """Transmit PNG inline (works remotely), in protocol-sized base64 chunks."""
    encoded = base64.b64encode(png).decode("ascii")
    commands = []
    for start in range(0, len(encoded), 4096):
        chunk = encoded[start:start + 4096]
        more = int(start + 4096 < len(encoded))
        control = f"a=t,t=d,f=100,i={image_id},q=2," if start == 0 else "q=2,"
        commands.append(kitty_command(f"{control}m={more}", chunk))
    return "".join(commands)


def kitty_delete(image_id: int, *, free: bool = True) -> str:
    """Delete only our image (uppercase I frees pixels as well as placements)."""
    return kitty_command(f"a=d,d={'I' if free else 'i'},i={image_id},q=2")


def kitty_place(image_id: int, x: int, y: int, width: int, height: int) -> str:
    """Place within the art widget, preserving Textual's cursor and footer."""
    columns = min(width, max(1, height * 8 // 3))
    rows = min(height, max(1, columns * 3 // 8))
    x += (width - columns) // 2
    y += (height - rows) // 2
    return ("\x1b7" + f"\x1b[{y + 1};{x + 1}H"
            + kitty_command(f"a=p,i={image_id},p=1,c={columns},r={rows},C=1,z=1,q=2")
            + "\x1b8")
