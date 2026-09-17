import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import playlist  # noqa: E402


def _no_shortlink_resolution(monkeypatch):
    # _resolver_link_encurtado só importa pra pin.it de verdade; pros
    # testes abaixo a URL já é a "final", então destrava sem fazer
    # requisição nenhuma.
    monkeypatch.setattr(playlist, "_resolver_link_encurtado", lambda url, depth=0: url)


# ------------------------------------------------------- _expand_via_gallery_dl
def test_expand_via_gallery_dl_returns_none_when_package_missing(monkeypatch):
    monkeypatch.setattr(playlist.gallery_engine, "available", lambda: False)
    assert playlist._expand_via_gallery_dl("https://www.pinterest.com/x/board/", None) is None


def test_expand_via_gallery_dl_returns_items_and_reports_progress(monkeypatch):
    monkeypatch.setattr(playlist.gallery_engine, "available", lambda: True)
    monkeypatch.setattr(
        playlist.gallery_engine, "list_items",
        lambda url, **kw: ["https://www.pinterest.com/pin/1/", "https://www.pinterest.com/pin/2/"],
    )
    progress_msgs = []

    items = playlist._expand_via_gallery_dl("https://www.pinterest.com/x/board/", progress_msgs.append)

    assert items == ["https://www.pinterest.com/pin/1/", "https://www.pinterest.com/pin/2/"]
    assert progress_msgs and "2" in progress_msgs[0]


# ------------------------------------------------------------------ expand_sync
def test_expand_sync_prefers_gallery_dl_over_old_scraper_for_pinterest(monkeypatch, tmp_path):
    """Estratégia nova (gallery-dl) deve ser tentada antes do scraper
    manual antigo -- e se ela funcionar, o scraper antigo nem deve ser
    chamado."""
    _no_shortlink_resolution(monkeypatch)
    monkeypatch.setattr(playlist, "_expand_via_ytdlp", lambda url: None)
    monkeypatch.setattr(
        playlist, "_expand_via_gallery_dl",
        lambda url, on_progress: ["https://www.pinterest.com/pin/1/"],
    )

    def _boom(url, on_progress):
        raise AssertionError("scraper antigo não deveria ter sido chamado")

    monkeypatch.setattr(playlist, "_expand_pinterest_board", _boom)

    result = playlist.expand_sync("https://www.pinterest.com/x/board/")

    assert result == ["https://www.pinterest.com/pin/1/"]


def test_expand_sync_falls_back_to_old_scraper_when_gallery_dl_fails(monkeypatch, tmp_path):
    """Se nem o yt-dlp nem o gallery-dl acharem nada, o scraper manual
    antigo continua disponível como última tentativa -- nenhuma regressão
    para quem já dependia dele."""
    _no_shortlink_resolution(monkeypatch)
    monkeypatch.setattr(playlist, "_expand_via_ytdlp", lambda url: None)
    monkeypatch.setattr(playlist, "_expand_via_gallery_dl", lambda url, on_progress: None)
    monkeypatch.setattr(
        playlist, "_expand_pinterest_board",
        lambda url, on_progress: ["https://i.pinimg.com/originals/x.jpg"],
    )

    result = playlist.expand_sync("https://www.pinterest.com/x/board/")

    assert result == ["https://i.pinimg.com/originals/x.jpg"]


def test_expand_sync_skips_pinterest_strategies_for_other_sites(monkeypatch, tmp_path):
    _no_shortlink_resolution(monkeypatch)
    monkeypatch.setattr(playlist, "_expand_via_ytdlp", lambda url: None)

    def _boom(*a, **kw):
        raise AssertionError("estratégias do Pinterest não deveriam rodar para outro site")

    monkeypatch.setattr(playlist, "_expand_via_gallery_dl", _boom)
    monkeypatch.setattr(playlist, "_expand_pinterest_board", _boom)

    result = playlist.expand_sync("https://example.com/nao-e-pinterest")

    assert result == ["https://example.com/nao-e-pinterest"]


def test_expand_sync_returns_single_url_when_everything_fails(monkeypatch, tmp_path):
    _no_shortlink_resolution(monkeypatch)
    monkeypatch.setattr(playlist, "_expand_via_ytdlp", lambda url: None)
    monkeypatch.setattr(playlist, "_expand_via_gallery_dl", lambda url, on_progress: None)
    monkeypatch.setattr(playlist, "_expand_pinterest_board", lambda url, on_progress: None)

    result = playlist.expand_sync("https://www.pinterest.com/x/board-vazio/")

    assert result == ["https://www.pinterest.com/x/board-vazio/"]
