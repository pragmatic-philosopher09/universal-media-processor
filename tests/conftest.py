from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import Settings

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not installed"
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        video_encoder="libx264",
        x264_preset="ultrafast",
        x264_crf=30,
        interp_quality="fast",
        ai_engine="off",
        max_concurrent_jobs=2,
        max_jobs_per_ip=2,
        job_ttl_minutes=1,
        ffmpeg_bin=FFMPEG or "ffmpeg",
        ffprobe_bin=FFPROBE or "ffprobe",
    )


@pytest.fixture(scope="session")
def tiny_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 64x64, 10 fps, 1 second H.264 clip with an AAC tone: 10 frames exactly."""
    if not FFMPEG:
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("media") / "tiny.mp4"
    cmd = [
        FFMPEG,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x64:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    subprocess.run(cmd, check=True)
    return path
