"""Persisted app settings and download presets."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent
SETTINGS_FILE = APP_DIR / "settings.json"

DEFAULT_PRESETS: dict[str, dict[str, Any]] = {
    "Melhor qualidade (MP4)": {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "merge_output_format": "mp4",
    },
    "Áudio (MP3)": {
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    },
    "Compacto (menor tamanho)": {
        "format": "worst[ext=mp4]/worst",
    },
}


# Browsers cujo cookie storage o yt-dlp sabe ler diretamente. Note que
# "opera" aqui NÃO cobre o Opera GX — o yt-dlp ainda trata os dois como
# navegadores diferentes (perfil fica em outra pasta) e não faz a leitura
# automática do GX. Quem usa Opera GX precisa do modo "arquivo cookies.txt".
SUPPORTED_COOKIE_BROWSERS = (
    "brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "whale",
)


# Opções do yt-dlp pra baixar legenda junto, quando marcado na hora de
# adicionar o download (ver api.py). Tenta português e inglês, e aceita
# legenda gerada automaticamente se não houver uma "de verdade" no idioma.
SUBTITLE_OPTS: dict[str, Any] = {
    "writesubtitles": True,
    "writeautomaticsub": True,
    "subtitleslangs": ["pt.*", "en.*"],
}


@dataclass
class Settings:
    output_dir: str = str(Path.home() / "Downloads")
    max_concurrent: int = 3
    last_preset: str = next(iter(DEFAULT_PRESETS))
    presets: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_PRESETS))
    # Autenticação opcional para sites que exigem login (ex.: YouTube pedindo
    # "Sign in to confirm you're not a bot"). cookies_file tem prioridade
    # sobre cookies_from_browser quando os dois estão preenchidos, por ser
    # mais previsível (não depende do navegador estar fechado, nem de
    # conseguir decifrar o cookie storage do sistema).
    cookies_from_browser: str = ""
    cookies_file: str = ""
    # Integração com outras ferramentas (ex.: um workflow n8n). api_key
    # protege só o endpoint de adicionar downloads (POST /api/downloads) —
    # o resto continua liberado, já que o servidor só escuta em 127.0.0.1
    # mesmo; a chave existe pra outra ferramenta precisar se identificar de
    # propósito, não pra virar uma segurança forte. webhook_url, se
    # preenchida, recebe um POST com o resultado de cada download que
    # terminar (sucesso ou erro de verdade — pausa/cancelamento não conta).
    api_key: str = ""
    webhook_url: str = ""

    @classmethod
    def load(cls) -> "Settings":
        defaults = cls()
        if not SETTINGS_FILE.exists():
            return defaults
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            raw_max_concurrent = data.get("max_concurrent")
            if raw_max_concurrent is None:
                max_concurrent = defaults.max_concurrent
            else:
                max_concurrent = int(raw_max_concurrent)
            return cls(
                output_dir=data.get("output_dir", defaults.output_dir),
                max_concurrent=max(1, max_concurrent),
                last_preset=data.get("last_preset", defaults.last_preset),
                presets=data.get("presets") or defaults.presets,
                cookies_from_browser=data.get("cookies_from_browser", defaults.cookies_from_browser),
                cookies_file=data.get("cookies_file", defaults.cookies_file),
                api_key=data.get("api_key", defaults.api_key),
                webhook_url=data.get("webhook_url", defaults.webhook_url),
            )
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # Arquivo corrompido OU algum campo com tipo inesperado (ex.:
            # max_concurrent virou null/texto por edição manual) — nesses
            # casos é mais seguro cair nos padrões do que travar o servidor
            # inteiro por causa de um settings.json malformado.
            return defaults

    def save(self) -> None:
        SETTINGS_FILE.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def ytdlp_auth_opts(self) -> dict[str, Any]:
        """Opções do yt-dlp que carregam a sessão logada do usuário, se
        configurada. Chamado tanto pra download novo quanto por
        resume/retry manual — "Retomar", "Retomar tudo" e "Tentar de novo
        os que falharam" reaplicam o valor atual (ver api.py), então
        configurar cookies depois de uma leva de falhas por login e clicar
        em "Tentar de novo" já usa a configuração nova. O que continua
        *não* acontecendo é uma troca "ao vivo": um item que já está
        baixando neste exato momento não muda de cookie no meio do
        download, só na próxima tentativa."""
        if self.cookies_file:
            return {"cookiefile": self.cookies_file}
        if self.cookies_from_browser:
            return {"cookiesfrombrowser": (self.cookies_from_browser,)}
        return {}
