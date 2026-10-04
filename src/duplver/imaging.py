"""Image decoding, fingerprints and quality scoring.

Every file is read and decoded exactly once per scan. From that single decode
we derive:

* ``sha256``     — of the file bytes            → exact copies
* ``pixel_hash`` — of the decoded RGB pixels     → same picture saved in another
                   format / with different metadata (PNG ↔ BMP ↔ TIFF, …)
* ``phash``      — 64-bit DCT perceptual hash    → re-encoded / resized / edited
* ``dhash``      — 64-bit gradient hash          → second opinion for phash
* sharpness, dimensions and format               → quality score / keeper choice
* mean colour (tone)                             → tells re-encodes from edits
* a tiny RGB preview                             → live terminal display

Pixels are always orientation-corrected (EXIF) and alpha is flattened onto
white, so a rotated phone JPEG and its exported PNG compare as equal.
Only Pillow is required here.
"""
from __future__ import annotations

import base64
import hashlib
import io
import math
import os
import re
import statistics
import warnings
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageOps, ImageStat

# Large panoramas and scans are legitimate input for a dedup tool; keep a
# (very generous) ceiling against corrupt files that claim absurd sizes.
Image.MAX_IMAGE_PIXELS = 1_000_000_000
warnings.simplefilter("ignore", Image.DecompressionBombWarning)
warnings.filterwarnings("ignore", module="PIL")

HEIF_SUPPORT = False
try:  # Optional: HEIC/HEIF (iPhone photos), and AVIF on older Pillow builds.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORT = True
    if ".avif" not in Image.registered_extensions() and hasattr(
        pillow_heif, "register_avif_opener"
    ):
        pillow_heif.register_avif_opener()
except Exception:
    pass

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".jpg", ".jpeg", ".jpe", ".jfif",
        ".png", ".webp", ".avif",
        ".tif", ".tiff", ".bmp", ".dib", ".gif",
        ".heic", ".heif",
    }
)

# Long side (px) of the working copy used for hashing and sharpness. A fixed
# size makes sharpness comparable between a full-size original and a
# downscaled copy; resolution is scored separately.
_WORK_SIZE = 512

# Gray-level standard deviation below which an image counts as "flat"
# (blank page, solid fill). Perceptual hashes of such images are noise, so
# they are only ever matched exactly.
_FLAT_STDDEV = 3.0

_FORMAT_SCORE = {
    "PNG": 90, "TIFF": 90, "BMP": 80,
    "WEBP": 75, "HEIF": 75, "AVIF": 75,
    "JPEG": 70, "MPO": 70, "GIF": 40,
}

_COPY_NAME = re.compile(
    r"(?<![a-z])(copy|kopie|copie|duplicate)(?![a-z])|\(\d+\)\s*$|~\d+$",
    re.IGNORECASE,
)


@dataclass
class Preview:
    """Small RGB thumbnail for the live terminal view."""

    width: int
    height: int
    data: bytes  # packed RGB, row-major


@dataclass
class ImageFeatures:
    size: int
    mtime: float
    sha256: str
    pixel_hash: str
    phash: int
    dhash: int
    flat: bool
    width: int
    height: int
    format: str
    sharpness: float
    tone: tuple[float, float, float] = (0.0, 0.0, 0.0)  # mean R, G, B
    preview: Preview | None = None


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def to_rgb(im: Image.Image) -> Image.Image:
    """Convert any Pillow mode to 8-bit RGB, flattening alpha onto white."""
    mode = im.mode
    if mode == "RGB":
        im.load()
        return im
    if mode in ("I", "F") or mode.startswith("I;16"):
        # 16/32-bit grayscale: rescale to 8 bits instead of clipping to white.
        im = im if mode == "F" else im.convert("I")
        hi = im.getextrema()[1] or 0
        scale = 255.0 / hi if hi > 255 else 1.0
        im = im.point(lambda v: v * scale).convert("L")
        return im.convert("RGB")
    if mode in ("RGBA", "LA", "PA", "RGBa", "La") or (
        mode == "P" and "transparency" in im.info
    ):
        rgba = im.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return im.convert("RGB")


def _orient(im: Image.Image) -> Image.Image:
    try:
        oriented = ImageOps.exif_transpose(im)
    except Exception:  # broken EXIF must not make the file unreadable
        return im
    return oriented if oriented is not None else im


def open_rgb(path: str, *, max_side: int | None = None) -> Image.Image:
    """Open ``path`` as an orientation-corrected RGB image.

    With ``max_side`` the result is downscaled to fit, and JPEGs are decoded
    at reduced resolution (much faster for thumbnails / AI input).
    """
    with Image.open(path) as raw:
        if max_side and raw.format in ("JPEG", "MPO"):
            raw.draft("RGB", (max_side, max_side))
        im = to_rgb(_orient(raw))
    if max_side:
        im = fit(im, max_side)
    return im


