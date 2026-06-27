"""Stage 7 — Clustering.

For each SHA-256 group of size ≥ 2:

* Create a cluster with kind='exact', detection_method='sha256', confidence=1.0.
* Score every member via ``duplver.quality.score_file``.
* The highest-scoring member is the ``best_file_id``; others are tagged
  ``role='duplicate'``.

Perceptual / crop clusters are built by their respective pipeline stages
(Stages 3 and 5). This stage only handles exact clusters. The pipeline is
incremental: re-running over an unchanged DB is a no-op (we delete exact
clusters first, since SHA-256 dedup is deterministic).

Stage contract:
    Clusterer().run(ctx) -> StageResult
"""
from __future__ import annotations

import time
from pathlib import Path

from duplver.models import PipelineContext, StageResult
from duplver.quality import score_file


class Clusterer:
    name = "clustering"

    def run(self, ctx: PipelineContext) -> StageResult:
        conn = ctx.conn
        # Always rebuild exact clusters — they're cheap and deterministic.
        # This deletes prior exact clusters; perceptual/similar clusters
        # added by future stages are preserved.
        conn.execute(
            "DELETE FROM cluster_members WHERE cluster_id IN "
            "(SELECT id FROM clusters WHERE kind = 'exact')"
        )
        conn.execute("DELETE FROM clusters WHERE kind = 'exact'")

        # Find SHA-256 groups with 2+ members.
        groups = conn.execute(
            """
            SELECT eh.sha256, COUNT(*) AS n, SUM(f.size_bytes) AS total_bytes
            FROM exact_hashes eh
            JOIN files f ON f.id = eh.file_id
            GROUP BY eh.sha256
            HAVING n >= 2
            """
        ).fetchall()

        total = len(groups)
        ctx.progress.update_total(self.name, total=total or 1)

        created = 0
        now = time.time()
        conn.execute("BEGIN")
        for g in groups:
            members = conn.execute(
                """
                SELECT f.id, f.path, f.size_bytes
                FROM exact_hashes eh
                JOIN files f ON f.id = eh.file_id
                WHERE eh.sha256 = ?
                """,
                (g["sha256"],),
            ).fetchall()

            scores = [(m, _safe_score(m["path"], m["size_bytes"])) for m in members]
            # Highest score wins; ties broken by larger file then older mtime
            # (already in DB; we don't reload mtime to keep it cheap).
            best = max(
                scores,
                key=lambda t: (t[1], t[0]["size_bytes"]),
            )

            cur = conn.execute(
                """
                INSERT INTO clusters(
                    kind, detection_method, confidence, best_file_id, created_at
                ) VALUES ('exact', 'sha256', 1.0, ?, ?)
                """,
                (best[0]["id"], now),
            )
            cluster_id = int(cur.lastrowid)

            for m, sc in scores:
                role = "best" if m["id"] == best[0]["id"] else "duplicate"
                conn.execute(
                    """
                    INSERT INTO cluster_members(
                        cluster_id, file_id, role, quality_score
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (cluster_id, m["id"], role, sc),
                )
            created += 1
            ctx.progress.advance(self.name)

        conn.execute("COMMIT")
        ctx.progress.finish_stage(self.name)
        return StageResult(
            stage=self.name,
            count=created,
            extra={"groups_seen": total},
        )


def _safe_score(path: str, size_bytes: int) -> int:
    """Score a file, defaulting to 0 on any error.

    A 0-score file will lose ties to any file that scored at all, so
    corrupt images naturally end up as the duplicates rather than the best.
    """
    try:
        return score_file(path, size_bytes=size_bytes).score
    except Exception:
        return 0