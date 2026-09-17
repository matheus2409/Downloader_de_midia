import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import downloader  # noqa: E402


def _make_task(monkeypatch, tmp_path, url, extra_opts=None):
    monkeypatch.setattr(downloader, "get_ffmpeg_path", lambda: None)
    return downloader.DownloadTask("t1", url, str(tmp_path), extra_opts or {})


def _record_events(monkeypatch):
    events = []
    monkeypatch.setattr(
        downloader.bus, "publish",
        lambda event_type, **data: events.append((event_type, data)),
    )
    return events


class _FakeOutcome:
    def __init__(self, files=None, login_required=False, cancelled=False):
        self.files = files or []
        self.login_required = login_required
        self.cancelled = cancelled

    @property
    def handled(self):
        return bool(self.files)


# ---------------------------------------------------- roteamento por host
def test_run_never_touches_ytdlp_when_gallery_dl_succeeds(monkeypatch, tmp_path):
    """Para um link do Instagram, um sucesso do gallery-dl deve encerrar a
    tarefa sem sequer instanciar o yt_dlp.YoutubeDL."""
    task = _make_task(monkeypatch, tmp_path, "https://www.instagram.com/p/ABC/")
    events = _record_events(monkeypatch)

    monkeypatch.setattr(downloader.gallery_engine, "is_supported", lambda url: True)
    monkeypatch.setattr(downloader.gallery_engine, "available", lambda: True)

    def _fake_download(url, output_dir, **kwargs):
        kwargs["on_file"]("/dest/foto.jpg")
        return _FakeOutcome(files=["/dest/foto.jpg"])

    monkeypatch.setattr(downloader.gallery_engine, "download", _fake_download)

    def _boom(opts):
        raise AssertionError("não deveria ter chamado o yt-dlp")

    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", _boom)

    retry_at = task.run()

    assert retry_at is None
    assert ("file_ready", {"task_id": "t1", "path": "/dest/foto.jpg"}) in events
    assert ("finished", {"task_id": "t1", "success": True, "message": ""}) in events


def test_run_falls_through_to_ytdlp_when_gallery_dl_finds_nothing(monkeypatch, tmp_path):
    """Um link do Instagram que o gallery-dl não conseguiu (ex.: pediu
    login) deve continuar funcionando do jeito de sempre -- cai pro yt-dlp
    normalmente, sem publicar nada no meio do caminho."""
    task = _make_task(monkeypatch, tmp_path, "https://www.instagram.com/p/PRIVADO/")
    events = _record_events(monkeypatch)

    monkeypatch.setattr(downloader.gallery_engine, "is_supported", lambda url: True)
    monkeypatch.setattr(downloader.gallery_engine, "available", lambda: True)
    monkeypatch.setattr(
        downloader.gallery_engine, "download",
        lambda url, output_dir, **kw: _FakeOutcome(login_required=True),
    )
    monkeypatch.setattr(task, "_try_fallbacks", lambda **kw: False)
    monkeypatch.setattr(
        downloader.yt_dlp, "YoutubeDL",
        lambda opts: _RaisingYDL("ERROR: [Instagram] x: Sign in to confirm you're not a bot."),
    )

    task.run()

    gallery_dl_events = [e for e in events if "gallery" in str(e).lower()]
    assert gallery_dl_events == [], "não deve publicar nada a partir da tentativa do gallery-dl que falhou"
    assert any(e[0] == "finished" and e[1]["success"] is False for e in events)


def test_run_skips_gallery_dl_for_unrelated_hosts(monkeypatch, tmp_path):
    """youtube.com etc. continuam indo direto pro yt-dlp, sem nem checar
    o gallery-dl -- is_supported() deve ser o único gate consultado."""
    task = _make_task(monkeypatch, tmp_path, "https://www.youtube.com/watch?v=x")

    def _boom(*a, **kw):
        raise AssertionError("gallery-dl não deveria ter sido chamado para este host")

    monkeypatch.setattr(downloader.gallery_engine, "download", _boom)
    monkeypatch.setattr(task, "_try_fallbacks", lambda **kw: False)
    monkeypatch.setattr(
        downloader.yt_dlp, "YoutubeDL",
        lambda opts: _RaisingYDL("ERROR: [youtube] x: Video unavailable"),
    )

    task.run()  # não deve levantar AssertionError


def test_run_skips_gallery_dl_when_package_not_installed(monkeypatch, tmp_path):
    task = _make_task(monkeypatch, tmp_path, "https://www.instagram.com/p/ABC/")
    monkeypatch.setattr(downloader.gallery_engine, "is_supported", lambda url: True)
    monkeypatch.setattr(downloader.gallery_engine, "available", lambda: False)

    def _boom(*a, **kw):
        raise AssertionError("gallery-dl não deveria ter sido chamado sem o pacote instalado")

    monkeypatch.setattr(downloader.gallery_engine, "download", _boom)
    monkeypatch.setattr(task, "_try_fallbacks", lambda **kw: False)
    monkeypatch.setattr(
        downloader.yt_dlp, "YoutubeDL",
        lambda opts: _RaisingYDL("ERROR: [Instagram] x: Video unavailable"),
    )

    task.run()  # não deve levantar AssertionError


