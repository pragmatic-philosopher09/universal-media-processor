"""Cookie handling for every supported platform.

Instagram stories (and, these days, most Instagram content) are only visible to logged-in
accounts; YouTube and TikTok occasionally demand a login too. Cookies can come from, in
priority order:

1. the request (user pastes the login cookie value, a `Cookie:` header string, or a cookies.txt
   export),
2. `<PLATFORM>_COOKIES` / `COOKIES_FILE` environment variables,
3. `COOKIES_FROM_BROWSER` (yt-dlp reads them straight from a local browser profile),
4. automatic discovery in a local browser for same-machine requests (see jobs.py).

Pasted cookies are only ever kept in memory and attached to a per-job cookie jar.
"""

from __future__ import annotations

import http.cookiejar
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yt_dlp.cookies import SUPPORTED_BROWSERS, YDLLogger, extract_cookies_from_browser

from .config import Settings
from .urls import PLATFORMS, Platform

log = logging.getLogger(__name__)

INSTAGRAM = PLATFORMS["instagram"]
INSTAGRAM_COOKIE_DOMAIN = INSTAGRAM.cookie_domain
RELEVANT_COOKIES = {"sessionid", "ds_user_id", "csrftoken", "mid", "ig_did", "rur", "datr"}


class CookieError(ValueError):
    """Raised when pasted cookie text cannot be understood."""


@dataclass(frozen=True)
class CookieSource:
    kind: str  # none | request | env | file | browser | browser-auto
    cookies: dict[str, str] = field(default_factory=dict)
    cookie_file: Path | None = None
    browser_spec: tuple[str, ...] | None = None
    browser_name: str | None = None
    domain: str = INSTAGRAM_COOKIE_DOMAIN  # domain in-memory cookies are attached to

    @property
    def authenticated(self) -> bool:
        return self.kind != "none"

    def describe(self) -> str:
        if self.kind in {"browser", "browser-auto"} and self.browser_name:
            name = self.browser_name[:1].upper() + self.browser_name[1:]
            return f"your {name} login"
        return {
            "none": "anonymous",
            "request": "cookie you pasted",
            "env": "server-configured session",
            "file": "server cookies file",
            "browser": "local browser session",
            "browser-auto": "your browser login",
        }[self.kind]


def _parse_netscape(text: str, domain_suffix: str) -> dict[str, str]:
    wanted = domain_suffix.lstrip(".").lower()
    cookies: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        domain, _flag, _path, _secure, _expiry, name, value = fields[:7]
        host = domain.lower().lstrip(".")
        if host != wanted and not host.endswith("." + wanted):
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


def parse_cookie_text(text: str | None, platform: Platform = INSTAGRAM) -> dict[str, str]:
    """Accept a bare login-cookie value, a `Cookie:` header string or a cookies.txt export."""
    if text is None or not text.strip():
        return {}
    text = text.strip()
    if any("\t" in line for line in text.splitlines()):
        cookies = _parse_netscape(text, platform.cookie_domain)
    elif "=" in text:
        cookies = _parse_header(text)
    else:
        cookies = {platform.login_cookie: text}

    cookies = {k: v for k, v in cookies.items() if v}
    if not cookies:
        raise CookieError(
            f"No cookies for {platform.cookie_domain.lstrip('.')} found. Paste the "
            f"`{platform.login_cookie}` value, the full Cookie header, or a cookies.txt export."
        )
    if platform.login_cookie not in cookies and platform.id == "instagram":
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


def resolve_cookie_source(
    settings: Settings, request_cookie_text: str | None, platform: Platform = INSTAGRAM
) -> CookieSource:
    domain = platform.cookie_domain
    if request_cookie_text and request_cookie_text.strip():
        if not settings.allow_user_cookies:
            raise CookieError("This server does not accept user-supplied cookies.")
        cookies = parse_cookie_text(request_cookie_text, platform)
        return CookieSource(kind="request", cookies=cookies, domain=domain)
    configured = settings.platform_cookies.get(platform.id)
    if configured:
        cookies = parse_cookie_text(configured, platform)
        return CookieSource(kind="env", cookies=cookies, domain=domain)
    if settings.cookies_file:
        return CookieSource(kind="file", cookie_file=settings.cookies_file, domain=domain)
    if settings.cookies_from_browser:
        spec = parse_browser_spec(settings.cookies_from_browser)
        return CookieSource(kind="browser", browser_spec=spec, browser_name=spec[0], domain=domain)
    return CookieSource(kind="none", domain=domain)


# --------------------------------------------------------------------------- browser discovery


@dataclass(frozen=True)
class BrowserSession:
    browser: str
    cookies: dict[str, str]
    profile: str | None = None
    domain: str = INSTAGRAM_COOKIE_DOMAIN

    @property
    def label(self) -> str:
        name = self.browser.capitalize()
        return f"{name} ({self.profile})" if self.profile else name

    def as_source(self) -> CookieSource:
        return CookieSource(
            kind="browser-auto", cookies=self.cookies, browser_name=self.label, domain=self.domain
        )


@dataclass(frozen=True)
class DiscoveryResult:
    session: BrowserSession | None
    attempts: tuple[tuple[str, str], ...]  # (browser label, outcome)

    def summary(self) -> str:
        return (
            "; ".join(f"{label}: {outcome}" for label, outcome in self.attempts)
            or "no browsers checked"
        )


