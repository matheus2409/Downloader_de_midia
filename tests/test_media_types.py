import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from media_types import classify  # noqa: E402


def test_classify_video():
    assert classify(".mp4") == "video"
    assert classify("mkv") == "video"


def test_classify_audio():
    assert classify(".mp3") == "audio"
    assert classify("FLAC") == "audio"


def test_classify_image():
    assert classify(".jpg") == "image"
    assert classify(".gif") == "image"


def test_classify_document():
    assert classify(".pdf") == "document"
    assert classify(".docx") == "document"


def test_classify_unknown_falls_back_to_other():
    assert classify(".xyz123") == "other"
    assert classify("") == "other"
