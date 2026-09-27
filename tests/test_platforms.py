"""Multi-platform URL handling and the DeviantArt extractor (offline)."""

from __future__ import annotations

import json

import pytest

from app import deviantart
from app.urls import DEFAULT_ALLOWED_DOMAINS, PLATFORMS, InvalidURL, normalize_url


@pytest.mark.parametrize(
    ("raw", "url", "platform", "kind"),
    [
        (
            "https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PL123&t=5s",
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "youtube",
            "video",
        ),
        (
            "https://youtu.be/jNQXAC9IVRw?si=abc",
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "youtube",
            "video",
        ),
        (
            "youtube.com/shorts/jNQXAC9IVRw",
            "https://www.youtube.com/shorts/jNQXAC9IVRw",
            "youtube",
            "short",
        ),
        (
            "https://m.youtube.com/watch?v=jNQXAC9IVRw",
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "youtube",
            "video",
        ),
        (
            "https://www.tiktok.com/@scout2015/video/6718335390845095173?lang=en",
            "https://www.tiktok.com/@scout2015/video/6718335390845095173",
            "tiktok",
            "video",
        ),
        ("https://vm.tiktok.com/ZMabc123/", "https://vm.tiktok.com/ZMabc123/", "tiktok", "share"),
        (
            "https://www.tiktok.com/t/ZTabc123/",
            "https://www.tiktok.com/ZTabc123/",
            "tiktok",
            "share",
        ),
        (
            "https://www.deviantart.com/artbysarf/art/I-Love-Whom-I-Love-1240121428",
            "https://www.deviantart.com/artbysarf/art/I-Love-Whom-I-Love-1240121428",
            "deviantart",
            "deviation",
        ),
        ("https://fav.me/dkic3tg", "https://fav.me/dkic3tg", "deviantart", "share"),
        (
            "https://www.deviantart.com/view/1240121428",
            "https://www.deviantart.com/view/1240121428",
            "deviantart",
            "share",
        ),
        (
            "look 👀 https://www.tiktok.com/@scout2015/video/6718335390845095173 lol",
            "https://www.tiktok.com/@scout2015/video/6718335390845095173",
            "tiktok",
            "video",
        ),
    ],
)
def test_normalize_other_platforms(raw, url, platform, kind):
    result = normalize_url(raw, DEFAULT_ALLOWED_DOMAINS)
    assert (result.url, result.platform, result.kind) == (url, platform, kind)
    assert result.requires_login is False


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/watch?v=short",
        "https://www.tiktok.com/@scout2015",
        "https://www.deviantart.com/artbysarf",
        "https://www.deviantart.com/artbysarf/gallery",
        "https://vimeo.com/123",
    ],
)
def test_unsupported_urls_are_rejected(raw):
    with pytest.raises(InvalidURL):
        normalize_url(raw, DEFAULT_ALLOWED_DOMAINS)


def test_platform_table_is_consistent():
    for platform in PLATFORMS.values():
        assert platform.cookie_domain.startswith(".")
        assert platform.login_cookie
        assert all(d in DEFAULT_ALLOWED_DOMAINS for d in platform.domains)


# --------------------------------------------------------------------------- DeviantArt parsing


def _state_html(state: dict) -> str:
    literal = json.dumps(json.dumps(state))  # JS string literal containing the JSON
    return f"<html><script>window.__INITIAL_STATE__ = JSON.parse({literal});</script></html>"


IMAGE_STATE = {
    "@@entities": {
        "user": {"27553587": {"userId": 27553587, "username": "artbySarf"}},
        "deviation": {
            "1240121428": {
                "deviationId": 1240121428,
                "title": "I Love Whom I Love",
                "author": 27553587,
                "url": "https://www.deviantart.com/artbysarf/art/I-Love-Whom-I-Love-1240121428",
                "isVideo": False,
                "isMature": False,
                "filetype": "png",
                "media": {
                    "baseUri": "https://images-wixmp.example/f/abc/dkic3tg.png",
                    "prettyName": "i_love_whom_i_love_by_artbysarf_dkic3tg",
                    "token": ["TOKEN0", "TOKEN1"],
                    "types": [
                        {
                            "t": "preview",
                            "r": 0,
                            "c": "/v1/fill/w_741,h_1079,q_70,strp/<prettyName>-pre.jpg",
                            "h": 1079,
                            "w": 741,
                        },
                        {"t": "fullview", "r": 1, "h": 1920, "w": 1318, "f": 2608447},
                    ],
                },
            }
        },
        "deviationExtended": {
            "1240121428": {"download": {"url": "https://www.deviantart.com/download/x"}}
        },
    }
}

VIDEO_STATE = {
    "@@entities": {
        "user": {"1": {"username": "bone-fish14"}},
        "deviation": {
            "272093412": {
                "deviationId": 272093412,
                "title": "Animated Short - Disconnect",
                "author": 1,
                "url": "https://www.deviantart.com/bone-fish14/art/Animated-Short---Disconnect-272093412",
                "isVideo": True,
                "type": "film",
                "media": {
                    "baseUri": "https://images-wixmp.example/i/aa/d4hzwmc.jpg",
                    "token": [],
                    "types": [
                        {"t": "fullview", "r": -1, "h": 720, "w": 1280},
                        {
                            "t": "video",
                            "r": -1,
                            "h": 360,
                            "w": 640,
                            "q": "360p",
                            "d": 199,
                            "b": "https://cdn.example/v/mp4/360.mp4",
                        },
                        {
                            "t": "video",
                            "r": -1,
                            "h": 720,
                            "w": 1280,
                            "q": "720p",
                            "d": 199,
                            "b": "https://cdn.example/v/mp4/720.mp4",
                        },
                        {
                            "t": "video",
                            "r": -1,
                            "h": 480,
                            "w": 854,
                            "q": "480p",
                            "d": 199,
                            "b": "https://cdn.example/v/mp4/480.mp4",
                        },
                    ],
                },
            }
        },
    }
}


def test_unescape_js_string_handles_js_only_escapes():
    assert deviantart.unescape_js_string(r"a\'b\/c\x41\u00e9\n") == "a'b/cAé\n"
    assert deviantart.unescape_js_string(r"\ud83d\ude00") == "😀"


def test_parse_image_deviation():
    state = deviantart.parse_initial_state(_state_html(IMAGE_STATE))
    dev = deviantart.deviation_from_state(
        state, "https://www.deviantart.com/artbysarf/art/I-Love-Whom-I-Love-1240121428"
    )
    assert dev.is_video is False
    assert dev.media_url == "https://images-wixmp.example/f/abc/dkic3tg.png?token=TOKEN0"
    assert dev.ext == "png" and (dev.width, dev.height) == (1318, 1920)
    assert dev.author == "artbySarf" and dev.title == "I Love Whom I Love"


def test_parse_video_deviation_picks_highest_rendition():
    state = deviantart.parse_initial_state(_state_html(VIDEO_STATE))
    dev = deviantart.deviation_from_state(
        state, "https://www.deviantart.com/bone-fish14/art/Animated-Short---Disconnect-272093412"
    )
    assert dev.is_video and dev.ext == "mp4"
    assert dev.media_url.endswith("/720.mp4") and dev.quality == "720p"
    assert dev.duration == 199.0 and (dev.width, dev.height) == (1280, 720)
    assert dev.author == "bone-fish14"


def test_parse_initial_state_missing():
    with pytest.raises(deviantart.DeviantArtError):
        deviantart.parse_initial_state("<html>please log in</html>")
