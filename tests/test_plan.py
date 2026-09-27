from __future__ import annotations

from fractions import Fraction

import pytest

from app.media import VideoInfo, fps_to_ffmpeg_rate
from app.plan import PlanError, choose_fps_ratio, compute_target_size, make_plan


def info(width: int, height: int, fps: float, frames: int | None = 900) -> VideoInfo:
    return VideoInfo(
        width=width,
        height=height,
        fps=fps,
        duration=frames / fps if frames else 30.0,
        nb_frames=frames,
        vcodec="h264",
        acodec="aac",
        bit_rate=4_000_000,
        size=15_000_000,
        has_audio=True,
    )


@pytest.mark.parametrize(
    ("src", "preset", "expected"),
    [
        ((1080, 1920), "2160p", (2160, 3840)),  # portrait reel -> portrait 4K
        ((1920, 1080), "2160p", (3840, 2160)),  # landscape -> UHD
        ((1080, 1350), "2160p", (2160, 2700)),  # 4:5 post
        ((720, 1280), "2160p", (2160, 3840)),  # 720p story -> 3x
        ((1080, 2340), "2160p", (1772, 3840)),  # very tall: long side capped
        ((1080, 1920), "1440p", (1440, 2560)),
        ((2160, 3840), "2160p", (2160, 3840)),  # already 4K: untouched
        ((1080, 1920), "original", (1080, 1920)),
    ],
)
def test_compute_target_size(src, preset, expected):
    assert compute_target_size(src[0], src[1], preset) == expected


def test_target_dimensions_are_even():
    w, h = compute_target_size(640, 1136, "2160p")
    assert w % 2 == 0 and h % 2 == 0
    assert w == 2160


@pytest.mark.parametrize(
    ("src_fps", "expected"),
    [
        (30.0, Fraction(2)),
        (30000 / 1001, Fraction(2)),  # 29.97 -> 59.94 (snapped, avoids audio drift)
        (25.0, Fraction(12, 5)),
        (24.0, Fraction(5, 2)),
        (24000 / 1001, Fraction(5, 2)),
        (50.0, Fraction(6, 5)),
        (15.0, Fraction(4)),
    ],
)
def test_choose_fps_ratio(src_fps, expected):
    assert choose_fps_ratio(src_fps, 60.0) == expected


def test_plan_typical_reel():
    plan = make_plan(info(1080, 1920, 30.0), "2160p", "60")
    assert (plan.target_width, plan.target_height) == (2160, 3840)
    assert plan.target_fps == 60.0
    assert plan.fps_ratio == 2
    assert plan.upscale and plan.interpolate and not plan.is_noop
    assert plan.total_output_frames == 1800
    assert plan.label == "2160p60"


def test_plan_keeps_high_fps_source():
    plan = make_plan(info(1080, 1920, 60.0), "2160p", "60")
    assert not plan.interpolate
    assert plan.target_fps == 60.0
    plan2 = make_plan(info(1080, 1920, 59.94), "original", "60")
    assert plan2.is_noop


def test_plan_noop_when_already_4k60():
    plan = make_plan(info(2160, 3840, 60.0), "2160p", "60")
    assert plan.is_noop


def test_plan_ntsc_rate_uses_exact_double():
    plan = make_plan(info(1080, 1920, 30000 / 1001), "original", "60")
    assert plan.fps_ratio == 2
    assert round(plan.target_fps, 2) == 59.94
    assert fps_to_ffmpeg_rate(plan.target_fps) == "60000/1001"


def test_plan_rejects_bad_presets():
    with pytest.raises(PlanError):
        make_plan(info(1080, 1920, 30.0), "8k", "60")
    with pytest.raises(PlanError):
        make_plan(info(1080, 1920, 30.0), "2160p", "120")
    with pytest.raises(PlanError):
        make_plan(info(0, 0, 30.0), "2160p", "60")


def test_fps_to_ffmpeg_rate_integer():
    assert fps_to_ffmpeg_rate(60.0) == "60"
    assert fps_to_ffmpeg_rate(24.0) == "24"
