"""Progress reporting for a scan.

``LiveUI``   — redraws a fixed region of the terminal ~10×/s, showing the
               image currently being processed (half-block preview) next to
               the progress bar, speed, ETA, file details and running counts.
``PlainUI``  — line-based progress for redirected output / old terminals.
``ProgressUI`` (base) — silent; used for ``--json``.

The pipeline calls these methods from the main thread only. ``LiveUI`` owns a
render thread that is the *only* writer to stdout while it is active.
"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import time
from dataclasses import dataclass, field

from duplver.imaging import Preview
from duplver.ui import preview as preview_mod
from duplver.ui import term
from duplver.ui.term import Style


@dataclass
class Item:
    """The file currently being worked on."""

    path: str
    detail: str = ""
    preview: Preview | None = None
    error: str | None = None


@dataclass
class _State:
    step: int = 0
    steps: int = 0
    title: str = ""
    total: int | None = None
    done: int = 0
    started: float = field(default_factory=time.monotonic)
    item: Item | None = None
    preview: Preview | None = None
    status: str = ""
    counters: dict = field(default_factory=dict)


class ProgressUI:
    """Silent base implementation (also documents the interface)."""

    # (columns, rows) of the preview box, or None when previews aren't shown.
    preview_box: tuple[int, int] | None = None

    def __init__(self) -> None:
        self.state = _State()

    def begin(self, root: str) -> None:
        self.root = root

    def stage(self, step: int, steps: int, title: str, total: int | None = None) -> None:
        self.state = _State(step=step, steps=steps, title=title, total=total)

    def set_total(self, total: int | None) -> None:
        self.state.total = total
        self.state.done = 0
        self.state.started = time.monotonic()

    def status(self, text: str) -> None:
        self.state.status = text

    def advance(self, n: int = 1, item: Item | None = None) -> None:
        self.state.done += n
        if item is not None:
            self.state.item = item
            if item.preview is not None:
                self.state.preview = item.preview

    def count(self, label: str, value: int) -> None:
        self.state.counters[label] = value

    def note(self, text: str) -> None:
        pass

    def end_stage(self, summary: str = "") -> None:
        pass

    def close(self) -> None:
        pass

    # Helpers shared by the visible UIs ------------------------------------
    def _rate_eta(self) -> tuple[float, float | None]:
        s = self.state
        elapsed = max(time.monotonic() - s.started, 1e-6)
        rate = s.done / elapsed
        if not s.total or rate <= 0:
            return rate, None
        return rate, max(s.total - s.done, 0) / rate


_CHECK = "√" if os.name == "nt" else "✓"  # √ renders in every Windows console font


class PlainUI(ProgressUI):
    """One line per event; progress at most every two seconds."""

    def __init__(self, style: Style) -> None:
        super().__init__()
        self.s = style
        self._last_print = 0.0

    def _out(self, text: str) -> None:
        print(text, flush=True)

    def begin(self, root: str) -> None:
        super().begin(root)
        self._out(self.s(f"Duplver · scanning {root}", Style.BOLD))

    def stage(self, step, steps, title, total=None) -> None:
        super().stage(step, steps, title, total)
        suffix = f" ({total:,})" if total else ""
        self._out(self.s(f"[{step}/{steps}] {title}{suffix}", Style.CYAN))
        self._last_print = time.monotonic()

    def status(self, text: str) -> None:
        super().status(text)
        if text:
            self._out(f"      {text}")

    def advance(self, n=1, item=None) -> None:
        super().advance(n, item)
        now = time.monotonic()
        if now - self._last_print < 2.0:
            return
        self._last_print = now
        s = self.state
        name = os.path.basename(s.item.path) if s.item else ""
        if s.total:
            self._out(f"      {s.done / s.total * 100:5.1f}%  {s.done:,}/{s.total:,}  {name}")
        else:
            self._out(f"      {s.done:,}  {name}")

    def note(self, text: str) -> None:
        self._out("  " + text)

    def end_stage(self, summary: str = "") -> None:
        took = term.duration(time.monotonic() - self.state.started)
        self._out(f"      {self.s(_CHECK, Style.GREEN)} {summary} ({took})")


class LiveUI(ProgressUI):
    """Full-screen-region live view with an inline image preview."""

    _FPS = 10

    def __init__(self, style: Style, *, show_image: bool = True) -> None:
        super().__init__()
        self.s = style
        self.depth = term.color_depth()
        cols, rows = term.size()
        self._box: tuple[int, int] | None = None
        if show_image and cols >= 72 and rows >= 18:
            box_cols = max(24, min(44, (cols - 1) // 3))
            box_rows = max(10, min(box_cols // 2, rows - 7))
            self._box = (box_cols, box_rows)
        self.preview_box = self._box
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._notes: list[str] = []
        self._height = 0
        self._thread: threading.Thread | None = None
        self._stage_started = time.monotonic()

    # ---- interface ------------------------------------------------------
    def begin(self, root: str) -> None:
        super().begin(root)
        sys.stdout.write(term.HIDE_CURSOR)
        sys.stdout.flush()
        atexit.register(self._restore_cursor)
        self._thread = threading.Thread(target=self._loop, name="duplver-ui", daemon=True)
        self._thread.start()

    def stage(self, step, steps, title, total=None) -> None:
        with self._lock:
            keep_preview = self.state.preview
            super().stage(step, steps, title, total)
            self.state.preview = keep_preview
            self._stage_started = time.monotonic()
        self._wake.set()

    def set_total(self, total) -> None:
        with self._lock:
            super().set_total(total)

    def status(self, text: str) -> None:
        with self._lock:
            super().status(text)
        self._wake.set()

    def advance(self, n=1, item=None) -> None:
        with self._lock:
            super().advance(n, item)

    def count(self, label: str, value: int) -> None:
        with self._lock:
            super().count(label, value)

    def note(self, text: str) -> None:
        with self._lock:
            self._notes.append("  " + text)
        self._wake.set()

    def end_stage(self, summary: str = "") -> None:
        with self._lock:
            took = term.duration(time.monotonic() - self._stage_started)
            line = (
                f"  {self.s(_CHECK, Style.GREEN, Style.BOLD)} "
                f"{self.s(self.state.title, Style.BOLD)}"
                f"{self.s(' — ' + summary if summary else '', Style.DIM)}"
                f"{self.s(f'  ({took})', Style.DIM)}"
            )
            self._notes.append(line)
        self._wake.set()

    def close(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._wake.set()
            self._thread.join(timeout=2.0)
            self._thread = None
            self._draw(final=True)
        self._restore_cursor()

    # ---- rendering ------------------------------------------------------
    def _restore_cursor(self) -> None:
        try:
            sys.stdout.write(term.SHOW_CURSOR)
            sys.stdout.flush()
        except Exception:
            pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._draw()
            except Exception:
                pass  # a rendering glitch must never kill the scan
            self._wake.wait(1.0 / self._FPS)
            self._wake.clear()

    def _draw(self, final: bool = False) -> None:
        with self._lock:
            notes, self._notes = self._notes, []
            lines = [] if final else self._compose()
        out = []
        if self._height:
            out.append(f"\r{term.ESC}{self._height}A")
        for line in notes + lines:
            out.append(line + term.RESET + term.CLEAR_LINE + "\n")
        out.append(term.CLEAR_DOWN)
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self._height = len(lines)

    def _compose(self) -> list[str]:
        cols, rows = term.size()
        width = cols - 1  # never touch the last column (avoids auto-wrap)
        s, st = self.s, self.state
        box = self._box if self._box and width >= 72 and rows >= self._box[1] + 6 else None

        header = term.truncate(f" Duplver · {getattr(self, 'root', '')}", width)
        lines = [s(header, Style.BOLD), ""]

        panel_w = width - (box[0] + 3 if box else 2)
        panel = self._panel(panel_w)
        if box:
            image = preview_mod.render(st.preview, box[0], box[1], self.depth)
            for i in range(max(box[1], len(panel))):
                left = image[i] if i < len(image) else " " * box[0]
                right = panel[i] if i < len(panel) else ""
                lines.append(f" {left}  {right}")
        else:
            lines.extend("  " + p for p in panel)

        lines.append("")
        lines.append(s(term.truncate("  Ctrl+C to stop · finished work is saved and the next scan resumes", width), Style.DIM))
        # Never draw taller than the screen, or cursor-up can't reach the top.
        return lines[: max(rows - 1, 3)]

    def _panel(self, width: int) -> list[str]:
        s, st = self.s, self.state
        out = [s(term.truncate(f"Step {st.step}/{st.steps} · {st.title}", width), Style.BOLD, Style.CYAN)]

        bar_w = max(10, min(40, width - 8))
        rate, eta = self._rate_eta()
        if st.total:
            frac = min(st.done / st.total, 1.0)
            filled = int(round(frac * bar_w))
            bar = s("█" * filled, Style.GREEN) + s("░" * (bar_w - filled), Style.DIM)
            out.append(f"{bar} {frac * 100:5.1f}%")
            counts = f"{st.done:,} / {st.total:,}  ·  {rate:,.1f}/s"
            if eta is not None and st.done:
                counts += f"  ·  ETA {term.duration(eta)}"
        else:
            # Indeterminate: a block sliding along the bar.
            pos = int(time.monotonic() * 12) % bar_w
            cells = ["░"] * bar_w
            for k in range(pos, min(pos + 4, bar_w)):
                cells[k] = "█"
            out.append(s("".join(cells), Style.CYAN))
            counts = f"{st.done:,} so far"
        out.append(term.truncate(counts, width))

        out.append(s(term.truncate(st.status, width), Style.YELLOW) if st.status else "")

        item = st.item
        if item is not None:
            folder, name = os.path.split(item.path)
            out.append(s("Now  ", Style.DIM) + s(term.truncate(name, width - 5), Style.BOLD))
            out.append(s("     " + term.truncate(folder, width - 5, keep_end=True), Style.DIM))
            if item.error:
                out.append(s("     " + term.truncate(item.error, width - 5), Style.RED))
            else:
                out.append("     " + term.truncate(item.detail, width - 5))
        else:
            out.extend(["", "", ""])

        if st.counters:
            out.append("")
            for label, value in st.counters.items():
                out.append(term.truncate(f"{label}: ", width - 8) + s(f"{value:,}", Style.BOLD))
        return out


def make_ui(mode: str, style: Style) -> ProgressUI:
    """mode: 'live' | 'text' (live without image) | 'plain' | 'silent'."""
    if mode == "silent":
        return ProgressUI()
    if mode in ("live", "text") and term.supports_ansi():
        return LiveUI(style, show_image=(mode == "live"))
    return PlainUI(style)
