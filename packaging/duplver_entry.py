"""PyInstaller entry point (absolute imports; the package's __main__ uses the same code)."""
import multiprocessing
import sys

from duplver.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
