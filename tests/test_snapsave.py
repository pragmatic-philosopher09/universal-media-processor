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
def test_mismatched_donor_is_rejected_and_warned(monkeypatch, settings, silent, tmp_path, caplog):
    fake_snapsave(monkeypatch, make_clip(tmp_path / "donor.mp4", seconds=3))
    warnings = extractor.restore_missing_audio([silent], POST, settings, CancelToken())
    assert "could not be restored" in warnings[0]
    assert "does not match" in caplog.text
    assert snapsave.is_silent_video(silent, settings)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["donor.mp4", silent.name]


@requires_ffmpeg
def test_provider_failure_keeps_silent_video_with_warning(monkeypatch, settings, silent, caplog):
    monkeypatch.setattr(
        snapsave, "fetch_video_urls", MagicMock(side_effect=snapsave.SnapSaveError("down"))
    )
    warnings = extractor.restore_missing_audio([silent], POST, settings, CancelToken())
    assert "without its audio track" in warnings[0] and "SnapSave" not in warnings[0]
    assert "audio: down" in caplog.text
    assert snapsave.fetch_video_urls.call_count == 2
    assert silent.exists()


@requires_ffmpeg
def test_transient_provider_failure_is_retried(monkeypatch, settings, silent, tmp_path):
    fake_snapsave(monkeypatch, make_clip(tmp_path / "donor.mp4"))
    snapsave.fetch_video_urls.side_effect = [snapsave.SnapSaveError("stall"), [LINK]]
    assert extractor.restore_missing_audio([silent], POST, settings, CancelToken()) == []
    assert snapsave.stream_summary(silent, settings)[1]


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


def test_download_videos_saves_each_link(monkeypatch, settings, tmp_path):
    monkeypatch.setattr(snapsave, "fetch_video_urls", MagicMock(return_value=[LINK, LINK]))
    monkeypatch.setattr(snapsave, "download", lambda link, path, *a: path)
    seen = []
    paths = snapsave.download_videos(
        "https://www.instagram.com/reel/x/",
        tmp_path,
        settings,
        CancelToken(),
        lambda i, n: seen.append((i, n)),
    )
    assert [p.name for p in paths] == ["src-snapsave-1.mp4", "src-snapsave-2.mp4"]
    assert seen == [(0, 2), (1, 2)]


def test_download_videos_cleans_up_on_failure(monkeypatch, settings, tmp_path):
    monkeypatch.setattr(snapsave, "fetch_video_urls", MagicMock(return_value=[LINK, LINK]))

    def download(link, path, *args):
        if path.name.endswith("2.mp4"):
            raise snapsave.SnapSaveError("broken")
        path.write_bytes(b"v")
        return path

    monkeypatch.setattr(snapsave, "download", download)
    with pytest.raises(snapsave.SnapSaveError):
        snapsave.download_videos(
            "https://www.instagram.com/reel/x/", tmp_path, settings, CancelToken()
        )
    assert not list(tmp_path.glob("src-snapsave-*"))
