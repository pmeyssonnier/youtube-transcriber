from __future__ import annotations

MIN_RANGE_SECONDS = 5.0


def validate_time_range(
    start: float | None,
    end: float | None,
    duration: float | None,
) -> tuple[float | None, float | None]:
    """Normalise an optional [start, end] range of the video, in seconds.

    A start at 0 and an end at (or past) the end of the video mean "no limit".
    """
    start_value = float(start) if start else None
    end_value = float(end) if end else None

    if duration is not None:
        if start_value is not None and start_value >= duration:
            raise ValueError("Le début doit se trouver avant la fin de la vidéo.")
        if end_value is not None and end_value >= duration - 1:
            end_value = None
    elif end_value is not None:
        raise ValueError("La durée de la vidéo est inconnue : indiquez seulement un début.")

    if start_value is not None and start_value < 1:
        start_value = None
    if start_value is not None and end_value is not None and end_value - start_value < MIN_RANGE_SECONDS:
        raise ValueError(f"La plage transcrite doit durer au moins {int(MIN_RANGE_SECONDS)} secondes.")
    if end_value is not None and start_value is None and end_value < MIN_RANGE_SECONDS:
        raise ValueError(f"La plage transcrite doit durer au moins {int(MIN_RANGE_SECONDS)} secondes.")
    return start_value, end_value


def effective_duration(start: float | None, end: float | None, duration: float | None) -> float | None:
    """Seconds that will really be transcribed, or None when unknown."""
    if duration is None:
        return None
    return max(0.0, (end if end is not None else duration) - (start or 0.0))
