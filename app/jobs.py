"""In-memory job queue: gather -> (optional review) -> process -> serve files, with TTL cleanup.

The v1 flow (one link, download, optional enhance) is unchanged. MediaFlow adds:

- multi-input jobs: several links and/or uploaded files processed with one set of options,
- a "standardize" outcome (conform clips for an editing timeline),
- an optional review step that shows per-source proposals before anything is processed,
- treatment labels on every output so it's clear what was native and what was generated.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from .config import Settings
from .cookies import (
    CookieError,
    CookieSource,
    discover_browser_session,
    parse_cookie_text,
    resolve_cookie_source,
)
from .enhance_ai import AIEngineError, enhance_with_ncnn, enhance_with_video2x, select_ai_engine
from .enhance_ffmpeg import enhance_image, enhance_with_ffmpeg
from .extractor import DownloadedItem, ExtractError, download
from .media import CancelToken, FFmpegError, JobCancelled, VideoInfo, choose_encoder, ffprobe
from .metrics import Metrics
from .plan import FPS_PRESETS, RESOLUTION_PRESETS, EnhancePlan, PlanError, make_plan
from .standardize import StandardizeOptions, make_standardize_plan, standardize_media
from .uploads import Upload, UploadStore
from .urls import InvalidURL, MediaURL, Platform, normalize_url

MEDIA_TYPES = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "avi": "video/x-msvideo",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

MODES = ("original", "enhance", "standardize")

log = logging.getLogger(__name__)


class JobStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    REVIEW = "review"
    ENHANCING = "enhancing"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = {
    JobStatus.QUEUED,
    JobStatus.DOWNLOADING,
    JobStatus.REVIEW,
    JobStatus.ENHANCING,
}
TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED}


class TooManyJobs(RuntimeError):
    pass


class JobNotFound(KeyError):
    pass


class NotInReview(RuntimeError):
    pass


@dataclass
class JobOptions:
    mode: str = "enhance"  # original | enhance | standardize
    resolution: str = "2160p"
    fps: str = "60"
    engine: str = "auto"  # auto | ffmpeg | ai
    cookies: str | None = None  # never serialised; cleared as soon as the job has used it
    standardize: StandardizeOptions = field(default_factory=StandardizeOptions)
    cookies_supplied: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self.cookies_supplied = bool(self.cookies and self.cookies.strip())

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError("mode must be 'original', 'enhance' or 'standardize'")
        if self.resolution not in RESOLUTION_PRESETS:
            raise ValueError(f"resolution must be one of {', '.join(RESOLUTION_PRESETS)}")
        if self.fps not in FPS_PRESETS:
            raise ValueError(f"fps must be one of {', '.join(FPS_PRESETS)}")
        if self.engine not in {"auto", "ffmpeg", "ai"}:
            raise ValueError("engine must be auto, ffmpeg or ai")
        self.standardize.validate()

    def with_choices_from(self, other: JobOptions) -> JobOptions:
        """Processing choices from `other`, keeping this job's (already consumed) login state."""
        updated = replace(other, cookies=None)
        updated.cookies_supplied = self.cookies_supplied
        return updated

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "resolution": self.resolution,
            "fps": self.fps,
            "engine": self.engine,
            "standardize": self.standardize.to_dict(),
            "cookies_supplied": self.cookies_supplied,
        }


