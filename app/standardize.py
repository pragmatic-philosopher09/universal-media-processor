"""Standardize for editing: conform clips to one frame size, constant frame rate and codec.

Unlike Enhance (which only ever goes *up* and interpolates motion), Standardize makes a set of
mixed clips consistent so they drop onto an editing timeline without surprises:

- every clip lands on the same canvas (letterboxed or cropped, never stretched),
- a constant frame rate (variable-frame-rate phone clips are a classic editor headache),
- H.264 / yuv420p / AAC 48 kHz stereo in MP4 with a 1-second keyframe interval for scrubbing.

Frame-rate changes here duplicate or drop frames; smooth motion interpolation stays an Enhance
feature, and the plan says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .enhance_ffmpeg import SCALE_FLAGS, encoder_args
from .media import CancelToken, ProgressCallback, VideoInfo, fps_to_ffmpeg_rate, run_ffmpeg
from .plan import PlanError, even

# preset -> (width, height); None keeps the source frame size
CANVAS_PRESETS: dict[str, tuple[int, int] | None] = {
    "source": None,
    "1920x1080": (1920, 1080),
    "1080x1920": (1080, 1920),
    "1280x720": (1280, 720),
    "720x1280": (720, 1280),
    "1080x1080": (1080, 1080),
    "3840x2160": (3840, 2160),
}
STD_FPS_PRESETS: dict[str, float | None] = {
    "source": None,
    "24": 24.0,
    "25": 25.0,
    "30": 30.0,
    "60": 60.0,
}
FIT_MODES = ("pad", "crop")

AUDIO_SAMPLE_RATE = 48000


@dataclass(frozen=True)
class StandardizeOptions:
    canvas: str = "1920x1080"
    fps: str = "30"
    fit: str = "pad"

    def validate(self) -> None:
        if self.canvas not in CANVAS_PRESETS:
            raise ValueError(f"canvas must be one of {', '.join(CANVAS_PRESETS)}")
        if self.fps not in STD_FPS_PRESETS:
            raise ValueError(f"standardize fps must be one of {', '.join(STD_FPS_PRESETS)}")
        if self.fit not in FIT_MODES:
            raise ValueError("fit must be 'pad' or 'crop'")

    def to_dict(self) -> dict:
        return {"canvas": self.canvas, "fps": self.fps, "fit": self.fit}


@dataclass(frozen=True)
class StandardizePlan:
    source: VideoInfo
    canvas_width: int
    canvas_height: int
    content_width: int  # size of the scaled picture before padding / cropping
    content_height: int
    target_fps: float
    fit: str
    treatments: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def resize(self) -> bool:
        return (self.content_width, self.content_height) != (
            self.source.width,
            self.source.height,
        )

    @property
    def pad(self) -> bool:
        return self.fit == "pad" and (self.content_width, self.content_height) != (
            self.canvas_width,
            self.canvas_height,
        )

    @property
    def crop(self) -> bool:
        return self.fit == "crop" and (self.content_width, self.content_height) != (
            self.canvas_width,
            self.canvas_height,
        )

    @property
    def label(self) -> str:
        if self.source.is_image:
            return f"std-{self.canvas_width}x{self.canvas_height}"
        return f"std-{self.canvas_width}x{self.canvas_height}-{fps_label(self.target_fps)}fps"

    def to_dict(self) -> dict:
        return {
            "target_width": self.canvas_width,
            "target_height": self.canvas_height,
            "target_fps": round(self.target_fps, 3),
            "fit": self.fit,
            "resize": self.resize,
            "pad": self.pad,
            "crop": self.crop,
            "label": self.label,
            "treatments": list(self.treatments),
            "warnings": list(self.warnings),
        }


def fps_label(fps: float) -> str:
    return str(int(round(fps))) if abs(fps - round(fps)) < 0.01 else f"{fps:.2f}"


def fit_size(width: int, height: int, canvas_w: int, canvas_h: int, fit: str) -> tuple[int, int]:
    """Scale (width, height) to cover (crop) or fit inside (pad) the canvas, keeping aspect."""
    scale_w, scale_h = canvas_w / width, canvas_h / height
    factor = max(scale_w, scale_h) if fit == "crop" else min(scale_w, scale_h)
    new_w, new_h = even(width * factor), even(height * factor)
    if fit == "pad":
        new_w, new_h = min(new_w, canvas_w), min(new_h, canvas_h)
    else:
        new_w, new_h = max(new_w, canvas_w), max(new_h, canvas_h)
    return new_w, new_h


def make_standardize_plan(source: VideoInfo, options: StandardizeOptions) -> StandardizePlan:
    options.validate()
    if source.width <= 0 or source.height <= 0:
        raise PlanError("Source dimensions are unknown")

    canvas = CANVAS_PRESETS[options.canvas]
    if canvas is None:
        canvas_w, canvas_h = even(source.width), even(source.height)
    else:
        canvas_w, canvas_h = canvas
    content_w, content_h = fit_size(source.width, source.height, canvas_w, canvas_h, options.fit)

    treatments: list[str] = []
    warnings: list[str] = []
    if (content_w, content_h) != (source.width, source.height):
        upscaling = content_w * content_h > source.width * source.height
        treatments.append("upscaled" if upscaling else "downscaled")
        if upscaling:
            warnings.append(
                f"Upscaling {source.width}x{source.height} to fit {canvas_w}x{canvas_h} makes the "
                "frame bigger, not sharper."
            )
    if (content_w, content_h) != (canvas_w, canvas_h):
        if options.fit == "pad":
            treatments.append("letterboxed")
            warnings.append(
                f"Aspect ratio differs from the {canvas_w}x{canvas_h} canvas: black bars are added."
            )
        else:
            treatments.append("cropped")
            warnings.append(
                f"Aspect ratio differs from the {canvas_w}x{canvas_h} canvas: edges are cropped."
            )

    target_fps = 0.0
    if not source.is_image:
        wanted = STD_FPS_PRESETS[options.fps]
        if wanted is None:
            if source.fps <= 0:
                raise PlanError("Source frame rate is unknown; pick an explicit frame rate")
            target_fps = source.fps
            treatments.append("constant_fps")
        else:
            target_fps = wanted
            if abs(source.fps - wanted) / wanted > 0.001:
                treatments.append("fps_converted")
                if source.fps < wanted:
                    warnings.append(
                        f"{fps_label(source.fps)} → {fps_label(wanted)} fps repeats frames; "
                        "choose Enhance for smooth interpolated motion."
                    )
                else:
                    warnings.append(
                        f"{fps_label(source.fps)} → {fps_label(wanted)} fps drops frames."
                    )
            else:
                treatments.append("constant_fps")
        treatments.append("transcoded")
    else:
        treatments.append("converted_png")

    return StandardizePlan(
        source=source,
        canvas_width=canvas_w,
        canvas_height=canvas_h,
        content_width=content_w,
        content_height=content_h,
        target_fps=target_fps,
        fit=options.fit,
        treatments=tuple(treatments),
        warnings=tuple(warnings),
    )


def build_standardize_filter(plan: StandardizePlan) -> str:
    filters: list[str] = []
    if not plan.source.is_image and plan.target_fps > 0:
        filters.append(f"fps={fps_to_ffmpeg_rate(plan.target_fps)}")
    if plan.resize:
        filters.append(f"scale={plan.content_width}:{plan.content_height}:flags={SCALE_FLAGS}")
    if plan.pad:
        filters.append(
            f"pad={plan.canvas_width}:{plan.canvas_height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    elif plan.crop:
        filters.append(f"crop={plan.canvas_width}:{plan.canvas_height}")
    filters.append("setsar=1")
    if not plan.source.is_image:
        filters.append("format=yuv420p")
    return ",".join(filters)


def build_standardize_command(
    plan: StandardizePlan, src: Path, dst: Path, encoder: str, settings: Settings
) -> list[str]:
    base = [
        settings.ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
    ]
    if plan.source.is_image:
        return [
            *base,
            "-i",
            str(src),
            "-frames:v",
            "1",
            "-vf",
            build_standardize_filter(plan),
            "-update",
            "1",
            str(dst),
        ]
    cmd = [
        *base,
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-filter:v",
        build_standardize_filter(plan),
    ]
    cmd += encoder_args(encoder, plan.canvas_width, plan.canvas_height, plan.target_fps, settings)
    # A keyframe every second keeps timeline scrubbing responsive in editors.
    cmd += ["-g", str(max(1, int(round(plan.target_fps))))]
    if plan.source.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_SAMPLE_RATE), "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", str(dst)]
    return cmd


async def standardize_media(
    plan: StandardizePlan,
    src: Path,
    dst: Path,
    *,
    encoder: str,
    settings: Settings,
    cancel: CancelToken,
    on_progress: ProgressCallback | None = None,
) -> None:
    cmd = build_standardize_command(plan, src, dst, encoder, settings)
    await run_ffmpeg(
        cmd,
        cancel=cancel,
        duration=None if plan.source.is_image else (plan.source.duration or None),
        on_progress=on_progress,
    )
