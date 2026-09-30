from __future__ import annotations

import subprocess
import sys
import threading
from importlib import metadata


PACKAGE_SPEC = "yt-dlp[default]"
UPDATE_TIMEOUT_SECONDS = 300

_update_lock = threading.Lock()


class YtDlpUpdateError(RuntimeError):
    """Error with a message suitable for the local user interface."""


def ytdlp_version() -> str | None:
    """Return the installed yt-dlp version, read from disk on every call."""
    try:
        return metadata.version("yt-dlp")
    except metadata.PackageNotFoundError:
        return None


def update_ytdlp(include_dev: bool = False) -> dict[str, str | bool | None]:
    """Upgrade yt-dlp in the application's own virtual environment.

    Only stable releases are considered unless ``include_dev`` is set, which allows the
    (newer, less tested) development builds that YouTube fixes often ship in first.

    yt-dlp only runs in short-lived subprocesses (see the pipeline), so the
    files are never locked by the web server and the new version applies to
    the next job without a restart.
    """
    if not _update_lock.acquire(blocking=False):
        raise YtDlpUpdateError("Une mise à jour de yt-dlp est déjà en cours.")
    try:
        previous = ytdlp_version()
        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
                    *(["--pre"] if include_dev else []),
                    PACKAGE_SPEC,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=UPDATE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise YtDlpUpdateError("La mise à jour a dépassé le délai autorisé. Vérifiez votre connexion.") from exc
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()[-800:]
            raise YtDlpUpdateError(f"La mise à jour de yt-dlp a échoué.\n{details}".strip())
        current = ytdlp_version()
        return {
            "previous_version": previous,
            "version": current,
            "updated": current != previous,
            "development_build": bool(current and ("dev" in current)),
        }
    finally:
        _update_lock.release()
