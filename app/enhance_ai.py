"""AI enhancement with external binaries (all optional):

* `rife-ncnn-vulkan`       - RIFE frame interpolation (30 -> 60 fps that looks like real motion)
* `realesrgan-ncnn-vulkan` - Real-ESRGAN super-resolution (1080p -> 4K with reconstructed detail)
* `video2x`                - one-binary alternative that wraps both of the above

The ncnn pipeline never materialises the whole video as images. ffmpeg decodes the source once
into a PNG stream; frames are grouped into small chunks (with the overlap RIFE needs), run
through the models, and streamed straight into a single ffmpeg encoder process. Peak disk usage
is one chunk of frames, regardless of clip length.
"""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .config import Settings
from .enhance_ffmpeg import (
    audio_args,
    encoder_args,
    interpolation_chain,
    scale_filter,
    sharpen_filter,
)
from .media import (
    CancelToken,
    FFmpegError,
    ProgressCallback,
    fps_to_ffmpeg_rate,
    run_ffmpeg,
    run_tool,
    which,
)
from .plan import EnhancePlan

log = logging.getLogger(__name__)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
IMAGE2PIPE_CODECS = {"png": "png", "webp": "webp", "jpg": "mjpeg"}
# Models that only ship a x4 network; everything else (realesr-animevideov3) offers x2/x3/x4.
FIXED_X4_MODEL_PREFIXES = ("realesrgan-x4plus", "realesrnet-x4plus", "realesr-general")
VIDEO2X_FIXED_X4_MODELS = {"realesrgan-plus", "realesrgan-plus-anime", "realesr-generalv3"}


class AIEngineError(RuntimeError):
    pass


@dataclass(frozen=True)
class AITools:
    rife: str | None
    realesrgan: str | None
    video2x: str | None

    def to_dict(self) -> dict:
        return {
            "rife": self.rife is not None,
            "realesrgan": self.realesrgan is not None,
            "video2x": self.video2x is not None,
        }


def detect_ai_tools(settings: Settings) -> AITools:
    if settings.ai_engine == "off":
        return AITools(None, None, None)
    return AITools(
        rife=which(settings.rife_bin),
        realesrgan=which(settings.realesrgan_bin),
        video2x=which(settings.video2x_bin),
    )


def select_ai_engine(
    plan: EnhancePlan, settings: Settings, tools: AITools | None = None
) -> str | None:
    """Return 'ncnn', 'video2x' or None depending on what the plan needs and what is installed."""
    if settings.ai_engine == "off":
        return None
    tools = tools or detect_ai_tools(settings)
    ncnn_ok = (not plan.interpolate or tools.rife is not None) and (
        not plan.upscale or tools.realesrgan is not None
    )
    video2x_ok = tools.video2x is not None
    if settings.ai_engine == "ncnn":
        return "ncnn" if ncnn_ok else None
    if settings.ai_engine == "video2x":
        return "video2x" if video2x_ok else None
    if ncnn_ok:
        return "ncnn"
    return "video2x" if video2x_ok else None


def ai_capabilities(settings: Settings) -> dict:
    tools = detect_ai_tools(settings)
    ncnn_full = tools.rife is not None and tools.realesrgan is not None
    return {
        "enabled": settings.ai_engine != "off",
        "available": ncnn_full or tools.video2x is not None,
        "tools": tools.to_dict(),
        "upscale_model": settings.ai_upscale_model,
        "interpolation_model": settings.ai_rife_model,
    }


# --------------------------------------------------------------------------- chunk planning


@dataclass(frozen=True)
class Chunk:
    start: int  # index of the first source frame in the chunk
    n_in: int  # source frames handed to the models (includes overlap frames)
    n_out: int  # RIFE output count (`-n`); equals n_in when not interpolating
    n_keep: int  # how many output frames are streamed to the encoder
    advance: int  # source frames consumed; the rest (n_in - advance) overlap the next chunk
    is_last: bool


