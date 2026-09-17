import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from errors import (  # noqa: E402
    friendly_message,
    is_login_required,
    is_permanently_unavailable,
    is_transient,
)


def test_known_pattern_is_translated():
    raw = "ERROR: [generic] Unsupported URL: https://example.com/x"
    assert friendly_message(raw) == "Esse site/link não é suportado."


def test_http_404_is_translated():
    assert "não existe mais" in friendly_message("HTTP Error 404: Not Found")


def test_case_insensitive_matching():
    assert friendly_message("NO SPACE LEFT ON DEVICE") == "Sem espaço livre em disco."


def test_unknown_error_falls_back_to_trimmed_original():
    raw = "Some completely novel error the app has never seen before"
    assert friendly_message(raw).startswith("Some completely novel error")


def test_empty_string_has_a_sane_fallback():
    assert friendly_message("") == "Falhou por um motivo desconhecido."


def test_connection_reset_is_transient():
    assert is_transient("ConnectionResetError(10054, 'Foi forçado o cancelamento...')") is True


def test_rate_limit_is_transient():
    assert is_transient("HTTP Error 429: Too Many Requests") is True


def test_login_required_is_not_transient():
    """Sem cookies configurados, tentar de novo sozinho nao muda nada --
    nao faz sentido auto-retry pra isso, so atrasa mostrar o erro real."""
    assert is_transient("Sign in to confirm you're not a bot") is False


def test_video_unavailable_is_permanent():
    assert is_permanently_unavailable("ERROR: [youtube] x: Video unavailable") is True


def test_login_required_detects_youtube_bot_check():
    raw = "ERROR: [youtube] XQ2yAQLYt-U: Sign in to confirm you're not a bot."
    assert is_login_required(raw) is True


def test_login_required_is_case_insensitive():
    assert is_login_required("SIGN IN TO CONFIRM you're not a bot") is True


def test_login_required_is_false_for_unrelated_errors():
    assert is_login_required("HTTP Error 404: Not Found") is False
    assert is_login_required("ERROR: [youtube] x: Video unavailable") is False


def test_transient_and_permanent_are_mutually_exclusive_for_common_cases():
    transient_msg = "Connection broken: ConnectionResetError(10054)"
    permanent_msg = "This video is no longer available because the uploader has closed their account."
    assert is_transient(transient_msg) and not is_permanently_unavailable(transient_msg)
    assert is_permanently_unavailable(permanent_msg) and not is_transient(permanent_msg)
