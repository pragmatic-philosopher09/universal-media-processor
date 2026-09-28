"""FastAPI application: JSON API for jobs plus the static single-page frontend."""

from __future__ import annotations

import ipaddress
import logging
import platform
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import anyio
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .cookies import CookieError
from .enhance_ai import ai_capabilities
from .jobs import JobManager, JobNotFound, JobOptions, TooManyJobs
from .media import JobCancelled, choose_encoder, list_encoders
from .plan import CONVERSION_PRESETS, ConversionPreset
from .urls import PLATFORMS, InvalidURL

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class CreateJobRequest(BaseModel):
    url: str = Field(..., min_length=5, max_length=2000)
    mode: Literal["original", "enhance"] = "enhance"
    resolution: Literal["original", "1440p", "2160p"] = "2160p"
    fps: Literal["original", "60"] = "60"
    engine: Literal["auto", "ffmpeg", "ai"] = "auto"
    cookies: str | None = Field(default=None, max_length=20000)


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
    if bool(settings.app_username) != bool(settings.app_password):
        raise ValueError("Set both APP_USERNAME and APP_PASSWORD to enable app authentication.")
    manager = JobManager(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    app = FastAPI(
        title="Media Downloader (Instagram · YouTube · TikTok · DeviantArt)",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.manager = manager

    if settings.app_username and settings.app_password:
        basic_auth = HTTPBasic(auto_error=False)
        expected_user = settings.app_username.encode("utf-8")
        expected_password = settings.app_password.encode("utf-8")

        @app.middleware("http")
        async def authenticate(request: Request, call_next):
            try:
                credentials = await basic_auth(request)
            except HTTPException:
                credentials = None
            valid_user = secrets.compare_digest(
                credentials.username.encode("utf-8") if credentials else b"", expected_user
            )
            valid_password = secrets.compare_digest(
                credentials.password.encode("utf-8") if credentials else b"", expected_password
            )
            if not (valid_user and valid_password):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Sign in with the app username and password."},
                    headers={
                        "WWW-Authenticate": 'Basic realm="Media Downloader", charset="UTF-8"',
                        "Cache-Control": "no-store",
                    },
                )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                expected_origin = f"{request.url.scheme}://{request.url.netloc}"
                if request.headers.get("sec-fetch-site") == "cross-site" or (
                    origin is not None and origin != expected_origin
                ):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Cross-origin requests are not allowed."},
                        headers={"Cache-Control": "no-store"},
                    )
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Authorization"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

    @app.get("/api/capabilities")
    async def capabilities(request: Request) -> dict:
        encoders = await list_encoders(settings)
        encoder = await choose_encoder(settings)
        local = client_is_local(request, settings)
        return {
            "instagram_fallback": "fastvideosave" if settings.fastvideosave_enabled else None,
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
            "conversion": {
                "enabled": settings.enhancement_enabled,
                "presets": [
                    {"id": key, "label": value[0]} for key, value in CONVERSION_PRESETS.items()
                ],
                "output_format": "mp4",
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
                "max_upload_bytes": settings.max_upload_bytes,
            },
            "platform": platform.system(),
        }

    @app.post("/api/jobs", status_code=202)
    async def create_job(payload: CreateJobRequest, request: Request) -> dict:
        if payload.mode == "enhance" and not settings.enhancement_enabled:
            raise HTTPException(
                status_code=400,
                detail="Enhancement is disabled on this server; choose 'Original (best available)'.",
            )
        options = JobOptions(
            mode=payload.mode,
            resolution=payload.resolution,
            fps=payload.fps,
            engine=payload.engine,
            cookies=payload.cookies,
        )
        try:
            job = manager.create(
                payload.url,
                options,
                client_ip(request, settings),
                client_is_local=client_is_local(request, settings),
            )
        except (InvalidURL, CookieError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TooManyJobs as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return job.to_dict()

    @app.post("/api/uploads", status_code=202)
    async def upload_video(
        request: Request,
        filename: str = Query(..., min_length=1, max_length=255),
        preset: ConversionPreset = "2160p60",
    ) -> dict:
        """Stream a raw video body (not multipart) into a bounded conversion job."""
        if not settings.enhancement_enabled:
            raise HTTPException(
                status_code=400, detail="Video conversion is disabled on this server."
            )
        length = request.headers.get("content-length")
        try:
            expected = int(length) if length is not None else None
            if expected is not None and expected < 0:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length.") from exc
        if expected is not None and expected > settings.max_upload_bytes:
            raise HTTPException(
                status_code=413, detail="Video exceeds the server upload size limit."
            )
        try:
            job = manager.create_upload(filename, preset, client_ip(request, settings))
        except TooManyJobs as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        accepted = False
        try:
            size = 0
            async with await anyio.open_file(job.dir / "source.video", "wb") as output:
                async for chunk in request.stream():
                    job.cancel.check()
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(
                            status_code=413, detail="Video exceeds the server upload size limit."
                        )
                    await output.write(chunk)
                    job.set_progress(size / expected if expected else 0)
            if not size:
                raise HTTPException(status_code=400, detail="The uploaded video is empty.")
            job.cancel.check()
            manager.enqueue(job)
            accepted = True
            return job.to_dict()
        except JobCancelled as exc:
            raise HTTPException(status_code=409, detail="Upload cancelled.") from exc
        finally:
            if not accepted:
                manager.delete(job.id)

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        try:
            return manager.get(job_id).to_dict()
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    @app.post("/api/jobs/{job_id}/files/{index}/convert", status_code=202)
    async def convert_file(
        job_id: str, index: int, request: Request, preset: ConversionPreset = "2160p60"
    ) -> dict:
        try:
            job = manager.convert_output(job_id, index, preset, client_ip(request, settings))
        except (JobNotFound, IndexError) as exc:
            raise HTTPException(status_code=404, detail="Original job or file not found.") from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TooManyJobs as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return job.to_dict()

    @app.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str) -> dict:
        try:
            job = manager.get(job_id)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        if job.done:
            manager.delete(job_id)
            return {"status": "deleted"}
        manager.cancel(job_id)
        return {"status": "cancelling"}

    @app.get("/api/jobs/{job_id}/files/{index}")
    async def get_file(job_id: str, index: int, inline: bool = False) -> FileResponse:
        try:
            job = manager.get(job_id)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        if index < 0 or index >= len(job.outputs):
            raise HTTPException(status_code=404, detail="File not found")
        output = job.outputs[index]
        if not output.path.exists():
            raise HTTPException(status_code=410, detail="File has expired")
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