# ------------------------------------------------------------- carrossel
def test_carousel_publishes_one_file_ready_per_item(monkeypatch, tmp_path):
    task = _make_task(monkeypatch, tmp_path, "https://www.instagram.com/p/CARROSSEL/")
    events = _record_events(monkeypatch)
    monkeypatch.setattr(downloader.gallery_engine, "is_supported", lambda url: True)
    monkeypatch.setattr(downloader.gallery_engine, "available", lambda: True)

    def _fake_download(url, output_dir, **kwargs):
        for path in ("/d/1.jpg", "/d/2.jpg", "/d/3.mp4"):
            kwargs["on_file"](path)
        return _FakeOutcome(files=["/d/1.jpg", "/d/2.jpg", "/d/3.mp4"])

    monkeypatch.setattr(downloader.gallery_engine, "download", _fake_download)

    task.run()

    file_ready_paths = [e[1]["path"] for e in events if e[0] == "file_ready"]
    assert file_ready_paths == ["/d/1.jpg", "/d/2.jpg", "/d/3.mp4"]
    assert task.dest_path == Path("/d/3.mp4")  # última mídia do post


# ----------------------------------------------------------- cancelamento
def test_cancelled_gallery_dl_download_emits_stopped_not_finished(monkeypatch, tmp_path):
    task = _make_task(monkeypatch, tmp_path, "https://www.pinterest.com/pin/1/")
    events = _record_events(monkeypatch)
    monkeypatch.setattr(downloader.gallery_engine, "is_supported", lambda url: True)
    monkeypatch.setattr(downloader.gallery_engine, "available", lambda: True)
    monkeypatch.setattr(
        downloader.gallery_engine, "download",
        lambda url, output_dir, **kw: _FakeOutcome(cancelled=True),
    )
    task.pause()

    retry_at = task.run()

    assert retry_at is None
    assert ("finished", {"task_id": "t1", "success": False, "message": "pausado"}) in events


# --------------------------------------------------- robustez contra bugs
def test_unexpected_exception_in_gallery_dl_falls_back_to_ytdlp(monkeypatch, tmp_path):
    """Um bug de verdade no motor novo (ex.: exceção não prevista) nunca
    pode impedir o caminho de sempre de funcionar."""
    task = _make_task(monkeypatch, tmp_path, "https://www.instagram.com/p/ABC/")
    monkeypatch.setattr(downloader.gallery_engine, "is_supported", lambda url: True)
    monkeypatch.setattr(downloader.gallery_engine, "available", lambda: True)

    def _boom(*a, **kw):
        raise RuntimeError("bug inesperado")

    monkeypatch.setattr(downloader.gallery_engine, "download", _boom)
    monkeypatch.setattr(task, "_try_fallbacks", lambda **kw: False)
    monkeypatch.setattr(
        downloader.yt_dlp, "YoutubeDL",
        lambda opts: _RaisingYDL("ERROR: [Instagram] x: Video unavailable"),
    )

    task.run()  # não deve propagar o RuntimeError


# --------------------------------------------------------- auth sync
def test_cookies_file_takes_priority_and_flows_to_gallery_engine(monkeypatch, tmp_path):
    task = _make_task(
        monkeypatch, tmp_path, "https://www.instagram.com/p/ABC/",
        extra_opts={"cookiefile": "/home/mats/cookies.txt", "cookiesfrombrowser": ("firefox",)},
    )
    assert task._gallery_dl_cookies_file == "/home/mats/cookies.txt"
    assert task._gallery_dl_cookies_from_browser == ""  # cookiefile tem prioridade, igual no yt-dlp


def test_cookies_from_browser_used_when_no_file_configured(monkeypatch, tmp_path):
    task = _make_task(
        monkeypatch, tmp_path, "https://www.instagram.com/p/ABC/",
        extra_opts={"cookiesfrombrowser": ("firefox",)},
    )
    assert task._gallery_dl_cookies_from_browser == "firefox"


def test_update_opts_refreshes_gallery_dl_auth(monkeypatch, tmp_path):
    """Configurar cookies depois de uma falha e clicar 'Tentar de novo'
    deve valer pros dois motores, não só pro yt-dlp -- ver
    DownloadManager.resume."""
    task = _make_task(monkeypatch, tmp_path, "https://www.instagram.com/p/ABC/")
    assert task._gallery_dl_cookies_file == ""

    task.update_opts({"cookiefile": "/home/mats/cookies.txt"})

    assert task._gallery_dl_cookies_file == "/home/mats/cookies.txt"


class _RaisingYDL:
    def __init__(self, message: str):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        raise RuntimeError(self._message)
