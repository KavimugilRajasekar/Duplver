"""Turn pairwise edges into duplicate groups and pick the file to keep.

* **Duplicate groups** are connected components of all non-``similar``
  edges. The component's spanning tree is built strongest-relation-first
  (Kruskal), so each member's relation to the keeper is the weakest link on
  the *best* path to it — e.g. a resized copy of an exact copy is "resized",
  never "edited" just because some weaker edge also exists.
* **Similar groups** join different duplicate groups / single images whose
  pictures are merely similar. They are shown for review only.

The keeper is the highest-quality member (resolution, sharpness, size,
format), with copy-like file names penalised and older files winning ties.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from duplver.imaging import looks_like_copy
from duplver.matching import RANK, SIMILAR, Edge, FileInfo

KEEP = "keep"
DUPLICATE = "duplicate"
KEEPER_RELATION = "keeper"
_COPY_NAME_PENALTY = 5.0


@dataclass
class Member:
    file_id: int
    role: str
    relation: str
    confidence: float


@dataclass
class Group:
    kind: str
    keeper_id: int
    confidence: float
    members: list[Member] = field(default_factory=list)


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        parent = self.parent
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.parent[rb] = ra
        return True

    def components(self) -> list[list[int]]:
        out: dict[int, list[int]] = defaultdict(list)
        for x in list(self.parent):
            out[self.find(x)].append(x)
        return list(out.values())


def keeper_rank(info: FileInfo, content_quality: Mapping[str, float] | None = None) -> tuple:
    """Sort key, higher is better.

    Files with identical pixels share the best quality of their pixel group:
    a PNG export of a JPEG holds no extra information, so it must not win
    just for being lossless/larger. Ties go to the file without a copy-like
    name, then the older file, then the smaller one, then the shorter path.
    """
    quality = info.quality
    if content_quality is not None:
        quality = content_quality.get(info.pixel_hash, quality)
    quality -= _COPY_NAME_PENALTY if looks_like_copy(info.path) else 0.0
    return (round(quality, 2), -info.mtime, -info.size, -len(info.path), -info.id)


def _spanning_groups(
    edges: Iterable[Edge],
    files: Mapping[int, FileInfo],
    sort_key,
) -> tuple[UnionFind, list[tuple[int, list[int], dict[int, list[tuple[int, Edge]]]]]]:
    """Kruskal over ``edges`` (in ``sort_key`` order). Returns components with their trees."""
    content_quality: dict[str, float] = {}
    for f in files.values():
        content_quality[f.pixel_hash] = max(f.quality, content_quality.get(f.pixel_hash, f.quality))

    uf = UnionFind()
    tree: dict[int, list[tuple[int, Edge]]] = defaultdict(list)
    for e in sorted(edges, key=sort_key):
        if e.a not in files or e.b not in files:
            continue
        if uf.union(e.a, e.b):
            tree[e.a].append((e.b, e))
            tree[e.b].append((e.a, e))
    comps = []
    for comp in uf.components():
        if len(comp) >= 2:
            keeper = max(comp, key=lambda fid: keeper_rank(files[fid], content_quality))
            comps.append((keeper, comp, tree))
    return uf, comps


def _walk_from_keeper(
    keeper: int, tree: Mapping[int, list[tuple[int, Edge]]]
) -> dict[int, tuple[str, float]]:
    """BFS over the spanning tree: relation = weakest link, confidence = lowest."""
    info: dict[int, tuple[str, float]] = {}
    queue = deque([(keeper, None, 1.0)])
    visited = {keeper}
    while queue:
        node, relation, confidence = queue.popleft()
        for nxt, edge in tree.get(node, ()):
            if nxt in visited:
                continue
            visited.add(nxt)
            rel = edge.relation if relation is None or RANK[edge.relation] > RANK[relation] else relation
            conf = min(confidence, edge.confidence)
            info[nxt] = (rel, conf)
            queue.append((nxt, rel, conf))
    return info


def _make_group(keeper: int, tree) -> Group:
    walk = _walk_from_keeper(keeper, tree)
    kind = max((rel for rel, _ in walk.values()), key=lambda r: RANK[r])
    group = Group(kind=kind, keeper_id=keeper, confidence=round(min(c for _, c in walk.values()), 3))
    group.members.append(Member(keeper, KEEP, KEEPER_RELATION, 1.0))
    for fid, (rel, conf) in sorted(walk.items(), key=lambda kv: (RANK[kv[1][0]], kv[0])):
        group.members.append(Member(fid, DUPLICATE, rel, conf))
    return group


def build_groups(files: Sequence[FileInfo], edges: Sequence[Edge]) -> list[Group]:
    by_id = {f.id: f for f in files}

    dup_edges = [e for e in edges if e.relation != SIMILAR]
    dup_uf, dup_comps = _spanning_groups(
        dup_edges, by_id, sort_key=lambda e: (RANK[e.relation], -e.confidence)
    )
    groups = [_make_group(keeper, tree) for keeper, _, tree in dup_comps]

    # Similar groups operate on whole duplicate groups: each duplicate group is
    # represented by its keeper, so a similar group never splits a duplicate group.
    keeper_of_root = {dup_uf.find(keeper): keeper for keeper, _, _ in dup_comps}

    def representative(fid: int) -> int:
        if fid in dup_uf.parent:
            return keeper_of_root.get(dup_uf.find(fid), fid)
        return fid

    similar_edges = []
    for e in edges:
        if e.relation != SIMILAR:
            continue
        a, b = representative(e.a), representative(e.b)
        if a != b:
            similar_edges.append(Edge(a, b, SIMILAR, e.confidence))
    _, sim_comps = _spanning_groups(similar_edges, by_id, sort_key=lambda e: -e.confidence)
    groups.extend(_make_group(keeper, tree) for keeper, _, tree in sim_comps)
    return groups


def save_groups(conn, groups: Sequence[Group]) -> None:
    """Replace all stored groups (caller owns the transaction)."""
    conn.execute("DELETE FROM groups")
    for g in groups:
        cur = conn.execute(
            "INSERT INTO groups(kind, keeper_id, confidence) VALUES (?, ?, ?)",
            (g.kind, g.keeper_id, g.confidence),
        )
        gid = cur.lastrowid
        conn.executemany(
            "INSERT INTO group_members(group_id, file_id, role, relation, confidence) "
            "VALUES (?, ?, ?, ?, ?)",
            [(gid, m.file_id, m.role, m.relation, m.confidence) for m in g.members],
        )
