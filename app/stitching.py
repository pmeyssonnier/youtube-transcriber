from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

# Recouvrement audio entre deux parties consécutives : la même parole est transcrite deux fois,
# ce qui permet de relier les étiquettes de locuteurs des deux parties (voir link_zone).
DEFAULT_OVERLAP_SECONDS = 45.0

# Réglage « Relier les intervenants » : recouvrement (en secondes) ajouté à chaque partie.
OVERLAP_BY_MODE = {"off": 0.0, "light": DEFAULT_OVERLAP_SECONDS, "strong": 180.0}
DEFAULT_LINK_MODE = "light"

# Une liaison exige un recouvrement de parole net, mutuel et majoritaire : mieux vaut laisser deux
# étiquettes séparées que fusionner deux personnes différentes.
LINK_MIN_SECONDS = 2.5
LINK_MIN_SHARE = 0.5
MIN_ZONE_SECONDS = 0.5


class _Groups:
    """Union-find over speaker labels."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, label: str) -> str:
        self.parent.setdefault(label, label)
        root = label
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[label] != root:
            self.parent[label], label = root, self.parent[label]
        return root

    def union(self, first: str, second: str) -> None:
        root_first, root_second = self.find(first), self.find(second)
        if root_first != root_second:
            self.parent[root_second] = root_first


def _clipped(segments: list[dict[str, Any]], low: float, high: float) -> list[tuple[str, float, float]]:
    clipped = []
    for segment in segments:
        start = max(float(segment["start"]), low)
        end = min(float(segment["end"]), high)
        if end > start:
            clipped.append((str(segment["speaker"]), start, end))
    return clipped


def link_zone(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    low: float,
    high: float,
    groups: _Groups,
) -> int:
    """Link labels of two consecutive chunks that speak at the same time in their shared audio.

    Two labels are linked only if each one is the other's best match, their common speech lasts at
    least LINK_MIN_SECONDS and covers at least LINK_MIN_SHARE of the shorter of the two. Matching is
    one-to-one, so a group never holds two labels of the same chunk.
    """
    first = _clipped(before, low, high)
    second = _clipped(after, low, high)
    common: dict[tuple[str, str], float] = defaultdict(float)
    total_first: dict[str, float] = defaultdict(float)
    total_second: dict[str, float] = defaultdict(float)

    for label, start, end in first:
        total_first[label] += end - start
    for label, start, end in second:
        total_second[label] += end - start
    for label_a, start_a, end_a in first:
        for label_b, start_b, end_b in second:
            overlap = min(end_a, end_b) - max(start_a, start_b)
            if overlap > 0:
                common[(label_a, label_b)] += overlap

    best_for_first: dict[str, tuple[str, float]] = {}
    best_for_second: dict[str, tuple[str, float]] = {}
    for (label_a, label_b), value in common.items():
        if value > best_for_first.get(label_a, ("", 0.0))[1]:
            best_for_first[label_a] = (label_b, value)
        if value > best_for_second.get(label_b, ("", 0.0))[1]:
            best_for_second[label_b] = (label_a, value)

    linked = 0
    for (label_a, label_b), value in common.items():
        if best_for_first[label_a][0] != label_b or best_for_second[label_b][0] != label_a:
            continue
        shorter = min(total_first[label_a], total_second[label_b])
        if value >= LINK_MIN_SECONDS and value >= LINK_MIN_SHARE * shorter:
            groups.union(label_a, label_b)
            linked += 1
    return linked


def choose_cut(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    low: float,
    high: float,
) -> float:
    """Pick where the transcript switches from one chunk to the next, inside their shared audio.

    The best cut is a moment where nobody is speaking in either transcript, so no sentence is lost
    or repeated. Only the central part of the shared audio is considered: the very start and end of
    a chunk are the least reliable. Falls back to the point crossed by the fewest segments.
    """
    middle = (low + high) / 2
    margin = (high - low) * 0.2
    inner_low, inner_high = low + margin, high - margin
    everything = [(float(item["start"]), float(item["end"])) for item in before + after]

    candidates = {middle}
    for start, end in everything:
        for moment in (start, end):
            if inner_low <= moment <= inner_high:
                candidates.add(moment)

    def crossed(moment: float) -> int:
        return sum(1 for start, end in everything if start < moment < end)

    return min(candidates, key=lambda moment: (crossed(moment), abs(moment - middle)))


def stitch_segments(
    results: list[list[dict[str, Any]]],
    windows: list[tuple[float, float]],
    relabel: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Merge per-chunk segments (absolute times) into one transcript.

    ``windows[i]`` is the (start, end) of chunk i in the video. Where two chunks overlap, the
    transcript switches from the first to the second at a quiet moment (see choose_cut) and the
    speaker labels seen speaking together in the shared audio are merged. With ``relabel`` the
    resulting voices are renamed ``Voix 01``, ``Voix 02``… in order of first appearance; the
    original label is kept in ``raw_speaker``.
    """
    count = len(results)
    groups = _Groups()
    cuts: list[float] = []
    for index in range(count - 1):
        low, high = windows[index + 1][0], windows[index][1]
        if high - low > MIN_ZONE_SECONDS:
            cuts.append(choose_cut(results[index], results[index + 1], low, high))
            if relabel:
                link_zone(results[index], results[index + 1], low, high, groups)
        else:
            cuts.append(windows[index + 1][0])

    kept: list[dict[str, Any]] = []
    for index, segments in enumerate(results):
        lower = cuts[index - 1] if index > 0 else -math.inf
        upper = cuts[index] if index < count - 1 else math.inf
        for segment in segments:
            if lower <= float(segment["start"]) < upper:
                kept.append(dict(segment))

    raw_labels = {str(segment["speaker"]) for segment in kept}
    if not relabel:
        return kept, {"raw_labels": len(raw_labels), "voices": len(raw_labels)}

    names: dict[str, str] = {}
    for segment in kept:
        raw = str(segment["speaker"])
        root = groups.find(raw)
        if root not in names:
            names[root] = f"Voix {len(names) + 1:02d}"
        segment["raw_speaker"] = raw
        segment["speaker"] = names[root]
    return kept, {"raw_labels": len(raw_labels), "voices": len(names)}
