"""Simple JSON-backed download history."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
HISTORY_FILE = APP_DIR / "history.json"
MAX_ENTRIES = 300


@dataclass
class HistoryEntry:
    title: str
    url: str
    path: str
    media_type: str
    timestamp: str


def load() -> list[HistoryEntry]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [HistoryEntry(**item) for item in data if isinstance(item, dict)]


def save(entries: list[HistoryEntry]) -> None:
    HISTORY_FILE.write_text(
        json.dumps([asdict(e) for e in entries], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def append(title: str, url: str, path: str, media_type: str) -> None:
    entries = load()
    entries.insert(
        0,
        HistoryEntry(
            title=title,
            url=url,
            path=path,
            media_type=media_type,
            timestamp=datetime.now().strftime("%d/%m/%Y %H:%M"),
        ),
    )
    save(entries[:MAX_ENTRIES])


def clear() -> None:
    save([])
