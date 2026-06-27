"""Pipeline workflow state machine.

Records completed pipeline stages as timestamps in the DB ``meta`` table,
and enforces prerequisite gates before each command runs.

Stage keys
----------
``stage:scan_done``     — written by ``duplver scan`` on success
``stage:analyze_done``  — written by ``duplver analyze`` on success
``stage:cluster_done``  — written by ``duplver cluster`` on success
``stage:cleanup_done``  — written by ``duplver cleanup`` on success

Gate contract
-------------
``require_stage(conn, key, path, *, cmd)`` exits 2 with a friendly panel if
the key is not in ``meta``. Every gated command calls this before doing work.
``record_stage(conn, key)`` writes / updates the key on success.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.panel import Panel
from rich.text import Text

from duplver.commands._common import console

# ── Stage key constants ──────────────────────────────────────────────────────

STAGE_SCAN_DONE    = "stage:scan_done"
STAGE_ANALYZE_DONE = "stage:analyze_done"
STAGE_CLUSTER_DONE = "stage:cluster_done"
STAGE_CLEANUP_DONE = "stage:cleanup_done"

# ── Human-readable label + "what to run next" per gate ──────────────────────

_GATE_META: dict[str, dict] = {
    STAGE_SCAN_DONE: {
        "label": "No scan has been run for this directory yet.",
        "run_first": "duplver scan --path {path}",
        "hint": "scan discovers your images and detects exact + perceptual duplicates.",
    },
    STAGE_ANALYZE_DONE: {
        "label": "Full AI analysis has not been run for this directory.",
        "run_first": "duplver analyze --path {path}",
        "hint": "analyze adds CLIP embeddings, crop detection, and feature matching on top of scan.",
    },
    STAGE_CLEANUP_DONE: {
        "label": "No cleanup has been run yet — nothing to restore.",
        "run_first": "duplver cleanup --path {path} --trash",
        "hint": "cleanup moves duplicate files to the OS Trash and logs every action.",
    },
}


# ── Public API ───────────────────────────────────────────────────────────────

def record_stage(conn: sqlite3.Connection, key: str) -> None:
    """Stamp ``key`` with an ISO-8601 UTC timestamp in the ``meta`` table."""
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, ts),
    )


def require_stage(
    conn: sqlite3.Connection,
    key: str,
    path: Path,
    *,
    cmd: str,
    then_run: str | None = None,
) -> None:
    """Exit 2 with a friendly panel if ``key`` is not recorded in ``meta``.

    Parameters
    ----------
    conn:     Open DB connection for the root.
    key:      Stage key to check (one of the ``STAGE_*`` constants).
    path:     The scanned root path (used to build the suggested command).
    cmd:      The command the user tried to run (for the panel title).
    then_run: Optional explicit "then run this" suggestion to override default.
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        row = None

    if row is not None:
        return  # gate passed

    meta = _GATE_META.get(key, {
        "label": f"Prerequisite stage '{key}' has not been completed.",
        "run_first": "duplver scan --path {path}",
        "hint": "",
    })

    path_str = str(path)
    run_first = meta["run_first"].format(path=path_str)
    hint      = meta["hint"]
    label     = meta["label"]

    body = Text()
    body.append("[FAIL]  ", style="bold red")
    body.append(label + "\n\n", style="red")
    body.append("Run this first:\n", style="bold")
    body.append(f"  -> {run_first}\n", style="bold cyan")
    if then_run:
        body.append("\nThen you can run:\n", style="bold")
        body.append(f"  -> {then_run}\n", style="dim cyan")
    if hint:
        body.append(f"\n{hint}", style="dim")

    console.print(
        Panel(
            body,
            title=f"[red]{cmd}[/red]",
            border_style="red",
            expand=False,
        )
    )
    raise typer.Exit(code=2)


def pipeline_status(conn: sqlite3.Connection) -> dict[str, str | None]:
    """Return a dict of all known stage keys → timestamp (or None if not done)."""
    keys = [STAGE_SCAN_DONE, STAGE_ANALYZE_DONE, STAGE_CLUSTER_DONE, STAGE_CLEANUP_DONE]
    out: dict[str, str | None] = {}
    for k in keys:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (k,)).fetchone()
            out[k] = row["value"] if row else None
        except sqlite3.OperationalError:
            out[k] = None
    return out
