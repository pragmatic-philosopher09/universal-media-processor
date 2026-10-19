"""Health check for the FastVideoSave fallback that re-learns its API host when it moves.

Run periodically on the server (see deploy/macos/install-fastvideosave-probe.sh):

    python -m app.fastvideosave_probe

Learned hosts go to DATA_DIR/fastvideosave-hosts.json, which the app re-reads per request,
so a running server adapts without a restart. Prints one JSON status line on stdout.
Exit codes: 0 healthy, 10 healthy after learning a new API host, 1 broken.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .fastvideosave import (
    BASE_BROWSER_HOSTS,
    HOSTNAME_RE,
    MEDIA_SELECTOR,
    FastVideoSaveError,
    browser_hosts,
    browser_request_allowed,
    fetch_media_urls,
    launch_browser,
    learned_hosts_file,
    provider_page_url,
    read_api_hosts,
    write_api_hosts,
)
from .media import CancelToken

DEFAULT_PROBE_URLS = (
    "https://www.instagram.com/reel/Dd_rcmHoBqo/",
    "https://www.instagram.com/p/DHZuFemSDC7/",
)
EXIT_OK, EXIT_BROKEN, EXIT_ADAPTED = 0, 1, 10
MEDIA_MARKERS = ("cdninstagram.com", "fbcdn.net")
log = logging.getLogger("fastvideosave_probe")


def discovery_request_allowed(url: str, resource_type: str) -> bool:
    """Only FastVideoSave's own page and scripts load (ads stay blocked), but the data
    requests those scripts make may go to any public host so a moved API can be seen."""
    if resource_type in {"fetch", "xhr"}:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and bool(HOSTNAME_RE.fullmatch(parsed.hostname or ""))
        )
    return browser_request_allowed(url, resource_type, BASE_BROWSER_HOSTS)


def discover_api_hosts(url: str, settings: Settings, timeout: float = 45) -> list[str]:
    """Return hosts whose data responses carry Instagram media addresses for `url`."""
    from playwright.sync_api import sync_playwright

    responses = []
    with sync_playwright() as playwright:
        browser = launch_browser(playwright, settings)
        try:
            context = browser.new_context(service_workers="block")
            context.route(
                "**/*",
                lambda route: (
                    route.continue_()
                    if discovery_request_allowed(route.request.url, route.request.resource_type)
                    else route.abort()
                ),
            )
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on(
                "response",
                lambda r: (
                    responses.append(r) if r.request.resource_type in {"fetch", "xhr"} else None
                ),
            )
            page.goto(provider_page_url(url), wait_until="domcontentloaded")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not page.locator(MEDIA_SELECTOR).count():
                page.wait_for_timeout(250)
            hosts: list[str] = []
            for response in responses:
                host = urlsplit(response.url).hostname or ""
                if host in BASE_BROWSER_HOSTS or host in hosts:
                    continue
                try:
                    body = response.text()
                except Exception:
                    continue
                if any(marker in body for marker in MEDIA_MARKERS):
                    hosts.append(host)
            return hosts
        finally:
            if browser.is_connected():
                browser.close()


def check(urls: Sequence[str], settings: Settings, hosts: Collection[str]) -> str | None:
    """Return None when any probe URL works, otherwise the last error."""
    error = "no probe URLs"
    for url in urls:
        try:
            fetch_media_urls(url, settings, CancelToken(), hosts)
            return None
        except FastVideoSaveError as exc:
            error = str(exc)
        except Exception as exc:  # Raw browser errors can contain signed media URLs.
            error = type(exc).__name__
        log.warning("Probe %s failed: %s", url, error)
    return error


def run(urls: Sequence[str], settings: Settings, hosts_file: Path) -> tuple[int, dict]:
    stored = read_api_hosts(hosts_file)
    hosts = browser_hosts(settings) | set(stored)
    error = check(urls, settings, hosts)
    if error is None:
        return EXIT_OK, {"status": "ok", "api_hosts": sorted(hosts)}

    discovered: list[str] = []
    for url in urls:
        try:
            discovered = discover_api_hosts(url, settings)
        except Exception as exc:
            log.warning("Discovery for %s failed: %s", url, type(exc).__name__)
        if discovered:
            break
    new = [host for host in discovered if host not in hosts]
    if not new:
        return EXIT_BROKEN, {
            "status": "broken",
            "error": error,
            "observed_hosts": discovered,
            "hint": "No new API host found; FastVideoSave may be down or its page changed.",
        }
    retry_error = check(urls, settings, hosts | set(new))
    if retry_error is not None:
        return EXIT_BROKEN, {"status": "broken", "error": retry_error, "observed_hosts": new}
    write_api_hosts(hosts_file, new + [host for host in stored if host not in new])
    log.warning("FastVideoSave moved; learned %s into %s", ", ".join(new), hosts_file)
    return EXIT_ADAPTED, {
        "status": "adapted",
        "new_hosts": new,
        "api_hosts": sorted(hosts | set(new)),
        "hosts_file": str(hosts_file),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", action="append", help="public Instagram URL to probe (repeatable)")
    parser.add_argument(
        "--hosts-file",
        type=Path,
        help="where learned hosts are stored (default: DATA_DIR/fastvideosave-hosts.json)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    env_urls = os.environ.get("FASTVIDEOSAVE_PROBE_URLS", "")
    urls = args.url or [u.strip() for u in env_urls.split(",") if u.strip()]
    code, report = run(
        urls or DEFAULT_PROBE_URLS, settings, args.hosts_file or learned_hosts_file(settings)
    )
    report["checked_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    print(json.dumps(report), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
