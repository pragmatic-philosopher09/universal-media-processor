"""Decide what "enhance to 4K 60fps" means for a given source file.

Instagram delivers at most 1080x1920, usually 30 fps. The plan describes how far we
upscale (integer-friendly targets, even dimensions) and which rational frame-rate multiplier
to use so that interpolation lands on a clean, exact frame grid.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .media import VideoInfo

# preset -> (short side, long side cap)
RESOLUTION_PRESETS: dict[str, tuple[int, int] | None] = {
    "original": None,
    "1440p": (1440, 2560),
    "2160p": (2160, 3840),
}
FPS_PRESETS: dict[str, float | None] = {"original": None, "60": 60.0}

# Ratios that deviate from the requested target by less than this are snapped to a "nice"
# rational (e.g. 29.97 -> 59.94 fps is treated as an exact 2x instead of 2.002x).
FPS_SNAP_TOLERANCE = 0.01


class PlanError(ValueError):
    pass


def even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def compute_target_size(width: int, height: int, preset: str) -> tuple[int, int]:
    """Scale so the short side reaches the preset, capping the long side (keeps aspect ratio)."""
    target = RESOLUTION_PRESETS.get(preset)
    if target is None:
        return width, height
    short_target, long_cap = target
    short_side, long_side = min(width, height), max(width, height)
    factor = short_target / short_side
    if factor <= 1.0:
        return width, height
    if long_side * factor > long_cap:
        factor = long_cap / long_side
        if factor <= 1.0:
            return width, height
    return even(width * factor), even(height * factor)


def choose_fps_ratio(src_fps: float, target_fps: float) -> Fraction:
    """Return the p/q multiplier applied to the source frame rate.

    Small denominators keep frame interpolation exact and chunkable; near-integer ratios snap
    to the integer (29.97 -> 59.94 rather than 60.00) to avoid audio drift.
    """
    if src_fps <= 0:
        raise PlanError("Source frame rate is unknown")
    ratio = Fraction(target_fps / src_fps).limit_denominator(1000)
    for max_den in (1, 2, 3, 4, 5, 6):
        candidate = ratio.limit_denominator(max_den)
        if (
            candidate > 0
            and abs(float(candidate) - float(ratio)) / float(ratio) <= FPS_SNAP_TOLERANCE
        ):
            return candidate
    return ratio.limit_denominator(12)


@dataclass(frozen=True)
class EnhancePlan:
    source: VideoInfo
    target_width: int
    target_height: int
    target_fps: float
    fps_ratio: Fraction

    @property
    def upscale(self) -> bool:
        return (self.target_width, self.target_height) != (self.source.width, self.source.height)

    @property
    def interpolate(self) -> bool:
        return self.fps_ratio != 1

    @property
    def is_noop(self) -> bool:
        return not self.upscale and not self.interpolate

    @property
    def total_output_frames(self) -> int | None:
        if self.source.nb_frames is None:
            return None
        return int(round(self.source.nb_frames * self.fps_ratio))

    @property
    def label(self) -> str:
        short = min(self.target_width, self.target_height)
        if self.source.is_image:
            return f"{short}p"
        fps = int(round(self.target_fps))
        return f"{short}p{fps}"

    def to_dict(self) -> dict:
        return {
            "target_width": self.target_width,
            "target_height": self.target_height,
            "target_fps": round(self.target_fps, 3),
            "fps_ratio": str(self.fps_ratio),
            "upscale": self.upscale,
            "interpolate": self.interpolate,
            "label": self.label,
        }


def make_plan(source: VideoInfo, resolution: str = "2160p", fps: str = "60") -> EnhancePlan:
    if resolution not in RESOLUTION_PRESETS:
        raise PlanError(f"Unknown resolution preset {resolution!r}")
    if fps not in FPS_PRESETS:
        raise PlanError(f"Unknown frame-rate preset {fps!r}")
    if source.width <= 0 or source.height <= 0:
        raise PlanError("Source dimensions are unknown")

    target_w, target_h = compute_target_size(source.width, source.height, resolution)

    target_fps_pref = FPS_PRESETS[fps]
    ratio = Fraction(1)
    target_fps = source.fps
    if source.is_image:
        target_fps_pref = None
    if target_fps_pref is not None and source.fps > 0 and source.fps < target_fps_pref * 0.99:
        ratio = choose_fps_ratio(source.fps, target_fps_pref)
        target_fps = source.fps * float(ratio)

    return EnhancePlan(
        source=source,
        target_width=target_w,
        target_height=target_h,
        target_fps=target_fps,
        fps_ratio=ratio,
    )
