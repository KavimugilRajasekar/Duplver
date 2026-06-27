"""`duplver cleanup` — move duplicates to the OS Trash.

Prerequisite: ``duplver scan --path`` must have been run first.

**Status: stubbed in v0.1.** The safety gates (dry-run default, double
confirmation, audit trail) are already implemented in ``duplver.safety``
and used by future iterations of this command.

When implemented, this command will:
* Show a dry-run preview of what would be trashed.
* Require an explicit ``--trash`` or ``--permanent-delete`` flag.
* Route to ``safety.trash_paths`` (defaulting to dry-run).
* Log every action in the ``trashed`` audit table.
"""
from __future__ import annotations

from pathlib import Path

import typer

from duplver.commands._common import (
    console,
    error_panel,
    friendly_error,
    mutex_flags,
    open_db,
    print_not_implemented,
    require_flags,
    require_path,
    resolve_root,
    validate_choice,
)
from duplver.safety import DEFAULT_DRY_RUN
from duplver.workflow import STAGE_CLEANUP_DONE, STAGE_SCAN_DONE, record_stage, require_stage

KEEP_CHOICES = ("best", "largest", "newest", "oldest")
CMD = "duplver cleanup"

app = typer.Typer(help="Move duplicates to Trash.", no_args_is_help=False)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    path: Path = typer.Option(
        None,
        "--path",
        "-p",
        help="Directory previously scanned.",
    ),
    trash: bool = typer.Option(False, "--trash", help="Send to OS Trash."),
    permanent_delete: bool = typer.Option(
        False, "--permanent-delete", help="Delete permanently (DANGEROUS)."
    ),
    dry_run: bool = typer.Option(
        DEFAULT_DRY_RUN, "--dry-run/--no-dry-run", help="Preview only."
    ),
    keep: str = typer.Option(
        "best",
        "--keep",
        help="Which file to keep: best | largest | newest | oldest.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt (still implies --trash, never --permanent-delete).",
    ),
) -> None:
    """Move duplicate files to Trash. Requires ``duplver scan`` to have run first."""
    if ctx.invoked_subcommand is not None:
        return
    resolved = require_path(path, label="cleanup")
    _run(
        resolved,
        trash=trash,
        permanent_delete=permanent_delete,
        dry_run=dry_run,
        keep=keep,
        yes=yes,
    )


@friendly_error(CMD)
def _run(
    path: Path,
    *,
    trash: bool,
    permanent_delete: bool,
    dry_run: bool,
    keep: str,
    yes: bool,
) -> None:
    root = resolve_root(path)
    _, conn = open_db(root)

    # ── Gate: scan must have run first ───────────────────────────────────────
    require_stage(
        conn,
        STAGE_SCAN_DONE,
        root,
        cmd=CMD,
        then_run=f"duplver cleanup --path {root} --trash",
    )

    # Flag validation — done early so the user doesn't see a traceback.
    validate_choice("keep", keep, KEEP_CHOICES, cmd_label=CMD)

    mutex_flags(
        CMD,
        conflicts=(("--trash", trash), ("--permanent-delete", permanent_delete)),
        message="Pick exactly one action.",
    )

    if permanent_delete:
        require_flags(
            CMD,
            requires=(("--yes", yes),),
            message="--permanent-delete requires --yes to confirm.",
        )

    if permanent_delete and dry_run:
        error_panel(
            CMD,
            "--permanent-delete cannot be combined with --dry-run.",
            "Either pass --no-dry-run or remove --permanent-delete.",
        )
        raise typer.Exit(code=2)

    if not (trash or permanent_delete):
        console.print(
            "[dim]No action flag provided. Defaulting to dry-run preview.[/dim]"
        )

    print_not_implemented(
        CMD,
        "Cleanup",
        "The audit-trail table and safety gate exist; the routing logic is "
        "coming in v0.2. Use 'duplver stats' to see what would be cleaned up.",
    )

    # When cleanup is implemented it should call record_stage(conn, STAGE_CLEANUP_DONE)
    # after successfully trashing files so restore becomes available.
    # record_stage(conn, STAGE_CLEANUP_DONE)