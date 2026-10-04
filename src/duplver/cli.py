"""Command-line interface.

    duplver scan    PATH   find, analyse, classify and group duplicates (live view)
    duplver results PATH   show what the last scan found
    duplver report  PATH   write an HTML (thumbnails) / CSV / JSON report
    duplver clean   PATH   preview, then (--apply) move duplicates aside
    duplver restore PATH   undo clean
    duplver doctor         check formats, AI model, terminal support

Run with no arguments (e.g. double-clicking Duplver.exe), or drop a folder
onto the executable, for a guided interactive mode.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import webbrowser
from dataclasses import asdict
from pathlib import Path

from duplver import __version__, cleanup, db, report as report_mod
from duplver.config import Settings
from duplver.matching import LABELS, SHORT_LABELS
from duplver.paths import QUARANTINE_DIRNAME, is_frozen, root_key, state_dir
from duplver.pipeline import run_scan
from duplver.results import GroupView, Summary, load_groups, summarize, unreadable_files
from duplver.ui import term
from duplver.ui.live import make_ui
from duplver.ui.term import Style, human_bytes


class UserError(Exception):
    """A problem the user can fix (bad path, no scan yet, …)."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _clean_path_text(text: str) -> str:
    # Drag-and-drop into a console wraps paths in quotes.
    return text.strip().strip('"').strip("'").strip()


def _root(args) -> Path:
    raw = args.path or args.path_opt
    if not raw:
        raise UserError("Missing folder. Example:  duplver scan \"C:\\Photos\"")
    root = Path(_clean_path_text(raw)).expanduser()
    if not root.exists():
        raise UserError(f"Folder not found: {root}")
    if not root.is_dir():
        raise UserError(f"Not a folder: {root}")
    return root.resolve()


def _open_scan(root: Path):
    conn = db.open_existing(root)
    if conn is None:
        raise UserError(f'No scan found for {root}.\nRun first:  duplver scan "{root}"')
    return conn


