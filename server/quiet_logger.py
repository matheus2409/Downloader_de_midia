"""A logger object handed to yt_dlp.YoutubeDL so its own error/warning
messages stop being printed straight to the terminal (which made every
probe attempt — even ones we already handle gracefully — look like a
crash) and instead flow through our own logging, into app.log."""
from __future__ import annotations

import logging

_yt_dlp_logger = logging.getLogger("yt_dlp")


class YtdlpSilentLogger:
    def debug(self, msg: str) -> None:
        _yt_dlp_logger.debug(msg)

    def info(self, msg: str) -> None:
        _yt_dlp_logger.debug(msg)

    def warning(self, msg: str) -> None:
        _yt_dlp_logger.debug("warning: %s", msg)

    def error(self, msg: str) -> None:
        _yt_dlp_logger.debug("error: %s", msg)


SILENT = YtdlpSilentLogger()
