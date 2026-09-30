from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app, store
from app.people import (
    _mayor_candidates,
    analyze_names,
    cue_suggestions,
    load_roster,
    mandate_years,
    names_csv,
    parse_video_date,
    phonetic,
    roster_available,
)

ELUS = [
    {"nom": "Georges Verzin", "conseiller_communal": "1982-présent", "echevin": None, "bourgmestre": None, "statut": "Conseiller communal"},
    {"nom": "Naïma Belkhatir", "conseiller_communal": "2018-présent", "echevin": None, "bourgmestre": None, "statut": "Conseiller communal"},
    {"nom": "Isabelle Durant", "conseiller_communal": "2024-présent", "echevin": None, "bourgmestre": None, "statut": "Conseiller communal"},
    {"nom": "Vincent Vanhalewyn", "conseiller_communal": "2006-présent", "echevin": "2012-présent", "bourgmestre": None, "statut": "Échevin"},
    {"nom": "Quentin Vanbaelen", "conseiller_communal": "2018-présent", "echevin": None, "bourgmestre": None, "statut": "Conseiller communal"},
    {"nom": "Leticia Sere", "conseiller_communal": "2018-2024", "echevin": None, "bourgmestre": None, "statut": "Ancienne conseillère communale"},
    {"nom": "Hasan Koyuncu", "conseiller_communal": "2012-présent", "echevin": None, "bourgmestre": None, "statut": "Président du conseil communal / Conseiller communal"},
    {"nom": "Martin de Brabant", "conseiller_communal": "2018-présent", "echevin": "2025-présent", "bourgmestre": "2026-présent (faisant fonction)", "statut": "Bourgmestre f.f."},
    {"nom": "Audrey Henry", "conseiller_communal": "2024-présent", "echevin": None, "bourgmestre": "2025-présent (en titre; empêchée en 2026)", "statut": "Bourgmestre empêchée"},
    {"nom": "Bernard Clerfayt", "conseiller_communal": "1988-présent", "echevin": "1995-2000", "bourgmestre": "2001-2024 (empêché 2008-2011 et 2019-2024 ; remplacé par Cécile Jodogne bourgmestre f.f.)", "statut": "Ancien bourgmestre"},
    {"nom": "Cécile Jodogne", "conseiller_communal": "2001-présent", "echevin": "2006-2014", "bourgmestre": "2008-2011 (faisant fonction), 2019-2024 (faisant fonction)", "statut": "Conseillère communale"},
    {"nom": "Laure Lita", "conseiller_communal": "2024-présent", "echevin": None, "bourgmestre": None, "statut": "Conseiller communal"},
]
EXTRAS = [
    {"nom": "David Neuprez", "actif": True, "fonctions": [{"intitule": "secrétaire communal"}]},
    {"nom": "Laure Lita", "fonctions": [{"intitule": "échevin", "du": "2025-01-01"}]},
]


def seg(start: float, speaker: str, text: str, chunk: int = 0) -> dict:
    return {"start": start, "end": start + 4.0, "speaker": speaker, "text": text, "chunk_index": chunk}


class RosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.elus = Path(self.directory.name) / "elus.json"
        self.extras = Path(self.directory.name) / "extras.json"
        self.elus.write_text(json.dumps(ELUS), encoding="utf-8")
        self.extras.write_text(json.dumps(EXTRAS), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_mandate_years(self) -> None:
        self.assertEqual(mandate_years("2000-2007, 2012-2013, 2018-présent"), [(2000, 2007), (2012, 2013), (2018, 9999)])
        self.assertEqual(mandate_years(None), [])

    def test_roster_depends_on_the_year_and_merges_both_files(self) -> None:
        active, former = load_roster(date(2026, 6, 1), self.elus, self.extras)
        by_name = {person.name: person for person in active}
        self.assertIn("Georges Verzin", by_name)
        self.assertEqual([person.name for person in former], ["Leticia Sere"])  # mandat terminé en 2024
        self.assertIn("échevin", by_name["Laure Lita"].functions)  # cumul des deux fichiers
        self.assertIn("secrétaire communal", by_name["David Neuprez"].functions)
        self.assertTrue(by_name["Hasan Koyuncu"].president)
        self.assertEqual(len([p for p in active if p.name == "Laure Lita"]), 1)

        earlier, earlier_former = load_roster(date(2021, 6, 1), self.elus, self.extras)
        self.assertIn("Leticia Sere", [person.name for person in earlier])
        self.assertNotIn("Isabelle Durant", [person.name for person in earlier])

    def test_missing_files_mean_no_roster(self) -> None:
        self.assertFalse(roster_available(Path("/nonexistent/a.json"), Path("/nonexistent/b.json")))
        self.assertTrue(roster_available(self.elus, Path("/nonexistent/b.json")))

    def test_who_is_the_bourgmestre_depends_on_date_and_impediment(self) -> None:
        active, _ = load_roster(date(2026, 6, 1), self.elus, self.extras)
        self.assertEqual([p.name for p in _mayor_candidates(active, "Monsieur le Bourgmestre")], ["Martin de Brabant"])  # titulaire empêchée
        self.assertEqual([p.name for p in _mayor_candidates(active, "Monsieur le Bourgmestre faisant fonction")], ["Martin de Brabant"])
        older, _ = load_roster(date(2019, 6, 1), self.elus, self.extras)
        # 2019 : Clerfayt est empêché, Cécile Jodogne exerce comme bourgmestre faisant fonction
        self.assertEqual([p.name for p in _mayor_candidates(older, "Monsieur le Bourgmestre")], ["Cécile Jodogne"])
        self.assertEqual([p.name for p in _mayor_candidates(older, "le bourgmestre faisant fonction")], ["Cécile Jodogne"])

    def test_phonetic_key_groups_misheard_spellings(self) -> None:
        for spelling in ("Belkattir", "Belkatir", "Belkhatir", "Belkattire"):
            self.assertEqual(phonetic(spelling), phonetic("Belkhatir"), spelling)
        self.assertEqual(phonetic("Durand"), phonetic("Durant"))
        self.assertEqual(phonetic("Deguide"), phonetic("Deguid"))  # « e » final puis consonne finale muette


class AnalyzeNamesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.elus = Path(self.directory.name) / "elus.json"
        self.extras = Path(self.directory.name) / "extras.json"
        self.elus.write_text(json.dumps(ELUS), encoding="utf-8")
        self.extras.write_text(json.dumps(EXTRAS), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def analyze(self, segments: list[dict]) -> dict:
        return analyze_names(segments, date(2026, 6, 1), self.elus, self.extras)

    def test_misheard_names_are_matched_and_the_spoken_text_is_kept(self) -> None:
        segments = [
            seg(0, "A", "Madame Belkattire, vous avez la parole."),
            seg(10, "B", "Merci Monsieur Verzin pour cette question."),
            seg(20, "A", "Madame Durand pour le groupe Ecolo."),
        ]
        original = [item["text"] for item in segments]
        result = self.analyze(segments)

        self.assertEqual([item["text"] for item in segments], original)  # jamais modifié
        self.assertEqual(segments[0]["text_normalized"], "Madame Belkhatir, vous avez la parole.")
        self.assertEqual(segments[2]["text_normalized"], "Madame Durant pour le groupe Ecolo.")
        self.assertNotIn("text_normalized", segments[1])  # « Verzin » était déjà juste
        self.assertEqual(segments[0]["mentions"][0]["person"], "Naïma Belkhatir")
        self.assertEqual(segments[0]["mentions"][0]["level"], "auto")
        self.assertEqual(result["stats"]["reconnues"], 3)
        self.assertEqual(result["stats"]["inconnues"], 0)
        names = {row["nom"]: row for row in result["summary"]}
        self.assertEqual(names["Naïma Belkhatir"]["variantes"], {"Belkattire": 1})

    def test_unknown_names_and_role_words_are_not_forced_onto_someone(self) -> None:
        segments = [seg(0, "A", "Madame Seigneur a la parole ce soir."), seg(5, "B", "Merci Monsieur l'échevin."), seg(9, "A", "Monsieur le Président.")]
        result = self.analyze(segments)
        self.assertEqual(result["stats"]["reconnues"], 0)
        self.assertEqual([row["nom"] for row in result["unknown"]], ["Seigneur"])
        self.assertNotIn("mentions", segments[1])  # « l'échevin » est un titre, pas un nom
        self.assertNotIn("mentions", segments[2])

    def test_former_members_are_only_proposed_never_corrected(self) -> None:
        segments = [seg(0, "A", "Madame Sierre, vous avez la parole.")]
        result = self.analyze(segments)
        mention = segments[0]["mentions"][0]
        self.assertEqual(mention["person"], "Leticia Sere")
        self.assertEqual(mention["level"], "verifier")
        self.assertTrue(mention["former_member"])
        self.assertNotIn("text_normalized", segments[0])
        self.assertTrue(result["summary"][0]["ancien_elu"])

    def test_close_names_are_flagged_instead_of_guessed(self) -> None:
        segments = [seg(0, "A", "Monsieur Van Alweyns.")]
        self.analyze(segments)
        mention = segments[0]["mentions"][0]
        self.assertNotEqual(mention["level"], "inconnu")
        if mention["level"] == "auto":  # ne doit pas être « sûr » si deux élus sont aussi proches
            self.assertNotIn("candidates", mention)
        else:
            self.assertEqual(mention["level"], "verifier")

    def test_csv_lists_recognised_and_unknown_names(self) -> None:
        segments = [seg(3661, "A", "Madame Belkattire, vous avez la parole."), seg(3700, "A", "Madame Belkatir.")]
        segments += [seg(3800, "B", "Madame Seigneur.")]
        csv_text = names_csv(self.analyze(segments))
        self.assertTrue(csv_text.startswith("﻿"))
        self.assertIn("Naïma Belkhatir;conseiller communal;2;", csv_text)
        self.assertIn("Belkattire (1) | Belkatir (1)", csv_text)
        self.assertIn("01:01:01", csv_text)
        self.assertIn("Seigneur;;1", csv_text)


class CueSuggestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.elus = Path(self.directory.name) / "elus.json"
        self.extras = Path(self.directory.name) / "extras.json"
        self.elus.write_text(json.dumps(ELUS), encoding="utf-8")
        self.extras.write_text(json.dumps(EXTRAS), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def suggest(self, segments: list[dict]) -> dict[str, dict]:
        items = cue_suggestions(segments, date(2026, 6, 1), self.elus, self.extras)
        return {item["label"]: item for item in items}

    def test_floor_handover_names_the_next_voice(self) -> None:
        segments = [
            seg(0, "Voix 01", "Madame Durand, vous avez la parole."),
            seg(6, "Voix 02", "Merci, je voulais parler du budget."),
            seg(60, "Voix 01", "Monsieur Verzin, vous avez la parole."),
            seg(66, "Voix 03", "Merci Monsieur le Président."),
            seg(120, "Voix 01", "Vous avez la parole, Madame Belkattire."),
            seg(126, "Voix 04", "Oui, merci."),
        ]
        found = self.suggest(segments)
        self.assertEqual(found["Voix 02"]["name"], "Isabelle Durant")
        self.assertEqual(found["Voix 03"]["name"], "Georges Verzin")
        self.assertEqual(found["Voix 04"]["name"], "Naïma Belkhatir")
        self.assertEqual(found["Voix 01"]["name"], "Hasan Koyuncu")  # a donné la parole 3 fois
        self.assertEqual(found["Voix 01"]["role"], "président du conseil communal")
        self.assertEqual(found["Voix 02"]["source"], "règles")
        self.assertIn("Madame Durand, vous avez la parole", found["Voix 02"]["evidence"])

    def test_thanks_name_the_voice_that_just_spoke(self) -> None:
        segments = [seg(0, "Voix 02", "Le budget est équilibré."), seg(8, "Voix 01", "Merci Monsieur Verzin pour cette réponse.")]
        self.assertEqual(self.suggest(segments)["Voix 02"]["name"], "Georges Verzin")

    def test_bourgmestre_is_resolved_with_the_date(self) -> None:
        segments = [seg(0, "Voix 01", "La parole est à Monsieur le Bourgmestre."), seg(6, "Voix 02", "Merci.")]
        self.assertEqual(self.suggest(segments)["Voix 02"]["name"], "Martin de Brabant")

    def test_contradicting_clues_are_not_decided(self) -> None:
        segments = [
            seg(0, "Voix 01", "Madame Durand, vous avez la parole."),
            seg(6, "Voix 02", "Bonjour."),
            seg(60, "Voix 01", "Monsieur Verzin, vous avez la parole."),
            seg(66, "Voix 02", "Bonjour."),
        ]
        self.assertNotIn("Voix 02", self.suggest(segments))

    def test_no_roster_no_suggestion(self) -> None:
        self.elus.write_text("[]", encoding="utf-8")
        self.extras.write_text("[]", encoding="utf-8")
        segments = [seg(0, "Voix 01", "Madame Durand, vous avez la parole."), seg(6, "Voix 02", "Merci.")]
        self.assertEqual(self.suggest(segments), {})


class AliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.elus = Path(self.directory.name) / "elus.json"
        self.extras = Path(self.directory.name) / "extras.json"
        self.elus.write_text(json.dumps(ELUS + [
            {"nom": "Chloé Deguide", "conseiller_communal": "2024-présent", "echevin": None, "bourgmestre": None, "statut": "Conseiller communal"},
        ]), encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def analyze(self, text: str, with_alias: bool) -> tuple[dict, dict]:
        self.extras.write_text(json.dumps([{"nom": "Chloé Deguide", "alias": ["Duguid"]}] if with_alias else []), encoding="utf-8")
        segments = [seg(0, "A", text)]
        return analyze_names(segments, date(2026, 6, 1), self.elus, self.extras), segments[0]

    def test_a_confirmed_alias_is_matched_exactly_and_corrected(self) -> None:
        result, segment = self.analyze("Madame Duguid, vous avez la parole.", with_alias=True)
        mention = segment["mentions"][0]
        self.assertEqual((mention["person"], mention["level"], mention["score"]), ("Chloé Deguide", "auto", 1.0))
        self.assertEqual(segment["text_normalized"], "Madame Deguide, vous avez la parole.")
        self.assertEqual(segment["text"], "Madame Duguid, vous avez la parole.")
        self.assertEqual(result["summary"][0]["variantes"], {"Duguid": 1})

    def test_without_the_alias_a_distorted_name_is_not_corrected_blindly(self) -> None:
        _, segment = self.analyze("Madame Duguid, vous avez la parole.", with_alias=False)
        self.assertNotEqual(segment["mentions"][0].get("level"), "auto")
        self.assertNotIn("text_normalized", segment)

    def test_alias_does_not_capture_other_names(self) -> None:
        _, segment = self.analyze("Monsieur Verzin, vous avez la parole.", with_alias=True)
        self.assertEqual(segment["mentions"][0]["person"], "Georges Verzin")

    def test_aliases_are_merged_across_files_and_the_voice_is_suggested(self) -> None:
        self.extras.write_text(json.dumps([{"nom": "Chloé Deguide", "alias": ["Duguid"], "fonctions": [{"intitule": "échevin", "du": "2025-01-01"}]}]), encoding="utf-8")
        active, _ = load_roster(date(2026, 6, 1), self.elus, self.extras)
        person = next(item for item in active if item.name == "Chloé Deguide")
        self.assertEqual(person.aliases, ["Duguid"])
        self.assertIn("échevin", person.functions)
        segments = [seg(0, "Voix 01", "Madame Duguid, vous avez la parole."), seg(6, "Voix 02", "Merci.")]
        found = cue_suggestions(segments, date(2026, 6, 1), self.elus, self.extras)
        self.assertEqual(found[0]["name"], "Chloé Deguide")


class VideoDateTests(unittest.TestCase):
    def test_parse_video_date(self) -> None:
        self.assertEqual(parse_video_date("2026-06-01"), date(2026, 6, 1))
        self.assertIsNone(parse_video_date(None))
        self.assertIsNone(parse_video_date("pas une date"))


class NamesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, base_url="http://localhost")
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        (root / "referentiel").mkdir()
        (root / "referentiel" / "elus_mandats.json").write_text(json.dumps(ELUS), encoding="utf-8")
        self.root = root

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _job(self, job_id: str) -> None:
        output = self.root / "jobs" / job_id / "outputs"
        store.create({
            "id": job_id, "status": "completed", "title": "Séance", "diarize": True, "video_date": "2026-06-01",
            "output_dir": str(output), "files": [], "speaker_names": {"Voix 01": "Voix 01", "Voix 02": "Voix 02"},
            "segments": [
                seg(0, "Voix 01", "Madame Belkattire, vous avez la parole."),
                seg(6, "Voix 02", "Merci, Monsieur Verzin a raison."),
            ],
        })
        output.mkdir(parents=True)

    def test_analysis_adds_the_csv_without_touching_the_text(self) -> None:
        with (
            patch("app.store.JOBS_DIR", self.root / "jobs"),
            patch("app.people.ELUS_FILE", self.root / "referentiel" / "elus_mandats.json"),
            patch("app.people.EXTRAS_FILE", self.root / "referentiel" / "absent.json"),
        ):
            self._job("f" * 32)
            response = self.client.post("/api/jobs/" + "f" * 32 + "/names")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["stats"]["reconnues"], 2)
            job = store.get("f" * 32)
            self.assertIn("noms_cites.csv", [item["name"] for item in job["files"]])
            self.assertTrue((self.root / "jobs" / ("f" * 32) / "outputs" / "noms_cites.csv").exists())
            self.assertEqual(job["segments"][0]["text"], "Madame Belkattire, vous avez la parole.")
            self.assertEqual(job["segments"][0]["text_normalized"], "Madame Belkhatir, vous avez la parole.")

    def test_analysis_needs_a_roster(self) -> None:
        with (
            patch("app.store.JOBS_DIR", self.root / "jobs"),
            patch("app.people.ELUS_FILE", self.root / "referentiel" / "absent.json"),
            patch("app.people.EXTRAS_FILE", self.root / "referentiel" / "absent2.json"),
        ):
            self._job("e" * 32)
            self.assertEqual(self.client.post("/api/jobs/" + "e" * 32 + "/names").status_code, 409)

    def test_suggestions_work_without_api_key_thanks_to_the_rules(self) -> None:
        with (
            patch("app.store.JOBS_DIR", self.root / "jobs"),
            patch("app.people.ELUS_FILE", self.root / "referentiel" / "elus_mandats.json"),
            patch("app.people.EXTRAS_FILE", self.root / "referentiel" / "absent.json"),
            patch("app.main.get_openai_api_key", return_value=None),
        ):
            self._job("d" * 32)
            response = self.client.post("/api/jobs/" + "d" * 32 + "/speakers/suggest")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            by_label = {item["label"]: item for item in body["suggestions"]}
            self.assertEqual(body["by_rules"], 2)
            self.assertEqual(by_label["Voix 02"]["name"], "Naïma Belkhatir")  # « Madame Belkattire, vous avez la parole »
            self.assertEqual(by_label["Voix 01"]["name"], "Georges Verzin")  # « Merci, Monsieur Verzin… » (indice plus faible)
            self.assertEqual(by_label["Voix 02"]["source"], "règles")

    def test_page_has_the_names_button(self) -> None:
        self.assertIn('id="analyzeNames"', self.client.get("/").text)


class PipelineNamesTests(unittest.TestCase):
    def test_names_are_never_analysed_by_default_even_with_a_roster(self) -> None:
        from app.pipeline import TranscriptionPipeline
        from app.store import JobStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            roster = root / "elus.json"
            roster.write_text(json.dumps(ELUS), encoding="utf-8")
            audio = root / "source.m4a"
            audio.write_bytes(b"audio")
            chunk = root / "chunk_000.mp3"
            chunk.write_bytes(b"c")
            with (
                patch("app.store.JOBS_DIR", jobs_dir),
                patch("app.pipeline.JOBS_DIR", jobs_dir),
                patch("app.people.ELUS_FILE", roster),
                patch("app.people.EXTRAS_FILE", root / "absent.json"),
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key-for-unit-tests-only"}),
            ):
                job_store = JobStore()
                job_id = "7" * 32
                job_store.create({
                    "id": job_id, "url": "https://youtu.be/euuLzgjj7z4", "video_identity": "euuLzgjj7z4", "title": "t",
                    "duration": 60.0, "video_date": "2026-06-01", "status": "queued", "progress": 0, "message": "",
                    "diarize": False, "chunk_minutes": 10, "api_concurrency": 1, "segments": [], "speaker_names": {}, "files": [],
                })
                pipeline = TranscriptionPipeline(job_store)
                text = [{"start": 0.0, "end": 3.0, "speaker": "Intervenant", "text": "Madame Belkattire, vous avez la parole.", "chunk_index": 0}]
                with (
                    patch.object(pipeline, "_existing_audio", return_value=audio),
                    patch.object(pipeline, "_split_audio", return_value=[chunk]),
                    patch.object(pipeline, "_duration", return_value=60.0),
                    patch.object(pipeline, "_transcribe_chunk", return_value=text),
                    patch("app.pipeline.OpenAI", return_value=object()),
                ):
                    pipeline._run(job_id)
                result = job_store.get(job_id)
                self.assertEqual(result["status"], "completed")
                # À la demande seulement : aucune analyse, aucun fichier supplémentaire, texte intact
                self.assertNotIn("names_analysis", result)
                self.assertNotIn("noms_cites.csv", [item["name"] for item in result["files"]])
                self.assertNotIn("mentions", result["segments"][0])
                self.assertNotIn("text_normalized", result["segments"][0])
                self.assertEqual(result["segments"][0]["text"], "Madame Belkattire, vous avez la parole.")
                pipeline.shutdown()


if __name__ == "__main__":
    unittest.main()
