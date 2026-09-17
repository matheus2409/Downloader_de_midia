import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import downloader  # noqa: E402


def _manager(monkeypatch, max_concurrent=2):
    monkeypatch.setattr(downloader, "get_ffmpeg_path", lambda: None)
    return downloader.DownloadManager(max_concurrent=max_concurrent)


def _wait_until(predicate, timeout=2.0, interval=0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_schedule_keeps_task_out_of_pending_until_due(monkeypatch):
    mgr = _manager(monkeypatch)
    task_id, task = mgr.create_task("https://example.com/a", "/tmp", {})
    monkeypatch.setattr(task, "run", lambda: None)  # nunca baixa de verdade

    mgr.schedule(task_id, time.time() + 100)

    assert task_id in mgr._scheduled
    assert task_id not in mgr._pending


def test_schedule_promotes_to_pending_once_due(monkeypatch):
    mgr = _manager(monkeypatch)
    task_id, task = mgr.create_task("https://example.com/b", "/tmp", {})
    monkeypatch.setattr(task, "run", lambda: None)

    mgr.schedule(task_id, time.time() + 0.1)

    assert _wait_until(lambda: task_id not in mgr._scheduled), "deveria ter saido de _scheduled"


def test_start_now_skips_the_wait(monkeypatch):
    mgr = _manager(monkeypatch)
    task_id, task = mgr.create_task("https://example.com/c", "/tmp", {})
    monkeypatch.setattr(task, "run", lambda: None)

    mgr.schedule(task_id, time.time() + 100)
    assert mgr.start_now(task_id) is True
    assert task_id not in mgr._scheduled


def test_start_now_returns_false_for_unscheduled_task(monkeypatch):
    mgr = _manager(monkeypatch)
    assert mgr.start_now("nao-existe") is False

    task_id, task = mgr.create_task("https://example.com/d", "/tmp", {})
    assert mgr.start_now(task_id) is False  # existe mas nao esta agendado


def test_remove_clears_pending_schedule(monkeypatch):
    mgr = _manager(monkeypatch)
    task_id, task = mgr.create_task("https://example.com/e", "/tmp", {})

    mgr.schedule(task_id, time.time() + 100)
    mgr.remove(task_id)

    assert task_id not in mgr._scheduled
    assert task_id not in mgr._tasks


def test_pause_on_scheduled_item_clears_schedule_and_publishes_paused(monkeypatch):
    mgr = _manager(monkeypatch)
    task_id, task = mgr.create_task("https://example.com/f", "/tmp", {})

    published = []
    monkeypatch.setattr(downloader.bus, "publish", lambda *a, **k: published.append(k))

    mgr.schedule(task_id, time.time() + 100)
    mgr.pause(task_id)

    assert task_id not in mgr._scheduled
    assert any(k.get("message") == "pausado" for k in published)


def test_run_task_reschedules_when_run_returns_a_retry_timestamp(monkeypatch):
    mgr = _manager(monkeypatch, max_concurrent=1)
    task_id, task = mgr.create_task("https://example.com/g", "/tmp", {})

    retry_at = time.time() + 100
    monkeypatch.setattr(task, "run", lambda: retry_at)

    mgr._run_task(task)

    assert mgr._scheduled.get(task_id) == retry_at


def test_run_task_does_not_reschedule_on_plain_completion(monkeypatch):
    mgr = _manager(monkeypatch, max_concurrent=1)
    task_id, task = mgr.create_task("https://example.com/h", "/tmp", {})
    monkeypatch.setattr(task, "run", lambda: None)

    mgr._run_task(task)

    assert task_id not in mgr._scheduled


def test_resume_with_extra_opts_updates_the_task_before_reenqueueing(monkeypatch):
    """Regressão do bug: 'Tentar de novo os que falharam' reusava as
    opções (cookies) congeladas na criação da task, então configurar
    cookies depois de uma falha de login e clicar em retry repetia o
    mesmo erro. resume() agora aceita extra_opts e aplica por cima."""
    mgr = _manager(monkeypatch)
    task_id, task = mgr.create_task("https://example.com/i", "/tmp", {"cookiefile": "antigo.txt"})

    mgr.resume(task_id, {"cookiefile": "novo.txt"})

    assert task._opts["cookiefile"] == "novo.txt"
    assert task_id in mgr._pending, "deveria ter sido reenfileirada"


def test_resume_without_extra_opts_leaves_existing_opts_untouched(monkeypatch):
    mgr = _manager(monkeypatch)
    task_id, task = mgr.create_task("https://example.com/j", "/tmp", {"cookiefile": "antigo.txt"})

    mgr.resume(task_id)

    assert task._opts["cookiefile"] == "antigo.txt"
    assert task_id in mgr._pending


def test_resume_on_unknown_task_id_does_not_raise(monkeypatch):
    mgr = _manager(monkeypatch)
    mgr.resume("nao-existe", {"cookiefile": "novo.txt"})  # não deve levantar exceção
