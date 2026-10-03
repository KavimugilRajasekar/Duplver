"""Download the CLIP weights once and store them for bundling into the exe.

Saved as a plain state-dict in float16 (half the size of the original
download, no measurable effect on similarity scores), then loaded back and
used once so a broken model file fails the build instead of the user's scan.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import open_clip  # noqa: E402
import torch  # noqa: E402
from PIL import Image  # noqa: E402

from duplver.ai import weights_filename  # noqa: E402
from duplver.config import Settings  # noqa: E402


def main() -> int:
    settings = Settings()
    dest = Path(__file__).resolve().parent / "models" / weights_filename(settings)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if not dest.exists():
        print(f"Downloading CLIP {settings.clip_model} / {settings.clip_pretrained} …")
        model, _, _ = open_clip.create_model_and_transforms(
            settings.clip_model, pretrained=settings.clip_pretrained
        )
        state = {
            k: (v.half() if v.is_floating_point() else v)
            for k, v in model.state_dict().items()
        }
        torch.save(state, dest)
        print(f"Saved {dest} ({dest.stat().st_size / 1e6:.0f} MB)")
    else:
        print(f"Using existing {dest}")

    # Smoke test: load exactly the way the app does and embed one image.
    model, _, preprocess = open_clip.create_model_and_transforms(
        settings.clip_model, pretrained=str(dest)
    )
    model.eval()
    with torch.no_grad():
        vec = model.encode_image(preprocess(Image.new("RGB", (64, 64), "red")).unsqueeze(0))
    assert vec.shape[-1] > 0 and torch.isfinite(vec).all(), "CLIP smoke test failed"
    print("CLIP weights OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
