from __future__ import annotations

import csv
import difflib
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .exporters import clean_inline_text

REFERENTIEL_DIR = DATA_DIR / "referentiel"
ELUS_FILE = REFERENTIEL_DIR / "elus_mandats.json"
EXTRAS_FILE = REFERENTIEL_DIR / "personnes_supplementaires.json"

# Seuils de ressemblance (0 à 1) entre un nom entendu et un nom du référentiel.
AUTO_SCORE = 0.90  # correction sûre : appliquée dans le texte normalisé
CHECK_SCORE = 0.80  # proposition à valider
AMBIGUITY_MARGIN = 0.04  # deux personnes aussi proches : on ne tranche pas
FORMER_MEMBER_CAP = "verifier"  # un ancien élu n'est jamais corrigé automatiquement

_RANGE = re.compile(r"(\d{4})\s*-\s*(\d{4}|pr[ée]sent)", re.I)
_PARTICLES = {"de", "del", "della", "van", "von", "den", "der", "el", "al", "ben", "bou", "ait", "le", "la", "du", "des", "ter", "ten", "d"}
_CIVILITY = r"(?:Monsieur|Madame|Mademoiselle|Mme|Mlle|Mr|M\.|monsieur|madame|mademoiselle)"
_TOKEN = r"[A-ZÉÈÀÂÊÎÔÛÇ][\w'’\-éèêëàâîïôöûüç]+"
_NAME = r"((?:(?:de|De|d'|D'|van|Van|Den|den|der|Der|el|El|du|Du|le|Le|von|Von)\s*){0,2}" + _TOKEN + r"(?:\s+" + _TOKEN + r")?(?:\s+" + _TOKEN + r")?)"
_MENTION = re.compile(_CIVILITY + r"\s+" + _NAME)
_NOT_A_NAME = {
    "le", "la", "les", "l", "président", "présidente", "president", "bourgmestre", "échevin", "échevine", "echevin", "echevine",
    "lechvin", "léchevin", "lechevin", "conseiller", "conseillère", "merci", "je", "vous", "de", "du", "monsieur", "madame",
    "secrétaire", "greffier", "directeur", "directrice", "collègue", "collègues",
}
_ROLE_MAYOR = r"(?:(?:Monsieur|Madame)\s+)?(?:le|la)\s+[Bb]ourgmestre(?:\s+(?:faisant\s+fonction|f\.?\s?f\.?))?"
# Les formules (« la parole est à… ») sont insensibles à la casse ; les noms propres restent sensibles (majuscule initiale).
_FLOOR_AFTER = re.compile(
    r"(?i:(?:(?:donne|cède|passe|redonne)\s+(?:donc\s+)?la\s+parole|la\s+parole\s+(?:est|va)|parole\s+(?:est|va))\s+(?:à|au|pour)\s+(?:vous\s+)?)"
    r"(?:(?P<role>" + _ROLE_MAYOR + r")|(?:(?:" + _CIVILITY + r")\s+)?(?:(?:la|le)\s+)?" + _NAME + r")"
)
_FLOOR_BEFORE = re.compile(
    r"(?:(?:" + _CIVILITY + r")\s+)?" + _NAME + r"\s*,?\s+(?i:vous\s+avez\s+la\s+parole|je\s+vous\s+donne\s+la\s+parole|la\s+parole\s+est\s+à\s+vous)"
)
_FLOOR_NAMED_AFTER = re.compile(
    r"(?i:vous\s+avez\s+la\s+parole|la\s+parole\s+est\s+à\s+vous)\s*,\s*(?:(?:" + _CIVILITY + r")\s+)?" + _NAME
)
_GENERIC_FLOOR = re.compile(r"(?:vous\s+avez\s+la\s+parole|je\s+vous\s+(?:donne|cède)\s+la\s+parole|la\s+parole\s+est\s+à\s+(?:vous|\w))", re.I)
_THANKS = re.compile(
    r"^\W*(?:(?i:alors)\s+)?(?:(?i:je\s+vous)\s+)?(?i:merci)(?:\s+(?i:beaucoup))?\s*,?\s+(?:" + _CIVILITY + r")\s+" + _NAME
)
_MAYOR_FF = re.compile(r"faisant\s+fonction|f\.?\s?f\b", re.I)


