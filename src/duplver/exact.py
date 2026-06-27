"""Stage 2 — Exact (SHA-256) duplicate detection.

Reads each known file from disk and computes its SHA-256. Files whose bytes
are identical (and size matches) share a hash and are clustered together.

Incremental: only files that don't yet have an ``exact_hashes`` row, or whose
size/mtime changed since the previous run, are re-hashed.

Stage contract:
    ExactHasher().run(ctx) -> StageResult
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from duplver.db import mark_status
from duplver.models import PipelineContext, StageResult
from duplver.utils.hashing import sha256_file

# Commit a batch of this size to avoid one fsync per file.
_BATCH_SIZE = 200


class ExactHasher:
    name = "exact"

    def run(self, ctx: PipelineContext) -> StageResult:
        conn = ctx.conn
        # Files needing a hash: those with no row in exact_hashes, or whose
        # current size differs from the cached value.
        rows = conn.execute(
            """
            SELECT f.id, f.path, f.size_bytes, f.mtime
            FROM files f
            LEFT JOIN exact_hashes eh ON eh.file_id = f.id
            WHERE f.status = 'discovered'
              AND (
                eh.file_id IS NULL
                OR eh.size_bytes != f.size_bytes
              )
            """
        ).fetchall()

        total = len(rows)
        ctx.progress.update_total(self.name, total=total or 1)

        if total == 0:
            ctx.progress.finish_stage(self.name)
            return StageResult(stage=self.name, count=0)

        workers = ctx.settings.resolved_threads()
        completed = 0
        errors: list[str] = []

        # Pending writes that will be flushed in batches.
        pending: list[tuple[int, str, int]] = []  # (file_id, digest, size)
        failed: list[tuple[int, str]] = []        # (file_id, error_msg)

        def _flush() -> None:
            """Write a batch of results inside a single transaction."""
            conn.execute("BEGIN")
            for fid, digest, size in pending:
                conn.execute(
                    """
                    INSERT INTO exact_hashes(file_id, sha256, size_bytes)
                    VALUES (?, ?, ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        sha256 = excluded.sha256,
                        size_bytes = excluded.size_bytes
                    """,
                    (fid, digest, size),
                )
                mark_status(conn, fid, "hashed")
            for fid, errmsg in failed:
                mark_status(conn, fid, "failed", error=errmsg)
            conn.execute("COMMIT")
            pending.clear()
            failed.clear()

        # Each worker is a pure-Python SHA-256 (releases the GIL inside hashlib).
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(sha256_file, str(Path(r["path"]))): r["id"]
                for r in rows
            }
            for fut in as_completed(futures):
                fid = futures[fut]
                try:
                    digest, size = fut.result()
                    pending.append((fid, digest, size))
                except Exception as e:
                    failed.append((fid, f"hash: {e}"))
                    errors.append(f"id={fid}: {e}")

                completed += 1
                ctx.progress.advance(self.name)

                if (len(pending) + len(failed)) >= _BATCH_SIZE:
                    _flush()

        # Flush whatever remains.
        if pending or failed:
            _flush()

        ctx.progress.finish_stage(self.name)
        return StageResult(
            stage=self.name,
            count=completed,
            errors=tuple(errors),
            extra={"workers": workers},
        )