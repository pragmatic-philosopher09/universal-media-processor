"""Instagram cookie handling.

Stories (and private content) are only visible to logged-in accounts, so yt-dlp needs a
valid `sessionid` cookie. Cookies can come from, in priority order:

1. the request (user pastes a `sessionid`, a `Cookie:` header string, or a cookies.txt export),
2. `IG_SESSIONID` / `IG_COOKIES` / `IG_COOKIES_FILE` environment variables,
3. `IG_COOKIES_FROM_BROWSER` (yt-dlp reads them straight from a local browser profile).

Pasted cookies are only ever kept in memory and attached to a per-job cookie jar.
"""

from __future__ import annotations

import http.cookiejar
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings

INSTAGRAM_COOKIE_DOMAIN = ".instagram.com"
RELEVANT_COOKIES = {"sessionid", "ds_user_id", "csrftoken", "mid", "ig_did", "rur", "datr"}


class CookieError(ValueError):
    """Raised when pasted cookie text cannot be understood."""


@dataclass(frozen=True)
class CookieSource:
    kind: str  # none | request | env | file | browser
    cookies: dict[str, str] = field(default_factory=dict)
    cookie_file: Path | None = None
    browser_spec: tuple[str, ...] | None = None

    @property
    def authenticated(self) -> bool:
        return self.kind != "none"

    def describe(self) -> str:
        return {
            "none": "anonymous",
            "request": "cookie supplied with the request",
            "env": "server-configured session",
            "file": "server cookies file",
            "browser": "local browser session",
        }[self.kind]


def _parse_netscape(text: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        domain, _flag, _path, _secure, _expiry, name, value = fields[:7]
        if "instagram" not in domain.lower():
            continue
        cookies[name.strip()] = value.strip()
    return cookies


def _parse_header(text: str) -> dict[str, str]:
    text = re.sub(r"^\s*cookie\s*:\s*", "", text, flags=re.IGNORECASE)
    cookies: dict[str, str] = {}
    for pair in re.split(r";|\n", text):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name, value = name.strip(), value.strip().strip('"')
        if name:
            cookies[name] = value
    return cookies


def parse_cookie_text(text: str | None) -> dict[str, str]:
    """Accept a bare sessionid, a `Cookie:` header string or a Netscape cookies.txt export."""
    if text is None or not text.strip():
        return {}
    text = text.strip()
    if any("\t" in line for line in text.splitlines()):
        cookies = _parse_netscape(text)
    elif "=" in text:
        cookies = _parse_header(text)
    else:
        cookies = {"sessionid": text}

    cookies = {k: v for k, v in cookies.items() if v}
    if "sessionid" not in cookies:
        raise CookieError(
            "No `sessionid` cookie found. Paste the sessionid value, the full Cookie header, "
            "or a cookies.txt export from instagram.com."
        )
    return cookies


def parse_browser_spec(spec: str) -> tuple[str, ...]:
    """Mirror yt-dlp's `--cookies-from-browser BROWSER[+KEYRING][:PROFILE][::CONTAINER]`."""
    container: str | None = None
    if "::" in spec:
        spec, container = spec.split("::", 1)
    profile: str | None = None
    if ":" in spec:
        spec, profile = spec.split(":", 1)
    keyring: str | None = None
    if "+" in spec:
        spec, keyring = spec.split("+", 1)
    parts: list[str | None] = [spec.strip().lower(), profile, keyring, container]
    while len(parts) > 1 and parts[-1] is None:
        parts.pop()
    return tuple(parts)


def resolve_cookie_source(settings: Settings, request_cookie_text: str | None) -> CookieSource:
    if request_cookie_text and request_cookie_text.strip():
        if not settings.allow_user_cookies:
            raise CookieError("This server does not accept user-supplied cookies.")
        return CookieSource(kind="request", cookies=parse_cookie_text(request_cookie_text))
    if settings.ig_cookies:
        return CookieSource(kind="env", cookies=parse_cookie_text(settings.ig_cookies))
    if settings.ig_sessionid:
        return CookieSource(kind="env", cookies={"sessionid": settings.ig_sessionid})
    if settings.ig_cookies_file:
        return CookieSource(kind="file", cookie_file=settings.ig_cookies_file)
    if settings.ig_cookies_from_browser:
        return CookieSource(
            kind="browser", browser_spec=parse_browser_spec(settings.ig_cookies_from_browser)
        )
    return CookieSource(kind="none")


def apply_cookie_source_to_opts(opts: dict[str, Any], source: CookieSource) -> None:
    """Set yt-dlp options that must be known before the YoutubeDL object is created."""
    if source.kind == "file" and source.cookie_file:
        opts["cookiefile"] = str(source.cookie_file)
    elif source.kind == "browser" and source.browser_spec:
        opts["cookiesfrombrowser"] = source.browser_spec


def make_cookie(name: str, value: str) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=INSTAGRAM_COOKIE_DOMAIN,
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None},
        rfc2109=False,
    )


def inject_cookies(cookiejar: http.cookiejar.CookieJar, source: CookieSource) -> None:
    """Add in-memory cookies to an existing yt-dlp cookie jar."""
    for name, value in source.cookies.items():
        cookiejar.set_cookie(make_cookie(name, value))
