"""SHA-256 and perceptual hash helpers."""
from __future__ import annotations

import hashlib
from pathlib import Path

# 1 MiB chunks: balances syscall overhead vs. memory footprint.
_CHUNK = 1 << 20


def sha256_file(path: str | Path) -> tuple[str, int]:
    """Compute (sha256_hex_digest, size_bytes) for a file in one pass.

    Reading size from the same buffered read as the hash is atomic — we
    never get a mismatch between the digest and the size.
    """
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(_CHUNK)
            if not block:
                break
            h.update(block)
            size += len(block)
    return h.hexdigest(), size