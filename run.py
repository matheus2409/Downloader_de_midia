#!/usr/bin/env python3
"""Convenience launcher — run this from the project root:

    python run.py

Starts the local server and opens it in your default browser at
http://127.0.0.1:8765. Leave the terminal window open; closing it stops
the server (and any downloads in progress).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))

from main import main  # noqa: E402

if __name__ == "__main__":
    main()
