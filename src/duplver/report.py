"""Reports: self-contained HTML (with thumbnails), CSV and JSON."""
from __future__ import annotations

import csv
import html
import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Sequence

from duplver.imaging import thumbnail_data_uri
from duplver.matching import LABELS
from duplver.results import GroupView, Summary
from duplver.ui.term import human_bytes

FORMATS = ("html", "csv", "json")


def _label(relation: str) -> str:
    return LABELS.get(relation, relation)


def write_json(path: Path, root: Path, summary: Summary, groups: Sequence[GroupView]) -> None:
    payload = {
        "root": str(root),
        "summary": asdict(summary),
        "groups": [
            {
                "id": g.id,
                "kind": g.kind,
                "confidence": g.confidence,
                "savings_bytes": 0 if g.is_similar else g.savings,
                "members": [
                    {
                        "path": m.path,
                        "keep": m.file_id == g.keeper_id,
                        "relation": m.relation,
                        "confidence": m.confidence,
                        "size_bytes": m.size,
                        "width": m.width,
                        "height": m.height,
                        "format": m.format,
                        "quality": m.quality,
                    }
                    for m in g.members
                ],
            }
            for g in groups
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, groups: Sequence[GroupView]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM: Excel-friendly
        w = csv.writer(f)
        w.writerow(["group", "group_kind", "action", "relation", "confidence",
                    "path", "size_bytes", "width", "height", "format", "quality"])
        for g in groups:
            for m in g.members:
                action = "keep" if m.file_id == g.keeper_id else ("review" if g.is_similar else "remove")
                w.writerow([g.id, g.kind, action, m.relation, m.confidence, m.path,
                            m.size, m.width, m.height, m.format, m.quality])


_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#677087;--line:#e3e6ec;
--keep:#1f9d55;--dup:#d64545;--sim:#b7791f;--accent:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1b2029;--text:#e6e9ef;
--muted:#9aa3b5;--line:#2a313d;--keep:#3ccf7d;--dup:#ff6b6b;--sim:#f0b44c;--accent:#6ea0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 12px}
.muted{color:var(--muted)}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:10px;margin:16px 0}.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.stat b{display:block;font-size:20px}.group{background:var(--card);border:1px solid var(--line);
border-radius:12px;margin:14px 0;padding:14px}.ghead{display:flex;flex-wrap:wrap;gap:8px 16px;
align-items:baseline;margin-bottom:10px}.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
.card{border:2px solid var(--line);border-radius:10px;overflow:hidden;min-width:0}
.card.keep{border-color:var(--keep)}.card.dup{border-color:var(--dup)}.card.sim{border-color:var(--sim)}
.thumb{height:170px;display:flex;align-items:center;justify-content:center;background:repeating-conic-gradient(#8881 0 25%,#0000 0 50%) 0 0/16px 16px}
.thumb img{max-width:100%;max-height:170px}.info{padding:8px 10px;font-size:12.5px}
.badge{display:inline-block;font-size:11px;font-weight:600;padding:1px 7px;border-radius:99px;color:#fff}
.b-keep{background:var(--keep)}.b-dup{background:var(--dup)}.b-sim{background:var(--sim)}
.path{word-break:break-all;color:var(--muted);margin-top:4px}code{font-size:12px}
nav a{color:var(--accent);margin-right:14px}
"""


def write_html(
    path: Path,
    root: Path,
    summary: Summary,
    groups: Sequence[GroupView],
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    esc = html.escape
    total = sum(len(g.members) for g in groups)
    done = 0
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Duplver report</title><style>{_CSS}</style></head><body><main>",
        "<h1>Duplver report</h1>",
        f"<div class='muted'>{esc(str(root))} · scanned {esc(summary.last_scan or '?')}"
        f" · AI similarity: {esc(summary.ai or 'off')}</div>",
        "<div class='stats'>",
    ]
    for label, value in [
        ("Images", f"{summary.images:,}"),
        ("Duplicate groups", f"{summary.dup_groups:,}"),
        ("Duplicate files", f"{summary.dup_files:,}"),
        ("Reclaimable", human_bytes(summary.reclaimable)),
        ("Similar groups", f"{summary.similar_groups:,}"),
        ("Unreadable", f"{summary.unreadable:,}"),
    ]:
        parts.append(f"<div class='stat'><span class='muted'>{label}</span><b>{value}</b></div>")
    parts.append("</div>")
    if summary.by_relation:
        parts.append("<div class='muted'>Duplicates by type: " + " · ".join(
            f"{esc(_label(r))} <b>{n:,}</b>" for r, n in summary.by_relation.items()) + "</div>")
    parts.append("<nav><a href='#dups'>Duplicate groups</a><a href='#similar'>Similar groups</a></nav>")

    for section, title, wanted in (("dups", "Duplicate groups", False), ("similar", "Similar groups (review only)", True)):
        parts.append(f"<h2 id='{section}'>{title}</h2>")
        chosen = [g for g in groups if g.is_similar == wanted]
        if not chosen:
            parts.append("<p class='muted'>None found.</p>")
        for i, g in enumerate(chosen, 1):
            kinds = ", ".join(_label(r) for r in g.relations)
            saving = "" if g.is_similar else f" · saves <b>{human_bytes(g.savings)}</b>"
            parts.append(
                f"<section class='group'><div class='ghead'><b>#{i}</b><span>{esc(kinds)}</span>"
                f"<span class='muted'>{len(g.members)} files{saving} · confidence {g.confidence * 100:.0f}%</span></div>"
                "<div class='cards'>"
            )
            for m in g.members:
                keep = m.file_id == g.keeper_id
                cls, badge = ("keep", "KEEP") if keep else (("sim", "SIMILAR") if g.is_similar else ("dup", "DUPLICATE"))
                uri = thumbnail_data_uri(m.path)
                img = f"<img loading='lazy' src='{uri}' alt=''>" if uri else "<span class='muted'>no preview</span>"
                detail = "" if keep else f" · {esc(_label(m.relation))} ({m.confidence * 100:.0f}%)"
                parts.append(
                    f"<div class='card {cls}'><div class='thumb'>{img}</div><div class='info'>"
                    f"<span class='badge b-{cls}'>{badge}</span>{detail}<br>"
                    f"{m.width}×{m.height} · {esc(m.format)} · {human_bytes(m.size)} · quality {m.quality:.0f}"
                    f"<div class='path'><code>{esc(m.path)}</code></div></div></div>"
                )
                done += 1
                if on_progress:
                    on_progress(done, total)
            parts.append("</div></section>")
    parts.append("</main></body></html>")
    path.write_text("".join(parts), encoding="utf-8")
