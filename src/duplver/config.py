"""Pydantic settings for Duplver.

Loaded from ~/.duplver/config.yaml if present, otherwise defaults. The
``duplver config`` command writes a default config file to disk.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from duplver.paths import default_state_dir

KeepStrategy = Literal["best", "largest", "newest", "oldest"]


class Settings(BaseModel):
    """User-tunable settings.

    All fields have safe defaults that work out-of-the-box on CPU-only systems.
    """

    # Concurrency
    threads: int = Field(
        default=0,
        description="Worker thread count. 0 = auto from psutil.cpu_count().",
    )

    # Perceptual hashing (Stage 3, stubbed in v0.1)
    perceptual_hash_size: int = Field(default=8)
    perceptual_max_distance: int = Field(default=10)

    # CLIP embeddings (Stage 4, stubbed in v0.1)
    clip_model: str = Field(default="ViT-B-32")
    clip_pretrained: str = Field(default="laion2b_s34b_b79k")
    batch_size: int = Field(default=32)

    # Memory
    max_memory_mb: int = Field(default=4096)

    # State
    state_dir: Path = Field(default_factory=default_state_dir)

    # Cleanup behavior
    default_quality_keep: KeepStrategy = Field(default="best")
    trash_verbosity: bool = Field(default=True)

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")

    def resolved_threads(self) -> int:
        """Return the configured thread count, defaulting to psutil.cpu_count()."""
        if self.threads > 0:
            return self.threads
        try:
            import psutil  # local import: optional dep at config-load time

            n = psutil.cpu_count(logical=False) or psutil.cpu_count() or 4
            return max(1, n)
        except Exception:
            return 4


def config_path(state_dir: Path | None = None) -> Path:
    """Return the path to the user's config.yaml."""
    from duplver.paths import ensure_state_dir

    sd = ensure_state_dir(state_dir)
    return sd / "config.yaml"


def load_settings(state_dir: Path | None = None) -> Settings:
    """Load settings from disk, falling back to defaults."""
    cp = config_path(state_dir)
    if not cp.exists():
        return Settings(state_dir=state_dir or default_state_dir())
    try:
        data = yaml.safe_load(cp.read_text(encoding="utf-8")) or {}
        return Settings(**data)
    except Exception:
        # Corrupt config: keep defaults rather than crash.
        return Settings(state_dir=state_dir or default_state_dir())


def save_settings(settings: Settings, state_dir: Path | None = None) -> Path:
    """Write settings to disk as YAML. Returns the path written."""
    cp = config_path(state_dir)
    cp.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.model_dump(mode="json")
    cp.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return cp