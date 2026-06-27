"""Stage 3 — Perceptual hashing (pHash / dHash / aHash).

Computes three perceptual hashes for every image with status='hashed',
stores them in ``perceptual_hashes``, then finds all near-duplicate pairs
within ``settings.perceptual_max_distance`` Hamming distance and creates
``kind='perceptual'`` clusters.

Uses the ``imagehash`` library (already a declared dependency).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import imagehash
from PIL import Image

from duplver.db import mark_status
from duplver.models import PipelineContext, StageResult
from duplver.quality import score_file

# Commit DB writes in batches to avoid per-file WAL fsyncs.
_BATCH_SIZE = 300


class PerceptualHasher:
    name = "perceptual"

    def run(self, ctx: PipelineContext) -> StageResult:
        conn = ctx.conn
        settings = ctx.settings
        hash_size = settings.perceptual_hash_size   # default 8  → 64-bit
        max_dist = settings.perceptual_max_distance  # default 10 bits

        # Files that have been SHA-256 hashed but not yet perceptually hashed.
        rows = conn.execute(
            """
            SELECT f.id, f.path
            FROM files f
            LEFT JOIN perceptual_hashes ph ON ph.file_id = f.id
            WHERE f.status = 'hashed'
              AND ph.file_id IS NULL
            """
        ).fetchall()

        total = len(rows)
        ctx.progress.update_total(self.name, total=max(total, 1))

        errors: list[str] = []
        workers = settings.resolved_threads()

        # ── Phase 1: compute hashes ──────────────────────────────────────────
        results: list[tuple[int, str, str, str]] = []  # (id, ph, dh, ah)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_hash_one, r["path"], hash_size): r["id"]
                for r in rows
            }
            for fut in as_completed(futures):
                fid = futures[fut]
                try:
                    ph, dh, ah = fut.result()
                    results.append((fid, ph, dh, ah))
                except Exception as e:
                    errors.append(f"id={fid}: {e}")
                ctx.progress.advance(self.name)

        # ── Phase 2: persist hashes in batches ───────────────────────────────
        conn.execute("BEGIN")
        for i, (fid, ph, dh, ah) in enumerate(results):
            conn.execute(
                """
                INSERT INTO perceptual_hashes(file_id, phash, dhash, ahash, bit_length)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    phash = excluded.phash,
                    dhash = excluded.dhash,
                    ahash = excluded.ahash,
                    bit_length = excluded.bit_length
                """,
                (fid, ph, dh, ah, hash_size * hash_size),
            )
            if (i + 1) % _BATCH_SIZE == 0:
                conn.execute("COMMIT")
                conn.execute("BEGIN")
        conn.execute("COMMIT")

        # ── Phase 3: cluster near-duplicates ─────────────────────────────────
        # Load all perceptual hashes that now exist.
        all_hashes = conn.execute(
            "SELECT file_id, phash FROM perceptual_hashes"
        ).fetchall()

        # Convert hex strings back to imagehash objects for Hamming comparison.
        parsed: list[tuple[int, imagehash.ImageHash]] = []
        for row in all_hashes:
            try:
                parsed.append((row["file_id"], imagehash.hex_to_hash(row["phash"])))
            except Exception:
                pass

        # O(n²) pair search — acceptable for typical photo libraries (< 50k).
        # For very large collections, a BK-tree or FAISS binary index would
        # be the upgrade path.
        pairs: list[tuple[int, int, int]] = []  # (fid_a, fid_b, distance)
        for i in range(len(parsed)):
            fid_a, h_a = parsed[i]
            for j in range(i + 1, len(parsed)):
                fid_b, h_b = parsed[j]
                dist = h_a - h_b  # imagehash __sub__ = Hamming distance
                if dist <= max_dist:
                    pairs.append((fid_a, fid_b, dist))

        # Remove existing perceptual clusters before rebuilding.
        conn.execute(
            "DELETE FROM cluster_members WHERE cluster_id IN "
            "(SELECT id FROM clusters WHERE kind = 'perceptual')"
        )
        conn.execute("DELETE FROM clusters WHERE kind = 'perceptual'")

        now = time.time()
        clusters_created = 0
        conn.execute("BEGIN")
        for fid_a, fid_b, dist in pairs:
            confidence = max(0.0, 1.0 - dist / (hash_size * hash_size))
            # Score both members to pick the best.
            score_a = _safe_score(conn, fid_a)
            score_b = _safe_score(conn, fid_b)
            best_id = fid_a if score_a >= score_b else fid_b
            dup_id = fid_b if best_id == fid_a else fid_a

            cur = conn.execute(
                """
                INSERT INTO clusters(kind, detection_method, confidence, best_file_id, created_at)
                VALUES ('perceptual', 'phash', ?, ?, ?)
                """,
                (confidence, best_id, now),
            )
            cid = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO cluster_members(cluster_id, file_id, role, quality_score) VALUES (?, ?, 'best', ?)",
                (cid, best_id, max(score_a, score_b)),
            )
            conn.execute(
                "INSERT INTO cluster_members(cluster_id, file_id, role, quality_score) VALUES (?, ?, 'duplicate', ?)",
                (cid, dup_id, min(score_a, score_b)),
            )
            clusters_created += 1
        conn.execute("COMMIT")

        ctx.progress.finish_stage(self.name)
        return StageResult(
            stage=self.name,
            count=len(results),
            errors=tuple(errors),
            extra={
                "pairs_found": len(pairs),
                "clusters_created": clusters_created,
                "max_distance": max_dist,
            },
        )


def _hash_one(path: str, hash_size: int) -> tuple[str, str, str]:
    """Return (phash_hex, dhash_hex, ahash_hex) for an image."""
    with Image.open(path) as im:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        ph = str(imagehash.phash(im, hash_size=hash_size))
        dh = str(imagehash.dhash(im, hash_size=hash_size))
        ah = str(imagehash.average_hash(im, hash_size=hash_size))
    return ph, dh, ah


def _safe_score(conn, file_id: int) -> float:
    """Return a quality score for the given file_id; 0.0 on any error."""
    try:
        row = conn.execute("SELECT path, size_bytes FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            return 0.0
        return float(score_file(row["path"], size_bytes=row["size_bytes"]).score)
    except Exception:
        return 0.0