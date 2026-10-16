from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from dotenv import dotenv_values, load_dotenv, set_key


PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Ancien emplacement de la clé : le fichier .env du dossier de l'application, qui peut se trouver dans un
# dossier synchronisé (OneDrive...). Il n'est plus qu'une source de repli, vidée de la clé à la migration.
LEGACY_ENV_FILE = PROJECT_ROOT / ".env"


def default_env_file(system: str | None = None) -> Path:
    """Where the API key is stored: a per-user folder that cloud sync tools do not copy.

    Windows: %LOCALAPPDATA%\\youtube-transcriber\\.env (not part of OneDrive, unlike Documents or Desktop).
    Elsewhere: $XDG_CONFIG_HOME/youtube-transcriber/.env or ~/.config/youtube-transcriber/.env.
    ``TRANSCRIBER_ENV_FILE`` (real environment variable) forces another path.
    """
    forced = os.getenv("TRANSCRIBER_ENV_FILE", "").strip()
    if forced:
        return Path(forced).expanduser()
    if (system or os.name) == "nt":
        base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "youtube-transcriber" / ".env"


ENV_FILE = default_env_file()
load_dotenv(ENV_FILE, override=False)
load_dotenv(LEGACY_ENV_FILE, override=False)  # TRANSCRIBER_DATA_DIR peut encore s'y trouver

# Les traitements, transcriptions et listes d'élus peuvent être rangés hors du dossier de l'application
# (par exemple hors d'un dossier synchronisé) avec la variable TRANSCRIBER_DATA_DIR, définie dans
# l'environnement ou dans un fichier .env.
DATA_DIR = Path(os.getenv("TRANSCRIBER_DATA_DIR", "").strip() or PROJECT_ROOT / "data").expanduser()
JOBS_DIR = DATA_DIR / "jobs"
STATIC_DIR = Path(__file__).resolve().parent / "static"

KEY_NAME = "OPENAI_API_KEY"
_KEY_LINE = re.compile(r"^\s*(export\s+)?" + KEY_NAME + r"\s*=")
# Résultat de la migration de la clé pendant ce lancement (affiché une fois par l'interface).
migration: dict[str, object] = {"moved": False, "error": None}


def _read_key(path: Path) -> str:
    try:
        return str((dotenv_values(path).get(KEY_NAME) or "")).strip() if path.is_file() else ""
    except OSError:
        return ""


def _write_key(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch(mode=0o600)
    set_key(str(path), KEY_NAME, value, quote_mode="always")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _remove_key(path: Path) -> None:
    """Delete the key line from a .env file, keeping every other line; atomic."""
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not _KEY_LINE.match(line)]
    if len(kept) == len(lines):
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(kept), encoding="utf-8")
    temporary.replace(path)


def migrate_legacy_key() -> bool:
    """Move the key from the old .env (application folder) to the per-user file.

    The old file is emptied of the key only after the new file is read back with the same value, so the
    key is never lost. Nothing happens when there is no key in the old file. Returns True when moved.
    """
    try:
        if LEGACY_ENV_FILE.resolve() == ENV_FILE.resolve():
            return False
        legacy_key = _read_key(LEGACY_ENV_FILE)
        if not legacy_key:
            return False
        if not _read_key(ENV_FILE):
            _write_key(ENV_FILE, legacy_key)
            if _read_key(ENV_FILE) != legacy_key:
                raise OSError("la relecture de la clé enregistrée ne correspond pas")
        _remove_key(LEGACY_ENV_FILE)
    except (OSError, ValueError) as exc:
        migration["error"] = str(exc)
        return False
    migration["moved"] = True
    migration["error"] = None
    return True


def initialize_directories() -> None:
    """Create the local application directories when missing and move an old key out of the app folder."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_key()
    load_dotenv(ENV_FILE, override=False)


def get_openai_api_key() -> str | None:
    """Return the configured API key without exposing it to the browser.

    Order: the per-user file, then the old .env of the application folder (not migrated yet or migration
    impossible), then the real environment variable.
    """
    value = _read_key(ENV_FILE) or _read_key(LEGACY_ENV_FILE) or os.getenv(KEY_NAME, "").strip()
    return value or None


def save_openai_api_key(api_key: str) -> None:
    """Persist the API key in the per-user .env file (and remove it from the old one)."""
    cleaned = api_key.strip()
    if not cleaned.startswith("sk-") or len(cleaned) < 20:
        raise ValueError("La clé API ne semble pas valide.")
    try:
        _write_key(ENV_FILE, cleaned)
    except OSError as exc:
        raise ValueError(f"Impossible d'enregistrer la clé dans {ENV_FILE} : {exc}") from exc
    try:
        _remove_key(LEGACY_ENV_FILE)
    except OSError:
        pass  # sans conséquence : la clé du nouveau fichier est prioritaire
    os.environ[KEY_NAME] = cleaned


def key_sync_folder() -> str | None:
    """Cloud-sync service holding a copy of the key, if any (the per-user file, or an old .env left behind)."""
    found = detect_synced_folder(ENV_FILE)
    if found:
        return found
    if _read_key(LEGACY_ENV_FILE):
        return detect_synced_folder(PROJECT_ROOT)
    return None


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

