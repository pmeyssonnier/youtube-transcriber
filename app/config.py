from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from dotenv import load_dotenv, set_key


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
load_dotenv(ENV_FILE, override=False)

# Les traitements, transcriptions et listes d'élus peuvent être rangés hors du dossier de l'application
# (par exemple hors d'un dossier synchronisé) avec la variable TRANSCRIBER_DATA_DIR, définie dans
# l'environnement ou dans le fichier .env.
DATA_DIR = Path(os.getenv("TRANSCRIBER_DATA_DIR", "").strip() or PROJECT_ROOT / "data").expanduser()
JOBS_DIR = DATA_DIR / "jobs"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def initialize_directories() -> None:
    """Create the local application directories when missing."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    load_dotenv(ENV_FILE, override=False)


def get_openai_api_key() -> str | None:
    """Return the configured API key without exposing it to the browser."""
    load_dotenv(ENV_FILE, override=True)
    value = os.getenv("OPENAI_API_KEY", "").strip()
    return value or None


def save_openai_api_key(api_key: str) -> None:
    """Persist the API key in the application's local .env file."""
    cleaned = api_key.strip()
    if not cleaned.startswith("sk-") or len(cleaned) < 20:
        raise ValueError("La clé API ne semble pas valide.")

    if not ENV_FILE.exists():
        ENV_FILE.touch(mode=0o600)
    set_key(str(ENV_FILE), "OPENAI_API_KEY", cleaned, quote_mode="always")
    try:
        ENV_FILE.chmod(0o600)
    except OSError:
        pass
    os.environ["OPENAI_API_KEY"] = cleaned


# Dossiers dont le contenu part dans le cloud : .env (clé API) et transcriptions y seraient copiés.
SYNC_FOLDER_MARKERS = (
    ("onedrive", "OneDrive"),
    ("dropbox", "Dropbox"),
    ("google drive", "Google Drive"),
    ("googledrive", "Google Drive"),
    ("icloud", "iCloud"),
    ("nextcloud", "Nextcloud"),
    ("owncloud", "ownCloud"),
    ("pcloud", "pCloud"),
    ("sync.com", "Sync.com"),
    ("mega", "MEGA"),
)


def detect_synced_folder(*paths: Path) -> str | None:
    """Name of the cloud-sync service whose folder holds one of the paths, if any."""
    for path in paths:
        for part in re.split(r"[\\/]+", str(path)):  # séparateurs Windows et Linux
            lowered = part.lower()
            for marker, name in SYNC_FOLDER_MARKERS:
                if lowered == marker or lowered.startswith(marker + " ") or lowered.startswith(marker + "-"):
                    return name
    return None


def synced_folder_name() -> str | None:
    return detect_synced_folder(PROJECT_ROOT, DATA_DIR)


def dependency_status() -> dict[str, bool]:
    """Return availability of external executables used by the pipeline."""
    return {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "deno": shutil.which("deno") is not None,
    }

