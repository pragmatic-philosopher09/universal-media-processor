from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from app.enhance_ai import choose_upscale_scale, chunk_length, plan_chunk, tail_chunk
from app.enhance_ffmpeg import (
    build_ffmpeg_command,
    build_video_filter,
    encoder_args,
    target_bitrate_kbps,
)
from app.media import VideoInfo
from app.plan import make_plan


def info(width=1080, height=1920, fps=30.0, frames=900, acodec="aac") -> VideoInfo:
    return VideoInfo(
        width, height, fps, frames / fps, frames, "h264", acodec, None, None, acodec is not None
    )


# ---------------------------------------------------------------------------- ffmpeg command


def test_filter_chain_interpolates_before_upscaling(settings):
    plan = make_plan(info(), "2160p", "60")
    chain = build_video_filter(plan, settings)
    parts = chain.split(",")
    assert parts[0] == "tpad=stop_mode=clone:stop_duration=0.5"
    assert parts[1].startswith("minterpolate=fps=60:")
    assert "mi_mode=mci" in parts[1] and "scd_threshold=8" in parts[1]
    assert parts[2] == "trim=duration=30.000000"
    assert parts[3].startswith("scale=2160:3840:flags=lanczos")
    assert parts[4] == "cas=strength=0.30"
    assert parts[-1] == "format=yuv420p"


def test_filter_chain_without_sharpen_or_interp(settings):
    quiet = replace(settings, sharpen=0.0)
    plan = make_plan(info(fps=60.0), "2160p", "60")
    assert build_video_filter(plan, quiet) == (
        "scale=2160:3840:flags=lanczos+accurate_rnd+full_chroma_int,format=yuv420p"
    )


def test_ntsc_rate_is_passed_as_fraction(settings):
    plan = make_plan(info(fps=30000 / 1001), "original", "60")
    assert ",minterpolate=fps=60000/1001:" in build_video_filter(plan, settings)


def test_build_command_libx264(settings):
    plan = make_plan(info(), "2160p", "60")
    cmd = build_ffmpeg_command(plan, Path("in.mp4"), Path("out.mp4"), "libx264", settings)
    assert cmd[0] == settings.ffmpeg_bin
    assert "-progress" in cmd and cmd[cmd.index("-progress") + 1] == "pipe:1"
    assert cmd[cmd.index("-i") + 1] == "in.mp4"
    assert cmd[-1] == "out.mp4"
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-crf") + 1] == "30"
    assert cmd[cmd.index("-preset") + 1] == "ultrafast"
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert "0:a:0?" in cmd
    assert "+faststart" in cmd


def test_non_aac_audio_is_transcoded(settings):
    plan = make_plan(info(acodec="opus"), "2160p", "60")
    cmd = build_ffmpeg_command(plan, Path("in.mp4"), Path("out.mp4"), "libx264", settings)
    assert cmd[cmd.index("-c:a") + 1] == "aac"


def test_silent_video_drops_audio(settings):
    plan = make_plan(info(acodec=None), "2160p", "60")
    cmd = build_ffmpeg_command(plan, Path("in.mp4"), Path("out.mp4"), "libx264", settings)
    assert "-an" in cmd


@pytest.mark.parametrize("encoder", ["h264_videotoolbox", "h264_nvenc", "hevc_videotoolbox"])
def test_hardware_encoders_use_bitrate_mode(encoder, settings):
    args = encoder_args(encoder, 2160, 3840, 60.0, settings)
    assert args[:2] == ["-c:v", encoder]
    kbps = int(args[args.index("-b:v") + 1].rstrip("k"))
    assert kbps == target_bitrate_kbps(2160, 3840, 60.0, settings.bits_per_pixel)
    assert "-maxrate" in args and "-bufsize" in args
    if encoder.startswith("hevc"):
        assert args[args.index("-tag:v") + 1] == "hvc1"


def test_target_bitrate_scales_with_pixels():
    assert target_bitrate_kbps(2160, 3840, 60.0, 0.08) == 39813
    assert target_bitrate_kbps(1080, 1920, 60.0, 0.08) == 9953
    assert target_bitrate_kbps(64, 64, 10.0, 0.08) == 4000  # floor
    assert target_bitrate_kbps(7680, 4320, 120.0, 0.08) == 80000  # ceiling


# ---------------------------------------------------------------------------- chunk maths


def simulate(total: int, chunk_frames: int, ratio: Fraction) -> tuple[int, list[int]]:
    """Mimic the producer loop: returns total kept frames and per-chunk input counts."""
    start, kept, inputs = 0, 0, []
    while start < total:
        chunk = plan_chunk(start, total - start, chunk_frames, ratio)
        available = total - start
        if available < chunk.n_in:
            chunk = tail_chunk(start, available, ratio)
        inputs.append(chunk.n_in)
        kept += chunk.n_keep
        start += chunk.advance
        if chunk.is_last:
            break
    return kept, inputs


@pytest.mark.parametrize(
    ("total", "chunk_frames", "ratio"),
    [
        (900, 32, Fraction(2)),
        (10, 4, Fraction(2)),
        (11, 4, Fraction(2)),
        (750, 32, Fraction(12, 5)),
        (10, 4, Fraction(12, 5)),
        (720, 32, Fraction(5, 2)),
        (1, 32, Fraction(2)),
        (33, 32, Fraction(2)),
        (900, 32, Fraction(1)),
        (7, 32, Fraction(1)),
    ],
)
def test_chunks_cover_every_output_frame(total, chunk_frames, ratio):
    kept, inputs = simulate(total, chunk_frames, ratio)
    assert kept == round(total * ratio)
    # Every non-final chunk carries exactly `q` overlap frames for the next one.
    q = ratio.denominator
    for n_in in inputs[:-1]:
        assert n_in == chunk_length(chunk_frames, ratio) + (q if ratio != 1 else 0)


def test_regular_chunk_shape():
    chunk = plan_chunk(0, 900, 32, Fraction(2))
    assert (chunk.n_in, chunk.n_out, chunk.n_keep, chunk.advance, chunk.is_last) == (
        33,
        66,
        64,
        32,
        False,
    )
    chunk = plan_chunk(0, 900, 32, Fraction(12, 5))
    assert chunk.n_in == 35 and chunk.n_out == 84 and chunk.n_keep == 72 and chunk.advance == 30
    assert chunk_length(4, Fraction(12, 5)) == 5


def test_tail_chunk_rounds_frame_count():
    chunk = tail_chunk(100, 7, Fraction(12, 5))
    assert chunk.n_out == 17 and chunk.n_keep == 17 and chunk.is_last


def test_choose_upscale_scale():
    plan = make_plan(info(), "2160p", "60")
    assert choose_upscale_scale("realesrgan-x4plus", plan, 0) == 4
    assert choose_upscale_scale("realesr-animevideov3", plan, 0) == 2
    assert (
        choose_upscale_scale("realesr-animevideov3", make_plan(info(720, 1280), "2160p", "60"), 0)
        == 3
    )
    assert choose_upscale_scale("realesr-animevideov3", plan, 3) == 3
