"""Find and classify relationships between images.

Every relationship is an ``Edge`` between two files with a *relation*:

    exact        byte-identical file
    same-pixels  identical picture, different format or metadata
    re-encoded   same size, recompressed / converted (tiny pixel differences)
    resized      same picture at another resolution
    cropped      cut-out of a larger image (verified geometrically)
    edited       same picture with colour/filter/minor edits (verified)
    similar      different shot of the same scene — review only, never
                 cleaned up automatically

Relations are listed strongest → weakest; ``RANK`` gives that order.

Pipeline (see ``pipeline.py``): identical edges → hash pairs → AI pairs →
geometric checks → ``grouping.build_groups``.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from duplver.config import Settings
from duplver.imaging import hamming, hex_to_hash, quality_score

EXACT = "exact"
SAME_PIXELS = "same-pixels"
REENCODED = "re-encoded"
RESIZED = "resized"
CROPPED = "cropped"
EDITED = "edited"
SIMILAR = "similar"

RELATIONS = (EXACT, SAME_PIXELS, REENCODED, RESIZED, CROPPED, EDITED, SIMILAR)
RANK = {r: i for i, r in enumerate(RELATIONS)}

LABELS = {
    EXACT: "Exact copy",
    SAME_PIXELS: "Same pixels, other format/metadata",
    REENCODED: "Re-saved / converted",
    RESIZED: "Resized copy",
    CROPPED: "Cropped version",
    EDITED: "Edited version",
    SIMILAR: "Visually similar",
}

SHORT_LABELS = {
    EXACT: "exact copy",
    SAME_PIXELS: "same pixels",
    REENCODED: "re-saved",
    RESIZED: "resized",
    CROPPED: "cropped",
    EDITED: "edited",
    SIMILAR: "similar",
}

# A crop can't hold more detail than the region it was cut from. If the
# smaller image has noticeably more pixels than that region, it is a
# separate (zoomed) shot or an upscale — treat it as merely similar.
_CROP_MAX_UPSCALE = 1.3
# Aspect-ratio tolerance for "same framing" (resized) and for loose hashes.
_ASPECT_SAME = 0.02
_ASPECT_LOOSE = 0.10
# Without CLIP or OpenCV to confirm, loose hash matches need to be closer.
_UNVERIFIED_PHASH = 8
_UNVERIFIED_DHASH = 10
# Mean-colour change (0-255 scale, any channel) beyond which a copy counts
# as edited rather than merely re-saved or resized.
_TONE_EDIT = 4.0
# CLIP similarity that confirms a loose hash match as an edit.
_AI_CONFIRMS_EDIT = 0.90


@dataclass
class FileInfo:
    id: int
    path: str
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
    tone: tuple = (0.0, 0.0, 0.0)
    quality: float = 0.0

    @classmethod
    def from_row(cls, row) -> "FileInfo":
        info = cls(
            id=row["id"],
            path=row["path"],
            size=row["size"],
            mtime=row["mtime"],
            sha256=row["sha256"],
            pixel_hash=row["pixel_hash"],
            phash=hex_to_hash(row["phash"]),
            dhash=hex_to_hash(row["dhash"]),
            flat=bool(row["flat"]),
            width=row["width"],
            height=row["height"],
            format=row["format"] or "",
            sharpness=row["sharpness"] or 0.0,
            tone=_parse_tone(row["tone"]),
        )
        info.quality = quality_score(info.width, info.height, info.size, info.format, info.sharpness)
        return info

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def aspect(self) -> float:
        return self.width / float(self.height) if self.height else 0.0


@dataclass
class Edge:
    a: int
    b: int
    relation: str
    confidence: float


@dataclass
class Check:
    """A pair that needs geometric verification before it becomes an Edge."""

    small: FileInfo
    large: FileInfo
    confidence: float
    on_same: str | None      # relation if verified with the same framing
    on_crop: str | None      # relation if verified as a crop
    on_fail: str | None      # relation if verification fails


@dataclass
class MatchResult:
    edges: list[Edge] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    def add(self, item: Edge | Check | None) -> None:
        if isinstance(item, Edge):
            self.edges.append(item)
        elif isinstance(item, Check):
            self.checks.append(item)


def _parse_tone(text: str | None) -> tuple:
    try:
        return tuple(float(v) for v in text.split(","))
    except (AttributeError, ValueError):
        return (0.0, 0.0, 0.0)


def _tone_shift(a: FileInfo, b: FileInfo) -> float:
    return max(abs(x - y) for x, y in zip(a.tone, b.tone))


def _aspect_close(a: FileInfo, b: FileInfo, tolerance: float) -> bool:
    if not a.aspect or not b.aspect:
        return False
    return abs(a.aspect - b.aspect) / max(a.aspect, b.aspect) <= tolerance


def _by_area(a: FileInfo, b: FileInfo) -> tuple[FileInfo, FileInfo]:
    return (a, b) if a.area <= b.area else (b, a)


def _edge(a: FileInfo, b: FileInfo, relation: str | None, confidence: float) -> Edge | None:
    if relation is None:
        return None
    return Edge(a.id, b.id, relation, round(max(0.0, min(1.0, confidence)), 3))


# ---------------------------------------------------------------------------
# 1. Identical content
# ---------------------------------------------------------------------------

def identical_edges(files: Sequence[FileInfo]) -> tuple[list[Edge], list[FileInfo]]:
    """Edges for byte-identical and pixel-identical files.

    Returns the edges plus one *representative* per distinct picture; only
    representatives take part in the (more expensive) similarity search.
    """
    edges: list[Edge] = []
    by_sha: dict[str, list[FileInfo]] = defaultdict(list)
    for f in files:
        by_sha[f.sha256].append(f)

    by_pixels: dict[str, list[FileInfo]] = defaultdict(list)
    for group in by_sha.values():
        group.sort(key=lambda f: f.id)
        anchor = group[0]
        for other in group[1:]:
            edges.append(Edge(anchor.id, other.id, EXACT, 1.0))
        by_pixels[anchor.pixel_hash].append(anchor)

    reps: list[FileInfo] = []
    for group in by_pixels.values():
        group.sort(key=lambda f: f.id)
        anchor = group[0]
        for other in group[1:]:
            edges.append(Edge(anchor.id, other.id, SAME_PIXELS, 1.0))
        reps.append(anchor)
    reps.sort(key=lambda f: f.id)
    return edges, reps


# ---------------------------------------------------------------------------
# 2. Perceptual hashes
# ---------------------------------------------------------------------------

def hash_candidates(
    reps: Sequence[FileInfo], settings: Settings
) -> list[tuple[FileInfo, FileInfo, int, int]]:
    """Pairs within the loose pHash *and* dHash thresholds (flat images excluded)."""
    pool = [f for f in reps if not f.flat]
    pairs = _phash_pairs([f.phash for f in pool], settings.phash_loose)
    out = []
    for i, j, hp in pairs:
        a, b = pool[i], pool[j]
        hd = hamming(a.dhash, b.dhash)
        if hd <= settings.dhash_loose:
            out.append((a, b, hp, hd))
    return out


def _phash_pairs(hashes: Sequence[int], max_dist: int) -> list[tuple[int, int, int]]:
    """All index pairs (i < j) whose Hamming distance is ≤ ``max_dist``."""
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is None or len(hashes) < 64:
        out = []
        for i in range(len(hashes)):
            hi = hashes[i]
            for j in range(i + 1, len(hashes)):
                d = hamming(hi, hashes[j])
                if d <= max_dist:
                    out.append((i, j, d))
        return out

    arr = np.array(hashes, dtype=np.uint64)
    n = len(arr)
    bitcount = getattr(np, "bitwise_count", None)
    lut = None if bitcount else np.array([bin(v).count("1") for v in range(256)], dtype=np.uint8)
    # Rows per block so each XOR block stays around 4M entries.
    block = max(1, 4_000_000 // n)
    out = []
    for start in range(0, n, block):
        stop = min(n, start + block)
        cols = arr[start:]                      # only compare against j >= start
        x = arr[start:stop, None] ^ cols[None, :]
        if bitcount is not None:
            dist = bitcount(x)
        else:
            dist = lut[x.view(np.uint8)].reshape(x.shape + (8,)).sum(axis=-1)
        rows, cols_idx = np.nonzero(dist <= max_dist)
        for r, c in zip(rows.tolist(), cols_idx.tolist()):
            i, j = start + r, start + c
            if j > i:
                out.append((i, j, int(dist[r, c])))
    return out


def _framing_relation(a: FileInfo, b: FileInfo) -> str:
    # Re-encoding and resizing keep the average colour; brightness, contrast,
    # colour or filter edits shift it even when the hashes barely change.
    if _tone_shift(a, b) > _TONE_EDIT:
        return EDITED
    if (a.width, a.height) == (b.width, b.height):
        return REENCODED
    if _aspect_close(a, b, _ASPECT_SAME):
        return RESIZED
    return EDITED


def classify_hash_pair(
    a: FileInfo,
    b: FileInfo,
    hp: int,
    hd: int,
    settings: Settings,
    *,
    ai_sim: float | None,
    can_verify: bool,
) -> Edge | Check | None:
    hash_conf = 1.0 - (hp + hd) / 128.0
    if hp <= settings.phash_strict and hd <= settings.dhash_strict:
        return _edge(a, b, _framing_relation(a, b), hash_conf)

    # Loose match: similar layout and tones, but needs a second opinion.
    if not _aspect_close(a, b, _ASPECT_LOOSE):
        return None
    if ai_sim is not None:
        if ai_sim >= _AI_CONFIRMS_EDIT:
            return _edge(a, b, EDITED, min(hash_conf, ai_sim))
        return None
    if can_verify:
        small, large = _by_area(a, b)
        return Check(small, large, hash_conf, on_same=EDITED, on_crop=None, on_fail=None)
    if hp <= _UNVERIFIED_PHASH and hd <= _UNVERIFIED_DHASH:
        return _edge(a, b, EDITED, hash_conf - 0.1)
    return None


# ---------------------------------------------------------------------------
# 3. AI (CLIP) similarity
# ---------------------------------------------------------------------------

def classify_ai_pair(
    a: FileInfo, b: FileInfo, sim: float, settings: Settings, *, can_verify: bool
) -> Edge | Check | None:
    small, large = _by_area(a, b)
    similar = SIMILAR if sim >= settings.similar_threshold else None
    area_ratio = small.area / float(large.area) if large.area else 1.0

    if area_ratio <= 0.90:
        # Possible crop (or a resized + edited copy).
        if not can_verify:
            return _edge(a, b, similar, sim)
        return Check(small, large, sim, on_same=EDITED, on_crop=CROPPED, on_fail=similar)

    if sim >= settings.ai_duplicate_threshold:
        if not can_verify:
            return _edge(a, b, similar or SIMILAR, sim)
        return Check(small, large, sim, on_same=EDITED, on_crop=CROPPED, on_fail=similar or SIMILAR)

    return _edge(a, b, similar, sim)


# ---------------------------------------------------------------------------
# 4. Geometric verification
# ---------------------------------------------------------------------------

def resolve_check(check: Check, geo) -> Edge | None:
    """Turn a verified (or failed) ``Check`` into an Edge.

    ``geo`` is a ``verify.GeoMatch`` or None.
    """
    small, large = check.small, check.large
    if geo is None or not geo.same_content:
        return _edge(small, large, check.on_fail, check.confidence)

    confidence = min(check.confidence, 0.5 + 0.5 * geo.ncc)
    if geo.is_crop:
        region_pixels = geo.coverage * large.area
        if check.on_crop and small.area <= _CROP_MAX_UPSCALE * region_pixels:
            return _edge(small, large, check.on_crop, confidence)
        return _edge(small, large, check.on_fail, check.confidence)
    return _edge(small, large, check.on_same, confidence)


def run_checks(
    checks: Sequence[Check],
    matcher: Callable[[str, str], object],
    mapper: Callable[[Callable, Iterable], Iterable],
    on_done: Callable[[Check], None] | None = None,
) -> list[Edge]:
    """Verify ``checks`` with ``matcher`` (usually ``verify.match``).

    ``mapper(fn, items)`` yields ``(item, result, error)`` — the pipeline
    passes its thread-pool helper so checks run in parallel.
    """
    edges = []
    for check, geo, error in mapper(lambda c: matcher(c.small.path, c.large.path), checks):
        edge = resolve_check(check, None if error else geo)
        if edge is not None:
            edges.append(edge)
        if on_done:
            on_done(check)
    return edges
