"""The scan pipeline: find → analyse → (AI fingerprints) → match & group.

One command (`duplver scan`) runs everything. Results are cached per file
(path + size + mtime), so re-scans only analyse new or changed files, and
files that disappeared are dropped from the database. Grouping is rebuilt
from the cache at the end of every scan, so it always reflects the folder
as it is now.
"""
from __future__ import annotations

import itertools
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar

from duplver import ai, db, verify
from duplver.config import Settings
from duplver.discovery import iter_images
from duplver.grouping import build_groups, save_groups
from duplver.imaging import analyze_file, hash_to_hex, make_preview, open_rgb
from duplver.matching import (
    SIMILAR,
    FileInfo,
    MatchResult,
    classify_ai_pair,
    classify_hash_pair,
    hash_candidates,
    identical_edges,
    resolve_check,
)
from duplver.ui.live import Item, ProgressUI
from duplver.ui.term import human_bytes

T = TypeVar("T")
R = TypeVar("R")
_COMMIT_EVERY = 200


@dataclass
class ScanReport:
    root: Path
    images: int = 0
    analyzed: int = 0
    cached: int = 0
    unreadable: int = 0
    removed: int = 0
    ai: str = "off"
    elapsed: float = 0.0


def bounded_map(
    fn: Callable[[T], R], items: Iterable[T], workers: int
) -> Iterator[tuple[T, R | None, BaseException | None]]:
    """Run ``fn`` over ``items`` in threads, yielding (item, result, error)
    in completion order.

    Only a few tasks are queued at a time, so Ctrl+C stops promptly instead
    of waiting for thousands of queued jobs. Use with ``contextlib.closing``.
    """
    source = iter(items)
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = {}
    try:
        for item in itertools.islice(source, workers * 3):
            pending[pool.submit(fn, item)] = item
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                item = pending.pop(fut)
                try:
                    result, error = fut.result(), None
                except Exception as exc:  # per-item failure, keep going
                    result, error = None, exc
                for nxt in itertools.islice(source, 1):
                    pending[pool.submit(fn, nxt)] = nxt
                yield item, result, error
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def describe_error(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    if "cannot identify image file" in text:
        return "not a readable image (corrupt or unsupported)"
    return text.splitlines()[0][:160]


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def _discover(root: Path, ui: ProgressUI, step: int, steps: int) -> list[str]:
    ui.stage(step, steps, "Finding images")
    found: list[str] = []
    for path in iter_images(root):
        found.append(str(path))
        if len(found) % 100 == 0:
            ui.advance(100, Item(str(path), detail="found"))
    ui.advance(len(found) % 100)
    ui.end_stage(f"{len(found):,} images")
    return found


def _reconcile(conn, found: list[str], gen: int, rescan: bool) -> tuple[list[str], int]:
    """Mark unchanged cached files as seen; return the files needing analysis."""
    known = {r["path"]: r for r in conn.execute("SELECT id, path, size, mtime FROM files")}
    todo, seen_ids = [], []
    for path in found:
        try:
            st = os.stat(path)
        except OSError:
            continue  # vanished between walk and stat
        row = known.get(path)
        if row is not None and not rescan and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
            seen_ids.append((gen, row["id"]))
        else:
            todo.append(path)
    with db.transaction(conn):
        conn.executemany("UPDATE files SET seen = ? WHERE id = ?", seen_ids)
    return todo, len(seen_ids)


_UPSERT = """
INSERT INTO files(path, size, mtime, seen, status, error, sha256, pixel_hash,
                  phash, dhash, flat, width, height, format, sharpness, tone)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(path) DO UPDATE SET
    size = excluded.size, mtime = excluded.mtime, seen = excluded.seen,
    status = excluded.status, error = excluded.error, sha256 = excluded.sha256,
    pixel_hash = excluded.pixel_hash, phash = excluded.phash, dhash = excluded.dhash,
    flat = excluded.flat, width = excluded.width, height = excluded.height,
    format = excluded.format, sharpness = excluded.sharpness, tone = excluded.tone
"""


def _store(conn, path: str, gen: int, feat=None, error: str | None = None) -> None:
    if feat is not None:
        values = (
            path, feat.size, feat.mtime, gen, "ok", None, feat.sha256, feat.pixel_hash,
            hash_to_hex(feat.phash), hash_to_hex(feat.dhash), int(feat.flat),
            feat.width, feat.height, feat.format, feat.sharpness, ",".join(map(str, feat.tone)),
        )
    else:
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        values = (path, size, mtime, gen, "error", error) + (None,) * 10
    conn.execute(_UPSERT, values)
    # The content may have changed: any stored AI fingerprint is stale.
    conn.execute(
        "DELETE FROM embeddings WHERE file_id = (SELECT id FROM files WHERE path = ?)", (path,)
    )


def _analyze(conn, todo, gen, settings, ui, step, steps) -> tuple[int, int]:
    ui.stage(step, steps, "Analyzing images", total=len(todo))
    box = ui.preview_box
    known_sha = {r[0] for r in conn.execute("SELECT sha256 FROM files WHERE status = 'ok' AND seen = ?", (gen,))}
    exact_copies = unreadable = ok = 0
    ui.count("Exact copies spotted", 0)
    ui.count("Unreadable files", 0)

    pending = 0
    conn.execute("BEGIN")
    try:
        with closing(bounded_map(lambda p: analyze_file(p, box), todo, settings.worker_count())) as results:
            for path, feat, error in results:
                if error is not None:
                    message = describe_error(error)
                    _store(conn, path, gen, error=message)
                    unreadable += 1
                    ui.count("Unreadable files", unreadable)
                    ui.advance(item=Item(path, error=message))
                else:
                    _store(conn, path, gen, feat=feat)
                    ok += 1
                    if feat.sha256 in known_sha:
                        exact_copies += 1
                        ui.count("Exact copies spotted", exact_copies)
                    known_sha.add(feat.sha256)
                    detail = f"{feat.width}×{feat.height} · {feat.format or '?'} · {human_bytes(feat.size)}"
                    ui.advance(item=Item(path, detail=detail, preview=feat.preview))
                pending += 1
                if pending >= _COMMIT_EVERY:
                    conn.execute("COMMIT")
                    conn.execute("BEGIN")
                    pending = 0
    finally:
        conn.execute("COMMIT")  # keep finished work even when interrupted
    summary = f"{ok:,} analysed"
    if unreadable:
        summary += f", {unreadable:,} unreadable"
    ui.end_stage(summary)
    return ok, unreadable


def _embed(conn, settings, ui, step, steps) -> str:
    """Compute CLIP fingerprints for one file per distinct picture. Returns AI status."""
    key = ai.model_key(settings)
    rows = conn.execute(
        """
        SELECT f.id, f.path FROM files f
        WHERE f.id IN (SELECT MIN(id) FROM files WHERE status = 'ok' GROUP BY pixel_hash)
          AND f.id NOT IN (SELECT file_id FROM embeddings WHERE model = ?)
        ORDER BY f.id
        """,
        (key,),
    ).fetchall()
    ui.stage(step, steps, "AI visual fingerprints (CLIP)", total=len(rows))
    if not rows:
        ui.end_stage("up to date")
        return "on"

    ui.status("Loading AI model… (first run can take a while)")
    try:
        encoder = ai.ClipEncoder(settings)
    except Exception as exc:
        ui.status("")
        ui.note(f"AI model could not be loaded ({describe_error(exc)}). Continuing without AI.")
        ui.end_stage("skipped")
        return "unavailable"
    ui.status(f"Model ready on {encoder.device.upper()} ({encoder.source} weights)")
    ui.set_total(len(rows))

    box = ui.preview_box

    def load(row):
        im = open_rgb(row["path"], max_side=448)
        return encoder.preprocess(im), (make_preview(im, box) if box else None)

    done = 0
    for start in range(0, len(rows), settings.batch_size):
        chunk = rows[start:start + settings.batch_size]
        with closing(bounded_map(load, chunk, settings.worker_count())) as loaded_iter:
            loaded = list(loaded_iter)
        good = [(row, res) for row, res, err in loaded if err is None]
        if good:
            vectors = encoder.encode([res[0] for _, res in good])
            with db.transaction(conn):
                conn.executemany(
                    "INSERT OR REPLACE INTO embeddings(file_id, model, vector) VALUES (?, ?, ?)",
                    [(row["id"], key, vec.tobytes()) for (row, _), vec in zip(good, vectors)],
                )
        for row, res, err in loaded:
            done += 1
            if err is not None:
                ui.advance(item=Item(row["path"], error=describe_error(err)))
            else:
                ui.advance(item=Item(row["path"], detail="fingerprinted", preview=res[1]))
    ui.end_stage(f"{done:,} images fingerprinted")
    return "on"


def _load_vectors(conn, settings, ids: set[int]):
    import numpy as np

    out = {}
    for row in conn.execute("SELECT file_id, vector FROM embeddings WHERE model = ?", (ai.model_key(settings),)):
        if row["file_id"] in ids:
            out[row["file_id"]] = np.frombuffer(row["vector"], dtype=np.float32)
    return out


def _match(conn, settings, ui, step, steps, use_vectors: bool) -> tuple[int, int]:
    ui.stage(step, steps, "Matching & classifying")
    ui.status("Comparing fingerprints…")
    files = [FileInfo.from_row(r) for r in conn.execute("SELECT * FROM files WHERE status = 'ok'")]
    by_id = {f.id: f for f in files}
    identical, reps = identical_edges(files)
    result = MatchResult(edges=list(identical))
    can_verify = verify.AVAILABLE

    vectors = _load_vectors(conn, settings, {f.id for f in reps}) if use_vectors else {}

    compared: set[tuple[int, int]] = set()
    for a, b, hp, hd in hash_candidates(reps, settings):
        sim = None
        if a.id in vectors and b.id in vectors:
            sim = float(vectors[a.id] @ vectors[b.id])
        compared.add((min(a.id, b.id), max(a.id, b.id)))
        result.add(classify_hash_pair(a, b, hp, hd, settings, ai_sim=sim, can_verify=can_verify))

    if len(vectors) >= 2:
        import numpy as np

        ids = sorted(vectors)
        matrix = np.stack([vectors[i] for i in ids])
        floor = min(settings.crop_candidate_threshold, settings.similar_threshold)
        for a_id, b_id, sim in ai.similar_pairs(ids, matrix, min_sim=floor, top_k=settings.ai_neighbours):
            if (a_id, b_id) in compared:
                continue
            result.add(classify_ai_pair(by_id[a_id], by_id[b_id], sim, settings, can_verify=can_verify))

    if result.checks:
        ui.status(f"Verifying {len(result.checks):,} look-alike pairs (crops / edits)")
        ui.set_total(len(result.checks))
        box = ui.preview_box

        def check(c):
            geo = verify.match(c.small.path, c.large.path)
            prev = None
            if box:
                try:
                    prev = make_preview(open_rgb(c.small.path, max_side=256), box)
                except Exception:
                    pass
            return geo, prev

        with closing(bounded_map(check, result.checks, settings.worker_count())) as checked:
            for c, res, err in checked:
                geo, prev = res if res else (None, None)
                edge = resolve_check(c, geo)
                if edge is not None:
                    result.edges.append(edge)
                large = os.path.basename(c.large.path)
                ui.advance(item=Item(c.small.path, detail=f"compared with {large}", preview=prev))

    ui.status("Grouping…")
    groups = build_groups(files, result.edges)
    with db.transaction(conn):
        save_groups(conn, groups)
    dup = sum(1 for g in groups if g.kind != SIMILAR)
    similar = len(groups) - dup
    ui.status("")
    ui.end_stage(f"{dup:,} duplicate groups, {similar:,} similar groups")
    return dup, similar


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_scan(root: Path, settings: Settings, ui: ProgressUI) -> ScanReport:
    started = time.monotonic()
    report = ScanReport(root=root)
    conn = db.connect_root(root)

    if settings.use_ai:
        ai_ok, reason = ai.installed()
        report.ai = "on" if ai_ok else "not installed"
    else:
        ai_ok, report.ai = False, "off"
    steps = 4 if ai_ok else 3

    ui.begin(str(root))
    try:
        if settings.use_ai and not ai_ok:
            ui.note(f"AI similarity is not available in this build ({reason}); using hashes + feature matching.")
        found = _discover(root, ui, 1, steps)
        report.images = len(found)

        gen = int(db.get_meta(conn, "generation") or "0") + 1
        todo, report.cached = _reconcile(conn, found, gen, settings.rescan)
        report.analyzed, report.unreadable = _analyze(conn, todo, gen, settings, ui, 2, steps)

        with db.transaction(conn):
            report.removed = conn.execute("DELETE FROM files WHERE seen != ?", (gen,)).rowcount
            db.set_meta(conn, "generation", str(gen))

        if ai_ok:
            report.ai = _embed(conn, settings, ui, 3, steps)
        _match(conn, settings, ui, steps, steps, use_vectors=(report.ai == "on"))

        report.unreadable = conn.execute(
            "SELECT COUNT(*) FROM files WHERE status = 'error'"
        ).fetchone()[0]
        with db.transaction(conn):
            db.set_meta(conn, "last_scan", datetime.now().isoformat(timespec="seconds"))
            db.set_meta(conn, "root", str(root))
            db.set_meta(conn, "ai", report.ai)
    finally:
        ui.close()
        conn.close()
    report.elapsed = time.monotonic() - started
    return report
