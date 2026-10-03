"""Remove duplicates safely, and undo it.

Default: duplicates are *moved* into ``<root>/_duplver_removed/`` keeping
their relative folder structure, so ``duplver restore`` can put them back
exactly. ``--recycle-bin`` sends them to the OS Recycle Bin / Trash instead
(restore those from the Recycle Bin itself). Nothing is ever deleted
permanently.

Before moving a file we re-check that it is unchanged since the scan and that
the copy being kept still exists.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from duplver import db
from duplver.paths import QUARANTINE_DIRNAME
from duplver.results import GroupView, MemberView

KEEP_STRATEGIES = ("best", "largest", "newest", "oldest")


@dataclass
class PlanItem:
    group: GroupView
    member: MemberView
    keeper: MemberView


def choose_keeper(group: GroupView, strategy: str) -> MemberView:
    if strategy == "largest":
        return max(group.members, key=lambda m: (m.width * m.height, m.size, m.quality))
    if strategy == "newest":
        return max(group.members, key=lambda m: (m.mtime, m.quality))
    if strategy == "oldest":
        return min(group.members, key=lambda m: (m.mtime, -m.quality))
    return group.keeper


def plan(groups: Sequence[GroupView], keep: str = "best", include_similar: bool = False) -> list[PlanItem]:
    selected = [g for g in groups if include_similar or not g.is_similar]
    keepers = {g.id: choose_keeper(g, keep) for g in selected}

    # Similar groups sit on top of duplicate groups: their members are the
    # keepers of duplicate groups (or single images). When a similar group
    # is cleaned, those keepers give way to the similar group's keeper.
    redirect: dict[int, MemberView] = {}
    for g in selected:
        if g.is_similar:
            k = keepers[g.id]
            for m in g.members:
                if m.file_id != k.file_id:
                    redirect[m.file_id] = k
    similar_ids = {g.id for g in selected if g.is_similar}
    protected = {
        k.file_id for gid, k in keepers.items() if gid in similar_ids or k.file_id not in redirect
    }

    items, seen = [], set()
    for g in selected:
        keeper = keepers[g.id]
        if not g.is_similar:
            keeper = redirect.get(keeper.file_id, keeper)
        for m in g.members:
            if m.file_id == keeper.file_id or m.file_id in protected or m.file_id in seen:
                continue
            seen.add(m.file_id)
            items.append(PlanItem(g, m, keeper))
    return items


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for n in range(1, 10_000):
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(path)


def quarantine_path(root: Path, original: str) -> Path:
    try:
        rel = Path(original).resolve().relative_to(root.resolve())
    except ValueError:
        rel = Path(Path(original).name)
    return _unique(root / QUARANTINE_DIRNAME / rel)


def _precheck(item: PlanItem) -> str | None:
    m = item.member
    try:
        st = os.stat(m.path)
    except FileNotFoundError:
        return "already gone"
    except OSError as exc:
        return f"cannot access: {exc}"
    if st.st_size != m.size or st.st_mtime != m.mtime:
        return "changed since the scan (re-scan first)"
    if not os.path.exists(item.keeper.path):
        return "the copy to keep is missing (re-scan first)"
    return None


def apply(
    conn,
    root: Path,
    items: Sequence[PlanItem],
    *,
    recycle: bool = False,
    on_item: Callable[[PlanItem, str | None], None] | None = None,
) -> tuple[list[PlanItem], list[tuple[PlanItem, str]]]:
    """Move (or recycle) each planned duplicate. Returns (moved, skipped)."""
    send2trash = None
    if recycle:
        from send2trash import send2trash  # noqa: F811 - optional dependency

    moved, skipped = [], []
    for item in items:
        problem = _precheck(item)
        stored = None
        if problem is None:
            try:
                if recycle:
                    send2trash(item.member.path)
                else:
                    dest = quarantine_path(root, item.member.path)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(item.member.path, str(dest))
                    stored = str(dest)
            except Exception as exc:
                problem = f"could not move: {exc}"
        if problem is not None:
            skipped.append((item, problem))
        else:
            moved.append(item)
            with db.transaction(conn):
                conn.execute(
                    "INSERT INTO actions(original_path, stored_path, method, at) VALUES (?, ?, ?, ?)",
                    (item.member.path, stored, "recycle" if recycle else "quarantine", time.time()),
                )
                conn.execute("DELETE FROM files WHERE id = ?", (item.member.file_id,))
        if on_item:
            on_item(item, problem)

    with db.transaction(conn):
        conn.execute(
            "DELETE FROM groups WHERE id NOT IN "
            "(SELECT group_id FROM group_members GROUP BY group_id HAVING COUNT(*) >= 2)"
        )
    return moved, skipped


@dataclass
class RestoreResult:
    restored: list[str]
    skipped: list[tuple[str, str]]
    recycled: list[str]


def restore(conn, root: Path) -> RestoreResult:
    """Move quarantined files back to where they came from."""
    result = RestoreResult([], [], [])
    rows = conn.execute(
        "SELECT id, original_path, stored_path, method FROM actions WHERE restored = 0 ORDER BY id DESC"
    ).fetchall()
    for r in rows:
        if r["method"] == "recycle":
            result.recycled.append(r["original_path"])
            continue
        src, dst = r["stored_path"], r["original_path"]
        if not src or not os.path.exists(src):
            result.skipped.append((dst, "no longer in the removed-files folder"))
            continue
        if os.path.exists(dst):
            result.skipped.append((dst, "a file already exists at the original location"))
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        except OSError as exc:
            result.skipped.append((dst, str(exc)))
            continue
        conn.execute("UPDATE actions SET restored = 1 WHERE id = ?", (r["id"],))
        result.restored.append(dst)
    _prune_empty_dirs(root / QUARANTINE_DIRNAME)
    return result


def _prune_empty_dirs(top: Path) -> None:
    if not top.is_dir():
        return
    for dirpath, _, _ in sorted(os.walk(top), key=lambda t: len(t[0]), reverse=True):
        try:
            os.rmdir(dirpath)  # only succeeds when empty
        except OSError:
            pass
