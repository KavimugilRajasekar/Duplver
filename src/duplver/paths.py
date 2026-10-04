"""State directory, per-root database paths and bundled-resource lookup."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

# Folder (inside the scanned root) that `clean` moves duplicates into.
# Discovery always skips it, so removed files never come back as duplicates.
QUARANTINE_DIRNAME = "_duplver_removed"

# Honors DUPLVER_STATE_DIR so tests (and portable setups) can redirect state.
_ENV_STATE = "DUPLVER_STATE_DIR"


def state_dir() -> Path:
    """Return (and create) the directory holding Duplver's databases.

    Windows: %LOCALAPPDATA%\\Duplver.  Elsewhere: ~/.duplver.
    """
    env = os.environ.get(_ENV_STATE)
    if env:
        base = Path(env).expanduser()
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / "Duplver"
    else:
        base = Path.home() / ".duplver"
    base.mkdir(parents=True, exist_ok=True)
    return base


def root_key(root: Path) -> str:
    """Stable, filesystem-safe identifier for a scanned root.

    On Windows paths are case-folded so C:\\Photos and c:\\photos share a key.
    """
    resolved = str(root.expanduser().resolve())
    if os.name == "nt":
        resolved = resolved.lower()
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]


def db_path(root: Path) -> Path:
    return state_dir() / f"{root_key(root)}.db"


def is_frozen() -> bool:
    """True when running from the PyInstaller-built executable."""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Directory holding bundled resources (PyInstaller unpacks to _MEIPASS)."""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else Path(__file__).resolve().parent
