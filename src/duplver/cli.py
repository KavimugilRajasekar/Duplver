"""Top-level Typer app for Duplver.

Wires every subcommand under one entry point. Shared options
(``--json``, ``--quiet``, ``--no-color``, ``--debug``) live on the root
callback so every subcommand inherits them.
"""
from __future__ import annotations

import sys

import typer

from duplver import __version__
from duplver.commands import (
    analyze,
    benchmark,
    cleanup,
    cluster,
    config_cmd,
    doctor,
    report,
    restore,
    review,
    scan,
    stats,
)
from duplver.commands._common import console, error_panel

# ---------------------------------------------------------------------------
# I/O configuration — must run before Rich touches stdout/stderr.
# ---------------------------------------------------------------------------

def _configure_io() -> None:
    """Force stdout/stderr to UTF-8 so Unicode glyphs survive on Windows.

    Python 3.7+ ``TextIOWrapper.reconfigure`` lets us retarget an already-
    opened stream. On non-Windows it's typically a no-op (already utf-8)
    but it's harmless to try. ``errors="replace"`` keeps the CLI alive
    even when the terminal encoding truly can't represent a glyph.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # e.g. captured by pytest
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # Some test runners hand us a stream that can't be reconfigured.
            pass


_configure_io()


# ---------------------------------------------------------------------------
# Typer app
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="duplver",
    help="Enterprise-grade AI image deduplication.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=False,
)

# Register all subcommand groups.
app.add_typer(scan.app, name="scan")
app.add_typer(analyze.app, name="analyze")
app.add_typer(cluster.app, name="cluster")
app.add_typer(review.app, name="review")
app.add_typer(cleanup.app, name="cleanup")
app.add_typer(restore.app, name="restore")
app.add_typer(report.app, name="report")
app.add_typer(stats.app, name="stats")
app.add_typer(doctor.app, name="doctor")
app.add_typer(benchmark.app, name="benchmark")
app.add_typer(config_cmd.app, name="config")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"duplver [bold cyan]v{__version__}[/bold cyan]")
        raise typer.Exit()


@app.callback()
def _root_callback(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        is_eager=False,
        help="Show full tracebacks on error (otherwise errors print as one-line panels).",
    ),
    no_color: bool = typer.Option(
        False,
        "--no-color",
        is_eager=False,
        help="Disable colored output and Unicode glyphs (use ASCII).",
    ),
) -> None:
    """Duplver — AI image deduplication."""


# ---------------------------------------------------------------------------
# Last-resort error trap
# ---------------------------------------------------------------------------

def _safe_main() -> None:
    """Wrap ``app()`` so an uncaught exception prints a friendly panel.

    Typer catches its own errors (bad args, unknown commands) before we
    get here, so this is only for the cases where something slipped past.
    """
    try:
        app()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except Exception as e:
        error_panel(
            "duplver",
            "Unexpected error.",
            str(e) or type(e).__name__,
            hints=[
                "Re-run with --debug to see the full traceback.",
                "If this persists, please open an issue.",
            ],
        )
        if "--debug" in sys.argv:
            console.print_exception(show_locals=False)
        sys.exit(1)


if __name__ == "__main__":
    _safe_main()
