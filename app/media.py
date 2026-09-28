"""ffprobe/ffmpeg helpers: probing, encoder detection and progress-aware subprocess running."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import platform
import re
import shutil
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from .config import HARDWARE_ENCODERS, Settings

log = logging.getLogger(__name__)

ProgressCallback = Callable[[float], None]
IMAGE_CODECS = {"png", "mjpeg", "webp", "gif", "bmp", "tiff", "jpegxl", "avif"}

# Only self-contained video containers: no playlists, image sequences or network inputs.
UPLOAD_INPUT_ARGS = [
    "-protocol_whitelist",
    "file,pipe",
    "-format_whitelist",
    "mov,matroska,webm,avi,mpegts,mpeg,flv,ogg,asf",
]


class JobCancelled(Exception):
    """Raised inside a pipeline when the user cancelled the job."""


class FFmpegError(RuntimeError):
    pass


class CancelToken:
    """Cooperative cancellation shared between the API and a running pipeline.

    `cancelled` may be read from worker threads; `cancel()` must be called from the event loop.
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._procs: set[asyncio.subprocess.Process] = set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True
        for proc in list(self._procs):
            _kill(proc)

    def check(self) -> None:
        if self._cancelled:
            raise JobCancelled()

    def register(self, proc: asyncio.subprocess.Process) -> None:
        self._procs.add(proc)
        if self._cancelled:
            _kill(proc)

    def unregister(self, proc: asyncio.subprocess.Process) -> None:
        self._procs.discard(proc)


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        if proc.returncode is None:
            proc.kill()
    except ProcessLookupError:
        pass


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float
    nb_frames: int | None
    vcodec: str | None
    acodec: str | None
    bit_rate: int | None
    size: int | None
    has_audio: bool
    video_duration: float | None = None

    @property
    def is_image(self) -> bool:
        return (self.vcodec or "") in IMAGE_CODECS and (self.nb_frames or 1) <= 1

    @property
    def frames_duration(self) -> float:
        """Duration of the video stream itself (audio can run slightly longer)."""
        if self.nb_frames and self.fps:
            return self.nb_frames / self.fps
        return self.video_duration or self.duration

    @property
    def short_side(self) -> int:
        return min(self.width, self.height)

    @property
    def portrait(self) -> bool:
        return self.height > self.width

    def to_dict(self) -> dict:
        data = asdict(self)
        data["fps"] = round(self.fps, 3)
        data["duration"] = round(self.duration, 3)
        data["is_image"] = self.is_image
        return data


def parse_rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


def fps_to_ffmpeg_rate(fps: float) -> str:
    """Render an fps value in the exact `num/den` form ffmpeg expects (e.g. 60000/1001)."""
    frac = Fraction(fps).limit_denominator(1001)
    return str(frac.numerator) if frac.denominator == 1 else f"{frac.numerator}/{frac.denominator}"


