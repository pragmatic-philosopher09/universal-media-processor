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
    assert caps["instagram_fallback"] is None
    assert caps["ffmpeg"]["encoder"] == "libx264"
    assert caps["ai"]["available"] is False
    assert caps["stories_auth_configured"] is False
    # TestClient connects from "testclient", which is not a loopback address.
    assert caps["browser_login"] == {
        "mode": "local",
        "active_for_you": False,
        "browsers": list(caps["browser_login"]["browsers"]),
    }
    assert "safari" in caps["browser_login"]["browsers"]
    assert caps["limits"]["max_duration_seconds"] == 600
    assert caps["yt_dlp_version"]


def test_fallback_provenance_and_source_limit(settings, fake_download, monkeypatch):
    from dataclasses import replace

    original_download = jobs_module.download

    def fallback(*args):
        result = original_download(*args)
        result.provider = "FastVideoSave"
        return result

    monkeypatch.setattr(jobs_module, "download", fallback)
    for duration_limit, expected in [(1800, "done"), (0.5, "error")]:
        configured = replace(
            settings, fastvideosave_enabled=True, max_source_duration_seconds=duration_limit
        )
        with TestClient(create_app(configured)) as client:
            assert client.get("/api/capabilities").json()["instagram_fallback"] == "fastvideosave"
            assert client.get("/api/capabilities").json()["instagram_public_media"] == {
                "photos": True,
                "stories": True,
            }
            created = client.post("/api/jobs", json={"url": REEL, "mode": "original"}).json()
            job = wait_for(client, created["id"])
            assert job["status"] == expected
            assert job["source_provider"] == "FastVideoSave"
            assert any("FastVideoSave" in warning for warning in job["warnings"])
            if expected == "done":
                assert job["outputs"][0]["has_audio"] is True
                assert client.get(job["outputs"][0]["url"]).status_code == 200
            else:
                assert "duration limit" in job["error"]


def test_frontend_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Media Downloader" in response.text
    assert 'name="mode" value="original" checked' in response.text
    assert 'name="mode" value="enhance" checked' not in response.text
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


@pytest.fixture
def tiny_image(tmp_path_factory, settings):
    import subprocess

    path = tmp_path_factory.mktemp("img") / "tiny.png"
    subprocess.run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=1",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
    )
    return path


def test_image_download_and_upscale(client, monkeypatch, tiny_image, settings):
    def download(target, job_dir: Path, settings_, cookie_source, cancel, on_progress=None):
        path = job_dir / "src-1240121428.png"
        shutil.copyfile(tiny_image, path)
        item = DownloadedItem(
            path,
            "1240121428",
            "Art",
            "artbySarf",
            "artbySarf",
            64,
            48,
            None,
            None,
            None,
            target.url,
            is_image=True,
        )
        return ExtractResult(items=[item], title="Art", uploader="artbySarf")

    monkeypatch.setattr(jobs_module, "download", download)
    response = client.post(
        "/api/jobs",
        json={
            "url": "https://www.deviantart.com/artbysarf/art/I-Love-Whom-I-Love-1240121428",
            "mode": "enhance",
            "resolution": "1440p",
            "fps": "60",
        },
    )
    assert response.status_code == 202, response.text
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "done", job
    assert job["platform"] == "deviantart" and job["platform_name"] == "DeviantArt"
    assert job["sources"][0]["is_image"] is True
    assert job["plan"]["interpolate"] is False and job["plan"]["label"] == "1440p"
    enhanced, original = job["outputs"]
    assert enhanced["kind"] == "enhanced" and enhanced["is_image"] is True
    assert enhanced["media_type"] == "image/png" and enhanced["download_name"].endswith(
        "_1440p.png"
    )
    assert (enhanced["width"], enhanced["height"]) == (1920, 1440)
    assert original["download_name"] == "artbySarf_1240121428.png"
    assert client.get(enhanced["url"]).headers["content-type"] == "image/png"


