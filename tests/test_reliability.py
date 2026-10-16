from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.exporters import write_exports, write_files_together
from app.main import app, store
from app.pipeline import TranscriptionPipeline, file_sha256
from app.stitching import stitch_segments
from app.store import JobStore


class ExportRollbackTests(unittest.TestCase):
    """Un remplacement qui échoue au milieu remet tout l'ancien jeu d'exports (vérification R2)."""

    def export(self, directory: Path, text: str) -> None:
        write_exports(directory, {"title": "x"}, [{"start": 0.0, "end": 2.0, "speaker": "P01-A", "text": text}], {"P01-A": "A"})

    def snapshot(self, directory: Path) -> dict[str, str]:
        return {path.name: path.read_text(encoding="utf-8") for path in sorted(directory.iterdir())}

    def test_failure_at_any_replacement_leaves_the_previous_set_intact(self) -> None:
        for failing_call in range(1, 6):
            with self.subTest(failing_call=failing_call), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                self.export(directory, "ancien texte")
                before = self.snapshot(directory)
                original = Path.replace
                calls = {"count": 0}

                def replace(path, target):
                    calls["count"] += 1
                    if calls["count"] == failing_call:
                        raise PermissionError("fichier ouvert dans un autre programme")
                    return original(path, target)

                with patch.object(Path, "replace", replace):
                    with self.assertRaises(PermissionError):
                        self.export(directory, "NOUVEAU texte")
                self.assertEqual(self.snapshot(directory), before)  # ni mélange, ni fichier temporaire ou .bak oublié

    def test_first_export_that_fails_leaves_nothing_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original = Path.replace
            calls = {"count": 0}

            def replace(path, target):
                calls["count"] += 1
                if calls["count"] == 3:
                    raise PermissionError("verrouillé")
                return original(path, target)

            with patch.object(Path, "replace", replace):
                with self.assertRaises(PermissionError):
                    self.export(directory, "premier")
            self.assertEqual(list(directory.iterdir()), [])

    def test_a_successful_rewrite_replaces_everything_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.export(directory, "ancien texte")
            self.export(directory, "NOUVEAU texte")
            after = self.snapshot(directory)
            self.assertEqual(len(after), 5)
            self.assertTrue(all("NOUVEAU" in content for content in after.values()))
            write_files_together(directory, {"a.txt": "1"})
            self.assertFalse(any(name.endswith((".bak", ".tmp")) for name in self.snapshot(directory)))


class WordsAtTheJunctionTests(unittest.TestCase):
    """Le temps couvert ne suffit pas : ce qui a été dit doit l'être aussi (vérification R1)."""

    WINDOWS = [(0.0, 60.0), (40.0, 100.0)]

    def test_an_interjection_heard_by_one_part_only_is_not_lost(self) -> None:
        first = [{"start": 0.0, "end": 60.0, "speaker": "A1", "text": "nous allons passer au point suivant de l'ordre du jour"}]
        second = [
            {"start": 40.0, "end": 50.0, "speaker": "B2", "text": "je demande une suspension de séance"},
            {"start": 50.0, "end": 100.0, "speaker": "B1", "text": "nous allons passer au point suivant de l'ordre du jour budget"},
        ]
        segments, info = stitch_segments([first, second], self.WINDOWS, relabel=False)
        texts = [item["text"] for item in segments]
        self.assertTrue(any("suspension de séance" in text for text in texts), texts)
        rescued = next(item for item in segments if "suspension" in item["text"])
        self.assertTrue(rescued["stitch_uncertain"])
        self.assertGreaterEqual(info["rescued_words"], 1)

    def test_the_same_sentence_slightly_misheard_is_not_copied_twice(self) -> None:
        first = [{"start": 0.0, "end": 60.0, "speaker": "A1", "text": "le conseil approuve le budget présenté par le collège"}]
        second = [
            {"start": 45.0, "end": 58.0, "speaker": "B1", "text": "le conseil approuve le budget presente par le collège"},
            {"start": 60.0, "end": 100.0, "speaker": "B1", "text": "suite des débats sur la mobilité"},
        ]
        segments, info = stitch_segments([first, second], self.WINDOWS, relabel=False)
        self.assertEqual(sum(1 for item in segments if "budget" in item["text"]), 1)
        self.assertEqual(info["rescued_words"], 0)

    def test_noisy_recognition_does_not_flood_the_result_with_copies(self) -> None:
        rnd = random.Random(3)
        copies = 0
        for _ in range(300):
            words = [f"mot{rnd.randrange(10**5):05d}x" for _ in range(30)]
            noisy = [w if rnd.random() > 0.12 else f"err{rnd.randrange(10**5)}" for w in words]
            first = [{"start": 0.0, "end": 60.0, "speaker": "A", "text": " ".join(words)}]
            second = [{"start": 42.0, "end": 52.0, "speaker": "B", "text": " ".join(noisy[:12])},
                      {"start": 60.0, "end": 100.0, "speaker": "B", "text": "fin"}]
            segments, _ = stitch_segments([first, second], self.WINDOWS, relabel=False)
            copies += sum(1 for item in segments if item["text"].startswith(noisy[0]) and item["start"] == 42.0)
        self.assertLess(copies, 15, "plus de 5 % de phrases recopiées malgré une reconnaissance à 12 % d'erreurs")

    def test_empty_text_is_never_rescued(self) -> None:
        first = [{"start": 0.0, "end": 60.0, "speaker": "A", "text": "bonjour à tous"}]
        second = [{"start": 42.0, "end": 52.0, "speaker": "B", "text": ""}, {"start": 60.0, "end": 100.0, "speaker": "B", "text": "suite"}]
        segments, info = stitch_segments([first, second], self.WINDOWS, relabel=False)
        self.assertEqual(info["rescued_words"], 0)


