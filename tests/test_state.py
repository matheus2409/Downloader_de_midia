import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import state  # noqa: E402


def setup_function():
    state.STATE.clear()


def test_upsert_creates_and_updates_row():
    state.upsert("abc", url="https://x.com", status="queued")
    assert state.get("abc")["url"] == "https://x.com"
    state.upsert("abc", status="downloading")
    assert state.get("abc")["status"] == "downloading"


def test_active_urls_excludes_terminal_statuses():
    state.upsert("a", url="https://x.com/1", status="downloading")
    state.upsert("b", url="https://x.com/2", status="done")
    assert state.active_urls() == {"https://x.com/1"}


def test_clear_finished_removes_only_terminal_rows():
    state.upsert("a", status="downloading")
    state.upsert("b", status="done")
    state.upsert("c", status="error")
    removed = state.clear_finished()
    assert set(removed) == {"b", "c"}
    assert "a" in state.STATE
    assert "b" not in state.STATE


def test_save_to_disk_then_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "queue_state.json")
    state.upsert(
        "a", url="https://x.com/1", folder="/tmp/x", status="downloading",
        percent=42.0, extra_opts={"format": "bv*+ba/b"},
    )
    state.save_to_disk()

    rows = state.load_from_disk()
    assert len(rows) == 1
    assert rows[0]["task_id"] == "a"
    assert rows[0]["url"] == "https://x.com/1"
    assert rows[0]["percent"] == 42.0
    assert rows[0]["extra_opts"] == {"format": "bv*+ba/b"}
    # transient/live-only fields must not leak into the persisted file
    assert "text" not in rows[0]
    assert "thumbnail" not in rows[0]


def test_load_from_disk_missing_file_returns_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "does-not-exist.json")
    assert state.load_from_disk() == []


def test_load_from_disk_corrupted_file_returns_empty_list(tmp_path, monkeypatch):
    broken = tmp_path / "queue_state.json"
    broken.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(state, "STATE_FILE", broken)
    assert state.load_from_disk() == []
