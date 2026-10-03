"""Offline coverage of provider routing, isolation, URL validation and bounded downloads."""

import io
from dataclasses import replace
from unittest.mock import MagicMock
from urllib.parse import urlencode

import pytest

from app import extractor, fastvideosave
from app.config import Settings
from app.cookies import CookieSource
from app.media import CancelToken, JobCancelled
from app.urls import DEFAULT_ALLOWED_DOMAINS, normalize_url

CDN = "https://scontent.cdninstagram.com/video.mp4?signature=example"
POST = "https://www.instagram.com/p/DWtEagdgB6n/"
REEL = "https://www.instagram.com/reel/DWtEagdgB6n/"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100


def test_unwrap_and_validate_provider_media():
    assert fastvideosave.media_url(CDN) == CDN
    assert fastvideosave.media_url("https://dl.videodropper.app/?" + urlencode({"url": CDN})) == CDN
    assert fastvideosave.media_url("https://video.xx.fbcdn.net/video.mp4")


@pytest.mark.parametrize(
    "url",
    [
        "http://scontent.cdninstagram.com/video.mp4",
        "https://scontent.cdninstagram.com.evil.test/video.mp4",
        "https://localhost/video.mp4",
        "https://127.0.0.1/video.mp4",
        "https://user:password@scontent.cdninstagram.com/video.mp4",
        "https://scontent.cdninstagram.com:8000/video.mp4",
        "https://scontent.cdninstagram.com:invalid/video.mp4",
        "file:///etc/passwd",
        "https://dl.videodropper.app/",
        "https://dl.videodropper.app/?url=https://localhost/video.mp4",
        "https://dl.videodropper.app/?url=x&url=y",
    ],
)
def test_unsafe_media_urls_are_rejected(url):
    with pytest.raises(fastvideosave.FastVideoSaveError):
        fastvideosave.media_url(url)


def test_browser_request_allowlist_blocks_ads_media_and_local_network():
    assert fastvideosave.browser_request_allowed("https://fastvideosave.net/", "document")
    assert fastvideosave.browser_request_allowed("https://api.videodropper.app/allinone", "fetch")
    assert not fastvideosave.browser_request_allowed(CDN, "media")
    assert not fastvideosave.browser_request_allowed("https://ads.example/", "script")
    assert not fastvideosave.browser_request_allowed("http://localhost/", "fetch")
    assert not fastvideosave.browser_request_allowed("https://fastvideosave.net:8000/", "fetch")


def test_redirect_destination_is_checked():
    with pytest.raises(fastvideosave.FastVideoSaveError):
        fastvideosave._CDNRedirectHandler().redirect_request(
            None, None, 302, "", {}, "http://127.0.0.1/"
        )


def test_fallback_routing(monkeypatch, tmp_path):
    def fail(*args):
        raise extractor.ExtractError("Instagram rejected request", "audience_restricted")

    fetch = MagicMock(return_value=[CDN])
    save = MagicMock(return_value=[tmp_path / "src-fastvideosave-1.mp4"])
    monkeypatch.setattr(extractor, "download_with_ytdlp", fail)
    monkeypatch.setattr(fastvideosave, "fetch_media_urls", fetch)
    monkeypatch.setattr(fastvideosave, "download_media", save)
    settings = Settings(fastvideosave_enabled=True)
    cancel = CancelToken()
    result = extractor.download(
        normalize_url(POST + "?igsh=tracking", DEFAULT_ALLOWED_DOMAINS),
        tmp_path,
        settings,
        CookieSource("none"),
        cancel,
    )
    fetch.assert_called_once_with(POST, settings, cancel)
    assert result.provider == "FastVideoSave"
    assert result.items[0].media_id == "DWtEagdgB6n"
    assert result.items[0].format_id == "fastvideosave"


