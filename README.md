# Duplver

**Find, classify and safely clean up duplicate images.** Exact copies, files converted to another format, re-saved, resized, cropped and edited versions, plus visually similar shots. Ships as a single Windows `.exe`. While it scans, the terminal shows each image as it's being processed.

```
 Duplver · C:\Photos

 ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  Step 2/4 · Analyzing images
 ▀▀▀▀▀▀  (the image that is  ▀  ███████████████░░░░░░░░░░░░  52.4%
 ▀▀▀▀▀▀   being processed,   ▀  2,143 / 4,090  ·  68.4/s  ·  ETA 00:28
 ▀▀▀▀▀▀   drawn in colour)   ▀
 ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀  Now  IMG_2041.JPG
                                   C:\Photos\2023\Trip
                                   4032×3024 · JPEG · 4.1 MB
                                   Exact copies spotted: 12
```

---

## Using the Windows executable

**Double-click `Duplver.exe`** (or drag a folder onto it) for guided mode: choose a folder, watch the scan, review the results, optionally open a visual HTML report, and optionally move the duplicates aside.

From a terminal:

```bat
Duplver.exe scan "C:\Photos"              :: find + classify duplicates (live view)
Duplver.exe results "C:\Photos"           :: show the last scan's findings
Duplver.exe report "C:\Photos" --open     :: HTML report with thumbnails (also --format csv|json)
Duplver.exe clean "C:\Photos"             :: preview what would be removed
Duplver.exe clean "C:\Photos" --apply     :: move duplicates to C:\Photos\_duplver_removed
Duplver.exe restore "C:\Photos"           :: put them back
Duplver.exe doctor                        :: check formats, AI model, terminal
```

Add `-h` to any command for its options. Useful ones: `scan --no-ai` (faster), `scan --rescan` (ignore the cache), `scan --plain` (for logs), `clean --keep largest|newest|oldest`, `clean --recycle-bin`, `clean --include-similar`.

## What it detects

| Label | Meaning | How it's found |
|---|---|---|
| **exact copy** | byte-identical file | SHA-256 |
| **same pixels** | identical picture, other format or metadata (PNG ↔ BMP ↔ TIFF, stripped EXIF, …) | hash of the decoded, orientation-corrected pixels |
| **re-saved** | same size, recompressed or converted to a lossy format (JPEG ↔ WebP ↔ HEIC) | perceptual hashes (pHash + dHash), unchanged colour |
| **resized** | same picture at another resolution | perceptual hashes, same aspect ratio |
| **edited** | brightness/colour/filter or small edits | hashes or AI, confirmed by feature alignment + pixel correlation |
| **cropped** | a cut-out of a larger image | AI candidate, confirmed by ORB features + RANSAC homography |
| **similar** | a different shot of the same scene (bursts, re-takes) | AI similarity. **Review only, never removed unless you ask** |

Supported formats: JPEG, PNG, WebP, AVIF, HEIC/HEIF, TIFF, BMP, GIF. EXIF rotation is applied and transparency is flattened before comparing, so a rotated phone photo matches its upright export.

**Which copy is kept?** The one with the best quality score: resolution 50%, sharpness 25%, file size 15%, format 10%. Files with identical pixels count as equal quality (a PNG export of a JPEG adds nothing), and names like `photo (1).jpg` or `IMG copy.jpg` lose ties to the original.

**Safety.** `clean` only previews unless you pass `--apply`. Files are *moved* into `_duplver_removed\` inside the scanned folder, keeping their sub-folders, so `restore` can undo it. Nothing is deleted permanently. Before each move Duplver re-checks that the file is unchanged since the scan and that the copy being kept still exists. Crops are only accepted when the smaller image has no more resolution than the region it matches. That stops a separately taken zoom shot from being mistaken for a crop and removed.

## How a scan works

1. **Find**: walk the folder (skips hidden folders, `$RECYCLE.BIN`, `_duplver_removed`, …).
2. **Analyse**: each file is read and decoded once, in parallel. That one pass produces the SHA-256, pixel hash, pHash, dHash, mean colour, sharpness, dimensions, and the live preview.
3. **AI fingerprints** (full build): CLIP ViT-B-32 embeddings, computed once per distinct picture.
4. **Match & classify**: exact/pixel groups → perceptual-hash pairs → AI nearest neighbours → geometric verification → groups (spanning tree, so each file gets its strongest relation to the kept copy).

Results are cached per file (path + size + modified time) in `%LOCALAPPDATA%\Duplver` (`~/.duplver` elsewhere). Re-scans only analyse new or changed files, files that disappeared are dropped, and Ctrl+C keeps finished work.

## Building the executable

PyInstaller can't cross-compile, so build **on Windows** (or use the GitHub Action below):

```bat
build_windows.bat          :: dist\Duplver.exe       — includes AI similarity (~0.6–0.9 GB)
build_windows.bat lite     :: dist\Duplver-lite.exe  — no AI (~80 MB), starts instantly
```

The script needs Python 3.10–3.12 from python.org. It creates a private virtual environment and installs the dependencies into it, downloads the CLIP weights once and bundles them (so the exe works offline), runs the tests, builds a single file and checks it with `doctor`.

The lite build still detects exact, same-pixel, re-saved, resized and edited copies. It can't find crops or "similar" shots. The full exe unpacks itself to a temp folder on every start, so expect a few seconds of startup.

**From macOS/Linux:** push the repo to GitHub, open *Actions → Build Windows executable → Run workflow*, and download both `.exe` files from the run's artifacts. Pushing a tag such as `v0.2.0` also triggers it.

## Development

```bash
pip install -e ".[ai,dev]"          # or just -e . without the AI extras
PYTHONPATH=src python -m unittest discover -s tests -v
python -m duplver scan ~/Pictures
```

The tests generate a small photo library (copies, conversions, resizes, edits, rotated and transparent images, blank and corrupt files) and check the classification, keeper choice, cache, reports, and clean/restore end to end. They need only Pillow.

| Module | Role |
|---|---|
| `cli.py` | commands, guided mode, result printing |
| `pipeline.py` | scan stages, caching, thread pool |
| `imaging.py` | decoding, fingerprints, quality score |
| `matching.py` | relations, thresholds, pair classification |
| `verify.py` | ORB/RANSAC crop & edit verification (OpenCV) |
| `ai.py` | optional CLIP embeddings + nearest-neighbour search |
| `grouping.py` | groups, keeper choice |
| `cleanup.py` | move / recycle / restore |
| `report.py`, `results.py` | HTML/CSV/JSON reports, reading results |
| `ui/` | live terminal view with half-block image preview |

Thresholds live in `config.py`.
