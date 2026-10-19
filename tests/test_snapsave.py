"""Offline coverage of restoring audio to silent FastVideoSave videos via SnapSave."""

import shutil
import subprocess
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from app import extractor, snapsave
from app.media import CancelToken

from .conftest import FFMPEG, requires_ffmpeg

POST = "https://www.instagram.com/reel/DWtEagdgB6n/"
LINK = "https://d.rapidcdn.app/v2?token=abc"


def make_clip(path, seconds=1.0, audio=True):
    sources = ["-f", "lavfi", "-i", f"testsrc=size=64x64:rate=10:duration={seconds}"]
    if audio:
        sources += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", *sources, "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        + (["-c:a", "aac"] if audio else [])
        + [str(path)],
        check=True,
    )
    return path


@pytest.fixture
def silent(tmp_path):
    return make_clip(tmp_path / "src-fastvideosave-1.mp4", audio=False)


def fake_snapsave(monkeypatch, donor):
    monkeypatch.setattr(snapsave, "fetch_video_urls", MagicMock(return_value=[LINK]))
    monkeypatch.setattr(
        snapsave, "download", lambda url, path, s, c: shutil.copyfile(donor, path) and path
    )


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        (LINK, True),
        ("https://d2.rapidcdn.app/v2?token=abc", True),
        ("http://d.rapidcdn.app/v2", False),
        ("https://rapidcdn.app.evil.test/v2", False),
        ("https://d.rapidcdn.app:8443/v2", False),
        ("https://user:pw@d.rapidcdn.app/v2", False),
        ("https://localhost/v2", False),
    ],
)
def test_download_hosts_are_restricted(url, allowed):
    assert snapsave.download_url_allowed(url) is allowed


@requires_ffmpeg
def test_silent_video_gets_audio_copied_in(monkeypatch, settings, silent, tmp_path):
    fake_snapsave(monkeypatch, make_clip(tmp_path / "donor.mp4"))
    assert snapsave.is_silent_video(silent, settings)
    assert extractor.restore_missing_audio([silent], POST, settings, CancelToken()) == []
    assert snapsave.stream_summary(silent, settings)[:2] == (True, True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["donor.mp4", silent.name]


@requires_ffmpeg
def test_videos_with_audio_are_left_alone(monkeypatch, settings, tmp_path):
    clip = make_clip(tmp_path / "a.mp4")
    fetch = MagicMock()
    monkeypatch.setattr(snapsave, "fetch_video_urls", fetch)
    assert extractor.restore_missing_audio([clip], POST, settings, CancelToken()) == []
    fetch.assert_not_called()


@requires_ffmpeg
def test_mismatched_donor_is_rejected_and_warned(monkeypatch, settings, silent, tmp_path):
    fake_snapsave(monkeypatch, make_clip(tmp_path / "donor.mp4", seconds=3))
    warnings = extractor.restore_missing_audio([silent], POST, settings, CancelToken())
    assert "does not match" in warnings[0]
    assert snapsave.is_silent_video(silent, settings)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["donor.mp4", silent.name]


@requires_ffmpeg
def test_provider_failure_keeps_silent_video_with_warning(monkeypatch, settings, silent):
    monkeypatch.setattr(
        snapsave, "fetch_video_urls", MagicMock(side_effect=snapsave.SnapSaveError("down"))
    )
    warnings = extractor.restore_missing_audio([silent], POST, settings, CancelToken())
    assert "without its audio track" in warnings[0] and "(down)" in warnings[0]
    assert silent.exists()


@requires_ffmpeg
def test_disabled_or_carousel_only_warns(monkeypatch, settings, silent, tmp_path):
    fetch = MagicMock()
    monkeypatch.setattr(snapsave, "fetch_video_urls", fetch)
    other = make_clip(tmp_path / "b.mp4")
    assert extractor.restore_missing_audio([silent, other], POST, settings, CancelToken())
    disabled = replace(settings, snapsave_audio_enabled=False)
    assert extractor.restore_missing_audio([silent], POST, disabled, CancelToken())
    fetch.assert_not_called()


def test_non_mp4_download_is_rejected(monkeypatch, settings, tmp_path):
    response = MagicMock()
    response.__enter__.return_value.read.side_effect = [b"<html>blocked</html>", b""]
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr(snapsave.urllib.request, "build_opener", lambda *a: opener)
    with pytest.raises(snapsave.SnapSaveError):
        snapsave.download(LINK, tmp_path / "x.part", settings, CancelToken())
    assert not (tmp_path / "x.part").exists()
