import datetime
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import health  # noqa: E402

FAKE_LOG = (
    "2026-08-20 01:16:44,351 WARNING [main] baixando item x\r\n"
    "2026-08-20 01:16:46,999 ERROR [downloader] Todas as estratégias falharam para aaaaaaaa: "
    "ERROR: [youtube] x: Sign in to confirm you're not a bot.\r\n"
    "2026-08-20 01:16:48,462 ERROR [downloader] Todas as estratégias falharam para bbbbbbbb: "
    "ERROR: [youtube] x: Sign in to confirm you're not a bot.\r\n"
    # falha por outro motivo -- não deveria contar
    "2026-08-20 01:16:49,000 ERROR [downloader] Todas as estratégias falharam para cccccccc: "
    "ERROR: HTTP Error 404: Not Found\r\n"
    # dentro do padrão de login, mas fora de uma janela curta de horas
    "2026-08-19 09:00:00,000 ERROR [downloader] Todas as estratégias falharam para dddddddd: "
    "ERROR: [youtube] x: Sign in to confirm you're not a bot.\r\n"
)

NOW = datetime.datetime(2026, 8, 20, 12, 0, 0)


class _FakeJSONResponse:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ------------------------------------------------- login-required count
def test_counts_login_required_failures_within_window():
    count = health.count_recent_login_required_failures(FAKE_LOG, within_hours=24, now=NOW)
    assert count == 2


def test_ignores_failures_outside_the_time_window():
    # com janela de 1h, nenhuma das duas falhas de bot-check (01:16 do
    # dia 20, e "now" é meio-dia do dia 20) entra
    count = health.count_recent_login_required_failures(FAKE_LOG, within_hours=1, now=NOW)
    assert count == 0


def test_ignores_non_login_failures():
    only_404 = (
        "2026-08-20 01:16:49,000 ERROR [downloader] Todas as estratégias falharam para cccccccc: "
        "ERROR: HTTP Error 404: Not Found\r\n"
    )
    assert health.count_recent_login_required_failures(only_404, within_hours=24, now=NOW) == 0


def test_ignores_lines_that_do_not_match_the_final_failure_pattern():
    noise = (
        "2026-08-20 01:16:44,351 WARNING [main] Sign in to confirm you're "
        "not a bot (só um aviso solto)\r\n"
    )
    assert health.count_recent_login_required_failures(noise, within_hours=24, now=NOW) == 0


def test_recent_login_required_failure_count_reads_log_file(tmp_path, monkeypatch):
    # usa "agora" de verdade (menos frágil que mockar datetime.datetime.now
    # globalmente) -- a falha fica "5 minutos atrás", bem dentro da janela
    now = datetime.datetime.now()
    recent_ts = (now - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    log_text = (
        f"{recent_ts},000 ERROR [downloader] Todas as estratégias falharam para aaaaaaaa: "
        "ERROR: [youtube] x: Sign in to confirm you're not a bot.\r\n"
    )
    log_file = tmp_path / "app.log"
    log_file.write_text(log_text, encoding="utf-8")
    monkeypatch.setattr(health, "LOG_FILE", log_file)
    assert health.recent_login_required_failure_count(within_hours=24) == 1


def test_recent_login_required_failure_count_missing_file_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "LOG_FILE", tmp_path / "does-not-exist.log")
    assert health.recent_login_required_failure_count() == 0


# --------------------------------------------------------- yt-dlp version
def test_check_ytdlp_update_detects_outdated_version(monkeypatch):
    monkeypatch.setattr(
        health.urllib.request,
        "urlopen",
        lambda request, timeout=None: _FakeJSONResponse({"info": {"version": "2026.7.4"}}),
    )
    result = health.check_ytdlp_update(use_cache=False, installed_version="2024.1.10")
    assert result == {"installed": "2024.1.10", "latest": "2026.7.4", "outdated": True}


def test_check_ytdlp_update_recognizes_up_to_date(monkeypatch):
    monkeypatch.setattr(
        health.urllib.request,
        "urlopen",
        lambda request, timeout=None: _FakeJSONResponse({"info": {"version": "2026.7.4"}}),
    )
    result = health.check_ytdlp_update(use_cache=False, installed_version="2026.7.4")
    assert result["outdated"] is False


