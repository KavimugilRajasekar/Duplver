"""Stage 6 — Local feature matching (ORB / AKAZE).

For every candidate pair identified by prior stages (exact, perceptual, crop),
extracts keypoints with ORB (or AKAZE as a fallback), matches descriptors,
then computes the inlier count via RANSAC homography.

The result is stored in ``feature_matches`` with:
    confidence = inliers / total_matches   (0.0–1.0)

This stage refines uncertain perceptual/crop matches — a high inlier ratio
confirms a genuine structural match even if the images look very different in
colour/tone.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError

from duplver.models import PipelineContext, StageResult

# Minimum number of raw matches before attempting RANSAC.
_MIN_MATCHES = 8
# Thumbnail size for keypoint extraction — keeps Stage 6 cheap on huge files.
_THUMB = 800
# RANSAC reprojection threshold (pixels).
_RANSAC_THRESH = 5.0


class FeatureMatcher:
    name = "features"

    def run(self, ctx: PipelineContext) -> StageResult:
        conn = ctx.conn
        settings = ctx.settings

        # Candidate pairs: all cluster members across perceptual + crop clusters.
        # We compare the 'best' vs each 'duplicate' within every cluster of
        # these types.
        pairs = conn.execute(
            """
            SELECT DISTINCT
                c.best_file_id   AS fid_a,
                cm.file_id       AS fid_b
            FROM clusters c
            JOIN cluster_members cm ON cm.cluster_id = c.id
            WHERE c.kind IN ('perceptual', 'crop')
              AND cm.role = 'duplicate'
              AND cm.file_id != c.best_file_id
            """
        ).fetchall()

        # Also add any pairs that already exist in feature_matches (re-score on
        # re-run) but filter already-matched pairs to avoid redundant work.
        existing = set(
            conn.execute(
                "SELECT file_a, file_b FROM feature_matches"
            ).fetchall()
        )
        existing_pairs = {(r[0], r[1]) for r in existing}

        todo = [
            (r["fid_a"], r["fid_b"])
            for r in pairs
            if r["fid_a"] is not None
            and (r["fid_a"], r["fid_b"]) not in existing_pairs
            and (r["fid_b"], r["fid_a"]) not in existing_pairs
        ]

        total = len(todo)
        ctx.progress.update_total(self.name, total=max(total, 1))

        if total == 0:
            ctx.progress.finish_stage(self.name)
            return StageResult(stage=self.name, count=0)

        # Fetch paths once.
        all_ids = {fid for pair in todo for fid in pair}
        id_to_path: dict[int, str] = {}
        for fid in all_ids:
            row = conn.execute("SELECT path FROM files WHERE id = ?", (fid,)).fetchone()
            if row:
                id_to_path[fid] = row["path"]

        workers = settings.resolved_threads()
        errors: list[str] = []
        results: list[tuple[int, int, int, int, float]] = []  # (a, b, inliers, total, conf)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _match_pair,
                    id_to_path.get(fid_a, ""),
                    id_to_path.get(fid_b, ""),
                ): (fid_a, fid_b)
                for fid_a, fid_b in todo
                if fid_a in id_to_path and fid_b in id_to_path
            }
            for fut in as_completed(futures):
                fid_a, fid_b = futures[fut]
                try:
                    inliers, total_m = fut.result()
                    conf = inliers / total_m if total_m > 0 else 0.0
                    results.append((fid_a, fid_b, inliers, total_m, conf))
                except Exception as e:
                    errors.append(f"({fid_a},{fid_b}): {e}")
                ctx.progress.advance(self.name)

        # ── Persist results in one transaction ───────────────────────────────
        method = "orb"
        conn.execute("BEGIN")
        for fid_a, fid_b, inliers, total_m, conf in results:
            conn.execute(
                """
                INSERT INTO feature_matches(file_a, file_b, method, inliers, total, confidence)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_a, file_b, method) DO UPDATE SET
                    inliers    = excluded.inliers,
                    total      = excluded.total,
                    confidence = excluded.confidence
                """,
                (fid_a, fid_b, method, inliers, total_m, conf),
            )
        conn.execute("COMMIT")

        ctx.progress.finish_stage(self.name)
        return StageResult(
            stage=self.name,
            count=len(results),
            errors=tuple(errors),
            extra={"pairs_evaluated": total, "method": method},
        )


def _load_gray(path: str) -> np.ndarray:
    """Open an image, thumbnail it, and return a grayscale uint8 array."""
    with Image.open(path) as im:
        im.thumbnail((_THUMB, _THUMB), Image.LANCZOS)
        if im.mode != "L":
            im = im.convert("L")
        return np.asarray(im, dtype=np.uint8)


def _match_pair(path_a: str, path_b: str) -> tuple[int, int]:
    """Return (inliers, total_matches) for a pair of images.

    Uses ORB with a brute-force Hamming matcher and RANSAC homography to
    count geometrically consistent matches.
    """
    if not path_a or not path_b:
        return 0, 0

    try:
        gray_a = _load_gray(path_a)
        gray_b = _load_gray(path_b)
    except (UnidentifiedImageError, OSError, ValueError):
        return 0, 0

    # Try ORB first; fall back to AKAZE if ORB finds too few keypoints.
    detector = cv2.ORB_create(nfeatures=1000)
    kp_a, des_a = detector.detectAndCompute(gray_a, None)
    kp_b, des_b = detector.detectAndCompute(gray_b, None)

    if des_a is None or des_b is None or len(kp_a) < 4 or len(kp_b) < 4:
        # Retry with AKAZE (works better on low-texture images).
        try:
            detector = cv2.AKAZE_create()
            kp_a, des_a = detector.detectAndCompute(gray_a, None)
            kp_b, des_b = detector.detectAndCompute(gray_b, None)
        except cv2.error:
            pass

    if des_a is None or des_b is None or len(kp_a) < 4 or len(kp_b) < 4:
        return 0, 0

    # Brute-force Hamming matcher with ratio test.
    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    try:
        raw_matches = bf.knnMatch(des_a, des_b, k=2)
    except cv2.error:
        return 0, 0

    # Lowe's ratio test.
    good: list = []
    for match_group in raw_matches:
        if len(match_group) == 2:
            m, n = match_group
            if m.distance < 0.75 * n.distance:
                good.append(m)

    total_m = len(good)
    if total_m < _MIN_MATCHES:
        return 0, total_m

    # RANSAC homography — count geometrically consistent inliers.
    pts_a = np.float32([kp_a[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    pts_b = np.float32([kp_b[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

    try:
        _, mask = cv2.findHomography(pts_a, pts_b, cv2.RANSAC, _RANSAC_THRESH)
    except cv2.error:
        return 0, total_m

    inliers = int(mask.sum()) if mask is not None else 0
    return inliers, total_m