async def ffprobe(
    path: Path, settings: Settings, *, local_upload: bool = False, cancel: CancelToken | None = None
) -> VideoInfo:
    cmd = [
        settings.ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,"
        "nb_frames,bit_rate,duration,sample_aspect_ratio:stream_side_data=rotation:"
        "format=duration,bit_rate,size",
        "-of",
        "json",
        *(UPLOAD_INPUT_ARGS if local_upload else []),
        str(path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    if cancel:
        cancel.register(proc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=60)
        if cancel:
            cancel.check()
    except TimeoutError as exc:
        raise FFmpegError("Video inspection timed out; try a different video.") from exc
    finally:
        _kill(proc)
        await proc.wait()
        if cancel:
            cancel.unregister(proc)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {path.name}: {err.decode(errors='replace').strip()}")
    data = json.loads(out or b"{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise FFmpegError(f"{path.name} has no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})

    fps = parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate"))
    duration = _to_float(fmt.get("duration")) or _to_float(video.get("duration")) or 0.0
    nb_frames = _to_int(video.get("nb_frames"))
    if not nb_frames and duration and fps:
        nb_frames = int(round(duration * fps))
    if (video.get("codec_name") or "") in IMAGE_CODECS and (nb_frames or 1) <= 1:
        fps, duration, nb_frames = 0.0, 0.0, 1
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if local_upload:
        sar = parse_rate((video.get("sample_aspect_ratio") or "").replace(":", "/")) or 1
        width = round(width * sar)
        rotation = next(
            (s["rotation"] for s in video.get("side_data_list", []) if "rotation" in s), 0
        )
        if abs(round(rotation)) % 180 == 90:
            width, height = height, width
    return VideoInfo(
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        nb_frames=nb_frames or None,
        vcodec=video.get("codec_name"),
        acodec=audio.get("codec_name") if audio else None,
        bit_rate=_to_int(fmt.get("bit_rate")) or _to_int(video.get("bit_rate")),
        size=_to_int(fmt.get("size")),
        has_audio=audio is not None,
        video_duration=_to_float(video.get("duration")),
    )


def _to_float(value) -> float | None:
    try:
        return float(value) if value not in (None, "N/A") else None
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    try:
        return int(value) if value not in (None, "N/A") else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- encoders


_encoder_cache: dict[str, str] = {}


async def list_encoders(settings: Settings) -> set[str]:
    proc = await asyncio.create_subprocess_exec(
        settings.ffmpeg_bin,
        "-hide_banner",
        "-encoders",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    names: set[str] = set()
    for line in out.decode(errors="replace").splitlines():
        match = re.match(r"^\s*[VASFXBD.]{6}\s+(\S+)", line)
        if match:
            names.add(match.group(1))
    return names


async def encoder_works(settings: Settings, encoder: str) -> bool:
    """Hardware encoders are listed even without a usable GPU; encode one frame to make sure."""
    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=256x256:r=30:d=0.1",
        "-frames:v",
        "1",
        "-c:v",
        encoder,
        "-pix_fmt",
        "yuv420p",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (TimeoutError, OSError):
        return False
    if proc.returncode != 0:
        log.debug("encoder %s unusable: %s", encoder, err.decode(errors="replace").strip())
        return False
    return True


async def choose_encoder(settings: Settings) -> str:
    """Pick the video encoder: explicit setting, else a working hardware encoder, else libx264."""
    key = f"{settings.ffmpeg_bin}|{settings.video_encoder}"
    if key in _encoder_cache:
        return _encoder_cache[key]
    if settings.video_encoder != "auto":
        chosen = settings.video_encoder
    else:
        available = await list_encoders(settings)
        preferred: list[str] = []
        if platform.system() == "Darwin":
            preferred.append("h264_videotoolbox")
        preferred += ["h264_nvenc", "h264_qsv"]
        chosen = "libx264"
        for candidate in preferred:
            if candidate in available and await encoder_works(settings, candidate):
                chosen = candidate
                break
    _encoder_cache[key] = chosen
    log.info("using video encoder %s", chosen)
    return chosen


def is_hardware_encoder(name: str) -> bool:
    return name in HARDWARE_ENCODERS


def which(binary: str) -> str | None:
    """Resolve a binary name or path; returns None when it is not executable."""
    path = Path(binary).expanduser()
    if path.is_file():
        return str(path)
    return shutil.which(binary)


# --------------------------------------------------------------------------- ffmpeg runner


_TIME_RE = re.compile(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$")


def parse_out_time(value: str) -> float | None:
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


async def run_ffmpeg(
    cmd: list[str],
    *,
    cancel: CancelToken,
    duration: float | None = None,
    on_progress: ProgressCallback | None = None,
    stdin_writer: Callable[[asyncio.StreamWriter], Awaitable[None]] | None = None,
) -> None:
    """Run ffmpeg, reporting progress parsed from `-progress pipe:1` output.

    `cmd` must already include `-progress pipe:1 -nostats` for progress reporting to work.
    """
    cancel.check()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_writer else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    cancel.register(proc)
    stderr_tail: deque[str] = deque(maxlen=30)

    async def read_progress() -> None:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text.startswith("out_time=") and duration and on_progress:
                seconds = parse_out_time(text.split("=", 1)[1])
                if seconds is not None:
                    on_progress(max(0.0, min(1.0, seconds / duration)))

    async def read_stderr() -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            stderr_tail.append(line.decode(errors="replace").rstrip())

    async def feed_stdin() -> None:
        assert proc.stdin is not None
        try:
            if stdin_writer:
                await stdin_writer(proc.stdin)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except BaseException:
            # The producer failed: stop the encoder instead of letting it finish a partial file.
            _kill(proc)
            raise
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.close()
                await proc.stdin.wait_closed()

    tasks = [read_progress(), read_stderr()]
    if stdin_writer:
        tasks.append(feed_stdin())
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        await proc.wait()
    finally:
        cancel.unregister(proc)
        _kill(proc)

    if cancel.cancelled:
        raise JobCancelled()
    for result in results:
        if isinstance(result, BaseException) and not isinstance(
            result, BrokenPipeError | ConnectionResetError
        ):
            raise result
    if proc.returncode != 0:
        tail = "\n".join(stderr_tail).strip() or f"exit code {proc.returncode}"
        raise FFmpegError(f"ffmpeg failed: {tail}")


async def run_tool(
    cmd: list[str], *, cancel: CancelToken, name: str | None = None, cwd: str | None = None
) -> tuple[str, str]:
    """Run an external tool to completion and return (stdout, stderr); raise on failure."""
    cancel.check()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(f"{name or cmd[0]} is not installed or not on PATH") from exc
    cancel.register(proc)
    try:
        out, err = await proc.communicate()
    finally:
        cancel.unregister(proc)
    if cancel.cancelled:
        raise JobCancelled()
    if proc.returncode != 0:
        tail = "\n".join(err.decode(errors="replace").splitlines()[-15:]).strip()
        raise FFmpegError(f"{name or cmd[0]} failed: {tail or f'exit code {proc.returncode}'}")
    return out.decode(errors="replace"), err.decode(errors="replace")
