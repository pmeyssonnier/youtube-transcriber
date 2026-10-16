from __future__ import annotations

from datetime import date
from typing import Any

from .people import parse_video_date

SOURCE_MANUAL = "saisie"
SOURCE_YOUTUBE = "publication_youtube"
SOURCE_NONE = "inconnue"

SOURCE_LABELS = {
    SOURCE_MANUAL: "date de séance saisie",
    SOURCE_YOUTUBE: "date de publication YouTube (une rediffusion peut être postérieure à la séance)",
    SOURCE_NONE: "aucune date connue : l'année en cours est utilisée",
}


def reference_date(job: dict[str, Any]) -> tuple[date | None, str]:
    """Date used for the mandates: the session date typed by the user, else YouTube's publication date."""
    session = parse_video_date(job.get("session_date"))
    if session is not None:
        return session, SOURCE_MANUAL
    published = parse_video_date(job.get("video_date"))
    if published is not None:
        return published, SOURCE_YOUTUBE
    return None, SOURCE_NONE


def date_metadata(job: dict[str, Any]) -> dict[str, Any]:
    used, source = reference_date(job)
    return {
        "session_date": job.get("session_date"),
        "publication_date": job.get("video_date"),
        "reference_date_used": used.isoformat() if used else None,
        "reference_date_source": source,
    }
