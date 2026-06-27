"""Image metadata extraction (EXIF, dimensions, format)."""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, UnidentifiedImageError

# Pillow's getexif() requires Pillow 10+; fall back gracefully on older.
try:  # pragma: no cover
    _getexif = Image.Image.getexif
except AttributeError:  # pragma: no cover
    _getexif = None


def read_image_meta(path: Path) -> dict | None:
    """Return a dict of metadata for ``path``, or None if it can't be opened.

    Never raises — corrupt or unreadable images return None and are recorded
    as 'failed' by the discovery stage.
    """
    try:
        with Image.open(path) as im:
            width, height = im.size
            fmt = im.format
            mode = im.mode
            exif: dict = {}
            if _getexif is not None:
                try:
                    raw = im.getexif()
                    if raw:
                        exif = {str(k): _exif_value(v) for k, v in raw.items()}
                except Exception:
                    exif = {}
    except (UnidentifiedImageError, OSError, ValueError, FileNotFoundError):
        return None

    try:
        exif_json = json.dumps(exif, default=str) if exif else None
    except Exception:
        exif_json = None

    return {
        "width": int(width) if width else None,
        "height": int(height) if height else None,
        "format": fmt,
        "mode": mode,
        "exif_json": exif_json,
    }


def _exif_value(v: object) -> object:
    """Coerce non-JSON-serializable EXIF values."""
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8", errors="replace")
        except Exception:
            return repr(v)
    return v