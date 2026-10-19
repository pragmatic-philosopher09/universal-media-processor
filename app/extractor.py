"""Download layer: yt-dlp for Instagram / YouTube / TikTok, a custom extractor for DeviantArt.

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

from . import deviantart, fastvideosave
from .config import Settings
from .cookies import CookieSource, apply_cookie_source_to_opts, inject_cookies
from .media import CancelToken, JobCancelled
from .urls import PLATFORMS, MediaURL, normalize_url

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
    """User-facing extraction failure; `kind` lets callers react (e.g. retry with a login)."""

    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def login_required(self) -> bool:
        return self.kind == "login"


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
    is_image: bool = False

    @property
    def ext(self) -> str:
        return self.path.suffix.lstrip(".").lower()

    @property
    def display_name(self) -> str:
        return self.channel or self.uploader or "media"


@dataclass
class ExtractResult:
    items: list[DownloadedItem]
    title: str | None
    uploader: str | None
    messages: list[str] = field(default_factory=list)
    provider: str | None = None
    warnings: list[str] = field(default_factory=list)


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
        self._record("error", msg if str(msg).startswith("ERROR:") else f"ERROR: {msg}")


def resolve_share_url(url: str, allowed_domains, timeout: float = 15) -> MediaURL:
    """Follow short/share links (instagram.com/share/…, vm.tiktok.com/…, fav.me/…) to the media."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = response.geturl()
    except urllib.error.HTTPError as exc:
        final = exc.geturl() or url
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ExtractError(f"Could not resolve the share link: {exc}", "network") from exc
    final = re.sub(r"[?#].*$", "", final) if "youtube" not in final else final
    try:
        resolved = normalize_url(final, allowed_domains)
    except ValueError as exc:
        raise ExtractError(
            "Could not resolve the share link; open it in a browser and copy the final URL.",
            "unsupported",
        ) from exc
    if resolved.kind == "share":
        raise ExtractError(
            "Could not resolve the share link; open it in a browser and copy the final URL.",
            "unsupported",
        )
    return resolved


class TooLong(ExtractError):
    pass


def _duration_filter(limit: int):
    def match_filter(info: dict[str, Any], *, incomplete: bool = False) -> str | None:
        duration = info.get("duration")
        if duration and limit and duration > limit:
            raise TooLong(
                f"This video is {duration / 60:.0f} min long; this server accepts up to "
                f"{limit / 60:.0f} min.",
                "too_long",
            )
        return None

    return match_filter


