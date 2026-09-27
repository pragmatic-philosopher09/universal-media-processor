"""End-to-end API tests with the Instagram download step replaced by a local file copy."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import jobs as jobs_module
from app.extractor import DownloadedItem, ExtractError, ExtractResult
from app.main import create_app

from .conftest import requires_ffmpeg

pytestmark = requires_ffmpeg

REEL = "https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=xyz"


@pytest.fixture
def fake_download(monkeypatch, tiny_video):
    calls: list[dict] = []

    def download(target, job_dir: Path, settings, cookie_source, cancel, on_progress=None):
        calls.append({"url": target.url, "kind": target.kind, "cookies": cookie_source.kind})
        if on_progress:
            on_progress(0.5, "Downloading best rendition")
        if "fail" in target.url:
            raise ExtractError("Instagram says this media is unavailable.")
        path = job_dir / "src-C1a2B3c4D5e.mp4"
        shutil.copyfile(tiny_video, path)
        item = DownloadedItem(
            path=path,
            media_id="C1a2B3c4D5e",
            title="Video by tester",
            uploader="Tester",
            channel="tester",
            width=64,
            height=64,
            fps=10.0,
            duration=1.0,
            format_id="dash-1",
            webpage_url=target.url,
        )
        return ExtractResult(items=[item], title=item.title, uploader="Tester")

    monkeypatch.setattr(jobs_module, "download", download)
    return calls


@pytest.fixture
def client(settings, fake_download):
    app = create_app(settings)
    with TestClient(app) as client:
        yield client


def wait_for(client: TestClient, job_id: str, timeout: float = 90) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"done", "error", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish in time")


def test_capabilities(client):
    caps = client.get("/api/capabilities").json()
    assert caps["ffmpeg"]["encoder"] == "libx264"
    assert caps["ai"]["available"] is False
    assert caps["stories_auth_configured"] is False
    assert caps["limits"]["max_duration_seconds"] == 600
    assert caps["yt_dlp_version"]


def test_frontend_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Reel Downloader" in response.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/style.css").status_code == 200


def test_original_download_job(client, fake_download):
    response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["kind"] == "reel" and job["url"] == "https://www.instagram.com/reel/C1a2B3c4D5e/"

    job = wait_for(client, job["id"])
    assert job["status"] == "done", job
    assert job["sources"][0]["width"] == 64 and job["sources"][0]["channel"] == "tester"
    assert [o["kind"] for o in job["outputs"]] == ["original"]
    output = job["outputs"][0]
    assert output["download_name"] == "tester_C1a2B3c4D5e.mp4"
    assert fake_download[0]["cookies"] == "none"

    file_response = client.get(output["url"])
    assert file_response.status_code == 200
    assert file_response.headers["content-type"] == "video/mp4"
    assert (
        'attachment; filename="tester_C1a2B3c4D5e.mp4"'
        in file_response.headers["content-disposition"]
    )
    assert len(file_response.content) == output["size"]
    inline = client.get(output["url"] + "?inline=1")
    assert inline.headers["content-disposition"].startswith("inline")


def test_enhance_job_produces_interpolated_output(client):
    response = client.post(
        "/api/jobs",
        json={
            "url": REEL,
            "mode": "enhance",
            "resolution": "original",
            "fps": "60",
            "engine": "ffmpeg",
        },
    )
    assert response.status_code == 202, response.text
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "done", job
    assert job["plan"]["interpolate"] is True and job["plan"]["upscale"] is False
    assert job["plan"]["fps_ratio"] == "6"
    assert job["engine"] == "ffmpeg"
    kinds = [o["kind"] for o in job["outputs"]]
    assert kinds == ["enhanced", "original"]
    enhanced = job["outputs"][0]
    assert enhanced["fps"] == 60.0 and enhanced["width"] == 64
    assert enhanced["download_name"].endswith("_64p60.mp4")
    assert enhanced["engine"] == "ffmpeg/libx264"


def test_enhance_noop_falls_back_to_original(client):
    response = client.post(
        "/api/jobs",
        json={"url": REEL, "mode": "enhance", "resolution": "original", "fps": "original"},
    )
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "done"
    assert [o["kind"] for o in job["outputs"]] == ["original"]
    assert job["warnings"] and "nothing to enhance" in job["warnings"][0]


def test_ai_engine_unavailable_is_reported(client):
    response = client.post("/api/jobs", json={"url": REEL, "mode": "enhance", "engine": "ai"})
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "error"
    assert "AI enhancement isn't available" in job["error"]


def test_download_failure_is_reported(client):
    response = client.post(
        "/api/jobs", json={"url": "https://www.instagram.com/reel/fail123/", "mode": "original"}
    )
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "error"
    assert job["error"] == "Instagram says this media is unavailable."


def test_invalid_inputs_are_rejected(client):
    assert (
        client.post("/api/jobs", json={"url": "https://youtube.com/watch?v=1"}).status_code == 400
    )
    assert (
        client.post("/api/jobs", json={"url": REEL, "cookies": "csrftoken=abc"}).status_code == 400
    )
    assert client.post("/api/jobs", json={"url": REEL, "resolution": "8k"}).status_code == 422
    assert client.get("/api/jobs/doesnotexist").status_code == 404
    assert client.get("/api/jobs/doesnotexist/files/0").status_code == 404


def test_request_cookies_reach_the_downloader(client, fake_download):
    response = client.post(
        "/api/jobs", json={"url": REEL, "mode": "original", "cookies": "sessionid=abc"}
    )
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "done"
    assert fake_download[-1]["cookies"] == "request"
    assert job["options"]["cookies_supplied"] is True


def test_delete_job_removes_files(client, settings):
    response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    job = wait_for(client, response.json()["id"])
    job_dir = settings.jobs_dir / job["id"]
    assert job_dir.exists()
    assert client.delete(f"/api/jobs/{job['id']}").json() == {"status": "deleted"}
    assert not job_dir.exists()
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404


def test_per_ip_job_limit(client):
    first = client.post("/api/jobs", json={"url": REEL, "mode": "enhance", "engine": "ffmpeg"})
    second = client.post("/api/jobs", json={"url": REEL, "mode": "enhance", "engine": "ffmpeg"})
    third = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    assert first.status_code == 202 and second.status_code == 202
    assert third.status_code == 429
    for response in (first, second):
        client.delete(f"/api/jobs/{response.json()['id']}")
        wait_for(client, response.json()["id"])
