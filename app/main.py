"""FastAPI application: JSON API for jobs plus the static single-page frontend."""

from __future__ import annotations

import ipaddress
import logging
import platform
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .cookies import CookieError
from .enhance_ai import ai_capabilities
from .jobs import JobManager, JobNotFound, JobOptions, TooManyJobs
from .media import choose_encoder, list_encoders
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
            },
            "platform": platform.system(),
        }

    @app.post("/api/jobs", status_code=202)
    async def create_job(payload: CreateJobRequest, request: Request) -> dict:
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

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str) -> dict:
        try:
            return manager.get(job_id).to_dict()
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

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
