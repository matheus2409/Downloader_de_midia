"""Translates common technical error strings (mostly from yt-dlp and
urllib) into short messages a non-technical user can actually act on.
Falls back to the original message, trimmed, when nothing matches."""
from __future__ import annotations

_PATTERNS: list[tuple[str, str]] = [
    ("unsupported url", "Esse site/link não é suportado."),
    (
        "unable to download webpage",
        "Não deu para acessar o link — confira sua internet ou se o link está certo.",
    ),
    ("name or service not known", "Sem conexão com a internet."),
    ("network is unreachable", "Sem conexão com a internet."),
    ("timed out", "O site demorou demais para responder. Tente de novo."),
    ("http error 404", "O link não existe mais (404)."),
    ("http error 403", "O site recusou o acesso (pode exigir login ou estar bloqueando downloads)."),
    (
        "http error 429",
        "O site bloqueou temporariamente por excesso de pedidos. Espere um pouco e tente de novo.",
    ),
    (
        "sign in to confirm",
        "O site pede login para acessar esse conteúdo — configure cookies em "
        "🔑 Login/Cookies e tente de novo.",
    ),
    ("private video", "Esse vídeo é privado."),
    ("video unavailable", "Esse vídeo não está mais disponível."),
    ("no video formats found", "Não achamos um formato de vídeo/áudio baixável nesse link."),
    ("requested format is not available", "A qualidade escolhida não está disponível para esse link."),
    ("ffmpeg not found", "Falta o ffmpeg instalado para converter esse arquivo."),
    ("permission denied", "Sem permissão para salvar nessa pasta."),
    ("no space left on device", "Sem espaço livre em disco."),
]


def friendly_message(raw: str) -> str:
    lowered = raw.lower()
    for needle, message in _PATTERNS:
        if needle in lowered:
            return message
    # Unknown error: show a trimmed version of the original instead of a
    # wall of text (yt-dlp messages can be long and include a URL/stacktrace).
    trimmed = raw.strip().splitlines()[0] if raw.strip() else "Falhou por um motivo desconhecido."
    return trimmed[:180]


# Sinais de que o problema é de rede/passageiro — vale tentar de novo
# sozinho, sem precisar que o usuário clique em nada. Propositalmente NÃO
# inclui coisas como "http error 403" ou "sign in to confirm": se o
# problema é bloqueio/login, tentar de novo automaticamente não muda nada,
# só atrasa mostrar o erro real pro usuário.
_TRANSIENT_PATTERNS = (
    "connection broken",
    "connection reset",
    "10054",
    "10053",
    "10060",
    "timed out",
    "network is unreachable",
    "name or service not known",
    "temporary failure in name resolution",
    "econnreset",
    "http error 429",
)

# Sinais de que o conteúdo não existe mais — nenhuma tentativa nova (manual
# ou automática) vai resolver, então nem vale gastar tempo com o fallback.
_PERMANENT_PATTERNS = (
    "video unavailable",
    "this video is not available",
    "has been terminated",
    "copyright",
    "no longer available",
    "video has been removed",
)


def is_transient(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in _TRANSIENT_PATTERNS)


def is_permanently_unavailable(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in _PERMANENT_PATTERNS)


# Mesmo texto que a 1ª entrada de _PATTERNS pra "sign in to confirm" —
# extraído como predicado próprio porque health.py precisa da mesma
# checagem pra contar, em lote, quantos downloads recentes falharam por
# esse motivo específico (ver find_recent_login_required_failures).
_LOGIN_REQUIRED_PATTERNS = ("sign in to confirm",)


def is_login_required(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in _LOGIN_REQUIRED_PATTERNS)
