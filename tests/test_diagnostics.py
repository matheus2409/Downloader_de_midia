import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import diagnostics  # noqa: E402

FAKE_LOG = (
    "2026-08-09 22:00:00,000 INFO [downloader] Iniciando download (aaaaaaaa): "
    "https://www.youtube.com/watch?v=1\n"
    "2026-08-09 22:00:01,000 WARNING [downloader] yt-dlp falhou para aaaaaaaa "
    "(https://www.youtube.com/watch?v=1): ERROR: [youtube] 1: Sign in to confirm you're not a bot.\n"
    "2026-08-09 22:00:02,000 INFO [downloader] Fallback funcionou para aaaaaaaa\n"
    # site que o yt-dlp nao reconhece: mesmo com fallback funcionando, nao deve ser sinalizado
    "2026-08-09 22:00:03,000 WARNING [downloader] yt-dlp falhou para bbbbbbbb "
    "(https://blog.example.com/post): ERROR: Unsupported URL: https://blog.example.com/post\n"
    "2026-08-09 22:00:04,000 INFO [downloader] Fallback funcionou para bbbbbbbb\n"
    # falhou mas fallback NAO funcionou -- nao deve ser sinalizado (a task terminou como erro mesmo)
    "2026-08-09 22:00:05,000 WARNING [downloader] yt-dlp falhou para cccccccc "
    "(https://www.youtube.com/watch?v=3): ERROR: [youtube] 3: Sign in to confirm you're not a bot.\n"
    "2026-08-09 22:00:06,000 ERROR [downloader] Todas as estratégias falharam para cccccccc: erro\n"
)

HISTORY = [
    {
        "title": "maxresdefault (1)", "url": "https://www.youtube.com/watch?v=1",
        "path": "D:\\musicas\\maxresdefault (1).webp", "media_type": "image",
        "timestamp": "09/08/2026 22:00",
    },
    {
        "title": "post-image", "url": "https://blog.example.com/post",
        "path": "D:\\musicas\\post-image.jpg", "media_type": "image",
        "timestamp": "09/08/2026 22:00",
    },
    {
        "title": "musica-legitima", "url": "https://www.youtube.com/watch?v=legit",
        "path": "D:\\musicas\\musica-legitima.mp3", "media_type": "audio",
        "timestamp": "09/08/2026 22:00",
    },
    # imagem de um site de video conhecido, sem cobertura no log (ex.: log rotacionou)
    {
        "title": "capa-vimeo", "url": "https://vimeo.com/123456",
        "path": "D:\\musicas\\capa-vimeo.jpg", "media_type": "image",
        "timestamp": "01/08/2026 12:00",
    },
]


def test_flags_youtube_thumbnail_confirmed_by_log(monkeypatch, tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text(FAKE_LOG, encoding="utf-8")
    monkeypatch.setattr(diagnostics, "LOG_FILE", log_file)

    result = diagnostics.find_suspicious_downloads(HISTORY)
    urls = {item["url"]: item["matched_via"] for item in result}

    assert urls["https://www.youtube.com/watch?v=1"] == "log"


def test_does_not_flag_unsupported_site_image(monkeypatch, tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text(FAKE_LOG, encoding="utf-8")
    monkeypatch.setattr(diagnostics, "LOG_FILE", log_file)

    result = diagnostics.find_suspicious_downloads(HISTORY)
    urls = {item["url"] for item in result}

    assert "https://blog.example.com/post" not in urls


def test_does_not_flag_real_audio_or_video_entries(monkeypatch, tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text(FAKE_LOG, encoding="utf-8")
    monkeypatch.setattr(diagnostics, "LOG_FILE", log_file)

    result = diagnostics.find_suspicious_downloads(HISTORY)
    urls = {item["url"] for item in result}

    assert "https://www.youtube.com/watch?v=legit" not in urls


def test_falls_back_to_domain_heuristic_when_log_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(diagnostics, "LOG_FILE", tmp_path / "no-such-file.log")

    result = diagnostics.find_suspicious_downloads(HISTORY)
    urls = {item["url"]: item["matched_via"] for item in result}

    assert urls["https://vimeo.com/123456"] == "url"


def test_task_that_never_recovered_via_fallback_is_not_flagged(monkeypatch, tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text(FAKE_LOG, encoding="utf-8")
    monkeypatch.setattr(diagnostics, "LOG_FILE", log_file)

    mapping = diagnostics._task_urls_with_fallback_masking(FAKE_LOG)
    assert "cccccccc" not in mapping
