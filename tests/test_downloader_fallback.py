import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import downloader  # noqa: E402

IMAGE_ONLY_HTML = (
    b'<html><head>'
    b'<meta property="og:image" content="https://example.com/thumb.jpg">'
    b'</head></html>'
)
VIDEO_HTML = (
    b'<html><head>'
    b'<meta property="og:video" content="https://example.com/video.mp4">'
    b'</head></html>'
)


class _FakeHeaders:
    def __init__(self, content_type):
        self._content_type = content_type

    def get_content_type(self):
        return self._content_type

    def get(self, key, default=None):
        return default


class _FakeResponse:
    def __init__(self, data: bytes, content_type: str):
        self._data = data
        self.headers = _FakeHeaders(content_type)

    def read(self, n=-1):
        if n is None or n < 0:
            data, self._data = self._data, b""
            return data
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    def getcode(self):
        return 200

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_task(monkeypatch, tmp_path, url="https://example.com/page"):
    # Evita que o teste dependa de resolver um ffmpeg de verdade.
    monkeypatch.setattr(downloader, "get_ffmpeg_path", lambda: None)
    return downloader.DownloadTask("t1", url, str(tmp_path), {})


def _fake_urlopen(responses: dict):
    def _urlopen(request, timeout=None):
        url = request.full_url
        for needle, (data, ctype) in responses.items():
            if needle in url:
                return _FakeResponse(data, ctype)
        raise AssertionError(f"URL inesperada no teste: {url}")

    return _urlopen


# ------------------------------------------------------- _download_direct_file
def test_download_direct_file_sends_referer_header(monkeypatch, tmp_path):
    """CDNs com proteção contra hotlink recusam (403) pedido de mídia sem
    um Referer condizente com a página de origem. self.url deve ir sempre
    como Referer, mesmo quando a mídia baixada está em outro domínio (ex.:
    fallback via metadata/html-scrape apontando pra um CDN)."""
    task = _make_task(monkeypatch, tmp_path, url="https://example.com/pagina-original")
    captured = {}

    def _urlopen(request, timeout=None):
        captured["referer"] = request.get_header("Referer")
        return _FakeResponse(b"fake-bytes", "video/mp4")

    monkeypatch.setattr(downloader.urllib.request, "urlopen", _urlopen)

    assert task._download_direct_file("https://cdn.example.com/video.mp4") is True
    assert captured["referer"] == "https://example.com/pagina-original"


# ------------------------------------------------- _looks_like_recognized_site
def test_recognized_site_error_is_detected():
    msg = (
        "ERROR: [youtube] abc123: Sign in to confirm you're not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication."
    )
    assert downloader.DownloadTask._looks_like_recognized_site(msg) is True


def test_unsupported_url_is_not_a_recognized_site():
    msg = "ERROR: [generic] Unsupported URL: https://example.com/x"
    assert downloader.DownloadTask._looks_like_recognized_site(msg) is False


# ------------------------------------------------------- _download_via_html_scrape
def test_recognized_site_rejects_image_only_match(monkeypatch, tmp_path):
    """O bug real: YouTube bloqueado por login só tinha uma imagem (og:image)
    disponível via scrape simples, e isso virava um 'sucesso' com a
    thumbnail no lugar do vídeo. allow_image_only=False deve recusar."""
    task = _make_task(monkeypatch, tmp_path)
    monkeypatch.setattr(
        downloader.urllib.request, "urlopen",
        _fake_urlopen({"example.com/page": (IMAGE_ONLY_HTML, "text/html")}),
    )
    assert task._download_via_html_scrape(allow_image_only=False) is False


def test_unrecognized_site_still_accepts_image_only_match(monkeypatch, tmp_path):
    """Para um site que o yt-dlp nem reconhece, uma imagem pode ser
    legitimamente o conteúdo (ex.: página de blog) — esse caso continua
    funcionando como antes."""
    task = _make_task(monkeypatch, tmp_path)
    monkeypatch.setattr(
        downloader.urllib.request, "urlopen",
        _fake_urlopen({
            "example.com/page": (IMAGE_ONLY_HTML, "text/html"),
            "thumb.jpg": (b"fake-jpeg-bytes", "image/jpeg"),
        }),
    )
    assert task._download_via_html_scrape(allow_image_only=True) is True
    assert task.dest_path.suffix == ".jpg"


def test_video_match_accepted_even_when_image_only_is_disallowed(monkeypatch, tmp_path):
    """Um link de vídeo de verdade (og:video) continua valendo mesmo com
    allow_image_only=False — a restrição é só para imagens."""
    task = _make_task(monkeypatch, tmp_path)
    monkeypatch.setattr(
        downloader.urllib.request, "urlopen",
        _fake_urlopen({
            "example.com/page": (VIDEO_HTML, "text/html"),
            "video.mp4": (b"fake-mp4-bytes", "video/mp4"),
        }),
    )
    assert task._download_via_html_scrape(allow_image_only=False) is True
    assert task.dest_path.suffix == ".mp4"


