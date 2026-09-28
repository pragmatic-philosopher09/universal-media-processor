"""Integration tests that run the real ffmpeg pipelines on a tiny synthetic clip."""

from __future__ import annotations

import asyncio
import stat
import textwrap
from dataclasses import replace
from fractions import Fraction

import pytest

from app.enhance_ai import AIEngineError, _stages, enhance_with_ncnn, read_png, select_ai_engine
from app.enhance_ffmpeg import enhance_with_ffmpeg
from app.media import CancelToken, JobCancelled, ffprobe
from app.plan import EnhancePlan, make_plan

from .conftest import requires_ffmpeg

pytestmark = requires_ffmpeg


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def source(tiny_video, settings):
    return run(ffprobe(tiny_video, settings))


def test_ffprobe_tiny_clip(source):
    assert (source.width, source.height) == (64, 64)
    assert source.fps == 10.0
    assert source.nb_frames == 10
    assert source.has_audio and source.acodec == "aac"
    assert 0.9 <= source.duration <= 1.2


def test_ffmpeg_enhancement_upscales_and_interpolates(tiny_video, source, settings, tmp_path):
    plan = EnhancePlan(source, 128, 128, 20.0, Fraction(2))
    dst = tmp_path / "out.mp4"
    progress: list[float] = []
    run(
        enhance_with_ffmpeg(
            plan,
            tiny_video,
            dst,
            encoder="libx264",
            settings=settings,
            cancel=CancelToken(),
            on_progress=progress.append,
        )
    )
    out = run(ffprobe(dst, settings))
    assert (out.width, out.height) == (128, 128)
    assert out.fps == 20.0
    assert out.nb_frames == 20
    assert out.has_audio
    assert progress and progress[-1] >= 0.5


def test_ffmpeg_enhancement_can_be_cancelled(tiny_video, source, settings, tmp_path):
    plan = make_plan(source, "2160p", "60")  # 64x64 -> 2160x2160 @ 60 fps: slow enough to cancel
    slow = replace(settings, x264_preset="veryslow")
    dst = tmp_path / "out.mp4"
    cancel = CancelToken()

    async def scenario():
        task = asyncio.create_task(
            enhance_with_ffmpeg(
                plan, tiny_video, dst, encoder="libx264", settings=slow, cancel=cancel
            )
        )
        await asyncio.sleep(0.3)
        cancel.cancel()
        with pytest.raises(JobCancelled):
            await task

    run(scenario())


# ---------------------------------------------------------------------------- stub AI binaries

FAKE_RIFE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import math, os, shutil, sys, time
    time.sleep(float(os.environ.get("FAKE_AI_DELAY", "0")))
    args = sys.argv[1:]
    opt = {}
    i = 0
    while i < len(args):
        opt[args[i]] = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else ""
        i += 2 if opt[args[i]] != "" else 1
    files = sorted(f for f in os.listdir(opt["-i"]) if not f.startswith("."))
    count = len(files)
    numframe = int(opt.get("-n") or count * 2)
    pattern = opt.get("-f", "%08d.png")
    # Mirror rife-ncnn-vulkan: output i sits at source position i * count / numframe.
    scale = count / numframe
    for i in range(numframe):
        fx = i * scale
        sx = int(math.floor(fx))
        if sx >= count - 1:
            sx = count - 2
        sx = max(sx, 0)
        shutil.copyfile(os.path.join(opt["-i"], files[sx]), os.path.join(opt["-o"], pattern % (i + 1)))
    """
)

FAKE_REALESRGAN = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, subprocess, sys
    args = sys.argv[1:]
    opt = {}
    i = 0
    while i < len(args):
        opt[args[i]] = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else ""
        i += 2 if opt[args[i]] != "" else 1
    scale = int(opt.get("-s", "4"))
    fmt = opt.get("-f", "png")
    for name in sorted(os.listdir(opt["-i"])):
        if name.startswith("."):
            continue
        stem = os.path.splitext(name)[0]
        subprocess.run([
            os.environ.get("FFMPEG_BIN", "ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
            "-i", os.path.join(opt["-i"], name),
            "-vf", f"scale=iw*{scale}:ih*{scale}:flags=neighbor",
            os.path.join(opt["-o"], f"{stem}.{fmt}"),
        ], check=True)
    """
)