@dataclass
class OutputFile:
    index: int
    path: Path
    download_name: str
    kind: str  # original | enhanced | standardized
    media_id: str
    info: VideoInfo
    engine: str | None = None
    treatments: list[str] = field(default_factory=list)
    source_index: int | None = None
    input_index: int | None = None
    sha256: str | None = None

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES.get(self.path.suffix.lstrip(".").lower(), "application/octet-stream")

    def to_dict(self, job_id: str) -> dict:
        return {
            "index": self.index,
            "kind": self.kind,
            "media_id": self.media_id,
            "download_name": self.download_name,
            "media_type": self.media_type,
            "is_image": self.info.is_image,
            "engine": self.engine,
            "treatments": list(self.treatments),
            "source_index": self.source_index,
            "input_index": self.input_index,
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
class JobInput:
    index: int
    kind: str  # link | upload
    label: str  # the link, or the uploaded file's name
    dir: Path
    target: MediaURL | None = None
    upload: Upload | None = None
    use_cookies: bool = False
    status: str = "pending"  # pending | fetching | ready | failed
    error: str | None = None
    auth: str | None = None
    item_count: int = 0

    def to_dict(self) -> dict:
        platform = self.target.platform_info if self.target else None
        return {
            "index": self.index,
            "kind": self.kind,
            "label": self.label,
            "platform": platform.id if platform else "upload",
            "platform_name": platform.name if platform else "Your file",
            "status": self.status,
            "error": self.error,
            "auth": self.auth,
            "item_count": self.item_count,
        }


@dataclass
class SourceItem:
    """One probed media item (a link can yield several, e.g. a carousel)."""

    index: int
    input_index: int
    kind: str  # link | upload
    item: DownloadedItem
    info: VideoInfo
    base_name: str
    download_name: str  # name of the untouched original


@dataclass
class Job:
    id: str
    target: MediaURL | None
    options: JobOptions
    client_ip: str
    dir: Path
    client_is_local: bool = False
    inputs: list[JobInput] = field(default_factory=list)
    review: bool = False
    cancel: CancelToken = field(default_factory=CancelToken)
    approved: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    status: JobStatus = JobStatus.QUEUED
    stage: str = "Queued"
    progress: float = 0.0
    error: str | None = None
    auth: str | None = None
    sources: list[dict] = field(default_factory=list)
    items: list[SourceItem] = field(default_factory=list, repr=False)
    proposals: list[dict] | None = None
    plan: dict | None = None
    engine: str | None = None
    outputs: list[OutputFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    current_item: int = 0
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

    @property
    def multi(self) -> bool:
        return len(self.inputs) > 1

    def _platform(self) -> tuple[str, str]:
        platforms = {i.target.platform_info for i in self.inputs if i.target}
        has_uploads = any(i.kind == "upload" for i in self.inputs)
        if len(platforms) == 1 and not has_uploads:
            only = next(iter(platforms))
            return only.id, only.name
        if not platforms:
            return "upload", "Your files"
        return "mixed", "Mixed sources"

    def to_dict(self) -> dict:
        platform, platform_name = self._platform()
        return {
            "id": self.id,
            "url": self.target.url if self.target else None,
            "platform": platform,
            "platform_name": platform_name,
            "kind": self.target.kind if self.target else "upload",
            "options": self.options.to_dict(),
            "status": self.status.value,
            "stage": self.stage,
            "progress": round(self.progress, 4),
            "error": self.error,
            "auth": self.auth,
            "review": self.review,
            "inputs": [i.to_dict() for i in self.inputs],
            "item_count": len(self.inputs),
            "current_item": self.current_item,
            "sources": self.sources,
            "proposals": self.proposals,
            "plan": self.plan,
            "engine": self.engine,
            "outputs": [output.to_dict(self.id) for output in self.outputs],
            "warnings": self.warnings,
            "failures": self.failures,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.created_at, 1),
        }


def safe_filename(value: str, fallback: str = "instagram") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "").strip("._")
    return cleaned[:60] or fallback


def enhance_treatments(plan: EnhancePlan, engine: str, settings: Settings) -> list[str]:
    ai = engine not in {"ffmpeg", ""}
    treatments: list[str] = []
    if plan.upscale:
        treatments.append("ai_upscaled" if ai else "upscaled")
        if not ai and settings.sharpen > 0:
            treatments.append("sharpened")
    if plan.interpolate:
        treatments.append("ai_interpolated" if ai else "interpolated")
    treatments.append("converted_png" if plan.source.is_image else "transcoded")
    return treatments


def noop_message(media_id: str, info: VideoInfo) -> str:
    what = (
        f"{info.width}x{info.height}"
        if info.is_image
        else f"{info.width}x{info.height} @ {info.fps:.0f} fps"
    )
    return f"{media_id}: source is already {what}; nothing to enhance."


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for value in values:
        key = value.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(key)
    return result


