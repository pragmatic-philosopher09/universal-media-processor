"""yt-dlp wrapper that always fetches Instagram's highest-quality rendition.

Instagram exposes several renditions per video (progressive MP4s plus a DASH manifest). Many
downloaders grab whichever appears first; we ask yt-dlp to rank every rendition by resolution,
then frame rate, then bitrate, and merge the best video and audio streams into one MP4.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError, FormatSorter

from .config import Settings
from .cookies import CookieSource, apply_cookie_source_to_opts, inject_cookies
from .media import CancelToken, JobCancelled
from .urls import InstagramURL

log = logging.getLogger(__name__)

# Resolution first, then frame rate, then bitrate; prefer H.264 among equals for compatibility.
FORMAT_SORT = ["res", "fps", "br", "vcodec:h264"]
FORMAT_SELECTOR = "bv*+ba/b"
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)

ProgressHook = Callable[[float, str], None]

# Instagram CDN URLs carry a base64 `efg` blob whose `vencode_tag` names the rendition class,
# e.g. "xpv_progressive.INSTAGRAM.CLIPS.C3.1080.dash_baseline_1_v1" -> 1080 (short side).
_EFG_RESOLUTION_RE = re.compile(r"\.(\d{3,4})\.")


class ExtractError(RuntimeError):
    """User-facing extraction failure."""


def resolution_hint_from_url(url: str | None) -> int | None:
    if not url:
        return None
    efg = parse_qs(urlsplit(url).query).get("efg", [None])[0]
    if not efg:
        return None
    padded = efg + "=" * (-len(efg) % 4)
    try:
        decoder = base64.urlsafe_b64decode if ("-" in efg or "_" in efg) else base64.b64decode
        payload = json.loads(decoder(padded))
    except (ValueError, UnicodeDecodeError):
        return None
    match = _EFG_RESOLUTION_RE.search(str(payload.get("vencode_tag", "")))
    if not match:
        return None
    value = int(match.group(1))
    return value if 144 <= value <= 4320 else None


def enrich_formats(formats: list[dict[str, Any]]) -> int:
    """Fill in the resolution of video formats yt-dlp could not size; returns how many changed."""
    changed = 0
    for fmt in formats:
        if fmt.get("vcodec") == "none" or fmt.get("height") or fmt.get("width"):
            continue
        hint = resolution_hint_from_url(fmt.get("url"))
        if hint is None:
            continue
        fmt["height"] = hint
        fmt["resolution"] = f"~{hint}p"
        fmt["format_note"] = "resolution from CDN tag"
        FormatSorter._fill_sorting_fields(fmt)
        changed += 1
    return changed


class BestRenditionSelector:
    """Callable yt-dlp format selector: size unsized renditions, re-rank, then pick the best.

    yt-dlp sorts formats before running the selector, so after enrichment we sort again with
    yt-dlp's own FormatSorter (honouring `format_sort`) and hand over to the normal
    `bv*+ba/b` selector.
    """

    def __init__(self, spec: str = FORMAT_SELECTOR) -> None:
        self.spec = spec
        self._ydl: yt_dlp.YoutubeDL | None = None
        self._inner: Callable[[dict[str, Any]], Any] | None = None

    def bind(self, ydl: yt_dlp.YoutubeDL) -> None:
        self._ydl = ydl
        self._inner = ydl.build_format_selector(self.spec)

    def __call__(self, ctx: dict[str, Any]):
        if self._inner is None or self._ydl is None:
            raise RuntimeError("BestRenditionSelector.bind() must be called first")
        formats = ctx["formats"]
        if enrich_formats(formats):
            formats.sort(key=FormatSorter(self._ydl, []).calculate_preference)
        return self._inner(ctx)


@dataclass
class DownloadedItem:
    path: Path
    media_id: str
    title: str | None
    uploader: str | None
    channel: str | None
    width: int | None
    height: int | None
    fps: float | None
    duration: float | None
    format_id: str | None
    webpage_url: str | None

    @property
    def display_name(self) -> str:
        return self.channel or self.uploader or "instagram"


@dataclass
class ExtractResult:
    items: list[DownloadedItem]
    title: str | None
    uploader: str | None
    messages: list[str] = field(default_factory=list)


class _CapturingLogger:
    """yt-dlp logger that keeps the last few messages so failures can be explained."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def _record(self, level: str, msg: str) -> None:
        text = str(msg).strip()
        if not text or text.startswith("[debug]"):
            return
        self.messages.append(text)
        del self.messages[:-40]
        log.log(
            logging.WARNING if level in {"warning", "error"} else logging.DEBUG, "yt-dlp: %s", text
        )

    def debug(self, msg: str) -> None:
        self._record("debug", msg)

    def info(self, msg: str) -> None:
        self._record("info", msg)

    def warning(self, msg: str) -> None:
        self._record("warning", msg)

    def error(self, msg: str) -> None:
        self._record("error", msg)


