from __future__ import annotations

from typing import Any

from .constants import DIARIZATION_MODEL, STANDARD_MODEL
from .dates import date_metadata


def job_metadata(job: dict[str, Any], *, duration: float | None, model: str | None = None) -> dict[str, Any]:
    """The ``metadata`` block of transcription.json, identical for the first export and every rewrite."""
    diarize = bool(job.get("diarize"))
    return {
        "title": job.get("title"),
        "source_url": job.get("url"),
        "video_id": job.get("video_identity"),
        "duration_seconds": duration,
        "transcribed_from": job.get("start_seconds"),
        "transcribed_to": job.get("end_seconds"),
        "model": model or (DIARIZATION_MODEL if diarize else STANDARD_MODEL),
        "language": job.get("language"),
        "diarized": diarize,
        "speaker_linking": job.get("speaker_linking", "off") if diarize else "off",
        "speaker_labels_need_review": diarize,
        "stitch_uncertain_segments": int(job.get("stitch_uncertain") or 0),
        **date_metadata(job),
    }

