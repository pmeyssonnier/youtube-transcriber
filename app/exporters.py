from __future__ import annotations

import json
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any

from .schema import SCHEMA_VERSION, generator_block


def clock(seconds: float, milliseconds: bool = False) -> str:
    """Format seconds as a human-readable timestamp."""
    total_ms = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, ms = divmod(remainder, 1000)
    if milliseconds:
        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{ms:03d}"
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"


def _speaker(segment: dict[str, Any], names: dict[str, str]) -> str:
    raw = segment.get("speaker", "Intervenant")
    return clean_inline_text(names.get(raw, raw)) or "Intervenant"


def clean_inline_text(value: Any) -> str:
    """Remove line breaks and control characters from labels and titles."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return " ".join(cleaned.split())


def safe_filename_stem(value: Any, fallback: str = "transcription") -> str:
    """Create a short Windows-compatible download filename stem."""
    normalized = unicodedata.normalize("NFKD", clean_inline_text(value))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_value).strip("-._")
    return (cleaned[:80] or fallback).lower()


def _markdown_escape(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", value)


def write_exports(
    output_dir: Path,
    metadata: dict[str, Any],
    segments: list[dict[str, Any]],
    speaker_names: dict[str, str],
) -> list[dict[str, str]]:
    """Write JSON, Markdown, TXT, SRT and VTT exports."""
    output_dir.mkdir(parents=True, exist_ok=True)
    base = "transcription"
    files: list[dict[str, str]] = []

    structured = {
        "schema_version": SCHEMA_VERSION,
        "generator": generator_block(),
        "metadata": metadata,
        "speakers": speaker_names,
        "segments": [
            {
                # « id » et « speaker_id » restent stables quand on renomme les intervenants.
                "id": number,
                **segment,
                "speaker_id": str(segment.get("speaker", "Intervenant")),
                "speaker_name": _speaker(segment, speaker_names),
            }
            for number, segment in enumerate(segments, start=1)
        ],
    }
    contents: dict[str, str] = {f"{base}.json": json.dumps(structured, ensure_ascii=False, indent=2)}
    files.append({"name": f"{base}.json", "label": "JSON structuré"})

    title = clean_inline_text(metadata.get("title") or "Transcription")
    markdown_lines = [
        f"# {_markdown_escape(title)}",
        "",
        f"Source : {metadata.get('source_url', '')}",
        "",
    ]
    text_lines: list[str] = []
    srt_lines: list[str] = []
    vtt_lines = ["WEBVTT", ""]

    for index, segment in enumerate(segments, start=1):
        start = float(segment.get("start", 0))
        end = max(start + 0.2, float(segment.get("end", start + 0.2)))
        speaker = _speaker(segment, speaker_names)
        text = str(segment.get("text", "")).replace("\x00", "").replace("\r\n", "\n").strip()
        if not text:
            continue

        markdown_lines.extend([f"## {clock(start)} — {_markdown_escape(speaker)}", "", text, ""])
        text_lines.append(f"[{clock(start)}] {speaker} : {text}")
        subtitle_text = f"[{speaker}] {text}"
        srt_lines.extend(
            [
                str(index),
                f"{clock(start, True)} --> {clock(end, True)}",
                subtitle_text,
                "",
            ]
        )
        vtt_lines.extend(
            [
                f"{clock(start, True).replace(',', '.')} --> {clock(end, True).replace(',', '.')}",
                subtitle_text,
                "",
            ]
        )

    for suffix, content, label in (
        ("md", "\n".join(markdown_lines), "Markdown lisible"),
        ("txt", "\n".join(text_lines) + "\n", "Texte"),
        ("srt", "\n".join(srt_lines), "Sous-titres SRT"),
        ("vtt", "\n".join(vtt_lines), "Sous-titres VTT"),
    ):
        contents[f"{base}.{suffix}"] = content
        files.append({"name": f"{base}.{suffix}", "label": label})

    write_files_together(output_dir, contents)
    return files


def write_files_together(directory: Path, contents: dict[str, str]) -> None:
    """Write several files so that a failure never leaves a mix of old and new versions.

    1. Everything is first written to temporary files next to the final ones (an error here changes
       nothing).
    2. Each existing file is copied to a backup, then replaced atomically by its new version.
    3. If a replacement fails (a file open in another program, a full disk...), the files already
       replaced are put back from their backups, so the previous set is complete again.

    Only a power cut in the middle of step 2 can still leave a mix (the leftover ``.bak`` files
    are then the previous versions).
    """
    directory.mkdir(parents=True, exist_ok=True)
    temporary: dict[str, Path] = {}
    backups: dict[str, Path | None] = {}  # name -> backup of the previous version (None: it did not exist)
    replaced: list[str] = []
    restore_failed = False
    try:
        for name, content in contents.items():
            path = directory / f".{name}.tmp"
            path.write_text(content, encoding="utf-8")
            temporary[name] = path
        for name in contents:
            final = directory / name
            if final.exists():
                backup = directory / f".{name}.bak"
                shutil.copy2(final, backup)
                backups[name] = backup
            else:
                backups[name] = None
            temporary[name].replace(final)
            replaced.append(name)
    except BaseException:
        for name in reversed(replaced):
            try:
                backup = backups.get(name)
                if backup is not None:
                    backup.replace(directory / name)
                else:
                    (directory / name).unlink(missing_ok=True)
            except OSError:
                restore_failed = True  # la sauvegarde .bak reste à côté en dernier recours
        raise
    finally:
        for path in temporary.values():
            path.unlink(missing_ok=True)
        if not restore_failed:
            for backup in backups.values():
                if backup is not None:
                    backup.unlink(missing_ok=True)
