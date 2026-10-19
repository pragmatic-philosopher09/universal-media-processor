"""Restores sound to Instagram videos that FastVideoSave returns without an audio track.

Instagram now serves many reels as separate DASH video and audio streams, and FastVideoSave
only returns the video stream. SnapSave's public page returns the muxed file; its audio track
is copied (not re-encoded) onto the FastVideoSave video. Public links only: no cookies or keys.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.request
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .fastvideosave import (
    FastVideoSaveError,
    browser_request_allowed,
    launch_browser,
    media_extension,
    validate_cdn_url,
)
from .media import CancelToken

BROWSER_HOSTS = frozenset({"snapsave.app", "challenges.cloudflare.com"})
DOWNLOAD_DOMAIN = "rapidcdn.app"
PAGE = "https://snapsave.app/instagram-reels-video-download"
FETCH_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 90
MAX_DURATION_GAP = 1.0
log = logging.getLogger(__name__)


class SnapSaveError(RuntimeError):
    pass


def download_url_allowed(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and host.endswith("." + DOWNLOAD_DOMAIN)
        )
    except ValueError:
        return False


def fetch_video_urls(url: str, settings: Settings, cancel: CancelToken) -> list[str]:
    from playwright.sync_api import Error, sync_playwright

    cancel.check()
    try:
        with sync_playwright() as playwright:
            browser = launch_browser(playwright, settings)
            try:
                context = browser.new_context(service_workers="block")
                context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if browser_request_allowed(
                            route.request.url, route.request.resource_type, BROWSER_HOSTS
                        )
                        else route.abort()
                    ),
                )
                page = context.new_page()
                page.set_default_timeout(15000)
                page.goto(PAGE, wait_until="domcontentloaded")
                box = page.locator("input#url")
                box.fill(url)
                box.press("Enter")
                deadline = time.monotonic() + FETCH_TIMEOUT
                while time.monotonic() < deadline:
                    cancel.check()
                    hrefs = page.locator("#download-section a[href]").evaluate_all(
                        "(els) => els.map(e => e.href)"
                    )
                    links = list(dict.fromkeys(h for h in hrefs if download_url_allowed(h)))
                    if links:
                        return links
                    page.wait_for_timeout(250)
                raise SnapSaveError("SnapSave did not return this video.")
            finally:
                if browser.is_connected():
                    browser.close()
    except (Error, FastVideoSaveError) as exc:
        cancel.check()
        # Raw browser errors can contain signed media URLs, so only the type is kept.
        raise SnapSaveError(f"SnapSave browser request failed ({type(exc).__name__}).") from exc


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not download_url_allowed(newurl):
            try:
                validate_cdn_url(newurl)
            except FastVideoSaveError as exc:
                raise SnapSaveError("SnapSave redirected to an unexpected host.") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, path: Path, settings: Settings, cancel: CancelToken) -> Path:
    if not download_url_allowed(url):
        raise SnapSaveError("SnapSave returned an unexpected download address.")
    opener = urllib.request.build_opener(_RedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    size = 0
    try:
        with opener.open(request, timeout=20) as response, path.open("wb") as output:
            while chunk := response.read(256 * 1024):
                cancel.check()
                if not size and media_extension(chunk[:32]) != "mp4":
                    raise SnapSaveError("SnapSave download is not an MP4 video.")
                size += len(chunk)
                if size > settings.max_upload_bytes or time.monotonic() > deadline:
                    raise SnapSaveError("SnapSave download was too large or too slow.")
                output.write(chunk)
    except (OSError, ValueError, HTTPException, FastVideoSaveError) as exc:
        path.unlink(missing_ok=True)
        raise SnapSaveError(f"SnapSave download failed ({type(exc).__name__}).") from exc
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def stream_summary(path: Path, settings: Settings) -> tuple[bool, bool, float]:
    """(has moving video, has audio, duration seconds) from ffprobe."""
    result = subprocess.run(
        [
            settings.ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        return False, False, 0.0
    data = json.loads(result.stdout or b"{}")
    streams = data.get("streams", [])
    try:
        duration = float(data.get("format", {}).get("duration") or 0)
    except ValueError:
        duration = 0.0
    video = any(
        s.get("codec_type") == "video" and s.get("codec_name") not in {"mjpeg", "png", "webp"}
        for s in streams
    )
    audio = any(s.get("codec_type") == "audio" for s in streams)
    return video and duration > 0, audio, duration


def is_silent_video(path: Path, settings: Settings) -> bool:
    video, audio, _ = stream_summary(path, settings)
    return video and not audio


def add_audio(video: Path, post_url: str, settings: Settings, cancel: CancelToken) -> None:
    """Copy SnapSave's audio track onto `video` in place. Raises SnapSaveError on failure."""
    _, _, duration = stream_summary(video, settings)
    try:
        links = fetch_video_urls(post_url, settings, cancel)
    except SnapSaveError as exc:
        # SnapSave intermittently stalls on a first request; one fresh attempt usually works.
        log.info("SnapSave attempt 1/2 failed (%s); retrying", exc)
        links = fetch_video_urls(post_url, settings, cancel)
    if len(links) != 1:
        raise SnapSaveError("SnapSave returned several items, so the audio could not be matched.")
    donor = video.with_name(video.stem + "-snapsave.part")
    merged = video.with_name(video.stem + "-with-audio" + video.suffix)
    try:
        download(links[0], donor, settings, cancel)
        _, has_audio, donor_duration = stream_summary(donor, settings)
        if not has_audio:
            raise SnapSaveError("SnapSave's copy has no audio either.")
        if abs(donor_duration - duration) > MAX_DURATION_GAP:
            raise SnapSaveError("SnapSave's copy does not match the video's length.")
        cancel.check()
        result = subprocess.run(
            [
                settings.ffmpeg_bin,
                "-y",
                "-v",
                "error",
                "-i",
                str(video),
                "-i",
                str(donor),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(merged),
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0 or not stream_summary(merged, settings)[1]:
            raise SnapSaveError("Could not add the audio track to the video.")
        os.replace(merged, video)
    finally:
        donor.unlink(missing_ok=True)
        merged.unlink(missing_ok=True)
