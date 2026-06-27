"""`duplver cluster` — rebuild clusters from the existing DB.

Prerequisite: ``duplver scan --path`` must have been run first.
"""
from __future__ import annotations

from pathlib import Path

import typer

from duplver import db
from duplver.clustering import Clusterer
from duplver.commands._common import (
    SilentConsole,
    console,
    friendly_error,
    open_db,
    require_path,
    resolve_root,
)
from duplver.config import load_settings
from duplver.models import PipelineContext
from duplver.paths import root_paths
from duplver.progress import MultiStageProgress
from duplver.workflow import STAGE_CLUSTER_DONE, STAGE_SCAN_DONE, record_stage, require_stage

app = typer.Typer(help="Rebuild duplicate clusters from the database.", no_args_is_help=False)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    path: Path = typer.Option(
        None,
        "--path",
        "-p",
        help="Directory previously scanned.",
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Rebuild clusters. Requires ``duplver scan`` to have run first."""
    if ctx.invoked_subcommand is not None:
        return
    resolved = require_path(path, label="cluster")
    _run(resolved, quiet=quiet)


@friendly_error("duplver cluster")
def _run(path: Path, *, quiet: bool) -> None:
    root = resolve_root(path)
    settings = load_settings()
    db_path, conn = open_db(root)

    # ── Gate: scan must have run first ───────────────────────────────────────
    require_stage(
        conn,
        STAGE_SCAN_DONE,
        root,
        cmd="duplver cluster",
        then_run=f"duplver cluster --path {root}",
    )

    progress_console = console if not quiet else SilentConsole()
    with MultiStageProgress(progress_console) as progress:
        progress.add_stage("clustering", "Clustering", total=1)
        pctx = PipelineContext(
            root=root,
            db_path=db_path,
            faiss_path=root_paths(root)["faiss"],
            settings=settings,
            progress=progress,
            conn=conn,
        )
        result = Clusterer().run(pctx)
        record_stage(conn, STAGE_CLUSTER_DONE)
        if not quiet:
            console.print(
                f"[green]OK[/green] Rebuilt [bold]{result.count}[/bold] exact clusters."
            )