@pytest.mark.parametrize(
    "url,enabled,authenticated,kind",
    [
        (POST, False, False, "login"),
        (POST, True, True, "login"),
        (REEL, True, False, "private"),
        (REEL, True, False, "too_long"),
        (REEL, True, False, "unsupported"),
        ("https://www.instagram.com/stories/test/123/", False, False, "login"),
        ("https://www.instagram.com/stories/test/123/", True, True, "login"),
        ("https://www.youtube.com/watch?v=abcdefghi12", True, False, "bot_check"),
    ],
)
def test_ineligible_requests_never_reach_provider(
    monkeypatch, tmp_path, url, enabled, authenticated, kind
):
    def fail(*args):
        raise extractor.ExtractError("direct failure", kind)

    fetch = MagicMock()
    monkeypatch.setattr(extractor, "download_with_ytdlp", fail)
    monkeypatch.setattr(fastvideosave, "fetch_media_urls", fetch)
    with pytest.raises(extractor.ExtractError, match="direct failure"):
        extractor.download(
            normalize_url(url, DEFAULT_ALLOWED_DOMAINS),
            tmp_path,
            Settings(fastvideosave_enabled=enabled),
            CookieSource("request" if authenticated else "none"),
            CancelToken(),
        )
    fetch.assert_not_called()


def test_successful_direct_request_never_reaches_provider(monkeypatch, tmp_path):
    result = extractor.ExtractResult([], None, None)
    monkeypatch.setattr(extractor, "download_with_ytdlp", lambda *args: result)
    fetch = MagicMock()
    monkeypatch.setattr(fastvideosave, "fetch_media_urls", fetch)
    assert (
        extractor.download(
            normalize_url(REEL, DEFAULT_ALLOWED_DOMAINS),
            tmp_path,
            Settings(fastvideosave_enabled=True),
            CookieSource("none"),
            CancelToken(),
        )
        is result
    )
    fetch.assert_not_called()


def test_fallback_failure_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(
        extractor,
        "download_with_ytdlp",
        MagicMock(side_effect=extractor.ExtractError("direct failure", "login")),
    )
    monkeypatch.setattr(
        fastvideosave,
        "fetch_media_urls",
        MagicMock(side_effect=fastvideosave.FastVideoSaveError("service unavailable")),
    )
    with pytest.raises(extractor.ExtractError, match="FastVideoSave fallback: service unavailable"):
        extractor.download(
            normalize_url(POST, DEFAULT_ALLOWED_DOMAINS),
            tmp_path,
            Settings(fastvideosave_enabled=True),
            CookieSource("none"),
            CancelToken(),
        )


def test_captured_error_is_detectable_when_ytdlp_returns_none():
    logger = extractor._CapturingLogger()
    logger.error("This content isn't available to everyone")
    assert "ERROR" in logger.messages[-1]
    assert extractor.classify_error(logger.messages[-1])[0] == "audience_restricted"


@pytest.fixture
def browser(monkeypatch):
    import playwright.sync_api

    manager = MagicMock()
    browser = manager.__enter__.return_value.chromium.launch.return_value
    browser.new_context.return_value.new_page.return_value.get_by_text.return_value.count.return_value = 0
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: manager)
    return browser


def test_browser_deduplicates_and_closes(browser):
    page = browser.new_context.return_value.new_page.return_value
    page.locator.return_value.evaluate_all.return_value = [CDN, CDN]
    assert fastvideosave.fetch_media_urls(POST, Settings(), CancelToken()) == [CDN]
    browser.new_context.assert_called_once_with(service_workers="block")
    browser.close.assert_called_once()


def test_browser_cancellation_closes_browser(browser):
    token = CancelToken()
    page = browser.new_context.return_value.new_page.return_value
    page.locator.return_value.evaluate_all.return_value = []
    page.wait_for_timeout.side_effect = lambda _: token.cancel()
    with pytest.raises(JobCancelled):
        fastvideosave.fetch_media_urls(POST, Settings(), token)
    browser.close.assert_called_once()


def test_browser_timeout_closes_browser(browser, monkeypatch):
    monkeypatch.setattr(fastvideosave.time, "monotonic", iter([0, 50]).__next__)
    with pytest.raises(fastvideosave.FastVideoSaveError, match="did not return media"):
        fastvideosave.fetch_media_urls(POST, Settings(), CancelToken())
    browser.close.assert_called_once()


class Response(io.BytesIO):
    def geturl(self):
        return CDN


def fake_opener(monkeypatch, content):
    opener = MagicMock()
    opener.open.side_effect = lambda *args, **kwargs: Response(content)
    monkeypatch.setattr(fastvideosave.urllib.request, "build_opener", lambda *args: opener)
    return opener


def test_download_is_cookie_free(monkeypatch, tmp_path):
    opener = fake_opener(monkeypatch, MP4)
    paths = fastvideosave.download_media([CDN], tmp_path, Settings(), CancelToken())
    assert paths[0].read_bytes() == MP4
    request = opener.open.call_args.args[0]
    assert not request.has_header("Cookie")
    assert not request.has_header("Authorization")