def strip_accents(text: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFD", text) if unicodedata.category(char) != "Mn")


def phonetic(text: str) -> str:
    """Rough French/Arabic/Dutch-friendly key: names misheard by the transcriber still collide."""
    value = re.sub(r"[^a-z ]", "", strip_accents(text.lower()))
    for old, new in (
        ("sch", "s"), ("ph", "f"), ("kh", "k"), ("ck", "k"), ("eck", "ek"), ("qu", "k"), ("ch", "s"), ("eau", "o"), ("ou", "u"),
        ("aille", "ai"), ("au", "o"), ("ay", "e"), ("ey", "e"), ("gn", "n"), ("gue", "ge"), ("gui", "gi"), ("g", "k"), ("x", "ks"), ("y", "i"), ("w", "v"),
        ("z", "s"), ("c", "k"), ("h", ""), ("ee", "e"), ("ai", "e"), ("ei", "e"),
    ):
        value = value.replace(old, new)
    value = re.sub(r"(.)\1+", r"\1", value)
    value = re.sub(r"e$", "", value)
    value = re.sub(r"[dtsxz]+$", "", value)
    value = re.sub(r"e$", "", value)
    return value.replace(" ", "")


def plain(text: str) -> str:
    return re.sub(r"[^a-z]", "", strip_accents(text.lower()))


def similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right).ratio()


def mandate_years(value: str | None) -> list[tuple[int, int]]:
    ranges = []
    for start, end in _RANGE.findall(value or ""):
        ranges.append((int(start), 9999 if end.lower().startswith("pr") else int(end)))
    return ranges


