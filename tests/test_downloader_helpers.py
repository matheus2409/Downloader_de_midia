import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from downloader import _human_eta, _human_rate, _unique_path  # noqa: E402


def test_unique_path_returns_same_path_when_free(tmp_path):
    target = tmp_path / "video.mp4"
    assert _unique_path(target) == target


def test_unique_path_avoids_collision(tmp_path):
    target = tmp_path / "video.mp4"
    target.write_text("existing")
    result = _unique_path(target)
    assert result != target
    assert result.name == "video (1).mp4"


def test_human_rate_formats_units():
    assert _human_rate(None) == ""
    assert "KB/s" in _human_rate(5_000)


def test_human_eta_formats_durations():
    assert _human_eta(None) == ""
    assert _human_eta(45) == "45s"
    assert _human_eta(125) == "2m05s"
    assert _human_eta(3700) == "1h01m"