@pytest.mark.parametrize(
    "content,limit,error",
    [
        (b"", 1024, "empty media"),
        (b"<html>Error</html>", 1024, "not an MP4"),
        (MP4, 10, "size limit"),
    ],
)
def test_bad_downloads_are_removed(monkeypatch, tmp_path, content, limit, error):
    fake_opener(monkeypatch, content)
    with pytest.raises(fastvideosave.FastVideoSaveError, match=error):
        fastvideosave.download_media(
            [CDN], tmp_path, Settings(max_upload_bytes=limit), CancelToken()
        )
    assert not list(tmp_path.iterdir())


def test_total_carousel_size_is_bounded(monkeypatch, tmp_path):
    fake_opener(monkeypatch, MP4)
    settings = replace(Settings(), max_upload_bytes=len(MP4) + 1)
    with pytest.raises(fastvideosave.FastVideoSaveError, match="size limit"):
        fastvideosave.download_media([CDN, CDN], tmp_path, settings, CancelToken())
    assert not list(tmp_path.iterdir())


def test_cancelled_download_removes_partial_files(monkeypatch, tmp_path):
    fake_opener(monkeypatch, MP4)
    token = CancelToken()
    with pytest.raises(JobCancelled):
        fastvideosave.download_media([CDN], tmp_path, Settings(), token, lambda *_: token.cancel())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "header,ext",
    [
        (b"\xff\xd8\xff" + b"\x00" * 20, "jpg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "png"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 20, "webp"),
        (MP4, "mp4"),
    ],
)
def test_media_types_detected_from_bytes(monkeypatch, tmp_path, header, ext):
    fake_opener(monkeypatch, header)
    paths = fastvideosave.download_media([CDN], tmp_path, Settings(), CancelToken())
    assert paths[0].suffix == "." + ext
    assert paths[0].read_bytes() == header
    assert not list(tmp_path.glob("*.part"))


def test_photo_story_browser_routing_and_thumbnail_exclusion(browser):
    page = browser.new_context.return_value.new_page.return_value
    image = "https://scontent.cdninstagram.com/photo.jpg"
    page.locator.return_value.evaluate_all.return_value = [CDN, image, image]
    story = "https://www.instagram.com/stories/nasa/123/"
    assert fastvideosave.fetch_media_urls(story, Settings(), CancelToken()) == [CDN, image]
    assert page.goto.call_args.args[0].startswith("https://fastvideosave.net/stories?")
    selector = page.locator.call_args.args[0]
    assert 'a[aria-label="Save Image"]' in selector
    assert "img" not in selector


@pytest.mark.parametrize("url", [POST, "https://www.instagram.com/stories/nasa/"])
def test_provider_first_keeps_mixed_media(monkeypatch, tmp_path, url):
    direct = MagicMock()
    monkeypatch.setattr(extractor, "download_with_ytdlp", direct)
    monkeypatch.setattr(fastvideosave, "fetch_media_urls", MagicMock(return_value=[CDN, CDN]))
    monkeypatch.setattr(
        fastvideosave,
        "download_media",
        MagicMock(
            return_value=[
                tmp_path / "src-fastvideosave-1.jpg",
                tmp_path / "src-fastvideosave-2.mp4",
            ]
        ),
    )
    result = extractor.download(
        normalize_url(url, DEFAULT_ALLOWED_DOMAINS),
        tmp_path,
        Settings(fastvideosave_enabled=True),
        CookieSource("none"),
        CancelToken(),
    )
    direct.assert_not_called()
    assert [i.is_image for i in result.items] == [True, False]
    assert len({i.media_id for i in result.items}) == 2


def test_provider_failure_warns_about_direct_only_results(monkeypatch, tmp_path):
    monkeypatch.setattr(
        fastvideosave,
        "fetch_media_urls",
        MagicMock(side_effect=fastvideosave.FastVideoSaveError("unavailable")),
    )
    direct_result = extractor.ExtractResult([], None, None)
    monkeypatch.setattr(extractor, "download_with_ytdlp", lambda *args: direct_result)
    result = extractor.download(
        normalize_url(POST, DEFAULT_ALLOWED_DOMAINS),
        tmp_path,
        Settings(fastvideosave_enabled=True),
        CookieSource("none"),
        CancelToken(),
    )
    assert result is direct_result
    assert "photos" in result.warnings[0]


