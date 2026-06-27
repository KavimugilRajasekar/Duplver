"""Path resolution for state directory and per-root database files."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

# Default state directory. On Windows: %USERPROFILE%\.duplver
# Honors DUPLVER_STATE_DIR env var so tests can redirect.
_ENV_STATE = "DUPLVER_STATE_DIR"


def default_state_dir() -> Path:
    """Return the default state directory, honoring DUPLVER_STATE_DIR."""
    env = os.environ.get(_ENV_STATE)
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".duplver").resolve()


def ensure_state_dir(state_dir: Path | None = None) -> Path:
    """Return the state directory, creating it if it does not exist."""
    sd = (state_dir or default_state_dir()).resolve()
    sd.mkdir(parents=True, exist_ok=True)
    return sd


def root_key(root: Path) -> str:
    """Stable, filesystem-safe identifier for a scanned root.

    Uses SHA-256 of the resolved absolute path, hex-truncated to 16 chars.
    On Windows, paths are case-folded so C:\\Photos and c:\\photos hash to the
    same key (Windows itself is case-insensitive).
    """
    resolved = str(root.expanduser().resolve())
    if os.name == "nt":
        resolved = resolved.lower()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def root_paths(root: Path, state_dir: Path | None = None) -> dict[str, Path]:
    """Return the on-disk paths associated with a scanned root."""
    sd = ensure_state_dir(state_dir)
    key = root_key(root)
    return {
        "db": sd / f"{key}.db",
        "faiss": sd / f"{key}.faiss",
        "meta": sd / f"{key}.meta.json",
    }