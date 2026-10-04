# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: builds ONE self-contained console executable.
#
#   DUPLVER_VARIANT=full  -> dist/Duplver.exe       (includes CLIP AI + bundled weights)
#   DUPLVER_VARIANT=lite  -> dist/Duplver-lite.exe  (no torch; much smaller and faster to start)
#
# Use build_windows.bat rather than calling this directly.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

variant = os.environ.get("DUPLVER_VARIANT", "full").lower()
ROOT = Path(SPECPATH).parent  # noqa: F821 - provided by PyInstaller

datas = []
binaries = collect_dynamic_libs("pillow_heif")
hiddenimports = ["pillow_heif", "send2trash"]
excludes = ["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PySide6", "pandas", "scipy"]
collection_mode = {}

if variant == "full":
    datas += collect_data_files("open_clip")  # model configs + tokenizer vocab
    weights = sorted((ROOT / "packaging" / "models").glob("*.bin"))
    if not weights:
        raise SystemExit("No CLIP weights in packaging/models — run packaging/fetch_clip_weights.py")
    datas += [(str(w), "models") for w in weights]
    # torch / open_clip read their own source at runtime in a few places.
    collection_mode = {"torch": "pyz+py", "open_clip": "pyz+py"}
    name = "Duplver"
else:
    excludes += ["torch", "torchvision", "open_clip", "timm", "huggingface_hub", "safetensors"]
    name = "Duplver-lite"

a = Analysis(
    [str(ROOT / "packaging" / "duplver_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    module_collection_mode=collection_mode,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=name,
    console=True,          # it's a terminal app (live image preview needs a console)
    upx=False,             # UPX breaks torch/OpenCV DLLs and triggers antivirus false positives
    strip=False,
    runtime_tmpdir=None,
)
