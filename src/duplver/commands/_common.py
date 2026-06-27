"""Shared helpers for CLI subcommands.

Things every command needs: a way to resolve the scan root, open the DB,
print a friendly error if the DB doesn't exist yet, emit the standard
summary screen at the end of an operation, and convert raw exceptions
into friendly Rich panels instead of stack traces.
"""
from __future__ import annotations

import functools
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, ParamSpec, TypeVar

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from duplver import db
from duplver.paths import ensure_state_dir, root_paths

# A single shared Console configured to work in both terminal and
# redirected-output contexts. ``legacy_windows=False`` is required because
# Rich's legacy path can't encode Unicode box-drawing characters to cp1252.
# ``safe_box=True`` falls back to ASCII when the terminal truly can't do
# Unicode (e.g. an old Windows cmd.exe redirected to a file).
console = Console(
    legacy_windows=False,
    safe_box=True,
    force_terminal=None,  # auto-detect TTY
)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_root(path: Path) -> Path:
    """Resolve a user-supplied path to an absolute, existing directory."""
    p = path.expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Path does not exist: {p}")
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {p}")
    return p


def require_path(path: Path | None, *, label: str = "PATH") -> Path:
    """Return ``path`` or exit 2 with a friendly panel if it's missing.

    Used by every subcommand that needs a directory. We previously raised
    ``typer.Exit(code=2)`` from each callback manually — this centralizes
    the user-facing message.
    """
    if path is None:
        console.print(
            Panel(
                f"[red]Missing --path / -p argument.[/red]\n\n"
                f"Example:  [bold]duplver {label} --path C:\\\\Photos[/bold]",
                title="duplver",
                border_style="red",
            )
        )
        raise typer.Exit(code=2)
    return path


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------

def open_db(path: Path) -> tuple[Path, sqlite3.Connection]:
    """Open the per-root DB. Returns (db_path, connection).

    Raises ``typer.Exit(2)`` with a friendly message if no scan exists yet.
    """
    ensure_state_dir()
    rp = root_paths(path)
    if not rp["db"].exists():
        console.print(
            Panel(
                f"[yellow]No scan database found for[/yellow] [cyan]{path}[/cyan]\n\n"
                "Run [bold]duplver scan --path[/bold] first to discover files.",
                title="duplver",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=2)
    return rp["db"], db.connect(rp["db"])


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def human_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:3.1f} {unit}"
        n /= 1024.0
    return f"{n:3.1f} PB"


def summary_screen(
    *,
    title: str,
    scanned: int,
    exact_groups: int,
    near: int,
    cropped: int,
    clusters: int,
    duplicates: int,
    savings_bytes: int,
    avg_confidence: float | None,
    elapsed: float,
) -> None:
    """Render the spec's summary panel after every operation.

    ``exact_groups`` is the count of distinct SHA-256 groups with 2+
    members (i.e. how many duplicate *sets* were found). ``duplicates`` is
    the total count of *non-best* files across those groups — the actual
    number of files that cleanup would remove.
    """
    elapsed_str = _fmt_duration(elapsed)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Images Scanned:", f"{scanned:,}")
    grid.add_row("Exact Duplicates (groups):", f"{exact_groups:,}")
    grid.add_row("Duplicate files:", f"{duplicates:,}")
    grid.add_row("Near Duplicates:", f"{near:,}")
    grid.add_row("Cropped Duplicates:", f"{cropped:,}")
    grid.add_row("Total Clusters:", f"{clusters:,}")
    grid.add_row("Potential Savings:", human_bytes(savings_bytes))
    if avg_confidence is not None:
        grid.add_row("Average Confidence:", f"{avg_confidence * 100:.1f}%")
    grid.add_row("Elapsed Time:", elapsed_str)

    console.print(
        Panel(
            grid,
            title=f"[bold cyan]{title}[/bold cyan]",
            border_style="cyan",
            expand=False,
        )
    )


