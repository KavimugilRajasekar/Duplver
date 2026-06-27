"""`duplver stats` — show database statistics for a scanned root."""
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.table import Table

from duplver.commands._common import (
    console,
    friendly_error,
    human_bytes,
    open_db,
    require_path,
    resolve_root,
)
from duplver.workflow import STAGE_SCAN_DONE, require_stage

app = typer.Typer(help="Show database statistics.", no_args_is_help=False)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    path: Path = typer.Option(
        None,
        "--path",
        "-p",
        help="Directory previously scanned.",
    ),
    json_output: bool = typer.Option(False, "--json"),
    top: int = typer.Option(
        0,
        "--top",
        min=0,
        help="Show the N largest duplicate groups (0 = omit).",
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    resolved = require_path(path, label="stats")
    _run(resolved, json_output=json_output, top=top)


@friendly_error("duplver stats")
def _run(path: Path, *, json_output: bool, top: int = 0) -> None:
    root = resolve_root(path)
    _, conn = open_db(root)

    # ── Gate: scan must have run first ───────────────────────────────────────
    require_stage(
        conn,
        STAGE_SCAN_DONE,
        root,
        cmd="duplver stats",
        then_run=f"duplver stats --path {root}",
    )
    files = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE status != 'failed'"
    ).fetchone()["n"]
    failed = conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE status = 'failed'"
    ).fetchone()["n"]
    hashed = conn.execute(
        "SELECT COUNT(*) AS n FROM exact_hashes"
    ).fetchone()["n"]
    clusters = conn.execute("SELECT COUNT(*) AS n FROM clusters").fetchone()["n"]
    exact_clusters = conn.execute(
        "SELECT COUNT(*) AS n FROM clusters WHERE kind = 'exact'"
    ).fetchone()["n"]
    duplicates = conn.execute(
        "SELECT COUNT(*) AS n FROM cluster_members WHERE role = 'duplicate'"
    ).fetchone()["n"]
    savings = conn.execute(
        "SELECT COALESCE(SUM(f.size_bytes), 0) AS s "
        "FROM cluster_members cm JOIN files f ON f.id = cm.file_id "
        "WHERE cm.role = 'duplicate'"
    ).fetchone()["s"]
    total_size = conn.execute(
        "SELECT COALESCE(SUM(size_bytes), 0) AS s FROM files"
    ).fetchone()["s"]

    payload = {
        "files": files,
        "failed": failed,
        "hashed": hashed,
        "clusters": clusters,
        "exact_clusters": exact_clusters,
        "duplicates": duplicates,
        "savings_bytes": int(savings),
        "total_size_bytes": int(total_size),
    }

    if top > 0:
        top_rows = conn.execute(
            """
            SELECT c.id AS cluster_id, c.kind, bf.path AS best_file,
                   COUNT(cm.file_id) AS members,
                   COALESCE(SUM(CASE WHEN cm.file_id != c.best_file_id
                                THEN f.size_bytes ELSE 0 END), 0) AS savings_bytes
            FROM clusters c
            LEFT JOIN files bf ON bf.id = c.best_file_id
            LEFT JOIN cluster_members cm ON cm.cluster_id = c.id
            LEFT JOIN files f ON f.id = cm.file_id
            GROUP BY c.id
            ORDER BY savings_bytes DESC, c.id
            LIMIT ?
            """,
            (top,),
        ).fetchall()
        payload["top_groups"] = [dict(r) for r in top_rows]

    if json_output:
        print(json.dumps(payload, indent=2))
        return

    table = Table(title=f"Duplver Stats: {root}", show_header=True, header_style="bold blue")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Files discovered", f"{files:,}")
    table.add_row("Files failed", f"{failed:,}")
    table.add_row("Files hashed (SHA-256)", f"{hashed:,}")
    table.add_row("Total clusters", f"{clusters:,}")
    table.add_row("  exact", f"{exact_clusters:,}")
    table.add_row("  near / crop / similar", f"{clusters - exact_clusters:,}")
    table.add_row("Duplicate files", f"{duplicates:,}")
    table.add_row("Potential savings", human_bytes(int(savings)))
    table.add_row("Total size scanned", human_bytes(int(total_size)))
    console.print(table)

    if top > 0:
        top_table = Table(
            title=f"Top {top} duplicate groups by potential savings",
            show_header=True,
            header_style="bold blue",
        )
        top_table.add_column("#", justify="right")
        top_table.add_column("Cluster", justify="right")
        top_table.add_column("Kind")
        top_table.add_column("Members", justify="right")
        top_table.add_column("Savings", justify="right")
        top_table.add_column("Best file")
        for i, r in enumerate(payload["top_groups"], start=1):
            top_table.add_row(
                str(i),
                str(r["cluster_id"]),
                r["kind"],
                f"{r['members']:,}",
                human_bytes(int(r["savings_bytes"])),
                r["best_file"] or "—",
            )
        console.print(top_table)
