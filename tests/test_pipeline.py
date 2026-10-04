"""End-to-end tests on a generated photo library.

Runs with only Pillow installed (numpy / OpenCV / CLIP are optional):

    PYTHONPATH=src python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import os
import random
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

TMP = Path(tempfile.mkdtemp(prefix="duplver-test-"))
os.environ["DUPLVER_STATE_DIR"] = str(TMP / "state")

from duplver import cleanup, db  # noqa: E402
from duplver.cli import main  # noqa: E402
from duplver.config import Settings  # noqa: E402
from duplver.grouping import build_groups  # noqa: E402
from duplver.imaging import analyze_file, hamming  # noqa: E402
from duplver.matching import (  # noqa: E402
    CROPPED, EDITED, EXACT, REENCODED, RESIZED, SAME_PIXELS, SIMILAR, Edge, FileInfo,
)
from duplver.paths import QUARANTINE_DIRNAME  # noqa: E402
from duplver.pipeline import run_scan  # noqa: E402
from duplver.results import load_groups, summarize  # noqa: E402
from duplver.ui.live import ProgressUI  # noqa: E402


def photo(seed: int, size=(1200, 900)) -> Image.Image:
    """A synthetic 'photo': gradient sky, shapes, texture and noise."""
    rnd = random.Random(seed)
    w, h = size
    im = Image.new("RGB", size)
    top = tuple(rnd.randint(0, 255) for _ in range(3))
    bottom = tuple(rnd.randint(0, 255) for _ in range(3))
    draw = ImageDraw.Draw(im)
    for y in range(h):
        t = y / (h - 1)
        draw.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    for _ in range(25):
        x0, y0 = rnd.randint(-100, w), rnd.randint(-100, h)
        x1, y1 = x0 + rnd.randint(40, 500), y0 + rnd.randint(40, 400)
        colour = tuple(rnd.randint(0, 255) for _ in range(3))
        (draw.ellipse if rnd.random() < 0.5 else draw.rectangle)([x0, y0, x1, y1], fill=colour)
    for _ in range(300):
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        draw.line([(x, y), (x + rnd.randint(-30, 30), y + rnd.randint(-30, 30))],
                  fill=tuple(rnd.randint(0, 255) for _ in range(3)), width=2)
    noise = Image.effect_noise(size, 18).convert("RGB")
    return Image.blend(im.filter(ImageFilter.GaussianBlur(1)), noise, 0.08)


def build_library(root: Path) -> dict:
    (root / "originals").mkdir(parents=True)
    (root / "backup" / "old").mkdir(parents=True)
    (root / "exports").mkdir()
    (root / ".thumbnails").mkdir()
    p = {}

    a = photo(1)
    p["a"] = root / "originals" / "beach.jpg"
    a.save(p["a"], quality=95)
    p["a_exact"] = root / "backup" / "old" / "beach copy.jpg"
    shutil.copy2(p["a"], p["a_exact"])
    a_decoded = Image.open(p["a"]).convert("RGB")
    p["a_png"] = root / "exports" / "beach.png"
    a_decoded.save(p["a_png"])                                # same pixels, other format
    p["a_resized"] = root / "exports" / "beach_small.jpg"
    a.resize((600, 450), Image.LANCZOS).save(p["a_resized"], quality=90)
    p["a_edit"] = root / "exports" / "beach_bright.jpg"
    ImageEnhance.Brightness(a).enhance(1.12).save(p["a_edit"], quality=92)

    b = photo(2, (1000, 1000))
    p["b"] = root / "originals" / "forest.png"
    b.save(p["b"])
    p["b_bmp"] = root / "backup" / "forest.bmp"
    b.save(p["b_bmp"])                                        # lossless conversion
    p["b_webp"] = root / "exports" / "forest.webp"
    b.save(p["b_webp"], quality=80)                           # lossy conversion

    # EXIF-rotated phone photo vs. an already-rotated export of it.
    c = photo(3, (900, 600))
    p["c_rot"] = root / "originals" / "phone.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW on display
    c.save(p["c_rot"], quality=95, exif=exif)
    p["c_upright"] = root / "exports" / "phone_upright.jpg"
    c.transpose(Image.ROTATE_270).save(p["c_upright"], quality=95)

    # Transparent PNG vs. the same picture flattened onto white.
    d = Image.new("RGBA", (500, 400), (0, 0, 0, 0))
    d.paste(photo(4, (300, 300)), (100, 50))
    p["d_rgba"] = root / "originals" / "logo.png"
    d.save(p["d_rgba"])
    flat = Image.new("RGB", d.size, (255, 255, 255))
    flat.paste(d, mask=d.getchannel("A"))
    p["d_flat"] = root / "exports" / "logo.bmp"
    flat.save(p["d_flat"])

    # 16-bit grayscale scan.
    p["g16"] = root / "originals" / "scan16.png"
    photo(5, (400, 300)).convert("L").point(lambda v: v * 256).convert("I").convert("I;16").save(p["g16"])

    # Unrelated photos — must not be grouped.
    for i in range(6, 12):
        p[f"u{i}"] = root / "originals" / f"unrelated_{i}.jpg"
        photo(i).save(p[f"u{i}"], quality=90)

    # Blank images of different sizes: hashes are meaningless, never match.
    p["blank1"] = root / "originals" / "blank1.png"
    Image.new("RGB", (300, 300), (255, 255, 255)).save(p["blank1"])
    p["blank2"] = root / "originals" / "blank2.png"
    Image.new("RGB", (640, 480), (255, 255, 255)).save(p["blank2"])

    p["corrupt"] = root / "originals" / "broken.jpg"
    p["corrupt"].write_bytes(b"\xff\xd8\xff\xe0 definitely not a jpeg")
    p["hidden"] = root / ".thumbnails" / "beach.jpg"
    shutil.copy2(p["a"], p["hidden"])
    (root / "notes.txt").write_text("not an image")
    return p


def scan(root: Path, **kw):
    return run_scan(root, Settings(use_ai=False, **kw), ProgressUI())


class LibraryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = TMP / "library"
        cls.p = build_library(cls.root)
        cls.report = scan(cls.root)

    def conn(self):
        return db.open_existing(self.root)

    def groups(self):
        return load_groups(self.conn())

    def group_of(self, path: Path):
        for g in self.groups():
            if not g.is_similar and any(m.path == str(path) for m in g.members):
                return g
        return None

    def relation(self, path: Path) -> str | None:
        g = self.group_of(path)
        if g is None:
            return None
        return next(m.relation for m in g.members if m.path == str(path))

    # --- scan bookkeeping -------------------------------------------------
    def test_counts(self):
        self.assertEqual(self.report.images, 22)  # hidden folder + .txt excluded
        self.assertEqual(self.report.unreadable, 1)

    # --- classification ---------------------------------------------------
    def test_beach_family(self):
        g = self.group_of(self.p["a"])
        self.assertIsNotNone(g)
        paths = {m.path for m in g.members}
        for key in ("a", "a_exact", "a_png", "a_resized", "a_edit"):
            self.assertIn(str(self.p[key]), paths, key)
        self.assertEqual(len(paths), 5)

    def test_relations(self):
        exact_pair = {self.relation(self.p["a"]), self.relation(self.p["a_exact"])}
        self.assertIn(EXACT, exact_pair)
        self.assertEqual(self.relation(self.p["a_resized"]), RESIZED)
        self.assertEqual(self.relation(self.p["a_edit"]), EDITED)
        self.assertIn(self.relation(self.p["b_webp"]), (REENCODED,))

    def test_lossless_conversions_are_same_pixels(self):
        self.assertIn(SAME_PIXELS, {self.relation(self.p["b"]), self.relation(self.p["b_bmp"])})
        self.assertIn(SAME_PIXELS, {self.relation(self.p["d_rgba"]), self.relation(self.p["d_flat"])})

    def test_exif_orientation(self):
        g = self.group_of(self.p["c_rot"])
        self.assertIsNotNone(g)
        self.assertIn(str(self.p["c_upright"]), {m.path for m in g.members})

    def test_keeper_is_original(self):
        g = self.group_of(self.p["a"])
        keeper = g.keeper.path
        # Full resolution, not the resized copy and not the "copy"-named file.
        self.assertNotIn(keeper, (str(self.p["a_resized"]), str(self.p["a_exact"])))

    def test_no_false_positives(self):
        for key in ("u6", "u7", "u8", "u9", "u10", "u11", "blank1", "blank2", "g16"):
            self.assertIsNone(self.group_of(self.p[key]), key)

    def test_summary(self):
        conn = self.conn()
        s = summarize(conn, load_groups(conn))
        self.assertEqual(s.dup_groups, 4)
        self.assertEqual(s.dup_files, 4 + 2 + 1 + 1)

    # --- incremental + cli --------------------------------------------------
    def test_rescan_uses_cache(self):
        again = scan(self.root)
        self.assertEqual(again.analyzed, 0)
        self.assertEqual(again.cached, 22)

    def test_reports_and_cli(self):
        out = io.StringIO()
        with redirect_stdout(out):
            for fmt in ("html", "csv", "json"):
                target = TMP / f"report.{fmt}"
                self.assertEqual(main(["report", str(self.root), "-f", fmt, "-o", str(target)]), 0)
                self.assertGreater(target.stat().st_size, 500)
            self.assertEqual(main(["results", str(self.root), "--all"]), 0)
            self.assertEqual(main(["results", "--path", str(self.root), "--unreadable"]), 0)
            self.assertEqual(main(["clean", str(self.root)]), 0)  # preview only
            self.assertEqual(main(["results", str(TMP / "nope")]), 2)
        self.assertIn("data:image/jpeg;base64", (TMP / "report.html").read_text(encoding="utf-8"))
        self.assertIn("Preview only", out.getvalue())
        for p in self.p.values():
            if p.name != "broken.jpg":
                self.assertTrue(p.exists(), p)  # preview must not move anything


class CleanRestoreTest(unittest.TestCase):
    def test_clean_then_restore(self):
        root = TMP / "clean-lib"
        p = build_library(root)
        scan(root)
        conn = db.open_existing(root)
        items = cleanup.plan(load_groups(conn))
        removed = {i.member.path for i in items}
        kept = {i.keeper.path for i in items}
        self.assertEqual(len(items), 8)
        self.assertFalse(removed & kept)

        # A file modified after the scan must be skipped, not moved.
        changed = next(i.member.path for i in items)
        os.utime(changed, (1, 1))
        moved, skipped = cleanup.apply(conn, root, items)
        self.assertEqual(len(moved), 7)
        self.assertEqual([i.member.path for i, _ in skipped], [changed])
        for item in moved:
            self.assertFalse(os.path.exists(item.member.path))
        for k in kept:
            self.assertTrue(os.path.exists(k))
        self.assertTrue((root / QUARANTINE_DIRNAME / "backup" / "old" / "beach copy.jpg").exists())

        # Re-scan: quarantine folder is ignored, removed files are gone.
        rescanned = scan(root)
        self.assertEqual(rescanned.images, 22 - 7)

        result = cleanup.restore(db.open_existing(root), root)
        self.assertEqual(len(result.restored), 7)
        self.assertFalse((root / QUARANTINE_DIRNAME).exists())
        for path in p.values():
            self.assertTrue(path.exists(), path)


class UnitTests(unittest.TestCase):
    def test_hash_robustness(self):
        d = TMP / "unit"
        d.mkdir(exist_ok=True)
        base = photo(42)
        base.save(d / "x.png")
        base.resize((300, 225)).save(d / "x_small.jpg", quality=70)
        photo(43).save(d / "y.png")
        fx, fs, fy = (analyze_file(str(d / n)) for n in ("x.png", "x_small.jpg", "y.png"))
        self.assertLessEqual(hamming(fx.phash, fs.phash), 4)
        self.assertGreater(hamming(fx.phash, fy.phash), 16)

    def _info(self, fid, w=100, h=100, q=0.0, path=None):
        return FileInfo(fid, path or f"/x/{fid}.jpg", 1000, 0.0, str(fid), str(fid), 0, 0,
                        False, w, h, "JPEG", 0.0, quality=q)

    def test_grouping_uses_strongest_path(self):
        files = [self._info(1, q=90), self._info(2, q=50), self._info(3, q=40)]
        edges = [Edge(1, 2, EXACT, 1.0), Edge(2, 3, RESIZED, 0.95), Edge(1, 3, EDITED, 0.8)]
        (g,) = build_groups(files, edges)
        rel = {m.file_id: m.relation for m in g.members}
        self.assertEqual(g.keeper_id, 1)
        self.assertEqual(rel[2], EXACT)
        self.assertEqual(rel[3], RESIZED)  # via 1→2→3, not the weaker direct edit edge
        self.assertEqual(g.kind, RESIZED)

    def test_similar_groups_sit_on_top_of_duplicates(self):
        files = [self._info(i, q=100 - i) for i in range(1, 5)]
        edges = [Edge(1, 2, EXACT, 1.0), Edge(2, 3, SIMILAR, 0.93), Edge(3, 4, CROPPED, 0.9)]
        groups = build_groups(files, edges)
        dup = [g for g in groups if g.kind != SIMILAR]
        sim = [g for g in groups if g.kind == SIMILAR]
        self.assertEqual(len(dup), 2)
        self.assertEqual(len(sim), 1)
        self.assertEqual({m.file_id for m in sim[0].members}, {1, 3})  # keepers of each dup group


class CleanupPlanTest(unittest.TestCase):
    def _m(self, fid, role="duplicate", rel=EXACT):
        from duplver.results import MemberView

        return MemberView(fid, f"/x/{fid}.jpg", 100 * fid, float(fid), 10, 10, "JPEG", role, rel, 1.0, 50.0)

    def _g(self, gid, kind, keeper, others, rel=EXACT):
        from duplver.results import GroupView

        g = GroupView(gid, kind, keeper, 1.0)
        g.members = [self._m(keeper, "keep", "keeper")] + [self._m(o, rel=rel) for o in others]
        return g

    def test_similar_groups_only_when_asked(self):
        # Dup group A: keep 1, remove 2. Dup group B: keep 3, remove 4.
        # Similar group joins the two keepers (1 keeps, 3 is the similar one).
        groups = [
            self._g(1, EXACT, 1, [2]),
            self._g(2, EXACT, 3, [4]),
            self._g(3, SIMILAR, 1, [3], rel=SIMILAR),
        ]
        removed = {i.member.file_id for i in cleanup.plan(groups)}
        self.assertEqual(removed, {2, 4})

        items = cleanup.plan(groups, include_similar=True)
        removed = {i.member.file_id for i in items}
        self.assertEqual(removed, {2, 3, 4})
        # Everything removed from group B now points at the surviving keeper.
        self.assertTrue(all(i.keeper.file_id == 1 for i in items))


if __name__ == "__main__":
    unittest.main()
