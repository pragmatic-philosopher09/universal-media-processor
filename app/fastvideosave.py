"""Optional public-page fallback. No personal browser profile or provider API keys."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .config import Settings
from .media import CancelToken, JobCancelled

BASE_BROWSER_HOSTS = frozenset({"fastvideosave.net", "challenges.cloudflare.com"})
# FastVideoSave moves the API its page calls between domains. The bundled list is kept
# current by the scheduled GitHub workflow; `python -m app.fastvideosave_probe` learns new
# hosts on a running server into DATA_DIR/HOSTS_FILE_NAME, which is re-read per request.
BUNDLED_HOSTS_FILE = Path(__file__).with_name("fastvideosave_hosts.json")
HOSTS_FILE_NAME = "fastvideosave-hosts.json"
MAX_API_HOSTS = 5
HOSTNAME_RE = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
log = logging.getLogger(__name__)


class FastVideoSaveError(RuntimeError):
    pass


def read_api_hosts(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        log.warning("Ignoring unreadable FastVideoSave hosts file %s", path)
        return []
    hosts = data.get("api_hosts") if isinstance(data, dict) else None
    if not isinstance(hosts, list):
        return []
    valid = [h for h in hosts if isinstance(h, str) and HOSTNAME_RE.fullmatch(h)]
    return list(dict.fromkeys(valid))[:MAX_API_HOSTS]


def write_api_hosts(path: Path, hosts: Collection[str]) -> None:
    hosts = [h for h in hosts if HOSTNAME_RE.fullmatch(h)][:MAX_API_HOSTS]
    payload = {
        "api_hosts": hosts,
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def learned_hosts_file(settings: Settings) -> Path:
    return settings.data_dir / HOSTS_FILE_NAME


BROWSER_HOSTS = BASE_BROWSER_HOSTS | frozenset(read_api_hosts(BUNDLED_HOSTS_FILE))


def browser_hosts(settings: Settings) -> frozenset[str]:
    return BROWSER_HOSTS | frozenset(read_api_hosts(learned_hosts_file(settings)))


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


def _is_cdn_host(host: str) -> bool:
    return any(host.endswith("." + domain) for domain in ("cdninstagram.com", "fbcdn.net"))


def validate_cdn_url(url: str) -> str:
    if not _is_cdn_host(_https_host(url)):
        raise FastVideoSaveError("FastVideoSave returned a non-Instagram media address.")
    return url


def media_url(url: str) -> str:
    # Its download proxies (dl.videodropper.app, cdn.turbosave.app, ...) wrap the CDN address in
    # ?url=; unwrap from any proxy host because the result must still be an Instagram CDN URL.
    if not _is_cdn_host(_https_host(url)):
        values = parse_qs(urlsplit(url).query).get("url", [])
        if len(values) != 1:
            raise FastVideoSaveError("FastVideoSave returned an invalid download link.")
        url = values[0]
    return validate_cdn_url(url)


def browser_request_allowed(
    url: str, resource_type: str, hosts: Collection[str] = BROWSER_HOSTS
) -> bool:
    try:
        return _https_host(url) in hosts and resource_type not in {
            "image",
            "media",
            "font",
        }
    except FastVideoSaveError:
        return False


def fetch_media_urls(
    url: str, settings: Settings, cancel: CancelToken, hosts: Collection[str] | None = None
) -> list[str]:
    from playwright.sync_api import Error, TimeoutError

    hosts = browser_hosts(settings) if hosts is None else hosts
    for attempt in range(2):
        cancel.check()
        try:
            return _fetch_media_urls_once(url, settings, cancel, hosts)
        except Error as exc:
            cancel.check()
            text = str(exc)
            if isinstance(exc, TimeoutError):
                reason = "timeout"
                message = "FastVideoSave timed out. Try again later."
            elif any(
                marker in text
                for marker in (
                    "net::ERR_CONNECTION_",
                    "net::ERR_NETWORK_CHANGED",
                    "net::ERR_INTERNET_DISCONNECTED",
                    "net::ERR_NAME_NOT_RESOLVED",
                    "net::ERR_TIMED_OUT",
                    "net::ERR_EMPTY_RESPONSE",
                    "net::ERR_HTTP2_PROTOCOL_ERROR",
                    "net::ERR_QUIC_PROTOCOL_ERROR",
                )
            ):
                reason = "network"
                message = (
                    "Could not connect to FastVideoSave from this server. Check its network "
                    "connection or try again later."
                )
            elif (
                "Target page, context or browser has been closed" in text or "Page crashed" in text
            ):
                reason = "browser_closed"
                message = (
                    "The FastVideoSave browser closed or crashed while retrieving media. "
                    "Try again later; the server may be low on memory."
                )
            else:
                log.warning("FastVideoSave browser request failed (unclassified Playwright error)")
                raise FastVideoSaveError(
                    "The FastVideoSave browser request failed. Its page may have changed "
                    "or rejected the request. Try again later or use Upload & convert."
                ) from exc
            # Do not log raw Playwright errors: they can contain signed media URLs.
            log.warning("FastVideoSave attempt %s/2 failed (%s)", attempt + 1, reason)
            if attempt == 1:
                raise FastVideoSaveError(message) from exc
    raise AssertionError("Unreachable")


def launch_browser(playwright, settings: Settings):
    from playwright.sync_api import Error

    try:
        return playwright.chromium.launch(
            headless=True, channel=settings.fastvideosave_browser_channel
        )
    except Error as exc:
        text = str(exc)
        if "Executable doesn't exist" in text or (
            "distribution" in text and "is not found" in text
        ):
            log.warning("FastVideoSave browser launch failed (browser missing)")
            raise FastVideoSaveError(
                "The configured FastVideoSave browser is not installed on the server. "
                "Install Playwright Chromium (python -m playwright install chromium) "
                "and unset FASTVIDEOSAVE_BROWSER_CHANNEL, or install the configured channel."
            ) from exc
        if "Host system is missing dependencies" in text:
            log.warning("FastVideoSave browser launch failed (system dependencies missing)")
            raise FastVideoSaveError(
                "The FastVideoSave browser is missing system libraries. The server "
                "operator must run python -m playwright install --with-deps chromium."
            ) from exc
        raise


def provider_page_url(url: str) -> str:
    section = "stories" if urlsplit(url).path.startswith("/stories/") else ""
    return f"https://fastvideosave.net/{section}?" + urlencode({"url": url})


MEDIA_SELECTOR = 'video source[src], video[src], a[aria-label="Save Image"]'
MEDIA_SCRIPT = "(els) => els.map(e => e.href || e.src).filter(Boolean)"


def _fetch_media_urls_once(
    url: str, settings: Settings, cancel: CancelToken, hosts: Collection[str]
) -> list[str]:
    # Imported only when enabled, so ordinary downloads do not need a browser installation.
    from playwright.sync_api import sync_playwright

    cancel.check()
    with sync_playwright() as playwright:
        try:
            browser = launch_browser(playwright, settings)
        except Exception:
            cancel.check()
            raise
        try:
            context = browser.new_context(service_workers="block")
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if browser_request_allowed(
                        route.request.url, route.request.resource_type, hosts
                    )
                    else route.abort()
                ),
            )
            page = context.new_page()
            page.set_default_timeout(15000)
            page.goto(provider_page_url(url), wait_until="domcontentloaded")
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                cancel.check()
                sources = page.locator(MEDIA_SELECTOR).evaluate_all(MEDIA_SCRIPT)
                if sources:
                    urls = list(dict.fromkeys(media_url(src) for src in sources))
                    if len(urls) > 50:
                        raise FastVideoSaveError("FastVideoSave returned too many media items.")
                    return urls
                if page.get_by_text("Oops! Something went wrong", exact=True).count():
                    raise FastVideoSaveError(
                        "FastVideoSave could not retrieve this media. The account may be "
                        "private, the story expired, or the provider temporarily unavailable."
                    )
                page.wait_for_timeout(250)
            raise FastVideoSaveError(
                "FastVideoSave did not return media within 45 seconds. Its service may "
                "be unavailable, require a browser check, or not support this post. "
                "Try later or use Upload & convert."
            )
        finally:
            if browser.is_connected():
                browser.close()


class _CDNRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_cdn_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def media_extension(header: bytes) -> str:
    if header[4:8] == b"ftyp":
        return "mp4"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    raise FastVideoSaveError("Provider download is not an MP4, JPEG, PNG or WebP file.")


def download_media(
    urls: list[str],
    job_dir: Path,
    settings: Settings,
    cancel: CancelToken,
    on_progress: Callable[[float, str], None] | None = None,
) -> list[Path]:
    if not urls or len(urls) > 50:
        raise FastVideoSaveError("FastVideoSave must return between 1 and 50 media items.")
    paths: list[Path] = []
    total_size = 0
    deadline = time.monotonic() + 120
    opener = urllib.request.build_opener(_CDNRedirectHandler())
    try:
        for index, url in enumerate(urls):
            cancel.check()
            path = job_dir / f"src-fastvideosave-{index + 1}.part"
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
                    if not size:
                        extension = media_extension(chunk[:32])
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
                            f"Downloading FastVideoSave media {index + 1}/{len(urls)} "
                            f"({size // 1024} KiB)",
                        )
                if not size:
                    raise FastVideoSaveError("FastVideoSave returned an empty media file.")
            final = path.with_suffix("." + extension)
            path.rename(final)
            paths[-1] = final
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
