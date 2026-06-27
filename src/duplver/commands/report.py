"""`duplver report` — generate JSON / CSV / HTML reports.

**Status: partially implemented.** JSON export is real (the writer is
in ``duplver.report``); CSV and HTML are stubbed for v0.1.
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel

from duplver import report
from duplver.commands._common import (
    console,
    friendly_error,
    open_db,
    require_path,
    resolve_root,
    validate_choice,
)

FORMAT_CHOICES = ("json", "csv", "html")
CMD = "duplver report"

app = typer.Typer(help="Generate reports.", no_args_is_help=False)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    path: Path = typer.Option(
        None,
        "--path",
        "-p",
        help="Directory previously scanned.",
    ),
    fmt: str = typer.Option("json", "--format", "-f", help="json | csv | html"),
    output: Path = typer.Option(
        None, "--output", "-o", help="Output file (default: cwd)."
    ),
    include: str = typer.Option(
        "all",
        "--include",
        help="What to include in the report: clusters | files | trashed | all.",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    resolved = require_path(path, label="report")
    _run(resolved, fmt=fmt, output=output, include=include)


@friendly_error(CMD)
def _run(path: Path, *, fmt: str, output: Path | None, include: str = "all") -> None:
    root = resolve_root(path)
    _, conn = open_db(root)

    # Validate the format choice up-front so the user gets a friendly error.
    fmt = validate_choice("format", fmt, FORMAT_CHOICES, cmd_label=CMD)
    validate_choice("include", include, ("clusters", "files", "trashed", "all"), cmd_label=CMD)

    if fmt == "json":
        out = output or Path.cwd() / "duplver-report.json"
        report.export_json(conn, out)
        console.print(f"[green]OK[/green] Wrote JSON report to {out}")
    elif fmt == "csv":
        out = output or Path.cwd() / "duplver-report.csv"
        report.export_csv(conn, out)
        console.print(
            Panel(
                "[yellow]CSV export is not yet implemented in v0.1.[/yellow]\n"
                f"Wrote a placeholder file to {out}.",
                title=CMD,
                border_style="yellow",
            )
        )
    elif fmt == "html":
        out = output or Path.cwd() / "duplver-report.html"
        report.export_html(conn, out)
        console.print(
            Panel(
                "[yellow]HTML export is not yet implemented in v0.1.[/yellow]\n"
                f"Wrote a placeholder file to {out}.",
                title=CMD,
                border_style="yellow",
            )
        )