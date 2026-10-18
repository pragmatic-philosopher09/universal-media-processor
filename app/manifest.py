"""Job manifests (JSON / CSV) and the "download all" ZIP archive.

The manifest is the "what happened to my media" record: every input, every probed source, and
every output with its treatment labels and checksum, so a batch can be audited or re-imported.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

from .jobs import Job, OutputFile

MANIFEST_VERSION = 1
GENERATOR = "MediaFlow (Media Downloader 4K60)"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_checksums(job: Job) -> None:
    """Blocking: hash outputs once (call from a worker thread)."""
    for output in job.outputs:
        if output.sha256 is None and output.path.exists():
            output.sha256 = sha256_of(output.path)


def primary_outputs(job: Job) -> list[OutputFile]:
    """One file per source: the processed result when there is one, else the original."""
    chosen: dict[int, OutputFile] = {}
    for output in job.outputs:
        key = output.source_index if output.source_index is not None else -1 - output.index
        current = chosen.get(key)
        if current is None or (current.kind == "original" and output.kind != "original"):
            chosen[key] = output
    return sorted(chosen.values(), key=lambda o: o.index)


def build_manifest(job: Job) -> dict:
    data = job.to_dict()
    primary = {o.index for o in primary_outputs(job)}
    outputs = []
    for output in job.outputs:
        entry = output.to_dict(job.id)
        entry["sha256"] = output.sha256
        entry["primary"] = output.index in primary
        outputs.append(entry)
    return {
        "manifest_version": MANIFEST_VERSION,
        "generator": GENERATOR,
        "generated_at": time.time(),
        "job": {
            "id": job.id,
            "status": data["status"],
            "created_at": job.created_at,
            "finished_at": job.finished_at,
            "elapsed_seconds": data["elapsed"],
            "options": data["options"],
            "reviewed": job.review,
        },
        "inputs": data["inputs"],
        "sources": job.sources,
        "outputs": outputs,
        "warnings": job.warnings,
        "failures": job.failures,
    }


CSV_COLUMNS = [
    "index",
    "primary",
    "kind",
    "download_name",
    "input_index",
    "input",
    "source_index",
    "width",
    "height",
    "fps",
    "duration",
    "vcodec",
    "size",
    "engine",
    "treatments",
    "sha256",
]


def manifest_csv(manifest: dict) -> str:
    inputs = {i["index"]: i["label"] for i in manifest["inputs"]}
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for output in manifest["outputs"]:
        row = dict(output)
        row["input"] = inputs.get(output.get("input_index"), "")
        row["treatments"] = ";".join(output.get("treatments") or [])
        writer.writerow(row)
    return buffer.getvalue()


def _unique_name(name: str, used: set[str]) -> str:
    candidate = name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    counter = 2
    while candidate.lower() in used:
        candidate = f"{stem}-{counter}.{ext}" if ext else f"{stem}-{counter}"
        counter += 1
    used.add(candidate.lower())
    return candidate


def build_archive(job: Job) -> Path:
    """Blocking: ZIP the primary outputs plus manifests (stored, not deflated: media is already
    compressed). The archive is cached in the job folder."""
    archive = job.dir / f"mediaflow-{job.id}.zip"
    if archive.exists():
        return archive
    ensure_checksums(job)
    manifest = build_manifest(job)
    partial = archive.with_suffix(".zip.part")
    used: set[str] = {"manifest.json", "manifest.csv"}
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for output in primary_outputs(job):
            if output.path.exists():
                zf.write(output.path, arcname=_unique_name(output.download_name, used))
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("manifest.csv", manifest_csv(manifest))
    partial.replace(archive)
    return archive
