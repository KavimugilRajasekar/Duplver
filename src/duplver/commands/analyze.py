"""`duplver analyze` — full 7-stage AI image analysis pipeline.

Stages:
    1. Discovery    — find all image files
    2. Exact        — SHA-256 exact duplicate detection
    3. Perceptual   — pHash / dHash / aHash near-duplicate clustering
    4. Embeddings   — CLIP AI visual embeddings + FAISS index
    5. Crop         — crop-pair detection via FAISS search
    6. Features     — ORB/AKAZE keypoint matching + RANSAC confirmation
    7. Clustering   — finalise exact duplicate clusters

Prerequisite: ``duplver scan --path`` must have been run first.
Unlike ``scan`` (Stages 1–3 + 7), ``analyze`` runs the complete pipeline
including the AI-powered Stages 4–6.
"""
from __future__ import annotations

from pathlib import Path

import typer

from duplver import db
from duplver.clustering import Clusterer
from duplver.commands._common import (
    SilentConsole,
    console,
    friendly_error,
    require_path,
    resolve_root,
)
from duplver.commands.scan import _emit_summary
from duplver.config import load_settings
from duplver.crop_detect import CropDetector
from duplver.discovery import Discovery
from duplver.embeddings import EmbeddingGenerator
from duplver.exact import ExactHasher
from duplver.features import FeatureMatcher
from duplver.models import PipelineContext
from duplver.paths import ensure_state_dir, root_paths
from duplver.perceptual import PerceptualHasher
from duplver.progress import MultiStageProgress
from duplver.workflow import (
    STAGE_ANALYZE_DONE,
    STAGE_SCAN_DONE,
    record_stage,
    require_stage,
)

app = typer.Typer(
    help="Full 7-stage AI image analysis (exact + perceptual + CLIP + crop + features).",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    path: Path = typer.Option(
        None,
        "--path",
        "-p",
        help="Directory to analyze (must have been scanned first).",
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Force re-analysis of all files."
    ),
    json_output: bool = typer.Option(False, "--json"),
    follow_symlinks: bool = typer.Option(
        True, "--follow-symlinks/--no-follow-symlinks"
    ),
    skip_ai: bool = typer.Option(
        False,
        "--skip-ai",
        help="Skip CLIP embeddings and crop detection (Stages 4–5). "
             "Useful when torch/faiss are not available.",
    ),
) -> None:
    """Run the full analysis pipeline on PATH.

    Requires ``duplver scan --path PATH`` to have been run first.
    """
    if ctx.invoked_subcommand is not None:
        return
    resolved = require_path(path, label="analyze")
    _run_analyze(
        resolved,
        quiet=quiet,
        no_cache=no_cache,
        json_output=json_output,
        follow_symlinks=follow_symlinks,
        skip_ai=skip_ai,
    )


@friendly_error("duplver analyze")
def _run_analyze(
    path: Path,
    *,
    quiet: bool,
    no_cache: bool,
    json_output: bool,
    follow_symlinks: bool = True,
    skip_ai: bool = False,
) -> None:
    """Walk ``PATH`` and run all 7 analysis stages."""
    root = resolve_root(require_path(path, label="analyze"))
    settings = load_settings()
    rp = root_paths(root)
    ensure_state_dir()
    conn = db.connect(rp["db"])

    # ── Gate: scan must have run first ───────────────────────────────────────
    require_stage(
        conn,
        STAGE_SCAN_DONE,
        root,
        cmd="duplver analyze",
        then_run=f"duplver analyze --path {root}",
    )

    if no_cache:
        conn.execute("DELETE FROM exact_hashes")
        conn.execute("DELETE FROM perceptual_hashes")
        conn.execute("DELETE FROM embeddings")
        conn.execute("DELETE FROM feature_matches")
        conn.execute("UPDATE files SET status = 'discovered' WHERE status != 'failed'")

    progress_console = console if not quiet else SilentConsole()
    progress = MultiStageProgress(progress_console)

    if skip_ai:
        stage_labels = [
            ("discovery",  "Discovery"),
            ("exact",      "Exact Hashing"),
            ("perceptual", "Perceptual Hashing"),
            ("clustering", "Clustering"),
        ]
    else:
        stage_labels = [
            ("discovery",  "Discovery"),
            ("exact",      "Exact Hashing"),
            ("perceptual", "Perceptual Hashing"),
            ("embeddings", "AI Embeddings (CLIP)"),
            ("crop",       "Crop Detection"),
            ("features",   "Feature Matching"),
            ("clustering", "Clustering"),
        ]

    with progress:
        for name, label in stage_labels:
            progress.add_stage(name, label, total=1)

        pctx = PipelineContext(
            root=root,
            db_path=rp["db"],
            faiss_path=rp["faiss"],
            settings=settings,
            progress=progress,
            conn=conn,
        )

        Discovery().run(pctx)
        ExactHasher().run(pctx)
        PerceptualHasher().run(pctx)

        if not skip_ai:
            EmbeddingGenerator().run(pctx)
            CropDetector().run(pctx)
            FeatureMatcher().run(pctx)

        Clusterer().run(pctx)

        # Stamp workflow gates.
        record_stage(conn, STAGE_SCAN_DONE)    # analyze is a superset of scan
        record_stage(conn, STAGE_ANALYZE_DONE)

        _emit_summary(pctx, "Duplver Analyze Complete", json_output=json_output)
