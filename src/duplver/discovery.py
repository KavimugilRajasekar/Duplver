"""Stage 1 — Discovery.

Recursive walk that collects image files, reads metadata (dimensions, EXIF,
format) via Pillow, and writes the results into the ``files`` table.

Robust to corruption: any unreadable file is recorded with status='failed'
and an error message; the walk continues.

Stage contract:
    Discovery().run(ctx) -> StageResult
        ctx.progress.add_stage("discovery", "Discovery") must already exist,
        but Discovery updates the total once the walk is done if it changes.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from duplver.db import mark_status, upsert_file
from duplver.models import PipelineContext, StageResult
from duplver.utils.images import read_image_meta

# Extensions we accept. The spec lists the canonical set; we keep this list
# authoritative for v0.1. Detection at extension level only — no magic
# bytes sniffing yet.
_IMAGE_EXTS: frozenset[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".avif",
        ".tif",
        ".tiff",
        ".bmp",
        ".gif",
        ".heic",
        ".heif",
    }
)

# Commit batches to avoid per-file WAL fsyncs.
_BATCH_SIZE = 500


class Discovery:
    """Pipeline stage that walks ``ctx.root`` and populates the files table."""

    name = "discovery"

    def run(self, ctx: PipelineContext) -> StageResult:
        root = ctx.root
        ctx_root = str(root)
        count = 0
        errors: list[str] = []

        # Single-pass collection — avoids a second full disk walk just for
        # counting, and removes the race-condition window between the two walks.
        all_paths = list(self._iter_files(root))
        total = len(all_paths)
        ctx.progress.update_total(self.name, total=total or 1)

        conn = ctx.conn
        conn.execute("BEGIN")
        batch_count = 0

        for path in all_paths:
            try:
                st = path.stat()
            except OSError as e:
                errors.append(f"{path}: stat failed: {e}")
                ctx.progress.advance(self.name)
                batch_count += 1
                if batch_count >= _BATCH_SIZE:
                    conn.execute("COMMIT")
                    conn.execute("BEGIN")
                    batch_count = 0
                continue

            meta = read_image_meta(path)
            if meta is None:
                # Not an image we can open. Mark failed but keep the row so
                # the user can audit.
                file_id = upsert_file(
                    conn,
                    path=str(path),
                    root=ctx_root,
                    filename=path.name,
                    extension=path.suffix.lower(),
                    size_bytes=st.st_size,
                    mtime=st.st_mtime,
                    ctime=st.st_ctime,
                    width=None,
                    height=None,
                    fmt=None,
                    mode=None,
                    exif_json=None,
                )
                mark_status(conn, file_id, "failed", error="unreadable or not an image")
                errors.append(f"{path}: unreadable")
                ctx.progress.advance(self.name)
                batch_count += 1
                if batch_count >= _BATCH_SIZE:
                    conn.execute("COMMIT")
                    conn.execute("BEGIN")
                    batch_count = 0
                continue

            file_id = upsert_file(
                conn,
                path=str(path),
                root=ctx_root,
                filename=path.name,
                extension=path.suffix.lower(),
                size_bytes=st.st_size,
                mtime=st.st_mtime,
                ctime=st.st_ctime,
                width=meta["width"],
                height=meta["height"],
                fmt=meta["format"],
                mode=meta["mode"],
                exif_json=meta["exif_json"],
            )
            # Re-mark discovered in case a previous run failed mid-way.
            mark_status(conn, file_id, "discovered")
            count += 1
            ctx.progress.advance(self.name)
            batch_count += 1
            if batch_count >= _BATCH_SIZE:
                conn.execute("COMMIT")
                conn.execute("BEGIN")
                batch_count = 0

        # Commit any remaining writes.
        conn.execute("COMMIT")

        ctx.progress.finish_stage(self.name)
        return StageResult(
            stage=self.name,
            count=count,
            errors=tuple(errors),
            elapsed=0.0,
            extra={"total_seen": total},
        )

    def _iter_files(self, root: Path) -> Iterator[Path]:
        """Yield image files under ``root`` recursively.

        Symlinks are followed (cheap) but loops are guarded against by
        tracking visited directories by their resolved path.
        """
        seen_dirs: set[str] = set()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            resolved = str(Path(dirpath).resolve())
            if resolved in seen_dirs:
                # Avoid infinite loops via symlink cycles.
                dirnames[:] = []
                continue
            seen_dirs.add(resolved)
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix.lower() in _IMAGE_EXTS:
                    yield p