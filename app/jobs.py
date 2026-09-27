"""In-memory job queue: download -> probe -> (optional) enhance -> serve files, with TTL cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .config import Settings
from .cookies import CookieError, CookieSource, discover_browser_session, resolve_cookie_source
from .enhance_ai import AIEngineError, enhance_with_ncnn, enhance_with_video2x, select_ai_engine
from .enhance_ffmpeg import enhance_with_ffmpeg
from .extractor import DownloadedItem, ExtractError, download
from .media import CancelToken, FFmpegError, JobCancelled, VideoInfo, choose_encoder, ffprobe
from .plan import FPS_PRESETS, RESOLUTION_PRESETS, EnhancePlan, PlanError, make_plan
from .urls import InstagramURL, InvalidURL, normalize_instagram_url

log = logging.getLogger(__name__)


class JobStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    ENHANCING = "enhancing"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = {JobStatus.QUEUED, JobStatus.DOWNLOADING, JobStatus.ENHANCING}
TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED}


class TooManyJobs(RuntimeError):
    pass


class JobNotFound(KeyError):
    pass


@dataclass
class JobOptions:
    mode: str = "enhance"  # original | enhance
    resolution: str = "2160p"
    fps: str = "60"
    engine: str = "auto"  # auto | ffmpeg | ai
    cookies: str | None = None  # never serialised; cleared as soon as the job has used it
    cookies_supplied: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self.cookies_supplied = bool(self.cookies and self.cookies.strip())

    def validate(self) -> None:
        if self.mode not in {"original", "enhance"}:
            raise ValueError("mode must be 'original' or 'enhance'")
        if self.resolution not in RESOLUTION_PRESETS:
            raise ValueError(f"resolution must be one of {', '.join(RESOLUTION_PRESETS)}")
        if self.fps not in FPS_PRESETS:
            raise ValueError(f"fps must be one of {', '.join(FPS_PRESETS)}")
        if self.engine not in {"auto", "ffmpeg", "ai"}:
            raise ValueError("engine must be auto, ffmpeg or ai")

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "resolution": self.resolution,
            "fps": self.fps,
            "engine": self.engine,
            "cookies_supplied": self.cookies_supplied,
        }


@dataclass
class OutputFile:
    index: int
    path: Path
    download_name: str
    kind: str  # original | enhanced
    media_id: str
    info: VideoInfo
    engine: str | None = None

    def to_dict(self, job_id: str) -> dict:
        return {
            "index": self.index,
            "kind": self.kind,
            "media_id": self.media_id,
            "download_name": self.download_name,
            "engine": self.engine,
            "size": self.path.stat().st_size if self.path.exists() else None,
            "width": self.info.width,
            "height": self.info.height,
            "fps": round(self.info.fps, 3),
            "duration": round(self.info.duration, 3),
            "vcodec": self.info.vcodec,
            "bit_rate": self.info.bit_rate,
            "url": f"/api/jobs/{job_id}/files/{self.index}",
        }


@dataclass
class Job:
    id: str
    target: InstagramURL
    options: JobOptions
    client_ip: str
    dir: Path
    client_is_local: bool = False
    cancel: CancelToken = field(default_factory=CancelToken)
    status: JobStatus = JobStatus.QUEUED
    stage: str = "Queued"
    progress: float = 0.0
    error: str | None = None
    auth: str | None = None
    sources: list[dict] = field(default_factory=list)
    plan: dict | None = None
    engine: str | None = None
    outputs: list[OutputFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    task: asyncio.Task | None = None

    def set_stage(self, status: JobStatus, stage: str, progress: float | None = None) -> None:
        self.status = status
        self.stage = stage
        if progress is not None:
            self.progress = progress
        self.updated_at = time.time()

    def set_progress(self, progress: float, stage: str | None = None) -> None:
        self.progress = max(0.0, min(1.0, progress))
        if stage:
            self.stage = stage
        self.updated_at = time.time()

    def finish(self, status: JobStatus, stage: str, error: str | None = None) -> None:
        self.status = status
        self.stage = stage
        self.error = error
        self.finished_at = time.time()
        self.updated_at = self.finished_at
        if status == JobStatus.DONE:
            self.progress = 1.0

    @property
    def done(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.target.url,
            "kind": self.target.kind,
            "options": self.options.to_dict(),
            "status": self.status.value,
            "stage": self.stage,
            "progress": round(self.progress, 4),
            "error": self.error,
            "auth": self.auth,
            "sources": self.sources,
            "plan": self.plan,
            "engine": self.engine,
            "outputs": [output.to_dict(self.id) for output in self.outputs],
            "warnings": self.warnings,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.created_at, 1),
        }


def safe_filename(value: str, fallback: str = "instagram") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("._")
    return cleaned[:60] or fallback


class JobManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobs: dict[str, Job] = {}
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._sweeper: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self.settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.settings.jobs_dir.iterdir():
            if stale.is_dir() and stale.name not in self.jobs:
                shutil.rmtree(stale, ignore_errors=True)
        self._sweeper = asyncio.create_task(self._sweep_forever())

    async def stop(self) -> None:
        if self._sweeper:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
        for job in list(self.jobs.values()):
            if not job.done:
                job.cancel.cancel()
                if job.task:
                    job.task.cancel()
        tasks = [job.task for job in self.jobs.values() if job.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                self.sweep()
            except Exception:  # pragma: no cover - defensive
                log.exception("job sweep failed")

    def sweep(self, now: float | None = None) -> int:
        now = now or time.time()
        ttl = self.settings.job_ttl_minutes * 60
        removed = 0
        for job in list(self.jobs.values()):
            reference = job.finished_at if job.done else None
            if reference is not None and now - reference > ttl:
                self.delete(job.id)
                removed += 1
            elif not job.done and now - job.created_at > max(ttl, 3600) * 4:
                log.warning("job %s exceeded its time budget; cancelling", job.id)
                self.cancel(job.id)
        return removed

    # ------------------------------------------------------------------ public API

    def get(self, job_id: str) -> Job:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise JobNotFound(job_id) from exc

    def create(
        self, url: str, options: JobOptions, client_ip: str, *, client_is_local: bool = False
    ) -> Job:
        options.validate()
        target = normalize_instagram_url(url, self.settings.allowed_domains)
        # Validate pasted cookies now so the user gets immediate feedback.
        resolve_cookie_source(self.settings, options.cookies)

        active = [
            job
            for job in self.jobs.values()
            if job.client_ip == client_ip and job.status in ACTIVE_STATUSES
        ]
        if len(active) >= self.settings.max_jobs_per_ip:
            raise TooManyJobs(
                f"You already have {len(active)} job(s) running. Wait for them to finish first."
            )

        job_id = uuid.uuid4().hex[:12]
        job = Job(
            id=job_id,
            target=target,
            options=options,
            client_ip=client_ip,
            dir=self.settings.jobs_dir / job_id,
            client_is_local=client_is_local,
        )
        job.dir.mkdir(parents=True, exist_ok=True)
        self.jobs[job_id] = job
        job.task = asyncio.create_task(self._run(job), name=f"job-{job_id}")
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.done:
            return job
        job.cancel.cancel()
        if job.status == JobStatus.QUEUED and job.task:
            job.task.cancel()
        return job

    def delete(self, job_id: str) -> None:
        job = self.get(job_id)
        self.cancel(job_id)
        self.jobs.pop(job_id, None)
        shutil.rmtree(job.dir, ignore_errors=True)

    # ------------------------------------------------------------------ execution

    async def _run(self, job: Job) -> None:
        try:
            async with self._semaphore:
                job.cancel.check()
                await self._execute(job)
        except (JobCancelled, asyncio.CancelledError):
            job.finish(JobStatus.CANCELLED, "Cancelled")
        except (
            ExtractError,
            PlanError,
            FFmpegError,
            AIEngineError,
            CookieError,
            InvalidURL,
        ) as exc:
            log.info("job %s failed: %s", job.id, exc)
            job.finish(JobStatus.ERROR, "Failed", str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("job %s crashed", job.id)
            job.finish(JobStatus.ERROR, "Failed", f"Unexpected error: {exc}")
        finally:
            job.options.cookies = None
            shutil.rmtree(job.dir / "chunks", ignore_errors=True)
            if job.status != JobStatus.DONE:
                for stray in job.dir.glob("*.part"):
                    stray.unlink(missing_ok=True)

    def _browser_login_allowed(self, job: Job) -> bool:
        mode = self.settings.auto_browser_cookies
        return mode == "always" or (mode == "local" and job.client_is_local)

    async def _browser_login(self, job: Job) -> tuple[CookieSource | None, str]:
        result = await asyncio.to_thread(discover_browser_session, self.settings)
        source = result.session.as_source() if result.session else None
        return source, result.summary()

    async def _fetch(self, job: Job) -> tuple[object, CookieSource]:
        """Download with the configured credentials, falling back to the operator's own browser
        login (same-machine requests only) when Instagram insists on a login."""
        settings = self.settings
        cookie_source = resolve_cookie_source(settings, job.options.cookies)
        job.options.cookies = None
        browser_allowed = self._browser_login_allowed(job)

        if cookie_source.kind == "none" and browser_allowed and job.target.requires_login:
            # Stories never work anonymously: skip straight to the browser session.
            job.set_progress(0.0, "Looking for your browser's Instagram login…")
            browser_source, _ = await self._browser_login(job)
            cookie_source = browser_source or cookie_source

        loop = asyncio.get_running_loop()

        def report_download(fraction: float, label: str) -> None:
            loop.call_soon_threadsafe(job.set_progress, fraction, label)

        job.set_progress(0.0, f"Contacting Instagram ({cookie_source.describe()})…")
        try:
            result = await asyncio.to_thread(
                download, job.target, job.dir, settings, cookie_source, job.cancel, report_download
            )
            return result, cookie_source
        except ExtractError as exc:
            if not (exc.login_required and cookie_source.kind == "none" and browser_allowed):
                raise
            job.set_progress(0.0, "Instagram wants a login — checking your browsers…")
            browser_source, summary = await self._browser_login(job)
            if browser_source is None:
                raise ExtractError(
                    "Instagram only shows this to logged-in users, and no Instagram login was "
                    f"found in your browsers ({summary}). Log in to instagram.com in one of them "
                    "and try again — the app picks the login up automatically — or paste a "
                    "sessionid under Advanced.",
                    "login",
                ) from exc
            job.set_progress(0.0, f"Retrying with {browser_source.describe()}…")
            result = await asyncio.to_thread(
                download,
                job.target,
                job.dir,
                settings,
                browser_source,
                job.cancel,
                report_download,
            )
            return result, browser_source

    async def _execute(self, job: Job) -> None:
        settings = self.settings
        job.set_stage(JobStatus.DOWNLOADING, "Contacting Instagram…", 0.0)
        result, cookie_source = await self._fetch(job)
        job.auth = cookie_source.describe()
        job.cancel.check()
        job.set_progress(1.0, "Download complete")

        probed: list[tuple[DownloadedItem, VideoInfo]] = []
        for item in result.items:
            info = await ffprobe(item.path, settings)
            probed.append((item, info))
            job.sources.append(
                {
                    "media_id": item.media_id,
                    "title": item.title,
                    "uploader": item.uploader,
                    "channel": item.channel,
                    "format_id": item.format_id,
                    **info.to_dict(),
                }
            )
        job.updated_at = time.time()

        outputs: list[OutputFile] = []
        enhance = job.options.mode == "enhance"
        for position, (item, info) in enumerate(probed, start=1):
            base_name = f"{safe_filename(item.display_name)}_{safe_filename(item.media_id)}"
            if enhance:
                enhanced = await self._enhance_item(
                    job, item, info, base_name, position, len(probed)
                )
                if enhanced is not None:
                    enhanced.index = len(outputs)
                    outputs.append(enhanced)
            outputs.append(
                OutputFile(
                    index=len(outputs),
                    path=item.path,
                    download_name=f"{base_name}.mp4",
                    kind="original",
                    media_id=item.media_id,
                    info=info,
                )
            )
        job.outputs = outputs
        job.finish(JobStatus.DONE, "Ready")

    async def _enhance_item(
        self,
        job: Job,
        item: DownloadedItem,
        info: VideoInfo,
        base_name: str,
        position: int,
        count: int,
    ) -> OutputFile | None:
        settings = self.settings
        plan = make_plan(info, job.options.resolution, job.options.fps)
        job.plan = plan.to_dict()
        if plan.is_noop:
            job.warnings.append(
                f"{item.media_id}: source is already {info.width}x{info.height} @ {info.fps:.0f} fps; "
                "nothing to enhance."
            )
            return None
        if settings.max_duration_seconds and info.duration > settings.max_duration_seconds:
            job.warnings.append(
                f"{item.media_id}: {info.duration:.0f}s exceeds the {settings.max_duration_seconds}s "
                "enhancement limit; only the original is provided."
            )
            return None

        engine = self._pick_engine(plan, job.options.engine)
        job.engine = engine
        encoder = await choose_encoder(settings)
        prefix = f"[{position}/{count}] " if count > 1 else ""
        job.set_stage(
            JobStatus.ENHANCING,
            f"{prefix}Enhancing to {plan.target_width}x{plan.target_height} @ {plan.target_fps:.0f} fps ({engine})",
            0.0,
        )

        dst = job.dir / f"out-{safe_filename(item.media_id)}-{plan.label}.mp4"
        dst.unlink(missing_ok=True)

        def report(fraction: float) -> None:
            job.set_progress(fraction)

        started = time.time()
        if engine == "ffmpeg":
            await enhance_with_ffmpeg(
                plan,
                item.path,
                dst,
                encoder=encoder,
                settings=settings,
                cancel=job.cancel,
                on_progress=report,
            )
        elif engine == "ncnn":
            await enhance_with_ncnn(
                plan,
                item.path,
                dst,
                workdir=job.dir,
                encoder=encoder,
                settings=settings,
                cancel=job.cancel,
                on_progress=report,
            )
        else:
            await enhance_with_video2x(
                plan,
                item.path,
                dst,
                workdir=job.dir,
                encoder=encoder,
                settings=settings,
                cancel=job.cancel,
                on_progress=report,
            )
        job.cancel.check()
        log.info(
            "job %s enhanced %s with %s in %.1fs",
            job.id,
            item.media_id,
            engine,
            time.time() - started,
        )

        out_info = await ffprobe(dst, settings)
        return OutputFile(
            index=0,
            path=dst,
            download_name=f"{base_name}_{plan.label}.mp4",
            kind="enhanced",
            media_id=item.media_id,
            info=out_info,
            engine=f"{engine}/{encoder}",
        )

    def _pick_engine(self, plan: EnhancePlan, requested: str) -> str:
        ai_engine = select_ai_engine(plan, self.settings)
        if requested == "ffmpeg":
            return "ffmpeg"
        if requested == "ai":
            if not ai_engine:
                raise AIEngineError(
                    "AI enhancement isn't available on this server (rife-ncnn-vulkan / "
                    "realesrgan-ncnn-vulkan or video2x not found). Choose the ffmpeg engine instead."
                )
            return ai_engine
        return ai_engine or "ffmpeg"
