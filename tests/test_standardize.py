"""Standardize-for-editing plans and ffmpeg commands (pure functions, no ffmpeg needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.media import VideoInfo
from app.plan import PlanError
from app.standardize import (
    StandardizeOptions,
    build_standardize_command,
    build_standardize_filter,
    fit_size,
    make_standardize_plan,
)


def video(width=1920, height=1080, fps=30.0, *, audio=True, vcodec="h264") -> VideoInfo:
    return VideoInfo(width, height, fps, 10.0, int(fps * 10), vcodec, "aac", None, None, audio)


def image(width=800, height=600) -> VideoInfo:
    return VideoInfo(width, height, 0.0, 0.0, 1, "png", None, None, None, False)


def test_fit_size_pad_and_crop():
    assert fit_size(64, 64, 1920, 1080, "pad") == (1080, 1080)
    assert fit_size(64, 64, 1920, 1080, "crop") == (1920, 1920)
    assert fit_size(1080, 1920, 1920, 1080, "pad") == (608, 1080)
    assert fit_size(3840, 2160, 1920, 1080, "pad") == (1920, 1080)


def test_matching_source_only_conforms_frame_rate_and_codec():
    plan = make_standardize_plan(video(), StandardizeOptions("1920x1080", "30", "pad"))
    assert not plan.resize and not plan.pad and not plan.crop
    assert plan.treatments == ("constant_fps", "transcoded")
    assert plan.warnings == ()
    assert build_standardize_filter(plan) == "fps=30,setsar=1,format=yuv420p"
    assert plan.label == "std-1920x1080-30fps"


def test_small_square_clip_is_upscaled_and_letterboxed_with_honest_warnings():
    plan = make_standardize_plan(video(64, 64, 10.0), StandardizeOptions("1280x720", "30", "pad"))
    assert (plan.content_width, plan.content_height) == (720, 720)
    assert plan.treatments == ("upscaled", "letterboxed", "fps_converted", "transcoded")
    joined = " ".join(plan.warnings)
    assert "bigger, not sharper" in joined
    assert "black bars" in joined
    assert "repeats frames" in joined and "Enhance" in joined
    vf = build_standardize_filter(plan)
    assert vf.startswith("fps=30,scale=720:720:")
    assert "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black" in vf


def test_crop_fit_and_frame_dropping():
    plan = make_standardize_plan(
        video(1080, 1920, 60.0), StandardizeOptions("1920x1080", "30", "crop")
    )
    assert plan.crop and not plan.pad
    assert "cropped" in plan.treatments and "downscaled" not in plan.treatments
    assert any("drops frames" in w for w in plan.warnings)
    assert "crop=1920:1080" in build_standardize_filter(plan)


def test_source_canvas_and_fps_keep_size_but_force_constant_frame_rate():
    plan = make_standardize_plan(
        video(1081, 721, 29.97), StandardizeOptions("source", "source", "pad")
    )
    assert (plan.canvas_width, plan.canvas_height) == (1080, 720)
    assert plan.treatments[0] == "downscaled"
    assert plan.target_fps == pytest.approx(29.97)
    assert "constant_fps" in plan.treatments and "fps_converted" not in plan.treatments
    assert build_standardize_filter(plan).startswith("fps=2997/100,")


def test_unknown_source_frame_rate_needs_an_explicit_choice():
    with pytest.raises(PlanError):
        make_standardize_plan(video(fps=0.0), StandardizeOptions("1920x1080", "source", "pad"))


def test_invalid_options_are_rejected():
    for options in (
        StandardizeOptions(canvas="8k"),
        StandardizeOptions(fps="120"),
        StandardizeOptions(fit="stretch"),
    ):
        with pytest.raises(ValueError):
            options.validate()


def test_video_command_is_editor_friendly(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, video_encoder="libx264")
    plan = make_standardize_plan(video(), StandardizeOptions("1280x720", "25", "pad"))
    cmd = build_standardize_command(plan, Path("in.mp4"), Path("out.mp4"), "libx264", settings)
    joined = " ".join(cmd)
    assert "-g 25" in joined
    assert "-c:a aac -b:a 192k -ar 48000 -ac 2" in joined
    assert "-movflags +faststart" in joined
    assert "-pix_fmt yuv420p" in joined

    silent = make_standardize_plan(video(audio=False), StandardizeOptions())
    cmd = build_standardize_command(silent, Path("in.mp4"), Path("out.mp4"), "libx264", settings)
    assert "-an" in cmd and "-ar" not in cmd


def test_image_is_placed_on_a_png_canvas(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    plan = make_standardize_plan(image(), StandardizeOptions("1080x1080", "30", "pad"))
    assert plan.label == "std-1080x1080"
    assert plan.treatments == ("upscaled", "letterboxed", "converted_png")
    vf = build_standardize_filter(plan)
    assert "fps=" not in vf and "format=yuv420p" not in vf
    cmd = build_standardize_command(plan, Path("in.png"), Path("out.png"), "", settings)
    assert "-frames:v" in cmd and "-progress" not in cmd
