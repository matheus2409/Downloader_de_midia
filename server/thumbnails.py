"""Fetches small preview thumbnails on background threads and pushes them
out as data: URLs over the event bus — simple to consume from the browser,
no separate image-serving endpoint needed."""
from __future__ import annotations

import base64
import logging
import threading
import urllib.request

import yt_dlp
from events import bus
from quiet_logger import SILENT

logger = logging.getLogger(__name__)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DownloaderDeMidias/2.0"


def _looks_like_image(url: str) -> bool:
    return url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"))


def _find_thumbnail_url(url: str) -> str | None:
    if _looks_like_image(url):
        return url
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "logger": SILENT,
            "skip_download": True,
            "ignore_no_formats_error": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return None
    if not info:
        return None
    thumb = info.get("thumbnail")
    if thumb:
        return thumb
    direct = info.get("url") or ""
    return direct if _looks_like_image(direct) else None


def _fetch_bytes(image_url: str, limit: int = 2_000_000) -> tuple[bytes, str] | None:
    try:
        request = urllib.request.Request(image_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=10) as response:
            content_type = response.headers.get_content_type()
            if not content_type.startswith("image/"):
                return None
            return response.read(limit), content_type
    except Exception:
        return None


def fetch_async(task_id: str, url: str) -> None:
    """Fire-and-forget: publishes a "thumbnail" event with a data: URL once
    (and if) a preview image is found. Silently gives up otherwise — a
    missing preview is never treated as an error."""

    def worker():
        image_url = _find_thumbnail_url(url)
        if not image_url:
            return
        result = _fetch_bytes(image_url)
        if not result:
            return
        data, content_type = result
        data_url = f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"
        bus.publish("thumbnail", task_id=task_id, data_url=data_url)

    threading.Thread(target=worker, daemon=True).start()
