"""Redirecting file downloads to a faster cloudflared quick tunnel."""

from __future__ import annotations

import anyio
import pytest

from app import download_tunnel
from app.download_tunnel import DownloadTunnel, is_direct_host


@pytest.fixture
def tunnel_up(monkeypatch):
    calls = {"fetch": 0}

    def fetch(metrics):
        calls["fetch"] += 1
        return "fast-words.trycloudflare.com"

    monkeypatch.setattr(download_tunnel, "_fetch_hostname", fetch)
    monkeypatch.setattr(download_tunnel, "_reachable", lambda hostname: True)
    return calls


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1:8766", "localhost:8766", "192.168.1.6:8766", "[::1]:8766", "mac.local", ""],
)
def test_direct_hosts(host):
    assert is_direct_host(host)


@pytest.mark.parametrize("host", ["media-processor.tail1ab246.ts.net", "example.com:443"])
def test_public_hosts(host):
    assert not is_direct_host(host)


def test_redirects_public_host_and_caches(tunnel_up):
    tunnel = DownloadTunnel("127.0.0.1:20241")

    async def run():
        first = await tunnel.redirect_url("app.ts.net", "/api/jobs/a/files/0", "inline=1")
        second = await tunnel.redirect_url("app.ts.net", "/api/jobs/a/archive", "")
        return first, second

    first, second = anyio.run(run)
    assert first == "https://fast-words.trycloudflare.com/api/jobs/a/files/0?inline=1"
    assert second == "https://fast-words.trycloudflare.com/api/jobs/a/archive"
    assert tunnel_up["fetch"] == 1


def test_no_redirect_on_tunnel_or_direct_host(tunnel_up):
    tunnel = DownloadTunnel("127.0.0.1:20241")

    async def run():
        return (
            await tunnel.redirect_url("fast-words.trycloudflare.com", "/x", ""),
            await tunnel.redirect_url("127.0.0.1:8766", "/x", ""),
        )

    assert anyio.run(run) == (None, None)


def test_disabled_or_unhealthy_tunnel_serves_directly(monkeypatch):
    assert anyio.run(DownloadTunnel(None).redirect_url, "app.ts.net", "/x", "") is None

    def broken(metrics):
        raise OSError("connection refused")

    monkeypatch.setattr(download_tunnel, "_fetch_hostname", broken)
    assert anyio.run(DownloadTunnel("127.0.0.1:1").redirect_url, "app.ts.net", "/x", "") is None

    monkeypatch.setattr(
        download_tunnel, "_fetch_hostname", lambda metrics: "dead.trycloudflare.com"
    )
    monkeypatch.setattr(download_tunnel, "_reachable", lambda hostname: False)
    assert anyio.run(DownloadTunnel("127.0.0.1:1").redirect_url, "app.ts.net", "/x", "") is None
