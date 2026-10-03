"""Tunable settings for a scan.

Every threshold lives here so the matching logic has one source of truth.
The CLI overrides a few of them (threads, AI on/off, similarity).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    # Worker threads for decoding/hashing. 0 = auto (CPU count, capped at 8
    # so peak memory stays bounded while full-resolution images are decoded).
    threads: int = 0

    # Ignore the cache and re-analyse every file.
    rescan: bool = False

    # Use the CLIP model (if installed / bundled) for crop + similarity search.
    use_ai: bool = True

    # Perceptual-hash thresholds: Hamming distance out of 64 bits.
    # "strict" pairs are near-identical and accepted directly; "loose" pairs
    # must be confirmed by CLIP or by local-feature (ORB) matching.
    phash_strict: int = 4
    dhash_strict: int = 6
    phash_loose: int = 10
    dhash_loose: int = 14

    # CLIP cosine-similarity thresholds.
    similar_threshold: float = 0.92        # "visually similar" (review only)
    ai_duplicate_threshold: float = 0.95   # same picture, edited/resized
    crop_candidate_threshold: float = 0.85 # candidate crop, ORB-verified
    ai_neighbours: int = 10                # nearest neighbours per image

    clip_model: str = "ViT-B-32"
    clip_pretrained: str = "laion2b_s34b_b79k"
    batch_size: int = 32

    def worker_count(self) -> int:
        if self.threads > 0:
            return self.threads
        return max(2, min(8, os.cpu_count() or 4))