def test_check_ytdlp_update_compares_zero_padded_versions_correctly(monkeypatch):
    """'2026.7.4' vs '2026.07.04' são a mesma data -- comparação por
    tupla de inteiros evita o erro de comparar como string."""
    monkeypatch.setattr(
        health.urllib.request,
        "urlopen",
        lambda request, timeout=None: _FakeJSONResponse({"info": {"version": "2026.07.04"}}),
    )
    result = health.check_ytdlp_update(use_cache=False, installed_version="2026.7.4")
    assert result["outdated"] is False


def test_check_ytdlp_update_handles_network_failure_gracefully(monkeypatch):
    def _boom(request, timeout=None):
        raise urllib.error.URLError("sem conexão")

    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    result = health.check_ytdlp_update(use_cache=False, installed_version="2024.1.10")
    assert result == {"installed": "2024.1.10", "latest": None, "outdated": None}


def test_check_ytdlp_update_uses_cache_within_ttl(monkeypatch):
    health._ytdlp_cache = None
    health._ytdlp_cache_at = 0.0
    calls = []

    def _urlopen(request, timeout=None):
        calls.append(1)
        return _FakeJSONResponse({"info": {"version": "2026.7.4"}})

    monkeypatch.setattr(health.urllib.request, "urlopen", _urlopen)
    health.check_ytdlp_update(use_cache=True, installed_version="2024.1.10")
    health.check_ytdlp_update(use_cache=True, installed_version="2024.1.10")
    assert len(calls) == 1, "a segunda chamada dentro do TTL deveria ter usado o cache"


# ----------------------------------------------------- gallery-dl version
def test_check_gallery_dl_update_detects_outdated_version(monkeypatch):
    monkeypatch.setattr(
        health.urllib.request,
        "urlopen",
        lambda request, timeout=None: _FakeJSONResponse({"info": {"version": "1.32.10"}}),
    )
    result = health.check_gallery_dl_update(use_cache=False, installed_version="1.30.0")
    assert result == {"installed": "1.30.0", "latest": "1.32.10", "outdated": True}


def test_check_gallery_dl_update_recognizes_up_to_date(monkeypatch):
    monkeypatch.setattr(
        health.urllib.request,
        "urlopen",
        lambda request, timeout=None: _FakeJSONResponse({"info": {"version": "1.32.10"}}),
    )
    result = health.check_gallery_dl_update(use_cache=False, installed_version="1.32.10")
    assert result["outdated"] is False


def test_check_gallery_dl_update_handles_network_failure_gracefully(monkeypatch):
    def _boom(request, timeout=None):
        raise urllib.error.URLError("sem conexão")

    monkeypatch.setattr(health.urllib.request, "urlopen", _boom)
    result = health.check_gallery_dl_update(use_cache=False, installed_version="1.30.0")
    assert result == {"installed": "1.30.0", "latest": None, "outdated": None}


def test_check_gallery_dl_update_uses_cache_within_ttl(monkeypatch):
    health._gallery_dl_cache = None
    health._gallery_dl_cache_at = 0.0
    calls = []

    def _urlopen(request, timeout=None):
        calls.append(1)
        return _FakeJSONResponse({"info": {"version": "1.32.10"}})

    monkeypatch.setattr(health.urllib.request, "urlopen", _urlopen)
    health.check_gallery_dl_update(use_cache=True, installed_version="1.30.0")
    health.check_gallery_dl_update(use_cache=True, installed_version="1.30.0")
    assert len(calls) == 1, "a segunda chamada dentro do TTL deveria ter usado o cache"


def test_check_gallery_dl_update_cache_is_independent_from_ytdlp(monkeypatch):
    """Os dois motores não devem compartilhar cache -- checar um não pode
    fazer o outro parecer "já checado"."""
    health._ytdlp_cache = None
    health._ytdlp_cache_at = 0.0
    health._gallery_dl_cache = None
    health._gallery_dl_cache_at = 0.0
    monkeypatch.setattr(
        health.urllib.request,
        "urlopen",
        lambda request, timeout=None: _FakeJSONResponse({"info": {"version": "9.9.9"}}),
    )

    health.check_ytdlp_update(use_cache=True, installed_version="1.0.0")

    assert health._gallery_dl_cache is None
