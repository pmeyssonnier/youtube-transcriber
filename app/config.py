from __future__ import annotations

import os
import shutil
from pathlib import Path

from dotenv import load_dotenv, set_key


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
JOBS_DIR = DATA_DIR / "jobs"
STATIC_DIR = Path(__file__).resolve().parent / "static"
ENV_FILE = PROJECT_ROOT / ".env"


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


def dependency_status() -> dict[str, bool]:
    """Return availability of external executables used by the pipeline."""
    return {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "deno": shutil.which("deno") is not None,
    }

