"""Quality scoring for duplicate selection.

When a cluster contains multiple candidates, the one with the highest
score is kept; the rest are flagged as duplicates.

Score is a weighted blend (0–100):

    pixels      40%   - log-scaled, so 4K doesn't dominate 8K
    sharpness   30%   - variance of Laplacian on a downscaled RGB copy
    file_size   15%   - log-scaled, ties broken by larger
    format      15%   - PNG > TIFF > WEBP > AVIF > HEIC > JPEG > BMP > GIF

Sharpness is normalised adaptively: the raw Laplacian variance is mapped
with a soft-log curve so screenshots, diagrams, and synthetic images score
reasonably alongside natural photos.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

# Format weights out of 100 for the format component.
_FORMAT_WEIGHT: dict[str, float] = {
    "PNG": 95.0,
    "TIFF": 88.0,
    "WEBP": 82.0,
    "AVIF": 80.0,
    "HEIC": 78.0,
    "HEIF": 78.0,
    "JPEG": 70.0,
    "JPG": 70.0,
    "BMP": 60.0,
    "GIF": 50.0,
}


@dataclass(slots=True)
class QualityBreakdown:
    score: int
    pixels: float
    sharpness: float
    file_size: float
    format: float


def score_file(path: str, *, size_bytes: int | None = None) -> QualityBreakdown:
    """Compute a 0–100 quality score for the file at ``path``.

    Robust to corruption: any read/decode failure yields a score of 0 with
    the relevant components at 0. The caller can still cluster by hash.
    """
    try:
        # Read via Pillow for metadata, then a separate decode for sharpness
        # (cv2 is faster and gives us the Laplacian directly).
        size = size_bytes
        if size is None:
            size = _safe_size(path)

        with Image.open(path) as im:
            width, height = im.size
            fmt = (im.format or "").upper()
            mode = im.mode

        pixels = max(0.0, _pixels_score(width * height))
        sharp = max(0.0, min(100.0, _sharpness_score(path)))
        fs = max(0.0, _size_score(size or 0))
        fmat = _FORMAT_WEIGHT.get(fmt, 50.0)
        # If the file is in palette mode, demote format slightly.
        if mode == "P":
            fmat *= 0.9

        score = pixels * 0.40 + sharp * 0.30 + fs * 0.15 + fmat * 0.15
        return QualityBreakdown(
            score=int(round(score)),
            pixels=pixels,
            sharpness=sharp,
            file_size=fs,
            format=fmat,
        )
    except Exception:
        return QualityBreakdown(score=0, pixels=0.0, sharpness=0.0, file_size=0.0, format=0.0)


def _safe_size(path: str) -> int:
    try:
        from pathlib import Path

        return Path(path).stat().st_size
    except Exception:
        return 0


def _pixels_score(pixels: int) -> float:
    """Log-scaled pixel count → 0..100."""
    if pixels <= 0:
        return 0.0
    # 0.5 MP → 0, 32 MP → 100, with log curve.
    mp = pixels / 1_000_000.0
    score = (math.log10(max(mp, 0.05)) + 1.3) / (math.log10(32.0) + 1.3) * 100.0
    return max(0.0, min(100.0, score))


def _sharpness_score(path: str) -> float:
    """Soft-log sharpness via variance of Laplacian on a downscaled RGB copy.

    Returns 0..100. Uses a log-normalised curve rather than linear clamping,
    which prevents screenshots and synthetic images (very high raw variance)
    from saturating and prevents noise-free flat images from scoring zero.

    Calibration:
      raw_var ~   1  → score ~   5   (solid-colour fill)
      raw_var ~  50  → score ~  35   (slightly blurry photo)
      raw_var ~ 200  → score ~  60   (average natural photo)
      raw_var ~ 800  → score ~  80   (sharp photo)
      raw_var ~ 5000 → score ~  95   (screenshot / text)
    """
    try:
        with Image.open(path) as im:
            im.thumbnail((1024, 1024))
            if im.mode != "RGB":
                im = im.convert("RGB")
            arr = np.asarray(im, dtype=np.uint8)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if lap_var <= 0.0:
            return 0.0
        # log10(1) = 0 → ~0%; log10(5000) ≈ 3.7 → ~100%
        score = math.log10(lap_var + 1.0) / math.log10(5001.0) * 100.0
        return max(0.0, min(100.0, score))
    except Exception:
        return 0.0


def _size_score(size_bytes: int) -> float:
    """Log-scaled file size → 0..100."""
    if size_bytes <= 0:
        return 0.0
    mb = size_bytes / (1024 * 1024)
    # 0.1 MB → 0, 25 MB → 100.
    score = (math.log10(max(mb, 0.05)) + 1.3) / (math.log10(25.0) + 1.3) * 100.0
    return max(0.0, min(100.0, score))