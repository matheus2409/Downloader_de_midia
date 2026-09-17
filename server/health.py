"""Checagens de "saúde" do app — pensadas pra virar um aviso único e
acionável, em vez de a pessoa ter que notar um padrão espalhado em
dezenas de itens de fila individuais.

Duas checagens, mesma filosofia read-only/best-effort de diagnostics.py:
nunca propaga erro pra quem chamou, só reporta o que deu pra apurar.

1. count_recent_login_required_failures / recent_login_required_failure_count
   — conta quantos downloads terminaram em erro definitivo, recentemente,
   por causa de login/bot-check (ex.: YouTube "Sign in to confirm you're
   not a bot"). É hoje a causa isolada mais comum de falha (ver app.log).

2. check_ytdlp_update / check_gallery_dl_update — compara a versão
   instalada de cada motor com a mais recente publicada no PyPI. Tanto
   YouTube quanto Instagram/Pinterest mudam de layout/detecção de bot com
   frequência, e só uma versão atualizada do motor correspondente
   acompanha essas mudanças; motor desatualizado é, na prática, a segunda
   causa mais comum de falha depois de cookies não configurados.
"""
from __future__ import annotations

import datetime
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

import gallery_engine
from errors import is_login_required

APP_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = APP_DIR / "app.log"

PYPI_YTDLP_URL = "https://pypi.org/pypi/yt-dlp/json"
PYPI_GALLERY_DL_URL = "https://pypi.org/pypi/gallery-dl/json"
# Não teria sentido bater no PyPI de novo a cada refresh de aba — nenhum
# dos dois motores lança versão nova a cada poucas horas.
_YTDLP_CHECK_TTL_SECONDS = 6 * 60 * 60
_GALLERY_DL_CHECK_TTL_SECONDS = 6 * 60 * 60

_TIMESTAMPED_FAILURE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} ERROR \[downloader\] "
    r"Todas as estratégias falharam para [0-9a-f]{8}: (.*)$"
)

_ytdlp_cache: dict[str, Any] | None = None
_ytdlp_cache_at: float = 0.0
_gallery_dl_cache: dict[str, Any] | None = None
_gallery_dl_cache_at: float = 0.0


# --------------------------------------------------- login-required count
def count_recent_login_required_failures(
    log_text: str,
    *,
    within_hours: float = 24.0,
    now: datetime.datetime | None = None,
) -> int:
    """Varre `log_text` (conteúdo de app.log) e conta quantas falhas
    definitivas ("Todas as estratégias falharam") nas últimas
    `within_hours` horas bateram no padrão de login/bot-check. `now` é
    injetável pra teste; em produção usa o relógio local (mesma
    referência de tempo que %(asctime)s grava no log)."""
    if now is None:
        now = datetime.datetime.now()
    cutoff = now - datetime.timedelta(hours=within_hours)

    count = 0
    for line in log_text.replace("\r\n", "\n").split("\n"):
        match = _TIMESTAMPED_FAILURE_RE.search(line)
        if not match:
            continue
        timestamp_str, message = match.groups()
        try:
            ts = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if is_login_required(message):
            count += 1
    return count


def recent_login_required_failure_count(within_hours: float = 24.0) -> int:
    """Wrapper best-effort que lê LOG_FILE do disco — 0 se o arquivo não
    existir ou não puder ser lido, em vez de propagar erro (mesmo espírito
    de diagnostics.find_suspicious_downloads)."""
    if not LOG_FILE.exists():
        return 0
    try:
        log_text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return count_recent_login_required_failures(log_text, within_hours=within_hours)


# --------------------------------------------------------- yt-dlp version
def _version_tuple(version: str) -> tuple[int, ...]:
    """'2026.07.04' -> (2026, 7, 4). Ignora sufixos não-numéricos (ex.:
    builds 'nightly') extraindo só os dígitos de cada parte, pra
    comparação funcionar mesmo com formatos ligeiramente diferentes."""
    parts = []
    for chunk in version.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_ytdlp_update(
    *,
    timeout: float = 4.0,
    use_cache: bool = True,
    installed_version: str | None = None,
) -> dict[str, Any]:
    """Devolve {"installed", "latest", "outdated"}. `installed_version`
    permite injetar a versão instalada (usado em teste); por padrão lê de
    `yt_dlp.version.__version__` (import feito aqui dentro, não no topo
    do módulo, pra health.py continuar importável mesmo em um ambiente
    sem yt_dlp instalado). Qualquer etapa que falhar (PyPI fora do ar,
    sem internet, yt_dlp não instalável) vira None no campo
    correspondente em vez de propagar erro — best-effort."""
    global _ytdlp_cache, _ytdlp_cache_at

    now = time.time()
    if use_cache and _ytdlp_cache is not None and (now - _ytdlp_cache_at) < _YTDLP_CHECK_TTL_SECONDS:
        return _ytdlp_cache

    installed = installed_version
    if installed is None:
        try:
            import yt_dlp

            installed = yt_dlp.version.__version__
        except Exception:
            installed = None

    latest: str | None = None
    try:
        request = urllib.request.Request(
            PYPI_YTDLP_URL, headers={"User-Agent": "Downloader-de-midias"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        latest = data.get("info", {}).get("version")
    except Exception:
        latest = None

    outdated = None
    if installed and latest:
        outdated = _version_tuple(installed) < _version_tuple(latest)

    result = {"installed": installed, "latest": latest, "outdated": outdated}
    _ytdlp_cache, _ytdlp_cache_at = result, now
    return result


# ----------------------------------------------------- gallery-dl version
def check_gallery_dl_update(
    *,
    timeout: float = 4.0,
    use_cache: bool = True,
    installed_version: str | None = None,
) -> dict[str, Any]:
    """Mesma ideia de check_ytdlp_update, pro motor gallery-dl (ver
    gallery_engine.py — Instagram/Pinterest). Cache próprio e separado do
    yt-dlp de propósito: TTLs iguais hoje, mas cada motor pode evoluir a
    frequência de checagem sem afetar o outro."""
    global _gallery_dl_cache, _gallery_dl_cache_at

    now = time.time()
    if (
        use_cache
        and _gallery_dl_cache is not None
        and (now - _gallery_dl_cache_at) < _GALLERY_DL_CHECK_TTL_SECONDS
    ):
        return _gallery_dl_cache

    installed = installed_version if installed_version is not None else gallery_engine.installed_version()

    latest: str | None = None
    try:
        request = urllib.request.Request(
            PYPI_GALLERY_DL_URL, headers={"User-Agent": "Downloader-de-midias"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        latest = data.get("info", {}).get("version")
    except Exception:
        latest = None

    outdated = None
    if installed and latest:
        outdated = _version_tuple(installed) < _version_tuple(latest)

    result = {"installed": installed, "latest": latest, "outdated": outdated}
    _gallery_dl_cache, _gallery_dl_cache_at = result, now
    return result