def build_ydl_opts(
    job_dir: Path,
    settings: Settings,
    cookie_source: CookieSource,
    logger: _CapturingLogger,
    progress_hook: Callable[[dict[str, Any]], None],
    *,
    single_item: bool,
    max_duration: int = 0,
    proxy: str | None = None,
    extractor_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "format": BestRenditionSelector(),
        "format_sort": FORMAT_SORT,
        "merge_output_format": "mp4",
        "outtmpl": {"default": str(job_dir / "src-%(id)s.%(ext)s")},
        "restrictfilenames": True,
        "noplaylist": single_item,
        "playlistend": 50,
        "match_filter": _duration_filter(max_duration) if max_duration else None,
        # YouTube needs a JS runtime for full format access; use whichever is installed.
        "js_runtimes": {"deno": {}, "node": {}},
        "proxy": proxy,
        "extractor_args": extractor_args or {},
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
    is_image = path.suffix.lower().lstrip(".") in deviantart.IMAGE_EXTENSIONS or (
        entry.get("vcodec") == "none" and entry.get("acodec") == "none"
    )
    return DownloadedItem(
        is_image=is_image,
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


def classify_error(message: str) -> tuple[str, str]:
    """Map a raw yt-dlp error to (kind, cleaned text)."""
    text = re.sub(r"^ERROR:\s*", "", message.strip())
    text = re.sub(r"^\[[^\]]+\]\s*(?:[\w-]+:\s*)?", "", text)
    lower = text.lower()
    if "isn't available to everyone" in lower or "can't be seen by certain audiences" in lower:
        return "audience_restricted", text
    if "not a bot" in lower or "sign in to confirm" in lower:
        return "bot_check", text
    if (
        "ip address is blocked" in lower
        or "ip is blocked" in lower
        or "blocked from accessing" in lower
    ):
        return "ip_blocked", text
    if "log in" in lower or "login" in lower or "cookies" in lower or "checkpoint" in lower:
        return "login", text
    if ("rate" in lower and "limit" in lower) or "429" in lower or "too many requests" in lower:
        return "rate_limit", text
    if "private" in lower:
        return "private", text
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
        return "unavailable", text
    if "unsupported url" in lower:
        return "unsupported", text
    if "no video" in lower or "no formats" in lower or "requested format" in lower:
        return "no_video", text
    if "unable to download webpage" in lower or "timed out" in lower:
        return "network", text
    return "other", text


def friendly_error(
    message: str, requires_login: bool, authenticated: bool, platform: str = "instagram"
) -> str:
    info = PLATFORMS.get(platform, PLATFORMS["instagram"])
    name = info.name
    site = info.cookie_domain.lstrip(".")
    kind, text = classify_error(message)
    if kind == "audience_restricted":
        access = "this server's site login" if authenticated else "anonymous requests"
        return (
            f"{name} restricts this post to certain audiences and has not made it available "
            f"to {access}. The platform did not specify which audience rule applies. "
            "This is not an upscaling error. If you already have the video and permission "
            "to use it, use Upload & convert."
        )
    if kind == "login":
        if authenticated:
            return (
                f"{name} rejected the session cookie (expired, or the account was challenged). "
                f"Log in to {site} again and paste a fresh `{info.login_cookie}` cookie."
            )
        if platform == "instagram" and not requires_login:
            return (
                "Instagram did not provide this post to anonymous requests. It may require "
                "a site login, or Instagram may be limiting access from this server. Try again "
                "later or upload the video directly. This is an Instagram restriction, not "
                "a website password requirement. Optional: use your own sessionid cookie "
                "under Advanced for content you are permitted to access."
            )
        hint = "Stories" if requires_login else "This content"
        return (
            f"{hint} can only be fetched while logged in. Paste a {name} `{info.login_cookie}` "
            f"cookie under Advanced, or configure {platform.upper()}_COOKIES on the server."
        )
    if kind == "bot_check":
        return (
            f"{name} is blocking this server's IP address (\"confirm you're not a bot\" — it "
            "treats cloud/datacenter IPs as bots). Options: run the app on your own computer, "
            f"configure {platform.upper()}_COOKIES from a throwaway account, or set PROXY_URL to "
            "a residential proxy."
        )
    if kind == "ip_blocked":
        return (
            f"{name} blocks this server's IP range outright. Run the app on your own computer or "
            "set PROXY_URL to a residential proxy — cookies don't help here."
        )
    if kind == "rate_limit":
        return f"{name} is rate-limiting this server. Wait a few minutes and try again."
    if kind == "private":
        return "This account is private. Use a session cookie for an account that follows it."
    if kind == "unavailable":
        return f"{name} says this media is unavailable (deleted, private or a broken link)."
    if kind == "unsupported":
        return f"That {name} URL type isn't supported yet."
    if kind == "no_video":
        if platform == "instagram":
            return (
                "No video was found at this link. Photo posts and photo stories aren't supported."
            )
        return "No downloadable media was found at this link."
    if kind == "network":
        return f"Couldn't reach {name}. Check the server's network connection and try again."
    return f"{name} download failed: {text[:300]}"


def _extract_error(message: str, target: MediaURL, cookie_source: CookieSource) -> ExtractError:
    kind, _ = classify_error(message)
    return ExtractError(
        friendly_error(
            message, target.requires_login, cookie_source.authenticated, target.platform
        ),
        kind,
    )


def download(
    target: MediaURL,
    job_dir: Path,
    settings: Settings,
    cookie_source: CookieSource,
    cancel: CancelToken,
    on_progress: ProgressHook | None = None,
) -> ExtractResult:
    """Blocking download of every media item at `target` into `job_dir` (run in a worker thread)."""
    if target.kind == "share":
        target = resolve_share_url(target.url, settings.allowed_domains)
    job_dir.mkdir(parents=True, exist_ok=True)
    if target.platform == "deviantart":
        return download_deviantart(target, job_dir, cookie_source, cancel, on_progress)
    provider_first = (
        settings.fastvideosave_enabled
        and target.platform == "instagram"
        and target.kind in {"post", "story", "profile-stories"}
        and not cookie_source.authenticated
    )
    if provider_first:
        # yt-dlp drops still images from Instagram posts/carousels and stories.
        try:
            return download_instagram_media(target, job_dir, settings, cancel, on_progress)
        except ExtractError as provider_error:
            log.info("FastVideoSave retrieval failed; trying direct Instagram extraction")
            try:
                result = download_with_ytdlp(
                    target, job_dir, settings, cookie_source, cancel, on_progress
                )
            except ExtractError as direct_error:
                raise ExtractError(
                    f"{provider_error} Direct Instagram retrieval also failed: {direct_error}",
                    "fallback",
                ) from direct_error
            result.warnings.append(
                "FastVideoSave was unavailable. Direct Instagram retrieval returned video "
                "items only; any photos in the post or stories may be missing."
            )
            return result
    try:
        return download_with_ytdlp(target, job_dir, settings, cookie_source, cancel, on_progress)
    except ExtractError as exc:
        if not (
            settings.fastvideosave_enabled
            and target.platform == "instagram"
            and target.kind in {"post", "reel", "igtv"}
            and not cookie_source.authenticated
            and exc.kind not in {"private", "too_long", "unsupported"}
        ):
            raise
        cancel.check()
        log.info("Instagram %s; trying the configured FastVideoSave fallback", exc.kind)
        if on_progress:
            on_progress(0.0, "Instagram direct fetch failed; contacting FastVideoSave")
        return download_instagram_media(target, job_dir, settings, cancel, on_progress)


def download_instagram_media(
    target: MediaURL,
    job_dir: Path,
    settings: Settings,
    cancel: CancelToken,
    on_progress: ProgressHook | None = None,
) -> ExtractResult:
    cancel.check()
    if on_progress:
        on_progress(0.0, "Retrieving Instagram photos and videos through FastVideoSave")
    try:
        urls = fastvideosave.fetch_media_urls(target.url, settings, cancel)
        paths = fastvideosave.download_media(urls, job_dir, settings, cancel, on_progress)
    except fastvideosave.FastVideoSaveError as exc:
        raise ExtractError(f"FastVideoSave fallback: {exc}", "fallback") from exc
    code = target.url.rstrip("/").rsplit("/", 1)[-1]
    return ExtractResult(
        items=[
            _to_item(
                {
                    "id": f"{code}-{index + 1}" if len(paths) > 1 else code,
                    "title": f"Instagram {code}",
                    "webpage_url": target.url,
                    "format_id": "fastvideosave",
                },
                path,
            )
            for index, path in enumerate(paths)
        ],
        title=f"Instagram {code}",
        uploader=None,
        provider="FastVideoSave",
    )


def download_deviantart(
    target: MediaURL,
    job_dir: Path,
    cookie_source: CookieSource,
    cancel: CancelToken,
    on_progress: ProgressHook | None = None,
) -> ExtractResult:
    try:
        if on_progress:
            on_progress(0.0, "Reading the deviation page")
        deviation = deviantart.fetch_deviation(target.url, cookie_source)
        cancel.check()
        path = job_dir / f"src-{deviation.deviation_id or 'deviation'}.{deviation.ext}"
        label = f"Downloading {deviation.quality or 'original'}"
        deviantart.download_file(
            deviation.media_url,
            path,
            cookie_source,
            cancel,
            (lambda f: on_progress(f, label)) if on_progress else None,
        )
    except deviantart.DeviantArtError as exc:
        raise ExtractError(
            friendly_error(str(exc), False, cookie_source.authenticated, "deviantart")
            if exc.kind in {"login", "rate_limit", "network", "unavailable"}
            else str(exc),
            exc.kind,
        ) from exc
    item = DownloadedItem(
        path=path,
        media_id=deviation.deviation_id or path.stem,
        title=deviation.title,
        uploader=deviation.author,
        channel=deviation.author,
        width=deviation.width,
        height=deviation.height,
        fps=None,
        duration=deviation.duration,
        format_id=deviation.quality,
        webpage_url=deviation.page_url,
        is_image=not deviation.is_video,
    )
    return ExtractResult(items=[item], title=deviation.title, uploader=deviation.author)


def download_with_ytdlp(
    target: MediaURL,
    job_dir: Path,
    settings: Settings,
    cookie_source: CookieSource,
    cancel: CancelToken,
    on_progress: ProgressHook | None = None,
) -> ExtractResult:
    url = target.url
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

    single_item = target.kind == "story" or target.platform != "instagram"

    def run(extractor_args: dict[str, Any] | None = None):
        opts = build_ydl_opts(
            job_dir,
            settings,
            cookie_source,
            logger,
            hook,
            single_item=single_item,
            max_duration=settings.max_source_duration_seconds,
            proxy=settings.proxy_for(target.platform),
            extractor_args=extractor_args,
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            opts["format"].bind(ydl)
            inject_cookies(ydl.cookiejar, cookie_source)
            return ydl.extract_info(url, download=True)

    try:
        try:
            info = run()
        except (DownloadError, ExtractorError, yt_dlp.utils.YoutubeDLError) as exc:
            kind, _ = classify_error(str(exc))
            if target.platform != "youtube" or kind != "bot_check" or cookie_source.authenticated:
                raise
            # Datacenter IPs trip YouTube's bot check on the web client; the TV client often
            # doesn't. Free second attempt before giving up.
            log.info("youtube bot check on web client; retrying with tv client")
            if on_progress:
                on_progress(0.0, "YouTube bot check — retrying with the TV client")
            info = run({"youtube": {"player_client": ["tv", "default"]}})
    except DownloadCancelled as exc:
        raise JobCancelled() from exc
    except (DownloadError, ExtractorError, yt_dlp.utils.YoutubeDLError) as exc:
        raise _extract_error(str(exc), target, cookie_source) from exc

    if cancel.cancelled:
        raise JobCancelled()
    if not info:
        message = next((m for m in reversed(logger.messages) if "ERROR" in m), None)
        raise _extract_error(message or "no media returned", target, cookie_source)

    items: list[DownloadedItem] = []
    for entry in _entries(info):
        path = _downloaded_path(entry)
        if path is None:
            continue
        items.append(_to_item(entry, path))

    if not items:
        message = next((m for m in reversed(logger.messages) if "ERROR" in m), None)
        if message:
            raise _extract_error(message, target, cookie_source)
        raise ExtractError(
            "No downloadable video found at this link (photo-only content?).", "no_video"
        )

    return ExtractResult(
        items=items,
        title=info.get("title"),
        uploader=info.get("uploader") or info.get("channel"),
        messages=list(logger.messages),
    )
