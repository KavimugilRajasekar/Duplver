"""`duplver scan` — discover files, hash them, cluster exact duplicates."""
from __future__ import annotations

import json
from pathlib import Path

import typer

from duplver import db
from duplver.clustering import Clusterer
from duplver.commands._common import (
    SilentConsole,
    console,
    friendly_error,
    require_path,
    resolve_root,
    summary_screen,
)
from duplver.config import load_settings
from duplver.discovery import Discovery
from duplver.exact import ExactHasher
from duplver.models import PipelineContext
from duplver.paths import ensure_state_dir, root_paths
from duplver.perceptual import PerceptualHasher
from duplver.progress import MultiStageProgress
from duplver.workflow import STAGE_SCAN_DONE, record_stage

app = typer.Typer(help="Discover files and detect exact duplicates.", no_args_is_help=True)


@app.callback(invoke_without_command=True)
def _main_callback(
    ctx: typer.Context,
    path: Path = typer.Option(
        None,
        "--path",
        "-p",
        help="Directory to scan recursively.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print JSON summary."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress progress UI."),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Force re-hash of all files (slower)."
    ),
    follow_symlinks: bool = typer.Option(
        True, "--follow-symlinks/--no-follow-symlinks", help="Follow symlinks during walk."
    ),
) -> None:
    """When invoked without an explicit `run` subcommand, run the scan."""
    if ctx.invoked_subcommand is not None:
        return
    resolved = require_path(path, label="scan")
    _run_scan(
        resolved,
        quiet=quiet,
        no_cache=no_cache,
        json_output=json_output,
        follow_symlinks=follow_symlinks,
    )


@friendly_error("duplver scan")
def _run_scan(
    path: Path,
    *,
    quiet: bool,
    no_cache: bool,
    json_output: bool,
    follow_symlinks: bool = True,
) -> None:
    """Walk ``PATH``, hash every image, and cluster exact duplicates."""
    # ``require_path`` rejects a missing PATH; ``resolve_root`` rejects a
    # path that exists but is not a directory (and raises the specific
    # exception class our friendly_error decorator maps to a red panel).
    root = resolve_root(require_path(path, label="scan"))
    settings = load_settings()
    rp = root_paths(root)
    ensure_state_dir()
    conn = db.connect(rp["db"])

    # If --no-cache, drop existing hashes so everything is reprocessed.
    if no_cache:
        conn.execute("DELETE FROM exact_hashes")
        conn.execute(
            "UPDATE files SET status = 'discovered' WHERE status = 'hashed'"
        )

    progress_console = console if not quiet else SilentConsole()
    progress = MultiStageProgress(progress_console)
    with progress:
        progress.add_stage("discovery",  "Discovery",          total=1)
        progress.add_stage("exact",      "Exact Hashing",      total=1)
        progress.add_stage("perceptual", "Perceptual Hashing", total=1)
        progress.add_stage("clustering", "Clustering",         total=1)

        pctx = PipelineContext(
            root=root,
            db_path=rp["db"],
            faiss_path=rp["faiss"],
            settings=settings,
            progress=progress,
            conn=conn,
        )

        Discovery().run(pctx)
        ExactHasher().run(pctx)
        PerceptualHasher().run(pctx)
        Clusterer().run(pctx)

        # Stamp the workflow gate so downstream commands know scan has run.
        record_stage(conn, STAGE_SCAN_DONE)

        _emit_summary(pctx, "Duplver Scan Complete", json_output=json_output)


def _emit_summary(ctx: PipelineContext, title: str, *, json_output: bool) -> None:
    """Aggregate the run stats and emit either JSON or the spec summary panel.

    Note on labels: ``exact_hashes`` is the count of *files* with a hash.
    We translate it into the two numbers the user actually wants:
        - ``exact_groups`` — distinct SHA-256s shared by 2+ files
        - ``duplicates``    — files that are not the best in their group
    """
    conn = ctx.conn
    scanned = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE status != 'failed'"
    ).fetchone()["n"]
    exact_groups = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM (
            SELECT eh.sha256
            FROM exact_hashes eh
            JOIN files f ON f.id = eh.file_id
            GROUP BY eh.sha256
            HAVING COUNT(*) >= 2
        )
        """
    ).fetchone()["n"]
    duplicates = conn.execute(
        "SELECT COUNT(*) AS n FROM cluster_members WHERE role = 'duplicate'"
    ).fetchone()["n"]
    near = conn.execute(
        "SELECT COUNT(*) AS n FROM clusters WHERE kind = 'perceptual'"
    ).fetchone()["n"]
    cropped = conn.execute(
        "SELECT COUNT(*) AS n FROM clusters WHERE kind = 'crop'"
    ).fetchone()["n"]
    cluster_rows = conn.execute("SELECT id, kind, confidence FROM clusters").fetchall()
    clusters = len(cluster_rows)
    savings = conn.execute(
        """
        SELECT COALESCE(SUM(f.size_bytes), 0) AS s
        FROM cluster_members cm
        JOIN files f ON f.id = cm.file_id
        JOIN clusters c ON c.id = cm.cluster_id
        WHERE cm.role = 'duplicate'
        """
    ).fetchone()["s"]
    avg = (
        sum(r["confidence"] for r in cluster_rows) / clusters if clusters else None
    )
    elapsed = ctx.progress.elapsed()

    if json_output:
        print(
            json.dumps(
                {
                    "scanned": scanned,
                    "exact_groups": exact_groups,
                    "duplicates": duplicates,
                    "near": near,
                    "cropped": cropped,
                    "clusters": clusters,
                    "savings_bytes": int(savings),
                    "avg_confidence": avg,
                    "elapsed_seconds": elapsed,
                },
                indent=2,
            )
        )
    else:
        summary_screen(
            title=title,
            scanned=scanned,
            exact_groups=exact_groups,
            near=near,
            cropped=cropped,
            clusters=clusters,
            duplicates=duplicates,
            savings_bytes=int(savings),
            avg_confidence=avg,
            elapsed=elapsed,
        )