class JobManager:
    # After Instagram refuses an anonymous request, skip straight to the browser login for a while
    # instead of burning a round trip (and an anonymous hit against the IP) on every job.
    ANONYMOUS_BACKOFF_SECONDS = 15 * 60

    def __init__(
        self,
        settings: Settings,
        *,
        uploads: UploadStore | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.settings = settings
        self.jobs: dict[str, Job] = {}
        self.uploads = uploads or UploadStore(settings)
        self.metrics = metrics or Metrics(enabled=settings.metrics_enabled)
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._sweeper: asyncio.Task | None = None
        self._anonymous_blocked_until: dict[str, float] = {}

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        try:
            self.settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Read-only or missing DATA_DIR (common on PaaS containers): fall back to a temp dir.
            fallback = Path(tempfile.gettempdir()) / "media-downloader"
            log.warning(
                "DATA_DIR %s unusable (%s); using %s", self.settings.data_dir, exc, fallback
            )
            self.settings = replace(self.settings, data_dir=fallback)
            self.settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        for stale in self.settings.jobs_dir.iterdir():
            if stale.is_dir() and stale.name not in self.jobs:
                shutil.rmtree(stale, ignore_errors=True)
        self.uploads.settings = self.settings
        self.uploads.start()
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
            elif job.status == JobStatus.REVIEW and now - job.updated_at > ttl:
                log.info("job %s was left in review; cancelling", job.id)
                self.cancel(job.id)
            elif not job.done and now - job.created_at > max(ttl, 3600) * 4:
                log.warning("job %s exceeded its time budget; cancelling", job.id)
                self.cancel(job.id)
        self.uploads.sweep(now)
        return removed

    # ------------------------------------------------------------------ public API

    def get(self, job_id: str) -> Job:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise JobNotFound(job_id) from exc

    def create(
        self,
        url: str,
        options: JobOptions,
        client_ip: str,
        *,
        client_is_local: bool = False,
        review: bool = False,
    ) -> Job:
        return self.create_from_inputs(
            [url], [], options, client_ip, client_is_local=client_is_local, review=review
        )

    def create_from_inputs(
        self,
        urls: list[str],
        upload_ids: list[str],
        options: JobOptions,
        client_ip: str,
        *,
        client_is_local: bool = False,
        review: bool = False,
    ) -> Job:
        settings = self.settings
        options.validate()
        urls, upload_ids = _dedupe(urls), _dedupe(upload_ids)
        if not urls and not upload_ids:
            raise ValueError("Add at least one link or file.")
        if len(urls) + len(upload_ids) > settings.max_items_per_job:
            raise ValueError(f"A job can hold at most {settings.max_items_per_job} links or files.")

        targets: list[MediaURL] = []
        for url in urls:
            target = normalize_url(url, settings.allowed_domains)
            if all(t.url != target.url for t in targets):
                targets.append(target)
        uploads = [self.uploads.get(upload_id, client_ip) for upload_id in upload_ids]

        # Validate pasted cookies now so the user gets immediate feedback. In a mixed job the
        # pasted login only applies to the links of the platform it belongs to.
        use_cookies: list[bool] = []
        first_error: CookieError | None = None
        for target in targets:
            try:
                resolve_cookie_source(settings, options.cookies, target.platform_info)
                use_cookies.append(options.cookies_supplied)
            except CookieError as exc:
                if len(targets) == 1 or not options.cookies_supplied:
                    raise
                resolve_cookie_source(settings, None, target.platform_info)
                use_cookies.append(False)
                first_error = first_error or exc
        if first_error and not any(use_cookies):
            raise first_error
        if len(targets) > 1 and any(use_cookies):
            # Any header "parses" for most platforms, so in a mixed batch prefer the platforms
            # whose login cookie is actually in the pasted text.
            matches = [
                ok
                and t.platform_info.login_cookie
                in parse_cookie_text(options.cookies, t.platform_info)
                for t, ok in zip(targets, use_cookies, strict=True)
            ]
            if any(matches):
                use_cookies = matches

        active = [
            job
            for job in self.jobs.values()
            if job.client_ip == client_ip and job.status in ACTIVE_STATUSES
        ]
        if len(active) >= settings.max_jobs_per_ip:
            raise TooManyJobs(
                f"You already have {len(active)} job(s) running. Wait for them to finish first."
            )

        job_id = uuid.uuid4().hex[:12]
        job_dir = settings.jobs_dir / job_id
        multi = len(targets) + len(uploads) > 1
        inputs: list[JobInput] = []
        for target, cookies_ok in zip(targets, use_cookies, strict=True):
            index = len(inputs)
            inputs.append(
                JobInput(
                    index=index,
                    kind="link",
                    label=target.url,
                    dir=job_dir / f"input-{index}" if multi else job_dir,
                    target=target,
                    use_cookies=cookies_ok,
                )
            )
        for upload in uploads:
            index = len(inputs)
            inputs.append(
                JobInput(
                    index=index,
                    kind="upload",
                    label=upload.filename,
                    dir=job_dir / f"input-{index}" if multi else job_dir,
                    upload=upload,
                )
            )

        job = Job(
            id=job_id,
            target=targets[0] if targets else None,
            options=options,
            client_ip=client_ip,
            dir=job_dir,
            client_is_local=client_is_local,
            inputs=inputs,
            review=review,
        )
        if not targets:
            options.cookies = None
        job.dir.mkdir(parents=True, exist_ok=True)
        for job_input in inputs:
            job_input.dir.mkdir(parents=True, exist_ok=True)
            if job_input.upload is not None:
                upload = job_input.upload
                stem = safe_filename(upload.stem, "upload")
                job_input.upload = self.uploads.claim(
                    upload.id, client_ip, job_input.dir / f"src-{stem}.{upload.ext}"
                )
        self.jobs[job_id] = job
        self.metrics.job_created(options.mode, [i.kind for i in inputs], review)
        job.task = asyncio.create_task(self._run(job), name=f"job-{job_id}")
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.done:
            return job
        job.cancel.cancel()
        if job.status in {JobStatus.QUEUED, JobStatus.REVIEW} and job.task:
            job.task.cancel()
        return job

    def delete(self, job_id: str) -> None:
        job = self.get(job_id)
        self.cancel(job_id)
        self.jobs.pop(job_id, None)
        self.metrics.forget(job_id)
        shutil.rmtree(job.dir, ignore_errors=True)

    def preview(self, job_id: str, options: JobOptions) -> list[dict]:
        """Proposals for different options, without changing the job."""
        job = self.get(job_id)
        if job.status != JobStatus.REVIEW:
            raise NotInReview("This job isn't waiting for review.")
        options.validate()
        return self.proposals(job, options)

    def approve(self, job_id: str, options: JobOptions | None = None) -> Job:
        job = self.get(job_id)
        if job.status != JobStatus.REVIEW:
            raise NotInReview("This job isn't waiting for review.")
        if options is not None:
            options.validate()
            job.options = job.options.with_choices_from(options)
            job.proposals = self.proposals(job, job.options)
        self.metrics.review_approved()
        job.set_stage(JobStatus.QUEUED, "Queued for processing", 0.0)
        job.approved.set()
        return job

    # ------------------------------------------------------------------ proposals

    def proposals(self, job: Job, options: JobOptions) -> list[dict]:
        return [self._propose(source, options) for source in job.items]

    def _propose(self, source: SourceItem, options: JobOptions) -> dict:
        info = source.info
        native = "uploaded" if source.kind == "upload" else "native"
        proposal = {
            "source_index": source.index,
            "input_index": source.input_index,
            "input_kind": source.kind,
            "media_id": source.item.media_id,
            "name": source.download_name,
            "source": {
                "width": info.width,
                "height": info.height,
                "fps": round(info.fps, 3),
                "duration": round(info.duration, 3),
                "is_image": info.is_image,
                "has_audio": info.has_audio,
                "vcodec": info.vcodec,
            },
            "action": "keep",
            "target": {"width": info.width, "height": info.height, "fps": round(info.fps, 3)},
            "treatments": [native],
            "warnings": [],
        }
        try:
            if options.mode == "enhance":
                plan = make_plan(info, options.resolution, options.fps)
                limit = self.settings.max_duration_seconds
                if plan.is_noop:
                    proposal["warnings"].append(noop_message(source.item.media_id, info))
                elif not info.is_image and limit and info.duration > limit:
                    proposal["warnings"].append(
                        f"{info.duration:.0f}s exceeds the {limit}s enhancement limit; "
                        "only the original is provided."
                    )
                else:
                    try:
                        engine = (
                            "ffmpeg" if info.is_image else self._pick_engine(plan, options.engine)
                        )
                    except AIEngineError as exc:
                        engine = "ffmpeg"
                        proposal["warnings"].append(str(exc))
                    proposal.update(
                        action="process",
                        engine=engine,
                        target={
                            "width": plan.target_width,
                            "height": plan.target_height,
                            "fps": round(plan.target_fps, 3),
                        },
                        treatments=enhance_treatments(plan, engine, self.settings),
                    )
                    if plan.upscale and engine == "ffmpeg":
                        proposal["warnings"].append(
                            "Upscaling with ffmpeg makes the frame bigger and crisper, but it "
                            "can't recover detail that isn't in the source."
                        )
            elif options.mode == "standardize":
                plan = make_standardize_plan(info, options.standardize)
                proposal.update(
                    action="process",
                    target={
                        "width": plan.canvas_width,
                        "height": plan.canvas_height,
                        "fps": round(plan.target_fps, 3),
                    },
                    treatments=list(plan.treatments),
                    warnings=list(plan.warnings),
                )
        except PlanError as exc:
            proposal["warnings"].append(str(exc))
        return proposal

    # ------------------------------------------------------------------ execution

    async def _run(self, job: Job) -> None:
        try:
            if job.review:
                async with self._semaphore:
                    job.cancel.check()
                    await self._gather(job)
                job.proposals = self.proposals(job, job.options)
                job.set_stage(JobStatus.REVIEW, "Waiting for your review", 1.0)
                await job.approved.wait()
                job.cancel.check()
                async with self._semaphore:
                    job.cancel.check()
                    await self._process(job)
            else:
                async with self._semaphore:
                    job.cancel.check()
                    await self._gather(job)
                    await self._process(job)
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
                for stray in job.dir.rglob("*.part"):
                    stray.unlink(missing_ok=True)
            self.metrics.job_finished(
                job.options.mode,
                job.status.value,
                [
                    (i.target.platform if i.target else "upload", i.status == "ready")
                    for i in job.inputs
                    if i.status in {"ready", "failed"}
                ],
            )

    def _browser_login_allowed(self, job: Job) -> bool:
        mode = self.settings.auto_browser_cookies
        return mode == "always" or (mode == "local" and job.client_is_local)

    async def _browser_login(self, platform: Platform) -> tuple[CookieSource | None, str]:
        result = await asyncio.to_thread(discover_browser_session, self.settings, platform)
        source = result.session.as_source() if result.session else None
        return source, result.summary()

    async def _fetch(
        self,
        job: Job,
        target: MediaURL,
        cookies_text: str | None,
        workdir: Path,
        prefix: str = "",
        span: tuple[float, float] = (0.0, 1.0),
    ) -> tuple[object, CookieSource]:
        """Download with the configured credentials, falling back to the operator's own browser
        login (same-machine requests only) when the platform insists on a login."""
        settings = self.settings
        platform = target.platform_info
        cookie_source = resolve_cookie_source(settings, cookies_text, platform)
        browser_allowed = self._browser_login_allowed(job)
        offset, width = span

        def progress(fraction: float, label: str) -> None:
            job.set_progress(offset + width * fraction, f"{prefix}{label}")

        blocked_until = self._anonymous_blocked_until.get(platform.id, 0.0)
        skip_anonymous = target.requires_login or time.time() < blocked_until
        if cookie_source.kind == "none" and browser_allowed and skip_anonymous:
            # Stories never work anonymously, and a recent refusal means reels won't either.
            progress(0.0, f"Looking for your browser's {platform.name} login…")
            browser_source, _ = await self._browser_login(platform)
            cookie_source = browser_source or cookie_source

        loop = asyncio.get_running_loop()

        def report_download(fraction: float, label: str) -> None:
            loop.call_soon_threadsafe(progress, fraction, label)

        progress(0.0, f"Contacting {platform.name} ({cookie_source.describe()})…")
        try:
            result = await asyncio.to_thread(
                download, target, workdir, settings, cookie_source, job.cancel, report_download
            )
            return result, cookie_source
        except ExtractError as exc:
            if not (exc.login_required and cookie_source.kind == "none" and browser_allowed):
                raise
            progress(0.0, f"{platform.name} wants a login — checking your browsers…")
            browser_source, summary = await self._browser_login(platform)
            if browser_source is None:
                site = platform.cookie_domain.lstrip(".")
                raise ExtractError(
                    f"{platform.name} only shows this to logged-in users, and no {platform.name} "
                    f"login was found in your browsers ({summary}). Log in to {site} in one of "
                    "them and try again — the app picks the login up automatically — or paste a "
                    f"`{platform.login_cookie}` cookie under Advanced.",
                    "login",
                ) from exc
            progress(0.0, f"Retrying with {browser_source.describe()}…")
            result = await asyncio.to_thread(
                download,
                target,
                workdir,
                settings,
                browser_source,
                job.cancel,
                report_download,
            )
            self._anonymous_blocked_until[platform.id] = (
                time.time() + self.ANONYMOUS_BACKOFF_SECONDS
            )
            return result, browser_source

    async def _gather(self, job: Job) -> None:
        """Download links / adopt uploads and probe everything. Multi-input jobs keep going when
        one input fails; a single-input job fails exactly as in v1."""
        if job.target is not None:
            first = f"Contacting {job.target.platform_info.name}…"
        else:
            first = "Reading your files…"
        job.set_stage(JobStatus.DOWNLOADING, first, 0.0)
        count = len(job.inputs)
        try:
            for job_input in job.inputs:
                job.current_item = job_input.index + 1
                prefix = f"[{job_input.index + 1}/{count}] " if job.multi else ""
                span = (job_input.index / count, 1 / count)
                job_input.status = "fetching"
                try:
                    if job_input.target is not None:
                        result, cookie_source = await self._fetch(
                            job,
                            job_input.target,
                            job.options.cookies if job_input.use_cookies else None,
                            job_input.dir,
                            prefix,
                            span,
                        )
                        job_input.auth = cookie_source.describe()
                        job.auth = job.auth or job_input.auth
                        items = list(result.items)
                    else:
                        job.set_progress(span[0], f"{prefix}Reading {job_input.label}")
                        items = [self._upload_item(job_input)]
                    job.cancel.check()
                    for item in items:
                        await self._add_source(job, job_input, item)
                    job_input.item_count = len(items)
                    job_input.status = "ready"
                except (ExtractError, FFmpegError, CookieError, InvalidURL, PlanError) as exc:
                    if not job.multi:
                        raise
                    log.info("job %s input %d failed: %s", job.id, job_input.index, exc)
                    job_input.status = "failed"
                    job_input.error = str(exc)
                    job.failures.append(
                        {
                            "input_index": job_input.index,
                            "label": job_input.label,
                            "error": str(exc),
                        }
                    )
                    job.warnings.append(f"{job_input.label}: {exc}")
        finally:
            job.options.cookies = None
        if not job.items:
            raise ExtractError("None of the links or files could be fetched; see the details.")
        job.cancel.check()
        job.set_progress(1.0, "Download complete")

    def _upload_item(self, job_input: JobInput) -> DownloadedItem:
        upload = job_input.upload
        assert upload is not None
        info = upload.info
        return DownloadedItem(
            upload.path,
            safe_filename(upload.stem, "upload"),
            upload.filename,
            None,
            None,
            info.width,
            info.height,
            info.fps,
            info.duration,
            "upload",
            "",
            info.is_image,
        )

    async def _add_source(self, job: Job, job_input: JobInput, item: DownloadedItem) -> None:
        info = await ffprobe(item.path, self.settings)
        if job_input.kind == "upload":
            base_name = safe_filename(Path(job_input.label).stem, "upload")
            download_name = job_input.label
        else:
            base_name = f"{safe_filename(item.display_name)}_{safe_filename(item.media_id)}"
            download_name = f"{base_name}.{item.ext or 'mp4'}"
        source = SourceItem(
            index=len(job.items),
            input_index=job_input.index,
            kind=job_input.kind,
            item=item,
            info=info,
            base_name=base_name,
            download_name=download_name,
        )
        job.items.append(source)
        job.sources.append(
            {
                "media_id": item.media_id,
                "title": item.title,
                "uploader": item.uploader,
                "channel": item.channel,
                "format_id": item.format_id,
                "input_index": job_input.index,
                "input_kind": job_input.kind,
                **info.to_dict(),
            }
        )
        job.updated_at = time.time()

    async def _process(self, job: Job) -> None:
        outputs: list[OutputFile] = []
        mode = job.options.mode
        count = len(job.items)
        for position, source in enumerate(job.items, start=1):
            job.current_item = source.input_index + 1
            processed: OutputFile | None = None
            failed = False
            try:
                if mode == "enhance":
                    processed = await self._enhance_item(
                        job,
                        source.item,
                        source.info,
                        source.base_name,
                        position,
                        count,
                        out_dir=source.item.path.parent,
                    )
                    if processed is not None:
                        plan = make_plan(source.info, job.options.resolution, job.options.fps)
                        engine = (processed.engine or "ffmpeg").split("/")[0]
                        processed.treatments = enhance_treatments(plan, engine, self.settings)
                elif mode == "standardize":
                    processed = await self._standardize_item(job, source, position, count)
            except (PlanError, FFmpegError, AIEngineError) as exc:
                if not job.multi:
                    raise
                failed = True
                log.info("job %s item %s failed: %s", job.id, source.item.media_id, exc)
                job.failures.append(
                    {
                        "input_index": source.input_index,
                        "label": source.download_name,
                        "error": str(exc),
                    }
                )
                job.warnings.append(f"{source.download_name}: {exc}")
            if processed is not None:
                processed.index = len(outputs)
                processed.source_index = source.index
                processed.input_index = source.input_index
                outputs.append(processed)
            # Uploaded originals are only handed back when nothing else came out of them.
            if source.kind == "link" or (processed is None and not failed):
                outputs.append(
                    OutputFile(
                        index=len(outputs),
                        path=source.item.path,
                        download_name=source.download_name,
                        kind="original",
                        media_id=source.item.media_id,
                        info=source.info,
                        treatments=["uploaded" if source.kind == "upload" else "native"],
                        source_index=source.index,
                        input_index=source.input_index,
                    )
                )
        if not outputs:
            raise FFmpegError("Nothing could be processed; see the details.")
        job.outputs = outputs
        job.finish(JobStatus.DONE, "Ready")

    async def _standardize_item(
        self, job: Job, source: SourceItem, position: int, count: int
    ) -> OutputFile:
        settings = self.settings
        plan = make_standardize_plan(source.info, job.options.standardize)
        job.plan = plan.to_dict()
        for warning in plan.warnings:
            message = f"{source.item.media_id}: {warning}"
            if message not in job.warnings:
                job.warnings.append(message)
        prefix = f"[{position}/{count}] " if count > 1 else ""
        out_dir = source.item.path.parent
        ext = "png" if source.info.is_image else "mp4"
        dst = out_dir / f"out-{safe_filename(source.item.media_id)}-{plan.label}.{ext}"
        dst.unlink(missing_ok=True)
        if source.info.is_image:
            encoder = ""
            what = f"{plan.canvas_width}x{plan.canvas_height}"
        else:
            encoder = await choose_encoder(settings)
            what = f"{plan.canvas_width}x{plan.canvas_height} @ {plan.target_fps:.0f} fps"
        job.set_stage(JobStatus.ENHANCING, f"{prefix}Standardizing to {what}", 0.0)

        def report(fraction: float) -> None:
            job.set_progress(fraction)

        await standardize_media(
            plan,
            source.item.path,
            dst,
            encoder=encoder,
            settings=settings,
            cancel=job.cancel,
            on_progress=report,
        )
        job.cancel.check()
        out_info = await ffprobe(dst, settings)
        return OutputFile(
            index=0,
            path=dst,
            download_name=f"{source.base_name}_{plan.label}.{ext}",
            kind="standardized",
            media_id=source.item.media_id,
            info=out_info,
            engine="ffmpeg/png" if source.info.is_image else f"ffmpeg/{encoder}",
            treatments=list(plan.treatments),
        )

    async def _enhance_item(
        self,
        job: Job,
        item: DownloadedItem,
        info: VideoInfo,
        base_name: str,
        position: int,
        count: int,
        out_dir: Path | None = None,
    ) -> OutputFile | None:
        settings = self.settings
        out_dir = out_dir or job.dir
        plan = make_plan(info, job.options.resolution, job.options.fps)
        job.plan = plan.to_dict()
        if plan.is_noop:
            job.warnings.append(noop_message(item.media_id, info))
            return None
        if info.is_image:
            job.set_stage(
                JobStatus.ENHANCING,
                f"Upscaling image to {plan.target_width}x{plan.target_height}",
                0.0,
            )
            dst = out_dir / f"out-{safe_filename(item.media_id)}-{plan.label}.png"
            dst.unlink(missing_ok=True)
            await enhance_image(plan, item.path, dst, settings=settings, cancel=job.cancel)
            job.cancel.check()
            out_info = await ffprobe(dst, settings)
            return OutputFile(
                index=0,
                path=dst,
                download_name=f"{base_name}_{plan.label}.png",
                kind="enhanced",
                media_id=item.media_id,
                info=out_info,
                engine="ffmpeg/lanczos+cas",
            )
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

        dst = out_dir / f"out-{safe_filename(item.media_id)}-{plan.label}.mp4"
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
