"""`duplver config` — show or initialize the user configuration."""
from __future__ import annotations

from pathlib import Path

import typer
import yaml
from rich.tree import Tree

from duplver.commands._common import console, friendly_error
from duplver.config import config_path, load_settings, save_settings

app = typer.Typer(help="Show or edit configuration.", no_args_is_help=False)


@app.command(name="show")
def show() -> None:
    """Print the active configuration as a Rich tree."""
    settings = load_settings()
    cp = config_path(settings.state_dir)

    tree = Tree(f"[bold blue]Duplver config[/bold blue] ({cp})")
    grp = tree.add("[bold]threads[/bold]")
    grp.add(f"value = {settings.threads} (0 = auto, {settings.resolved_threads()} detected)")
    grp = tree.add("[bold]perceptual[/bold]")
    grp.add(f"hash_size = {settings.perceptual_hash_size}")
    grp.add(f"max_distance = {settings.perceptual_max_distance}")
    grp = tree.add("[bold]clip[/bold]")
    grp.add(f"model = {settings.clip_model}")
    grp.add(f"pretrained = {settings.clip_pretrained}")
    grp.add(f"batch_size = {settings.batch_size}")
    grp = tree.add("[bold]memory[/bold]")
    grp.add(f"max_memory_mb = {settings.max_memory_mb}")
    grp = tree.add("[bold]paths[/bold]")
    grp.add(f"state_dir = {settings.state_dir}")
    grp = tree.add("[bold]cleanup[/bold]")
    grp.add(f"default_quality_keep = {settings.default_quality_keep}")
    grp.add(f"trash_verbosity = {settings.trash_verbosity}")
    grp = tree.add("[bold]logging[/bold]")
    grp.add(f"log_level = {settings.log_level}")
    console.print(tree)
    if not cp.exists():
        console.print(
            f"\n[dim]No config file at {cp} yet. Run "
            f"'duplver config init' to write one.[/dim]"
        )


@app.command(name="init")
def init(
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite existing."),
) -> None:
    """Write a default config.yaml to the state directory."""
    cp = config_path()
    if cp.exists() and not force:
        console.print(
            f"[yellow]Config already exists at {cp}. Use --force to overwrite.[/yellow]"
        )
        raise typer.Exit(code=1)
    settings = load_settings()
    write_path = save_settings(settings)
    console.print(f"[green]OK[/green] Wrote default config to {write_path}")


@app.command(name="validate")
@friendly_error("duplver config validate")
def validate(
    path: Path = typer.Option(
        None,
        "--path",
        "-p",
        help="Specific config.yaml to validate (default: active one).",
    ),
) -> None:
    """Parse the user's config.yaml and report per-key validity.

    Exits 0 if every key is recognized and well-typed; exits 1 if any
    key is unknown or has the wrong type. Useful as a pre-flight check
    before `duplver config init` is replaced by an editor flow.
    """
    cp = path or config_path()
    if not cp.exists():
        console.print(
            f"[yellow]No config file at {cp}.[/yellow]\n"
            "Run 'duplver config init' to write one, then validate."
        )
        raise typer.Exit(code=1)

    try:
        raw = yaml.safe_load(cp.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        console.print(f"[red]YAML parse error in {cp}:[/red] {e}")
        raise typer.Exit(code=1)

    if not isinstance(raw, dict):
        console.print(f"[red]{cp}: top-level must be a mapping, got {type(raw).__name__}.[/red]")
        raise typer.Exit(code=1)

    from duplver.config import Settings

    known = set(Settings.model_fields.keys())
    issues: list[str] = []

    for key in sorted(raw):
        if key not in known:
            issues.append(f"unknown key: {key!r}")
            continue
        # Per-key validation by constructing a Settings with only this key
        # and letting Pydantic raise. We isolate failures so a single bad
        # value doesn't poison the rest of the file.
        try:
            Settings(**{key: raw[key]})
        except Exception as e:
            issues.append(f"{key!r}: {e}")

    if issues:
        console.print(f"[red]{len(issues)} issue(s) in {cp}:[/red]")
        for line in issues:
            console.print(f"  → {line}")
        raise typer.Exit(code=1)

    console.print(f"[green]OK[/green] {cp} is valid ({len(raw)} key(s)).")


@app.callback(invoke_without_command=True)
def _main_callback(ctx: typer.Context) -> None:
    """Default to `duplver config show` when no subcommand is given."""
    if ctx.invoked_subcommand is None:
        show()