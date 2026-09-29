from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app, store
from app.speakers import (
    SuggestionError,
    _clean_suggestions,
    naming_contexts,
    request_name_suggestions,
    speaker_stats,
)


def seg(start: float, speaker: str, text: str) -> dict:
    return {"start": start, "end": start + 2.0, "speaker": speaker, "text": text}


SEGMENTS = [
    seg(0, "P01-A", "Je donne la parole à Madame Durand pour la présentation du budget."),
    seg(3, "P01-B", "Merci Monsieur le bourgmestre, je vais présenter le budget de la commune."),
    seg(6, "P01-B", "Les recettes augmentent de trois pour cent cette année."),
    seg(9, "P01-A", "Merci Madame Durand pour cette présentation très claire."),
    seg(12, "P01-B", "Avec plaisir."),
    seg(15, "P01-C", "Oui"),
]


class SpeakerStatsTests(unittest.TestCase):
    def test_sorted_by_words_with_shares_and_samples(self) -> None:
        stats = speaker_stats(SEGMENTS, {"P01-B": "Mme Durand"})
        self.assertEqual([item["label"] for item in stats], ["P01-B", "P01-A", "P01-C"])
        main = stats[0]
        self.assertEqual(main["name"], "Mme Durand")
        self.assertEqual(main["segments"], 3)
        self.assertEqual(main["first_start"], 3.0)
        self.assertTrue(main["sample"].startswith("Merci Monsieur le bourgmestre"))
        self.assertAlmostEqual(sum(item["share"] for item in stats), 1.0, places=2)
        self.assertEqual(stats[2]["sample"], "")  # aucune phrase assez longue

    def test_empty_segments(self) -> None:
        self.assertEqual(speaker_stats([], {}), [])


class NamingContextTests(unittest.TestCase):
    def test_contexts_keep_neighbouring_turns_and_skip_tiny_voices(self) -> None:
        segments = SEGMENTS + [seg(20 + i, "P01-B", "Encore une phrase de plus.") for i in range(3)]
        segments += [seg(40, "P01-A", "Merci."), seg(43, "P01-A", "Suite de la séance.")]
        contexts, skipped = naming_contexts(segments)
        labels = {item["label"] for item in contexts}
        self.assertEqual(labels, {"P01-A", "P01-B"})
        self.assertEqual(skipped, 1)  # P01-C n'a qu'un segment
        voice_b = next(item for item in contexts if item["label"] == "P01-B")
        first = voice_b["extraits"][0]
        self.assertIn("Madame Durand", first["avant"])
        self.assertTrue(first["cette_voix"].startswith("Merci Monsieur le bourgmestre"))

    def test_excerpts_are_bounded(self) -> None:
        long_text = "mot " * 400
        segments = [seg(i * 3, "P01-A" if i % 2 else "P01-B", long_text) for i in range(40)]
        contexts, _ = naming_contexts(segments)
        for item in contexts:
            self.assertLessEqual(len(item["extraits"]), 3)
            for excerpt in item["extraits"]:
                for value in excerpt.values():
                    self.assertLessEqual(len(value), 180)


