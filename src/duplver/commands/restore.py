"""`duplver restore` — restore trashed files.

Prerequisite: ``duplver cleanup --trash`` must have been run first.

**Status: stubbed in v0.1.** When implemented, this will iterate the
``trashed`` audit table and attempt to move each entry back to its
``original_path`` (best-effort; OS Trash has its own retention rules).
"""
from __future__ import annotations

from pathlib import Path

import typer

from duplver.commands._common import (
    console,
    open_db,
    print_not_implemented,
    require_path,
    resolve_root,
)
from duplver.workflow import STAGE_CLEANUP_DONE, require_stage

app = typer.Typer(help="Restore previously trashed files.", no_args_is_help=False)


@app.callback(invoke_without_command=True)
def _main_callback(
    ctx: typer.Context,
    path: Path = typer.Option(
        None,
        "--path",
        "-p",
        help="Directory whose cleanup you want to reverse.",
    ),
) -> None:
    """Restore trashed files. Requires ``duplver cleanup`` to have run first."""
    if ctx.invoked_subcommand is not None:
        return
    if path is None:
        console.print(
            "[dim]Use 'duplver restore list --path <dir>' to see trashed files.[/dim]"
        )
        return
    resolved = require_path(path, label="restore")
    _run(resolved)


def _run(path: Path) -> None:
    root = resolve_root(path)
    _, conn = open_db(root)

    # ── Gate: cleanup must have run first ────────────────────────────────────
    require_stage(
        conn,
        STAGE_CLEANUP_DONE,
        root,
        cmd="duplver restore",
        then_run=f"duplver restore --path {root}",
    )

    print_not_implemented(
        "duplver restore",
        "Restore",
        "The trashed audit table is already populated by cleanup runs.",
    )


@app.command(name="list")
def list_trashed(
    path: Path = typer.Option(
        None,
        "--path",
        "-p",
        help="Directory whose cleanup you want to inspect.",
    ),
) -> None:
    """List all files that Duplver has sent to the Trash."""
    if path is None:
        console.print("[dim]Pass --path to list trashed files for a specific directory.[/dim]")
        return
    resolved = require_path(path, label="restore list")
    root = resolve_root(resolved)
    _, conn = open_db(root)

    # ── Gate: cleanup must have run first ────────────────────────────────────
    require_stage(
        conn,
        STAGE_CLEANUP_DONE,
        root,
        cmd="duplver restore list",
        then_run=f"duplver restore list --path {root}",
    )

    print_not_implemented(
        "duplver restore list",
        "Restore listing",
        "The trashed audit table is already populated by future cleanup runs.",
    )