"""Classifies a downloaded file by extension so the UI can show a matching
icon/label — shared between the downloader (which knows the final file) and
the row widget (which displays it)."""
from __future__ import annotations

VIDEO_EXTS = {"mp4", "mkv", "webm", "mov", "avi", "flv", "m4v", "wmv", "3gp"}
AUDIO_EXTS = {"mp3", "m4a", "wav", "flac", "ogg", "opus", "aac", "wma"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif", "heic"}
DOCUMENT_EXTS = {"pdf", "doc", "docx", "txt", "csv", "xlsx", "pptx", "zip", "rar", "7z"}

MEDIA_ICONS = {
    "video": "🎬",
    "audio": "🎵",
    "image": "🖼",
    "document": "📄",
    "other": "📦",
    "pending": "⏳",
}

MEDIA_LABELS = {
    "video": "Vídeo",
    "audio": "Áudio",
    "image": "Imagem",
    "document": "Documento",
    "other": "Arquivo",
    "pending": "Identificando...",
}


def classify(extension: str) -> str:
    ext = extension.lower().lstrip(".")
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in DOCUMENT_EXTS:
        return "document"
    return "other"
