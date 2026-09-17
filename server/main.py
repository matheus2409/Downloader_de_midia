"""Entry point: FastAPI app, static frontend, and the background loop that
keeps server-side state in sync with the download engine's events.

Binds only to 127.0.0.1 — this is a personal local tool with full
filesystem write access, so it must never be reachable from the network.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import api
import state
import uvicorn
from events import bus
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from logging_config import setup_logging

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
HOST = "127.0.0.1"
PORT = 8765

app = FastAPI(title="Downloader de mídias")
app.include_router(api.router)


@app.on_event("startup")
async def on_startup() -> None:
    bus.bind_loop(asyncio.get_running_loop())
    asyncio.create_task(_state_sync_loop())
    _rehydrate_from_disk()
    logger.info("Servidor pronto em http://%s:%s", HOST, PORT)


def _rehydrate_from_disk() -> None:
    """Restores the queue from queue_state.json after a restart.

    The actual background threads/tasks from before the restart are gone
    — there's no way around that — so this reconnects each saved row to a
    *fresh* task object instead:
    - Rows that were still waiting in line ("queued", never actually
      started) are safe to restart automatically: nothing was written to
      disk for them yet, so there's no partial file to worry about.
    - Rows that were actively transferring ("downloading"/"processando")
      are restored as "paused" instead of auto-resumed — a partial file
      likely exists, and silently reopening network connections for
      possibly many items right at boot isn't something that should
      happen without the person noticing. One click on "Retomar tudo"
      picks them back up.
    - Finished rows (done/error/cancelado) are restored as-is, just for
      the list to show what happened — no task object is recreated for
      those, there's nothing left to do with them.
    """
    rows = state.load_from_disk()
    if not rows:
        return

    restored = 0
    for row in rows:
        url = row.get("url")
        status = row.get("status")
        if not url or not status:
            continue

        if status in ("done", "error", "cancelado"):
            state.upsert(
                row.get("task_id") or url,
                url=url,
                folder=row.get("folder"),
                status=status,
                percent=row.get("percent", 100.0),
                media_type=row.get("media_type"),
                path=row.get("path"),
            )
            restored += 1
            continue

        if status in ("agendado", "reagendando"):
            extra_opts = row.get("extra_opts") or {}
            folder = row.get("folder") or ""
            scheduled_for = row.get("scheduled_for")
            task_id, task = api.manager.create_task(url, folder, extra_opts)
            if scheduled_for:
                state.upsert(
                    task_id, url=url, folder=folder, status=status,
                    percent=0.0, extra_opts=extra_opts, scheduled_for=scheduled_for,
                )
                # Se o horário já passou (servidor ficou desligado até depois
                # da hora marcada), a promoção pra fila normal acontece no
                # primeiro ciclo do dispatcher — não perde o agendamento.
                api.manager.schedule(task_id, scheduled_for, status_label=status)
            else:
                # scheduled_for corrompido/ausente — mais seguro colocar na
                # fila normal do que descartar o item.
                state.upsert(
                    task_id, url=url, folder=folder, status="queued", percent=0.0, extra_opts=extra_opts
                )
                api.manager.start(task)
            restored += 1
            continue

        if status not in ("queued", "downloading", "processando", "paused"):
            continue

        extra_opts = row.get("extra_opts") or {}
        folder = row.get("folder") or ""
        task_id, task = api.manager.create_task(url, folder, extra_opts)
        new_status = "queued" if status == "queued" else "paused"
        state.upsert(
            task_id, url=url, folder=folder, status=new_status,
            percent=row.get("percent", 0.0), extra_opts=extra_opts,
        )
        if new_status == "queued":
            api.manager.start(task)
        restored += 1

    if restored:
        logger.info("Fila restaurada de queue_state.json: %d item(ns)", restored)
        state.save_to_disk()


async def _state_sync_loop() -> None:
    """Permanent subscriber that keeps `state.STATE` current regardless of
    whether any browser tab is connected — downloads must keep working
    (and their status must stay correct) with every tab closed."""
    subscriber_queue = bus.subscribe()
    try:
        while True:
            message = await subscriber_queue.get()
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            _apply_event(data)
    finally:
        bus.unsubscribe(subscriber_queue)


def _apply_event(data: dict) -> None:
    event_type = data.get("type")
    task_id = data.get("task_id")
    if not task_id:
        return

    if event_type == "created":
        state.upsert(
            task_id,
            url=data.get("url"),
            folder=data.get("folder"),
            status=data.get("status", "queued"),
            percent=0.0,
            text="",
            **({"scheduled_for": data["scheduled_for"]} if "scheduled_for" in data else {}),
        )
        state.save_to_disk()
        return

    # Todo evento além de "created" só faz sentido para uma linha que ainda
    # existe. Sem essa checagem, remover um item que está baixando ativamente
    # recria um item "fantasma" pouco depois: a remoção já apaga a linha e
    # avisa o front na hora, mas a thread de download só percebe o
    # cancelamento um pouco mais tarde e publica um evento "finished" atrasado
    # para esse mesmo task_id — e como state.upsert() cria a linha se ela não
    # existir, isso ressuscitava o item removido (com status "cancelado") e
    # persistia em queue_state.json, voltando a aparecer numa aba nova ou após
    # reiniciar o servidor.
    if state.get(task_id) is None:
        return

    if event_type == "status":
        state.upsert(
            task_id,
            status=data.get("status"),
            **({"scheduled_for": data["scheduled_for"]} if "scheduled_for" in data else {}),
        )
        state.save_to_disk()
    elif event_type == "progress":
        # Fires many times per second while a download is active — persist
        # the percentage, but skip the disk write here; it'll be caught by
        # the next status/finished event either way.
        state.upsert(task_id, percent=data.get("percent", 0.0), text=data.get("text", ""))
    elif event_type == "thumbnail":
        state.upsert(task_id, thumbnail=data.get("data_url"))
    elif event_type == "file_ready":
        row = state.get(task_id) or {}
        path = data.get("path")
        media_type = api.record_file_ready(task_id, path, row.get("url", ""))
        state.upsert(task_id, path=path, media_type=media_type)
        state.save_to_disk()
    elif event_type == "finished":
        success = data.get("success")
        message = data.get("message", "")
        if success:
            state.upsert(task_id, status="done", percent=100.0)
            _fire_webhook_async(task_id, "done")
        elif message == "pausado":
            state.upsert(task_id, status="paused")
        elif message == "cancelado":
            state.upsert(task_id, status="cancelado", percent=100.0)
        else:
            friendly = api.translate_error(message)
            state.upsert(task_id, status="error", percent=100.0, text=friendly)
            logger.warning("Download %s falhou: %s", task_id, friendly)
            _fire_webhook_async(task_id, "error", message=friendly)
        state.save_to_disk()
    elif event_type == "removed":
        state.remove(task_id)
        state.save_to_disk()


def _fire_webhook_async(task_id: str, status: str, message: str = "") -> None:
    """Avisa uma URL externa (configurável em Configurações) quando um
    download termina — pensado pra outra ferramenta (ex.: um workflow do
    n8n) reagir sem precisar ficar checando a API. Só dispara pra
    sucesso/erro de verdade, não pra pausa/cancelamento (esses são ação do
    usuário, não um evento de pipeline). Roda numa thread solta pra não
    travar o loop de eventos com uma chamada de rede síncrona."""
    webhook_url = api.settings.webhook_url
    if not webhook_url:
        return
    row = state.get(task_id) or {}
    payload = {
        "task_id": task_id,
        "url": row.get("url"),
        "status": status,
        "path": row.get("path"),
        "media_type": row.get("media_type"),
        "message": message,
    }
    threading.Thread(target=_post_webhook, args=(webhook_url, payload), daemon=True).start()


def _post_webhook(webhook_url: str, payload: dict) -> None:
    try:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(request, timeout=10).close()
    except Exception as exc:
        logger.warning("Webhook falhou (%s): %s", webhook_url, exc)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    subscriber_queue = bus.subscribe()
    try:
        while True:
            message = await subscriber_queue.get()
            await websocket.send_text(message)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(subscriber_queue)


# Mounted last so it doesn't shadow the /api and /ws routes registered above.
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def _open_browser_when_ready() -> None:
    time.sleep(1.0)
    try:
        webbrowser.open(f"http://{HOST}:{PORT}")
    except Exception:
        logger.info("Não deu para abrir o navegador automaticamente — acesse http://%s:%s", HOST, PORT)


def main() -> None:
    setup_logging()
    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
