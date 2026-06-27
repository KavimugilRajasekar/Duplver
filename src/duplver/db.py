"""SQLite connection and migration helpers.

Each scanned root has its own database file. Schema is applied once via
``init_db``, and a simple ``schema_version`` row in ``meta`` lets future
versions migrate safely.
"""
from __future__ import annotations

import sqlite3
import time
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = "1"

_SCHEMA_TEXT = (
    resources.files("duplver").joinpath("schema.sql").read_text(encoding="utf-8")
)


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (and initialize) a database for the given root.

    Creates parent directories if needed. Enables foreign keys, WAL mode,
    and sets a busy timeout so concurrent readers don't immediately fail.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        isolation_level=None,  # autocommit; we manage transactions explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """Apply schema.sql if the DB has never been initialized.

    Order matters: a fresh DB has no ``meta`` table, so the schema_version
    check can fail with ``OperationalError``. We try the check first (cheap
    path on already-initialized DBs); on failure we apply the schema, then
    re-check. ``schema.sql`` uses ``CREATE TABLE IF NOT EXISTS`` everywhere,
    so re-applying on a half-initialized DB is safe.
    """
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is not None:
            return
    except sqlite3.OperationalError:
        # ``meta`` doesn't exist yet — apply schema now and insert version rows.
        conn.executescript(_SCHEMA_TEXT)
        conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('created_at', ?)",
        (str(time.time()),),
    )


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def upsert_file(
    conn: sqlite3.Connection,
    *,
    path: str,
    root: str,
    filename: str,
    extension: str,
    size_bytes: int,
    mtime: float,
    ctime: float,
    width: int | None,
    height: int | None,
    fmt: str | None,
    mode: str | None,
    exif_json: str | None,
) -> int:
    """Insert a new files row, or update an existing one if the path is known.

    Returns the row id. A new file starts with status='discovered'.
    """
    existing = conn.execute(
        "SELECT id, size_bytes, mtime FROM files WHERE path = ?", (path,)
    ).fetchone()

    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO files(
                path, root, filename, extension, size_bytes, mtime, ctime,
                width, height, format, mode, exif_json, status, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?)
            """,
            (
                path,
                root,
                filename,
                extension,
                size_bytes,
                mtime,
                ctime,
                width,
                height,
                fmt,
                mode,
                exif_json,
                time.time(),
            ),
        )
        return int(cur.lastrowid)

    # Only update metadata if size or mtime changed. Otherwise keep existing row.
    if existing["size_bytes"] != size_bytes or existing["mtime"] != mtime:
        conn.execute(
            """
            UPDATE files SET
                size_bytes = ?, mtime = ?, ctime = ?, width = ?, height = ?,
                format = ?, mode = ?, exif_json = ?, status = 'discovered',
                error = NULL
            WHERE id = ?
            """,
            (
                size_bytes,
                mtime,
                ctime,
                width,
                height,
                fmt,
                mode,
                exif_json,
                existing["id"],
            ),
        )
    return int(existing["id"])


def mark_status(
    conn: sqlite3.Connection, file_id: int, status: str, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE files SET status = ?, error = ? WHERE id = ?",
        (status, error, file_id),
    )