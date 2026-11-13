from __future__ import annotations

import http.cookiejar
import stat
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

import app.extractor as extractor
from app.config import Settings
from app.cookies import CookieSource
from app.extractor import ExtractError, classify_error, friendly_error
from app.media import CancelToken, JobCancelled
from app.tor import TorClient, TorError, find_tor, tor_available
from app.urls import MediaURL

TIKTOK_URL = "https://www.tiktok.com/@seerahb2/video/7332982236171472170"
BLOCKED = (
    "ERROR: [TikTok] 7332982236171472170: Unexpected response from webpage request; "
    "please report this issue on https://github.com/yt-dlp/yt-dlp/issues"
)


def fake_tor(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-tor"
    script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def tor_settings(settings: Settings, tmp_path: Path) -> Settings:
    return replace(
        settings,
        tor_mode="auto",
        tor_bin=fake_tor(tmp_path, 'echo "Bootstrapped 100% (done): Done"\nexec sleep 60\n'),
        tor_bootstrap_timeout=5,
    )


def test_settings_from_env_parses_tor(monkeypatch):
    monkeypatch.setenv("TOR_MODE", "ON")
    monkeypatch.setenv("TOR_EXIT_COUNTRIES", "US, nl")
    monkeypatch.setenv("TOR_PLATFORMS", "tiktok,youtube")
    monkeypatch.setenv("TOR_BOOTSTRAP_TIMEOUT", "1")
    settings = Settings.from_env()
    assert settings.tor_mode == "on"
    assert settings.tor_exit_countries == ("us", "nl")
    assert settings.tor_platforms == ("tiktok", "youtube")
    assert settings.tor_bootstrap_timeout == 10  # floored


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TOR_MODE", "sometimes"),
        ("TOR_EXIT_COUNTRIES", "usa"),
        ("TOR_PLATFORMS", "tiktok,myspace"),
    ],
)
def test_settings_from_env_rejects_bad_tor_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        Settings.from_env()


def test_tor_eligibility(settings: Settings):
    auto = replace(settings, tor_mode="auto")
    assert auto.tor_eligible("tiktok")
    assert not auto.tor_eligible("instagram")
    assert not replace(auto, tor_mode="off").tor_eligible("tiktok")
    assert not replace(auto, platform_proxies={"tiktok": "http://p:1"}).tor_eligible("tiktok")
    assert not replace(auto, proxy_url="http://p:1").tor_eligible("tiktok")


def test_find_tor_honours_tor_bin(settings: Settings, tmp_path: Path):
    binary = fake_tor(tmp_path, "exit 0\n")
    assert find_tor(replace(settings, tor_bin=binary)) == binary
    assert find_tor(replace(settings, tor_bin=str(tmp_path / "missing"))) is None
    assert not tor_available(replace(settings, tor_mode="auto", tor_bin=str(tmp_path / "nope")))
    assert not tor_available(replace(settings, tor_mode="off", tor_bin=binary))


def test_command_restricts_exit_country_and_binds_loopback(tor_settings: Settings):
    cmd = TorClient(replace(tor_settings, tor_exit_countries=("us", "nl"))).command("tor", 9999)
    assert cmd[cmd.index("--SocksPort") + 1] == "127.0.0.1:9999"
    assert cmd[cmd.index("--ExitNodes") + 1] == "{us},{nl}"
    assert cmd[cmd.index("--StrictNodes") + 1] == "1"
    open_exit = TorClient(replace(tor_settings, tor_exit_countries=())).command("tor", 9999)
    assert "--ExitNodes" not in open_exit


def test_ensure_starts_once_and_stop_kills(tor_settings: Settings):
    client = TorClient(tor_settings)
    try:
        url = client.ensure()
        proc = client._proc
        assert url.startswith("socks5h://127.0.0.1:")
        assert client.ensure() == url
        assert client._proc is proc
    finally:
        client.stop()
    assert proc is not None and proc.poll() is not None


def test_ensure_restarts_after_tor_dies(tor_settings: Settings):
    client = TorClient(tor_settings)
    try:
        client.ensure()
        first = client._proc
        assert first is not None
        first.kill()
        first.wait()
        client.ensure()
        assert client._proc is not first
    finally:
        client.stop()


def test_ensure_reports_missing_binary(settings: Settings, tmp_path: Path):
    client = TorClient(replace(settings, tor_mode="auto", tor_bin=str(tmp_path / "nope")))
    with pytest.raises(TorError, match="not installed"):
        client.ensure()


def test_ensure_reports_early_exit_and_timeout(tor_settings: Settings, tmp_path: Path):
    crash = fake_tor(tmp_path, 'echo "Failed to parse config"\nexit 1\n')
    with pytest.raises(TorError, match="Failed to parse config"):
        TorClient(replace(tor_settings, tor_bin=crash)).ensure()

    hang = fake_tor(tmp_path, "exec sleep 60\n")
    client = TorClient(replace(tor_settings, tor_bin=hang, tor_bootstrap_timeout=1))
    with pytest.raises(TorError, match="did not connect"):
        client.ensure()
    assert client._proc is None