def test_provider_error_is_not_a_success(browser):
    page = browser.new_context.return_value.new_page.return_value
    page.locator.return_value.evaluate_all.return_value = []
    page.get_by_text.return_value.count.return_value = 1
    with pytest.raises(fastvideosave.FastVideoSaveError, match="private"):
        fastvideosave.fetch_media_urls(POST, Settings(), CancelToken())
    browser.close.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        "net::ERR_CONNECTION_RESET at https://fastvideosave.net/",
        "net::ERR_NETWORK_CHANGED",
        "Target page, context or browser has been closed",
        "Page crashed",
    ],
)
def test_transient_browser_failure_retries_once(browser, error, caplog):
    from playwright.sync_api import Error

    page = browser.new_context.return_value.new_page.return_value
    page.goto.side_effect = [Error(error + "?private-token=do-not-log"), None]
    page.locator.return_value.evaluate_all.return_value = [CDN]
    assert fastvideosave.fetch_media_urls(POST, Settings(), CancelToken()) == [CDN]
    assert page.goto.call_count == 2
    assert browser.new_context.call_count == 2
    assert browser.close.call_count == 2
    assert "attempt 1/2 failed" in caplog.text
    assert "do-not-log" not in caplog.text


@pytest.mark.parametrize(
    "error,expected",
    [
        ("net::ERR_CONNECTION_RESET", "Could not connect"),
        ("Page crashed", "closed or crashed"),
    ],
)
def test_transient_browser_failure_stops_after_second_attempt(browser, error, expected):
    from playwright.sync_api import Error

    page = browser.new_context.return_value.new_page.return_value
    page.goto.side_effect = Error(error)
    with pytest.raises(fastvideosave.FastVideoSaveError, match=expected) as failure:
        fastvideosave.fetch_media_urls(POST, Settings(), CancelToken())
    assert "install" not in str(failure.value)
    assert page.goto.call_count == 2
    assert browser.close.call_count == 2


def test_navigation_timeout_retried_but_not_missing_browser(browser):
    from playwright.sync_api import TimeoutError

    page = browser.new_context.return_value.new_page.return_value
    page.goto.side_effect = TimeoutError("Page.goto: Timeout 15000ms exceeded")
    with pytest.raises(fastvideosave.FastVideoSaveError, match="timed out"):
        fastvideosave.fetch_media_urls(POST, Settings(), CancelToken())
    assert page.goto.call_count == 2


def test_unclassified_runtime_error_does_not_suggest_installation(browser):
    from playwright.sync_api import Error

    page = browser.new_context.return_value.new_page.return_value
    page.locator.return_value.evaluate_all.side_effect = Error("Execution context was destroyed")
    with pytest.raises(fastvideosave.FastVideoSaveError, match="browser request failed") as failure:
        fastvideosave.fetch_media_urls(POST, Settings(), CancelToken())
    assert "install" not in str(failure.value)
    page.goto.assert_called_once()
    browser.close.assert_called_once()


@pytest.mark.parametrize(
    "error,expected",
    [
        ("Executable doesn't exist at /missing/chromium", "not installed"),
        ("Chromium distribution 'chrome' is not found", "not installed"),
        ("Host system is missing dependencies to run browsers.", "system libraries"),
    ],
)
def test_launch_setup_errors_are_specific_and_not_retried(monkeypatch, error, expected):
    from playwright import sync_api

    manager = MagicMock()
    launch = manager.__enter__.return_value.chromium.launch
    launch.side_effect = sync_api.Error(error)
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: manager)
    with pytest.raises(fastvideosave.FastVideoSaveError, match=expected):
        fastvideosave.fetch_media_urls(POST, Settings(), CancelToken())
    launch.assert_called_once()


def test_cancellation_during_failure_prevents_retry(browser):
    from playwright.sync_api import Error

    token = CancelToken()
    page = browser.new_context.return_value.new_page.return_value

    def disconnected(*args, **kwargs):
        token.cancel()
        raise Error("net::ERR_CONNECTION_RESET")

    page.goto.side_effect = disconnected
    with pytest.raises(JobCancelled):
        fastvideosave.fetch_media_urls(POST, Settings(), token)
    page.goto.assert_called_once()
    browser.close.assert_called_once()