def resolve_share_url(url: str, timeout: float = 15) -> str:
    """Follow Instagram `/share/...` redirects to the canonical reel/post URL."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = response.geturl()
    except urllib.error.HTTPError as exc:
        final = exc.geturl() or url
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ExtractError(f"Could not resolve the share link: {exc}") from exc
    final = re.sub(r"[?#].*$", "", final)
    if "/share/" in final or not re.search(r"/(?:p|reels?|tv)/[A-Za-z0-9_-]+", final):
        raise ExtractError(
            "Could not resolve the share link; open it in a browser and copy the reel URL."
        )
    return final


def build_ydl_opts(
    job_dir: Path,
    settings: Settings,
    cookie_source: CookieSource,
    logger: _CapturingLogger,
    progress_hook: Callable[[dict[str, Any]], None],
    *,
    single_item: bool,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "format": BestRenditionSelector(),
        "format_sort": FORMAT_SORT,
        "merge_output_format": "mp4",
        "outtmpl": {"default": str(job_dir / "src-%(id)s.%(ext)s")},
        "restrictfilenames": True,
        "noplaylist": single_item,
        "playlistend": 50,
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": logger,
        "progress_hooks": [progress_hook],
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "overwrites": True,
        "nopart": False,
        "writethumbnail": False,
    }
    ffmpeg_dir = Path(settings.ffmpeg_bin).expanduser().parent
    if str(ffmpeg_dir) not in {"", "."}:
        opts["ffmpeg_location"] = str(ffmpeg_dir)
    apply_cookie_source_to_opts(opts, cookie_source)
    return opts


def _entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    if info.get("_type") in {"playlist", "multi_video"}:
        return [entry for entry in (info.get("entries") or []) if entry]
    return [info]


def _downloaded_path(entry: dict[str, Any]) -> Path | None:
    for item in entry.get("requested_downloads") or []:
        path = item.get("filepath") or item.get("_filename")
        if path and Path(path).exists():
            return Path(path)
    for key in ("filepath", "_filename"):
        path = entry.get(key)
        if path and Path(path).exists():
            return Path(path)
    return None


def _to_item(entry: dict[str, Any], path: Path) -> DownloadedItem:
    fps = entry.get("fps")
    return DownloadedItem(
        path=path,
        media_id=str(entry.get("id") or path.stem.removeprefix("src-")),
        title=entry.get("title"),
        uploader=entry.get("uploader"),
        channel=entry.get("channel") or entry.get("uploader_id"),
        width=entry.get("width"),
        height=entry.get("height"),
        fps=float(fps) if fps else None,
        duration=entry.get("duration"),
        format_id=entry.get("format_id"),
        webpage_url=entry.get("webpage_url"),
    )


def friendly_error(message: str, requires_login: bool, authenticated: bool) -> str:
    text = re.sub(r"^ERROR:\s*", "", message.strip())
    text = re.sub(r"^\[[^\]]+\]\s*(?:[\w-]+:\s*)?", "", text)
    lower = text.lower()
    if "log in" in lower or "login" in lower or "cookies" in lower or "checkpoint" in lower:
        if authenticated:
            return (
                "Instagram rejected the session cookie (expired, or the account was challenged). "
                "Log in to instagram.com again and paste a fresh sessionid."
            )
        hint = "Stories" if requires_login else "This content"
        return (
            f"{hint} can only be fetched while logged in. Paste an Instagram sessionid cookie "
            "under Advanced, or configure IG_SESSIONID on the server."
        )
    if ("rate" in lower and "limit" in lower) or "429" in lower or "too many requests" in lower:
        return "Instagram is rate-limiting this server. Wait a few minutes and try again."
    if "private" in lower:
        return "This account is private. Use a session cookie for an account that follows it."
    if any(
        word in lower
        for word in (
            "not found",
            "404",
            "unavailable",
            "not available",
            "removed",
            "does not exist",
        )
    ):
        return "Instagram says this media is unavailable (deleted, private or a broken link)."
    if "unsupported url" in lower:
        return "That Instagram URL type isn't supported yet."
    if "no video" in lower or "no formats" in lower or "requested format" in lower:
        return "No video was found at this link. Photo posts and photo stories aren't supported."
    if "unable to download webpage" in lower or "timed out" in lower:
        return "Couldn't reach Instagram. Check the server's network connection and try again."
    return f"Instagram download failed: {text[:300]}"


def download(
    target: InstagramURL,
    job_dir: Path,
    settings: Settings,
    cookie_source: CookieSource,
    cancel: CancelToken,
    on_progress: ProgressHook | None = None,
) -> ExtractResult:
    """Blocking download of every video at `target` into `job_dir` (run in a worker thread)."""
    url = target.url
    if target.kind == "share":
        url = resolve_share_url(url)

    logger = _CapturingLogger()

    def hook(data: dict[str, Any]) -> None:
        if cancel.cancelled:
            raise DownloadCancelled()
        if not on_progress:
            return
        status = data.get("status")
        info = data.get("info_dict") or {}
        index = info.get("playlist_index") or 1
        count = info.get("n_entries") or info.get("playlist_count") or 1
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            done = data.get("downloaded_bytes") or 0
            fraction = (done / total) if total else 0.0
            overall = ((index - 1) + fraction) / max(count, 1)
            label = f"Downloading {index}/{count}" if count > 1 else "Downloading best rendition"
            on_progress(min(0.99, overall), label)
        elif status == "finished":
            on_progress(min(0.99, index / max(count, 1)), "Merging audio and video")

    opts = build_ydl_opts(
        job_dir, settings, cookie_source, logger, hook, single_item=target.kind == "story"
    )
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            opts["format"].bind(ydl)
            inject_cookies(ydl.cookiejar, cookie_source)
            info = ydl.extract_info(url, download=True)
    except DownloadCancelled as exc:
        raise JobCancelled() from exc
    except (DownloadError, ExtractorError) as exc:
        raise ExtractError(
            friendly_error(str(exc), target.requires_login, cookie_source.authenticated)
        ) from exc
    except yt_dlp.utils.YoutubeDLError as exc:
        raise ExtractError(
            friendly_error(str(exc), target.requires_login, cookie_source.authenticated)
        ) from exc

    if cancel.cancelled:
        raise JobCancelled()
    if not info:
        message = next((m for m in reversed(logger.messages) if "ERROR" in m), None)
        raise ExtractError(
            friendly_error(
                message or "no media returned", target.requires_login, cookie_source.authenticated
            )
        )

    items: list[DownloadedItem] = []
    for entry in _entries(info):
        path = _downloaded_path(entry)
        if path is None:
            continue
        items.append(_to_item(entry, path))

    if not items:
        message = next((m for m in reversed(logger.messages) if "ERROR" in m), None)
        if message:
            raise ExtractError(
                friendly_error(message, target.requires_login, cookie_source.authenticated)
            )
        raise ExtractError("No downloadable video found at this link (photo-only content?).")

    return ExtractResult(
        items=items,
        title=info.get("title"),
        uploader=info.get("uploader") or info.get("channel"),
        messages=list(logger.messages),
    )