def fit(im: Image.Image, max_side: int) -> Image.Image:
    """Return ``im`` downscaled so its long side is at most ``max_side``."""
    w, h = im.size
    if max(w, h) <= max_side:
        return im
    scale = max_side / float(max(w, h))
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return im.resize(size, Image.BILINEAR, reducing_gap=2.0)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_file(path: str, preview_box: tuple[int, int] | None = None) -> ImageFeatures:
    """Read, decode and fingerprint one image. Raises on unreadable files."""
    st = os.stat(path)
    with open(path, "rb") as f:
        data = f.read()
    sha = hashlib.sha256(data).hexdigest()

    with Image.open(io.BytesIO(data)) as raw:
        fmt = (raw.format or "").upper()
        im = to_rgb(_orient(raw))
    del data

    width, height = im.size
    pixel_hash = hashlib.sha256(
        f"{width}x{height}:".encode("ascii") + im.tobytes()
    ).hexdigest()

    work = fit(im, _WORK_SIZE)
    del im
    gray = work.convert("L")

    return ImageFeatures(
        size=st.st_size,
        mtime=st.st_mtime,
        sha256=sha,
        pixel_hash=pixel_hash,
        phash=phash(gray),
        dhash=dhash(gray),
        flat=is_flat(gray),
        width=width,
        height=height,
        format=fmt,
        sharpness=sharpness(gray),
        tone=tuple(round(v, 2) for v in ImageStat.Stat(work).mean[:3]),
        preview=make_preview(work, preview_box) if preview_box else None,
    )


# DCT-II basis for the 8 lowest frequencies of a 32-sample signal.
_DCT_N = 32
_DCT_K = 8
_DCT_BASIS = [
    [math.cos(math.pi * (2 * x + 1) * u / (2 * _DCT_N)) for x in range(_DCT_N)]
    for u in range(_DCT_K)
]


def phash(gray: Image.Image) -> int:
    """64-bit perceptual hash: low-frequency DCT coefficients vs. their median."""
    px = list(gray.resize((_DCT_N, _DCT_N), Image.LANCZOS).getdata())
    rows = [px[y * _DCT_N:(y + 1) * _DCT_N] for y in range(_DCT_N)]
    # 1-D DCT along each row (only the lowest 8 frequencies are needed)...
    row_dct = [[sum(c * p for c, p in zip(basis, row)) for basis in _DCT_BASIS] for row in rows]
    # ...then along each column of that result.
    coeffs = [
        sum(_DCT_BASIS[v][y] * row_dct[y][u] for y in range(_DCT_N))
        for v in range(_DCT_K)
        for u in range(_DCT_K)
    ]
    median = statistics.median(coeffs)
    return _pack_bits(c > median for c in coeffs)


def dhash(gray: Image.Image) -> int:
    """64-bit difference hash: is each pixel brighter than its left neighbour."""
    px = list(gray.resize((9, 8), Image.LANCZOS).getdata())
    bits = (px[y * 9 + x + 1] > px[y * 9 + x] for y in range(8) for x in range(8))
    return _pack_bits(bits)


def _pack_bits(bits) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | (1 if bit else 0)
    return value


def is_flat(gray: Image.Image) -> bool:
    small = gray.resize((32, 32), Image.BILINEAR)
    return ImageStat.Stat(small).stddev[0] < _FLAT_STDDEV


def sharpness(gray: Image.Image) -> float:
    """Variance of the Laplacian (higher = more fine detail / in focus)."""
    lap = gray.filter(
        ImageFilter.Kernel((3, 3), (0, 1, 0, 1, -4, 1, 0, 1, 0), scale=1, offset=128)
    )
    return float(ImageStat.Stat(lap).var[0])


def make_preview(im: Image.Image, box: tuple[int, int]) -> Preview:
    """Thumbnail fitting ``box`` = (columns, rows) of terminal cells.

    Each cell shows two vertically stacked pixels (half-block glyph), so the
    pixel box is columns × (rows * 2) and pixels come out roughly square.
    """
    cols, rows = box
    thumb = im.copy()
    thumb.thumbnail((cols, rows * 2), Image.BILINEAR)
    if thumb.mode != "RGB":
        thumb = thumb.convert("RGB")
    return Preview(thumb.width, thumb.height, thumb.tobytes())


def thumbnail_data_uri(path: str, max_side: int = 220) -> str | None:
    """Base64 JPEG thumbnail for the HTML report (None if unreadable)."""
    try:
        im = open_rgb(path, max_side=max_side)
    except Exception:
        return None
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------

def hash_to_hex(value: int) -> str:
    return f"{value:016x}"


def hex_to_hash(text: str) -> int:
    return int(text, 16)


if hasattr(int, "bit_count"):
    def popcount(value: int) -> int:
        return value.bit_count()
else:  # Python < 3.10
    def popcount(value: int) -> int:
        return bin(value).count("1")


def hamming(a: int, b: int) -> int:
    return popcount(a ^ b)


# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def quality_score(width: int, height: int, size: int, fmt: str, sharp: float) -> float:
    """0–100 score used to choose which copy to keep.

    resolution 50% · sharpness 25% · file size 15% · format 10%

    Resolution dominates so an original always beats its downscaled or
    cropped copies; sharpness separates same-size copies where one was
    blurred or heavily recompressed.
    """
    mp = max(width * height, 1) / 1e6
    pixels = _clamp01((math.log10(max(mp, 0.01)) + 2.0) / (math.log10(50.0) + 2.0))
    detail = _clamp01(math.log10(max(sharp, 0.0) + 1.0) / math.log10(3001.0))
    mb = max(size, 1) / 1e6
    size_s = _clamp01((math.log10(mb + 0.001) + 2.0) / (math.log10(30.0) + 2.0))
    fmt_s = _FORMAT_SCORE.get(fmt.upper(), 60) / 100.0
    return round(100.0 * (0.50 * pixels + 0.25 * detail + 0.15 * size_s + 0.10 * fmt_s), 2)


def looks_like_copy(path: str) -> bool:
    """File names such as 'IMG_1 copy.jpg', 'photo (2).png', 'scan~1.tif'."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return bool(_COPY_NAME.search(stem))
