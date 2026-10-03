"""Terminal capabilities and text helpers (stdlib only).

Handles the Windows specifics: enabling ANSI escape processing on the
classic console, and UTF-8 output when stdout is redirected.
"""
from __future__ import annotations

import os
import shutil
import sys
import unicodedata

ESC = "\x1b["
HIDE_CURSOR = ESC + "?25l"
SHOW_CURSOR = ESC + "?25h"
CLEAR_LINE = ESC + "K"
CLEAR_DOWN = ESC + "J"
RESET = ESC + "0m"

_ansi_cache: bool | None = None


def configure_streams() -> None:
    """Make stdout/stderr UTF-8 so box glyphs survive redirection on Windows."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _enable_windows_vt() -> bool:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if mode.value & enable_vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except Exception:
        return False


def supports_ansi() -> bool:
    """True when stdout is an interactive terminal that understands ANSI codes."""
    global _ansi_cache
    if _ansi_cache is None:
        if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
            _ansi_cache = False
        elif not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
            _ansi_cache = False
        elif os.name == "nt":
            _ansi_cache = _enable_windows_vt()
        else:
            _ansi_cache = True
    return _ansi_cache


def disable_ansi() -> None:
    global _ansi_cache
    _ansi_cache = False


def color_depth() -> str:
    """'truecolor' or '256'. Windows 10+ consoles and Windows Terminal do 24-bit."""
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return "truecolor"
    if os.name == "nt":
        return "truecolor"
    if os.environ.get("TERM_PROGRAM") in ("iTerm.app", "vscode", "WezTerm", "ghostty", "Hyper"):
        return "truecolor"
    return "256"


def size() -> tuple[int, int]:
    s = shutil.get_terminal_size((100, 30))
    return max(s.columns, 20), max(s.lines, 10)


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def truncate(text: str, width: int, *, keep_end: bool = False) -> str:
    """Cut ``text`` to ``width`` cells, marking the cut with '…'.

    ``keep_end`` keeps the tail (useful for long folder paths).
    """
    if width <= 0:
        return ""
    if text_width(text) <= width:
        return text
    if width == 1:
        return "…"
    budget = width - 1
    chars = reversed(text) if keep_end else iter(text)
    kept, used = [], 0
    for ch in chars:
        w = char_width(ch)
        if used + w > budget:
            break
        kept.append(ch)
        used += w
    if keep_end:
        return "…" + "".join(reversed(kept))
    return "".join(kept) + "…"


def pad(text: str, width: int, *, keep_end: bool = False) -> str:
    """Truncate then right-pad with spaces to exactly ``width`` cells."""
    text = truncate(text, width, keep_end=keep_end)
    return text + " " * (width - text_width(text))


class Style:
    """Wraps text in SGR codes when colour is enabled; otherwise a no-op."""

    BOLD = "1"
    DIM = "2"
    INVERSE = "7"
    RED = "31"
    GREEN = "32"
    YELLOW = "33"
    BLUE = "34"
    MAGENTA = "35"
    CYAN = "36"

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes or not text:
            return text
        return f"{ESC}{';'.join(codes)}m{text}{RESET}"


def human_bytes(n: float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def duration(seconds: float) -> str:
    s = int(max(seconds, 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
