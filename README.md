# Duplver

**Enterprise-grade AI image deduplication CLI.** Finds exact, perceptual, and AI-similar duplicate images across massive collections — then safely moves inferior copies to the OS Trash/Recycle Bin.

---

## Pipeline

Duplver runs a **7-stage analysis pipeline**, executing stages sequentially and caching results in SQLite so re-runs are incremental (only changed files are reprocessed).

| Stage | Name | Method | Command |
|-------|------|---------|---------|
| 1 | **Discovery** | Recursive walk, EXIF + dimension metadata via Pillow | `scan` / `analyze` |
| 2 | **Exact Hashing** | SHA-256 — atomic read of digest + size, batched DB writes | `scan` / `analyze` |
| 3 | **Perceptual Hashing** | pHash + dHash + aHash (imagehash); Hamming-distance clustering | `scan` / `analyze` |
| 4 | **AI Embeddings** | OpenCLIP (`ViT-B-32`) → FAISS `IndexFlatIP` (cosine sim) | `analyze` only |
| 5 | **Crop Detection** | FAISS top-K neighbour search + pixel-area ratio filter | `analyze` only |
| 6 | **Feature Matching** | ORB / AKAZE keypoints → Lowe's ratio test → RANSAC homography inliers | `analyze` only |
| 7 | **Clustering** | Groups exact + perceptual + crop duplicates; quality-scores each member | `scan` / `analyze` |

### Quality Scoring

Each file in a duplicate cluster is scored 0–100 using a weighted blend:

| Component | Weight | Method |
|-----------|--------|--------|
| Pixel count | 40% | Log-scaled (0.5 MP → 0, 32 MP → 100) |
| Sharpness | 30% | Laplacian variance via log₁₀ curve (screenshots, photos, and diagrams all score fairly) |
| File size | 15% | Log-scaled (0.1 MB → 0, 25 MB → 100) |
| Format | 15% | PNG > TIFF > WEBP > AVIF > HEIC > JPEG > BMP > GIF |

The highest-scoring file in each cluster is kept as `role='best'`; the rest become `role='duplicate'`.

---

## Install

```bash
# Recommended: via uv (handles heavy deps like torch automatically)
uv run duplver --help

# Or standard pip (editable/dev install)
pip install -e ".[dev]"
```

Requires **Python ≥ 3.10**. Heavy dependencies (torch, open_clip_torch, faiss-cpu) are declared in `pyproject.toml` and installed automatically.

---

## Quick Start

```bash
# 1. Verify environment (checks all optional dependencies)
duplver doctor

# 2. Scan for exact + perceptual duplicates (Stages 1–3 + 7)
duplver scan --path C:\Photos

# 3. Full AI analysis — also finds crops and similar images (all 7 stages)
duplver analyze --path C:\Photos

# 4. Skip CLIP/crop stages when torch is not available
duplver analyze --path C:\Photos --skip-ai

# 5. Inspect results
duplver stats --path C:\Photos

# 6. Force full re-analysis (ignore cache)
duplver analyze --path C:\Photos --no-cache
```

State lives in `~/.duplver/` — one SQLite database per scanned root, plus a FAISS index sidecar.

---

## Commands

| Command | Description |
|---------|-------------|
| `duplver scan --path <dir>` | Stages 1–3 + 7: discovery, exact & perceptual hashing, clustering |
| `duplver analyze --path <dir>` | Full 7-stage pipeline including CLIP, crop & feature matching |
| `duplver analyze --path <dir> --skip-ai` | Stages 1–3 + 7 only (no torch/faiss required) |
| `duplver cluster --path <dir>` | Rebuild clusters from existing DB without re-hashing |
| `duplver stats --path <dir>` | Summary statistics from the DB |
| `duplver review --path <dir>` | Interactive duplicate review |
| `duplver cleanup --path <dir>` | Move duplicate files to OS Trash |
| `duplver restore --path <dir>` | Undo a cleanup operation |
| `duplver report --path <dir>` | Generate a duplicate report |
| `duplver doctor` | Check environment health (Python, deps, GPU) |
| `duplver config show` | Print current configuration |
| `duplver config init` | Write default `~/.duplver/config.yaml` |
| `duplver benchmark --path <dir>` | Performance benchmark |

---

## Configuration

Settings are loaded from `~/.duplver/config.yaml` (auto-created with defaults on first run via `duplver config init`).

```yaml
# Concurrency (0 = auto from CPU count)
threads: 0

# Perceptual hashing (Stage 3)
perceptual_hash_size: 8         # hash bits = size²; 8 → 64-bit
perceptual_max_distance: 10     # max Hamming distance for near-duplicate

# CLIP AI embeddings (Stage 4)
clip_model: "ViT-B-32"
clip_pretrained: "laion2b_s34b_b79k"
batch_size: 32

# Memory cap
max_memory_mb: 4096

# Cleanup behaviour
default_quality_keep: "best"    # best | largest | newest | oldest
trash_verbosity: true
```

---

## Safety

**Read-only by default.** `duplver` never deletes files. The `cleanup` command moves them to the OS Trash (Windows Recycle Bin / Linux Trash / macOS Trash). The `restore` command reverses this. Permanent deletion requires explicit double confirmation.

---

## State & Incremental Runs

All analysis results are persisted in SQLite (`~/.duplver/<root_hash>.db`). Re-running any command only reprocesses files whose `size` or `mtime` has changed since the last run — making large collections fast to re-check.

The FAISS index is written alongside the DB as `<root_hash>.faiss` (+ a `.faiss.ids` sidecar mapping FAISS positions to file IDs).