def _split_top_level(value: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for char in value:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    return parts + [current] if current.strip() else parts


@dataclass
class Person:
    name: str
    functions: list[str] = field(default_factory=list)
    president: bool = False
    mayor: str | None = None  # "titre" | "ff"
    mayor_impeded: bool = False  # bourgmestre en titre empêché cette année-là : le f.f. exerce
    current: bool = True
    aliases: list[str] = field(default_factory=list)  # graphies déformées confirmées par l'utilisateur
    _forms: list[tuple[str, str, str, str]] = field(default_factory=list, repr=False)

    @property
    def label(self) -> str:
        return self.name

    def forms(self) -> list[tuple[str, str, str, str]]:
        """(texte, clé phonétique, clé simple, graphie officielle à écrire en cas de correction)."""
        if not self._forms:
            official = {text: text for text in _name_forms(self.name)}
            surname = _surname(self.name)
            self._forms = [(text, phonetic(text), plain(text), text) for text in sorted(official)]
            self._forms += [(alias, phonetic(alias), plain(alias), surname) for alias in self.aliases if len(plain(alias)) >= 3]
        return self._forms


def _surname(name: str) -> str:
    forms = sorted(_name_forms(name), key=lambda text: (len(text.split()), len(text)))
    core = [text for text in forms if text != name]
    return core[0] if core else name


def _name_forms(name: str) -> set[str]:
    tokens = name.split()
    index = 1
    while index < len(tokens) - 1 and tokens[index].lower() not in _PARTICLES:
        index += 1
    family = tokens[index:] if len(tokens) > 1 else tokens
    core = [token for token in family if token.lower() not in _PARTICLES]
    forms = {name, " ".join(family)}
    if core:
        forms.update({" ".join(core), core[-1]})
    return {form for form in forms if len(plain(form)) >= 3}


def load_roster(
    reference_date: date | None = None,
    elus_file: Path | None = None,
    extras_file: Path | None = None,
) -> tuple[list[Person], list[Person]]:
    """Return (people in office at the date, former members) from the user's JSON files."""
    year = (reference_date or date.today()).year
    merged: dict[str, Person] = {}
    for entry in _read_entries(elus_file or ELUS_FILE) + _read_entries(extras_file or EXTRAS_FILE):
        person = _person_from_entry(entry, year)
        known = merged.get(plain(person.name))
        if known is None:
            merged[plain(person.name)] = person
            continue
        # même personne dans deux fichiers : on cumule les fonctions
        known.functions = list(dict.fromkeys(known.functions + person.functions))
        known.aliases = list(dict.fromkeys(known.aliases + person.aliases))
        known.president = known.president or person.president
        known.mayor = known.mayor or person.mayor
        known.mayor_impeded = known.mayor_impeded and person.mayor_impeded if known.mayor else person.mayor_impeded
        known.current = known.current or person.current
    people = list(merged.values())
    return [person for person in people if person.current], [person for person in people if not person.current]


def _read_entries(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("nom")] if isinstance(data, list) else []


def _person_from_entry(entry: dict[str, Any], year: int) -> Person:
    person = Person(name=clean_inline_text(entry["nom"]))
    person.aliases = [clean_inline_text(item) for item in (entry.get("alias") or []) if clean_inline_text(item)]
    statut = str(entry.get("statut") or "")
    if any(start <= year <= end for start, end in mandate_years(entry.get("conseiller_communal"))):
        person.functions.append("conseiller communal")
    if any(start <= year <= end for start, end in mandate_years(entry.get("echevin"))):
        person.functions.append("échevin")
    for item in _split_top_level(str(entry.get("bourgmestre") or "")):
        ranges = mandate_years(item.split("(")[0])
        if any(start <= year <= end for start, end in ranges):
            ff = bool(_MAYOR_FF_TEXT.search(item.split("remplac")[0]))  # « remplacé par X f.f. » ne vise pas le titulaire
            impeded = not ff and _impeded_in(item, year)
            person.mayor = person.mayor if person.mayor == "titre" else ("ff" if ff else "titre")
            person.mayor_impeded = impeded
            person.functions.append("bourgmestre f.f." if ff else "bourgmestre en titre (empêché)" if impeded else "bourgmestre")
    if "président du conseil communal" in statut.lower() or entry.get("president_conseil") is True:
        person.president = True
        person.functions.append("président du conseil communal")
    for extra in entry.get("fonctions", []) or []:
        if isinstance(extra, dict) and _function_active(extra, year):
            person.functions.append(clean_inline_text(extra.get("intitule", "")))
            person.president = person.president or bool(extra.get("president_conseil"))
    person.current = bool(person.functions) or entry.get("actif") is True
    return person


def _impeded_in(text: str, year: int) -> bool:
    """Does « empêché(e) … » in the free text of a mandate cover this year?"""
    match = re.search(r"emp[êe]ch\w*\s+([^;)]*)", text, re.I)
    if not match:
        return False
    zone = match.group(1)
    if any(start <= year <= end for start, end in mandate_years(zone)):
        return True
    return str(year) in re.findall(r"\b(\d{4})\b", zone)


_MAYOR_FF_TEXT = re.compile(r"faisant\s+fonction|f\.?\s?f\b", re.I)


def _function_active(item: dict[str, Any], year: int) -> bool:
    start = int(str(item.get("du") or "0000")[:4] or 0)
    end = int(str(item.get("au") or "9999")[:4] or 9999)
    return start <= year <= end


@dataclass
class Match:
    person: Person | None
    score: float
    level: str  # "auto" | "verifier" | "inconnu"
    form: str = ""
    candidates: list[str] = field(default_factory=list)
    former: bool = False


class Matcher:
    def __init__(self, active: list[Person], former: list[Person] | None = None) -> None:
        self.active = active
        self.former = former or []

    def _rank(self, candidate: str, people: list[Person]) -> list[tuple[float, str, Person]]:
        key_phonetic, key_plain = phonetic(candidate), plain(candidate)
        if len(key_plain) < 3:
            return []
        ranked = []
        for person in people:
            best_score, best_form = 0.0, ""
            for _text, form_phonetic, form_plain, replacement in person.forms():
                score = max(similarity(key_phonetic, form_phonetic), similarity(key_plain, form_plain))
                if score > best_score:
                    best_score, best_form = score, replacement
            ranked.append((best_score, best_form, person))
        ranked.sort(key=lambda item: -item[0])
        return ranked

    def match(self, candidate: str) -> Match:
        ranked = self._rank(candidate, self.active)
        if ranked and ranked[0][0] >= CHECK_SCORE:
            score, form, person = ranked[0]
            close = [item for item in ranked[1:] if item[0] >= score - AMBIGUITY_MARGIN]
            ambiguous = bool(close)
            level = "auto" if score >= AUTO_SCORE and not ambiguous else "verifier"
            return Match(person, score, level, form, [person.name] + [item[2].name for item in close] if ambiguous else [])
        ranked_former = self._rank(candidate, self.former)
        if ranked_former and ranked_former[0][0] >= CHECK_SCORE:
            score, form, person = ranked_former[0]
            return Match(person, score, FORMER_MEMBER_CAP, form, former=True)
        return Match(None, ranked[0][0] if ranked else 0.0, "inconnu")

    def match_tokens(self, text: str) -> Match:
        """Best match among the first 1-3 words of a candidate name."""
        tokens = text.split()
        best = Match(None, 0.0, "inconnu")
        for size in range(1, min(3, len(tokens)) + 1):
            result = self.match(" ".join(tokens[:size]))
            if (result.level != "inconnu", result.score) > (best.level != "inconnu", best.score):
                best = result
        return best


def _clock(seconds: float) -> str:
    total = int(max(0, seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def _is_name(candidate: str) -> bool:
    first = candidate.split()[0].lower().strip("'’") if candidate.split() else ""
    return bool(first) and first not in _NOT_A_NAME


def name_spans(text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, name, how) of every person name found in a sentence, left to right."""
    found: dict[int, tuple[int, int, str, str]] = {}
    for pattern, via in ((_MENTION, "civilité"), (_FLOOR_AFTER, "parole"), (_FLOOR_BEFORE, "parole"), (_FLOOR_NAMED_AFTER, "parole")):
        for match in pattern.finditer(text):
            raw = match.group(1)
            if raw and _is_name(raw):
                start = match.start(1)
                # une formule de passage de parole l'emporte sur une simple mention du même nom
                if via == "parole" or start not in found:
                    found[start] = (start, match.end(1), raw, via)
    return [found[key] for key in sorted(found)]


def analyze_names(
    segments: list[dict[str, Any]],
    reference_date: date | None = None,
    elus_file: Path | None = None,
    extras_file: Path | None = None,
) -> dict[str, Any]:
    """Find the names cited in the transcript and match them with the list of elected members.

    The spoken text is never modified: every segment gains ``mentions`` and, when a correction is
    certain, ``text_normalized`` (same sentence with the official spelling of surnames).
    """
    active, former = load_roster(reference_date, elus_file, extras_file)
    matcher = Matcher(active, former)
    rows: dict[str, dict[str, Any]] = {}
    unknown: dict[str, int] = {}

    for index, segment in enumerate(segments):
        segment.pop("mentions", None)
        segment.pop("text_normalized", None)
        text = str(segment.get("text", ""))
        mentions, corrected = [], text
        for start, end, raw, via in name_spans(text):
            result = matcher.match_tokens(raw)
            mention = {
                "raw": raw, "via": via, "level": result.level, "score": round(result.score, 2),
                "person": result.person.name if result.person else None,
                "function": ", ".join(dict.fromkeys(result.person.functions)) if result.person else None,
            }
            if result.candidates:
                mention["candidates"] = result.candidates
            if result.former:
                mention["former_member"] = True
            mentions.append(mention)
            if result.level == "auto" and result.form and raw != result.form:
                corrected = corrected.replace(raw, result.form, 1)
            if result.person:
                row = rows.setdefault(result.person.name, {"nom": result.person.name, "fonction": mention["function"], "occurrences": 0, "variantes": {}, "niveaux": {}, "heures": [], "ancien_elu": result.former})
                row["occurrences"] += 1
                row["variantes"][raw] = row["variantes"].get(raw, 0) + 1
                row["niveaux"][result.level] = row["niveaux"].get(result.level, 0) + 1
                if len(row["heures"]) < 5:
                    row["heures"].append(_clock(float(segment.get("start", 0))))
            else:
                unknown[raw] = unknown.get(raw, 0) + 1
        if mentions:
            segment["mentions"] = mentions
            if corrected != text:
                segment["text_normalized"] = corrected

    summary = sorted(rows.values(), key=lambda row: -row["occurrences"])
    for row in summary:
        row["variantes"] = dict(sorted(row["variantes"].items(), key=lambda kv: -kv[1]))
    unknown_rows = [{"nom": name, "occurrences": count} for name, count in sorted(unknown.items(), key=lambda kv: -kv[1])]
    stats = {
        "mentions": sum(row["occurrences"] for row in summary) + sum(unknown.values()),
        "reconnues": sum(row["niveaux"].get("auto", 0) for row in summary),
        "a_verifier": sum(row["niveaux"].get("verifier", 0) for row in summary),
        "inconnues": sum(unknown.values()),
        "annee_reference": (reference_date or date.today()).year,
        "elus_en_exercice": len(active),
    }
    return {"summary": summary, "unknown": unknown_rows, "stats": stats}


def names_csv(analysis: dict[str, Any]) -> str:
    """Spreadsheet-friendly list of the names cited (semicolon separated, UTF-8 with BOM)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["nom_reconnu", "fonction", "occurrences", "corrigees_automatiquement", "a_verifier", "variantes_entendues", "premieres_heures", "remarque"])
    for row in analysis.get("summary", []):
        writer.writerow([
            row["nom"], row.get("fonction") or "", row["occurrences"], row["niveaux"].get("auto", 0), row["niveaux"].get("verifier", 0),
            " | ".join(f"{name} ({count})" for name, count in row["variantes"].items()), " ".join(row["heures"]),
            "ancien élu : mandat terminé à la date de la séance" if row.get("ancien_elu") else "",
        ])
    writer.writerow([])
    writer.writerow(["nom_non_reconnu", "", "occurrences"])
    for row in analysis.get("unknown", []):
        writer.writerow([row["nom"], "", row["occurrences"]])
    return "﻿" + buffer.getvalue()


def parse_video_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def roster_available(elus_file: Path | None = None, extras_file: Path | None = None) -> bool:
    return bool(_read_entries(elus_file or ELUS_FILE) or _read_entries(extras_file or EXTRAS_FILE))


def write_names_csv(output_dir: Path, analysis: dict[str, Any]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "noms_cites.csv").write_text(names_csv(analysis), encoding="utf-8")
    return {"name": "noms_cites.csv", "label": "Noms cités (CSV)"}


def names_message(analysis: dict[str, Any]) -> str:
    stats = analysis["stats"]
    return (
        f"Noms cités : {stats['reconnues']} corrigés automatiquement, "
        f"{stats['a_verifier']} à vérifier, {stats['inconnues']} non reconnus."
    )


# ---------------------------------------------------------------------------------------------
# Étiquetage des voix par les formules de passage de parole
# ---------------------------------------------------------------------------------------------

FLOOR_LOOKAHEAD = 6  # segments examinés après « vous avez la parole » pour trouver qui répond
THANKS_LOOKBACK = 4
PRESIDENT_MIN_CUES = 3


def _mayor_candidates(active: list[Person], text: str) -> list[Person]:
    """Who « Monsieur le Bourgmestre » designates: the holder, or the acting one (f.f.) when the holder is impeded."""
    holders = [person for person in active if person.mayor]
    ff = [person for person in holders if person.mayor == "ff"]
    if _MAYOR_FF.search(text):
        return ff
    in_office = [person for person in holders if person.mayor == "titre" and not person.mayor_impeded]
    return in_office or ff or holders


def cue_suggestions(
    segments: list[dict[str, Any]],
    reference_date: date | None = None,
    elus_file: Path | None = None,
    extras_file: Path | None = None,
) -> list[dict[str, str]]:
    """Name the voices from the way the chair hands over the floor.

    « Madame X, vous avez la parole » names the voice that speaks next; « Merci Madame X » names the
    voice that just spoke. The voice that hands over the floor again and again is the chair.
    """
    active, former = load_roster(reference_date, elus_file, extras_file)
    matcher = Matcher(active, former)
    votes: dict[str, dict[str, float]] = {}
    evidence: dict[tuple[str, str], str] = {}
    cues: dict[str, int] = {}

    def vote(label: str, person: Person, weight: float, quote: str, segment: dict[str, Any]) -> None:
        votes.setdefault(label, {}).setdefault(person.name, 0.0)
        votes[label][person.name] += weight
        evidence.setdefault((label, person.name), f"« {quote.strip()[:110]} » ({_clock(float(segment.get('start', 0)))})")

    for index, segment in enumerate(segments):
        label = str(segment.get("speaker", ""))
        text = str(segment.get("text", ""))
        gives_floor = bool(_GENERIC_FLOOR.search(text)) or any(via == "parole" for *_, via in name_spans(text))
        if gives_floor:
            cues[label] = cues.get(label, 0) + 1

            target = next(
                (other for other in segments[index + 1: index + 1 + FLOOR_LOOKAHEAD] if other.get("speaker") != label), None
            )
            person = None
            for _start, _end, raw, via in name_spans(text):
                if via != "parole":
                    continue
                result = matcher.match_tokens(raw)
                if result.person and not result.former and result.level in ("auto", "verifier") and not result.candidates:
                    person = result.person
            role = _FLOOR_AFTER.search(text)
            if person is None and role and role.group("role"):
                holders = _mayor_candidates(active, role.group("role"))
                person = holders[0] if len(holders) == 1 else None
            if person and target:
                vote(str(target["speaker"]), person, 1.0, text, segment)

        thanks = _THANKS.search(text[:90])
        if thanks:
            result = matcher.match_tokens(thanks.group(1))
            if result.person and not result.former and result.level in ("auto", "verifier") and not result.candidates:
                previous = next(
                    (other for other in reversed(segments[max(0, index - THANKS_LOOKBACK): index]) if other.get("speaker") != label), None
                )
                if previous:
                    vote(str(previous["speaker"]), result.person, 0.7, text, segment)

    suggestions: list[dict[str, str]] = []
    president = next((person for person in active if person.president), None)
    chair_labels = {label for label, count in cues.items() if count >= PRESIDENT_MIN_CUES}
    for label in sorted(chair_labels):
        suggestions.append({
            "label": label,
            "name": president.name if president else "Le Président",
            "confidence": "haute" if cues[label] >= 5 else "moyenne",
            "evidence": f"a donné la parole {cues[label]} fois",
            "source": "règles",
            "role": "président du conseil communal",
        })
    for label, ranking in votes.items():
        if label in chair_labels:
            continue
        ordered = sorted(ranking.items(), key=lambda item: -item[1])
        name, weight = ordered[0]
        runner_up = ordered[1][1] if len(ordered) > 1 else 0.0
        if weight < 0.7 or runner_up * 2 > weight:
            continue  # indices contradictoires : on ne tranche pas
        suggestions.append({
            "label": label,
            "name": name,
            "confidence": "haute" if weight >= 2 else "moyenne",
            "evidence": evidence[(label, name)] + (f" · {int(weight)} indice(s)" if weight >= 2 else ""),
            "source": "règles",
        })
    return suggestions


def roster_names(reference_date: date | None = None, elus_file: Path | None = None, extras_file: Path | None = None) -> list[str]:
    """Official spelling of the people in office, handed to the language model as a reference."""
    active, _ = load_roster(reference_date, elus_file, extras_file)
    return [f"{person.name} ({', '.join(dict.fromkeys(person.functions))})" for person in active]
