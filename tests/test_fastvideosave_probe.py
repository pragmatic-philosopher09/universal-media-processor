"""Offline coverage of the FastVideoSave health probe and host re-learning."""

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from app import fastvideosave, fastvideosave_probe
from app.config import Settings

URLS = ["https://www.instagram.com/p/DWtEagdgB6n/"]


@pytest.fixture
def settings(tmp_path):
    return replace(Settings(), data_dir=tmp_path)


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
