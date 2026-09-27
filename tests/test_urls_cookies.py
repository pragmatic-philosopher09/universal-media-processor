from __future__ import annotations

from dataclasses import replace

import pytest

from app.config import DEFAULT_ALLOWED_DOMAINS
from app.cookies import (
    CookieError,
    CookieSource,
    inject_cookies,
    parse_browser_spec,
    parse_cookie_text,
    resolve_cookie_source,
)
from app.urls import InvalidURL, normalize_instagram_url

# ---------------------------------------------------------------------------- URLs


@pytest.mark.parametrize(
    ("raw", "url", "kind", "login"),
    [
        (
            "https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=abc123&utm_source=ig_web",
            "https://www.instagram.com/reel/C1a2B3c4D5e/",
            "reel",
            False,
        ),
        (
            "instagram.com/reels/C1a2B3c4D5e",
            "https://www.instagram.com/reels/C1a2B3c4D5e/",
            "reel",
            False,
        ),
        (
            "https://instagram.com/p/C1a2B3c4D5e/",
            "https://www.instagram.com/p/C1a2B3c4D5e/",
            "post",
            False,
        ),
        (
            "https://www.instagram.com/someuser/reel/C1a2B3c4D5e/",
            "https://www.instagram.com/someuser/reel/C1a2B3c4D5e/",
            "reel",
            False,
        ),
        (
            "https://www.instagram.com/stories/some.user_1/3570766765028588805/",
            "https://www.instagram.com/stories/some.user_1/3570766765028588805/",
            "story",
            True,
        ),
        (
            "https://www.instagram.com/stories/some.user_1/",
            "https://www.instagram.com/stories/some.user_1/",
            "profile-stories",
            True,
        ),
        (
            "https://www.instagram.com/stories/highlights/18090946048123978/",
            "https://www.instagram.com/stories/highlights/18090946048123978/",
            "highlight",
            True,
        ),
        (
            "https://www.instagram.com/share/reel/_abc-123",
            "https://www.instagram.com/share/reel/_abc-123/",
            "share",
            False,
        ),
        (
            "https://www.instagram.com/tv/C1a2B3c4D5e/",
            "https://www.instagram.com/tv/C1a2B3c4D5e/",
            "igtv",
            False,
        ),
    ],
)
def test_normalize_instagram_url(raw, url, kind, login):
    result = normalize_instagram_url(raw, DEFAULT_ALLOWED_DOMAINS)
    assert result.url == url
    assert result.kind == kind
    assert result.requires_login is login


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://www.youtube.com/watch?v=abc",
        "https://evil.com/instagram.com/reel/abc/",
        "https://instagram.com.evil.com/reel/abc/",
        "ftp://www.instagram.com/reel/abc/",
        "https://www.instagram.com/",
        "https://www.instagram.com/reel/",
        "http://127.0.0.1:8000/api/jobs",
    ],
)
def test_rejects_non_instagram_urls(raw):
    with pytest.raises(InvalidURL):
        normalize_instagram_url(raw, DEFAULT_ALLOWED_DOMAINS)


def test_custom_allow_list():
    result = normalize_instagram_url("https://mirror.example/reel/abc/", ("mirror.example",))
    assert result.url == "https://mirror.example/reel/abc/"


# ---------------------------------------------------------------------------- cookies

SESSION = "1234567890%3AabcDEFghiJKL%3A12%3AAYd..."


def test_parse_bare_sessionid():
    assert parse_cookie_text(f"  {SESSION}\n") == {"sessionid": SESSION}


def test_parse_cookie_header():
    text = f'Cookie: ds_user_id=1234567890; sessionid={SESSION}; csrftoken=abc; ig_did="X"'
    cookies = parse_cookie_text(text)
    assert cookies["sessionid"] == SESSION
    assert cookies["ds_user_id"] == "1234567890"
    assert cookies["ig_did"] == "X"


def test_parse_netscape_export():
    text = (
        "# Netscape HTTP Cookie File\n"
        f".instagram.com\tTRUE\t/\tTRUE\t1999999999\tsessionid\t{SESSION}\n"
        ".instagram.com\tTRUE\t/\tTRUE\t1999999999\tds_user_id\t42\n"
        ".facebook.com\tTRUE\t/\tTRUE\t1999999999\tsessionid\tnope\n"
    )
    cookies = parse_cookie_text(text)
    assert cookies == {"sessionid": SESSION, "ds_user_id": "42"}


def test_parse_requires_sessionid():
    with pytest.raises(CookieError):
        parse_cookie_text("csrftoken=abc; ds_user_id=1")
    assert parse_cookie_text("") == {}
    assert parse_cookie_text(None) == {}


def test_parse_browser_spec():
    assert parse_browser_spec("chrome") == ("chrome",)
    assert parse_browser_spec("Chrome:Profile 1") == ("chrome", "Profile 1")
    assert parse_browser_spec("firefox+gnomekeyring:default::personal") == (
        "firefox",
        "default",
        "gnomekeyring",
        "personal",
    )


def test_resolve_priority(settings):
    env = replace(settings, ig_sessionid="env-session")
    assert resolve_cookie_source(env, None) == CookieSource(
        kind="env", cookies={"sessionid": "env-session"}
    )
    request = resolve_cookie_source(env, "user-session")
    assert request.kind == "request" and request.cookies == {"sessionid": "user-session"}
    assert resolve_cookie_source(settings, None).kind == "none"
    browser = replace(settings, ig_cookies_from_browser="safari")
    assert resolve_cookie_source(browser, None).browser_spec == ("safari",)


def test_user_cookies_can_be_disabled(settings):
    locked = replace(settings, allow_user_cookies=False)
    with pytest.raises(CookieError):
        resolve_cookie_source(locked, "abc")


def test_inject_cookies_into_jar():
    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    inject_cookies(jar, CookieSource(kind="request", cookies={"sessionid": "s", "ds_user_id": "1"}))
    names = {c.name: c for c in jar}
    assert set(names) == {"sessionid", "ds_user_id"}
    assert names["sessionid"].domain == ".instagram.com"
    assert names["sessionid"].secure
