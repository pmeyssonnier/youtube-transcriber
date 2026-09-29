from __future__ import annotations


APP_NAME = "youtube-transcriber"
APP_VERSION = "1.5.0"

DIARIZATION_MODEL = "gpt-4o-transcribe-diarize"
STANDARD_MODEL = "whisper-1"
# Le modèle de diarisation refuse les fichiers de plus de ~1400 s (~23 min).
DIARIZATION_MAX_CHUNK_MINUTES = 20

ACTIVE_JOB_STATUSES = frozenset({"queued", "downloading", "preparing", "transcribing", "exporting"})
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed"})

LONG_VIDEO_SECONDS = 2 * 60 * 60
INSPECTION_TTL_SECONDS = 30 * 60
MAX_AUDIO_FILE_BYTES = 24 * 1024 * 1024

ALLOWED_COOKIE_BROWSERS = frozenset({"chrome", "edge", "firefox", "brave"})
