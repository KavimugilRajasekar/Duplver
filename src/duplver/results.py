"""Read stored groups back from the database for display, reports and cleanup."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from duplver import db
from duplver.grouping import KEEP
from duplver.imaging import quality_score
from duplver.matching import RANK, SIMILAR


@dataclass
class MemberView:
    file_id: int
    path: str
    size: int
    mtime: float
    width: int
    height: int
    format: str
    role: str
    relation: str
    confidence: float
    quality: float


@dataclass
class GroupView:
    id: int
    kind: str
    keeper_id: int
    confidence: float
    members: list[MemberView] = field(default_factory=list)

    @property
    def keeper(self) -> MemberView:
        return next(m for m in self.members if m.file_id == self.keeper_id)

    @property
    def duplicates(self) -> list[MemberView]:
        return [m for m in self.members if m.file_id != self.keeper_id]

    @property
    def savings(self) -> int:
        return sum(m.size for m in self.duplicates)

    @property
    def is_similar(self) -> bool:
        return self.kind == SIMILAR

    @property
    def relations(self) -> list[str]:
        """Distinct relations of the duplicates, strongest first."""
        return sorted({m.relation for m in self.duplicates}, key=lambda r: RANK.get(r, 99))


@dataclass
class Summary:
    images: int
    unreadable: int
    dup_groups: int
    dup_files: int
    reclaimable: int
    similar_groups: int
    similar_files: int
    by_relation: dict
    last_scan: str | None
    ai: str | None


def load_groups(conn) -> list[GroupView]:
    rows = conn.execute(
        """
        SELECT g.id AS gid, g.kind, g.keeper_id, g.confidence AS gconf,
               m.file_id, m.role, m.relation, m.confidence,
               f.path, f.size, f.mtime, f.width, f.height, f.format, f.sharpness
        FROM groups g
        JOIN group_members m ON m.group_id = g.id
        JOIN files f ON f.id = m.file_id
        ORDER BY g.id
        """
    ).fetchall()
    groups: dict[int, GroupView] = {}
    for r in rows:
        g = groups.get(r["gid"])
        if g is None:
            g = groups[r["gid"]] = GroupView(r["gid"], r["kind"], r["keeper_id"], r["gconf"])
        g.members.append(
            MemberView(
                file_id=r["file_id"], path=r["path"], size=r["size"], mtime=r["mtime"],
                width=r["width"] or 0, height=r["height"] or 0, format=r["format"] or "",
                role=r["role"], relation=r["relation"], confidence=r["confidence"],
                quality=quality_score(r["width"] or 0, r["height"] or 0, r["size"], r["format"] or "", r["sharpness"] or 0.0),
            )
        )
    out = [g for g in groups.values() if len(g.members) >= 2]
    for g in out:
        g.members.sort(key=lambda m: (m.role != KEEP, RANK.get(m.relation, 99), m.path))
    # Duplicate groups by space saved, then similar groups by size.
    out.sort(key=lambda g: (g.is_similar, -g.savings if not g.is_similar else -len(g.members), g.id))
    return out


def summarize(conn, groups: list[GroupView]) -> Summary:
    images = conn.execute("SELECT COUNT(*) FROM files WHERE status = 'ok'").fetchone()[0]
    unreadable = conn.execute("SELECT COUNT(*) FROM files WHERE status = 'error'").fetchone()[0]
    dup = [g for g in groups if not g.is_similar]
    sim = [g for g in groups if g.is_similar]
    by_relation = Counter(m.relation for g in dup for m in g.duplicates)
    return Summary(
        images=images,
        unreadable=unreadable,
        dup_groups=len(dup),
        dup_files=sum(len(g.duplicates) for g in dup),
        reclaimable=sum(g.savings for g in dup),
        similar_groups=len(sim),
        similar_files=sum(len(g.members) for g in sim),
        by_relation=dict(sorted(by_relation.items(), key=lambda kv: RANK.get(kv[0], 99))),
        last_scan=db.get_meta(conn, "last_scan"),
        ai=db.get_meta(conn, "ai"),
    )


def unreadable_files(conn) -> list[tuple[str, str]]:
    return [
        (r["path"], r["error"] or "")
        for r in conn.execute("SELECT path, error FROM files WHERE status = 'error' ORDER BY path")
    ]
