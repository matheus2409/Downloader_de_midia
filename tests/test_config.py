import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import config  # noqa: E402


def test_load_without_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    settings = config.Settings.load()
    assert settings.max_concurrent == 3
    assert settings.presets


def test_save_then_load_round_trip(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    original = config.Settings(output_dir=str(tmp_path), max_concurrent=7, last_preset="Áudio (MP3)")
    original.save()
    assert settings_file.exists()

    reloaded = config.Settings.load()
    assert reloaded.output_dir == str(tmp_path)
    assert reloaded.max_concurrent == 7
    assert reloaded.last_preset == "Áudio (MP3)"


def test_load_with_corrupted_file_falls_back_to_defaults(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    settings = config.Settings.load()
    assert settings.max_concurrent == 3


def test_load_with_bad_max_concurrent_type_falls_back_to_defaults(tmp_path, monkeypatch):
    """max_concurrent virando null/texto por edição manual do settings.json
    não pode derrubar o servidor inteiro no startup."""
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"max_concurrent": null}', encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    settings = config.Settings.load()
    assert settings.max_concurrent == 3


def test_load_clamps_max_concurrent_to_at_least_one(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"max_concurrent": 0}', encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    settings = config.Settings.load()
    assert settings.max_concurrent == 1


def test_cookies_file_round_trips(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    original = config.Settings(cookies_file=r"C:\Users\mats\cookies.txt")
    original.save()

    reloaded = config.Settings.load()
    assert reloaded.cookies_file == r"C:\Users\mats\cookies.txt"
    assert reloaded.cookies_from_browser == ""


def test_ytdlp_auth_opts_empty_by_default():
    assert config.Settings().ytdlp_auth_opts() == {}


def test_ytdlp_auth_opts_prefers_cookies_file_over_browser():
    settings = config.Settings(cookies_file="/tmp/cookies.txt", cookies_from_browser="chrome")
    assert settings.ytdlp_auth_opts() == {"cookiefile": "/tmp/cookies.txt"}


def test_ytdlp_auth_opts_uses_browser_when_no_file_set():
    settings = config.Settings(cookies_from_browser="firefox")
    assert settings.ytdlp_auth_opts() == {"cookiesfrombrowser": ("firefox",)}


def test_api_key_and_webhook_url_round_trip(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    original = config.Settings(api_key="segredo123", webhook_url="https://n8n.example.com/webhook/abc")
    original.save()

    reloaded = config.Settings.load()
    assert reloaded.api_key == "segredo123"
    assert reloaded.webhook_url == "https://n8n.example.com/webhook/abc"


def test_subtitle_opts_are_present_and_reasonable():
    assert config.SUBTITLE_OPTS["writesubtitles"] is True
    assert config.SUBTITLE_OPTS["writeautomaticsub"] is True
    assert "pt.*" in config.SUBTITLE_OPTS["subtitleslangs"]
