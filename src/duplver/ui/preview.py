"""Draw an image inside the terminal with coloured half-block characters.

Each character cell shows two pixels: the upper one as the foreground of
'▀', the lower one as the background. Works in Windows Terminal, the
Windows 10+ console, and any ANSI terminal (24-bit or 256 colours).
"""
from __future__ import annotations

from duplver.imaging import Preview
from duplver.ui.term import ESC, RESET

_UPPER = "▀"  # ▀
_LOWER = "▄"  # ▄


def _cube(v: int) -> int:
    return 0 if v < 48 else 1 if v < 115 else (v - 35) // 40


def rgb_to_256(r: int, g: int, b: int) -> int:
    """Nearest xterm-256 palette index (6×6×6 cube or the 24-step gray ramp)."""
    if max(r, g, b) - min(r, g, b) < 12:
        gray = (r + g + b) // 3
        if gray < 8:
            return 16
        if gray > 246:
            return 231
        return 232 + round((gray - 8) / 238 * 23)
    return 16 + 36 * _cube(r) + 6 * _cube(g) + _cube(b)


def _sgr(rgb: tuple[int, int, int], background: bool, depth: str) -> str:
    if depth == "truecolor":
        return f"{ESC}{48 if background else 38};2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    return f"{ESC}{48 if background else 38};5;{rgb_to_256(*rgb)}m"


def render(preview: Preview | None, cols: int, rows: int, depth: str = "truecolor") -> list[str]:
    """Return ``rows`` strings, each exactly ``cols`` cells wide.

    The image is centred in the box; empty space uses the default background.
    """
    if preview is None or preview.width == 0 or preview.height == 0:
        return [" " * cols for _ in range(rows)]

    w, h, data = preview.width, preview.height, preview.data
    off_x = max(0, (cols - w) // 2)
    off_y = max(0, (rows * 2 - h) // 2)

    def pixel(x: int, y: int) -> tuple[int, int, int] | None:
        ix, iy = x - off_x, y - off_y
        if 0 <= ix < w and 0 <= iy < h:
            i = (iy * w + ix) * 3
            return data[i], data[i + 1], data[i + 2]
        return None

    lines = []
    for row in range(rows):
        parts = []
        fg = bg = None  # currently active colours on this line
        for x in range(cols):
            top, bottom = pixel(x, row * 2), pixel(x, row * 2 + 1)
            if top is None and bottom is None:
                glyph, want_fg, want_bg = " ", None, None
            elif bottom is None:
                glyph, want_fg, want_bg = _UPPER, top, None
            elif top is None:
                glyph, want_fg, want_bg = _LOWER, bottom, None
            else:
                glyph, want_fg, want_bg = _UPPER, top, bottom
            if want_bg != bg:
                parts.append(_sgr(want_bg, True, depth) if want_bg else ESC + "49m")
                bg = want_bg
            if want_fg != fg and want_fg is not None:
                parts.append(_sgr(want_fg, False, depth))
                fg = want_fg
            parts.append(glyph)
        parts.append(RESET)
        lines.append("".join(parts))
    return lines
