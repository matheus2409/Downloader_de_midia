"""Resolves a working ffmpeg binary without requiring the user to install
anything by hand.

Preference order:
1. A system ffmpeg already on PATH — respected if present, since it might
   be a newer/differently-configured build the user already relies on.
2. The portable binary bundled by the `imageio-ffmpeg` package, downloaded
   automatically on first use (cached afterwards) — this is what makes
   `pip install -r requirements.txt` enough, with no separate download.
"""
from __future__ import annotations

import functools
import logging
import shutil

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def get_ffmpeg_path() -> str | None:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        logger.info("Usando ffmpeg do sistema: %s", system_ffmpeg)
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        logger.info("Usando ffmpeg embutido (imageio-ffmpeg): %s", bundled)
        return bundled
    except Exception:
        logger.warning("Não foi possível obter um ffmpeg embutido.", exc_info=True)
        return None


def is_available() -> bool:
    return get_ffmpeg_path() is not None
