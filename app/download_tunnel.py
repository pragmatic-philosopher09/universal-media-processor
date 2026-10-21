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

OK_TTL = 300.0
FAIL_TTL = 10.0
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
        # Reachability is cached per hostname; the hostname itself is re-read every time (a local,
        # millisecond call) so a restarted tunnel's new hostname is picked up immediately.
        self._checked: tuple[str, bool, float] | None = None
        self._lock = anyio.Lock()

    async def hostname(self) -> str | None:
        if not self.metrics:
            return None
        try:
            candidate = await anyio.to_thread.run_sync(_fetch_hostname, self.metrics)
        except Exception as exc:  # noqa: BLE001 - any failure just disables the redirect
            log.debug("download tunnel metrics unavailable: %s", exc)
            return None
        if not candidate:
            return None
        async with self._lock:
            now = time.monotonic()
            if self._checked and self._checked[0] == candidate and now < self._checked[2]:
                return candidate if self._checked[1] else None
            try:
                ok = await anyio.to_thread.run_sync(_reachable, candidate)
            except Exception as exc:  # noqa: BLE001
                log.info("download tunnel %s not reachable yet: %s", candidate, exc)
                ok = False
            if ok and (not self._checked or self._checked[0] != candidate or not self._checked[1]):
                log.info("download tunnel hostname: %s", candidate)
            self._checked = (candidate, ok, now + (OK_TTL if ok else FAIL_TTL))
            return candidate if ok else None

    async def redirect_url(self, host: str, path: str, query: str) -> str | None:
        """Absolute URL on the tunnel for this request, or None to serve it directly."""
        if not self.metrics or is_direct_host(host):
            return None
        hostname = await self.hostname()
        if not hostname or host.lower().split(":", 1)[0] == hostname:
            return None
        return f"https://{hostname}{path}" + (f"?{query}" if query else "")
