"""Keep third-party tool and service names out of text shown in the UI.

Hand-written messages already use neutral wording; this is the safety net for text that
embeds tool output (stderr tails, upstream error messages). Server logs keep the details.
"""

from __future__ import annotations

import re

_SENTENCE_DROP = re.compile(
    r"[^.;\n]*(?:github\.com/yt-dlp|yt-dlp\s+-U|--cookies|--update|bug report)[^.;\n]*[.;]?\s*",
    re.IGNORECASE,
)
_NAMES = [
    (r"\b(?:video2x|rife-ncnn-vulkan|realesrgan-ncnn-vulkan)(?:\s*\([^)]*\))?", "the AI engine"),
    (r"\bRIFE\s*\+\s*Real-?ESRGAN\b", "the AI engine"),
    (r"\bReal-?ESRGAN\b|\bRIFE\b", "the AI engine"),
    (r"\bFastVideoSave\b", "the backup service"),
    (r"\bSnapSave\b", "the audio source"),
    (r"\byt-dlp\b|\byoutube-dl\b", "the downloader"),
    (r"\bffprobe\b", "the media reader"),
    (r"\bffmpeg\b", "the video processor"),
    (r"\b(?:Playwright|Chromium)\b", "the server browser"),
]
_PATTERNS = [(re.compile(pattern, re.IGNORECASE), name) for pattern, name in _NAMES]


def _replacer(name: str):
    def replace(match: re.Match) -> str:
        before = match.string[: match.start()].rstrip()
        return name[0].upper() + name[1:] if not before or before[-1] in ".!?:" else name

    return replace


def scrub(text: str | None) -> str | None:
    if not text:
        return text
    cleaned = _SENTENCE_DROP.sub("", text).strip().rstrip(";,").rstrip() or text
    for pattern, name in _PATTERNS:
        cleaned = pattern.sub(_replacer(name), cleaned)
    return cleaned


def scrub_list(values: list[str]) -> list[str]:
    return [scrub(value) or "" for value in values]


def scrub_records(records: list[dict] | None, *keys: str) -> list[dict] | None:
    if records is None:
        return None
    out = []
    for record in records:
        record = dict(record)
        for key in keys:
            value = record.get(key)
            if isinstance(value, str):
                record[key] = scrub(value)
            elif isinstance(value, list):
                record[key] = [scrub(v) if isinstance(v, str) else v for v in value]
        out.append(record)
    return out
