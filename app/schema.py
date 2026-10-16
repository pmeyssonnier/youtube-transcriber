from __future__ import annotations

from datetime import UTC, datetime

from .constants import APP_NAME, APP_VERSION

# Version du format de transcription.json. Les champs existants ne changent jamais de sens dans une
# même version majeure ; des champs peuvent être ajoutés. Voir docs/format-json.md.
SCHEMA_VERSION = "1.0"


def generator_block() -> dict[str, str]:
    return {"name": APP_NAME, "version": APP_VERSION, "generated_at": datetime.now(UTC).isoformat()}
