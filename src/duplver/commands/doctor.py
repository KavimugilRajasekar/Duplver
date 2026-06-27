"""`duplver doctor` — environment health checks."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import psutil
import typer
from rich.table import Table

from duplver import __version__
from duplver.commands._common import (
    console,
    friendly_error,
    human_bytes,
    is_no_color,
    open_db,
    require_path,
    resolve_root,
)
from duplver.paths import ensure_state_dir
from duplver.workflow import (
    STAGE_ANALYZE_DONE,
    STAGE_CLEANUP_DONE,
    STAGE_CLUSTER_DONE,
    STAGE_SCAN_DONE,
    pipeline_status,
)

app = typer.Typer(help="Diagnose your Duplver installation.", no_args_is_help=False)


# Icon set: Unicode when the terminal supports it, ASCII otherwise.
_UNICODE_ICONS = {
    "ok": "[green]✓[/green]",
    "warn": "[yellow]⚠[/yellow]",
    "fail": "[red]✗[/red]",
}
_ASCII_ICONS = {
    "ok": "[green][OK][/green]",
    "warn": "[yellow][WARN][/yellow]",
    "fail": "[red][FAIL][/red]",
}


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _run()


@friendly_error("duplver doctor")
def _run() -> None:
    """Check Python version, dependencies, GPU availability, and disk space."""
    rows: list[tuple[str, str, str]] = []
    status: dict[str, int] = {"ok": 0, "warn": 0, "fail": 0}

    def add(status_str: str, label: str, detail: str) -> None:
        rows.append((status_str, label, detail))
        status[status_str] += 1

    # Python version.
    py_ok = sys.version_info >= (3, 10)
    add(
        "ok" if py_ok else "fail",
        f"Python {sys.version_info.major}.{sys.version_info.minor}",
        f"{sys.version}",
    )

    # Required deps.
    for mod, friendly in [
        ("rich", "Rich"),
        ("typer", "Typer"),
        ("PIL", "Pillow"),
        ("imagehash", "imagehash"),
        ("cv2", "OpenCV"),
        ("numpy", "NumPy"),
        ("send2trash", "send2trash"),
        ("psutil", "psutil"),
        ("pydantic", "Pydantic"),
        ("yaml", "PyYAML"),
        ("faiss", "faiss"),
        ("open_clip", "open_clip"),
        ("torch", "torch"),
    ]:
        try:
            mod_obj = __import__(mod)
            ver = getattr(mod_obj, "__version__", "—")
            add("ok", friendly, ver)
        except Exception as e:
            add("fail", friendly, str(e) or "not installed")

    # GPU detection (informational, not required).
    gpu_line = "CPU only"
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            gpu_line = f"CUDA available ({torch.cuda.device_count()} device(s))"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            gpu_line = "Apple MPS available"
        add("warn" if "only" in gpu_line else "ok", "GPU", gpu_line)
    except Exception:
        add("warn", "GPU", "torch not importable")

    # Disk space at state dir.
    sd = ensure_state_dir()
    usage = shutil.disk_usage(sd)
    free_pct = usage.free / usage.total * 100 if usage.total else 0
    disk_status = "ok" if free_pct > 5 else "warn" if free_pct > 1 else "fail"
    add(
        disk_status,
        "State directory",
        f"{sd} — {human_bytes(usage.free)} free of {human_bytes(usage.total)}",
    )

    # Send2trash smoke test (Windows-only here).
    if sys.platform.startswith("win"):
        try:
            from send2trash import send2trash  # type: ignore

            tmp = sd / "_doctor_probe.tmp"
            tmp.write_bytes(b"x")
            send2trash(str(tmp))
            if tmp.exists():  # pragma: no cover
                add("warn", "Trash routing", "send2trash returned but file still exists")
            else:
                add("ok", "Trash routing", "Windows Recycle Bin reachable")
        except Exception as e:
            add("warn", "Trash routing", str(e))

    # Render.
    icons = _ASCII_ICONS if is_no_color() else _UNICODE_ICONS
    table = Table(
        title=f"Duplver Doctor — v{__version__}",
        show_header=True,
        header_style="bold blue",
    )
    table.add_column("Status", width=6)
    table.add_column("Check", style="bold")
    table.add_column("Detail")
    for st, label, detail in rows:
        table.add_row(icons[st], label, detail)
    console.print(table)

    if status["fail"]:
        console.print(
            f"\n[red]{status['fail']} check(s) failed.[/red] "
            "Reinstall with: pip install -e ."
        )
        raise typer.Exit(code=1)
    if status["warn"]:
        console.print(
            f"\n[yellow]{status['warn']} warning(s).[/yellow] "
            "Most features will still work."
        )
    else:
        console.print("\n[green]All checks passed.[/green]")


# ---------------------------------------------------------------------------
# `duplver doctor status` — show pipeline workflow state for a root
# ---------------------------------------------------------------------------

@app.command(name="status")
def pipeline_status_cmd(
    path: Path = typer.Option(
        None,
        "--path",
        "-p",
        help="Directory to show pipeline status for.",
    ),
) -> None:
    """Show which pipeline stages have been completed for a directory."""
    resolved = require_path(path, label="doctor status")
    root = resolve_root(resolved)
    _, conn = open_db(root)

    stages = pipeline_status(conn)
    icons = _ASCII_ICONS if is_no_color() else _UNICODE_ICONS

    _STAGE_META = [
        (STAGE_SCAN_DONE,    "1–3 + 7", "scan",    "duplver scan --path {path}"),
        (STAGE_ANALYZE_DONE, "4–6",     "analyze", "duplver analyze --path {path}"),
        (STAGE_CLUSTER_DONE, "7",       "cluster", "duplver cluster --path {path}"),
        (STAGE_CLEANUP_DONE, "—",       "cleanup", "duplver cleanup --path {path} --trash"),
    ]

    table = Table(
        title=f"Pipeline Status — {root}",
        show_header=True,
        header_style="bold blue",
    )
    table.add_column("Done", width=6)
    table.add_column("Stages", style="bold", width=8)
    table.add_column("Command", style="bold cyan")
    table.add_column("Completed at")

    next_cmd: str | None = None
    for key, stage_range, cmd_name, cmd_template in _STAGE_META:
        ts = stages.get(key)
        done = ts is not None
        icon = icons["ok"] if done else icons["warn"]
        cmd_str = f"duplver {cmd_name}"
        ts_display = ts[:19].replace("T", " ") + " UTC" if ts else "[dim]not yet run[/dim]"
        table.add_row(icon, stage_range, cmd_str, ts_display)
        if not done and next_cmd is None:
            next_cmd = cmd_template.format(path=root)

    console.print(table)

    if next_cmd:
        console.print(f"\n[bold]Next step:[/bold] [cyan]{next_cmd}[/cyan]")
    else:
        console.print("\n[green]Full pipeline complete.[/green]")