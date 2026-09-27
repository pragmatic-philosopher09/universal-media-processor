"""Automatic fallback to the operator's browser login for same-machine requests."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace

import pytest

from app import jobs as jobs_module
from app.cookies import BrowserSession, DiscoveryResult
from app.extractor import DownloadedItem, ExtractError, ExtractResult
from app.jobs import JobManager, JobOptions

from .conftest import requires_ffmpeg

pytestmark = requires_ffmpeg

REEL = "https://www.instagram.com/reel/C1a2B3c4D5e/"
STORY = "https://www.instagram.com/stories/someone/1234567890/"


def found(session: BrowserSession) -> DiscoveryResult:
    return DiscoveryResult(session, ((session.label, "logged in"),))


@pytest.fixture
def fake_download(monkeypatch, tiny_video):
    calls: list[str] = []

    def download(target, job_dir, settings, cookie_source, cancel, on_progress=None):
        calls.append(cookie_source.kind)
        if cookie_source.kind == "none":
            raise ExtractError("This content can only be fetched while logged in.", "login")
        path = job_dir / "src-C1a2B3c4D5e.mp4"
        shutil.copyfile(tiny_video, path)
        item = DownloadedItem(
            path, "C1a2B3c4D5e", "t", "T", "tester", 64, 64, 10.0, 1.0, "f", target.url
        )
        return ExtractResult(items=[item], title="t", uploader="T")

    monkeypatch.setattr(jobs_module, "download", download)
    return calls


def run_job(settings, url, *, local, discovery, monkeypatch, mode="original"):
    monkeypatch.setattr(jobs_module, "discover_browser_session", discovery)

    async def scenario():
        manager = JobManager(settings)
        await manager.start()
        try:
            job = manager.create(
                url,
                JobOptions(mode=mode),
                "127.0.0.1" if local else "203.0.113.9",
                client_is_local=local,
            )
            await asyncio.wait_for(job.task, timeout=60)
            return job
        finally:
            await manager.stop()

    return asyncio.run(scenario())


def test_local_client_falls_back_to_browser_login(settings, fake_download, monkeypatch):
    discovered = []

    def discovery(_settings):
        discovered.append(True)
        return found(BrowserSession("safari", {"sessionid": "abc", "ds_user_id": "1"}))

    job = run_job(settings, REEL, local=True, discovery=discovery, monkeypatch=monkeypatch)
    assert job.status.value == "done", job.error
    assert fake_download == ["none", "browser-auto"]  # anonymous first, then the browser login
    assert job.auth == "your Safari login"
    assert job.to_dict()["auth"] == "your Safari login"
    assert discovered == [True]


def test_story_uses_browser_login_immediately(settings, fake_download, monkeypatch):
    job = run_job(
        settings,
        STORY,
        local=True,
        monkeypatch=monkeypatch,
        discovery=lambda _s: found(BrowserSession("firefox", {"sessionid": "abc"})),
    )
    assert job.status.value == "done", job.error
    assert fake_download == ["browser-auto"]
    assert job.auth == "your Firefox login"


def test_remote_client_never_gets_operator_cookies(settings, fake_download, monkeypatch):
    def discovery(_settings):
        raise AssertionError("browser cookies must not be read for remote clients")

    job = run_job(settings, REEL, local=False, discovery=discovery, monkeypatch=monkeypatch)
    assert job.status.value == "error"
    assert fake_download == ["none"]
    assert "logged in" in job.error


def test_no_browser_session_gives_actionable_error(settings, fake_download, monkeypatch):
    missed = DiscoveryResult(
        None,
        (
            ("Safari", "no permission to read its cookies (grant Full Disk Access)"),
            ("Chrome", "not logged in to Instagram"),
        ),
    )
    job = run_job(settings, REEL, local=True, discovery=lambda _s: missed, monkeypatch=monkeypatch)
    assert job.status.value == "error"
    assert "no Instagram login was found in your browsers" in job.error
    assert "Chrome: not logged in to Instagram" in job.error
    assert "Safari: no permission" in job.error
    assert job.auth is None


def test_auto_browser_cookies_can_be_disabled(settings, fake_download, monkeypatch):
    off = replace(settings, auto_browser_cookies="off")

    def discovery(_settings):
        raise AssertionError("discovery disabled")

    job = run_job(off, REEL, local=True, discovery=discovery, monkeypatch=monkeypatch)
    assert job.status.value == "error"
    assert fake_download == ["none"]


def test_always_mode_applies_to_remote_clients(settings, fake_download, monkeypatch):
    always = replace(settings, auto_browser_cookies="always")
    job = run_job(
        always,
        REEL,
        local=False,
        monkeypatch=monkeypatch,
        discovery=lambda _s: found(BrowserSession("chrome", {"sessionid": "abc"})),
    )
    assert job.status.value == "done", job.error
    assert job.auth == "your Chrome login"


def test_anonymous_attempts_are_skipped_after_a_refusal(settings, fake_download, monkeypatch):
    monkeypatch.setattr(
        jobs_module,
        "discover_browser_session",
        lambda _s: found(BrowserSession("chrome", {"sessionid": "abc"})),
    )

    async def scenario():
        manager = JobManager(settings)
        await manager.start()
        try:
            first = manager.create(
                REEL, JobOptions(mode="original"), "127.0.0.1", client_is_local=True
            )
            await asyncio.wait_for(first.task, timeout=60)
            second = manager.create(
                REEL, JobOptions(mode="original"), "127.0.0.1", client_is_local=True
            )
            await asyncio.wait_for(second.task, timeout=60)
            return first, second
        finally:
            await manager.stop()

    first, second = asyncio.run(scenario())
    assert first.status.value == "done" and second.status.value == "done"
    # first job: anonymous refusal then browser login; second job: browser login straight away
    assert fake_download == ["none", "browser-auto", "browser-auto"]
    assert second.auth == "your Chrome login"
