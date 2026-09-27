"""Application settings, loaded from environment variables.

Every knob has a sensible default so `uvicorn app.main:app` works out of the box.
See `.env.example` and the README for documentation of each variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ALLOWED_DOMAINS = ("instagram.com", "instagr.am", "ig.me")
DEFAULT_BROWSER_ORDER = (
    "safari",
    "chrome",
    "firefox",
    "edge",
    "brave",
    "chromium",
    "vivaldi",
    "opera",
)

SOFTWARE_ENCODERS = ("libx264", "libx265")
HARDWARE_ENCODERS = (
    "h264_videotoolbox",
    "hevc_videotoolbox",
    "h264_nvenc",
    "hevc_nvenc",
    "h264_qsv",
    "hevc_qsv",
)
KNOWN_ENCODERS = ("auto", *SOFTWARE_ENCODERS, *HARDWARE_ENCODERS)


def load_dotenv(path: Path | None = None) -> int:
    """Load KEY=VALUE lines from `.env` (or DOTENV_PATH) into os.environ without overriding
    variables that are already set. Returns the number of variables loaded."""
    path = path or Path(os.environ.get("DOTENV_PATH", ".env"))
    if not path.is_file():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _str(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _path(name: str, default: str | None = None) -> Path | None:
    raw = _str(name, default)
    return Path(raw).expanduser() if raw else None


@dataclass(frozen=True)
class Settings:
    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    trust_proxy: bool = False
    data_dir: Path = Path("data")
    job_ttl_minutes: int = 60
    max_concurrent_jobs: int = 2
    max_jobs_per_ip: int = 2
    max_duration_seconds: int = 600
    allowed_domains: tuple[str, ...] = DEFAULT_ALLOWED_DOMAINS

    # Instagram authentication (needed for stories / private content)
    ig_sessionid: str | None = None
    ig_cookies: str | None = None
    ig_cookies_file: Path | None = None
    ig_cookies_from_browser: str | None = None
    allow_user_cookies: bool = True
    # local = use the operator's own browser login for requests from this machine,
    # always = for every request (single-user deployments only), off = never
    auto_browser_cookies: str = "local"
    browser_cookie_order: tuple[str, ...] = DEFAULT_BROWSER_ORDER

    # ffmpeg
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    video_encoder: str = "auto"
    x264_preset: str = "medium"
    x264_crf: int = 18
    sharpen: float = 0.3
    interp_quality: str = "high"
    bits_per_pixel: float = 0.08

    # AI enhancement (optional external binaries)
    ai_engine: str = "auto"
    rife_bin: str = "rife-ncnn-vulkan"
    realesrgan_bin: str = "realesrgan-ncnn-vulkan"
    video2x_bin: str = "video2x"
    ai_rife_model: str = "rife-v4.6"
    ai_upscale_model: str = "realesrgan-x4plus"
    ai_upscale_scale: int = 0
    ai_chunk_frames: int = 32
    ai_frame_format: str = "png"
    ai_gpu_id: str | None = None
    ai_tile_size: int = 0
    video2x_rife_model: str = "rife-v4.26"
    video2x_realesrgan_model: str = "realesr-animevideov3"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        allowed = _str("ALLOWED_DOMAINS")
        domains = (
            tuple(d.strip().lower() for d in allowed.split(",") if d.strip())
            if allowed
            else DEFAULT_ALLOWED_DOMAINS
        )
        encoder = (_str("VIDEO_ENCODER", "auto") or "auto").lower()
        if encoder not in KNOWN_ENCODERS:
            raise ValueError(f"VIDEO_ENCODER must be one of {', '.join(KNOWN_ENCODERS)}")
        ai_engine = (_str("AI_ENGINE", "auto") or "auto").lower()
        if ai_engine not in {"auto", "ncnn", "video2x", "off"}:
            raise ValueError("AI_ENGINE must be one of auto, ncnn, video2x, off")
        interp_quality = (_str("FFMPEG_INTERP_QUALITY", "high") or "high").lower()
        if interp_quality not in {"high", "fast"}:
            raise ValueError("FFMPEG_INTERP_QUALITY must be 'high' or 'fast'")
        frame_format = (_str("AI_FRAME_FORMAT", "png") or "png").lower()
        if frame_format not in {"png", "webp", "jpg"}:
            raise ValueError("AI_FRAME_FORMAT must be png, webp or jpg")
        auto_browser = (_str("AUTO_BROWSER_COOKIES", "local") or "local").lower()
        if auto_browser not in {"local", "always", "off"}:
            raise ValueError("AUTO_BROWSER_COOKIES must be local, always or off")
        browser_order_raw = _str("BROWSER_COOKIE_ORDER")
        # Profile names are case-sensitive paths on Linux; parse_browser_spec lowercases the
        # browser part later.
        browser_order = (
            tuple(b.strip() for b in browser_order_raw.split(",") if b.strip())
            if browser_order_raw
            else DEFAULT_BROWSER_ORDER
        )

        return cls(
            host=_str("HOST", "0.0.0.0") or "0.0.0.0",
            port=_int("PORT", 8000),
            trust_proxy=_bool("TRUST_PROXY", False),
            data_dir=_path("DATA_DIR", "data") or Path("data"),
            job_ttl_minutes=_int("JOB_TTL_MINUTES", 60),
            max_concurrent_jobs=max(1, _int("MAX_CONCURRENT_JOBS", 2)),
            max_jobs_per_ip=max(1, _int("MAX_JOBS_PER_IP", 2)),
            max_duration_seconds=_int("MAX_DURATION_SECONDS", 600),
            allowed_domains=domains,
            ig_sessionid=_str("IG_SESSIONID"),
            ig_cookies=_str("IG_COOKIES"),
            ig_cookies_file=_path("IG_COOKIES_FILE"),
            ig_cookies_from_browser=_str("IG_COOKIES_FROM_BROWSER"),
            allow_user_cookies=_bool("ALLOW_USER_COOKIES", True),
            auto_browser_cookies=auto_browser,
            browser_cookie_order=browser_order,
            ffmpeg_bin=_str("FFMPEG_BIN", "ffmpeg") or "ffmpeg",
            ffprobe_bin=_str("FFPROBE_BIN", "ffprobe") or "ffprobe",
            video_encoder=encoder,
            x264_preset=_str("X264_PRESET", "medium") or "medium",
            x264_crf=_int("X264_CRF", 18),
            sharpen=max(0.0, min(1.0, _float("SHARPEN", 0.3))),
            interp_quality=interp_quality,
            bits_per_pixel=_float("BITS_PER_PIXEL", 0.08),
            ai_engine=ai_engine,
            rife_bin=_str("RIFE_BIN", "rife-ncnn-vulkan") or "rife-ncnn-vulkan",
            realesrgan_bin=_str("REALESRGAN_BIN", "realesrgan-ncnn-vulkan")
            or "realesrgan-ncnn-vulkan",
            video2x_bin=_str("VIDEO2X_BIN", "video2x") or "video2x",
            ai_rife_model=_str("AI_RIFE_MODEL", "rife-v4.6") or "rife-v4.6",
            ai_upscale_model=_str("AI_UPSCALE_MODEL", "realesrgan-x4plus") or "realesrgan-x4plus",
            ai_upscale_scale=_int("AI_UPSCALE_SCALE", 0),
            ai_chunk_frames=max(2, _int("AI_CHUNK_FRAMES", 32)),
            ai_frame_format=frame_format,
            ai_gpu_id=_str("AI_GPU_ID"),
            ai_tile_size=_int("AI_TILE_SIZE", 0),
            video2x_rife_model=_str("VIDEO2X_RIFE_MODEL", "rife-v4.26") or "rife-v4.26",
            video2x_realesrgan_model=_str("VIDEO2X_REALESRGAN_MODEL", "realesr-animevideov3")
            or "realesr-animevideov3",
        )

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def stories_auth_configured(self) -> bool:
        return any(
            (
                self.ig_sessionid,
                self.ig_cookies,
                self.ig_cookies_file,
                self.ig_cookies_from_browser,
            )
        )
