"""Stage 4 — AI visual embeddings via OpenCLIP, indexed with FAISS.

Encodes every 'hashed' image through a CLIP model, stores float32 vectors
in the ``embeddings`` table, and builds/updates a FAISS ``IndexFlatIP``
(cosine similarity via L2-normalised vectors) persisted as a sidecar file.

Graceful degradation:
  - If ``torch`` / ``open_clip_torch`` / ``faiss`` are not installed,
    the stage is skipped with an informational message (no crash).
  - CPU fallback is automatic when CUDA is unavailable.
"""
from __future__ import annotations

import struct
import time
from pathlib import Path

from duplver.models import PipelineContext, StageResult


class EmbeddingGenerator:
    name = "embeddings"

    def run(self, ctx: PipelineContext) -> StageResult:
        # ── Optional dependency check ────────────────────────────────────────
        try:
            import faiss  # noqa: F401
            import open_clip
            import torch
        except ImportError as exc:
            ctx.progress.update_total(self.name, total=1)
            ctx.progress.finish_stage(self.name)
            ctx.progress.message(
                f"[yellow]Stage 4 skipped — missing dependency: {exc.name}. "
                "Install torch, open_clip_torch, and faiss-cpu to enable.[/yellow]"
            )
            return StageResult(stage=self.name, count=0)

        import faiss
        import numpy as np

        conn = ctx.conn
        settings = ctx.settings
        model_name = settings.clip_model          # default "ViT-B-32"
        pretrained = settings.clip_pretrained     # default "laion2b_s34b_b79k"
        batch_size = settings.batch_size          # default 32

        # Files that have been hashed but not yet embedded (or embedded with a
        # different model/pretrained combination).
        rows = conn.execute(
            """
            SELECT f.id, f.path
            FROM files f
            LEFT JOIN embeddings e
                ON e.file_id = f.id
                AND e.model = ?
                AND e.pretrained = ?
            WHERE f.status = 'hashed'
              AND e.file_id IS NULL
            """,
            (model_name, pretrained),
        ).fetchall()

        total = len(rows)
        ctx.progress.update_total(self.name, total=max(total, 1))

        if total == 0:
            ctx.progress.finish_stage(self.name)
            return StageResult(stage=self.name, count=0)

        # ── Load model ───────────────────────────────────────────────────────
        device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            model, _, preprocess = open_clip.create_model_and_transforms(
                model_name, pretrained=pretrained
            )
        except Exception as e:
            ctx.progress.finish_stage(self.name)
            ctx.progress.message(
                f"[red]Stage 4 failed to load CLIP model '{model_name}': {e}[/red]"
            )
            return StageResult(stage=self.name, count=0, errors=(str(e),))

        model = model.to(device).eval()
        dim: int = model.visual.output_dim  # e.g. 512 for ViT-B-32

        # ── Load or create FAISS index ───────────────────────────────────────
        faiss_path = ctx.faiss_path
        if faiss_path.exists():
            index: faiss.IndexFlatIP = faiss.read_index(str(faiss_path))
        else:
            index = faiss.IndexFlatIP(dim)

        # Map FAISS sequential position → file_id (stored alongside index).
        id_map_path = Path(str(faiss_path) + ".ids")
        if id_map_path.exists():
            with open(id_map_path, "rb") as f:
                raw = f.read()
            # Each id is stored as a 4-byte little-endian int.
            id_map: list[int] = list(struct.unpack_from(f"<{len(raw)//4}i", raw))
        else:
            id_map = []

        # ── Batch encode ─────────────────────────────────────────────────────
        errors: list[str] = []
        completed = 0

        from PIL import Image, UnidentifiedImageError

        conn.execute("BEGIN")
        batch_ids: list[int] = []
        batch_tensors: list = []

        def _flush_batch() -> None:
            nonlocal completed
            if not batch_tensors:
                return
            import torch as _torch

            tensor = _torch.stack(batch_tensors).to(device)
            with _torch.no_grad(), _torch.autocast(device_type=device, enabled=(device == "cuda")):
                vecs = model.encode_image(tensor)
            vecs = vecs.float().cpu().numpy()
            # L2-normalise so IndexFlatIP == cosine similarity.
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            vecs = (vecs / norms).astype(np.float32)

            for fid, vec in zip(batch_ids, vecs):
                blob = vec.tobytes()
                conn.execute(
                    """
                    INSERT INTO embeddings(file_id, model, pretrained, dim, vector)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        model = excluded.model,
                        pretrained = excluded.pretrained,
                        dim = excluded.dim,
                        vector = excluded.vector
                    """,
                    (fid, model_name, pretrained, dim, blob),
                )
                id_map.append(fid)
                index.add(vec.reshape(1, -1))
                completed += 1

            conn.execute("COMMIT")
            conn.execute("BEGIN")
            batch_ids.clear()
            batch_tensors.clear()

        for row in rows:
            fid = row["id"]
            path = row["path"]
            try:
                with Image.open(path) as im:
                    if im.mode != "RGB":
                        im = im.convert("RGB")
                    tensor = preprocess(im)
                batch_ids.append(fid)
                batch_tensors.append(tensor)
            except (UnidentifiedImageError, OSError, ValueError) as e:
                errors.append(f"id={fid}: {e}")
                ctx.progress.advance(self.name)
                continue

            if len(batch_tensors) >= batch_size:
                _flush_batch()

            ctx.progress.advance(self.name)

        # Flush remainder.
        _flush_batch()
        conn.execute("COMMIT")

        # ── Persist FAISS index ───────────────────────────────────────────────
        faiss_path.parent.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(faiss_path))
        with open(id_map_path, "wb") as f:
            f.write(struct.pack(f"<{len(id_map)}i", *id_map))

        ctx.progress.finish_stage(self.name)
        return StageResult(
            stage=self.name,
            count=completed,
            errors=tuple(errors),
            extra={
                "model": model_name,
                "device": device,
                "dim": dim,
                "index_size": index.ntotal,
            },
        )