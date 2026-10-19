"""Health check for the FastVideoSave fallback that re-learns its API host when it moves.

Run periodically on the server (see deploy/macos/install-fastvideosave-probe.sh):

    python -m app.fastvideosave_probe

Learned hosts go to DATA_DIR/fastvideosave-hosts.json, which the app re-reads per request,
so a running server adapts without a restart. The last report is kept in
DATA_DIR/fastvideosave-probe.json and served at /api/providers/fastvideosave.

FastVideoSave's Cloudflare blocks datacenter IPs such as GitHub's runners, so the scheduled
workflow first asks a deployed server what it saw (--from-server URL) and only probes from
the runner when no recent server report is available.

Prints one JSON status line on stdout. Exit codes: 0 healthy, 10 healthy after learning a
new API host, 1 broken, 2 inconclusive (this network is challenged by Cloudflare),
3 no recent server report (--from-server only).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .fastvideosave import (
    BASE_BROWSER_HOSTS,
    BUNDLED_HOSTS_FILE,
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
EXIT_OK, EXIT_BROKEN, EXIT_BLOCKED, EXIT_NO_REPORT, EXIT_ADAPTED = 0, 1, 2, 3, 10
STATUS_FILE_NAME = "fastvideosave-probe.json"
PUBLIC_REPORT_KEYS = ("status", "checked_at", "new_hosts", "error", "hint", "diagnostics")
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


def discover_api_hosts(
    url: str, settings: Settings, timeout: float = 45, diagnostics: dict | None = None
) -> list[str]:
    """Return hosts whose data responses carry Instagram media addresses for `url`.

    `diagnostics`, when given, is filled with what the provider page did (no media URLs)."""
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
            landing = page.goto(provider_page_url(url), wait_until="domcontentloaded")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not page.locator(MEDIA_SELECTOR).count():
                page.wait_for_timeout(250)
            if diagnostics is not None:
                diagnostics.update(page_diagnostics(page, landing, responses))
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


def page_diagnostics(page, landing, responses) -> dict:
    """Summarise the provider page without exposing media or API URLs' query strings."""
    try:
        title = page.title()[:120]
    except Exception:
        title = ""
    challenged = "just a moment" in title.lower() or any(
        (urlsplit(frame.url).hostname or "") == "challenges.cloudflare.com" for frame in page.frames
    )
    return {
        "page_status": landing.status if landing else None,
        "page_title": title,
        "cloudflare_challenge": challenged,
        "data_requests": [f"{urlsplit(r.url).hostname} {r.status}" for r in responses[:20]],
    }


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
    diagnostics: dict = {}
    for url in urls:
        try:
            discovered = discover_api_hosts(url, settings, diagnostics=diagnostics)
        except Exception as exc:
            log.warning("Discovery for %s failed: %s", url, type(exc).__name__)
        if discovered:
            break
    new = [host for host in discovered if host not in hosts]
    if not new and diagnostics.get("cloudflare_challenge"):
        return EXIT_BLOCKED, {
            "status": "blocked",
            "error": error,
            "hint": "Cloudflare challenged this network, so the result is inconclusive.",
            "diagnostics": diagnostics,
        }
    if not new:
        return EXIT_BROKEN, {
            "status": "broken",
            "error": error,
            "observed_hosts": discovered,
            "hint": "No new API host found; FastVideoSave may be down or its page changed.",
            "diagnostics": diagnostics,
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


def status_file(settings: Settings) -> Path:
    return settings.data_dir / STATUS_FILE_NAME


def save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(report) + "\n", encoding="utf-8")
    os.replace(temp, path)


def public_status(settings: Settings) -> dict:
    """What a deployed server reports at /api/providers/fastvideosave (no local paths)."""
    try:
        report = json.loads(status_file(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None
    return {
        "enabled": settings.fastvideosave_enabled,
        "learned_hosts": read_api_hosts(learned_hosts_file(settings)),
        "last_probe": (
            {k: report[k] for k in PUBLIC_REPORT_KEYS if k in report}
            if isinstance(report, dict)
            else None
        ),
    }


def fetch_server_status(base_url: str, timeout: float = 20) -> dict:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("server URL must be http(s)")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/providers/fastvideosave",
        headers={"Accept": "application/json", "User-Agent": "fastvideosave-probe"},
    )
    username = os.environ.get("MEDIAFLOW_USERNAME", "")
    password = os.environ.get("MEDIAFLOW_PASSWORD", "")
    if username or password:
        token = b64encode(f"{username}:{password}".encode()).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read(1_000_000))
    if not isinstance(data, dict):
        raise ValueError("unexpected server response")
    return data


def from_server(
    base_url: str, hosts_file: Path, max_age: timedelta = timedelta(hours=3)
) -> tuple[int, dict]:
    """Adopt a deployed server's verdict; learned hosts are copied into `hosts_file`."""
    try:
        data = fetch_server_status(base_url)
        probe = data.get("last_probe") or {}
        checked = datetime.fromisoformat(probe["checked_at"])
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        return EXIT_NO_REPORT, {"status": "no_report", "error": type(exc).__name__}
    if datetime.now(UTC) - checked > max_age:
        return EXIT_NO_REPORT, {"status": "no_report", "error": "stale", "checked_at": str(checked)}
    status = probe.get("status")
    report = {"source": "server", **probe}
    if status == "blocked":
        return EXIT_BLOCKED, report
    if status not in {"ok", "adapted"}:
        return EXIT_BROKEN, report
    current = read_api_hosts(hosts_file)
    learned = data.get("learned_hosts")
    learned = learned if isinstance(learned, list) else []
    new = [h for h in learned if isinstance(h, str) and HOSTNAME_RE.fullmatch(h)]
    new = [h for h in dict.fromkeys(new) if h not in current]
    if not new:
        return EXIT_OK, report
    write_api_hosts(hosts_file, new + current)
    return EXIT_ADAPTED, {**report, "status": "adapted", "new_hosts": new}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", action="append", help="public Instagram URL to probe (repeatable)")
    parser.add_argument(
        "--hosts-file",
        type=Path,
        help="where learned hosts are stored (default: DATA_DIR/fastvideosave-hosts.json)",
    )
    parser.add_argument(
        "--from-server",
        metavar="URL",
        help="adopt the latest report of the server at URL instead of probing "
        "(credentials: MEDIAFLOW_USERNAME/MEDIAFLOW_PASSWORD)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.from_server:
        code, report = from_server(args.from_server, args.hosts_file or BUNDLED_HOSTS_FILE)
        print(json.dumps(report), flush=True)
        return code
    settings = Settings.from_env()
    env_urls = os.environ.get("FASTVIDEOSAVE_PROBE_URLS", "")
    urls = args.url or [u.strip() for u in env_urls.split(",") if u.strip()]
    code, report = run(
        urls or DEFAULT_PROBE_URLS, settings, args.hosts_file or learned_hosts_file(settings)
    )
    report["checked_at"] = datetime.now(UTC).replace(microsecond=0).isoformat()
    try:
        save_report(status_file(settings), report)
    except OSError as exc:
        log.warning("Could not save probe report: %s", type(exc).__name__)
    print(json.dumps(report), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
