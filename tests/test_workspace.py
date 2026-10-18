"""MediaFlow workspace: uploads, multi-input jobs, standardize, review, manifest, archive, metrics."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import shutil
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import jobs as jobs_module
from app.extractor import DownloadedItem, ExtractError, ExtractResult
from app.main import create_app
from app.uploads import UploadNotFound, UploadStore

from .conftest import requires_ffmpeg

pytestmark = requires_ffmpeg

REEL = "https://www.instagram.com/reel/C1a2B3c4D5e/"
REEL2 = "https://www.instagram.com/reel/Z9y8X7w6V5u/"
YOUTUBE = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
FAIL = "https://www.instagram.com/reel/fail123/"


@pytest.fixture
def fake_download(monkeypatch, tiny_video):
    calls: list[dict] = []

    def download(target, job_dir: Path, settings, cookie_source, cancel, on_progress=None):
        calls.append({"url": target.url, "cookies": cookie_source.kind, "dir": job_dir})
        if "fail" in target.url:
            raise ExtractError("This media is unavailable.")
        path = job_dir / "src-C1a2B3c4D5e.mp4"
        shutil.copyfile(tiny_video, path)
        item = DownloadedItem(
            path, "C1a2B3c4D5e", "t", "Tester", "tester", 64, 64, 10.0, 1.0, "f", target.url
        )
        return ExtractResult(items=[item], title="t", uploader="Tester")

    monkeypatch.setattr(jobs_module, "download", download)
    return calls


@pytest.fixture
def client(settings, fake_download):
    with TestClient(create_app(settings)) as client:
        yield client


def wait_for(client: TestClient, job_id: str, statuses=("done", "error", "cancelled")) -> dict:
    deadline = time.time() + 90
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not reach the expected status in time")


def upload(client: TestClient, path: Path, name: str | None = None) -> dict:
    response = client.post(
        "/api/uploads", params={"filename": name or path.name}, content=path.read_bytes()
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------- capabilities


def test_capabilities_describe_the_workspace(client):
    caps = client.get("/api/capabilities").json()
    assert caps["product"] == {"name": "MediaFlow", "version": "2.0.0"}
    assert [o["id"] for o in caps["outcomes"]] == ["original", "enhance", "standardize"]
    assert all(o["available"] and o["tradeoff"] for o in caps["outcomes"])
    assert "1920x1080" in caps["standardize"]["canvas"] and caps["standardize"]["fit"]
    assert caps["uploads"]["enabled"] is True and "mp4" in caps["uploads"]["extensions"]
    assert caps["limits"]["max_items_per_job"] == 10
    assert caps["metrics_enabled"] is True


# ---------------------------------------------------------------- uploads


def test_upload_then_keep_original(client, tiny_video):
    info = upload(client, tiny_video, "My Holiday.mp4")
    assert info["filename"] == "My Holiday.mp4" and info["info"]["width"] == 64

    response = client.post("/api/jobs", json={"uploads": [info["id"]], "mode": "original"})
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["url"] is None and job["platform"] == "upload"
    assert job["platform_name"] == "Your files"
    job = wait_for(client, job["id"])
    assert job["status"] == "done", job
    (output,) = job["outputs"]
    assert output["kind"] == "original" and output["treatments"] == ["uploaded"]
    assert output["download_name"] == "My Holiday.mp4"
    assert client.get(output["url"]).content == tiny_video.read_bytes()
    # The job took ownership: the upload can't be reused or deleted any more.
    assert client.delete(f"/api/uploads/{info['id']}").status_code == 404
    again = client.post("/api/jobs", json={"uploads": [info["id"]], "mode": "original"})
    assert again.status_code == 400 and "expired" in again.json()["detail"]


def test_upload_validation(client, tiny_video, tmp_path):
    bad_type = client.post("/api/uploads", params={"filename": "notes.txt"}, content=b"hello")
    assert bad_type.status_code == 400 and "Unsupported file type" in bad_type.json()["detail"]
    garbage = client.post("/api/uploads", params={"filename": "clip.mp4"}, content=b"x" * 2048)
    assert garbage.status_code == 400 and "isn't a readable" in garbage.json()["detail"]
    empty = client.post("/api/uploads", params={"filename": "clip.mp4"}, content=b"")
    assert empty.status_code == 400
    info = upload(client, tiny_video)
    assert client.delete(f"/api/uploads/{info['id']}").json() == {"status": "deleted"}
    assert client.delete(f"/api/uploads/{info['id']}").status_code == 404
    # Nothing is left behind on disk for rejected or deleted uploads.
    uploads_dir = client.app.state.manager.settings.uploads_dir
    assert list(uploads_dir.iterdir()) == []


def test_upload_size_limit_and_switch(settings, fake_download, tiny_video):
    limited = replace(settings, max_upload_mb=1)
    with TestClient(create_app(limited)) as client:
        too_big = client.post(
            "/api/uploads", params={"filename": "big.mp4"}, content=b"\0" * (1024 * 1024 + 1)
        )
        assert too_big.status_code == 413
    disabled = replace(settings, uploads_enabled=False)
    with TestClient(create_app(disabled)) as client:
        assert client.get("/api/capabilities").json()["uploads"]["enabled"] is False
        response = client.post(
            "/api/uploads", params={"filename": "a.mp4"}, content=tiny_video.read_bytes()
        )
        assert response.status_code == 403


def test_uploads_belong_to_the_client_that_sent_them(settings, tiny_video):
    store = UploadStore(settings)
    store.start()

    async def chunks():
        yield tiny_video.read_bytes()

    saved = asyncio.run(store.save(chunks(), "a.mp4", "198.51.100.1"))
    assert store.get(saved.id, "198.51.100.1") is saved
    with pytest.raises(UploadNotFound):
        store.get(saved.id, "203.0.113.7")
    assert store.sweep(now=time.time() + 3600) == 1
    assert not saved.path.exists()


# ---------------------------------------------------------------- multi-input + standardize


def test_batch_standardize_links_and_upload(client, fake_download, tiny_video):
    info = upload(client, tiny_video, "phone clip.mp4")
    response = client.post(
        "/api/jobs",
        json={
            "urls": [REEL, REEL2, REEL + "?igsh=dup"],
            "uploads": [info["id"]],
            "mode": "standardize",
            "standardize": {"canvas": "1280x720", "fps": "30", "fit": "pad"},
        },
    )
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["item_count"] == 3  # the duplicate link was dropped
    assert [i["kind"] for i in created["inputs"]] == ["link", "link", "upload"]
    job = wait_for(client, created["id"])
    assert job["status"] == "done", job
    assert all(i["status"] == "ready" for i in job["inputs"])
    # Each link downloads into its own folder, so identical media ids don't collide.
    assert len({call["dir"] for call in fake_download}) == 2

    kinds = [(o["input_index"], o["kind"]) for o in job["outputs"]]
    assert kinds == [
        (0, "standardized"),
        (0, "original"),
        (1, "standardized"),
        (1, "original"),
        (2, "standardized"),
    ]
    std = job["outputs"][0]
    assert (std["width"], std["height"], std["fps"]) == (1280, 720, 30.0)
    assert std["treatments"] == ["upscaled", "letterboxed", "fps_converted", "transcoded"]
    assert std["download_name"] == "tester_C1a2B3c4D5e_std-1280x720-30fps.mp4"
    assert job["outputs"][4]["download_name"] == "phone_clip_std-1280x720-30fps.mp4"
    assert job["outputs"][1]["treatments"] == ["native"]
    assert any("repeats frames" in w for w in job["warnings"])
    assert job["options"]["standardize"] == {"canvas": "1280x720", "fps": "30", "fit": "pad"}


def test_one_failed_link_does_not_sink_the_batch(client):
    response = client.post("/api/jobs", json={"urls": [REEL, FAIL], "mode": "original"})
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "done", job
    assert [i["status"] for i in job["inputs"]] == ["ready", "failed"]
    assert job["failures"] == [
        {"input_index": 1, "label": FAIL, "error": "This media is unavailable."}
    ]
    assert len(job["outputs"]) == 1
    assert any("unavailable" in w for w in job["warnings"])


def test_batch_where_everything_fails_is_an_error(client):
    response = client.post(
        "/api/jobs", json={"urls": [FAIL, FAIL.replace("123", "456")], "mode": "original"}
    )
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "error" and len(job["failures"]) == 2


def test_pasted_login_only_applies_to_its_own_platform(client, fake_download):
    response = client.post(
        "/api/jobs", json={"urls": [REEL, YOUTUBE], "mode": "original", "cookies": "sessionid=abc"}
    )
    assert response.status_code == 202, response.text
    assert response.json()["platform"] == "mixed"
    job = wait_for(client, response.json()["id"])
    assert job["status"] == "done"
    assert [c["cookies"] for c in fake_download] == ["request", "none"]
    # Cookies that fit none of the links are still rejected up front.
    bad = client.post(
        "/api/jobs", json={"urls": [REEL, REEL2], "mode": "original", "cookies": "csrftoken=abc"}
    )
    assert bad.status_code == 400


def test_job_input_validation(client):
    assert client.post("/api/jobs", json={"mode": "original"}).status_code == 400
    too_many = [f"https://www.instagram.com/reel/A{i:010d}/" for i in range(11)]
    response = client.post("/api/jobs", json={"urls": too_many, "mode": "original"})
    assert response.status_code == 400 and "at most 10" in response.json()["detail"]
    bad_canvas = {"url": REEL, "mode": "standardize", "standardize": {"canvas": "8k"}}
    assert client.post("/api/jobs", json=bad_canvas).status_code == 422


def test_enhance_outputs_carry_treatment_labels(client):
    response = client.post(
        "/api/jobs",
        json={"url": REEL, "mode": "enhance", "resolution": "original", "engine": "ffmpeg"},
    )
    job = wait_for(client, response.json()["id"])
    assert [o["treatments"] for o in job["outputs"]] == [
        ["interpolated", "transcoded"],
        ["native"],
    ]
    assert job["inputs"][0]["auth"] == job["auth"]


# ---------------------------------------------------------------- review


def test_review_then_change_outcome_and_process(client):
    response = client.post("/api/jobs", json={"url": REEL, "mode": "enhance", "review": True})
    assert response.status_code == 202
    job_id = response.json()["id"]
    job = wait_for(client, job_id, ("review", "error"))
    assert job["status"] == "review", job
    assert job["outputs"] == []
    (proposal,) = job["proposals"]
    assert proposal["action"] == "process"
    assert proposal["target"]["height"] == 2160 and proposal["target"]["fps"] == 60.0
    assert proposal["treatments"][:2] == ["upscaled", "sharpened"]
    assert any("can't recover detail" in w for w in proposal["warnings"])

    preview = client.post(
        f"/api/jobs/{job_id}/plan",
        json={"mode": "standardize", "standardize": {"canvas": "1080x1920", "fps": "30"}},
    )
    assert preview.status_code == 200
    (alt,) = preview.json()["proposals"]
    assert alt["target"] == {"width": 1080, "height": 1920, "fps": 30.0}
    assert "letterboxed" in alt["treatments"]
    assert client.get(f"/api/jobs/{job_id}").json()["options"]["mode"] == "enhance"

    started = client.post(f"/api/jobs/{job_id}/start", json={"mode": "original"})
    assert started.status_code == 202, started.text
    assert started.json()["options"]["mode"] == "original"
    job = wait_for(client, job_id)
    assert job["status"] == "done"
    assert [o["kind"] for o in job["outputs"]] == ["original"]
    assert client.post(f"/api/jobs/{job_id}/start").status_code == 409
    assert client.post(f"/api/jobs/{job_id}/plan", json={}).status_code == 409


def test_start_with_empty_body_keeps_reviewed_plan(client):
    job_id = client.post(
        "/api/jobs", json={"url": REEL, "mode": "original", "review": True}
    ).json()["id"]
    assert wait_for(client, job_id, ("review", "error"))["status"] == "review"
    started = client.post(f"/api/jobs/{job_id}/start", json={})
    assert started.status_code == 202, started.text
    assert started.json()["options"]["mode"] == "original"
    job = wait_for(client, job_id)
    assert [o["kind"] for o in job["outputs"]] == ["original"]


def test_review_can_be_cancelled(client):
    job_id = client.post(
        "/api/jobs", json={"url": REEL, "mode": "original", "review": True}
    ).json()["id"]
    wait_for(client, job_id, ("review",))
    assert client.delete(f"/api/jobs/{job_id}").json() == {"status": "cancelling"}
    assert wait_for(client, job_id)["status"] == "cancelled"


def test_review_jobs_count_towards_the_active_limit(client):
    for _ in range(2):
        job_id = client.post(
            "/api/jobs", json={"url": REEL, "mode": "original", "review": True}
        ).json()["id"]
        wait_for(client, job_id, ("review",))
    assert client.post("/api/jobs", json={"url": REEL, "mode": "original"}).status_code == 429


def test_abandoned_reviews_are_swept(client):
    manager = client.app.state.manager
    job_id = client.post(
        "/api/jobs", json={"url": REEL, "mode": "original", "review": True}
    ).json()["id"]
    wait_for(client, job_id, ("review",))
    manager.sweep(now=time.time() + 3600)
    assert wait_for(client, job_id)["status"] == "cancelled"


def test_enhance_review_respects_server_switch(settings, fake_download):
    weak = replace(settings, enhancement_enabled=False)
    with TestClient(create_app(weak)) as client:
        caps = client.get("/api/capabilities").json()
        assert [o["available"] for o in caps["outcomes"]] == [True, False, True]
        job_id = client.post(
            "/api/jobs", json={"url": REEL, "mode": "standardize", "review": True}
        ).json()["id"]
        wait_for(client, job_id, ("review",))
        assert client.post(f"/api/jobs/{job_id}/plan", json={"mode": "enhance"}).status_code == 400
        assert client.post(f"/api/jobs/{job_id}/start", json={"mode": "enhance"}).status_code == 400


# ---------------------------------------------------------------- manifest, archive, metrics


def test_manifest_archive_and_north_star_metric(client, tiny_video):
    info = upload(client, tiny_video, "clip.mp4")
    job_id = client.post(
        "/api/jobs",
        json={
            "url": REEL,
            "uploads": [info["id"]],
            "mode": "standardize",
            "standardize": {"canvas": "source", "fps": "source"},
        },
    ).json()["id"]
    job = wait_for(client, job_id)
    assert job["status"] == "done", job
    assert client.get("/api/metrics").json()["north_star"]["total"] == 0

    manifest = client.get(f"/api/jobs/{job_id}/manifest").json()
    assert manifest["manifest_version"] == 1 and manifest["job"]["id"] == job_id
    assert [i["kind"] for i in manifest["inputs"]] == ["link", "upload"]
    assert len(manifest["outputs"]) == 3
    assert [o["primary"] for o in manifest["outputs"]] == [True, False, True]
    for output in manifest["outputs"]:
        body = client.get(output["url"] + "?inline=1").content
        assert output["sha256"] == hashlib.sha256(body).hexdigest()
    assert manifest["outputs"][0]["treatments"] == ["constant_fps", "transcoded"]
    # Inline previews are not "retrieval" for the north-star metric.
    assert client.get("/api/metrics").json()["north_star"]["total"] == 0

    csv_response = client.get(f"/api/jobs/{job_id}/manifest", params={"format": "csv"})
    assert csv_response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert len(rows) == 3 and rows[0]["treatments"] == "constant_fps;transcoded"
    assert rows[2]["input"] == "clip.mp4"

    archive = client.get(f"/api/jobs/{job_id}/archive")
    assert archive.status_code == 200
    assert archive.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zf:
        names = sorted(zf.namelist())
        assert names == sorted(
            [
                manifest["outputs"][0]["download_name"],
                manifest["outputs"][2]["download_name"],
                "manifest.json",
                "manifest.csv",
            ]
        )
        assert json.loads(zf.read("manifest.json"))["job"]["id"] == job_id

    metrics = client.get("/api/metrics").json()
    assert metrics["north_star"]["total"] == 1 and metrics["north_star"]["last_7_days"] == 1
    assert metrics["time_to_first_output_seconds"]["samples"] == 1
    # Repeat downloads of the same job count once.
    client.get(manifest["outputs"][0]["url"])
    metrics = client.get("/api/metrics").json()
    assert metrics["north_star"]["total"] == 1
    assert metrics["jobs"]["by_mode"] == {"standardize": 1}
    assert metrics["jobs"]["inputs_by_kind"] == {"link": 1, "upload": 1}
    assert metrics["platforms"] == {"instagram": {"ok": 1}, "upload": {"ok": 1}}
    assert metrics["outcomes"] == {"standardize": {"done": 1}}
    assert REEL not in json.dumps(metrics)


def test_archive_needs_a_finished_job(client):
    job_id = client.post(
        "/api/jobs", json={"url": REEL, "mode": "original", "review": True}
    ).json()["id"]
    wait_for(client, job_id, ("review",))
    assert client.get(f"/api/jobs/{job_id}/archive").status_code == 409
    assert client.get("/api/jobs/nope/archive").status_code == 404
    assert client.get("/api/jobs/nope/manifest").status_code == 404


def test_metrics_can_be_switched_off(settings, fake_download):
    with TestClient(create_app(replace(settings, metrics_enabled=False))) as client:
        assert client.get("/api/metrics").status_code == 404
        assert client.get("/api/capabilities").json()["metrics_enabled"] is False
