"""Recursive discovery of image files under a root folder."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from duplver.imaging import IMAGE_EXTENSIONS
from duplver.paths import QUARANTINE_DIRNAME

# Folders that never contain the user's own photos (or contain files Duplver
# itself moved away). Hidden folders (".thumbnails", ".git", …) are skipped too.
_SKIP_DIRS = frozenset(
    {
        QUARANTINE_DIRNAME.lower(),
        "$recycle.bin",
        "system volume information",
        "@eadir",        # Synology thumbnail cache
        "__macosx",      # zip-extraction residue
    }
)


def _skip_dir(name: str) -> bool:
    lowered = name.lower()
    return lowered in _SKIP_DIRS or lowered.startswith(".")


def iter_images(root: Path) -> Iterator[Path]:
    """Yield image files under ``root`` in a stable (sorted) order.

    Symlinked folders are not followed, which also rules out cycles.
    Unreadable folders are skipped silently.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
        for name in sorted(filenames):
            if name.startswith("._"):  # macOS resource-fork companions
                continue
            if os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS:
                yield Path(dirpath, name)
