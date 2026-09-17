"""Cross-references app.log + history.json to flag downloads that most
likely came back as a thumbnail image instead of the video/audio that was
actually asked for — the exact failure mode the fallback chain used to
mask as a plain "success" before allow_image_only existed (see
downloader.py). Read-only and best-effort: it never touches history.json
or the queue, only reads them, and it's fine if it misses something or
flags a false positive — this is a pointer for a human to double-check,
not an automatic fixer.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = APP_DIR / "app.log"

# Domínios que o yt-dlp reconhece como fonte de vídeo/áudio de verdade. Uma
# entrada do histórico com media_type "image" vinda de um desses é um
# forte sinal de que veio da thumbnail em vez do conteúdo pedido — ninguém
# cola o link de um vídeo esperando receber só a capa dele. Não é uma
# lista exaustiva dos ~1800 extractors do yt-dlp, só os mais comuns; serve
# de sinal auxiliar para quando o app.log não cobre o período (rotacionou,
# ou é de antes dessa checagem existir).
#
# Propositalmente NÃO inclui instagram.com: diferente de YouTube/Vimeo/etc,
# lá uma imagem é frequentemente o post inteiro, não uma capa — ainda mais
# agora que existe um motor dedicado pra isso (gallery_engine.py) que sabe
# baixar foto/carrossel de verdade. Sinalizar isso como "suspeito" só por
# causa do domínio viraria falso positivo constante.
KNOWN_VIDEO_DOMAINS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "twitch.tv",
    "tiktok.com", "facebook.com", "fb.watch", "twitter.com",
    "x.com", "soundcloud.com", "bilibili.com", "reddit.com", "v.redd.it",
)

_FALHOU_RE = re.compile(r"yt-dlp falhou para ([0-9a-f]{8}) \((.+?)\): (.*)$")
_FALLBACK_OK_RE = re.compile(r"Fallback funcionou para ([0-9a-f]{8})")


def _task_urls_with_fallback_masking(log_text: str) -> dict[str, str]:
    """Varre o app.log e devolve {task_id: url} das tasks que (1) tomaram
    um erro do yt-dlp indicando site reconhecido (não "Unsupported URL"),
    e (2) mesmo assim terminaram com "Fallback funcionou" — exatamente o
    padrão que allow_image_only passou a barrar dali pra frente."""
    warned_urls: dict[str, str] = {}
    fallback_ok: set[str] = set()

    for line in log_text.split("\n"):
        match = _FALHOU_RE.search(line)
        if match:
            task_id, url, message = match.group(1), match.group(2), match.group(3)
            if "unsupported url" not in message.lower():
                warned_urls[task_id] = url
            continue
        match_ok = _FALLBACK_OK_RE.search(line)
        if match_ok:
            fallback_ok.add(match_ok.group(1))

    return {task_id: url for task_id, url in warned_urls.items() if task_id in fallback_ok}


def _looks_like_video_url(url: str) -> bool:
    return any(domain in url for domain in KNOWN_VIDEO_DOMAINS)


def find_suspicious_downloads(history_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`history_entries`: itens de history.load() já como dict (precisa
    pelo menos de url/title/path/media_type/timestamp). Devolve os que
    parecem ter vindo como thumbnail no lugar do conteúdo pedido, cada um
    com um campo extra `matched_via` ("log" quando confirmado pelo
    app.log, "url" quando é só o domínio bater com um site de vídeo
    conhecido)."""
    log_urls: set[str] = set()
    if LOG_FILE.exists():
        try:
            log_text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
            log_urls = set(_task_urls_with_fallback_masking(log_text).values())
        except OSError:
            log_urls = set()

    suspicious = []
    for entry in history_entries:
        if entry.get("media_type") != "image":
            continue
        url = entry.get("url", "")
        if url in log_urls:
            suspicious.append({**entry, "matched_via": "log"})
        elif _looks_like_video_url(url):
            suspicious.append({**entry, "matched_via": "url"})
    return suspicious