def chunk_length(chunk_frames: int, ratio: Fraction) -> int:
    """Chunk length must be a multiple of the ratio denominator so frame counts stay integral."""
    q = ratio.denominator
    return max(q, (chunk_frames // q) * q)


def plan_chunk(start: int, remaining: int | None, chunk_frames: int, ratio: Fraction) -> Chunk:
    """Describe the next chunk; `remaining` is the estimated number of frames left (or None).

    With RIFE, output frame i sits at source position i * n_in / n_out. Feeding L + q frames and
    asking for (L + q) * p / q outputs puts frame i exactly at i * q / p, so the first L * p / q
    outputs cover the first L source frames on the exact target grid. The trailing q source
    frames are handed to the next chunk again, which keeps interpolation seamless at chunk edges.
    """
    if ratio == 1:
        n = chunk_frames if remaining is None else max(1, min(chunk_frames, remaining))
        is_last = remaining is not None and remaining <= chunk_frames
        return Chunk(start, n, n, n, n, is_last)

    p, q = ratio.numerator, ratio.denominator
    length = chunk_length(chunk_frames, ratio)
    if remaining is not None and remaining <= length + q:
        return tail_chunk(start, remaining, ratio)
    n_in = length + q
    return Chunk(start, n_in, n_in * p // q, length * p // q, length, False)


def tail_chunk(start: int, n_in: int, ratio: Fraction) -> Chunk:
    """Final chunk: no overlap, every output frame is kept."""
    n_in = max(1, n_in)
    n_out = max(1, int(round(n_in * ratio)))
    return Chunk(start, n_in, n_out, n_out, n_in, True)


def choose_upscale_scale(model: str, plan: EnhancePlan, configured: int) -> int:
    if configured in (2, 3, 4):
        return configured
    if model.startswith(FIXED_X4_MODEL_PREFIXES):
        return 4
    needed = max(plan.target_width / plan.source.width, plan.target_height / plan.source.height)
    return int(min(4, max(2, math.ceil(needed - 1e-6))))


# --------------------------------------------------------------------------- frame streaming


async def read_png(reader: asyncio.StreamReader) -> bytes | None:
    """Read one complete PNG image from a concatenated PNG stream (ffmpeg image2pipe)."""
    try:
        signature = await reader.readexactly(len(PNG_SIGNATURE))
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise FFmpegError("truncated PNG stream from ffmpeg") from exc
    if signature != PNG_SIGNATURE:
        raise FFmpegError("unexpected data in PNG stream from ffmpeg")
    parts = [signature]
    while True:
        header = await reader.readexactly(8)
        length = int.from_bytes(header[:4], "big")
        body = await reader.readexactly(length + 4)  # chunk data + CRC
        parts.append(header)
        parts.append(body)
        if header[4:8] == b"IEND":
            return b"".join(parts)


class FrameStream:
    """Decodes a video into PNG frames on demand (pipe back-pressure throttles ffmpeg)."""

    def __init__(self, src: Path, settings: Settings, cancel: CancelToken) -> None:
        self.src = src
        self.settings = settings
        self.cancel = cancel
        self.proc: asyncio.subprocess.Process | None = None
        self.exhausted = False
        self._stderr: list[str] = []
        self._stderr_task: asyncio.Task | None = None
        self._killed = False

    async def __aenter__(self) -> FrameStream:
        cmd = [
            self.settings.ffmpeg_bin,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(self.src),
            "-map",
            "0:v:0",
            "-fps_mode",
            "passthrough",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1 << 22,
        )
        self.cancel.register(self.proc)
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        return self

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            self._stderr.append(line.decode(errors="replace").rstrip())

    async def take(self, count: int) -> list[bytes]:
        frames: list[bytes] = []
        assert self.proc and self.proc.stdout
        while len(frames) < count and not self.exhausted:
            frame = await read_png(self.proc.stdout)
            if frame is None:
                self.exhausted = True
                break
            frames.append(frame)
        return frames

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.proc:
            self.cancel.unregister(self.proc)
            if self.proc.returncode is None and not self.exhausted:
                self._killed = True
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
            await self.proc.wait()
        if self._stderr_task:
            await self._stderr_task
        if exc_type is None and not self._killed and self.proc and self.proc.returncode != 0:
            tail = "\n".join(self._stderr[-10:])
            raise FFmpegError(f"frame extraction failed: {tail}")


async def _write_frames(directory: Path, frames: list[bytes], ext: str) -> list[Path]:
    def write() -> list[Path]:
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for index, data in enumerate(frames, start=1):
            path = directory / f"{index:08d}.{ext}"
            path.write_bytes(data)
            paths.append(path)
        return paths

    return await asyncio.to_thread(write)


def _sorted_images(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.is_file() and not p.name.startswith("."))


def _tool_cwd(binary: str) -> str:
    """Run ncnn tools from their own directory so the bundled model folders resolve."""
    return str(Path(binary).resolve().parent)


# --------------------------------------------------------------------------- ncnn pipeline


@dataclass(frozen=True)
class NcnnStages:
    rife: str | None
    realesrgan: str | None
    scale: int
    frame_format: str  # format of the frames that reach the encoder


def _stages(plan: EnhancePlan, settings: Settings) -> NcnnStages:
    tools = detect_ai_tools(settings)
    if plan.interpolate and not tools.rife:
        raise AIEngineError(f"{settings.rife_bin} not found")
    if plan.upscale and not tools.realesrgan:
        raise AIEngineError(f"{settings.realesrgan_bin} not found")
    if plan.interpolate and plan.fps_ratio != 2 and "rife-v4" not in settings.ai_rife_model:
        raise AIEngineError(
            f"A {plan.fps_ratio}x frame-rate change needs a rife-v4 model (AI_RIFE_MODEL), "
            f"got {settings.ai_rife_model!r}"
        )
    scale = (
        choose_upscale_scale(settings.ai_upscale_model, plan, settings.ai_upscale_scale)
        if plan.upscale
        else 1
    )
    # Without an upscale stage the source PNGs may reach the encoder untouched, so keep PNG.
    frame_format = settings.ai_frame_format if plan.upscale else "png"
    return NcnnStages(
        rife=tools.rife if plan.interpolate else None,
        realesrgan=tools.realesrgan if plan.upscale else None,
        scale=scale,
        frame_format=frame_format,
    )


def build_ncnn_encoder_command(
    plan: EnhancePlan, src: Path, dst: Path, encoder: str, stages: NcnnStages, settings: Settings
) -> list[str]:
    up_w = plan.source.width * stages.scale
    up_h = plan.source.height * stages.scale
    filters: list[str] = []
    if (up_w, up_h) != (plan.target_width, plan.target_height):
        filters.append(scale_filter(plan.target_width, plan.target_height))
    if plan.upscale:
        sharpen = sharpen_filter(settings.sharpen)
        if sharpen:
            filters.append(sharpen)
    filters.append("format=yuv420p")

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
        "-f",
        "image2pipe",
        "-vcodec",
        IMAGE2PIPE_CODECS[stages.frame_format],
        "-framerate",
        fps_to_ffmpeg_rate(plan.target_fps),
        "-i",
        "pipe:0",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-filter:v",
        ",".join(filters),
    ]
    cmd += encoder_args(encoder, plan.target_width, plan.target_height, plan.target_fps, settings)
    cmd += audio_args(plan.source.acodec, plan.source.has_audio)
    cmd += ["-movflags", "+faststart", str(dst)]
    return cmd


async def _run_rife(
    chunk: Chunk,
    in_dir: Path,
    out_dir: Path,
    stages: NcnnStages,
    settings: Settings,
    cancel: CancelToken,
) -> list[Path]:
    assert stages.rife
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        stages.rife,
        "-i",
        str(in_dir),
        "-o",
        str(out_dir),
        "-m",
        settings.ai_rife_model,
        "-f",
        f"%08d.{stages.frame_format}",
    ]
    if chunk.n_out != 2 * chunk.n_in:
        cmd += ["-n", str(chunk.n_out)]
    if settings.ai_gpu_id:
        cmd += ["-g", settings.ai_gpu_id]
    await run_tool(cmd, cancel=cancel, name="rife-ncnn-vulkan", cwd=_tool_cwd(stages.rife))
    outputs = _sorted_images(out_dir)
    if len(outputs) < chunk.n_keep:
        raise AIEngineError(
            f"rife-ncnn-vulkan produced {len(outputs)} frames, expected at least {chunk.n_keep}"
        )
    for extra in outputs[chunk.n_keep :]:
        extra.unlink(missing_ok=True)
    return outputs[: chunk.n_keep]


async def _run_realesrgan(
    in_dir: Path,
    out_dir: Path,
    expected: int,
    stages: NcnnStages,
    settings: Settings,
    cancel: CancelToken,
) -> list[Path]:
    assert stages.realesrgan
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        stages.realesrgan,
        "-i",
        str(in_dir),
        "-o",
        str(out_dir),
        "-n",
        settings.ai_upscale_model,
        "-s",
        str(stages.scale),
        "-f",
        stages.frame_format,
    ]
    if settings.ai_gpu_id:
        cmd += ["-g", settings.ai_gpu_id]
    if settings.ai_tile_size > 0:
        cmd += ["-t", str(settings.ai_tile_size)]
    await run_tool(
        cmd, cancel=cancel, name="realesrgan-ncnn-vulkan", cwd=_tool_cwd(stages.realesrgan)
    )
    outputs = _sorted_images(out_dir)
    if len(outputs) != expected:
        raise AIEngineError(
            f"realesrgan-ncnn-vulkan produced {len(outputs)} frames, expected {expected}"
        )
    return outputs


