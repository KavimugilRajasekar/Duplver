"""`duplver benchmark` — performance tests.

**Status: stubbed in v0.1.** When implemented, this command will run
synthetic benchmarks for SHA-256 throughput, perceptual hashing, and
fake embedding generation, then report MB/s and files/s on this machine.
"""
from __future__ import annotations

import typer

from duplver.commands._common import print_not_implemented

app = typer.Typer(help="Run performance benchmarks.", no_args_is_help=False)


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _run()


def _run() -> None:
    """Run the benchmark suite."""
    print_not_implemented(
        "duplver benchmark",
        "Benchmarking",
        "Synthetic benchmarks for hashing/embedding throughput are coming in v0.2.",
    )