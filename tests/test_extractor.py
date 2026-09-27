"""Best-rendition selection logic (offline: no network access)."""

from __future__ import annotations

import base64
import json

import pytest
import yt_dlp

from app.extractor import (
    FORMAT_SORT,
    BestRenditionSelector,
    enrich_formats,
    friendly_error,
    resolution_hint_from_url,
)


def cdn_url(tag: str) -> str:
    efg = base64.b64encode(json.dumps({"vencode_tag": tag, "duration_s": 4}).encode()).decode()
    return f"https://scontent.cdninstagram.com/o1/v/t2/f2/m86/ABC.mp4?efg={efg}&_nc_ht=x"


def test_resolution_hint_from_cdn_tag():
    assert (
        resolution_hint_from_url(
            cdn_url("xpv_progressive.INSTAGRAM.CLIPS.C3.1080.dash_baseline_1_v1")
        )
        == 1080
    )
    assert (
        resolution_hint_from_url(
            cdn_url("xpv_progressive.INSTAGRAM.CLIPS.C3.480.dash_baseline_2_v1")
        )
        == 480
    )
    assert resolution_hint_from_url(cdn_url("ig-xpvds.clips.igwww-C3.dash_high_4_v1")) is None
    assert resolution_hint_from_url("https://example.com/video.mp4") is None
    assert resolution_hint_from_url("https://example.com/video.mp4?efg=%%%") is None
    assert resolution_hint_from_url(None) is None


def test_enrich_formats_only_touches_unsized_video():
    formats = [
        {"format_id": "0", "url": cdn_url("x.C3.1080.y"), "vcodec": "h264", "ext": "mp4"},
        {
            "format_id": "dash-v",
            "url": cdn_url("x.C3.480.y"),
            "width": 720,
            "height": 1280,
            "vcodec": "avc1",
        },
        {
            "format_id": "dash-a",
            "url": cdn_url("x.C3.1080.y"),
            "vcodec": "none",
            "acodec": "mp4a.40.2",
        },
    ]
    assert enrich_formats(formats) == 1
    assert formats[0]["height"] == 1080 and formats[0]["resolution"] == "~1080p"
    assert formats[1]["height"] == 1280
    assert "height" not in formats[2]


@pytest.fixture
def ydl():
    with yt_dlp.YoutubeDL(
        {"quiet": True, "no_warnings": True, "format_sort": FORMAT_SORT}
    ) as instance:
        yield instance


def select(ydl, formats):
    selector = BestRenditionSelector()
    selector.bind(ydl)
    for fmt in formats:
        fmt.setdefault("ext", "mp4")
        fmt.setdefault("protocol", "https")
    chosen = list(
        selector({"formats": formats, "has_merged_format": True, "incomplete_formats": False})
    )
    assert len(chosen) == 1
    return chosen[0]


def test_selector_prefers_unsized_1080p_progressive_over_720p_dash(ydl):
    formats = [
        {"format_id": "p1080", "url": cdn_url("x.C3.1080.y"), "vcodec": "h264", "acodec": "aac"},
        {"format_id": "p480", "url": cdn_url("x.C3.480.y"), "vcodec": "h264", "acodec": "aac"},
        {
            "format_id": "dash-v",
            "url": cdn_url("d"),
            "width": 720,
            "height": 1280,
            "fps": 30,
            "tbr": 510,
            "vcodec": "avc1.64001F",
            "acodec": "none",
        },
        {
            "format_id": "dash-a",
            "url": cdn_url("d"),
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "tbr": 64,
        },
    ]
    chosen = select(ydl, formats)
    video_ids = chosen["format_id"].split("+")
    assert video_ids[0] == "p1080"


def test_selector_prefers_highest_bitrate_dash_at_equal_resolution(ydl):
    formats = [
        {
            "format_id": "dash-low",
            "url": cdn_url("d"),
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "tbr": 1500,
            "vcodec": "avc1",
            "acodec": "none",
        },
        {
            "format_id": "dash-high",
            "url": cdn_url("d"),
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "tbr": 4200,
            "vcodec": "avc1",
            "acodec": "none",
        },
        {"format_id": "prog-720", "url": cdn_url("x.C3.720.y"), "vcodec": "h264", "acodec": "aac"},
        {
            "format_id": "dash-a",
            "url": cdn_url("d"),
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "tbr": 96,
        },
    ]
    chosen = select(ydl, formats)
    assert chosen["format_id"] == "dash-high+dash-a"


def test_selector_prefers_60fps_when_resolution_ties(ydl):
    formats = [
        {
            "format_id": "v30",
            "url": cdn_url("d"),
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "tbr": 4000,
            "vcodec": "avc1",
            "acodec": "none",
        },
        {
            "format_id": "v60",
            "url": cdn_url("d"),
            "width": 1080,
            "height": 1920,
            "fps": 60,
            "tbr": 3800,
            "vcodec": "avc1",
            "acodec": "none",
        },
        {"format_id": "a", "url": cdn_url("d"), "vcodec": "none", "acodec": "mp4a.40.2", "tbr": 96},
    ]
    assert select(ydl, formats)["format_id"] == "v60+a"


def test_friendly_errors():
    login = friendly_error(
        "ERROR: [Instagram] abc: You need to log in to access this content", True, False
    )
    assert "logged in" in login and "sessionid" in login
    expired = friendly_error("ERROR: [Instagram] abc: login required", True, True)
    assert "rejected the session cookie" in expired
    assert "rate-limiting" in friendly_error("HTTP Error 429: Too Many Requests", False, False)
    assert "unavailable" in friendly_error(
        "ERROR: [Instagram] abc: Requested content is not available", False, False
    )
    assert "Photo posts" in friendly_error(
        "ERROR: [Instagram] abc: No video formats found!", False, False
    )
    assert friendly_error(
        "ERROR: [Instagram] abc: something odd happened", False, False
    ).startswith("Instagram download failed: something odd happened")
