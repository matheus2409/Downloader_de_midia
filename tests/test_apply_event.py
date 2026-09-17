import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import main  # noqa: E402
import state  # noqa: E402


def setup_function():
    state.STATE.clear()


def _use_tmp_state_file(monkeypatch, tmp_path):
    # _apply_event chama state.save_to_disk() a cada evento — sem isso o
    # teste escreveria por cima do queue_state.json de verdade do projeto.
    monkeypatch.setattr(state, "STATE_FILE", tmp_path / "queue_state.json")


def test_finished_event_does_not_resurrect_a_removed_row(tmp_path, monkeypatch):
    _use_tmp_state_file(monkeypatch, tmp_path)

    main._apply_event({"type": "created", "task_id": "x", "url": "https://x.com", "status": "queued"})
    assert state.get("x") is not None

    # O endpoint de remoção apaga a linha e avisa o front na hora; a thread
    # de download só percebe o cancelamento um instante depois e publica um
    # "finished" atrasado para o mesmo task_id.
    state.remove("x")
    assert state.get("x") is None

    main._apply_event({"type": "finished", "task_id": "x", "success": False, "message": "cancelado"})

    assert state.get("x") is None


def test_finished_event_after_removal_is_ignored_even_on_success(tmp_path, monkeypatch):
    _use_tmp_state_file(monkeypatch, tmp_path)

    main._apply_event({"type": "created", "task_id": "z", "url": "https://z.com", "status": "queued"})
    state.remove("z")

    main._apply_event({"type": "finished", "task_id": "z", "success": True, "message": ""})

    assert state.get("z") is None


def test_status_event_updates_an_existing_row(tmp_path, monkeypatch):
    _use_tmp_state_file(monkeypatch, tmp_path)

    main._apply_event({"type": "created", "task_id": "y", "url": "https://y.com", "status": "queued"})
    main._apply_event({"type": "status", "task_id": "y", "status": "downloading"})

    assert state.get("y")["status"] == "downloading"


def test_progress_event_for_unknown_task_is_a_no_op(tmp_path, monkeypatch):
    _use_tmp_state_file(monkeypatch, tmp_path)

    main._apply_event({"type": "progress", "task_id": "never-created", "percent": 50.0})

    assert state.get("never-created") is None


def test_created_and_status_events_propagate_scheduled_for(tmp_path, monkeypatch):
    _use_tmp_state_file(monkeypatch, tmp_path)

    main._apply_event({
        "type": "created", "task_id": "s1", "url": "https://site.com/v",
        "status": "agendado", "scheduled_for": 1234.0,
    })
    assert state.get("s1")["scheduled_for"] == 1234.0
    assert state.get("s1")["status"] == "agendado"

    main._apply_event({"type": "status", "task_id": "s1", "status": "reagendando", "scheduled_for": 5678.0})
    assert state.get("s1")["scheduled_for"] == 5678.0
    assert state.get("s1")["status"] == "reagendando"


class _ImmediateThread:
    """Substitui threading.Thread nos testes de webhook — roda o alvo na
    hora, na mesma thread, em vez de disparar uma thread de verdade. Deixa
    o teste determinístico (sem precisar de time.sleep pra 'esperar' a
    thread real terminar)."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        self._target(*self._args, **self._kwargs)


def test_webhook_fires_on_successful_finish(tmp_path, monkeypatch):
    _use_tmp_state_file(monkeypatch, tmp_path)
    monkeypatch.setattr(main.api.settings, "webhook_url", "https://example.com/hook")
    monkeypatch.setattr(main.threading, "Thread", _ImmediateThread)
    calls = []
    monkeypatch.setattr(main, "_post_webhook", lambda url, payload: calls.append((url, payload)))

    main._apply_event({"type": "created", "task_id": "w1", "url": "https://site.com/v", "status": "queued"})
    main._apply_event({"type": "finished", "task_id": "w1", "success": True, "message": ""})

    assert len(calls) == 1
    assert calls[0][0] == "https://example.com/hook"
    assert calls[0][1]["status"] == "done"
    assert calls[0][1]["url"] == "https://site.com/v"


def test_webhook_fires_on_real_failure_with_translated_message(tmp_path, monkeypatch):
    _use_tmp_state_file(monkeypatch, tmp_path)
    monkeypatch.setattr(main.api.settings, "webhook_url", "https://example.com/hook")
    monkeypatch.setattr(main.threading, "Thread", _ImmediateThread)
    calls = []
    monkeypatch.setattr(main, "_post_webhook", lambda url, payload: calls.append((url, payload)))

    main._apply_event({"type": "created", "task_id": "w2", "url": "https://site.com/v", "status": "queued"})
    main._apply_event({"type": "finished", "task_id": "w2", "success": False, "message": "http error 404"})

    assert len(calls) == 1
    assert calls[0][1]["status"] == "error"
    assert "404" in calls[0][1]["message"]


def test_webhook_does_not_fire_on_pause_or_cancel(tmp_path, monkeypatch):
    _use_tmp_state_file(monkeypatch, tmp_path)
    monkeypatch.setattr(main.api.settings, "webhook_url", "https://example.com/hook")
    monkeypatch.setattr(main.threading, "Thread", _ImmediateThread)
    calls = []
    monkeypatch.setattr(main, "_post_webhook", lambda url, payload: calls.append((url, payload)))

    main._apply_event({"type": "created", "task_id": "w3", "url": "https://site.com/v", "status": "queued"})
    main._apply_event({"type": "finished", "task_id": "w3", "success": False, "message": "pausado"})
    main._apply_event({"type": "created", "task_id": "w4", "url": "https://site.com/v2", "status": "queued"})
    main._apply_event({"type": "finished", "task_id": "w4", "success": False, "message": "cancelado"})

    assert calls == []


def test_webhook_does_not_fire_when_url_not_configured(tmp_path, monkeypatch):
    _use_tmp_state_file(monkeypatch, tmp_path)
    monkeypatch.setattr(main.api.settings, "webhook_url", "")
    monkeypatch.setattr(main.threading, "Thread", _ImmediateThread)
    calls = []
    monkeypatch.setattr(main, "_post_webhook", lambda url, payload: calls.append((url, payload)))

    main._apply_event({"type": "created", "task_id": "w5", "url": "https://site.com/v", "status": "queued"})
    main._apply_event({"type": "finished", "task_id": "w5", "success": True, "message": ""})

    assert calls == []
