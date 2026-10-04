"""Optional AI visual fingerprints with OpenCLIP.

CLIP embeddings find what hashes cannot: crops, heavy edits, and different
shots of the same scene. Everything here is optional — if torch/open_clip
are missing, or the model weights can't be loaded, the scan continues
without AI and says so.

Weights are looked up in this order (first hit wins):
  1. $DUPLVER_CLIP_WEIGHTS
  2. bundled inside the executable:  <resources>/models/<model>-<tag>.bin
  3. the state directory:            <state>/models/<model>-<tag>.bin
  4. open_clip's own download cache (needs internet on first use)
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Callable, Sequence

from duplver.config import Settings
from duplver.paths import resource_dir, state_dir


def installed() -> tuple[bool, str]:
    """Cheap check (no heavy import) for the AI dependencies."""
    for module in ("numpy", "torch", "open_clip"):
        if importlib.util.find_spec(module) is None:
            return False, f"{module} is not installed"
    return True, ""


def weights_filename(settings: Settings) -> str:
    return f"{settings.clip_model}-{settings.clip_pretrained}.bin"


def find_weights(settings: Settings) -> Path | None:
    env = os.environ.get("DUPLVER_CLIP_WEIGHTS")
    candidates = [Path(env)] if env else []
    name = weights_filename(settings)
    candidates += [resource_dir() / "models" / name, state_dir() / "models" / name]
    for path in candidates:
        if path.is_file():
            return path
    return None


def model_key(settings: Settings) -> str:
    return f"{settings.clip_model}/{settings.clip_pretrained}"


class ClipEncoder:
    """Loads the model once; turns PIL images into unit-length vectors."""

    def __init__(self, settings: Settings) -> None:
        import open_clip
        import torch

        weights = find_weights(settings)
        pretrained = str(weights) if weights else settings.clip_pretrained
        model, _, preprocess = open_clip.create_model_and_transforms(
            settings.clip_model, pretrained=pretrained
        )
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = model.to(self.device).eval()
        self.preprocess = preprocess
        self.key = model_key(settings)
        self.source = "bundled" if weights else "download cache"
        self._torch = torch

    def encode(self, tensors: Sequence) -> "np.ndarray":  # noqa: F821
        import numpy as np

        torch = self._torch
        batch = torch.stack(list(tensors)).to(self.device)
        with torch.no_grad():
            vecs = self.model.encode_image(batch).float().cpu().numpy()
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (vecs / norms).astype(np.float32)


def similar_pairs(
    ids: Sequence[int],
    vectors: "np.ndarray",  # noqa: F821
    *,
    min_sim: float,
    top_k: int,
    on_progress: Callable[[int], None] | None = None,
) -> list[tuple[int, int, float]]:
    """Return (id_a, id_b, cosine) for each image's nearest neighbours.

    Exact search via blocked matrix products: for a few hundred thousand
    images this is as fast as a flat FAISS index without the extra dependency.
    """
    import numpy as np

    n = len(ids)
    if n < 2:
        return []
    k = min(top_k, n - 1)
    block = max(1, min(1024, 50_000_000 // max(n, 1)))
    seen: dict[tuple[int, int], float] = {}
    for start in range(0, n, block):
        stop = min(n, start + block)
        sims = vectors[start:stop] @ vectors.T
        rows = np.arange(stop - start)
        sims[rows, rows + start] = -1.0  # ignore self-matches
        top = np.argpartition(-sims, k - 1, axis=1)[:, :k]
        for r in range(stop - start):
            i = start + r
            for j in top[r]:
                sim = float(sims[r, j])
                if sim < min_sim:
                    continue
                a, b = (ids[i], ids[int(j)]) if ids[i] < ids[int(j)] else (ids[int(j)], ids[i])
                if sim > seen.get((a, b), -1.0):
                    seen[(a, b)] = sim
        if on_progress:
            on_progress(stop - start)
    return [(a, b, s) for (a, b), s in seen.items()]
