from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any


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
        "metadata": metadata,
        "speakers": speaker_names,
        "segments": [
            {
                **segment,
                "speaker_name": _speaker(segment, speaker_names),
            }
            for segment in segments
        ],
    }
    json_path = output_dir / f"{base}.json"
    json_path.write_text(json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8")
    files.append({"name": json_path.name, "label": "JSON structuré"})

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
        path = output_dir / f"{base}.{suffix}"
        path.write_text(content, encoding="utf-8")
        files.append({"name": path.name, "label": label})

    return files
