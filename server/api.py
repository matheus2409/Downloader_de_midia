"""REST endpoints. Every action that changes a download's state (pause,
resume, remove, prioritize, adding new links) is a plain thread-safe call
into the download manager — the actual UI update always flows out through
the WebSocket, never in the HTTP response, so every connected tab (or a
tab that reconnects later) sees the same thing."""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import diagnostics
import health
import history
import state
from config import SUBTITLE_OPTS, SUPPORTED_COOKIE_BROWSERS, Settings
from downloader import DownloadManager
from errors import friendly_message
from events import bus
from fastapi import APIRouter, Depends, Header, HTTPException
from ffmpeg_provider import is_available as ffmpeg_is_available
from media_types import classify
from playlist import expand_async
from pydantic import BaseModel
from thumbnails import fetch_async

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

settings = Settings.load()
manager = DownloadManager(settings.max_concurrent)

MIN_FREE_SPACE_BYTES = 500 * 1024 * 1024


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Só entra em ação quando uma chave é configurada em Configurações —
    por padrão (chave vazia) o endpoint continua livre, igual sempre foi.
    Pensado pra outra ferramenta (ex.: n8n) se identificar de propósito ao
    chamar a API, não como uma segurança forte — o servidor já só escuta
    em 127.0.0.1 de qualquer forma."""
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(401, "Chave de API ausente ou inválida (cabeçalho X-API-Key).")


class AddDownloadsRequest(BaseModel):
    urls: list[str]
    folder: str
    preset: str
    subtitles: bool = False
    scheduled_for: str | None = None  # ISO 8601 local, ex. "2026-08-14T02:00"


class SettingsUpdate(BaseModel):
    output_dir: str | None = None
    max_concurrent: int | None = None
    last_preset: str | None = None
    cookies_from_browser: str | None = None
    cookies_file: str | None = None
    api_key: str | None = None
    webhook_url: str | None = None


class PresetUpdate(BaseModel):
    name: str
    options: dict[str, Any]


class OpenFolderRequest(BaseModel):
    path: str


# ------------------------------------------------------------- downloads
@router.get("/downloads")
def list_downloads():
    return {"downloads": state.snapshot()}


@router.post("/downloads", dependencies=[Depends(require_api_key)])
def add_downloads(payload: AddDownloadsRequest):
    folder = payload.folder.strip()
    if not folder:
        raise HTTPException(400, "Escolha uma pasta de destino.")
    Path(folder).mkdir(parents=True, exist_ok=True)

    scheduled_for: float | None = None
    if payload.scheduled_for:
        try:
            scheduled_for = datetime.fromisoformat(payload.scheduled_for).timestamp()
        except ValueError:
            raise HTTPException(400, "Data/hora de agendamento inválida.")

    extra_opts = dict(settings.presets.get(payload.preset, {}))
    extra_opts.update(settings.ytdlp_auth_opts())
    if payload.subtitles:
        extra_opts.update(SUBTITLE_OPTS)

    for url in payload.urls:
        url = url.strip()
        if not url:
            continue
        expand_async(
            url, on_expanded=lambda orig, urls: _on_expanded(orig, urls, folder, extra_opts, scheduled_for)
        )

    settings.output_dir = folder
    settings.last_preset = payload.preset
    settings.save()
    return {"accepted": len(payload.urls)}


def _on_expanded(
    original_url: str, urls: list[str], folder: str, extra_opts: dict, scheduled_for: float | None = None
) -> None:

    already = state.active_urls()
    added = 0
    for url in urls:
        if url in already:
            continue
        already.add(url)
        task_id, task = manager.create_task(url, folder, extra_opts)
        if scheduled_for is not None:
            state.upsert(
                task_id, url=url, folder=folder, status="agendado", percent=0.0, text="",
                extra_opts=extra_opts, scheduled_for=scheduled_for,
            )
            bus.publish(
                "created", task_id=task_id, url=url, folder=folder,
                status="agendado", scheduled_for=scheduled_for,
            )
            manager.schedule(task_id, scheduled_for)
        else:
            state.upsert(
                task_id, url=url, folder=folder, status="queued", percent=0.0, text="", extra_opts=extra_opts
            )
            bus.publish("created", task_id=task_id, url=url, folder=folder, status="queued")
            manager.start(task)
        fetch_async(task_id, url)
        added += 1

    if len(urls) > 1:
        bus.publish("toast", message=f"{added} item(ns) adicionados de uma pasta/playlist ({original_url})")
        logger.info("Pasta/playlist %s expandida: %d item(ns)", original_url, len(urls))


@router.post("/downloads/{task_id}/pause")
def pause_download(task_id: str):
    manager.pause(task_id)
    return {"ok": True}


@router.post("/downloads/{task_id}/resume")
def resume_download(task_id: str):

    manager.resume(task_id, settings.ytdlp_auth_opts())
    state.upsert(task_id, status="queued")
    bus.publish("status", task_id=task_id, status="queued")
    return {"ok": True}


@router.post("/downloads/{task_id}/start-now")
def start_now_download(task_id: str):
    """Pula a espera de um item agendado ou aguardando nova tentativa
    automática e começa na hora."""
    ok = manager.start_now(task_id)
    if ok:
        bus.publish("status", task_id=task_id, status="queued")
    return {"ok": ok}


@router.post("/downloads/{task_id}/remove")
def remove_download(task_id: str):

    manager.remove(task_id)
    state.remove(task_id)
    bus.publish("removed", task_id=task_id)
    return {"ok": True}


@router.post("/downloads/{task_id}/prioritize")
def prioritize_download(task_id: str):
    ok = manager.prioritize(task_id)
    return {"ok": ok}


@router.post("/downloads/pause_all")
def pause_all():
    for task_id in state.ids_with_status("queued", "downloading", "processando", "agendado", "reagendando"):
        manager.pause(task_id)
    return {"ok": True}


@router.post("/downloads/resume_all")
def resume_all():

    auth_opts = settings.ytdlp_auth_opts()
    for task_id in state.ids_with_status("paused"):
        manager.resume(task_id, auth_opts)
        state.upsert(task_id, status="queued")
        bus.publish("status", task_id=task_id, status="queued")
    return {"ok": True}


@router.post("/downloads/retry_failed")
def retry_failed():
    """Reenfileira tudo que está com status "error" — inclusive
    reaplicando settings.ytdlp_auth_opts() atual (ver
    DownloadManager.resume), então se a falha original era login/cookies
    e a pessoa acabou de configurar isso em 🔑 Login/Cookies, este botão
    já usa a configuração nova em vez de repetir o mesmo erro."""
    auth_opts = settings.ytdlp_auth_opts()
    ids = state.ids_with_status("error")
    for task_id in ids:
        manager.resume(task_id, auth_opts)
        state.upsert(task_id, status="queued")
        bus.publish("status", task_id=task_id, status="queued")
    return {"ok": True, "count": len(ids)}


@router.post("/downloads/clear_finished")
def clear_finished():

    ids = state.clear_finished()
    for task_id in ids:
        manager.forget(task_id)
        bus.publish("removed", task_id=task_id)
    return {"ok": True, "count": len(ids)}


# -------------------------------------------------------------- settings
@router.get("/settings")
def get_settings():
    return {
        "output_dir": settings.output_dir,
        "max_concurrent": settings.max_concurrent,
        "last_preset": settings.last_preset,
        "presets": settings.presets,
        "ffmpeg_available": ffmpeg_is_available(),
        "cookies_from_browser": settings.cookies_from_browser,
        "cookies_file": settings.cookies_file,
        "supported_cookie_browsers": list(SUPPORTED_COOKIE_BROWSERS),
        "api_key": settings.api_key,
        "webhook_url": settings.webhook_url,
    }


@router.post("/settings")
def update_settings(payload: SettingsUpdate):
    if payload.output_dir is not None:
        settings.output_dir = payload.output_dir
    if payload.max_concurrent is not None:
        settings.max_concurrent = max(1, payload.max_concurrent)
        manager.set_max_concurrent(settings.max_concurrent)
    if payload.last_preset is not None:
        settings.last_preset = payload.last_preset
    if payload.cookies_from_browser is not None:
        settings.cookies_from_browser = payload.cookies_from_browser.strip()
    if payload.cookies_file is not None:
        settings.cookies_file = payload.cookies_file.strip()
    if payload.api_key is not None:
        settings.api_key = payload.api_key.strip()
    if payload.webhook_url is not None:
        settings.webhook_url = payload.webhook_url.strip()
    settings.save()
    return {"ok": True}


@router.get("/disk-space")
def disk_space(folder: str):
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        Path(folder).mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(folder).free
    return {"free_bytes": free, "low": free < MIN_FREE_SPACE_BYTES}


# --------------------------------------------------------------- presets
@router.post("/presets")
def save_preset(payload: PresetUpdate):
    settings.presets[payload.name] = payload.options
    settings.save()
    return {"ok": True}


@router.delete("/presets/{name}")
def delete_preset(name: str):
    if len(settings.presets) <= 1:
        raise HTTPException(400, "Precisa sobrar pelo menos um preset.")
    settings.presets.pop(name, None)
    if settings.last_preset == name:
        settings.last_preset = next(iter(settings.presets))
    settings.save()
    return {"ok": True}


# --------------------------------------------------------------- history
@router.get("/history")
def get_history():
    return {"entries": [entry.__dict__ for entry in history.load()]}


@router.delete("/history")
def clear_history():
    history.clear()
    return {"ok": True}


# ----------------------------------------------------------- diagnostics
@router.get("/diagnostics/suspicious-downloads")
def suspicious_downloads():
    """Cruza history.json com app.log pra apontar itens que provavelmente
    vieram como thumbnail no lugar do vídeo/áudio pedido — o padrão que a
    correção do fallback (allow_image_only) passou a impedir dali pra
    frente, mas que pode ter deixado itens desse jeito no histórico de
    antes da correção."""
    entries = [entry.__dict__ for entry in history.load()]
    return {"items": diagnostics.find_suspicious_downloads(entries)}


@router.get("/diagnostics/health")
def diagnostics_health():
    """Resumo pra decidir se vale mostrar um aviso pro usuário assim que
    a página abre: quantos downloads recentes falharam por exigir login
    (hoje, a causa isolada mais comum de falha — normalmente YouTube
    pedindo "Sign in to confirm you're not a bot"), e se o yt-dlp e/ou o
    gallery-dl instalados estão desatualizados (YouTube e Instagram/
    Pinterest mudam a detecção de bot com frequência; só versão nova do
    motor correspondente acompanha)."""
    return {
        "login_required_count": health.recent_login_required_failure_count(),
        "ytdlp": health.check_ytdlp_update(),
        "gallery_dl": health.check_gallery_dl_update(),
    }


# ----------------------------------------------------------------- misc
@router.get("/browse")
def browse(path: str | None = None):
    """Minimal server-side folder listing so the browser can offer a
    "choose folder" picker without a native file dialog."""
    base = Path(path) if path else Path.home()
    if not base.exists() or not base.is_dir():
        base = Path.home()
    try:
        dirs = sorted(
            [p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")],
            key=str.lower,
        )
    except PermissionError:
        dirs = []
    return {"path": str(base), "parent": str(base.parent) if base.parent != base else None, "dirs": dirs}


@router.post("/open-folder")
def open_folder(payload: OpenFolderRequest):
    target = Path(payload.path)
    folder = target.parent if target.is_file() else target
    if not folder.exists():
        raise HTTPException(404, "Pasta não encontrada.")
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", str(folder)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception as exc:
        raise HTTPException(500, f"Não deu para abrir a pasta: {exc}")
    return {"ok": True}


def record_file_ready(task_id: str, path: str, url: str) -> str:
    """Called from main.py's state-sync loop when a file finishes
    downloading — classifies it and writes the history entry, since this
    is the one place both the task_id→url mapping (via state) and the
    final path are both available."""
    media_type = classify(Path(path).suffix)
    title = Path(path).stem
    history.append(title, url, path, media_type)
    return media_type


def translate_error(message: str) -> str:
    return friendly_message(message)
