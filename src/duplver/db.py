"""SQLite storage: one database per scanned root, kept in the state dir.

The database is a cache of per-file analysis plus the latest grouping
result and the cleanup audit log. If the schema version changes the file is
simply rebuilt — the next scan re-analyses everything.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from duplver.paths import db_path

SCHEMA_VERSION = "3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT    UNIQUE NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL    NOT NULL,
    seen        INTEGER NOT NULL DEFAULT 0,  -- scan generation that last saw it
    status      TEXT    NOT NULL,            -- 'ok' | 'error'
    error       TEXT,
    sha256      TEXT,                        -- bytes
    pixel_hash  TEXT,                        -- decoded, orientation-corrected pixels
    phash       TEXT,                        -- 64-bit DCT hash (hex)
    dhash       TEXT,                        -- 64-bit gradient hash (hex)
    flat        INTEGER,                     -- 1 = near-uniform image, hashes unreliable
    width       INTEGER,
    height      INTEGER,
    format      TEXT,
    sharpness   REAL,
    tone        TEXT                         -- mean 'R,G,B' (0-255)
);
CREATE INDEX IF NOT EXISTS idx_files_sha   ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_pixel ON files(pixel_hash);
CREATE INDEX IF NOT EXISTS idx_files_seen  ON files(seen);

CREATE TABLE IF NOT EXISTS embeddings (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    model   TEXT NOT NULL,
    vector  BLOB NOT NULL                    -- float32, L2-normalised
);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,            -- weakest relation in the group
    keeper_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    confidence  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id    INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL,            -- 'keep' | 'duplicate'
    relation    TEXT    NOT NULL,            -- how it relates to the keeper
    confidence  REAL    NOT NULL,
    PRIMARY KEY (group_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_members_file ON group_members(file_id);

CREATE TABLE IF NOT EXISTS actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    original_path TEXT    NOT NULL,
    stored_path   TEXT,                      -- quarantine location (NULL for recycle bin)
    method        TEXT    NOT NULL,          -- 'quarantine' | 'recycle'
    at            REAL    NOT NULL,
    restored      INTEGER NOT NULL DEFAULT 0
);
"""


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,  # autocommit; transactions are explicit
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _schema_version(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return row["value"] if row else None


def connect(path: Path) -> sqlite3.Connection:
    """Open the database at ``path``, (re)creating it if the schema is stale."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open(path)
    if _schema_version(conn) == SCHEMA_VERSION:
        return conn

    # Missing, older or corrupt: it's only a cache, so start fresh.
    conn.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(path) + suffix)
        except FileNotFoundError:
            pass
    conn = _open(path)
    conn.executescript(_SCHEMA)
    set_meta(conn, "schema_version", SCHEMA_VERSION)
    return conn


def connect_root(root: Path) -> sqlite3.Connection:
    return connect(db_path(root))


def open_existing(root: Path) -> sqlite3.Connection | None:
    """Open the database for ``root`` only if a scan has created one."""
    path = db_path(root)
    if not path.exists():
        return None
    conn = connect(path)
    if get_meta(conn, "last_scan") is None:
        return None
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
