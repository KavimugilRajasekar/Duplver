"""Rich-based logger.

Stages and commands call ``log.info(...)`` etc. instead of print(). When
the Rich progress widget is active, log lines render above the bars; when
not, they go straight to the console.
"""
from __future__ import annotations

from rich.console import Console
from rich.logging import RichHandler

import logging


def make_logger(name: str = "duplver", level: str = "INFO") -> logging.Logger:
    """Return a configured logger. Safe to call multiple times."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    handler = RichHandler(
        console=Console(
            stderr=True,
            legacy_windows=False,
            safe_box=True,
            force_terminal=None,
        ),
        show_path=False,
        show_time=False,
        markup=True,
        rich_tracebacks=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


log = make_logger()