"""FastAPI application: JSON API for jobs plus the static single-page frontend."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import platform
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .cookies import CookieError
from .enhance_ai import ai_capabilities
from .jobs import Job, JobManager, JobNotFound, JobOptions, JobStatus, NotInReview, TooManyJobs
from .manifest import build_archive, build_manifest, ensure_checksums, manifest_csv
from .media import choose_encoder, list_encoders
from .standardize import CANVAS_PRESETS, FIT_MODES, STD_FPS_PRESETS, StandardizeOptions
from .uploads import (
    ALLOWED_UPLOAD_EXTENSIONS,
    UploadError,
    UploadNotFound,
    UploadTooLarge,
)
from .urls import PLATFORMS, InvalidURL

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
APP_VERSION = "2.0.0"

OUTCOMES = [
    {
        "id": "original",
        "name": "Keep original",
        "description": "Best quality the source offers, untouched.",
        "tradeoff": "Fastest. No changes, so nothing is invented.",
    },
    {
        "id": "enhance",
        "name": "Enhance",
        "description": "Upscale towards 4K and interpolate to 60 fps.",
        "tradeoff": "Slowest. Bigger, smoother files; new pixels and frames are generated.",
    },
    {
        "id": "standardize",
        "name": "Standardize for editing",
        "description": "One frame size, constant frame rate and H.264/AAC for every clip.",
        "tradeoff": "Fast. Drops onto a timeline cleanly; may letterbox, crop or repeat frames.",
    },
]


class StandardizeRequest(BaseModel):
    canvas: Literal[
        "source", "1920x1080", "1080x1920", "1280x720", "720x1280", "1080x1080", "3840x2160"
    ] = "1920x1080"
    fps: Literal["source", "24", "25", "30", "60"] = "30"
    fit: Literal["pad", "crop"] = "pad"


class ProcessingRequest(BaseModel):
    mode: Literal["original", "enhance", "standardize"] = "enhance"
    resolution: Literal["original", "1440p", "2160p"] = "2160p"
    fps: Literal["original", "60"] = "60"
    engine: Literal["auto", "ffmpeg", "ai"] = "auto"
    standardize: StandardizeRequest = Field(default_factory=StandardizeRequest)

    def to_options(self, cookies: str | None = None) -> JobOptions:
        return JobOptions(
            mode=self.mode,
            resolution=self.resolution,
            fps=self.fps,
            engine=self.engine,
            cookies=cookies,
            standardize=StandardizeOptions(**self.standardize.model_dump()),
        )


class CreateJobRequest(ProcessingRequest):
    url: str | None = Field(default=None, min_length=5, max_length=2000)
    urls: list[Annotated[str, Field(max_length=2000)]] = Field(default_factory=list, max_length=50)
    uploads: list[Annotated[str, Field(max_length=64)]] = Field(default_factory=list, max_length=50)
    cookies: str | None = Field(default=None, max_length=20000)
    review: bool = False


def client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def client_is_local(request: Request, settings: Settings) -> bool:
    """True only when the request demonstrably comes from the machine running the server.

    Any proxy header (even with TRUST_PROXY off) marks the client as remote, so a reverse proxy
    on the same host can never make remote users look local.
    """
    if any(request.headers.get(h) for h in ("x-forwarded-for", "forwarded", "x-real-ip")):
        return False
    try:
        return ipaddress.ip_address(client_ip(request, settings)).is_loopback
    except ValueError:
        return False


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    manager = JobManager(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(
        title="MediaFlow — Media Downloader (Instagram · YouTube · TikTok · DeviantArt)",
        version=APP_VERSION,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.manager = manager
    archive_locks: dict[str, asyncio.Lock] = {}

    def find_job(job_id: str) -> Job:
        try:
            return manager.get(job_id)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    def check_enhancement(mode: str) -> None:
        if mode == "enhance" and not settings.enhancement_enabled:
            raise HTTPException(
                status_code=400,
                detail="Enhancement is disabled on this server; choose 'Original (best available)'.",
            )

    def record_retrieval(job: Job) -> None:
        if job.status == JobStatus.DONE:
            manager.metrics.output_retrieved(job.id, job.created_at)

    @app.get("/api/capabilities")
    async def capabilities(request: Request) -> dict:
        encoders = await list_encoders(settings)
        encoder = await choose_encoder(settings)
        local = client_is_local(request, settings)
        return {
            "browser_login": {
                "mode": settings.auto_browser_cookies,
                "active_for_you": settings.auto_browser_cookies == "always"
                or (settings.auto_browser_cookies == "local" and local),
                "browsers": list(settings.browser_cookie_order),
            },
            "ffmpeg": {
                "encoder": encoder,
                "hardware": encoder not in {"libx264", "libx265"},
                "minterpolate": True,
                "encoders_available": sorted(
                    e for e in encoders if e in {"libx264", "libx265", encoder}
                ),
            },
            "ai": ai_capabilities(settings),
            "enhancement": {
                "enabled": settings.enhancement_enabled,
                "reason": None
                if settings.enhancement_enabled
                else "Enhancement is switched off on this server (not enough CPU); "
                "originals download in the best quality the platform serves.",
            },
            "platforms": [
                {
                    "id": p.id,
                    "name": p.name,
                    "domains": list(p.domains),
                    "max_native": p.max_native,
                    "login_walled": p.login_walled,
                    "login_configured": settings.login_configured(p.id),
                }
                for p in PLATFORMS.values()
            ],
            "stories_auth_configured": settings.stories_auth_configured,
            "allow_user_cookies": settings.allow_user_cookies,
            "yt_dlp_version": yt_dlp.version.__version__,
            "limits": {
                "max_duration_seconds": settings.max_duration_seconds,
                "max_jobs_per_ip": settings.max_jobs_per_ip,
                "job_ttl_minutes": settings.job_ttl_minutes,
                "max_items_per_job": settings.max_items_per_job,
            },
            "product": {"name": "MediaFlow", "version": APP_VERSION},
            "outcomes": [
                {
                    **outcome,
                    "available": outcome["id"] != "enhance" or settings.enhancement_enabled,
                }
                for outcome in OUTCOMES
            ],
            "standardize": {
                "canvas": list(CANVAS_PRESETS),
                "fps": list(STD_FPS_PRESETS),
                "fit": list(FIT_MODES),
                "defaults": StandardizeOptions().to_dict(),
            },
            "uploads": {
                "enabled": settings.uploads_enabled,
                "max_mb": settings.max_upload_mb,
                "max_waiting": settings.max_uploads_per_ip,
                "extensions": sorted(ALLOWED_UPLOAD_EXTENSIONS),
            },
            "metrics_enabled": settings.metrics_enabled,
            "platform": platform.system(),
        }

    @app.post("/api/uploads", status_code=201)
    async def create_upload(request: Request, filename: str) -> dict:
        if not settings.uploads_enabled:
            raise HTTPException(status_code=403, detail="File uploads are disabled on this server.")
        declared = request.headers.get("content-length")
        try:
            upload = await manager.uploads.save(
                request.stream(),
                filename,
                client_ip(request, settings),
                declared_size=int(declared) if declared and declared.isdigit() else None,
            )
        except UploadTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        except UploadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return upload.to_dict()

    @app.delete("/api/uploads/{upload_id}")
    async def delete_upload(upload_id: str, request: Request) -> dict:
        try:
            manager.uploads.delete(upload_id, client_ip(request, settings))
        except UploadNotFound as exc:
            raise HTTPException(status_code=404, detail="Upload not found") from exc
        return {"status": "deleted"}

    @app.post("/api/jobs", status_code=202)
    async def create_job(payload: CreateJobRequest, request: Request) -> dict:
        check_enhancement(payload.mode)
        urls = ([payload.url] if payload.url else []) + list(payload.urls)
        try:
            job = manager.create_from_inputs(
                urls,
                list(payload.uploads),
                payload.to_options(payload.cookies),
                client_ip(request, settings),
                client_is_local=client_is_local(request, settings),
                review=payload.review,
            )
        except UploadNotFound as exc:
            raise HTTPException(
                status_code=400,
                detail="One of the uploaded files has expired or was removed; add it again.",
            ) from exc
        except (InvalidURL, CookieError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TooManyJobs as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return job.to_dict()

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        return find_job(job_id).to_dict()

    @app.post("/api/jobs/{job_id}/plan")
    async def plan_job(job_id: str, payload: ProcessingRequest) -> dict:
        find_job(job_id)
        check_enhancement(payload.mode)
        try:
            return {"proposals": manager.preview(job_id, payload.to_options())}
        except NotInReview as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/start", status_code=202)
    async def start_job(job_id: str, payload: ProcessingRequest | None = None) -> dict:
        find_job(job_id)
        if payload is not None and not payload.model_fields_set:
            # An empty body ({}) means "approve the reviewed plan", not "switch to the defaults".
            payload = None
        if payload is not None:
            check_enhancement(payload.mode)
        try:
            job = manager.approve(job_id, payload.to_options() if payload else None)
        except NotInReview as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job.to_dict()

    @app.get("/api/jobs/{job_id}/manifest")
    async def get_manifest(job_id: str, format: Literal["json", "csv"] = "json") -> Response:
        job = find_job(job_id)
        if job.status == JobStatus.DONE:
            await asyncio.to_thread(ensure_checksums, job)
        manifest = build_manifest(job)
        name = f"mediaflow_{job.id}_manifest.{format}"
        headers = {"Content-Disposition": f'attachment; filename="{name}"'}
        if format == "csv":
            return Response(manifest_csv(manifest), media_type="text/csv", headers=headers)
        return Response(
            json.dumps(manifest, indent=2), media_type="application/json", headers=headers
        )

    @app.get("/api/jobs/{job_id}/archive")
    async def get_archive(job_id: str) -> FileResponse:
        job = find_job(job_id)
        if job.status != JobStatus.DONE or not job.outputs:
            raise HTTPException(status_code=409, detail="The job has no finished files yet.")
        lock = archive_locks.setdefault(job.id, asyncio.Lock())
        async with lock:
            if not job.dir.exists():
                raise HTTPException(status_code=410, detail="Files have expired")
            archive = await asyncio.to_thread(build_archive, job)
        record_retrieval(job)
        headers = {"Content-Disposition": f'attachment; filename="mediaflow_{job.id}.zip"'}
        return FileResponse(archive, media_type="application/zip", headers=headers)

    @app.get("/api/metrics")
    async def get_metrics() -> dict:
        if not settings.metrics_enabled:
            raise HTTPException(status_code=404, detail="Metrics are disabled")
        return manager.metrics.snapshot()

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str) -> dict:
        job = find_job(job_id)
        if job.done:
            manager.delete(job_id)
            archive_locks.pop(job_id, None)
            return {"status": "deleted"}
        manager.cancel(job_id)
        return {"status": "cancelling"}

    @app.get("/api/jobs/{job_id}/files/{index}")
    async def get_file(job_id: str, index: int, inline: bool = False) -> FileResponse:
        job = find_job(job_id)
        if index < 0 or index >= len(job.outputs):
            raise HTTPException(status_code=404, detail="File not found")
        output = job.outputs[index]
        if not output.path.exists():
            raise HTTPException(status_code=410, detail="File has expired")
        if not inline:
            record_retrieval(job)
        headers = {
            "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{output.download_name}"'
        }
        return FileResponse(output.path, media_type=output.media_type, headers=headers)

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        log.exception("unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


app = create_app()
