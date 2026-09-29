from __future__ import annotations

import json
import os
from typing import Any

from .exporters import clean_inline_text

NAMING_MODEL_DEFAULT = "gpt-4o-mini"
MIN_SEGMENTS_FOR_SUGGESTION = 3
MAX_LABELS_FOR_SUGGESTION = 60
LABELS_PER_REQUEST = 20
TURNS_PER_LABEL = 3
SNIPPET_CHARS = 180
CONFIDENCE_LEVELS = ("haute", "moyenne", "faible")


class SuggestionError(RuntimeError):
    """Error with a message suitable for the local user interface."""


def _words(text: str) -> int:
    return len(text.split())


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    cleaned = clean_inline_text(text)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


def speaker_stats(segments: list[dict[str, Any]], names: dict[str, str]) -> list[dict[str, Any]]:
    """Summarise every speaker label so the user can identify who is who.

    Sorted by number of words, because the few main voices carry most of the text.
    """
    stats: dict[str, dict[str, Any]] = {}
    total_words = 0
    for segment in segments:
        label = str(segment.get("speaker", "Intervenant"))
        text = str(segment.get("text", ""))
        start = float(segment.get("start", 0))
        end = float(segment.get("end", start))
        words = _words(text)
        total_words += words
        item = stats.setdefault(
            label,
            {"label": label, "segments": 0, "words": 0, "seconds": 0.0, "first_start": start, "sample": ""},
        )
        item["segments"] += 1
        item["words"] += words
        item["seconds"] += max(0.0, end - start)
        item["first_start"] = min(item["first_start"], start)
        if not item["sample"] and words >= 6:
            item["sample"] = _snippet(text)

    result = []
    for label, item in stats.items():
        item["name"] = names.get(label, label)
        item["share"] = round(item["words"] / total_words, 4) if total_words else 0.0
        item["seconds"] = round(item["seconds"], 1)
        item["first_start"] = round(item["first_start"], 1)
        result.append(item)
    result.sort(key=lambda entry: (-entry["words"], entry["label"]))
    return result


def naming_contexts(segments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Build short excerpts around the first turns of each important label.

    Names are usually spoken by someone else just before ("je donne la parole à…")
    or right after ("merci Madame…"), so each excerpt keeps the neighbouring turns.
    Returns (contexts, number_of_labels_left_out).
    """
    stats = speaker_stats(segments, {})
    eligible = [item["label"] for item in stats if item["segments"] >= MIN_SEGMENTS_FOR_SUGGESTION]
    selected = eligible[:MAX_LABELS_FOR_SUGGESTION]
    skipped = len(stats) - len(selected)

    turn_starts: dict[str, list[int]] = {label: [] for label in selected}
    previous_label = None
    for index, segment in enumerate(segments):
        label = str(segment.get("speaker", "Intervenant"))
        if label != previous_label and label in turn_starts:
            turn_starts[label].append(index)
        previous_label = label

    contexts = []
    for label in selected:
        starts = turn_starts[label]
        if len(starts) > TURNS_PER_LABEL:
            step = (len(starts) - 1) / (TURNS_PER_LABEL - 1)
            starts = [starts[round(step * position)] for position in range(TURNS_PER_LABEL)]
        excerpts = []
        for index in starts:
            before = next(
                (segments[i] for i in range(index - 1, max(-1, index - 4), -1) if segments[i].get("speaker") != label),
                None,
            )
            after = next(
                (segments[i] for i in range(index + 1, min(len(segments), index + 12)) if segments[i].get("speaker") != label),
                None,
            )
            excerpts.append(
                {
                    "avant": _snippet(str(before.get("text", ""))) if before else "",
                    "cette_voix": _snippet(str(segments[index].get("text", ""))),
                    "apres": _snippet(str(after.get("text", ""))) if after else "",
                }
            )
        contexts.append({"label": label, "extraits": excerpts})
    return contexts, skipped


PROMPT = (
    "Tu aides à identifier les intervenants d'une séance (par exemple un conseil communal) "
    "à partir d'une transcription automatique. Pour chaque étiquette tu reçois quelques extraits : "
    "ce qui est dit juste avant (souvent par un autre intervenant), la prise de parole de l'étiquette, "
    "et ce qui suit.\n"
    "Règles strictes :\n"
    "- Propose un nom UNIQUEMENT si un indice explicite le justifie : la personne se présente, "
    "quelqu'un lui donne la parole nommément, ou on la remercie nommément juste après.\n"
    "- Sans indice explicite, mets name à null. Ne devine jamais et n'invente jamais de nom.\n"
    "- Recopie le nom sans la civilité (Monsieur, Madame) et garde la même orthographe pour une même "
    "personne ; la transcription automatique peut déformer les noms.\n"
    "- Si la fonction est claire mais pas le nom (ex. « le bourgmestre »), utilise-la comme name.\n"
    "- confidence vaut « haute » (indice explicite et direct), « moyenne » ou « faible ».\n"
    "- evidence est une citation très courte qui justifie le choix.\n"
    'Réponds uniquement avec un objet JSON : {"suggestions":[{"label":"…","name":"…"|null,'
    '"confidence":"haute|moyenne|faible","evidence":"…"}]}'
)


def _clean_suggestions(payload: Any, allowed: set[str]) -> list[dict[str, str]]:
    items = payload.get("suggestions") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise SuggestionError("La réponse du modèle est illisible.")
    cleaned: dict[str, dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        name = clean_inline_text(item.get("name"))[:100]
        confidence = str(item.get("confidence", "")).lower()
        if label not in allowed or not name or name.lower() in {"null", "none", "inconnu"}:
            continue
        cleaned[label] = {
            "label": label,
            "name": name,
            "confidence": confidence if confidence in CONFIDENCE_LEVELS else "faible",
            "evidence": _snippet(str(item.get("evidence", "")), 160),
        }
    return list(cleaned.values())


def request_name_suggestions(client: Any, contexts: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Ask the language model for name proposals, in small batches."""
    model = os.getenv("NAMING_MODEL", NAMING_MODEL_DEFAULT)
    suggestions: list[dict[str, str]] = []
    for start in range(0, len(contexts), LABELS_PER_REQUEST):
        batch = contexts[start : start + LABELS_PER_REQUEST]
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": json.dumps({"etiquettes": batch}, ensure_ascii=False)},
            ],
        )
        try:
            payload = json.loads(response.choices[0].message.content or "")
        except (json.JSONDecodeError, IndexError, AttributeError) as exc:
            raise SuggestionError("La réponse du modèle est illisible.") from exc
        suggestions.extend(_clean_suggestions(payload, {entry["label"] for entry in batch}))
    return suggestions
