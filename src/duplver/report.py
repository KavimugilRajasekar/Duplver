"""Reporting — JSON, CSV, HTML writers.

**Status: stubbed for CSV and HTML.** The JSON path is exercised by the
verification flow; CSV and HTML will be filled in once ``cleanup`` is real.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Iterable

from rich.console import Console
from rich.panel import Panel

# Stable column order for cluster rows.
_CLUSTER_COLUMNS: tuple[str, ...] = (
    "cluster_id",
    "kind",
    "detection_method",
    "confidence",
    "best_file",
    "members",
    "savings_bytes",
)


def export_json(conn: sqlite3.Connection, out_path: Path) -> Path:
    """Write a JSON report of all clusters to ``out_path``. Returns the path."""
    rows = conn.execute(
        """
        SELECT c.id AS cluster_id, c.kind, c.detection_method, c.confidence,
               bf.path AS best_file,
               COUNT(cm.file_id) AS members,
               COALESCE(SUM(CASE WHEN cm.file_id != c.best_file_id
                            THEN f.size_bytes ELSE 0 END), 0) AS savings_bytes
        FROM clusters c
        LEFT JOIN files bf ON bf.id = c.best_file_id
        LEFT JOIN cluster_members cm ON cm.cluster_id = c.id
        LEFT JOIN files f ON f.id = cm.file_id
        GROUP BY c.id
        ORDER BY savings_bytes DESC, c.id
        """
    ).fetchall()
    payload = {
        "clusters": [
            {
                "cluster_id": r["cluster_id"],
                "kind": r["kind"],
                "detection_method": r["detection_method"],
                "confidence": r["confidence"],
                "best_file": r["best_file"],
                "members": r["members"],
                "savings_bytes": r["savings_bytes"],
            }
            for r in rows
        ]
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def export_csv(conn: sqlite3.Connection, out_path: Path) -> Path:
    """Stubbed CSV export. Writes a header row and a 'coming soon' notice."""
    out_path.write_text(
        "# duplver report — CSV export coming in the next phase.\n", encoding="utf-8"
    )
    return out_path


def export_html(conn: sqlite3.Connection, out_path: Path) -> Path:
    """Stubbed HTML export. Writes a minimal placeholder document."""
    out_path.write_text(
        "<!doctype html><html><body><h1>duplver report</h1>"
        "<p>HTML export coming in the next phase.</p></body></html>",
        encoding="utf-8",
    )
    return out_path


def report_panel(console: Console, message: str) -> None:
    console.print(
        Panel(
            message,
            title="duplver report",
            border_style="yellow",
        )
    )