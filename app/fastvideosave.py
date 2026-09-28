"""Optional public-page fallback. No personal browser profile or provider API keys."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.client import HTTPException
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .config import Settings
from .media import CancelToken, JobCancelled

BROWSER_HOSTS = {"fastvideosave.net", "api.videodropper.app", "challenges.cloudflare.com"}


class FastVideoSaveError(RuntimeError):
    pass


def _https_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
        ):
            return parsed.hostname or ""
    except ValueError:
        pass
    raise FastVideoSaveError("FastVideoSave returned an invalid media address.")


def validate_cdn_url(url: str) -> str:
    host = _https_host(url)
    if not any(host.endswith("." + domain) for domain in ("cdninstagram.com", "fbcdn.net")):
        raise FastVideoSaveError("FastVideoSave returned a non-Instagram media address.")
    return url


def media_url(url: str) -> str:
    if _https_host(url) == "dl.videodropper.app":
        values = parse_qs(urlsplit(url).query).get("url", [])
        if len(values) != 1:
            raise FastVideoSaveError("FastVideoSave returned an invalid download link.")
        url = values[0]
    return validate_cdn_url(url)


def browser_request_allowed(url: str, resource_type: str) -> bool:
    try:
        return _https_host(url) in BROWSER_HOSTS and resource_type not in {
            "image",
            "media",
            "font",
        }
    except FastVideoSaveError:
        return False


def fetch_media_urls(url: str, settings: Settings, cancel: CancelToken) -> list[str]:
    # Imported only when enabled, so ordinary downloads do not need a browser installation.
    from playwright.sync_api import Error, TimeoutError, sync_playwright

    cancel.check()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, channel=settings.fastvideosave_browser_channel
            )
            try:
                context = browser.new_context(service_workers="block")
                context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if browser_request_allowed(route.request.url, route.request.resource_type)
                        else route.abort()
                    ),
                )
                page = context.new_page()
                page.set_default_timeout(15000)
                page.goto(
                    "https://fastvideosave.net/?" + urlencode({"url": url}),
                    wait_until="domcontentloaded",
                )
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    cancel.check()
                    sources = page.locator("video source[src], video[src]").evaluate_all(
                        "(els) => els.map(e => e.src).filter(Boolean)"
                    )
                    if sources:
                        urls = list(dict.fromkeys(media_url(src) for src in sources))
                        if len(urls) > 50:
                            raise FastVideoSaveError("FastVideoSave returned too many videos.")
                        return urls
                    page.wait_for_timeout(250)
                raise FastVideoSaveError(
                    "FastVideoSave did not return a video within 45 seconds. Its service may "
                    "be unavailable, require a browser check, or not support this post. "
                    "Try later or use Upload & convert."
                )
            finally:
                browser.close()
    except TimeoutError as exc:
        raise FastVideoSaveError("FastVideoSave timed out. Try again later.") from exc
    except Error as exc:
        raise FastVideoSaveError(
            "The FastVideoSave browser failed. The server needs Playwright Chromium "
            "(python -m playwright install chromium), or a configured Chrome channel."
        ) from exc


class _CDNRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_cdn_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_media(
    urls: list[str],
    job_dir: Path,
    settings: Settings,
    cancel: CancelToken,
    on_progress: Callable[[float, str], None] | None = None,
) -> list[Path]:
    paths: list[Path] = []
    total_size = 0
    deadline = time.monotonic() + 120
    opener = urllib.request.build_opener(_CDNRedirectHandler())
    try:
        for index, url in enumerate(urls):
            cancel.check()
            path = job_dir / f"src-fastvideosave-{index + 1}.mp4"
            paths.append(path)
            request = urllib.request.Request(
                validate_cdn_url(url),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.instagram.com/"},
            )
            with opener.open(request, timeout=15) as response, path.open("wb") as output:
                validate_cdn_url(response.geturl())
                size = 0
                while True:
                    cancel.check()
                    if time.monotonic() >= deadline:
                        raise FastVideoSaveError("FastVideoSave media download timed out.")
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    if not size and b"ftyp" not in chunk[:32]:
                        raise FastVideoSaveError("FastVideoSave's download was not an MP4 video.")
                    size += len(chunk)
                    total_size += len(chunk)
                    if total_size > settings.max_upload_bytes:
                        raise FastVideoSaveError(
                            "FastVideoSave media exceeds the server's upload/download size limit."
                        )
                    output.write(chunk)
                    if on_progress:
                        on_progress(
                            index / len(urls),
                            f"Downloading FastVideoSave video {index + 1}/{len(urls)} "
                            f"({size // 1024} KiB)",
                        )
                if not size:
                    raise FastVideoSaveError("FastVideoSave returned an empty video.")
        return paths
    except (OSError, HTTPException, ValueError, FastVideoSaveError, JobCancelled) as exc:
        for path in paths:
            path.unlink(missing_ok=True)
        if isinstance(exc, (FastVideoSaveError, JobCancelled)):
            raise
        raise FastVideoSaveError(
            "Could not save the FastVideoSave video. The media link may have expired, "
            "or the server's network/storage is unavailable."
        ) from exc
