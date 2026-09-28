"""Upload API and actual ffmpeg conversion, without platform extraction or AI."""

from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from app.jobs import JobManager, JobOptions, JobStatus, TooManyJobs
from app.main import create_app
from app.plan import CONVERSION_PRESETS, make_conversion_plan

from .conftest import requires_ffmpeg
from .test_api import wait_for
from .test_plan import info


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def landscape_video(tiny_video, settings, tmp_path):
    path = tmp_path / "landscape.mkv"
    subprocess.run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(tiny_video),
            "-vf",
            "scale=96:54,setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
    )
    return path


@pytest.mark.parametrize("preset", CONVERSION_PRESETS)
@requires_ffmpeg
def test_convert_every_preset(client, landscape_video, settings, preset):
    response = client.post(
        "/api/uploads",
        params={"filename": "../My clip.mkv", "preset": preset},
        content=landscape_video.read_bytes(),
    )
    assert response.status_code == 202, response.text
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "done", job
    assert job["kind"] == "upload" and job["platform"] == "upload"
    assert job["auth"] is None and job["engine"] == "ffmpeg"
    assert job["options"]["engine"] == "ffmpeg"
    (output,) = job["outputs"]
    _, height, width, fps = CONVERSION_PRESETS[preset]
    assert (output["width"], output["height"], output["fps"]) == (width, height, fps)
    assert output["kind"] == "converted" and output["media_type"] == "video/mp4"
    assert output["download_name"] == f"My_clip_{preset}.mp4"
    assert 0.9 <= output["duration"] <= 1.2
    internal = client.app.state.manager.get(job["id"])
    assert internal.outputs[0].info.has_audio
    assert internal.outputs[0].info.acodec == "aac"
    assert not (internal.dir / "source.video").exists()
    download = client.get(output["url"])
    assert download.status_code == 200 and len(download.content) == output["size"]
    assert download.headers["content-disposition"].startswith("attachment")
    assert (
        client.get(output["url"] + "?inline=1").headers["content-disposition"].startswith("inline")
    )
    assert client.get(f"/api/jobs/{job['id']}/files/1").status_code == 404
    client.delete(f"/api/jobs/{job['id']}")
    assert not (settings.jobs_dir / job["id"]).exists()


@pytest.mark.parametrize(
    ("width", "height", "fps", "preset", "expected"),
    [
        (1080, 1920, 30, "2160p60", (2160, 3840, 60)),
        (7680, 4320, 120, "2160p60", (3840, 2160, 60)),
        (3840, 2160, 60, "720p30", (1280, 720, 30)),
        (1920, 1080, 30000 / 1001, "1080p60", (1920, 1080, 60)),
        (1920, 800, 24, "2160p60", (3840, 1600, 60)),
        (640, 640, 30, "720p30", (720, 720, 30)),
    ],
)
def test_conversion_plan(width, height, fps, preset, expected):
    plan = make_conversion_plan(info(width, height, fps), preset)
    assert (plan.target_width, plan.target_height, plan.target_fps) == expected


def test_upload_request_validation(client, settings):
    assert client.post("/api/uploads?filename=a.mp4&preset=8k", content=b"x").status_code == 422
    assert client.post("/api/uploads", content=b"x").status_code == 422
    assert client.post("/api/uploads?filename=a.mp4", content=b"").status_code == 400
    response = client.post(
        "/api/uploads?filename=a.mp4",
        content=b"x",
        headers={"Content-Length": str(settings.max_upload_bytes + 1)},
    )
    assert response.status_code == 413
    assert not client.app.state.manager.jobs
    assert not list(settings.jobs_dir.iterdir())


def test_chunked_upload_limit(settings):
    app = create_app(replace(settings, max_upload_bytes=4))
    with TestClient(app) as client:
        response = client.post("/api/uploads?filename=a.mp4", content=iter([b"123", b"456"]))
        assert response.status_code == 413
        assert not app.state.manager.jobs
        assert not list(settings.jobs_dir.iterdir())


def test_disconnect_removes_partial_upload(client, settings, monkeypatch):
    from starlette.requests import Request

    async def interrupted(_):
        yield b"partial"
        raise ClientDisconnect()

    monkeypatch.setattr(Request, "stream", interrupted)
    with pytest.raises(ClientDisconnect):
        client.post("/api/uploads?filename=a.mp4", content=b"video")
    assert not client.app.state.manager.jobs
    assert not list(settings.jobs_dir.iterdir())


@requires_ffmpeg
@pytest.mark.parametrize(
    "content",
    [
        b"not a video",
        b"#EXTM3U\n#EXTINF:1\nhttp://127.0.0.1/private.ts\n",
    ],
)
def test_invalid_video_reports_error(client, content):
    response = client.post("/api/uploads?filename=video.mp4", content=content)
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "error" and job["error"]
    assert not job["outputs"]
    assert not list(client.app.state.manager.get(job["id"]).dir.iterdir())