def test_ensure_can_be_cancelled(tor_settings: Settings, tmp_path: Path):
    hang = fake_tor(tmp_path, "exec sleep 60\n")
    cancel = CancelToken()
    cancel._cancelled = True
    client = TorClient(replace(tor_settings, tor_bin=hang))
    with pytest.raises(JobCancelled):
        client.ensure(cancel)
    assert client._proc is None


def test_geo_block_classification_and_message():
    assert classify_error(BLOCKED)[0] == "geo_blocked"
    message = friendly_error(BLOCKED, False, False, "tiktok")
    assert "TikTok" in message and "Tor" in message and "TIKTOK_PROXY" in message


class FakeYoutubeDL:
    """Fails like TikTok does for a blocked country unless a proxy is configured."""

    calls: list[str | None] = []

    def __init__(self, opts):
        self.opts = opts
        self.cookiejar = http.cookiejar.CookieJar()

    def build_format_selector(self, spec):
        return lambda ctx: iter(())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        FakeYoutubeDL.calls.append(self.opts["proxy"])
        if self.opts["proxy"] is None:
            # the app runs yt-dlp with ignoreerrors=True: log the error and return None
            self.opts["logger"].error(BLOCKED)
            return None
        path = Path(self.opts["outtmpl"]["default"].replace("%(id)s.%(ext)s", "123.mp4"))
        path.write_bytes(b"video")
        return {
            "id": "123",
            "title": "t",
            "uploader": "u",
            "requested_downloads": [{"filepath": str(path)}],
        }


class FakeTor:
    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.ensured = 0

    def ensure(self, cancel=None):
        self.ensured += 1
        if self.fail:
            raise TorError(self.fail)
        return "socks5h://127.0.0.1:1"


@pytest.fixture
def blocked_tiktok(monkeypatch):
    FakeYoutubeDL.calls = []
    monkeypatch.setattr(extractor.yt_dlp, "YoutubeDL", FakeYoutubeDL)

    def install(tor: FakeTor) -> FakeTor:
        monkeypatch.setattr(extractor, "get_tor", lambda settings: tor)
        return tor

    return install


def run_download(settings: Settings, tmp_path: Path, platform: str = "tiktok"):
    target = MediaURL(url=TIKTOK_URL, platform=platform, kind="video", requires_login=False)
    stages: list[str] = []
    result = extractor.download_with_ytdlp(
        target,
        tmp_path / "job",
        settings,
        CookieSource(kind="none"),
        CancelToken(),
        lambda fraction, label: stages.append(label),
    )
    return result, stages


def test_auto_mode_retries_through_tor_after_geo_block(settings, tmp_path, blocked_tiktok):
    tor = blocked_tiktok(FakeTor())
    (tmp_path / "job").mkdir()
    result, stages = run_download(replace(settings, tor_mode="auto"), tmp_path)
    assert len(result.items) == 1
    assert FakeYoutubeDL.calls == [None, "socks5h://127.0.0.1:1"]
    assert tor.ensured == 1
    assert "Routing TikTok through Tor" in stages


def test_on_mode_goes_straight_to_tor(settings, tmp_path, blocked_tiktok):
    tor = blocked_tiktok(FakeTor())
    (tmp_path / "job").mkdir()
    run_download(replace(settings, tor_mode="on"), tmp_path)
    assert FakeYoutubeDL.calls == ["socks5h://127.0.0.1:1"]
    assert tor.ensured == 1


def test_off_mode_and_explicit_proxy_never_touch_tor(settings, tmp_path, blocked_tiktok):
    tor = blocked_tiktok(FakeTor())
    (tmp_path / "job").mkdir()
    with pytest.raises(ExtractError) as err:
        run_download(replace(settings, tor_mode="off"), tmp_path)
    assert err.value.kind == "geo_blocked"
    assert FakeYoutubeDL.calls == [None]

    FakeYoutubeDL.calls = []
    result, _ = run_download(
        replace(settings, tor_mode="on", platform_proxies={"tiktok": "http://mine:8080"}),
        tmp_path,
    )
    assert FakeYoutubeDL.calls == ["http://mine:8080"]
    assert tor.ensured == 0
    assert len(result.items) == 1


def test_other_platforms_do_not_use_tor(settings, tmp_path, blocked_tiktok):
    tor = blocked_tiktok(FakeTor())
    (tmp_path / "job").mkdir()
    with pytest.raises(ExtractError):
        run_download(replace(settings, tor_mode="on"), tmp_path, platform="instagram")
    assert tor.ensured == 0


def test_tor_failure_gives_a_clear_error(settings, tmp_path, blocked_tiktok):
    blocked_tiktok(FakeTor(fail="Tor is not installed."))
    (tmp_path / "job").mkdir()
    with pytest.raises(ExtractError, match="Tor fallback failed: Tor is not installed") as err:
        run_download(replace(settings, tor_mode="auto"), tmp_path)
    assert err.value.kind == "geo_blocked"
