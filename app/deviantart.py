"""DeviantArt extractor (yt-dlp has none).

Every deviation page embeds `window.__INITIAL_STATE__` with the media descriptor:

* films expose ready-to-download MP4 renditions (`media.types[t=video]`, with `q`, `w`, `h`, `b`),
* images expose the original file at `media.baseUri?token=<token[0]>` (`fullview`, `r=1`).

Only public deviations are supported anonymously; mature content needs a DeviantArt login
cookie (`auth`), which the normal cookie plumbing supplies.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cookies import CookieSource
from .media import CancelToken, JobCancelled

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
_STATE_RE = re.compile(r'window\.__INITIAL_STATE__\s*=\s*JSON\.parse\("((?:[^"\\]|\\.)*)"\)')
_JS_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
    "v": "\v",
    "0": "\0",
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


class DeviantArtError(RuntimeError):
    def __init__(self, message: str, kind: str = "other") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Deviation:
    deviation_id: str
    title: str
    author: str | None
    page_url: str
    media_url: str
    ext: str
    width: int | None
    height: int | None
    is_video: bool
    duration: float | None
    is_mature: bool
    quality: str | None = None


def unescape_js_string(text: str) -> str:
    """Decode the body of a JavaScript string literal (JSON.parse("...") payload)."""
    out: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= length:
            break
        nxt = text[i + 1]
        if nxt == "x" and i + 3 < length:
            out.append(chr(int(text[i + 2 : i + 4], 16)))
            i += 4
        elif nxt == "u" and i + 5 < length:
            code = int(text[i + 2 : i + 6], 16)
            i += 6
            if 0xD800 <= code < 0xDC00 and text[i : i + 2] == "\\u":
                low = int(text[i + 2 : i + 6], 16)
                i += 6
                code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
            out.append(chr(code))
        elif nxt in _JS_ESCAPES:
            out.append(_JS_ESCAPES[nxt])
            i += 2
        elif nxt in "\n\u2028\u2029":
            i += 2  # line continuation
        else:
            out.append(nxt)
            i += 2
    return "".join(out)


def parse_initial_state(html: str) -> dict[str, Any]:
    match = _STATE_RE.search(html)
    if not match:
        raise DeviantArtError(
            "Couldn't read the deviation page (DeviantArt may have changed its layout, or the "
            "content requires a login).",
            "login" if "log in" in html.lower()[:200000] and "deviation" not in html else "other",
        )
    return json.loads(unescape_js_string(match.group(1)))


def _pick_deviation(state: dict[str, Any], page_url: str) -> tuple[dict, dict]:
    entities = state.get("@@entities") or {}
    deviations: dict[str, dict] = entities.get("deviation") or {}
    if not deviations:
        raise DeviantArtError("No deviation found on that page.", "unavailable")
    wanted_id = re.search(r"-(\d+)/?$", page_url) or re.search(r"/view/(\d+)", page_url)
    deviation = None
    if wanted_id and wanted_id.group(1) in deviations:
        deviation = deviations[wanted_id.group(1)]
    else:
        deviation = next(
            (
                d
                for d in deviations.values()
                if d.get("url", "").rstrip("/") == page_url.rstrip("/")
            ),
            None,
        ) or next(iter(deviations.values()))
    return deviation, entities.get("user") or {}


def deviation_from_state(state: dict[str, Any], page_url: str) -> Deviation:
    deviation, users = _pick_deviation(state, page_url)
    media = deviation.get("media") or {}
    types: list[dict] = media.get("types") or []
    author = None
    author_id = deviation.get("author")
    if author_id is not None:
        author = (users.get(str(author_id)) or {}).get("username")
    title = deviation.get("title") or f"deviation-{deviation.get('deviationId')}"
    deviation_id = str(deviation.get("deviationId") or "")
    is_mature = bool(deviation.get("isMature"))

    videos = [t for t in types if t.get("t") == "video" and t.get("b")]
    if deviation.get("isVideo") or videos:
        if not videos:
            raise DeviantArtError(
                "This film has no downloadable renditions (it may be mature content that needs "
                "a login).",
                "login" if is_mature else "no_video",
            )
        best = max(videos, key=lambda t: (t.get("h") or 0, t.get("w") or 0, t.get("f") or 0))
        return Deviation(
            deviation_id=deviation_id,
            title=title,
            author=author,
            page_url=deviation.get("url") or page_url,
            media_url=best["b"],
            ext="mp4",
            width=best.get("w"),
            height=best.get("h"),
            is_video=True,
            duration=float(best["d"]) if best.get("d") else None,
            is_mature=is_mature,
            quality=best.get("q"),
        )

    base_uri = media.get("baseUri")
    if not base_uri:
        raise DeviantArtError(
            "This deviation has no downloadable image (literature, journals and mature content "
            "without a login aren't supported).",
            "login" if is_mature else "no_video",
        )
    fullview = next((t for t in types if t.get("t") == "fullview"), None)
    tokens = media.get("token") or []
    media_url = base_uri
    if fullview and fullview.get("r") == 1 and tokens:
        media_url = f"{base_uri}?token={tokens[0]}"
    elif fullview and fullview.get("c"):
        path = fullview["c"].replace("<prettyName>", media.get("prettyName", ""))
        media_url = f"{base_uri}{path}"
        if tokens:
            media_url += f"?token={tokens[-1]}"
    ext = (deviation.get("filetype") or base_uri.rsplit(".", 1)[-1].split("?")[0] or "jpg").lower()
    if ext == "jpeg":
        ext = "jpg"
    if ext not in IMAGE_EXTENSIONS:
        ext = "jpg"
    return Deviation(
        deviation_id=deviation_id,
        title=title,
        author=author,
        page_url=deviation.get("url") or page_url,
        media_url=media_url,
        ext=ext,
        width=(fullview or {}).get("w"),
        height=(fullview or {}).get("h"),
        is_video=False,
        duration=None,
        is_mature=is_mature,
    )


def _request(url: str, cookie_source: CookieSource, method: str = "GET") -> urllib.request.Request:
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "*/*",
        "Referer": "https://www.deviantart.com/",
    }
    if cookie_source.cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookie_source.cookies.items())
    return urllib.request.Request(url, method=method, headers=headers)


def fetch_deviation(url: str, cookie_source: CookieSource, timeout: float = 30) -> Deviation:
    try:
        with urllib.request.urlopen(_request(url, cookie_source), timeout=timeout) as response:
            final_url = response.geturl()
            html = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise DeviantArtError(
                "DeviantArt refused the request (login required?).", "login"
            ) from exc
        if exc.code == 404:
            raise DeviantArtError(
                "That deviation doesn't exist (or was removed).", "unavailable"
            ) from exc
        raise DeviantArtError(f"DeviantArt returned HTTP {exc.code}.", "network") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DeviantArtError(f"Couldn't reach DeviantArt: {exc}", "network") from exc
    state = parse_initial_state(html)
    return deviation_from_state(state, final_url)


def download_file(
    url: str,
    destination: Path,
    cookie_source: CookieSource,
    cancel: CancelToken,
    on_progress: Callable[[float], None] | None = None,
    timeout: float = 60,
) -> Path:
    try:
        with urllib.request.urlopen(_request(url, cookie_source), timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with destination.open("wb") as handle:
                while True:
                    if cancel.cancelled:
                        raise JobCancelled()
                    chunk = response.read(1 << 18)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if on_progress and total:
                        on_progress(min(0.99, done / total))
    except urllib.error.HTTPError as exc:
        destination.unlink(missing_ok=True)
        raise DeviantArtError(f"Media download failed with HTTP {exc.code}.", "network") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        destination.unlink(missing_ok=True)
        raise DeviantArtError(f"Media download failed: {exc}", "network") from exc
    return destination
