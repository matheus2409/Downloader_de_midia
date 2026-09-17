"""Detects when a pasted link points at a collection (a YouTube/SoundCloud
playlist, a Vimeo album, a Pinterest board...) instead of a single item,
and expands it into the list of individual item URLs it contains.

Three strategies, tried in order:
1. yt-dlp's own playlist support (`extract_flat`) — reliable, covers
   every site yt-dlp already understands as a playlist/channel/album.
2. gallery-dl's own Pinterest extractor (see gallery_engine.list_items) —
   actively maintained upstream, so it tracks Pinterest's internal format
   changes without needing a patch here every time it moves.
3. A hand-rolled Pinterest board walker, kept as a last resort in case a
   Pinterest redesign breaks gallery-dl's extractor too before upstream
   catches up. This one is inherently fragile — Pinterest is a JS-rendered
   app with an undocumented internal API, and this walks that API by hand.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from typing import Any, Callable

import gallery_engine
import yt_dlp
from quiet_logger import SILENT

logger = logging.getLogger(__name__)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DownloaderDeMidias/2.0"
PINTEREST_HOST_HINTS = ("pinterest.", "pin.it")


def _resolver_link_encurtado(url: str, depth: int = 0) -> str:
    """
    Desempacota links encurtados de forma recursiva, seguindo a cadeia
    de redirecionamentos (ex: pin.it -> api.pinterest.com -> br.pinterest.com).
    Limite de 5 saltos para evitar loops infinitos.
    """
    if depth > 5:
        return url
        
    url_lower = url.lower()
    # Verifica se é um encurtador ou o redirecionador da API do Pinterest
    if "pin.it" not in url_lower and "api.pinterest.com" not in url_lower:
        return url
        
    try:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        
        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        
        html = ""
        try:
            resp = opener.open(req, timeout=15)
            html = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            # Captura redirecionamento direto via HTTP Headers
            if e.code in (301, 302, 303, 307, 308):
                location = e.headers.get("Location")
                if location:
                    # Chama a si mesma com o próximo elo da corrente
                    return _resolver_link_encurtado(location, depth + 1)
            try:
                html = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
        
        # Captura redirecionamento via HTML (Meta Refresh ou Canonical)
        if html:
            match_refresh = re.search(
                r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^;]+;\s*url=([^"\']+)["\']',
                html, re.I,
            )
            if match_refresh:
                return _resolver_link_encurtado(match_refresh.group(1), depth + 1)

            match_canon = re.search(
                r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html, re.I
            )
            if match_canon:
                return _resolver_link_encurtado(match_canon.group(1), depth + 1)

            match_og = re.search(
                r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', html, re.I
            )
            if match_og:
                return _resolver_link_encurtado(match_og.group(1), depth + 1)
                
    except Exception as exc:
        logger.warning("Falha técnica ao resolver o link encurtado %s: %s", url, exc)

    return url


def _looks_like_pinterest(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(hint in host for hint in PINTEREST_HOST_HINTS)


def _expand_via_ytdlp(url: str) -> list[str] | None:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": SILENT,
        "extract_flat": True,
        "skip_download": True,
        "ignore_no_formats_error": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return None
    if not info or "entries" not in info:
        return None
    urls = []
    for entry in info["entries"]:
        if not entry:
            continue
        link = entry.get("url") or entry.get("webpage_url")
        if link:
            urls.append(link)
    if urls:
        logger.info("yt-dlp encontrou %d item(ns) em %s", len(urls), url)
    return urls or None


def _best_pin_image(pin: dict) -> str | None:
    images = pin.get("images") or {}
    for key in ("orig", "736x", "600x", "474x", "236x"):
        candidate = images.get(key)
        if isinstance(candidate, dict) and candidate.get("url"):
            return candidate["url"]
    return None


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


_EMBEDDED_DATA_PATTERNS = [
    re.compile(r'<script[^>]+id=["\']__PWS_DATA__["\'][^>]*>(.*?)</script>', re.S),
    re.compile(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S),
    re.compile(r'<script[^>]+id=["\']initial-state["\'][^>]*>(.*?)</script>', re.S),
]
_PINIMG_PATTERN = re.compile(r'https://i\.pinimg\.com/[^"\'\s\\]+\.(?:jpg|jpeg|png|gif|webp)', re.I)


def _fetch_board_html(url: str) -> str | None:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            status = getattr(response, "status", None) or response.getcode()
            html = response.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.warning("Pinterest: não consegui baixar a página da pasta %s: %s", url, exc)
        return None
    logger.info("Pinterest: página da pasta baixada (status %s, %d bytes)", status, len(html))
    return html


def _parse_embedded_pins(html: str, url: str):
    """Tries each known "app state embedded in the page" format in turn."""
    data = None
    matched_pattern = None
    for pattern in _EMBEDDED_DATA_PATTERNS:
        match = pattern.search(html)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
            matched_pattern = pattern.pattern
            break
        except json.JSONDecodeError as exc:
            logger.debug("Pinterest: achei um bloco de dados mas não deu para decodificar como JSON: %s", exc)

    if data is None:
        logger.warning(
            "Pinterest: não encontrei nenhum dos formatos de dados embutidos conhecidos na página de %s "
            "— o site provavelmente mudou de estrutura.", url,
        )
        return None
    logger.debug("Pinterest: dados embutidos encontrados via padrão %s", matched_pattern)

    pin_urls: list[str] = []
    board_id: str | None = None
    bookmark: str | None = None
    for node in _walk(data):
        if not isinstance(node, dict):
            continue
        if node.get("type") == "pin" and node.get("id"):
            image_url = _best_pin_image(node)
            if image_url:
                pin_urls.append(image_url)
        elif board_id is None and node.get("type") == "board" and node.get("id"):
            board_id = str(node["id"])
        if bookmark is None and isinstance(node.get("bookmark"), str):
            bookmark = node["bookmark"]

    logger.info(
        "Pinterest: %d pin(s) no carregamento inicial, board_id=%s, bookmark=%s",
        len(pin_urls), board_id, "presente" if bookmark else "ausente",
    )

    if not pin_urls and not board_id:
        logger.warning(
            "Pinterest: dados embutidos encontrados, mas nenhum pin ou board_id dentro deles — "
            "o formato interno dos objetos provavelmente mudou."
        )
        return None
    return pin_urls, bookmark, board_id


def _regex_scrape_pin_images(html: str) -> list[str]:
    """Last-resort fallback that doesn't try to understand the page's data
    structure at all."""
    found = _PINIMG_PATTERN.findall(html)
    by_filename: dict[str, str] = {}
    for image_url in found:
        filename = image_url.rsplit("/", 1)[-1]
        current = by_filename.get(filename)
        if current is None or ("/originals/" in image_url and "/originals/" not in current):
            by_filename[filename] = image_url
    return list(by_filename.values())


def _pinterest_next_page(board_id: str, bookmark: str):
    options = {"board_id": board_id, "bookmarks": [bookmark], "field_set_key": "grid_item"}
    query = urllib.parse.urlencode({"data": json.dumps({"options": options})})
    url = f"https://www.pinterest.com/resource/BoardFeedResource/get/?{query}"
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    except Exception:
        return None

    items = (payload.get("resource_response") or {}).get("data") or []
    urls = [
        _best_pin_image(item)
        for item in items
        if isinstance(item, dict) and item.get("type") == "pin"
    ]
    urls = [u for u in urls if u]
    bookmarks = ((payload.get("resource") or {}).get("options") or {}).get("bookmarks") or [None]
    return urls, bookmarks[0]


def _expand_via_gallery_dl(url: str, on_progress: Callable[[str], None] | None) -> list[str] | None:
    if not gallery_engine.available():
        return None
    items = gallery_engine.list_items(url)
    if not items:
        return None
    logger.info("gallery-dl encontrou %d item(ns) em %s", len(items), url)
    if on_progress:
        on_progress(f"{len(items)} pins encontrados na pasta...")
    return items


def _expand_pinterest_board(url: str, on_progress: Callable[[str], None] | None) -> list[str] | None:
    """Última tentativa, só usada se nem o yt-dlp nem o gallery-dl (ver
    _expand_via_gallery_dl) conseguirem nada — ver expand_sync."""
    html = _fetch_board_html(url)
    if html is None:
        return None

    seed = _parse_embedded_pins(html, url)
    if seed is not None:
        urls, bookmark, board_id = seed
        seen = set(urls)
        ordered = list(urls)
        guard = 0
        while bookmark and bookmark != "-end-" and board_id and guard < 300:
            guard += 1
            batch = _pinterest_next_page(board_id, bookmark)
            if not batch:
                break
            new_urls, bookmark = batch
            if not new_urls:
                break
            for image_url in new_urls:
                if image_url not in seen:
                    seen.add(image_url)
                    ordered.append(image_url)
            if on_progress:
                on_progress(f"{len(ordered)} pins encontrados na pasta...")
        if ordered:
            logger.info(
                "Pasta do Pinterest %s: %d imagem(ns) encontrada(s) via dados embutidos", url, len(ordered)
            )
            return ordered
        logger.warning(
            "Pinterest: board_id/bookmark encontrados mas nenhuma imagem veio deles — "
            "tentando fallback bruto."
        )

    regex_urls = _regex_scrape_pin_images(html)
    if regex_urls:
        logger.info(
            "Pasta do Pinterest %s: %d imagem(ns) via fallback de regex no HTML", url, len(regex_urls)
        )
        if on_progress:
            on_progress(f"{len(regex_urls)} pins encontrados (modo básico)...")
        return regex_urls

    logger.error("Pinterest: nenhuma estratégia encontrou pins para %s — ver avisos acima no log.", url)
    return None


def expand_sync(url: str, on_progress: Callable[[str], None] | None = None) -> list[str]:
    """Blocking. Returns a single-item list for an ordinary link, or many
    for a recognized playlist/board."""
    
    url = _resolver_link_encurtado(url)
    
    entries: list[str] | None = None
    try:
        entries = _expand_via_ytdlp(url)
        if entries is None and _looks_like_pinterest(url):
            logger.info("Pinterest: tentando gallery-dl para %s", url)
            entries = _expand_via_gallery_dl(url, on_progress)
            if entries is None:
                logger.info("Pinterest: gallery-dl não achou nada, tentando scraper de pasta próprio")
                entries = _expand_pinterest_board(url, on_progress)
    except Exception as exc:
        logger.warning("Falha ao expandir %s: %s", url, exc)
        entries = None
    return entries or [url]


def expand_async(
    url: str,
    on_expanded: Callable[[str, list[str]], None],
    on_progress: Callable[[str, str], None] | None = None,
) -> None:
    """Runs expand_sync() on a background thread and calls on_expanded(url,
    urls) when done — never blocks the caller."""

    def worker():
        progress_cb = (lambda msg: on_progress(url, msg)) if on_progress else None
        urls = expand_sync(url, progress_cb)
        on_expanded(url, urls)

    threading.Thread(target=worker, daemon=True).start()