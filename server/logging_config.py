"""Rotating file logger, so a crash or a bad download leaves a trail that
can be inspected after the fact instead of only appearing in a terminal
that's already gone."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = APP_DIR / "app.log"


def setup_logging(level: int = logging.INFO) -> None:
    handler = RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    # Keep noisy third-party loggers from flooding the file.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
