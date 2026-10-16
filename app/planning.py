from __future__ import annotations

import math

from .constants import DIARIZATION_MAX_AUDIO_SECONDS, DIARIZATION_MAX_CHUNK_MINUTES
from .stitching import OVERLAP_BY_MODE

# Une dernière partie plus courte que cela est absorbée par la précédente.
MIN_LAST_CHUNK_SECONDS = 30.0


def chunk_seconds_for(chunk_minutes: int, diarize: bool, overlap: float) -> int:
    """Length of the step between two chunks, as the pipeline will really cut the audio.

    With speaker separation the model accepts at most DIARIZATION_MAX_AUDIO_SECONDS of audio per
    request, overlap included, so the step shrinks when the overlap grows.
    """
    seconds = chunk_minutes * 60
    if diarize:
        seconds = min(seconds, DIARIZATION_MAX_CHUNK_MINUTES * 60, int(DIARIZATION_MAX_AUDIO_SECONDS - overlap))
    return seconds


def chunk_plan(span_start: float, span_end: float, seconds: int, overlap: float) -> list[tuple[float, float]]:
    """(start, length) of every chunk: each one runs ``overlap`` seconds into the next."""
    span = max(0.0, span_end - span_start)
    count = max(1, math.ceil(span / seconds - 1e-9))
    if count > 1 and span - (count - 1) * seconds < MIN_LAST_CHUNK_SECONDS:
        count -= 1
    plan = []
    for index in range(count):
        chunk_start = span_start + index * seconds
        remaining = span_end - chunk_start
        length = remaining if index == count - 1 else min(seconds + overlap, remaining)
        plan.append((chunk_start, length))
    return plan


def plan_summary(span_seconds: float, chunk_minutes: int, diarize: bool, link_mode: str) -> dict:
    """Real number of chunks and audio to send for one setting (nothing is a fixed percentage)."""
    overlap = OVERLAP_BY_MODE.get(link_mode, 0.0) if diarize else 0.0
    step = chunk_seconds_for(chunk_minutes, diarize, overlap)
    plan = chunk_plan(0.0, float(span_seconds), step, overlap)
    audio = sum(length for _, length in plan)
    extra = max(0.0, audio - span_seconds)
    return {
        "chunks": len(plan),
        "step_seconds": step,
        "overlap_seconds": overlap,
        "audio_seconds": round(audio, 1),
        "extra_seconds": round(extra, 1),
        "extra_percent": round(100 * extra / span_seconds, 1) if span_seconds > 0 else 0.0,
    }


def plan_all_modes(span_seconds: float, chunk_minutes: int, diarize: bool) -> dict[str, dict]:
    return {mode: plan_summary(span_seconds, chunk_minutes, diarize, mode) for mode in OVERLAP_BY_MODE}