async def _process_chunk(
    chunk: Chunk,
    frames: list[bytes],
    chunk_dir: Path,
    stages: NcnnStages,
    settings: Settings,
    cancel: CancelToken,
) -> list[Path]:
    """Run the AI stages over one chunk and return the frame files to stream, in order."""
    in_dir = chunk_dir / "in"
    current = await _write_frames(in_dir, frames, "png")
    stage_dir = in_dir
    repeat = 1

    if stages.rife:
        if chunk.n_in >= 2:
            stage_dir = chunk_dir / "interp"
            current = await _run_rife(chunk, in_dir, stage_dir, stages, settings, cancel)
        else:
            # RIFE needs a frame pair; a lone trailing frame is simply held for its duration.
            repeat = chunk.n_out

    if stages.realesrgan:
        current = await _run_realesrgan(
            stage_dir, chunk_dir / "up", len(current), stages, settings, cancel
        )
    return current * repeat


async def enhance_with_ncnn(
    plan: EnhancePlan,
    src: Path,
    dst: Path,
    *,
    workdir: Path,
    encoder: str,
    settings: Settings,
    cancel: CancelToken,
    on_progress: ProgressCallback | None = None,
) -> None:
    stages = _stages(plan, settings)
    encoder_cmd = build_ncnn_encoder_command(plan, src, dst, encoder, stages, settings)
    ratio = plan.fps_ratio
    total_out = plan.total_output_frames
    chunks_dir = workdir / "chunks"
    shutil.rmtree(chunks_dir, ignore_errors=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    async def produce(writer: asyncio.StreamWriter) -> None:
        frames_done = 0
        start = 0
        estimated_total = plan.source.nb_frames
        carry: list[bytes] = []
        chunk_index = 0
        async with FrameStream(src, settings, cancel) as stream:
            while True:
                cancel.check()
                remaining = None if estimated_total is None else estimated_total - start
                if remaining is not None and remaining <= 0:
                    remaining = None  # the estimate was low; keep consuming until EOF
                chunk = plan_chunk(start, remaining, settings.ai_chunk_frames, ratio)
                fresh = await stream.take(max(0, chunk.n_in - len(carry)))
                frames = carry + fresh
                if not frames:
                    break
                if len(frames) < chunk.n_in or (stream.exhausted and not chunk.is_last):
                    chunk = tail_chunk(start, len(frames), ratio)
                    frames = frames[: chunk.n_in]

                chunk_dir = chunks_dir / f"{chunk_index:05d}"
                outputs = await _process_chunk(chunk, frames, chunk_dir, stages, settings, cancel)
                for path in outputs:
                    writer.write(await asyncio.to_thread(path.read_bytes))
                    await writer.drain()
                    frames_done += 1
                    if on_progress and total_out:
                        on_progress(min(0.999, frames_done / total_out))
                await asyncio.to_thread(shutil.rmtree, chunk_dir, True)

                carry = frames[chunk.advance :]
                start += chunk.advance
                chunk_index += 1
                if chunk.is_last and stream.exhausted:
                    break

    await run_ffmpeg(encoder_cmd, cancel=cancel, stdin_writer=produce)
    shutil.rmtree(chunks_dir, ignore_errors=True)
    if on_progress:
        on_progress(1.0)


# --------------------------------------------------------------------------- video2x pipeline


def _video2x_scale(model: str, plan: EnhancePlan) -> int:
    if model in VIDEO2X_FIXED_X4_MODELS:
        return 4
    needed = max(plan.target_width / plan.source.width, plan.target_height / plan.source.height)
    return int(min(4, max(2, math.ceil(needed - 1e-6))))


def _video2x_base(binary: str, src: Path, dst: Path) -> list[str]:
    return [
        binary,
        "-i",
        str(src),
        "-o",
        str(dst),
        "--no-progress",
        "-c",
        "libx264",
        "-e",
        "crf=12",
        "-e",
        "preset=veryfast",
    ]


async def enhance_with_video2x(
    plan: EnhancePlan,
    src: Path,
    dst: Path,
    *,
    workdir: Path,
    encoder: str,
    settings: Settings,
    cancel: CancelToken,
    on_progress: ProgressCallback | None = None,
) -> None:
    binary = which(settings.video2x_bin)
    if not binary:
        raise AIEngineError(f"{settings.video2x_bin} not found")

    def report(value: float) -> None:
        if on_progress:
            on_progress(value)

    current = src
    interp_via_ffmpeg = False
    if plan.interpolate:
        report(0.02)
        if plan.fps_ratio.denominator == 1:
            interp = workdir / "v2x-interp.mp4"
            cmd = _video2x_base(binary, current, interp) + [
                "-p",
                "rife",
                "-m",
                str(plan.fps_ratio.numerator),
                "--rife-model",
                settings.video2x_rife_model,
            ]
            await run_tool(cmd, cancel=cancel, name="video2x (rife)")
            current = interp
        else:
            # video2x only supports integer multipliers; fall back to ffmpeg for this stage.
            interp_via_ffmpeg = True
        report(0.45)

    if plan.upscale:
        scale = _video2x_scale(settings.video2x_realesrgan_model, plan)
        upscaled = workdir / "v2x-up.mp4"
        cmd = _video2x_base(binary, current, upscaled) + [
            "-p",
            "realesrgan",
            "-s",
            str(scale),
            "--realesrgan-model",
            settings.video2x_realesrgan_model,
        ]
        await run_tool(cmd, cancel=cancel, name="video2x (realesrgan)")
        current = upscaled
        report(0.85)

    # Final pass: normalise size/frame rate, apply gentle sharpening and the chosen encoder.
    filters: list[str] = []
    if interp_via_ffmpeg:
        filters.extend(interpolation_chain(plan, settings))
    filters.append(scale_filter(plan.target_width, plan.target_height))
    if plan.upscale:
        sharpen = sharpen_filter(settings.sharpen)
        if sharpen:
            filters.append(sharpen)
    filters.append("format=yuv420p")
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
        str(current),
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-filter:v",
        ",".join(filters),
    ]
    cmd += encoder_args(encoder, plan.target_width, plan.target_height, plan.target_fps, settings)
    cmd += audio_args(plan.source.acodec, plan.source.has_audio)
    cmd += ["-movflags", "+faststart", str(dst)]
    await run_ffmpeg(
        cmd,
        cancel=cancel,
        duration=plan.source.duration or None,
        on_progress=(lambda v: report(0.85 + 0.15 * v)) if on_progress else None,
    )
    for temp in (workdir / "v2x-interp.mp4", workdir / "v2x-up.mp4"):
        temp.unlink(missing_ok=True)
    report(1.0)
