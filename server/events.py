"""Tiny pub/sub bus. Download work happens on plain background threads;
this is how they push updates out to whatever browser tabs are connected
via WebSocket, without the download engine needing to know anything about
FastAPI or WebSockets."""
from __future__ import annotations

import asyncio
import json
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once, from the FastAPI startup event, so publish() — which
        may run on any worker thread — knows which event loop actually owns
        the WebSocket connections."""
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event_type: str, **payload: Any) -> None:
        """Thread-safe: callable from any worker thread, not just the
        asyncio loop's own thread."""
        if self._loop is None:
            return
        message = json.dumps({"type": event_type, **payload}, ensure_ascii=False, default=str)
        for queue in list(self._subscribers):
            self._loop.call_soon_threadsafe(queue.put_nowait, message)


bus = EventBus()
