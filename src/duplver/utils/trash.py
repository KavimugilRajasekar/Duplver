"""Thin wrapper around send2trash + audit-trail helpers.

The actual deletion routing goes through ``duplver.safety.trash_paths``;
this module adds per-platform helpers and restore-list queries.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path


def platform_tag() -> str:
    """Short identifier for the current OS — used in the trashed table."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def record_trashed(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    original_path: str,
) -> None:
    """Insert a row in the trashed audit table."""
    conn.execute(
        "INSERT INTO trashed(file_id, original_path, trashed_at, platform) "
        "VALUES (?, ?, ?, ?)",
        (file_id, original_path, time.time(), platform_tag()),
    )


def list_trashed(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all trashed-but-not-restored rows, newest first."""
    return list(
        conn.execute(
            "SELECT * FROM trashed WHERE restored = 0 ORDER BY trashed_at DESC"
        )
    )