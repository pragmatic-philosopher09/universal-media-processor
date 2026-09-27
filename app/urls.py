"""Supported platforms, URL validation and normalisation.

Only known hosts are accepted (configurable allow-list) so the server never fetches arbitrary
URLs on behalf of anonymous visitors.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


class InvalidURL(ValueError):
    """Raised when the submitted URL is not an acceptable media URL."""


@dataclass(frozen=True)
class Platform:
    id: str
    name: str
    domains: tuple[str, ...]
    cookie_domain: str  # domain pasted / discovered cookies are attached to
    login_cookie: str  # cookie whose presence means "logged in"
    canonical_host: str
    max_native: str  # what the platform's CDN tops out at (shown in the UI)
    login_walled: bool  # anonymous access is usually refused for ordinary content
    single_item: bool = True  # never expand playlists / channels


PLATFORMS: dict[str, Platform] = {
    "instagram": Platform(
        id="instagram",
        name="Instagram",
        domains=("instagram.com", "instagr.am", "ig.me"),
        cookie_domain=".instagram.com",
        login_cookie="sessionid",
        canonical_host="www.instagram.com",
        max_native="1080p, usually 30 fps",
        login_walled=True,
    ),
    "youtube": Platform(
        id="youtube",
        name="YouTube",
        domains=("youtube.com", "youtu.be", "youtube-nocookie.com"),
        cookie_domain=".youtube.com",
        login_cookie="SAPISID",
        canonical_host="www.youtube.com",
        max_native="up to 4K/8K natively",
        login_walled=False,
    ),
    "tiktok": Platform(
        id="tiktok",
        name="TikTok",
        domains=("tiktok.com",),
        cookie_domain=".tiktok.com",
        login_cookie="sessionid",
        canonical_host="www.tiktok.com",
        max_native="1080p, 30 fps (some 60)",
        login_walled=False,
    ),
    "deviantart": Platform(
        id="deviantart",
        name="DeviantArt",
        domains=("deviantart.com", "fav.me", "sta.sh"),
        cookie_domain=".deviantart.com",
        login_cookie="auth",
        canonical_host="www.deviantart.com",
        max_native="original upload size (images), 1080p (film)",
        login_walled=False,
    ),
}

DEFAULT_ALLOWED_DOMAINS: tuple[str, ...] = tuple(
    domain for platform in PLATFORMS.values() for domain in platform.domains
)


@dataclass(frozen=True)
class MediaURL:
    url: str
    platform: str  # key of PLATFORMS
    kind: str
    requires_login: bool

    @property
    def platform_info(self) -> Platform:
        return PLATFORMS[self.platform]

    @property
    def needs_redirect(self) -> bool:
        return self.kind == "share"


InstagramURL = MediaURL  # backwards-compatible alias

_TRAILING = ".,;:!?)\"'"
_URL_IN_TEXT_RE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)*(?:"
    + "|".join(re.escape(d) for d in DEFAULT_ALLOWED_DOMAINS)
    + r")/\S+",
    re.I,
)


def extract_url(text: str) -> str:
    """Return the media URL inside pasted text (share sheets often add a caption)."""
    text = text.strip()
    if " " not in text and "\n" not in text:
        return text.rstrip(_TRAILING)
    match = _URL_IN_TEXT_RE.search(text)
    return match.group(0).rstrip(_TRAILING) if match else text


def _host_allowed(host: str, allowed: Iterable[str]) -> bool:
    for domain in allowed:
        domain = domain.lower().lstrip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _platform_for_host(host: str) -> Platform | None:
    for platform in PLATFORMS.values():
        if _host_allowed(host, platform.domains):
            return platform
    return None


# --------------------------------------------------------------------------- per-platform rules

Classifier = Callable[[str, str, dict[str, list[str]]], tuple[str, str, bool] | None]
"""(host, path, query) -> (canonical_url, kind, requires_login) or None when unsupported."""

_IG_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("share", re.compile(r"^/share/(?:reel|p|reels|tv|s)?/?[A-Za-z0-9_-]+/?$"), False),
    ("reel", re.compile(r"^/(?!share/)(?:[^/]+/)?reels?/[A-Za-z0-9_-]+/?$"), False),
    ("post", re.compile(r"^/(?!share/)(?:[^/]+/)?p/[A-Za-z0-9_-]+/?$"), False),
    ("igtv", re.compile(r"^/(?!share/)(?:[^/]+/)?tv/[A-Za-z0-9_-]+/?$"), False),
    ("highlight", re.compile(r"^/stories/highlights/\d+/?$"), True),
    ("story", re.compile(r"^/stories/[A-Za-z0-9_.]+/\d+/?$"), True),
    ("profile-stories", re.compile(r"^/stories/[A-Za-z0-9_.]+/?$"), True),
)


def _classify_instagram(host: str, path: str, query: dict) -> tuple[str, str, bool] | None:
    if path in {"/", "/reel/", "/reels/", "/p/", "/stories/"}:
        return None
    for kind, pattern, login in _IG_PATTERNS:
        if pattern.match(path):
            return urlunsplit(("https", "www.instagram.com", path, "", "")), kind, login
    return None


_YT_ID = r"[A-Za-z0-9_-]{11}"


def _classify_youtube(host: str, path: str, query: dict) -> tuple[str, str, bool] | None:
    video_id: str | None = None
    kind = "video"
    if host.endswith("youtu.be"):
        match = re.match(rf"^/({_YT_ID})/?$", path)
        video_id = match.group(1) if match else None
    elif path.startswith("/watch/"):
        video_id = (query.get("v") or [None])[0]
    else:
        match = re.match(rf"^/(shorts|live|embed|v|e)/({_YT_ID})/?$", path)
        if match:
            kind = "short" if match.group(1) == "shorts" else "video"
            video_id = match.group(2)
    if not video_id or not re.fullmatch(_YT_ID, video_id):
        return None
    canonical = f"https://www.youtube.com/watch?{urlencode({'v': video_id})}"
    if kind == "short":
        canonical = f"https://www.youtube.com/shorts/{video_id}"
    return canonical, kind, False


def _classify_tiktok(host: str, path: str, query: dict) -> tuple[str, str, bool] | None:
    if host in {"vm.tiktok.com", "vt.tiktok.com"} or re.match(r"^/t/[A-Za-z0-9]+/?$", path):
        code = path.strip("/").split("/")[-1]
        if not code:
            return None
        return f"https://{host}/{code}/", "share", False
    match = re.match(r"^/(@[\w.-]+)/(video|photo)/(\d+)/?$", path)
    if match:
        user, media_kind, media_id = match.groups()
        return f"https://www.tiktok.com/{user}/{media_kind}/{media_id}", media_kind, False
    match = re.match(r"^/(?:embed/v2|embed|video)/(\d+)/?$", path)
    if match:
        return f"https://www.tiktok.com/embed/v2/{match.group(1)}", "video", False
    return None


def _classify_deviantart(host: str, path: str, query: dict) -> tuple[str, str, bool] | None:
    if host.endswith("fav.me"):
        match = re.match(r"^/([A-Za-z0-9]+)/?$", path)
        return (f"https://fav.me/{match.group(1)}", "share", False) if match else None
    if host.endswith("sta.sh"):
        return None
    match = re.match(r"^/([\w-]+)/art/([\w%-]+)-(\d+)/?$", path)
    if match:
        user, slug, deviation_id = match.groups()
        return f"https://www.deviantart.com/{user}/art/{slug}-{deviation_id}", "deviation", False
    match = re.match(r"^/(?:view|deviation)/(\d+)/?$", path)
    if match:
        return f"https://www.deviantart.com/view/{match.group(1)}", "share", False
    match = re.match(r"^/art/([\w%-]+)-(\d+)/?$", path)
    if match:
        return (
            f"https://www.deviantart.com/art/{match.group(1)}-{match.group(2)}",
            "deviation",
            False,
        )
    return None


_CLASSIFIERS: dict[str, Classifier] = {
    "instagram": _classify_instagram,
    "youtube": _classify_youtube,
    "tiktok": _classify_tiktok,
    "deviantart": _classify_deviantart,
}

_UNSUPPORTED_HINTS = {
    "instagram": "Please link to a specific reel, post or story.",
    "youtube": "Please link to a single YouTube video or Short (playlists and channels aren't supported).",
    "tiktok": "Please link to a single TikTok video (e.g. tiktok.com/@user/video/…).",
    "deviantart": "Please link to a single deviation (deviantart.com/<artist>/art/<title>-<id>).",
}


def normalize_url(raw: str, allowed_domains: Iterable[str]) -> MediaURL:
    """Validate `raw`, strip tracking noise, identify the platform and media type."""
    if not raw or not raw.strip():
        raise InvalidURL("Please paste a link.")
    candidate = extract_url(raw)
    if "://" not in candidate:
        candidate = "https://" + candidate

    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"}:
        raise InvalidURL("Only http(s) links are supported.")
    host = (parts.hostname or "").lower()
    if not host or not _host_allowed(host, allowed_domains):
        raise InvalidURL(
            "That doesn't look like a supported link. Supported: "
            + ", ".join(p.name for p in PLATFORMS.values())
            + "."
        )
    platform = _platform_for_host(host)
    if platform is None:
        raise InvalidURL("That host is allowed but no extractor knows how to handle it.")

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if not path.endswith("/"):
        path += "/"
    query = parse_qs(parts.query, keep_blank_values=False)
    result = _CLASSIFIERS[platform.id](host, path, query)
    if result is None:
        raise InvalidURL(_UNSUPPORTED_HINTS[platform.id])
    url, kind, requires_login = result
    return MediaURL(url=url, platform=platform.id, kind=kind, requires_login=requires_login)


def normalize_instagram_url(raw: str, allowed_domains: Iterable[str]) -> MediaURL:
    """Backwards-compatible wrapper that also insists on an Instagram host."""
    result = normalize_url(raw, allowed_domains)
    if result.platform != "instagram":
        raise InvalidURL("That doesn't look like an Instagram link.")
    return result
