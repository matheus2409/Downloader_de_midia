import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import gallery_engine  # noqa: E402


# ------------------------------------------------------------- is_supported
def test_is_supported_matches_instagram_and_pinterest():
    assert gallery_engine.is_supported("https://www.instagram.com/p/ABC123/") is True
    assert gallery_engine.is_supported("https://www.pinterest.com/pin/12345/") is True
    assert gallery_engine.is_supported("https://pin.it/abcXYZ") is True


def test_is_supported_rejects_unrelated_sites():
    assert gallery_engine.is_supported("https://www.youtube.com/watch?v=x") is False
    assert gallery_engine.is_supported("https://example.com/pinteresting-article") is False


# ------------------------------------------------------------------ download
_ORIG_POPEN = subprocess.Popen
_ORIG_RUN = subprocess.run


def _fake_process(monkeypatch, script: str):
    """Troca o comando real do gallery-dl por um script Python inline que
    imprime no mesmo formato -- evita depender de rede real contra
    Instagram/Pinterest para testar o parsing/loop de leitura."""

    def _popen(cmd, **kwargs):
        return _ORIG_POPEN([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(gallery_engine.subprocess, "Popen", _popen)


def test_download_reports_each_file_via_callbacks(monkeypatch, tmp_path):
    script = (
        "import sys\n"
        "print('GDL_OK\\t/tmp/a.jpg'); sys.stdout.flush()\n"
        "print('GDL_OK\\t/tmp/b.jpg'); sys.stdout.flush()\n"
    )
    _fake_process(monkeypatch, script)

    files, progress_calls = [], []
    outcome = gallery_engine.download(
        "https://www.instagram.com/p/FAKE/", str(tmp_path),
        on_file=files.append, on_progress=progress_calls.append,
    )

    assert outcome.handled is True
    assert outcome.files == ["/tmp/a.jpg", "/tmp/b.jpg"]
    assert files == ["/tmp/a.jpg", "/tmp/b.jpg"]
    assert progress_calls == [1, 2]


def test_download_counts_partial_failures_in_a_carousel(monkeypatch, tmp_path):
    """Um carrossel/álbum onde 1 dos 3 itens falha ainda deve reportar os 2
    que deram certo -- não descarta um resultado parcial."""
    script = (
        "import sys\n"
        "print('GDL_OK\\t/tmp/a.jpg'); sys.stdout.flush()\n"
        "print('GDL_ERR\\tb.jpg'); sys.stdout.flush()\n"
        "print('GDL_OK\\t/tmp/c.jpg'); sys.stdout.flush()\n"
    )
    _fake_process(monkeypatch, script)

    outcome = gallery_engine.download("https://www.pinterest.com/pin/1/", str(tmp_path))

    assert outcome.handled is True
    assert outcome.files == ["/tmp/a.jpg", "/tmp/c.jpg"]
    assert outcome.failed_count == 1


def test_download_falls_back_to_directory_diff_when_no_ok_lines(monkeypatch, tmp_path):
    """Rede de segurança: se o processo terminar com sucesso mas nenhuma
    linha GDL_OK aparecer (ex.: um placeholder do --Print mudou de nome
    numa versão futura do gallery-dl), ainda assim conta os arquivos que
    realmente apareceram na pasta em vez de reportar 0."""
    (tmp_path / "ja-existia.jpg").write_bytes(b"old")
    script = "import sys\nsys.exit(0)\n"

    def _popen(cmd, **kwargs):
        proc = _ORIG_POPEN([sys.executable, "-c", script], **kwargs)
        proc.wait()
        (tmp_path / "novo-arquivo.jpg").write_bytes(b"fake")
        return proc

    monkeypatch.setattr(gallery_engine.subprocess, "Popen", _popen)

    outcome = gallery_engine.download("https://www.instagram.com/p/FAKE/", str(tmp_path))

    assert outcome.handled is True
    assert outcome.files == [str(tmp_path / "novo-arquivo.jpg")]


def test_download_marks_login_required_on_auth_exit_code(monkeypatch, tmp_path):
    script = "import sys\nsys.exit(16)\n"
    _fake_process(monkeypatch, script)

    outcome = gallery_engine.download("https://www.instagram.com/p/PRIVADO/", str(tmp_path))

    assert outcome.handled is False
    assert outcome.login_required is True


def test_download_generic_failure_is_not_login_required(monkeypatch, tmp_path):
    script = "import sys\nsys.stderr.write('boom'); sys.exit(4)\n"
    _fake_process(monkeypatch, script)

    outcome = gallery_engine.download("https://www.pinterest.com/pin/sumiu/", str(tmp_path))

    assert outcome.handled is False
    assert outcome.login_required is False


def test_download_respects_should_cancel(monkeypatch, tmp_path):
    """Cancelamento deve derrubar o processo prontamente, mesmo que ele
    esteja preso (ex.: rede lenta) -- não pode esperar o job inteiro
    terminar sozinho."""
    script = (
        "import time, sys\n"
        "print('GDL_OK\\t/tmp/a.jpg'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    _fake_process(monkeypatch, script)

    start = time.time()
    outcome = gallery_engine.download(
        "https://www.instagram.com/p/FAKE/", str(tmp_path),
        should_cancel=lambda: time.time() - start > 0.8,
    )
    elapsed = time.time() - start

    assert outcome.cancelled is True
    assert elapsed < 5, "cancelamento demorou demais para derrubar o processo"
    mensagem = "arquivo que já tinha terminado antes do cancelamento deve continuar valendo"
    assert outcome.files == ["/tmp/a.jpg"], mensagem


def test_download_handles_missing_gallery_dl_binary_gracefully(monkeypatch, tmp_path):
    def _popen(cmd, **kwargs):
        raise OSError("No such file or directory")

    monkeypatch.setattr(gallery_engine.subprocess, "Popen", _popen)

    outcome = gallery_engine.download("https://www.instagram.com/p/FAKE/", str(tmp_path))

    assert outcome.handled is False
    assert "gallery-dl" in outcome.message.lower()


# --------------------------------------------------------------- list_items
def _fake_run(monkeypatch, payload):
    script = f"import sys; print({json.dumps(json.dumps(payload))})"

    def _run(cmd, **kwargs):
        return _ORIG_RUN([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(gallery_engine.subprocess, "run", _run)


def test_list_items_extracts_url_and_queue_entries_in_order(monkeypatch):
    _fake_run(monkeypatch, [
        [2, {"board_id": "123"}],
        [6, "https://www.pinterest.com/pin/1111111111/", {}],
        [6, "https://www.pinterest.com/pin/2222222222/", {}],
        [3, "https://i.pinimg.com/originals/aa/full.jpg", {}],
    ])

    items = gallery_engine.list_items("https://www.pinterest.com/user/board/")

    assert items == [
        "https://www.pinterest.com/pin/1111111111/",
        "https://www.pinterest.com/pin/2222222222/",
        "https://i.pinimg.com/originals/aa/full.jpg",
    ]


def test_list_items_deduplicates(monkeypatch):
    _fake_run(monkeypatch, [
        [6, "https://www.pinterest.com/pin/1111111111/", {}],
        [6, "https://www.pinterest.com/pin/1111111111/", {}],
    ])
    items = gallery_engine.list_items("https://www.pinterest.com/user/board/")
    assert items == ["https://www.pinterest.com/pin/1111111111/"]


def test_list_items_returns_none_for_single_item_or_error(monkeypatch):
    _fake_run(monkeypatch, [[-1, {"error": "NotFoundError", "message": "sumiu"}]])
    assert gallery_engine.list_items("https://www.pinterest.com/pin/sumiu/") is None


def test_list_items_returns_none_on_invalid_json(monkeypatch):
    def _run(cmd, **kwargs):
        return _ORIG_RUN([sys.executable, "-c", "print('not json')"], **kwargs)

    monkeypatch.setattr(gallery_engine.subprocess, "run", _run)
    assert gallery_engine.list_items("https://www.pinterest.com/user/board/") is None


def test_list_items_returns_none_on_timeout(monkeypatch):
    def _run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(gallery_engine.subprocess, "run", _run)
    assert gallery_engine.list_items("https://www.pinterest.com/user/board/", timeout=0.1) is None


# ------------------------------------------------------------------- misc
def test_available_reflects_installed_package():
    # O pacote está em requirements.txt e instalado neste ambiente de teste.
    assert gallery_engine.available() is True


def test_installed_version_returns_a_string_when_available():
    version = gallery_engine.installed_version()
    assert isinstance(version, str) and version