def _fmt_duration(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def print_not_implemented(
    panel_title: str,
    feature: str,
    workaround: str,
) -> None:
    """Standard 'honest stub' panel used by not-yet-implemented commands."""
    console.print(
        Panel(
            f"[yellow]{feature} is not yet implemented in v0.1.[/yellow]\n\n"
            f"[dim]{workaround}[/dim]",
            title=panel_title,
            border_style="yellow",
        )
    )


# ---------------------------------------------------------------------------
# Friendly error handling
# ---------------------------------------------------------------------------

# A small set of "what to do next" hints, keyed by exception class.
_HINTS: dict[type, list[str]] = {
    FileNotFoundError: [
        "Check that the path is correct and try again.",
        "Use 'duplver doctor' to verify permissions.",
        "On Windows, also check the path isn't on a disconnected drive.",
    ],
    NotADirectoryError: [
        "The path exists but is a file, not a folder.",
        "Pass the directory that contains your images.",
    ],
    PermissionError: [
        "Run as administrator (Windows) or with sudo (Linux/macOS).",
        "Check that the folder is readable by your user account.",
    ],
    sqlite3.OperationalError: [
        "The state directory may be on a full or read-only disk.",
        "If the DB is corrupted, delete ~/.duplver/<hash>.db and re-scan.",
        "Use --no-cache to force a clean rebuild.",
    ],
    OSError: [
        "The OS refused the I/O operation.",
        "Run 'duplver doctor' to verify disk and permissions.",
    ],
}


def error_panel(
    cmd_label: str,
    headline: str,
    detail: str,
    *,
    color: str = "red",
    hints: list[str] | None = None,
) -> None:
    """Render a single friendly error panel.

    The intent is: *one* short headline, *one* short detail line, then a
    small bulleted list of next steps. No Python traceback — those are
    reserved for ``--debug``.
    """
    body = f"[{color}]{headline}[/{color}]\n\n[dim]{detail}[/dim]"
    if hints:
        body += "\n\n" + "\n".join(f"  → {h}" for h in hints)
    console.print(
        Panel(
            body,
            title=f"[{color}]{cmd_label}[/{color}]",
            border_style=color,
        )
    )


P = ParamSpec("P")
R = TypeVar("R")


def friendly_error(cmd_label: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap a command's ``_run`` so raw exceptions become friendly panels.

    Mapping (caught → exit code):
        typer.Exit                → re-raised unchanged (Typer contract)
        FileNotFoundError         → 2  (usage-ish)
        NotADirectoryError        → 2
        PermissionError           → 2
        OSError                   → 2
        sqlite3.OperationalError  → 4  (state-dir / DB issue)
        KeyboardInterrupt         → 130 (POSIX convention)
        anything else             → 1  (with hint to re-run with --debug)
    """

    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return fn(*args, **kwargs)
            except typer.Exit:
                raise
            except KeyboardInterrupt:
                error_panel(
                    cmd_label,
                    "Interrupted.",
                    "Partial results were kept in the state directory.",
                    color="yellow",
                )
                raise typer.Exit(code=130)
            except (
                FileNotFoundError,
                NotADirectoryError,
                PermissionError,
                OSError,
            ) as e:
                error_panel(
                    cmd_label,
                    _heading_for(e),
                    str(e) or _heading_for(e),
                    hints=_HINTS.get(type(e)),
                )
                raise typer.Exit(code=2)
            except sqlite3.OperationalError as e:
                error_panel(
                    cmd_label,
                    "Database error.",
                    str(e),
                    hints=_HINTS.get(sqlite3.OperationalError),
                )
                raise typer.Exit(code=4)
            except Exception as e:  # last resort — never leak traceback
                error_panel(
                    cmd_label,
                    "Unexpected error.",
                    str(e) or type(e).__name__,
                    hints=[
                        "Re-run with --debug to see the full traceback.",
                        "If this persists, please open an issue.",
                    ],
                )
                if "--debug" in sys.argv:
                    console.print_exception(show_locals=False)
                raise typer.Exit(code=1)

        return wrapper

    return deco


def _heading_for(e: BaseException) -> str:
    """Pick a short, human headline based on the exception type."""
    if isinstance(e, FileNotFoundError):
        return "That path does not exist."
    if isinstance(e, NotADirectoryError):
        return "That path is not a directory."
    if isinstance(e, PermissionError):
        return "Permission denied."
    if isinstance(e, OSError):
        return "I/O error."
    return type(e).__name__


# ---------------------------------------------------------------------------
# Flag validation
# ---------------------------------------------------------------------------

def validate_choice(
    name: str,
    value: str,
    allowed: Iterable[str],
    *,
    cmd_label: str,
) -> str:
    """Validate a free-form string flag. Exits 2 with a panel on miss."""
    allowed_list = list(allowed)
    if value in allowed_list:
        return value
    error_panel(
        cmd_label,
        f"Invalid value for --{name}: [bold]{value}[/bold]",
        "Allowed values: " + " | ".join(allowed_list),
    )
    raise typer.Exit(code=2)


def mutex_flags(
    cmd_label: str,
    *,
    conflicts: tuple[tuple[str, bool], ...],
    message: str,
) -> None:
    """Enforce mutual exclusion between flags. Exits 2 on violation."""
    active = [name for name, on in conflicts if on]
    if len(active) <= 1:
        return
    error_panel(
        cmd_label,
        "Conflicting flags.",
        f"Flags {active!r} cannot be used together. {message}",
    )
    raise typer.Exit(code=2)


def require_flags(
    cmd_label: str,
    *,
    requires: tuple[tuple[str, bool], ...],
    message: str,
) -> None:
    """Enforce that some flag implies another. Exits 2 on violation."""
    for name, on in requires:
        if on:
            return
    names = [name for name, _ in requires]
    error_panel(
        cmd_label,
        "Missing required flag.",
        f"None of {names!r} were given. {message}",
    )
    raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# Silent progress console (used by --quiet)
# ---------------------------------------------------------------------------

class SilentConsole:
    """Drop-in for Rich Console that swallows Live/Progress output.

    Used by ``--quiet`` so non-interactive runs (CI, scripts) get plain
    results without the Rich widgets. One definition, used everywhere.
    """

    def print(self, *args: Any, **kwargs: Any) -> None:
        return None

    def log(self, *args: Any, **kwargs: Any) -> None:
        return None

    def rule(self, *args: Any, **kwargs: Any) -> None:
        return None


def is_no_color() -> bool:
    """Honor --no-color and the NO_COLOR env-var convention."""
    if os.environ.get("NO_COLOR"):
        return True
    if "--no-color" in sys.argv:
        return True
    return False