class ChunkHashTests(unittest.TestCase):
    """Un résultat sauvegardé n'est réutilisé que pour les mêmes octets audio (vérification R3)."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.pipeline = TranscriptionPipeline(JobStore())
        self.chunk = self.root / "chunk_000.mp3"
        self.chunk.write_bytes(b"audio original")
        self.window = (0.0, 600.0)

    def tearDown(self) -> None:
        self.pipeline.shutdown()
        self.directory.cleanup()

    def save(self, **extra) -> None:
        data = {
            "version": 1, "chunk_index": 0, "model": "m", "language": "fr", "duration": 600.0, "offset": 0.0,
            "segments": [{"start": 0.0, "end": 5.0, "speaker": "P01-A", "text": "bonjour"}], **extra,
        }
        self.pipeline._write_json_atomic(self.pipeline._chunk_result_path(self.root, 0), data)

    def load(self):
        return self.pipeline._load_chunk_result(self.root, 0, "m", "fr", self.window, self.chunk)

    def test_same_bytes_are_accepted(self) -> None:
        self.save(chunk_sha256=file_sha256(self.chunk))
        self.assertIsNotNone(self.load())

    def test_other_bytes_with_the_same_size_are_refused(self) -> None:
        self.save(chunk_sha256=file_sha256(self.chunk))
        self.chunk.write_bytes(b"audio origin_l")  # même taille (14 octets), contenu différent
        self.assertEqual(len(b"audio original"), len(b"audio origin_l"))
        self.assertIsNone(self.load())

    def test_result_saved_before_the_hash_existed_is_still_reused(self) -> None:
        self.save()
        self.chunk.write_bytes(b"n'importe quoi")
        self.assertIsNotNone(self.load())

    def test_missing_audio_with_a_recorded_hash_is_refused(self) -> None:
        self.save(chunk_sha256=file_sha256(self.chunk))
        self.chunk.unlink()
        self.assertIsNone(self.load())

    def test_new_results_record_the_hash(self) -> None:
        class Client:
            pass

        with patch("app.pipeline.OpenAI", return_value=Client()), patch.object(
            self.pipeline, "_transcribe_chunk", return_value=[{"start": 0.0, "end": 2.0, "speaker": "P01-A", "text": "salut", "chunk_index": 0}]
        ):
            checkpoint = self.pipeline._transcribe_and_checkpoint(
                "cle", self.chunk, 0, True, "m", "fr", 0.0, 600.0, self.pipeline._chunk_result_path(self.root, 0)
            )
        self.assertEqual(checkpoint["chunk_sha256"], file_sha256(self.chunk))
        self.assertIsNotNone(self.load())


class StaleNamesTests(unittest.TestCase):
    """Changer la date de séance retire les noms corrigés calculés avec l'ancienne (vérification R5)."""

    JOB_ID = "cd" * 16

    def setUp(self) -> None:
        self.client = TestClient(app, base_url="http://localhost")
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "referentiel").mkdir()
        (self.root / "referentiel" / "elus_mandats.json").write_text(
            json.dumps([{"nom": "Jean Verzin", "actif": True, "fonctions": []}]), encoding="utf-8"
        )
        self.patches = [
            patch("app.store.JOBS_DIR", self.root / "jobs"),
            patch("app.people.ELUS_FILE", self.root / "referentiel" / "elus_mandats.json"),
            patch("app.people.EXTRAS_FILE", self.root / "referentiel" / "absent.json"),
        ]
        for item in self.patches:
            item.start()
        self.output = self.root / "jobs" / self.JOB_ID / "outputs"
        store.create({
            "id": self.JOB_ID, "status": "completed", "title": "Séance", "diarize": True, "video_date": "2026-06-01",
            "output_dir": str(self.output), "files": [], "speaker_names": {"V": "V"},
            "segments": [{"start": 0.0, "end": 5.0, "speaker": "V", "text": "Monsieur Verzinn a raison."}],
        })
        self.output.mkdir(parents=True)

    def tearDown(self) -> None:
        for item in self.patches:
            item.stop()
        self.directory.cleanup()

    def exported(self) -> dict:
        return json.loads((self.output / "transcription.json").read_text(encoding="utf-8"))

    def test_changing_the_date_removes_the_old_corrections_everywhere(self) -> None:
        self.client.post(f"/api/jobs/{self.JOB_ID}/names")
        self.assertTrue((self.output / "noms_cites.csv").exists())
        self.assertIn("mentions", store.get(self.JOB_ID)["segments"][0])

        summary = self.client.post(f"/api/jobs/{self.JOB_ID}/session-date", json={"session_date": "2012-05-05"}).json()
        self.assertTrue(summary["names_analysis_stale"])
        job = store.get(self.JOB_ID)
        segment = job["segments"][0]
        self.assertNotIn("mentions", segment)
        self.assertNotIn("text_normalized", segment)
        self.assertEqual(segment["text"], "Monsieur Verzinn a raison.")  # le texte d'origine n'est jamais touché
        self.assertIsNone(job["names_analysis"])
        self.assertFalse((self.output / "noms_cites.csv").exists())
        self.assertNotIn("noms_cites.csv", [item["name"] for item in job["files"]])
        exported = self.exported()
        self.assertEqual(exported["metadata"]["reference_date_used"], "2012-05-05")
        self.assertNotIn("mentions", exported["segments"][0])

    def test_running_the_analysis_again_clears_the_marker(self) -> None:
        self.client.post(f"/api/jobs/{self.JOB_ID}/names")
        self.client.post(f"/api/jobs/{self.JOB_ID}/session-date", json={"session_date": "2012-05-05"})
        stats = self.client.post(f"/api/jobs/{self.JOB_ID}/names").json()["stats"]
        job = store.get(self.JOB_ID)
        self.assertEqual(stats["annee_reference"], 2012)
        self.assertFalse(job["names_analysis_stale"])
        self.assertIn("mentions", job["segments"][0])

    def test_an_unchanged_reference_date_keeps_the_analysis(self) -> None:
        self.client.post(f"/api/jobs/{self.JOB_ID}/session-date", json={"session_date": "2012-05-05"})
        self.client.post(f"/api/jobs/{self.JOB_ID}/names")
        self.client.post(f"/api/jobs/{self.JOB_ID}/session-date", json={"session_date": "2012-05-05"})
        job = store.get(self.JOB_ID)
        self.assertIn("mentions", job["segments"][0])
        self.assertFalse(job.get("names_analysis_stale"))

    def test_a_job_never_analysed_is_not_marked_stale(self) -> None:
        summary = self.client.post(f"/api/jobs/{self.JOB_ID}/session-date", json={"session_date": "2012-05-05"}).json()
        self.assertFalse(summary.get("names_analysis_stale"))


if __name__ == "__main__":
    unittest.main()
