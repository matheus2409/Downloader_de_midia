"""Server-side snapshot of every known download. Kept in sync with the
event bus (see main.py's state_sync_loop) independently of whether any
browser tab is connected, so downloads keep going — and their state stays
correct — even with every tab closed. A freshly opened tab hydrates from
GET /api/downloads instead of starting blank.

Also persisted to disk (queue_state.json), so the whole list — not just
history of completed files — survives a server restart. See main.py's
rehydrate_from_disk() for how items get reconnected to the download
engine after a restart.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = APP_DIR / "queue_state.json"

TERMINAL_STATUSES = {"done", "error", "cancelado"}

# Only these fields are written to disk — things like "text" (a live speed
# string) or a data: URL thumbnail churn too often and aren't needed to
# reconstruct the list after a restart.
PERSISTED_FIELDS = (
    "task_id", "url", "folder", "status", "percent", "media_type", "path", "extra_opts", "scheduled_for",
)

STATE: dict[str, dict] = {}


def upsert(task_id: str, **fields) -> None:
    row = STATE.setdefault(task_id, {"task_id": task_id})
    row.update(fields)


def remove(task_id: str) -> None:
    STATE.pop(task_id, None)


def get(task_id: str) -> dict | None:
    return STATE.get(task_id)


def snapshot() -> list[dict]:
    return list(STATE.values())


def active_urls() -> set[str]:
    """URLs currently in the list and not yet finished — used for
    duplicate detection when adding new links."""
    return {row["url"] for row in STATE.values() if row.get("status") not in TERMINAL_STATUSES}


def ids_with_status(*statuses: str) -> list[str]:
    return [tid for tid, row in STATE.items() if row.get("status") in statuses]


def clear_finished() -> list[str]:
    done_ids = ids_with_status(*TERMINAL_STATUSES)
    for task_id in done_ids:
        STATE.pop(task_id, None)
    return done_ids


# ------------------------------------------------------------- persistence
def save_to_disk() -> None:
    try:
        rows = [{k: row.get(k) for k in PERSISTED_FIELDS if k in row} for row in STATE.values()]
        STATE_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("Não deu para salvar queue_state.json", exc_info=True)


def load_from_disk() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("queue_state.json corrompido ou ilegível, ignorando.", exc_info=True)
        return []
