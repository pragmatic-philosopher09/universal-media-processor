"""Offline coverage of the FastVideoSave health probe and host re-learning."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app import fastvideosave, fastvideosave_probe
from app.config import Settings
from app.snapsave import SnapSaveError

URLS = ["https://www.instagram.com/p/DWtEagdgB6n/"]


@pytest.fixture
def settings(tmp_path):
    return replace(Settings(), data_dir=tmp_path)


@pytest.fixture(autouse=True)
def fallback_down(monkeypatch):
    fetch = MagicMock(side_effect=SnapSaveError("fallback down"))
    monkeypatch.setattr(fastvideosave_probe, "fetch_video_urls", fetch)
    return fetch


@pytest.fixture
def hosts_file(settings):
    return fastvideosave.learned_hosts_file(settings)


def fake_fetch(working_host):
    def fetch(url, settings, cancel, hosts):
        if working_host not in hosts:
            raise fastvideosave.FastVideoSaveError("did not return media")
        return ["https://scontent.cdninstagram.com/v.mp4"]

    return MagicMock(side_effect=fetch)


def test_healthy_provider_writes_nothing(monkeypatch, settings, hosts_file):
    monkeypatch.setattr(fastvideosave_probe, "fetch_media_urls", fake_fetch("api.getrequest.dev"))
    discover = MagicMock()
    monkeypatch.setattr(fastvideosave_probe, "discover_api_hosts", discover)
    code, report = fastvideosave_probe.run(URLS, settings, hosts_file)
    assert (code, report["status"]) == (fastvideosave_probe.EXIT_OK, "ok")
    discover.assert_not_called()
    assert not hosts_file.exists()


def test_moved_api_is_learned_after_verification(monkeypatch, settings, hosts_file):
    fastvideosave.write_api_hosts(hosts_file, ["api.older.example"])
    monkeypatch.setattr(fastvideosave_probe, "fetch_media_urls", fake_fetch("api.moved.example"))
    monkeypatch.setattr(
        fastvideosave_probe,
        "discover_api_hosts",
        MagicMock(return_value=["api.getrequest.dev", "api.moved.example"]),
    )
    code, report = fastvideosave_probe.run(URLS, settings, hosts_file)
    assert code == fastvideosave_probe.EXIT_ADAPTED
    assert report["new_hosts"] == ["api.moved.example"]
    assert fastvideosave.read_api_hosts(hosts_file) == ["api.moved.example", "api.older.example"]
    assert "api.moved.example" in fastvideosave.browser_hosts(settings)


def test_unverified_discovery_is_not_saved(monkeypatch, settings, hosts_file):
    monkeypatch.setattr(fastvideosave_probe, "fetch_media_urls", fake_fetch("never.example"))
    monkeypatch.setattr(
        fastvideosave_probe, "discover_api_hosts", MagicMock(return_value=["api.moved.example"])
    )
    code, report = fastvideosave_probe.run(URLS, settings, hosts_file)
    assert (code, report["status"]) == (fastvideosave_probe.EXIT_BROKEN, "broken")
    assert not hosts_file.exists()


def test_outage_without_new_host_is_reported(monkeypatch, settings, hosts_file):
    monkeypatch.setattr(fastvideosave_probe, "fetch_media_urls", fake_fetch("never.example"))
    monkeypatch.setattr(
        fastvideosave_probe, "discover_api_hosts", MagicMock(side_effect=RuntimeError("x"))
    )
    code, report = fastvideosave_probe.run(URLS, settings, hosts_file)
    assert code == fastvideosave_probe.EXIT_BROKEN
    assert report["error"] == "did not return media"


def test_working_fallback_makes_outage_degraded(monkeypatch, settings, hosts_file, fallback_down):
    monkeypatch.setattr(fastvideosave_probe, "fetch_media_urls", fake_fetch("never.example"))
    monkeypatch.setattr(fastvideosave_probe, "discover_api_hosts", MagicMock(return_value=[]))
    fallback_down.side_effect = None
    fallback_down.return_value = ["https://cdn.example/v.mp4"]
    code, report = fastvideosave_probe.run(URLS, settings, hosts_file)
    assert (code, report["status"], report["fallback"]) == (
        fastvideosave_probe.EXIT_DEGRADED,
        "degraded",
        "ok",
    )
    assert report["error"] == "did not return media"


def test_failed_fallback_keeps_outage_broken(monkeypatch, settings, hosts_file):
    monkeypatch.setattr(fastvideosave_probe, "fetch_media_urls", fake_fetch("never.example"))
    monkeypatch.setattr(fastvideosave_probe, "discover_api_hosts", MagicMock(return_value=[]))
    code, report = fastvideosave_probe.run(URLS, settings, hosts_file)
    assert (code, report["status"]) == (fastvideosave_probe.EXIT_BROKEN, "broken")
    assert report["fallback"] == "failed: fallback down"


def test_disabled_fallback_is_not_checked(monkeypatch, settings, hosts_file, fallback_down):
    monkeypatch.setattr(fastvideosave_probe, "fetch_media_urls", fake_fetch("never.example"))
    monkeypatch.setattr(fastvideosave_probe, "discover_api_hosts", MagicMock(return_value=[]))
    settings = replace(settings, snapsave_fallback_enabled=False)
    code, report = fastvideosave_probe.run(URLS, settings, hosts_file)
    assert code == fastvideosave_probe.EXIT_BROKEN
    assert report["fallback"] == "failed: disabled"
    fallback_down.assert_not_called()


def test_discovery_blocks_third_party_pages_but_sees_any_data_host():
    allowed = fastvideosave_probe.discovery_request_allowed
    assert allowed("https://fastvideosave.net/", "document")
    assert allowed("https://api.anything.example/v1", "fetch")
    assert allowed("https://api.anything.example/v1", "xhr")
    assert not allowed("https://ads.example/tag.js", "script")
    assert not allowed("https://ads.example/", "document")
    assert not allowed("http://api.anything.example/", "fetch")
    assert not allowed("https://localhost/", "fetch")
    assert not allowed("https://10.0.0.1/", "fetch")
    assert not allowed("https://scontent.cdninstagram.com/v.mp4", "media")


def test_main_prints_json_status(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fastvideosave_probe, "fetch_media_urls", fake_fetch("api.getrequest.dev"))
    assert fastvideosave_probe.main(["--url", URLS[0]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_cloudflare_challenge_is_inconclusive_not_broken(monkeypatch, settings, hosts_file):
    monkeypatch.setattr(fastvideosave_probe, "fetch_media_urls", fake_fetch("never.example"))

    def discover(url, settings, diagnostics):
        diagnostics.update(cloudflare_challenge=True, page_status=403)
        return []

    monkeypatch.setattr(fastvideosave_probe, "discover_api_hosts", discover)
    code, report = fastvideosave_probe.run(URLS, settings, hosts_file)
    assert (code, report["status"]) == (fastvideosave_probe.EXIT_BLOCKED, "blocked")


def test_main_saves_report_for_status_endpoint(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(fastvideosave_probe, "fetch_media_urls", fake_fetch("api.getrequest.dev"))
    fastvideosave_probe.main(["--url", URLS[0]])
    status = fastvideosave_probe.public_status(replace(Settings(), data_dir=tmp_path))
    assert status["last_probe"]["status"] == "ok"
    assert "api_hosts" not in status["last_probe"]


def server_reply(status="ok", learned=(), age=timedelta(minutes=5)):
    checked = (datetime.now(UTC) - age).isoformat()
    return {"learned_hosts": list(learned), "last_probe": {"status": status, "checked_at": checked}}


def test_from_server_copies_learned_hosts(monkeypatch, tmp_path):
    bundled = tmp_path / "bundled.json"
    fastvideosave.write_api_hosts(bundled, ["api.getrequest.dev"])
    monkeypatch.setattr(
        fastvideosave_probe,
        "fetch_server_status",
        lambda url: server_reply("adapted", ["api.moved.example", "api.getrequest.dev"]),
    )
    code, report = fastvideosave_probe.from_server("https://srv.example", bundled)
    assert code == fastvideosave_probe.EXIT_ADAPTED
    assert report["new_hosts"] == ["api.moved.example"]
    assert fastvideosave.read_api_hosts(bundled) == ["api.moved.example", "api.getrequest.dev"]
    code, _ = fastvideosave_probe.from_server("https://srv.example", bundled)
    assert code == fastvideosave_probe.EXIT_OK


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (server_reply("broken"), fastvideosave_probe.EXIT_BROKEN),
        (server_reply("blocked"), fastvideosave_probe.EXIT_BLOCKED),
        (server_reply("degraded"), fastvideosave_probe.EXIT_DEGRADED),
        (server_reply("ok", age=timedelta(hours=5)), fastvideosave_probe.EXIT_NO_REPORT),
        ({"last_probe": None}, fastvideosave_probe.EXIT_NO_REPORT),
    ],
)
def test_from_server_verdicts(monkeypatch, tmp_path, reply, expected):
    monkeypatch.setattr(fastvideosave_probe, "fetch_server_status", lambda url: reply)
    code, _ = fastvideosave_probe.from_server("https://srv.example", tmp_path / "h.json")
    assert code == expected
    assert not (tmp_path / "h.json").exists()


def test_unreachable_server_has_no_report(tmp_path):
    code, _ = fastvideosave_probe.from_server("http://127.0.0.1:9", tmp_path / "h.json")
    assert code == fastvideosave_probe.EXIT_NO_REPORT