OUTCOME_TEXT = {
    "ok": "logged in",
    "not-logged-in": "not logged in",
    "no-cookies": "no cookies for this site",
    "not-installed": "not installed",
    "no-permission": "no permission to read its cookies (grant Full Disk Access)",
    "error": "could not read cookies",
}

# Successful lookups are reused for a while; failures are retried after a shorter pause so a
# user who logs in to Instagram in their browser does not have to restart the server.
_DISCOVERY_TTL_OK = 600.0
_DISCOVERY_TTL_MISS = 45.0
_discovery_cache: dict[tuple, tuple[float, DiscoveryResult]] = {}


def _site_cookies(jar: http.cookiejar.CookieJar, domain_suffix: str) -> dict[str, str]:
    wanted = domain_suffix.lstrip(".").lower()
    cookies: dict[str, str] = {}
    for cookie in jar:
        domain = (cookie.domain or "").lstrip(".").lower()
        if (domain == wanted or domain.endswith("." + wanted)) and cookie.value:
            cookies[cookie.name] = cookie.value
    return cookies


def _instagram_cookies(jar: http.cookiejar.CookieJar) -> dict[str, str]:
    return {
        k: v
        for k, v in _site_cookies(jar, INSTAGRAM_COOKIE_DOMAIN).items()
        if k in RELEVANT_COOKIES
    }


def _classify_failure(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, PermissionError) or "not permitted" in text or "permission" in text:
        return "no-permission"
    if isinstance(exc, FileNotFoundError) or "could not find" in text or "no such file" in text:
        return "not-installed"
    return "error"


def _read_browser(
    entry: str, platform: Platform
) -> tuple[str, str | None, dict[str, str] | None, str]:
    """Return (browser, profile, site cookies or None, outcome) for one order entry."""
    spec = parse_browser_spec(entry)
    browser = spec[0]
    profile = spec[1] if len(spec) > 1 else None
    keyring = spec[2] if len(spec) > 2 else None
    container = spec[3] if len(spec) > 3 else None
    if browser not in SUPPORTED_BROWSERS:
        return browser, profile, None, "error"
    try:
        jar = extract_cookies_from_browser(
            browser, profile, YDLLogger(), keyring=keyring, container=container
        )
    except Exception as exc:  # noqa: BLE001 - any failure just means "not this browser"
        outcome = _classify_failure(exc)
        log.info("no usable %s cookies (%s): %s", entry, outcome, str(exc).splitlines()[0][:160])
        return browser, profile, None, outcome
    cookies = _site_cookies(jar, platform.cookie_domain)
    if cookies.get(platform.login_cookie):
        return browser, profile, cookies, "ok"
    return browser, profile, None, "not-logged-in" if cookies else "no-cookies"


def discover_browser_session(
    settings: Settings,
    platform: Platform = INSTAGRAM,
    *,
    now: float | None = None,
    use_cache: bool = True,
) -> DiscoveryResult:
    """Find a logged-in session for `platform` in a locally installed browser (blocking).

    Entries in `settings.browser_cookie_order` use yt-dlp's `BROWSER[+KEYRING][:PROFILE]`
    syntax, e.g. `chrome` (default profile) or `chrome:Profile 3`. Browsers that are missing,
    locked or unreadable (Safari needs Full Disk Access, Chrome asks the Keychain once) are
    reported in the result instead of raising.
    """
    now = time.time() if now is None else now
    key = (platform.id, *settings.browser_cookie_order)
    cached = _discovery_cache.get(key) if use_cache else None
    if cached:
        stamp, result = cached
        if now - stamp < (_DISCOVERY_TTL_OK if result.session else _DISCOVERY_TTL_MISS):
            return result

    attempts: list[tuple[str, str]] = []
    session: BrowserSession | None = None
    for entry in settings.browser_cookie_order:
        browser, profile, cookies, outcome = _read_browser(entry, platform)
        label = f"{browser.capitalize()} ({profile})" if profile else browser.capitalize()
        attempts.append((label, OUTCOME_TEXT.get(outcome, outcome)))
        if cookies:
            log.info("using %s session from %s", platform.name, label)
            session = BrowserSession(
                browser=browser, cookies=cookies, profile=profile, domain=platform.cookie_domain
            )
            break
    result = DiscoveryResult(session=session, attempts=tuple(attempts))
    _discovery_cache[key] = (now, result)
    return result


def clear_discovery_cache() -> None:
    _discovery_cache.clear()


def apply_cookie_source_to_opts(opts: dict[str, Any], source: CookieSource) -> None:
    """Set yt-dlp options that must be known before the YoutubeDL object is created."""
    if source.kind == "file" and source.cookie_file:
        opts["cookiefile"] = str(source.cookie_file)
    elif source.kind == "browser" and source.browser_spec:
        opts["cookiesfrombrowser"] = source.browser_spec


def make_cookie(
    name: str, value: str, domain: str = INSTAGRAM_COOKIE_DOMAIN
) -> http.cookiejar.Cookie:
    return http.cookiejar.Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
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
        cookiejar.set_cookie(make_cookie(name, value, source.domain))
