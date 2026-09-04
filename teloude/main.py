# teloude/main.py
"""Teloude entry point: `python -m teloude.main [--offline] [--data-dir DIR]`."""
import sys

from teloude.ui.app import run


if __name__ == "__main__":
    sys.exit(run())
