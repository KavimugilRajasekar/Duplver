"""Domain models and pipeline context types."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from duplver.config import Settings
    from duplver.progress import MultiStageProgress


@dataclass(slots=True)
class FileRecord:
    """One image file known to Duplver."""

    id: int | None
    path: Path
    root: Path
    filename: str
    extension: str
    size_bytes: int
    mtime: float
    ctime: float
    width: int | None
    height: int | None
    format: str | None
    mode: str | None
    exif_json: str | None
    status: str = "discovered"
    error: str | None = None

    @property
    def pixels(self) -> int | None:
        if self.width and self.height:
            return self.width * self.height
        return None


@dataclass(slots=True)
class Cluster:
    """One duplicate cluster. ``kind`` is 'exact', 'perceptual', 'crop', or 'similar'."""

    id: int | None
    kind: str
    detection_method: str
    confidence: float
    best_file_id: int | None
    created_at: float
    members: list[tuple[int, str, float]] = field(default_factory=list)
    # members: list of (file_id, role, quality_score)


@dataclass(slots=True)
class StageResult:
    """Outcome of a pipeline stage."""

    stage: str
    count: int = 0
    errors: tuple[str, ...] = ()
    elapsed: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def merge(self, other: "StageResult") -> "StageResult":
        return StageResult(
            stage=self.stage,
            count=self.count + other.count,
            errors=(*self.errors, *other.errors),
            elapsed=self.elapsed + other.elapsed,
            extra={**self.extra, **other.extra},
        )


@dataclass(slots=True)
class PipelineContext:
    """Shared state passed to every pipeline stage.

    A context is created per invocation of ``duplver scan/analyze/cluster``.
    Stages may attach arbitrary attributes (e.g. ``ctx.discovery_task_id``)
    as needed — the typed core fields are what the orchestrator relies on.
    """

    root: Path
    db_path: Path
    faiss_path: Path
    settings: "Settings"
    progress: "MultiStageProgress"
    conn: sqlite3.Connection
    start_time: float = field(default_factory=time.monotonic)
    extra: dict[str, Any] = field(default_factory=dict)