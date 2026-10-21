"""Serve large file downloads through a faster public tunnel than the one hosting the UI.

Some reverse tunnels (e.g. Tailscale Funnel over a DERP relay) cap throughput far below the
host's uplink. A cloudflared quick tunnel is much faster but gets a new random hostname on every
restart, so the UI stays on the stable hostname and only file downloads are redirected. The
current hostname is read from cloudflared's metrics server (``/quicktunnel``) and verified to be
reachable before use; when anything is off, downloads are simply served directly.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
import urllib.request

import anyio

log = logging.getLogger(__name__)

OK_TTL = 120.0
FAIL_TTL = 20.0
TIMEOUT = 4.0


def _fetch_hostname(metrics: str) -> str | None:
    with urllib.request.urlopen(f"http://{metrics}/quicktunnel", timeout=TIMEOUT) as resp:
        hostname = (json.load(resp).get("hostname") or "").strip().lower()
    return hostname or None


def _reachable(hostname: str) -> bool:
    req = urllib.request.Request(f"https://{hostname}/", method="HEAD")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status < 400


def is_direct_host(host: str) -> bool:
    """Loopback, LAN and other IP-literal hosts are already as fast as it gets."""
    name = host.lower()
    if name.startswith("["):
        name = name[1:].split("]", 1)[0]
    elif name.count(":") == 1:
        name = name.split(":", 1)[0]
    if name in {"", "localhost"} or name.endswith((".local", ".localhost")):
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


class DownloadTunnel:
    def __init__(self, metrics: str | None) -> None:
        self.metrics = (metrics or "").strip() or None
        self._hostname: str | None = None
        self._expires = 0.0
        self._lock = anyio.Lock()

    async def hostname(self) -> str | None:
        if not self.metrics:
            return None
        async with self._lock:
            if time.monotonic() < self._expires:
                return self._hostname
            hostname = None
            try:
                candidate = await anyio.to_thread.run_sync(_fetch_hostname, self.metrics)
                if candidate and await anyio.to_thread.run_sync(_reachable, candidate):
                    hostname = candidate
            except Exception as exc:  # noqa: BLE001 - any failure just disables the redirect
                log.info("download tunnel unavailable: %s", exc)
            if hostname != self._hostname:
                log.info("download tunnel hostname: %s", hostname or "none")
            self._hostname = hostname
            self._expires = time.monotonic() + (OK_TTL if hostname else FAIL_TTL)
            return hostname

    async def redirect_url(self, host: str, path: str, query: str) -> str | None:
        """Absolute URL on the tunnel for this request, or None to serve it directly."""
        if not self.metrics or is_direct_host(host):
            return None
        hostname = await self.hostname()
        if not hostname or host.lower().split(":", 1)[0] == hostname:
            return None
        return f"https://{hostname}{path}" + (f"?{query}" if query else "")