# ------------------------------------------------------------- _try_fallbacks
def test_try_fallbacks_resets_dest_path_between_strategies(monkeypatch, tmp_path):
    """Uma 1ª estratégia que baixa alguns bytes e falha no meio não pode
    deixar o _dest_path 'sujo' para a 2ª estratégia (URL/extensão
    completamente diferente) tentar retomar por cima."""
    task = _make_task(monkeypatch, tmp_path, url="https://example.com/direct.bin")

    def fake_direct_link():
        task._dest_path = tmp_path / "arquivo-da-1a-tentativa.bin"
        return False

    def fake_metadata(allow_image_only):
        assert task._dest_path is None, "dest_path deveria ter sido restaurado entre as estratégias"
        return False

    monkeypatch.setattr(task, "_download_direct_link", fake_direct_link)
    monkeypatch.setattr(task, "_download_via_metadata", fake_metadata)
    monkeypatch.setattr(task, "_download_via_html_scrape", lambda allow_image_only: False)

    assert task._try_fallbacks(allow_image_only=True) is False
    assert task._dest_path is None


# --------------------------------------------------------------- auto-retry
class _RaisingYDL:
    """yt_dlp.YoutubeDL stand-in cujo extract_info sempre falha com uma
    mensagem escolhida — evita precisar de rede de verdade pra testar como
    run() reage a diferentes tipos de erro."""

    def __init__(self, message: str):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        raise RuntimeError(self._message)


def test_maybe_schedule_retry_returns_none_for_permanent_error(monkeypatch, tmp_path):
    task = _make_task(monkeypatch, tmp_path)
    assert task._maybe_schedule_retry("Video unavailable") is None


def test_maybe_schedule_retry_returns_future_timestamp_for_transient_error(monkeypatch, tmp_path):
    task = _make_task(monkeypatch, tmp_path)
    before = time.time()
    retry_at = task._maybe_schedule_retry("Connection broken: ConnectionResetError(10054)")
    assert retry_at is not None
    assert retry_at > before


def test_maybe_schedule_retry_gives_up_after_max_attempts(monkeypatch, tmp_path):
    task = _make_task(monkeypatch, tmp_path)
    msg = "Connection broken: ConnectionResetError(10054)"
    for _ in downloader.RETRY_BACKOFF_SECONDS:
        assert task._maybe_schedule_retry(msg) is not None
    assert task._maybe_schedule_retry(msg) is None


def test_permanently_unavailable_error_skips_fallback_chain(monkeypatch, tmp_path):
    """run() nao deveria nem tentar _try_fallbacks quando o proprio yt-dlp
    ja diz que o video sumiu de vez -- so tempo perdido em rede."""
    task = _make_task(monkeypatch, tmp_path)
    monkeypatch.setattr(
        downloader.yt_dlp, "YoutubeDL",
        lambda opts: _RaisingYDL("ERROR: [youtube] x: Video unavailable"),
    )
    calls = []
    monkeypatch.setattr(task, "_try_fallbacks", lambda **kw: calls.append(kw) or False)

    retry_at = task.run()

    assert calls == [], "fallback nao deveria ter sido chamado para conteudo permanentemente indisponivel"
    assert retry_at is None


def test_transient_error_schedules_auto_retry_when_fallback_fails(monkeypatch, tmp_path):
    task = _make_task(monkeypatch, tmp_path)
    monkeypatch.setattr(
        downloader.yt_dlp, "YoutubeDL",
        lambda opts: _RaisingYDL("Connection broken: ConnectionResetError(10054)"),
    )
    monkeypatch.setattr(task, "_try_fallbacks", lambda **kw: False)

    retry_at = task.run()

    assert retry_at is not None
    assert retry_at > time.time()
    assert task._auto_retries == 1


def test_recognized_site_login_error_does_not_auto_retry(monkeypatch, tmp_path):
    """Sem cookies configurados, tentar de novo sozinho nunca vai
    resolver -- deve virar erro normal, nao ficar reagendando."""
    task = _make_task(monkeypatch, tmp_path)
    monkeypatch.setattr(
        downloader.yt_dlp, "YoutubeDL",
        lambda opts: _RaisingYDL("ERROR: [youtube] x: Sign in to confirm you're not a bot."),
    )
    monkeypatch.setattr(task, "_try_fallbacks", lambda **kw: False)

    retry_at = task.run()

    assert retry_at is None
