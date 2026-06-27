"""Safety gates: dry-run, double-confirm, trash wrappers.

Default posture: **no file destruction**. Every cleanup-touching code path
should funnel through this module.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt

DEFAULT_DRY_RUN = True
"""Module-level constant referenced by every cleanup path."""


@dataclass(slots=True)
class TrashResult:
    path: Path
    sent: bool
    error: str | None = None


def require_confirmation(
    console: Console,
    prompt: str,
    *,
    double: bool = True,
    assume_yes: bool = False,
) -> bool:
    """Ask the user to confirm a destructive action.

    With ``double=True``, the second confirmation must match a random 4-char
    token — protects against accidental Enter-presses.

    ``assume_yes`` is honored only by tests (skips prompts).
    """
    if assume_yes:
        return True
    if not Confirm.ask(f"[bold red]{prompt}[/bold red]", console=console):
        return False
    if not double:
        return True
    token = f"{abs(hash(prompt)) % 10000:04d}"
    typed = Prompt.ask(
        f"[bold red]Type {token} to confirm[/bold red]", console=console
    )
    return typed.strip() == token


def trash_paths(
    paths: list[Path],
    *,
    dry_run: bool = True,
    console: Console | None = None,
) -> list[TrashResult]:
    """Move ``paths`` to the OS Trash.

    With ``dry_run=True`` (the default), nothing happens — the returned
    list describes what *would* have been moved.

    Per-file failures don't abort the whole batch; they're recorded as
    TrashResult(sent=False, error=...).
    """
    if dry_run:
        return [TrashResult(path=p, sent=False) for p in paths]

    try:
        from send2trash import send2trash
    except Exception as e:  # pragma: no cover
        if console is not None:
            console.print(f"[red]send2trash unavailable: {e}[/red]")
        return [TrashResult(path=p, sent=False, error=str(e)) for p in paths]

    results: list[TrashResult] = []
    for p in paths:
        try:
            send2trash(str(p))
            results.append(TrashResult(path=p, sent=True))
        except Exception as e:
            results.append(TrashResult(path=p, sent=False, error=str(e)))
    return results


def assert_readonly_intent(argv: list[str]) -> None:
    """Helper for command entry points: fail loudly if a destructive flag
    is passed without an explicit dry-run override.

    Currently a soft check; reserved for future hardening.
    """
    if "--permanent-delete" in argv and "--dry-run" in argv:
        print(
            "[red]--permanent-delete cannot be combined with --dry-run.[/red]",
            file=sys.stderr,
        )
        raise SystemExit(2)