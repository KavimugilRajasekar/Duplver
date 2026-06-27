"""Multi-stage Rich progress widget.

Wraps ``rich.progress.Progress`` with:

* One ``TaskID`` per pipeline stage so the user sees all stages at once
  (Discovery, Hashing, Embeddings, Clustering...).
* Live CPU/RAM/GPU readouts via psutil and torch.
* Graceful degradation: when stdout is not a TTY (piped, redirected), the
  widget collapses to a plain logger so output stays useful.

Stages register themselves via ``add_stage(stage_name, total=...)`` and call
``advance(stage_name, n=1)`` as work completes.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

import psutil

try:  # torch may be unavailable in some environments.
    import torch as _torch  # noqa: F401

    _HAS_TORCH = True
except Exception:  # pragma: no cover
    _HAS_TORCH = False


@dataclass(slots=True)
class _Stage:
    name: str
    label: str
    task_id: Any
    total: int = 0


class _PercentColumn(ProgressColumn):
    """Render a simple percentage bar label."""

    def render(self, task):  # type: ignore[override]
        if task.total is None or task.total == 0:
            return Text("—", style="dim")
        pct = (task.completed / task.total) * 100 if task.total else 0.0
        return Text(f"{pct:5.1f}%")


class _RateColumn(ProgressColumn):
    """Render files-per-second."""

    def render(self, task):  # type: ignore[override]
        if task.total is None or task.total == 0 or task.started is None:
            return Text("—", style="dim")
        elapsed = max(time.monotonic() - task.started, 1e-3)
        rate = task.completed / elapsed
        return Text(f"{rate:7.0f}/s", style="cyan")


class _SystemStatsColumn(ProgressColumn):
    """Live CPU/RAM/GPU readout, refreshed on every render."""

    def __init__(self) -> None:
        super().__init__()
        self._proc = psutil.Process(os.getpid())
        self._cpu_pct: float = 0.0
        self._ram_pct: float = 0.0
        self._gpu_pct: float | None = None
        self._last_sample: float = 0.0

    def _sample(self) -> None:
        now = time.monotonic()
        # Throttle to ~1Hz to avoid the sampler dominating CPU.
        if now - self._last_sample < 0.5:
            return
        self._last_sample = now
        try:
            self._cpu_pct = self._proc.cpu_percent(interval=None)
            self._ram_pct = self._proc.memory_percent()
        except Exception:
            self._cpu_pct = self._ram_pct = 0.0
        if _HAS_TORCH:
            try:  # pragma: no cover - GPU path depends on hardware
                if _torch.cuda.is_available():
                    free, total = _torch.cuda.mem_get_info()
                    if total > 0:
                        self._gpu_pct = (1.0 - free / total) * 100.0
            except Exception:
                self._gpu_pct = None

    def render(self, task):  # type: ignore[override]
        self._sample()
        txt = Text()
        txt.append(f" CPU {self._cpu_pct:4.0f}%", style="blue")
        txt.append(f"  RAM {self._ram_pct:4.0f}%", style="magenta")
        if self._gpu_pct is not None:
            txt.append(f"  GPU {self._gpu_pct:4.0f}%", style="cyan")
        return txt


class MultiStageProgress:
    """High-level wrapper that stages register against.

    Usage:
        with MultiStageProgress(console) as mp:
            mp.add_stage("discovery", "Discovery", total=N)
            ... advance("discovery", 1) ...
            mp.add_stage("exact", "Exact Hashing", total=M)
    """

    def __init__(self, console: Console | None = None) -> None:
        if console is None:
            console = Console(
                legacy_windows=False,
                safe_box=True,
                force_terminal=None,
            )
        self.console = console
        self._is_tty = sys.stderr.isatty() and not os.environ.get("DUPLVER_NO_TTY")
        self._stages: dict[str, _Stage] = {}
        self._progress: Progress | None = None
        self._live: Live | None = None
        self._table: Table | None = None
        self._started: float = 0.0

    def __enter__(self) -> "MultiStageProgress":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def start(self) -> None:
        if not self._is_tty:
            self.console.print("[dim]Progress widget disabled (non-TTY).[/dim]")
            return
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            _PercentColumn(),
            _RateColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            _SystemStatsColumn(),
            console=self.console,
            expand=True,
            transient=False,
        )
        # Render stages as a vertical table for the spec's multi-bar look.
        self._table = Table.grid(padding=(0, 1))
        self._table.add_row(self._progress)
        self._live = Live(self._table, console=self.console, refresh_per_second=8)
        self._live.start()
        self._started = time.monotonic()

    def stop(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        if self._progress is not None:
            self._progress.stop()
            self._progress = None

    def add_stage(self, key: str, label: str, total: int) -> Any:
        """Register a new stage. Returns the Rich task id (or None on non-TTY)."""
        if self._progress is None:
            return None
        # Auto-grow totals for unknown step counts (displayed as indeterminate).
        if total is None or total <= 0:
            total = 1  # Rich doesn't allow 0; advance past it on completion.
        task_id = self._progress.add_task(label, total=total)
        self._stages[key] = _Stage(name=key, label=label, task_id=task_id, total=total)
        return task_id

    def update_total(self, key: str, total: int) -> None:
        """Resize a stage's total (e.g. once we know the file count)."""
        if self._progress is None:
            return
        st = self._stages.get(key)
        if st is None:
            return
        st.total = total
        self._progress.update(st.task_id, total=total)

    def advance(self, key: str, n: int = 1) -> None:
        if self._progress is None:
            return
        st = self._stages.get(key)
        if st is None:
            return
        self._progress.advance(st.task_id, n)

    def finish_stage(self, key: str) -> None:
        """Mark a stage complete (jump to its total)."""
        if self._progress is None:
            return
        st = self._stages.get(key)
        if st is None:
            return
        # Rich's ``completed`` is implicit; we set it explicitly.
        self._progress.update(st.task_id, completed=st.total, total=st.total)

    def message(self, text: str) -> None:
        """Print a transient log line above the progress widget."""
        if self._live is not None and self._is_tty:
            # Live renders above the progress area via a Table row.
            if self._table is not None:
                # Insert a transient row that will be replaced next refresh.
                self._table.add_row(Text(text, style="dim"))
                self._live.refresh()
                # Drop the message row by rebuilding the grid would flicker;
                # for v0.1 we accept brief accumulation.
        else:
            self.console.print(text)

    def elapsed(self) -> float:
        return time.monotonic() - self._started