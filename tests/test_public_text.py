"""UI text never names the third-party tools and services behind the app."""

import re

import pytest

from app.public_text import scrub, scrub_records

TOOL_NAMES = re.compile(
    r"fastvideosave|snapsave|yt-dlp|ffmpeg|ffprobe|esrgan|\brife\b|video2x|playwright|chromium",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ffmpeg failed: x", "The video processor failed: x"),
        (
            "Backup Instagram retrieval: FastVideoSave timed out",
            "Backup Instagram retrieval: The backup service timed out",
        ),
        ("rife-ncnn-vulkan produced 3 frames", "The AI engine produced 3 frames"),
        ("SnapSave did not return this video.", "The audio source did not return this video."),
        (
            "Instagram download failed: Unable to extract data; please report this issue on "
            "https://github.com/yt-dlp/yt-dlp/issues , filling out the template. Confirm you "
            "are on the latest version using yt-dlp -U",
            "Instagram download failed: Unable to extract data",
        ),
        ("Plain message about Instagram.", "Plain message about Instagram."),
    ],
)
def test_scrub(raw, expected):
    assert scrub(raw) == expected
    assert not TOOL_NAMES.search(scrub(raw))


def test_scrub_records_only_touches_named_keys():
    records = [{"error": "ffprobe failed", "engine": "ffmpeg/libx264"}]
    assert scrub_records(records, "error") == [
        {"error": "The media reader failed", "engine": "ffmpeg/libx264"}
    ]
    assert scrub(None) is None