def _ask_yes(prompt: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {hint} ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes")


def _open_file(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            webbrowser.open(path.resolve().as_uri())
    except Exception:
        print(f"Open it manually: {path}")


def _rel(path: str, root: Path) -> str:
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return path


# ---------------------------------------------------------------------------
# Printing results
# ---------------------------------------------------------------------------

def print_summary(s: Style, root: Path, summary: Summary) -> None:
    print()
    print(s(f" Duplver results · {root}", Style.BOLD))
    print(s(f" last scan {summary.last_scan or '?'} · AI similarity: {summary.ai or 'off'}", Style.DIM))
    print()
    rows = [
        ("Images analysed", f"{summary.images:,}"),
        ("Duplicate groups", f"{summary.dup_groups:,}"),
        ("Duplicate files", f"{summary.dup_files:,}"),
        ("Space you can free", human_bytes(summary.reclaimable)),
        ("Similar-photo groups", f"{summary.similar_groups:,}  (review only, never auto-removed)"),
    ]
    if summary.unreadable:
        rows.append(("Unreadable files", f"{summary.unreadable:,}  (see: duplver results --unreadable)"))
    for label, value in rows:
        print(f"  {label:<22}" + s(value, Style.BOLD))
    if summary.by_relation:
        kinds = "  ·  ".join(f"{SHORT_LABELS.get(r, r)} {n:,}" for r, n in summary.by_relation.items())
        print()
        print("  " + s("By type: ", Style.DIM) + kinds)


def print_groups(s: Style, root: Path, groups: list[GroupView], limit: int | None) -> None:
    width = term.size()[0] - 1
    shown = groups if limit is None else groups[:limit]
    if not shown:
        return
    print()
    info_w, rel_w = 26, 11
    name_w = max(16, width - 9 - rel_w - info_w - 2)
    for i, g in enumerate(shown, 1):
        kinds = ", ".join(SHORT_LABELS.get(r, r) for r in g.relations)
        extra = "similar photos, review" if g.is_similar else f"saves {human_bytes(g.savings)}"
        colour = Style.YELLOW if g.is_similar else Style.CYAN
        head = f" #{i}  {len(g.members)} files  ·  {kinds}  ·  {extra}  ·  {g.confidence * 100:.0f}% sure"
        print(s(term.truncate(head, width), Style.BOLD, colour))
        for m in g.members:
            keep = m.file_id == g.keeper_id
            if keep:
                tag, rel = s(" KEEP ", Style.GREEN, Style.BOLD), "best copy"
            else:
                tag = s(" look ", Style.YELLOW) if g.is_similar else s("  dup ", Style.RED)
                rel = SHORT_LABELS.get(m.relation, m.relation)
            info = f"{m.width}×{m.height} {m.format} {human_bytes(m.size)}"
            print(f"  {tag} {s(term.pad(rel, rel_w), Style.DIM)} "
                  f"{term.pad(_rel(m.path, root), name_w, keep_end=True)} "
                  + s(term.truncate(info, info_w).rjust(info_w), Style.DIM))
        print()
    if limit is not None and len(groups) > limit:
        print(s(f"  … and {len(groups) - limit:,} more groups (use --all, or open the HTML report)", Style.DIM))


def _next_steps(s: Style, root: Path, summary: Summary) -> None:
    if not (summary.dup_groups or summary.similar_groups):
        print(s("\n  No duplicates found.", Style.GREEN))
        return
    q = f'"{root}"'
    print(s("\n  Next:", Style.BOLD))
    print(f"    duplver report {q} --open     visual report with thumbnails")
    print(f"    duplver clean {q}             preview what would be removed")
    print(f"    duplver clean {q} --apply     move duplicates to {QUARANTINE_DIRNAME} (undo: restore)")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_scan(args, s: Style) -> int:
    root = _root(args)
    settings = Settings(threads=args.threads, rescan=args.rescan, use_ai=not args.no_ai)
    if args.similarity is not None:
        settings.similar_threshold = args.similarity
    mode = "silent" if args.json else "plain" if args.plain else "text" if args.no_preview else "live"
    scan = run_scan(root, settings, make_ui(mode, s))

    conn = _open_scan(root)
    groups = load_groups(conn)
    summary = summarize(conn, groups)
    if args.json:
        payload = {"scan": {**asdict(scan), "root": str(root)}, "summary": asdict(summary)}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    print(s(f"\n  Scan finished in {term.duration(scan.elapsed)} "
            f"({scan.analyzed:,} analysed, {scan.cached:,} unchanged from cache"
            + (f", {scan.removed:,} gone since last scan" if scan.removed else "") + ")", Style.DIM))
    print_summary(s, root, summary)
    print_groups(s, root, groups, limit=5)
    _next_steps(s, root, summary)
    return 0


def cmd_results(args, s: Style) -> int:
    root = _root(args)
    conn = _open_scan(root)
    if args.unreadable:
        for path, error in unreadable_files(conn):
            print(f"{path}\n    {error}")
        return 0
    groups = load_groups(conn)
    summary = summarize(conn, groups)
    if args.json:
        print(json.dumps(asdict(summary), indent=2, ensure_ascii=False))
        return 0
    print_summary(s, root, summary)
    print_groups(s, root, groups, limit=None if args.all else args.top)
    _next_steps(s, root, summary)
    return 0


def _write_report(root: Path, fmt: str, output: Path | None, quiet: bool = False) -> Path:
    conn = _open_scan(root)
    groups = load_groups(conn)
    summary = summarize(conn, groups)
    out = output or Path.cwd() / f"duplver-report.{fmt}"
    out = out.expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        report_mod.write_json(out, root, summary, groups)
    elif fmt == "csv":
        report_mod.write_csv(out, groups)
    else:
        tty = term.supports_ansi() and not quiet

        def progress(done: int, total: int) -> None:
            if tty and (done == total or done % 10 == 0):
                sys.stdout.write(f"\r  Building thumbnails… {done:,}/{total:,}")
                sys.stdout.flush()

        report_mod.write_html(out, root, summary, groups, on_progress=progress)
        if tty:
            sys.stdout.write("\r" + term.CLEAR_LINE)
    return out


def cmd_report(args, s: Style) -> int:
    root = _root(args)
    out = _write_report(root, args.format, Path(args.output) if args.output else None)
    print(s("  Report written: ", Style.GREEN) + str(out))
    if args.open:
        _open_file(out)
    return 0


def _print_plan(s: Style, root: Path, items, limit: int = 40) -> None:
    width = term.size()[0] - 1
    for item in items[:limit]:
        why = SHORT_LABELS.get(item.member.relation, item.member.relation)
        line = f"  - {_rel(item.member.path, root)}  [{why}]  keeps {_rel(item.keeper.path, root)}"
        print(term.truncate(line, width))
    if len(items) > limit:
        print(s(f"  … and {len(items) - limit:,} more", Style.DIM))


def _run_clean(conn, root: Path, items, s: Style, *, recycle: bool) -> int:
    def progress(item, problem) -> None:
        if problem:
            print(s(f"  skipped {_rel(item.member.path, root)}: {problem}", Style.YELLOW))

    moved, skipped = cleanup.apply(conn, root, items, recycle=recycle, on_item=progress)
    freed = sum(i.member.size for i in moved)
    where = "the Recycle Bin" if recycle else str(root / QUARANTINE_DIRNAME)
    print(s(f"\n  Moved {len(moved):,} files ({human_bytes(freed)}) to {where}.", Style.GREEN, Style.BOLD))
    if skipped:
        print(s(f"  Skipped {len(skipped):,} files (listed above).", Style.YELLOW))
    if not recycle and moved:
        print(f'  Undo any time:  duplver restore "{root}"')
        print(f"  Happy with the result? Delete the {QUARANTINE_DIRNAME} folder to free the space.")
    return 0 if not skipped else 3


def cmd_clean(args, s: Style) -> int:
    root = _root(args)
    if args.recycle_bin:
        import importlib.util

        if importlib.util.find_spec("send2trash") is None:
            raise UserError(f"Recycle Bin support (send2trash) is not installed. "
                            f"Leave out --recycle-bin to move files to {QUARANTINE_DIRNAME} instead.")
    conn = _open_scan(root)
    groups = load_groups(conn)
    items = cleanup.plan(groups, keep=args.keep, include_similar=args.include_similar)
    if not items:
        print(s("  Nothing to clean — no duplicates in the last scan.", Style.GREEN))
        return 0
    total = sum(i.member.size for i in items)
    print(s(f"\n  {len(items):,} duplicate files, {human_bytes(total)} (keeping: {args.keep})", Style.BOLD))
    _print_plan(s, root, items, limit=len(items) if args.all else 40)

    dest = "the Recycle Bin / Trash" if args.recycle_bin else str(root / QUARANTINE_DIRNAME)
    if not args.apply:
        print(s(f"\n  Preview only — nothing was moved. Add --apply to move these files to {dest}.", Style.YELLOW))
        return 0
    if not args.yes and not _ask_yes(f"\n  Move {len(items):,} files to {dest}?"):
        print("  Cancelled.")
        return 0
    return _run_clean(conn, root, items, s, recycle=args.recycle_bin)


def cmd_restore(args, s: Style) -> int:
    root = _root(args)
    conn = _open_scan(root)
    pending = conn.execute("SELECT COUNT(*) FROM actions WHERE restored = 0").fetchone()[0]
    if not pending:
        print("  Nothing to restore.")
        return 0
    if not args.yes and not _ask_yes(f"  Restore up to {pending:,} removed files to their original folders?", True):
        print("  Cancelled.")
        return 0
    result = cleanup.restore(conn, root)
    print(s(f"  Restored {len(result.restored):,} files.", Style.GREEN, Style.BOLD))
    for path, why in result.skipped:
        print(s(f"  skipped {path}: {why}", Style.YELLOW))
    if result.recycled:
        print(f"  {len(result.recycled):,} files were sent to the Recycle Bin — restore those from the Recycle Bin itself.")
    if result.restored:
        print(f'  Run  duplver scan "{root}"  to refresh the results.')
    return 0


def cmd_doctor(args, s: Style) -> int:
    import importlib.util

    from PIL import Image, features

    from duplver import ai, imaging, verify
    from duplver.imaging import Preview
    from duplver.ui import preview as preview_mod

    ok, warn = s("  ok  ", Style.GREEN, Style.BOLD), s(" warn ", Style.YELLOW, Style.BOLD)

    def line(good: bool, label: str, detail: str) -> None:
        print(f"{ok if good else warn} {label:<22} {detail}")

    print(s(f"\n Duplver {__version__} — environment check\n", Style.BOLD))
    line(True, "Python", sys.version.split()[0] + (" (standalone exe)" if is_frozen() else ""))
    exts = Image.registered_extensions()
    fmts = ["JPEG", "PNG", "GIF", "BMP", "TIFF"]
    fmts += ["WEBP"] if features.check("webp") else []
    fmts += ["AVIF"] if ".avif" in exts else []
    fmts += ["HEIC"] if imaging.HEIF_SUPPORT else []
    line(True, "Image formats", ", ".join(fmts))
    if not imaging.HEIF_SUPPORT:
        line(False, "HEIC (iPhone)", "pillow-heif not installed")
    has_np = importlib.util.find_spec("numpy") is not None
    line(has_np, "numpy", "fast hash matching" if has_np else "missing — matching is slower on big folders")
    line(verify.AVAILABLE, "OpenCV", "crop / edit verification" if verify.AVAILABLE
         else "missing — crops can't be confirmed, edits matched more strictly")
    ai_ok, reason = ai.installed()
    if ai_ok:
        weights = ai.find_weights(Settings())
        line(True, "AI similarity (CLIP)", f"installed · weights: {weights or 'downloaded on first use'}")
    else:
        line(False, "AI similarity (CLIP)", f"{reason} — exact/near/edited detection still works")
    has_trash = importlib.util.find_spec("send2trash") is not None
    line(has_trash, "Recycle Bin support", "available" if has_trash else "send2trash missing (use quarantine)")
    line(True, "State folder", str(state_dir()))
    cols, rows = term.size()
    ansi = term.supports_ansi()
    line(ansi, "Terminal", f"{cols}×{rows}, " + (f"ANSI {term.color_depth()} colour" if ansi
                                                    else "no ANSI — plain progress, no image preview"))
    if ansi:
        w, h = 32, 8
        data = bytearray()
        for y in range(h):
            for x in range(w):
                data += bytes((int(255 * x / (w - 1)), int(255 * y / (h - 1)), 255 - int(255 * x / (w - 1))))
        print("\n  Image preview test (you should see a smooth colour gradient):")
        for row in preview_mod.render(Preview(w, h, bytes(data)), w, h // 2, term.color_depth()):
            print("    " + row)
    print()
    return 0


# ---------------------------------------------------------------------------
# Guided mode (double-click / drag a folder onto the exe)
# ---------------------------------------------------------------------------

def wizard(folder: str | None, s: Style) -> int:
    print(s(f"\n  Duplver {__version__} — duplicate image finder", Style.BOLD, Style.CYAN))
    print(s("  Finds exact copies, converted/resized/cropped/edited versions and similar photos.\n", Style.DIM))
    root = None
    while root is None:
        if folder is None:
            try:
                folder = input("  Drag a folder here (or type its path) and press Enter: ")
            except EOFError:
                return 0
            if not folder.strip():
                return 0
        candidate = Path(_clean_path_text(folder)).expanduser()
        if candidate.is_dir():
            root = candidate.resolve()
        else:
            print(s(f"  Not a folder: {candidate}", Style.RED))
            folder = None

    scan = run_scan(root, Settings(), make_ui("live", s))
    conn = _open_scan(root)
    groups = load_groups(conn)
    summary = summarize(conn, groups)
    print(s(f"\n  Scan finished in {term.duration(scan.elapsed)}.", Style.DIM))
    print_summary(s, root, summary)
    print_groups(s, root, groups, limit=8)

    if groups and _ask_yes("  Open a visual report with thumbnails?", True):
        out = _write_report(root, "html", state_dir() / "reports" / f"{root_key(root)}.html")
        print(s("  Report: ", Style.GREEN) + str(out))
        _open_file(out)

    items = cleanup.plan(groups)
    if items:
        total = sum(i.member.size for i in items)
        print()
        if _ask_yes(f"  Move {len(items):,} duplicate files ({human_bytes(total)}) into "
                    f"{root / QUARANTINE_DIRNAME}? (undo any time with restore)"):
            _run_clean(conn, root, items, s, recycle=False)
    elif not groups:
        print(s("\n  No duplicates found.", Style.GREEN))
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="duplver",
        description="Find, classify and safely clean up duplicate images.",
        epilog="Run without arguments for guided mode. Help for a command: duplver scan -h",
    )
    parser.add_argument("--version", action="version", version=f"duplver {__version__}")
    parser.add_argument("--no-color", action="store_true", help="disable colours and the image preview")
    parser.add_argument("--debug", action="store_true", help="show full tracebacks on errors")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def command(name: str, help_text: str, func) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(func=func)
        return p

    def with_path(p: argparse.ArgumentParser) -> None:
        p.add_argument("path", nargs="?", help="folder with images")
        p.add_argument("-p", "--path", dest="path_opt", metavar="PATH", help=argparse.SUPPRESS)

    p = command("scan", "Find, classify and group duplicate images (live view).", cmd_scan)
    with_path(p)
    p.add_argument("--no-ai", action="store_true", help="skip CLIP similarity (faster, no crop detection)")
    p.add_argument("--rescan", action="store_true", help="ignore the cache and re-analyse every file")
    p.add_argument("--threads", type=int, default=0, metavar="N", help="worker threads (default: auto)")
    p.add_argument("--similarity", type=float, metavar="0-1",
                   help="CLIP threshold for 'similar' groups (default 0.92)")
    p.add_argument("--no-preview", action="store_true", help="live progress without the image preview")
    p.add_argument("--plain", action="store_true", help="simple line-by-line progress (logs, CI)")
    p.add_argument("--json", action="store_true", help="print a JSON summary only")

    p = command("results", "Show what the last scan found.", cmd_results)
    with_path(p)
    p.add_argument("--top", type=int, default=20, metavar="N", help="groups to list (default 20)")
    p.add_argument("--all", action="store_true", help="list every group")
    p.add_argument("--unreadable", action="store_true", help="list files that could not be read")
    p.add_argument("--json", action="store_true", help="print the summary as JSON")

    p = command("report", "Write a report (HTML with thumbnails, CSV or JSON).", cmd_report)
    with_path(p)
    p.add_argument("-f", "--format", choices=report_mod.FORMATS, default="html")
    p.add_argument("-o", "--output", metavar="FILE", help="output file (default: ./duplver-report.<format>)")
    p.add_argument("--open", action="store_true", help="open the report when done")

    p = command("clean", "Preview, then (--apply) move duplicates out of the way.", cmd_clean)
    with_path(p)
    p.add_argument("--apply", action="store_true", help="actually move the files (default: preview only)")
    p.add_argument("--keep", choices=cleanup.KEEP_STRATEGIES, default="best",
                   help="which copy to keep (default: best quality)")
    p.add_argument("--include-similar", action="store_true",
                   help="also remove 'similar' photos, keeping one per group (careful!)")
    p.add_argument("--recycle-bin", action="store_true",
                   help=f"send to the Recycle Bin instead of {QUARANTINE_DIRNAME}")
    p.add_argument("--all", action="store_true", help="list every file in the preview")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    p = command("restore", f"Move files from {QUARANTINE_DIRNAME} back to where they were.", cmd_restore)
    with_path(p)
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    command("doctor", "Check image formats, AI model and terminal support.", cmd_doctor)
    return parser


def _pause_if_double_clicked() -> None:
    """Keep the console window open when the exe was started from Explorer."""
    if is_frozen() and os.name == "nt" and sys.stdin and sys.stdin.isatty():
        try:
            input("\n  Press Enter to close…")
        except EOFError:
            pass


def main(argv: list[str] | None = None) -> int:
    term.configure_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--no-color" in argv:
        term.disable_ansi()
    s = Style(term.supports_ansi())
    debug = "--debug" in argv

    guided = not argv or (len(argv) == 1 and os.path.isdir(_clean_path_text(argv[0])))
    if guided and not argv and not (sys.stdin and sys.stdin.isatty()):
        build_parser().print_help()
        return 0
    code = _dispatch(argv, s, debug, guided)
    if guided:
        _pause_if_double_clicked()
    return code


def _dispatch(argv: list[str], s: Style, debug: bool, guided: bool) -> int:
    try:
        if guided:
            return wizard(argv[0] if argv else None, s)
        args = build_parser().parse_args(argv)
        if not getattr(args, "func", None):
            build_parser().print_help()
            return 0
        return args.func(args, s)
    except KeyboardInterrupt:
        print(s("\n  Interrupted. Finished work is saved — run the same command again to continue.", Style.YELLOW))
        return 130
    except UserError as exc:
        print(s(f"  {exc}", Style.RED))
        return 2
    except Exception as exc:
        print(s(f"  Error: {exc or type(exc).__name__}", Style.RED))
        if debug:
            traceback.print_exc()
        else:
            print(s("  Re-run with --debug for details.", Style.DIM))
        return 1