@requires_ffmpeg
def test_duration_limit_is_error_not_original_fallback(settings, tiny_video):
    with TestClient(create_app(replace(settings, max_duration_seconds=0.5))) as client:
        response = client.post("/api/uploads?filename=video.mp4", content=tiny_video.read_bytes())
        job = wait_for(client, response.json()["id"])
        assert job["status"] == "error" and "conversion limit" in job["error"]
        assert not job["outputs"]


@requires_ffmpeg
def test_conversion_capabilities_and_disabled_server(settings):
    with TestClient(create_app(replace(settings, enhancement_enabled=False))) as client:
        caps = client.get("/api/capabilities").json()
        assert caps["conversion"]["enabled"] is False
        assert {p["id"] for p in caps["conversion"]["presets"]} == set(CONVERSION_PRESETS)
        assert caps["limits"]["max_upload_bytes"] == 500 * 1024 * 1024
        assert client.post("/api/uploads?filename=a.mp4", content=b"x").status_code == 400


def test_uploads_share_per_ip_limit_and_queued_cancellation(settings):
    async def scenario():
        manager = JobManager(replace(settings, max_jobs_per_ip=1))
        job = manager.create_upload("video.mp4", "720p30", "client")
        with pytest.raises(TooManyJobs):
            manager.create_upload("video.mp4", "720p30", "client")
        with pytest.raises(TooManyJobs):
            manager.create("https://youtu.be/jNQXAC9IVRw", JobOptions(), "client")
        manager.enqueue(job)
        (job.dir / "source.video").write_bytes(b"queued upload")
        manager.cancel(job.id)
        await asyncio.gather(job.task, return_exceptions=True)
        assert job.status == JobStatus.CANCELLED
        assert not (job.dir / "source.video").exists()
        manager.sweep(time.time() + settings.job_ttl_minutes * 60 + 1)
        assert not job.dir.exists()

    asyncio.run(scenario())


@requires_ffmpeg
def test_running_conversion_can_be_cancelled(settings, landscape_video):
    with TestClient(create_app(replace(settings, x264_preset="veryslow"))) as client:
        response = client.post(
            "/api/uploads?filename=video.mkv", content=landscape_video.read_bytes()
        )
        job_id = response.json()["id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            if client.get(f"/api/jobs/{job_id}").json()["stage"].startswith("Converting"):
                break
            time.sleep(0.02)
        client.delete(f"/api/jobs/{job_id}")
        job = wait_for(client, job_id)
        assert job["status"] == "cancelled", job
        assert not list(client.app.state.manager.get(job_id).dir.iterdir())


@requires_ffmpeg
def test_rotated_phone_video(client, landscape_video, settings, tmp_path):
    path = tmp_path / "rotated.mp4"
    help_text = subprocess.run(
        [settings.ffmpeg_bin, "-hide_banner", "-h", "full"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rotation_input = ["-display_rotation", "90"] if "-display_rotation" in help_text else []
    subprocess.run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-loglevel",
            "error",
            *rotation_input,
            "-i",
            str(landscape_video),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-metadata:s:v:0",
            "rotate=90",
            str(path),
        ],
        check=True,
    )
    response = client.post(
        "/api/uploads?filename=phone.mov&preset=720p30", content=path.read_bytes()
    )
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "done", job
    (output,) = job["outputs"]
    assert (output["width"], output["height"], output["fps"]) == (720, 1280, 30)


@requires_ffmpeg
@pytest.mark.parametrize("fps", [30, 60])
def test_same_size_conversion_and_frame_rate_reduction(client, tiny_video, settings, tmp_path, fps):
    path = tmp_path / "hd.mkv"
    subprocess.run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(tiny_video),
            "-vf",
            f"scale=1280:720,setsar=1,fps={fps}",
            "-an",
            "-t",
            "0.3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(path),
        ],
        check=True,
    )
    response = client.post("/api/uploads?filename=hd.mkv&preset=720p30", content=path.read_bytes())
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "done", job
    (output,) = job["outputs"]
    assert (output["width"], output["height"], output["fps"]) == (1280, 720, 30)
    assert output["kind"] == "converted"
    assert not client.app.state.manager.get(job["id"]).outputs[0].info.has_audio


@requires_ffmpeg
@pytest.mark.parametrize("extension", ["png", "wav"])
def test_image_and_audio_only_rejected(client, tiny_video, settings, tmp_path, extension):
    path = tmp_path / f"not-video.{extension}"
    options = ["-frames:v", "1"] if extension == "png" else ["-vn"]
    subprocess.run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(tiny_video),
            *options,
            str(path),
        ],
        check=True,
    )
    response = client.post("/api/uploads?filename=video.mp4", content=path.read_bytes())
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "error" and job["error"]
    assert not job["outputs"]
