"""`duplver review` — interactive review of clusters.

Prerequisite: ``duplver scan --path`` must have been run first.

**Status: stubbed in v0.1.** When implemented, this command will present
each cluster in turn and let the user mark which file to keep / trash /
skip, with shortcuts for keyboard navigation.
"""
from __future__ import annotations

from pathlib import Path

import typer

from duplver.commands._common import (
    friendly_error,
    open_db,
    print_not_implemented,
    require_path,
    resolve_root,
)
from duplver.workflow import STAGE_SCAN_DONE, require_stage

app = typer.Typer(help="Interactively review clusters.", no_args_is_help=False)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    path: Path = typer.Option(
        None,
        "--path",
        "-p",
        help="Directory previously scanned.",
    ),
    interactive: bool = typer.Option(True, "--interactive/--no-interactive"),
    auto_keep: str = typer.Option(
        "best",
        "--auto-keep",
        help="Non-interactive default: best | largest | newest | oldest.",
    ),
) -> None:
    """Review duplicate clusters. Requires ``duplver scan`` to have run first."""
    if ctx.invoked_subcommand is not None:
        return
    resolved = require_path(path, label="review")
    _run(resolved, interactive=interactive, auto_keep=auto_keep)


@friendly_error("duplver review")
def _run(path: Path, *, interactive: bool, auto_keep: str) -> None:
    root = resolve_root(path)
    _, conn = open_db(root)

    # ── Gate: scan must have run first ───────────────────────────────────────
    require_stage(
        conn,
        STAGE_SCAN_DONE,
        root,
        cmd="duplver review",
        then_run=f"duplver review --path {root}",
    )

    print_not_implemented(
        "duplver review",
        "Interactive review",
        "Until implemented, use 'duplver stats' to inspect clusters.",
    )