@pytest.fixture
def fake_ai_settings(settings, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rife = bin_dir / "rife-ncnn-vulkan"
    esrgan = bin_dir / "realesrgan-ncnn-vulkan"
    rife.write_text(FAKE_RIFE)
    esrgan.write_text(FAKE_REALESRGAN)
    for path in (rife, esrgan):
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FFMPEG_BIN", settings.ffmpeg_bin)
    return replace(
        settings,
        ai_engine="ncnn",
        rife_bin=str(rife),
        realesrgan_bin=str(esrgan),
        ai_upscale_model="realesr-animevideov3",
        ai_chunk_frames=4,
    )


def test_ncnn_pipeline_2x(tiny_video, source, fake_ai_settings, tmp_path):
    plan = EnhancePlan(source, 128, 128, 20.0, Fraction(2))
    assert select_ai_engine(plan, fake_ai_settings) == "ncnn"
    dst = tmp_path / "ai.mp4"
    progress: list[float] = []
    run(
        enhance_with_ncnn(
            plan,
            tiny_video,
            dst,
            workdir=tmp_path / "work",
            encoder="libx264",
            settings=fake_ai_settings,
            cancel=CancelToken(),
            on_progress=progress.append,
        )
    )
    out = run(ffprobe(dst, fake_ai_settings))
    assert (out.width, out.height) == (128, 128)
    assert out.fps == 20.0
    assert out.nb_frames == 20
    assert out.has_audio
    assert progress[-1] == 1.0
    assert not (tmp_path / "work" / "chunks").exists()


def test_ncnn_pipeline_fractional_ratio(tiny_video, source, fake_ai_settings, tmp_path):
    """10 fps -> 24 fps is a 12/5 ratio: chunks of 5 with 5 overlap frames, `-n` passed to RIFE."""
    plan = EnhancePlan(source, 128, 128, 24.0, Fraction(12, 5))
    dst = tmp_path / "ai24.mp4"
    run(
        enhance_with_ncnn(
            plan,
            tiny_video,
            dst,
            workdir=tmp_path / "work",
            encoder="libx264",
            settings=fake_ai_settings,
            cancel=CancelToken(),
        )
    )
    out = run(ffprobe(dst, fake_ai_settings))
    assert (out.width, out.height) == (128, 128)
    assert out.fps == 24.0
    assert out.nb_frames == 24


def test_ncnn_upscale_only_keeps_frame_count(tiny_video, source, fake_ai_settings, tmp_path):
    plan = EnhancePlan(source, 192, 192, 10.0, Fraction(1))
    dst = tmp_path / "up.mp4"
    run(
        enhance_with_ncnn(
            plan,
            tiny_video,
            dst,
            workdir=tmp_path / "work",
            encoder="libx264",
            settings=fake_ai_settings,
            cancel=CancelToken(),
        )
    )
    out = run(ffprobe(dst, fake_ai_settings))
    assert (out.width, out.height) == (192, 192)
    assert out.nb_frames == 10 and out.fps == 10.0


def test_ncnn_requires_v4_model_for_fractional_ratio(source, fake_ai_settings):
    plan = EnhancePlan(source, 128, 128, 24.0, Fraction(12, 5))
    old_model = replace(fake_ai_settings, ai_rife_model="rife-v2.3")
    with pytest.raises(AIEngineError):
        _stages(plan, old_model)
    assert _stages(plan, fake_ai_settings).scale == 2


def test_ncnn_pipeline_can_be_cancelled(
    tiny_video, source, fake_ai_settings, tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_AI_DELAY", "5")
    plan = EnhancePlan(source, 128, 128, 20.0, Fraction(2))
    cancel = CancelToken()

    async def scenario():
        task = asyncio.create_task(
            enhance_with_ncnn(
                plan,
                tiny_video,
                tmp_path / "c.mp4",
                workdir=tmp_path / "work",
                encoder="libx264",
                settings=fake_ai_settings,
                cancel=cancel,
            )
        )
        await asyncio.sleep(0.2)
        cancel.cancel()
        with pytest.raises(JobCancelled):
            await task

    run(scenario())


def test_read_png_splits_concatenated_stream(tiny_video, settings):
    async def scenario():
        proc = await asyncio.create_subprocess_exec(
            settings.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(tiny_video),
            "-frames:v",
            "3",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        frames = []
        while True:
            frame = await read_png(proc.stdout)
            if frame is None:
                break
            frames.append(frame)
        await proc.wait()
        return frames

    frames = run(scenario())
    assert len(frames) == 3
    for frame in frames:
        assert frame.startswith(b"\x89PNG") and frame.endswith(b"IEND\xaeB`\x82")