class SuggestionParsingTests(unittest.TestCase):
    def test_keeps_only_valid_named_labels(self) -> None:
        payload = {
            "suggestions": [
                {"label": "P01-A", "name": "Le bourgmestre", "confidence": "HAUTE", "evidence": "je donne la parole"},
                {"label": "P01-B", "name": None, "confidence": "haute", "evidence": ""},
                {"label": "P99-Z", "name": "Inconnu du fichier", "confidence": "haute", "evidence": ""},
                {"label": "P01-C", "name": "Ligne\nbizarre", "confidence": "n'importe quoi", "evidence": "x"},
                "pas un dict",
            ]
        }
        result = _clean_suggestions(payload, {"P01-A", "P01-B", "P01-C"})
        by_label = {item["label"]: item for item in result}
        self.assertEqual(set(by_label), {"P01-A", "P01-C"})
        self.assertEqual(by_label["P01-A"]["confidence"], "haute")
        self.assertEqual(by_label["P01-C"]["confidence"], "faible")
        self.assertEqual(by_label["P01-C"]["name"], "Ligne bizarre")

    def test_rejects_unreadable_payload(self) -> None:
        with self.assertRaises(SuggestionError):
            _clean_suggestions({"autre": []}, set())

    def test_request_batches_and_parses_model_answer(self) -> None:
        contexts = [{"label": f"P01-{index}", "extraits": []} for index in range(45)]
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            payload = json.loads(kwargs["messages"][1]["content"])
            first = payload["etiquettes"][0]["label"]
            answer = {"suggestions": [{"label": first, "name": "Nom " + first, "confidence": "haute", "evidence": "e"}]}
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result = request_name_suggestions(client, contexts)
        self.assertEqual(len(calls), 3)  # 20 + 20 + 5
        self.assertEqual([item["label"] for item in result], ["P01-0", "P01-20", "P01-40"])
        self.assertEqual(calls[0]["temperature"], 0)
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})

    def test_bad_json_from_model_is_a_readable_error(self) -> None:
        create = lambda **_: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="pas du json"))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with self.assertRaises(SuggestionError):
            request_name_suggestions(client, [{"label": "P01-A", "extraits": []}])


class SpeakerApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, base_url="http://localhost")

    def test_list_and_suggest_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.store.JOBS_DIR", Path(directory)):
            job_id = "e" * 32
            segments = SEGMENTS + [seg(20 + i, "P01-B", "Encore une phrase de plus.") for i in range(3)]
            segments += [seg(40, "P01-A", "Merci."), seg(43, "P01-A", "Suite de la séance.")]
            store.create(
                {
                    "id": job_id,
                    "status": "completed",
                    "title": "Séance",
                    "diarize": True,
                    "segments": segments,
                    "speaker_names": {"P01-A": "P01-A", "P01-B": "P01-B", "P01-C": "P01-C"},
                }
            )
            listing = self.client.get(f"/api/jobs/{job_id}/speakers").json()
            self.assertEqual(listing["speakers"][0]["label"], "P01-B")
            self.assertNotIn("segments", listing)

            suggestion = [{"label": "P01-A", "name": "Le bourgmestre", "confidence": "haute", "evidence": "e"}]
            with (
                patch("app.main.get_openai_api_key", return_value="sk-test-key-for-unit-tests-only"),
                patch("app.main.OpenAI", return_value=object()),
                patch("app.main.request_name_suggestions", return_value=suggestion) as request,
            ):
                response = self.client.post(f"/api/jobs/{job_id}/speakers/suggest")
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["suggestions"], suggestion)
            self.assertEqual(body["analyzed_labels"], 2)
            self.assertEqual(body["skipped_labels"], 1)
            request.assert_called_once()

            with (
                patch("app.main.get_openai_api_key", return_value="sk-test-key-for-unit-tests-only"),
                patch("app.main.OpenAI", return_value=object()),
                patch("app.main.request_name_suggestions", side_effect=SuggestionError("illisible")),
            ):
                self.assertEqual(self.client.post(f"/api/jobs/{job_id}/speakers/suggest").status_code, 502)

            with patch("app.main.get_openai_api_key", return_value=None):
                self.assertEqual(self.client.post(f"/api/jobs/{job_id}/speakers/suggest").status_code, 409)

            store.update(job_id, status="failed")
            self.assertEqual(self.client.get(f"/api/jobs/{job_id}/speakers").status_code, 409)
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/speakers/suggest").status_code, 409)

    def test_suggest_rejects_cross_site_and_unknown_job(self) -> None:
        self.assertEqual(self.client.get("/api/jobs/" + "f" * 32 + "/speakers").status_code, 404)
        response = self.client.post(
            "/api/jobs/" + "f" * 32 + "/speakers/suggest", headers={"Origin": "https://malveillant.example"}
        )
        self.assertEqual(response.status_code, 403)

    def test_page_has_speaker_tools(self) -> None:
        html = self.client.get("/").text
        self.assertIn('id="suggestNames"', html)
        self.assertIn('id="hideMinor"', html)


if __name__ == "__main__":
    unittest.main()
