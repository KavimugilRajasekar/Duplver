"""Stage 5 — Crop detection via FAISS embedding search.

For every embedded image, queries the FAISS index for its nearest neighbours.
Candidate pairs where one image is significantly smaller (in pixel area) than
the other — but has a high cosine similarity — are flagged as crop duplicates.

Requires Stage 4 (embeddings) to have run and written a FAISS index.
Gracefully skips if no index exists or if faiss is not installed.
"""
from __future__ import annotations

import struct
import time
from pathlib import Path

from duplver.models import PipelineContext, StageResult

# A pair is a "crop" candidate when:
#   - cosine similarity ≥ CROP_SIM_THRESHOLD
#   - the smaller image covers ≤ CROP_AREA_RATIO of the larger image's pixel area
_CROP_SIM_THRESHOLD = 0.88
_CROP_AREA_RATIO = 0.90   # smaller must be ≤ 90 % of larger
_TOP_K = 10               # FAISS neighbours to consider per query


class CropDetector:
    name = "crop"

    def run(self, ctx: PipelineContext) -> StageResult:
        # ── Dependency check ─────────────────────────────────────────────────
        try:
            import faiss
            import numpy as np
        except ImportError as exc:
            ctx.progress.update_total(self.name, total=1)
            ctx.progress.finish_stage(self.name)
            ctx.progress.message(
                f"[yellow]Stage 5 skipped — missing dependency: {exc.name}.[/yellow]"
            )
            return StageResult(stage=self.name, count=0)

        faiss_path: Path = ctx.faiss_path
        id_map_path = Path(str(faiss_path) + ".ids")

        if not faiss_path.exists() or not id_map_path.exists():
            ctx.progress.update_total(self.name, total=1)
            ctx.progress.finish_stage(self.name)
            ctx.progress.message(
                "[yellow]Stage 5 skipped — no FAISS index found. "
                "Run the full analyze pipeline first.[/yellow]"
            )
            return StageResult(stage=self.name, count=0)

        conn = ctx.conn

        # Load FAISS index and id-map.
        index: faiss.IndexFlatIP = faiss.read_index(str(faiss_path))
        with open(id_map_path, "rb") as f:
            raw = f.read()
        id_map: list[int] = list(struct.unpack_from(f"<{len(raw)//4}i", raw))

        n = index.ntotal
        ctx.progress.update_total(self.name, total=max(n, 1))

        if n == 0:
            ctx.progress.finish_stage(self.name)
            return StageResult(stage=self.name, count=0)

        # Retrieve all stored vectors as a matrix for batch search.
        all_vectors = np.zeros((n, index.d), dtype=np.float32)
        for i in range(n):
            index.reconstruct(i, all_vectors[i])

        # File metadata (pixel area) keyed by file_id.
        rows = conn.execute("SELECT id, width, height FROM files").fetchall()
        area_map: dict[int, int] = {
            r["id"]: (r["width"] or 0) * (r["height"] or 0)
            for r in rows
        }
        fid_to_idx: dict[int, int] = {fid: i for i, fid in enumerate(id_map)}

        # Remove old crop clusters before rebuilding.
        conn.execute(
            "DELETE FROM cluster_members WHERE cluster_id IN "
            "(SELECT id FROM clusters WHERE kind = 'crop')"
        )
        conn.execute("DELETE FROM clusters WHERE kind = 'crop'")

        # Batch FAISS search.
        k = min(_TOP_K + 1, n)  # +1 because a vector matches itself at rank 0
        distances, indices = index.search(all_vectors, k)

        now = time.time()
        pairs_seen: set[frozenset[int]] = set()
        clusters_created = 0
        errors: list[str] = []

        conn.execute("BEGIN")
        for qi in range(n):
            fid_a = id_map[qi]
            area_a = area_map.get(fid_a, 0)

            for rank in range(1, k):  # skip rank 0 (self)
                ni = int(indices[qi, rank])
                if ni < 0 or ni >= n:
                    continue
                sim = float(distances[qi, rank])
                if sim < _CROP_SIM_THRESHOLD:
                    break  # results are sorted descending

                fid_b = id_map[ni]
                pair_key = frozenset((fid_a, fid_b))
                if pair_key in pairs_seen:
                    continue
                pairs_seen.add(pair_key)

                area_b = area_map.get(fid_b, 0)
                if area_a == 0 or area_b == 0:
                    continue

                smaller_area = min(area_a, area_b)
                larger_area = max(area_a, area_b)
                ratio = smaller_area / larger_area

                if ratio > _CROP_AREA_RATIO:
                    # Very similar in size — not a crop, leave for embedding stage.
                    continue

                # The larger image is the "best"; the crop is the duplicate.
                best_id = fid_a if area_a >= area_b else fid_b
                dup_id = fid_b if best_id == fid_a else fid_a

                try:
                    cur = conn.execute(
                        """
                        INSERT INTO clusters(
                            kind, detection_method, confidence, best_file_id, created_at
                        ) VALUES ('crop', 'faiss_crop', ?, ?, ?)
                        """,
                        (sim, best_id, now),
                    )
                    cid = int(cur.lastrowid)
                    conn.execute(
                        "INSERT INTO cluster_members(cluster_id, file_id, role, quality_score) "
                        "VALUES (?, ?, 'best', ?)",
                        (cid, best_id, sim),
                    )
                    conn.execute(
                        "INSERT INTO cluster_members(cluster_id, file_id, role, quality_score) "
                        "VALUES (?, ?, 'duplicate', ?)",
                        (cid, dup_id, sim * ratio),
                    )
                    clusters_created += 1
                except Exception as e:
                    errors.append(str(e))

            ctx.progress.advance(self.name)

        conn.execute("COMMIT")
        ctx.progress.finish_stage(self.name)
        return StageResult(
            stage=self.name,
            count=clusters_created,
            errors=tuple(errors),
            extra={
                "pairs_evaluated": len(pairs_seen),
                "sim_threshold": _CROP_SIM_THRESHOLD,
            },
        )