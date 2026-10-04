"""Geometric verification of candidate pairs with ORB features + RANSAC.

Hashes and AI embeddings say two images *look* alike; this module checks
that one is really the other (possibly cropped, scaled or re-encoded) by
finding a consistent homography between local features. It is what turns
"CLIP thinks these are similar" into "B is a crop of A".

OpenCV is optional; without it ``AVAILABLE`` is False and callers fall back
to stricter hash/AI thresholds.
"""
from __future__ import annotations

from dataclasses import dataclass

from duplver.imaging import open_rgb

try:
    import cv2
    import numpy as np

    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
    except Exception:
        pass
    AVAILABLE = True
except Exception:  # pragma: no cover - depends on the environment
    AVAILABLE = False

_MAX_SIDE = 800        # analyse thumbnails; ORB's pyramid handles the scale gap
_MIN_GOOD = 12         # ratio-test matches needed before fitting a homography
_MIN_INLIERS = 12
_MIN_INLIER_RATIO = 0.25

# Coverage = area of the smaller image projected into the larger one, as a
# fraction of the larger image. ≥ SAME_FRAMING means "same picture".
SAME_FRAMING = 0.90


# Normalised cross-correlation (after alignment) needed to call two images
# the same content. Brightness/contrast/colour-to-gray edits keep NCC high;
# a different moment (moved subject, re-take) drops it.
MIN_NCC = 0.80


@dataclass
class GeoMatch:
    inliers: int
    ratio: float      # inliers / ratio-test matches
    coverage: float   # 0..1, see above
    ncc: float        # pixel agreement of the aligned overlap, -1..1

    @property
    def is_crop(self) -> bool:
        return self.coverage < SAME_FRAMING

    @property
    def same_content(self) -> bool:
        return self.ncc >= MIN_NCC


def _gray(path: str):
    return np.asarray(open_rgb(path, max_side=_MAX_SIDE).convert("L"))


def match(small_path: str, large_path: str) -> GeoMatch | None:
    """Locate ``small_path`` inside ``large_path``; None if they don't match.

    Pass the image with fewer pixels first (the potential crop).
    """
    if not AVAILABLE:
        return None
    try:
        g_small = _gray(small_path)
        g_large = _gray(large_path)
    except Exception:
        return None

    orb = cv2.ORB_create(nfeatures=1500)
    kp_s, des_s = orb.detectAndCompute(g_small, None)
    kp_l, des_l = orb.detectAndCompute(g_large, None)
    if des_s is None or des_l is None or len(kp_s) < _MIN_GOOD or len(kp_l) < _MIN_GOOD:
        return None

    try:
        knn = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(des_s, des_l, k=2)
    except cv2.error:
        return None
    good = [p[0] for p in knn if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < _MIN_GOOD:
        return None

    src = np.float32([kp_s[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kp_l[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    try:
        homography, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    except cv2.error:
        return None
    if homography is None or mask is None:
        return None
    inliers = int(mask.sum())
    ratio = inliers / float(len(good))
    if inliers < _MIN_INLIERS or ratio < _MIN_INLIER_RATIO:
        return None

    # Project the small image's outline into the large image and make sure it
    # is a sane, convex quadrilateral that lies (roughly) inside it.
    h_s, w_s = g_small.shape[:2]
    h_l, w_l = g_large.shape[:2]
    corners = np.float32([[0, 0], [w_s, 0], [w_s, h_s], [0, h_s]]).reshape(-1, 1, 2)
    quad = cv2.perspectiveTransform(corners, homography).reshape(-1, 2)
    if not cv2.isContourConvex(quad.astype(np.float32)):
        return None
    tol_x, tol_y = 0.05 * w_l, 0.05 * h_l
    if (
        quad[:, 0].min() < -tol_x or quad[:, 0].max() > w_l + tol_x
        or quad[:, 1].min() < -tol_y or quad[:, 1].max() > h_l + tol_y
    ):
        return None
    coverage = float(cv2.contourArea(quad)) / float(w_l * h_l)
    if coverage < 0.02:
        return None
    return GeoMatch(
        inliers=inliers,
        ratio=ratio,
        coverage=min(coverage, 1.0),
        ncc=_aligned_ncc(g_small, g_large, homography),
    )


def _aligned_ncc(g_small, g_large, homography) -> float:
    """Warp the small image onto the large one and correlate the overlap."""
    h_l, w_l = g_large.shape[:2]
    warped = cv2.warpPerspective(g_small, homography, (w_l, h_l))
    footprint = cv2.warpPerspective(np.full_like(g_small, 255), homography, (w_l, h_l))
    # Erode so resampling artefacts at the warped border don't count.
    mask = cv2.erode(footprint, np.ones((7, 7), np.uint8)) > 0
    if int(mask.sum()) < 400:
        return 0.0
    a = cv2.GaussianBlur(warped, (5, 5), 0)[mask].astype(np.float32)
    b = cv2.GaussianBlur(g_large, (5, 5), 0)[mask].astype(np.float32)
    a -= a.mean()
    b -= b.mean()
    denom = float(np.sqrt((a * a).sum() * (b * b).sum()))
    if denom <= 0.0:
        return 0.0
    return float((a * b).sum() / denom)
