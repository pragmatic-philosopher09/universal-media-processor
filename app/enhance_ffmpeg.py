"""Pure-ffmpeg enhancement: motion-compensated frame interpolation + high-quality upscaling.

Order matters: interpolation runs at the source resolution (4x cheaper than at 4K and just as
good), then the frames are upscaled with a Lanczos kernel and lightly sharpened with CAS
(contrast-adaptive sharpening), which adds perceived detail without the halo artefacts that
make cheap upscales look "artificial".
"""

from __future__ import annotations

from pathlib import Path

from .config import Settings
from .media import CancelToken, ProgressCallback, fps_to_ffmpeg_rate, run_ffmpeg
from .plan import EnhancePlan

INTERPOLATION_PRESETS = {
    # Best quality minterpolate: adaptive overlapped-block MC, bidirectional motion estimation
    # and variable-size blocks. Slow (a few frames/sec at 1080p) but visibly smoother.
    "high": "mi_mode=mci:mc_mode=aobmc:me_mode=bidir:me=epzs:vsbmc=1:search_param=48",
    # ffmpeg defaults with a tighter search window: roughly 2x faster, a bit more ghosting.
    "fast": "mi_mode=mci:mc_mode=obmc:me_mode=bilat:me=epzs:search_param=16",
}

SCALE_FLAGS = "lanczos+accurate_rnd+full_chroma_int"


# minterpolate needs look-ahead and drops the last ~1.5 source frames; cloning the final frame
# for this long before interpolating (and trimming afterwards) keeps the full clip.
TAIL_PAD_SECONDS = 0.5


def interpolation_filter(target_fps: float, quality: str) -> str:
    preset = INTERPOLATION_PRESETS.get(quality, INTERPOLATION_PRESETS["high"])
    # scd=fdiff with a slightly lower threshold than default avoids blending across the hard
    # cuts that short-form video is full of.
    return f"minterpolate=fps={fps_to_ffmpeg_rate(target_fps)}:{preset}:scd=fdiff:scd_threshold=8"


def interpolation_chain(plan: EnhancePlan, settings: Settings) -> list[str]:
    """tpad -> minterpolate -> trim, so the output covers exactly the source duration."""
    chain = [
        f"tpad=stop_mode=clone:stop_duration={TAIL_PAD_SECONDS}",
        interpolation_filter(plan.target_fps, settings.interp_quality),
    ]
    duration = plan.source.frames_duration
    if duration > 0:
        chain.append(f"trim=duration={duration:.6f}")
    return chain


def scale_filter(width: int, height: int) -> str:
    return f"scale={width}:{height}:flags={SCALE_FLAGS}"


def sharpen_filter(strength: float) -> str | None:
    if strength <= 0:
        return None
    return f"cas=strength={strength:.2f}"


def build_video_filter(plan: EnhancePlan, settings: Settings) -> str:
    filters: list[str] = []
    if plan.interpolate:
        filters.extend(interpolation_chain(plan, settings))
    if plan.upscale:
        filters.append(scale_filter(plan.target_width, plan.target_height))
        sharpen = sharpen_filter(settings.sharpen)
        if sharpen:
            filters.append(sharpen)
    filters.append("format=yuv420p")
    return ",".join(filters)


def target_bitrate_kbps(width: int, height: int, fps: float, bits_per_pixel: float) -> int:
    bits_per_second = width * height * fps * bits_per_pixel
    return int(max(4000, min(80000, bits_per_second / 1000)))


def encoder_args(
    encoder: str, width: int, height: int, fps: float, settings: Settings
) -> list[str]:
    """Encoder flags tuned for quality-per-bit while staying widely playable (MP4/H.264 default)."""
    hevc = encoder.startswith("hevc") or encoder == "libx265"
    args: list[str] = ["-c:v", encoder]
    if encoder == "libx264":
        args += [
            "-preset",
            settings.x264_preset,
            "-crf",
            str(settings.x264_crf),
            "-profile:v",
            "high",
        ]
    elif encoder == "libx265":
        args += ["-preset", settings.x264_preset, "-crf", str(settings.x264_crf + 2)]
    else:
        kbps = target_bitrate_kbps(width, height, fps, settings.bits_per_pixel)
        args += [
            "-b:v",
            f"{kbps}k",
            "-maxrate",
            f"{int(kbps * 1.5)}k",
            "-bufsize",
            f"{kbps * 2}k",
        ]
        if encoder.endswith("_videotoolbox"):
            args += ["-allow_sw", "1"]
            if not hevc:
                args += ["-profile:v", "high"]
        elif encoder.endswith("_nvenc"):
            args += ["-preset", "p5", "-rc", "vbr", "-multipass", "qres"]
            if not hevc:
                args += ["-profile:v", "high"]
        elif encoder.endswith("_qsv"):
            args += ["-preset", "slower"]
    if hevc:
        args += ["-tag:v", "hvc1"]
    args += ["-pix_fmt", "yuv420p"]
    return args


def audio_args(acodec: str | None, has_audio: bool) -> list[str]:
    if not has_audio:
        return ["-an"]
    if acodec in {"aac", "mp3"}:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "192k"]


def build_ffmpeg_command(
    plan: EnhancePlan, src: Path, dst: Path, encoder: str, settings: Settings
) -> list[str]:
    cmd = [
        settings.ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
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
        build_video_filter(plan, settings),
    ]
    cmd += encoder_args(encoder, plan.target_width, plan.target_height, plan.target_fps, settings)
    cmd += audio_args(plan.source.acodec, plan.source.has_audio)
    cmd += ["-movflags", "+faststart", str(dst)]
    return cmd


async def enhance_with_ffmpeg(
    plan: EnhancePlan,
    src: Path,
    dst: Path,
    *,
    encoder: str,
    settings: Settings,
    cancel: CancelToken,
    on_progress: ProgressCallback | None = None,
) -> None:
    cmd = build_ffmpeg_command(plan, src, dst, encoder, settings)
    await run_ffmpeg(
        cmd,
        cancel=cancel,
        duration=plan.source.duration or None,
        on_progress=on_progress,
    )


def build_image_command(plan: EnhancePlan, src: Path, dst: Path, settings: Settings) -> list[str]:
    """Upscale a still image (PNG output keeps it lossless)."""
    filters = [scale_filter(plan.target_width, plan.target_height)]
    sharpen = sharpen_filter(settings.sharpen)
    if sharpen:
        filters.append(sharpen)
    return [
        settings.ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-vf",
        ",".join(filters),
        "-update",
        "1",
        str(dst),
    ]


async def enhance_image(
    plan: EnhancePlan, src: Path, dst: Path, *, settings: Settings, cancel: CancelToken
) -> None:
    await run_ffmpeg(build_image_command(plan, src, dst, settings), cancel=cancel)
