"""Instagram URL validation and normalisation.

Only Instagram hosts are accepted (configurable allow-list) so the server never fetches
arbitrary URLs on behalf of anonymous visitors.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


class InvalidURL(ValueError):
    """Raised when the submitted URL is not an acceptable Instagram media URL."""


@dataclass(frozen=True)
class InstagramURL:
    url: str
    kind: str  # reel | post | igtv | story | highlight | share | profile-stories | unknown
    requires_login: bool


_KIND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("share", re.compile(r"^/share/(?:reel|p|reels|tv|s)?/?[A-Za-z0-9_-]+/?$")),
    ("reel", re.compile(r"^/(?:[^/]+/)?reels?/[A-Za-z0-9_-]+/?$")),
    ("post", re.compile(r"^/(?:[^/]+/)?p/[A-Za-z0-9_-]+/?$")),
    ("igtv", re.compile(r"^/(?:[^/]+/)?tv/[A-Za-z0-9_-]+/?$")),
    ("highlight", re.compile(r"^/stories/highlights/\d+/?$")),
    ("story", re.compile(r"^/stories/[A-Za-z0-9_.]+/\d+/?$")),
    ("profile-stories", re.compile(r"^/stories/[A-Za-z0-9_.]+/?$")),
)


def _host_allowed(host: str, allowed: Iterable[str]) -> bool:
    for domain in allowed:
        domain = domain.lower().lstrip(".")
        if host == domain or host.endswith("." + domain):
            return True
    return False


def normalize_instagram_url(raw: str, allowed_domains: Iterable[str]) -> InstagramURL:
    """Validate `raw`, strip tracking noise and classify the media type."""
    if not raw or not raw.strip():
        raise InvalidURL("Please paste an Instagram link.")
    candidate = raw.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate

    parts = urlsplit(candidate)
    if parts.scheme not in {"http", "https"}:
        raise InvalidURL("Only http(s) links are supported.")
    host = (parts.hostname or "").lower()
    if not host or not _host_allowed(host, allowed_domains):
        raise InvalidURL("That doesn't look like an Instagram link.")

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if not path.endswith("/"):
        path += "/"
    if path == "/" or path in {"/reel/", "/reels/", "/p/", "/stories/"}:
        raise InvalidURL("Please link to a specific reel, post or story.")

    kind = "unknown"
    for name, pattern in _KIND_PATTERNS:
        if pattern.match(path):
            kind = name
            break

    # Instagram tolerates the canonical www host for every media type.
    canonical_host = "www.instagram.com" if host.endswith("instagram.com") else host
    url = urlunsplit(("https", canonical_host, path, "", ""))
    return InstagramURL(
        url=url,
        kind=kind,
        requires_login=kind in {"story", "highlight", "profile-stories"},
    )
