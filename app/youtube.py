from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from .constants import ALLOWED_COOKIE_BROWSERS


VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


def youtube_video_identity(value: str) -> str | None:
    """Extract a YouTube video id from supported single-video URL formats."""
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    video_id = ""

    if parsed.scheme not in {"http", "https"}:
        return None
    if host == "youtu.be" and path_parts:
        video_id = path_parts[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path.rstrip("/") == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(path_parts) == 2 and path_parts[0] in {"live", "shorts", "embed"}:
            video_id = path_parts[1]

    return video_id if VIDEO_ID_PATTERN.fullmatch(video_id) else None


def validate_youtube_url(value: str) -> str:
    """Accept only a URL that points to exactly one YouTube video."""
    cleaned = value.strip()
    if not youtube_video_identity(cleaned):
        raise ValueError(
            "Indiquez un lien vers une seule vidéo YouTube. "
            "Les playlists, chaînes et pages de recherche ne sont pas acceptées."
        )
    return cleaned


def validate_cookie_browser(value: str | None) -> str | None:
    """Validate the optional browser whose local cookies yt-dlp may read."""
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return None
    if cleaned not in ALLOWED_COOKIE_BROWSERS:
        raise ValueError("Navigateur de cookies non pris en charge.")
    return cleaned