def test_youtube_job_is_accepted(client, fake_download):
    response = client.post(
        "/api/jobs", json={"url": "https://youtu.be/jNQXAC9IVRw", "mode": "original"}
    )
    assert response.status_code == 202
    job = wait_for(client, response.json()["id"])
    assert (
        job["platform"] == "youtube" and job["url"] == "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    )
    assert job["status"] == "done"


def test_enhancement_can_be_disabled(settings, fake_download):
    from dataclasses import replace

    app = create_app(replace(settings, enhancement_enabled=False))
    with TestClient(app) as client:
        caps = client.get("/api/capabilities").json()
        assert caps["enhancement"]["enabled"] is False and caps["enhancement"]["reason"]
        assert client.post("/api/jobs", json={"url": REEL, "mode": "enhance"}).status_code == 400
        response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
        assert response.status_code == 202
        assert wait_for(client, response.json()["id"])["status"] == "done"


def test_convert_original_without_fetching_again(client, fake_download):
    response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    original = wait_for(client, response.json()["id"])
    original_output = original["outputs"][0]
    source_bytes = client.get(original_output["url"]).content
    response = client.post(original_output["url"] + "/convert")
    assert response.status_code == 202, response.text
    converted = wait_for(client, response.json()["id"])
    assert converted["status"] == "done", converted
    (output,) = converted["outputs"]
    assert (output["width"], output["height"], output["fps"]) == (2160, 2160, 60)
    assert output["kind"] == "converted"
    assert len(fake_download) == 1
    assert client.get(original_output["url"]).content == source_bytes
    assert client.get(output["url"]).status_code == 200


def test_conversion_survives_deleting_original_job(client, fake_download):
    response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    original = wait_for(client, response.json()["id"])
    response = client.post(original["outputs"][0]["url"] + "/convert?preset=720p30")
    assert response.status_code == 202
    assert client.delete(f"/api/jobs/{original['id']}").status_code == 200
    converted = wait_for(client, response.json()["id"])
    assert converted["status"] == "done", converted
    assert client.get(converted["outputs"][0]["url"]).status_code == 200
    assert len(fake_download) == 1


def test_conversion_rejects_expired_and_invalid_sources(client):
    assert client.post("/api/jobs/missing/files/0/convert").status_code == 404
    response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    original = wait_for(client, response.json()["id"])
    url = original["outputs"][0]["url"]
    assert client.post(url + "/convert?preset=8k").status_code == 422
    assert client.post(f"/api/jobs/{original['id']}/files/-1/convert").status_code == 404
    assert client.post(f"/api/jobs/{original['id']}/files/1/convert").status_code == 404
    internal = client.app.state.manager.get(original["id"])
    internal.outputs[0].path.unlink()
    assert client.post(url + "/convert").status_code == 410
    assert len(client.app.state.manager.jobs) == 1


def test_conversion_rejects_images(client):
    from dataclasses import replace

    response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    original = wait_for(client, response.json()["id"])
    internal = client.app.state.manager.get(original["id"])
    internal.outputs[0].info = replace(internal.outputs[0].info, vcodec="png", nb_frames=1)
    response = client.post(original["outputs"][0]["url"] + "/convert")
    assert response.status_code == 400
    assert "not an image" in response.json()["detail"]


@pytest.mark.parametrize(
    "source_url",
    [
        "https://www.instagram.com/p/Photo123/",
        "https://www.instagram.com/stories/nasa/123/",
    ],
)
def test_instagram_image_upscale_reuses_file(settings, monkeypatch, tiny_image, source_url):
    from dataclasses import replace
    from unittest.mock import MagicMock

    from app import fastvideosave

    def save_media(urls, folder, settings, cancel, progress):
        path = folder / "src-fastvideosave-1.png"
        shutil.copyfile(tiny_image, path)
        return [path]

    fetch = MagicMock(return_value=["https://scontent.cdninstagram.com/photo.png"])
    monkeypatch.setattr(fastvideosave, "fetch_media_urls", fetch)
    monkeypatch.setattr(fastvideosave, "download_media", save_media)
    with TestClient(create_app(replace(settings, fastvideosave_enabled=True))) as client:
        response = client.post("/api/jobs", json={"url": source_url, "mode": "original"})
        original = wait_for(client, response.json()["id"])
        assert original["status"] == "done", original
        output = original["outputs"][0]
        assert output["is_image"] and output["media_type"] == "image/png"
        assert not any("no audio" in warning for warning in original["warnings"])
        old = client.get(output["url"]).content
        response = client.post(output["url"] + "/upscale?resolution=2160p")
        assert response.status_code == 202
        assert client.get(output["url"]).content == old
        client.delete("/api/jobs/" + original["id"])
        upscaled = wait_for(client, response.json()["id"])
        assert upscaled["status"] == "done", upscaled
        assert upscaled["image_resolution"] == "2160p"
        assert upscaled["options"]["mode"] == "upscale_image"
        result = upscaled["outputs"][0]
        assert (result["width"], result["height"]) == (2880, 2160)
        assert result["fps"] == 0 and result["is_image"]
        assert result["download_name"].endswith("_2160p.png")
        assert client.get(result["url"]).headers["content-type"] == "image/png"
        assert not (settings.jobs_dir / upscaled["id"] / "source.image").exists()
        fetch.assert_called_once()


def test_image_upscale_rejects_invalid_sources(client):
    assert client.post("/api/jobs/missing/files/0/upscale").status_code == 404
    response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    original = wait_for(client, response.json()["id"])
    url = original["outputs"][0]["url"]
    assert client.post(url + "/upscale").status_code == 400
    assert client.post(url + "/upscale?resolution=8k").status_code == 422
    assert client.post(f"/api/jobs/{original['id']}/files/-1/upscale").status_code == 404


def test_missing_audio_is_reported_not_invented(client, monkeypatch):
    from dataclasses import replace

    original_probe = jobs_module.ffprobe

    async def silent_probe(*args, **kwargs):
        info = await original_probe(*args, **kwargs)
        return replace(info, acodec=None, has_audio=False)

    monkeypatch.setattr(jobs_module, "ffprobe", silent_probe)
    response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    result = wait_for(client, response.json()["id"])
    assert result["status"] == "done"
    assert result["outputs"][0]["has_audio"] is False
    assert any("post may still have sound" in w for w in result["warnings"])


def test_image_upscale_limits_expiry_and_cancellation(client, settings, tiny_image, monkeypatch):
    import asyncio
    from dataclasses import replace

    from app.media import ffprobe

    response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    original = wait_for(client, response.json()["id"])
    manager = client.app.state.manager
    output = manager.get(original["id"]).outputs[0]
    output.path = output.path.with_suffix(".png")
    shutil.copyfile(tiny_image, output.path)
    output.info = asyncio.run(ffprobe(output.path, settings, local_image=True))
    url = original["outputs"][0]["url"] + "/upscale"
    manager.settings = replace(settings, enhancement_enabled=False)
    assert client.post(url).status_code == 400
    manager.settings = settings
    reserved = [
        manager.create_upload("pending.mp4", "720p30", "testclient")
        for _ in range(settings.max_jobs_per_ip)
    ]
    assert client.post(url).status_code == 429
    for job in reserved:
        manager.delete(job.id)

    async def wait_until_cancelled(*args, cancel, **kwargs):
        while not cancel.cancelled:
            await asyncio.sleep(0.01)
        cancel.check()

    monkeypatch.setattr(jobs_module, "enhance_image", wait_until_cancelled)
    response = client.post(url)
    assert response.status_code == 202
    conversion_id = response.json()["id"]
    client.delete("/api/jobs/" + conversion_id)
    assert wait_for(client, conversion_id)["status"] == "cancelled"
    assert not (settings.jobs_dir / conversion_id / "source.image").exists()
    assert not (settings.jobs_dir / conversion_id / "upscaled.png").exists()
    assert client.get(original["outputs"][0]["url"]).status_code == 200
    output.path.unlink()
    assert client.post(url).status_code == 410


def test_conversion_respects_disabled_server(settings, fake_download):
    from dataclasses import replace

    with TestClient(create_app(replace(settings, enhancement_enabled=False))) as client:
        response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
        original = wait_for(client, response.json()["id"])
        response = client.post(original["outputs"][0]["url"] + "/convert")
        assert response.status_code == 400
        assert "disabled" in response.json()["detail"]
        assert len(fake_download) == 1


def test_conversion_respects_job_limit(client):
    response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    original = wait_for(client, response.json()["id"])
    manager = client.app.state.manager
    for _ in range(manager.settings.max_jobs_per_ip):
        manager.create_upload("busy.mp4", "720p30", "testclient")
    assert client.post(original["outputs"][0]["url"] + "/convert").status_code == 429


def test_cancelled_conversion_keeps_original(client):
    response = client.post("/api/jobs", json={"url": REEL, "mode": "original"})
    original = wait_for(client, response.json()["id"])
    url = original["outputs"][0]["url"]
    response = client.post(url + "/convert")
    assert response.status_code == 202
    client.delete(f"/api/jobs/{response.json()['id']}")
    converted = wait_for(client, response.json()["id"])
    assert converted["status"] == "cancelled"
    assert client.get(url).status_code == 200
