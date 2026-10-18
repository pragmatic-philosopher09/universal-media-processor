"""Local file uploads for the MediaFlow workspace.

Files are streamed to disk as the raw request body (no multipart dependency), size-capped while
streaming, validated with ffprobe and kept until a job claims them or they expire. Uploads
belong to the client IP that sent them; nobody else can attach them to a job.
"""

from __future__ import annotations

import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .media import FFmpegError, VideoInfo, ffprobe

ALLOWED_UPLOAD_EXTENSIONS = {
    "mp4",
    "m4v",
    "mov",
    "mkv",
    "webm",
    "avi",
    "png",
    "jpg",
    "jpeg",
    "webp",
    "gif",
}


class UploadError(ValueError):
    pass


class UploadTooLarge(UploadError):
    pass


class UploadNotFound(KeyError):
    pass


def clean_filename(name: str) -> str:
    base = Path(name or "").name
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" ._")
    return cleaned[:120] or "upload"


@dataclass
class Upload:
    id: str
    filename: str
    path: Path
    size: int
    client_ip: str
    info: VideoInfo
    created_at: float = field(default_factory=time.time)

    @property
    def ext(self) -> str:
        return self.path.suffix.lstrip(".").lower()

    @property
    def stem(self) -> str:
        return Path(self.filename).stem or "upload"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "filename": self.filename,
            "size": self.size,
            "created_at": self.created_at,
            "info": self.info.to_dict(),
        }


class UploadStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.uploads: dict[str, Upload] = {}

    @property
    def dir(self) -> Path:
        return self.settings.uploads_dir

    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        for stale in self.dir.iterdir():
            if stale.is_dir() and stale.name not in self.uploads:
                shutil.rmtree(stale, ignore_errors=True)

    def count_for(self, client_ip: str) -> int:
        return sum(1 for upload in self.uploads.values() if upload.client_ip == client_ip)

    async def save(
        self,
        chunks: AsyncIterator[bytes],
        filename: str,
        client_ip: str,
        *,
        declared_size: int | None = None,
    ) -> Upload:
        if not self.settings.uploads_enabled:
            raise UploadError("File uploads are disabled on this server.")
        name = clean_filename(filename)
        ext = Path(name).suffix.lstrip(".").lower()
        if ext not in ALLOWED_UPLOAD_EXTENSIONS:
            raise UploadError(
                f"Unsupported file type '.{ext or '?'}'. Upload a video "
                "(mp4, mov, mkv, webm, avi, m4v) or an image (png, jpg, webp, gif)."
            )
        limit = self.settings.max_upload_mb * 1024 * 1024
        if declared_size is not None and declared_size > limit:
            raise UploadTooLarge(f"File is larger than the {self.settings.max_upload_mb} MB limit.")
        if self.count_for(client_ip) >= self.settings.max_uploads_per_ip:
            raise UploadError(
                f"You already have {self.settings.max_uploads_per_ip} files waiting. "
                "Process or remove some first."
            )

        upload_id = uuid.uuid4().hex[:12]
        folder = self.dir / upload_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"source.{ext}"
        size = 0
        try:
            with path.open("wb") as handle:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > limit:
                        raise UploadTooLarge(
                            f"File is larger than the {self.settings.max_upload_mb} MB limit."
                        )
                    handle.write(chunk)
            if size == 0:
                raise UploadError("The uploaded file is empty.")
            try:
                info = await ffprobe(path, self.settings)
            except FFmpegError as exc:
                raise UploadError(
                    f"{name} isn't a readable video or image file (no picture stream found)."
                ) from exc
            if info.width <= 0 or info.height <= 0:
                raise UploadError(f"{name} has no usable picture dimensions.")
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

        upload = Upload(
            id=upload_id, filename=name, path=path, size=size, client_ip=client_ip, info=info
        )
        self.uploads[upload_id] = upload
        return upload

    def get(self, upload_id: str, client_ip: str) -> Upload:
        upload = self.uploads.get(upload_id)
        if upload is None or upload.client_ip != client_ip:
            raise UploadNotFound(upload_id)
        return upload

    def claim(self, upload_id: str, client_ip: str, destination: Path) -> Upload:
        """Move the file into a job folder; the upload is no longer listed afterwards."""
        upload = self.get(upload_id, client_ip)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(upload.path), destination)
        shutil.rmtree(upload.path.parent, ignore_errors=True)
        self.uploads.pop(upload_id, None)
        upload.path = destination
        return upload

    def delete(self, upload_id: str, client_ip: str) -> None:
        upload = self.get(upload_id, client_ip)
        self.uploads.pop(upload_id, None)
        shutil.rmtree(upload.path.parent, ignore_errors=True)

    def sweep(self, now: float | None = None) -> int:
        now = now or time.time()
        ttl = self.settings.job_ttl_minutes * 60
        removed = 0
        for upload in list(self.uploads.values()):
            if now - upload.created_at > ttl:
                self.uploads.pop(upload.id, None)
                shutil.rmtree(upload.path.parent, ignore_errors=True)
                removed += 1
        return removed
