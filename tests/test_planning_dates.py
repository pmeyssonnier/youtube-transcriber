from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.constants import APP_VERSION
from app.dates import SOURCE_MANUAL, SOURCE_NONE, SOURCE_YOUTUBE, date_metadata, reference_date
from app.main import app, store
from app.planning import chunk_seconds_for, plan_all_modes, plan_summary

ROOT = Path(__file__).resolve().parent.parent


class PlanningTests(unittest.TestCase):
    """Les surcoûts affichés viennent du vrai découpage, pas de pourcentages fixes (audit P2-05)."""

    def test_default_ten_minute_parts_cost_more_than_the_old_fixed_labels(self) -> None:
        plans = plan_all_modes(6 * 3600, 10, True)
        self.assertEqual(plans["off"]["extra_percent"], 0.0)
        self.assertEqual(plans["off"]["chunks"], 36)
        # 45 s ajoutées à chaque frontière de 10 min : environ 7,5 %, et non 4 %
        self.assertAlmostEqual(plans["light"]["extra_percent"], 7.3, delta=0.2)
        # 180 s : environ 30 %, et non 15 %
        self.assertAlmostEqual(plans["strong"]["extra_percent"], 29.2, delta=0.3)
        self.assertEqual(plans["light"]["chunks"], 36)

    def test_twenty_minute_parts_are_close_to_the_old_labels(self) -> None:
        plans = plan_all_modes(6 * 3600, 20, True)
        self.assertAlmostEqual(plans["light"]["extra_percent"], 3.5, delta=0.2)
        self.assertAlmostEqual(plans["strong"]["extra_percent"], 15.8, delta=0.3)  # le plafond de 1 300 s réduit le pas et ajoute 2 parties

    def test_audio_cap_shrinks_the_step_and_adds_parts(self) -> None:
        # 1300 s maximum par requête, recouvrement compris : 20 min + 180 s ne rentre pas
        self.assertEqual(chunk_seconds_for(20, True, 180.0), 1120)
        self.assertEqual(chunk_seconds_for(20, True, 45.0), 1200)
        self.assertEqual(chunk_seconds_for(30, False, 0.0), 1800)
        plans = plan_all_modes(2 * 3600, 20, True)
        self.assertEqual(plans["off"]["chunks"], 6)
        self.assertEqual(plans["strong"]["chunks"], 7)  # le plafond ajoute une partie
        self.assertEqual(plans["strong"]["step_seconds"], 1120)

    def test_without_diarization_there_is_no_overlap(self) -> None:
        plan = plan_summary(3600, 10, False, "strong")
        self.assertEqual((plan["overlap_seconds"], plan["extra_seconds"]), (0.0, 0.0))

    def test_plan_endpoint_and_inspection_return_the_same_numbers(self) -> None:
        client = TestClient(app, base_url="http://localhost")
        data = client.post("/api/plan", json={"duration_seconds": 7200, "chunk_minutes": 20, "diarize": True}).json()
        self.assertEqual(data["plans"], plan_all_modes(7200, 20, True))
        bad = client.post("/api/plan", json={"duration_seconds": 0, "chunk_minutes": 20, "diarize": True})
        self.assertEqual(bad.status_code, 422)
        metadata = {"video_identity": "euuLzgjj7z4", "title": "T", "duration": 7200.0, "uploader": None, "video_date": "2025-09-16"}
        with patch("app.main.pipeline.inspect_video", return_value=metadata):
            inspected = client.post(
                "/api/videos/inspect", json={"url": "https://youtu.be/euuLzgjj7z4", "chunk_minutes": 20, "diarize": True}
            ).json()
        self.assertEqual(inspected["plans"], data["plans"])
        self.assertEqual(inspected["video_date"], "2025-09-16")

    def test_page_has_no_fixed_percentages(self) -> None:
        html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("+4 %", html)
        self.assertNotIn("+15 %", html)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("environ +4 %", readme)


class ReferenceDateTests(unittest.TestCase):
    """Date de séance saisie, sinon publication YouTube, sinon année en cours signalée (audit P2-07)."""

    def test_priority_and_source(self) -> None:
        self.assertEqual(reference_date({"session_date": "2020-05-05", "video_date": "2026-06-01"})[1], SOURCE_MANUAL)
        self.assertEqual(reference_date({"session_date": "2020-05-05", "video_date": "2026-06-01"})[0].year, 2020)
        self.assertEqual(reference_date({"video_date": "2026-06-01"})[1], SOURCE_YOUTUBE)
        self.assertEqual(reference_date({"session_date": None, "video_date": None}), (None, SOURCE_NONE))
        self.assertEqual(reference_date({"session_date": "n'importe quoi", "video_date": "2026-06-01"})[1], SOURCE_YOUTUBE)

    def test_metadata_keeps_both_dates(self) -> None:
        meta = date_metadata({"session_date": "2020-05-05", "video_date": "2026-06-01"})
        self.assertEqual(meta["session_date"], "2020-05-05")
        self.assertEqual(meta["publication_date"], "2026-06-01")
        self.assertEqual(meta["reference_date_used"], "2020-05-05")
        self.assertEqual(meta["reference_date_source"], SOURCE_MANUAL)
        self.assertIsNone(date_metadata({})["reference_date_used"])


class SessionDateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, base_url="http://localhost")
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "referentiel").mkdir()
        (self.root / "referentiel" / "elus_mandats.json").write_text(
            json.dumps([{"nom": "Jean Verzin", "actif": True, "fonctions": []}]), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def make_job(self, job_id: str, **extra) -> None:
        output = self.root / "jobs" / job_id / "outputs"
        store.create({
            "id": job_id, "status": "completed", "title": "Séance", "diarize": True, "video_date": "2026-06-01",
            "output_dir": str(output), "files": [], "speaker_names": {"Voix 01": "Voix 01"},
            "segments": [{"start": 0.0, "end": 5.0, "speaker": "Voix 01", "text": "Monsieur Verzin a raison."}],
            **extra,
        })
        output.mkdir(parents=True)

    def test_correcting_the_date_rewrites_the_exports_and_drives_the_roster_year(self) -> None:
        job_id = "a1" * 16
        with (
            patch("app.store.JOBS_DIR", self.root / "jobs"),
            patch("app.people.ELUS_FILE", self.root / "referentiel" / "elus_mandats.json"),
            patch("app.people.EXTRAS_FILE", self.root / "referentiel" / "absent.json"),
        ):
            self.make_job(job_id)
            before = self.client.post(f"/api/jobs/{job_id}/names").json()["stats"]
            self.assertEqual((before["annee_reference"], before["date_source"]), (2026, SOURCE_YOUTUBE))

            response = self.client.post(f"/api/jobs/{job_id}/session-date", json={"session_date": "2020-05-05"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["session_date"], "2020-05-05")
            exported = json.loads((self.root / "jobs" / job_id / "outputs" / "transcription.json").read_text(encoding="utf-8"))
            self.assertEqual(exported["metadata"]["session_date"], "2020-05-05")
            self.assertEqual(exported["metadata"]["publication_date"], "2026-06-01")

            after = self.client.post(f"/api/jobs/{job_id}/names").json()["stats"]
            self.assertEqual((after["annee_reference"], after["date_source"]), (2020, SOURCE_MANUAL))

            # effacer la date revient à la publication YouTube
            self.client.post(f"/api/jobs/{job_id}/session-date", json={"session_date": None})
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/names").json()["stats"]["annee_reference"], 2026)

    def test_invalid_dates_and_unfinished_jobs_are_refused(self) -> None:
        with patch("app.store.JOBS_DIR", self.root / "jobs"):
            self.make_job("b2" * 16)
            self.make_job("c3" * 16, status="transcribing")
            self.assertEqual(self.client.post(f"/api/jobs/{'b2' * 16}/session-date", json={"session_date": "pas une date"}).status_code, 422)
            self.assertEqual(self.client.post(f"/api/jobs/{'b2' * 16}/session-date", json={"session_date": "2026-13-45"}).status_code, 400)
            self.assertEqual(self.client.post(f"/api/jobs/{'c3' * 16}/session-date", json={"session_date": "2020-05-05"}).status_code, 409)
            self.assertEqual(self.client.post(f"/api/jobs/{'d4' * 16}/session-date", json={"session_date": None}).status_code, 404)

    def test_stage_start_is_recorded_only_when_the_status_changes(self) -> None:
        with patch("app.store.JOBS_DIR", self.root / "jobs"):
            store.create({"id": "e5" * 16, "status": "queued"})
            first = store.update("e5" * 16, status="downloading")["stage_started_at"]
            again = store.update("e5" * 16, status="downloading", progress=10)["stage_started_at"]
            self.assertEqual(first, again)
            later = store.update("e5" * 16, status="preparing")["stage_started_at"]
            self.assertGreaterEqual(later, first)


class ConsistencyTests(unittest.TestCase):
    """VERSION, installateur, README et contraintes annoncent la même version (audit P3-12)."""

    def read(self, name: str) -> str:
        return (ROOT / name).read_text(encoding="utf-8")

    def test_version_is_the_same_everywhere(self) -> None:
        self.assertIn(f"**Version : {APP_VERSION}**", self.read("README.md"))
        self.assertIn(f"Installation terminee (version {APP_VERSION})", self.read("installer_windows.ps1"))
        history = self.read("README.md").split("## Historique", 1)[1]
        self.assertTrue(history.lstrip().startswith(f"- **{APP_VERSION}**"), "la dernière version doit ouvrir l'historique")

    def test_documentation_matches_the_code(self) -> None:
        readme = self.read("README.md")
        self.assertNotIn("n’est pas encore fusionné", readme)
        self.assertNotIn("n'est pas encore fusionné", readme)
        self.assertNotIn("\n- il n’y a pas de chevauchement entre les parties", readme)
        constraints = self.read("constraints.txt").splitlines()[0]
        self.assertNotRegex(constraints, r"\d+\.\d+\.\d+", "pas de numéro de version à tenir à jour dans constraints.txt")


@unittest.skipUnless(shutil.which("node"), "Node.js absent")
class DurationFormatTests(unittest.TestCase):
    """formatReadableDuration(null) ne doit pas afficher « 0 min 00 s » (audit P3-13)."""

    def test_missing_duration_is_unknown(self) -> None:
        js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        start = js.index("function formatReadableDuration")
        end = js.index("\n}\n", start) + 3
        # Fichier UTF-8 en entrée et sortie en ASCII (\\uXXXX) : le résultat ne dépend pas de l'encodage
        # de la console (sous Windows, « é » arrivait déformé).
        script = js[start:end] + (
            "\nconst out = [null, undefined, '', 'abc', 0, 65, 3725].map(formatReadableDuration);"
            "\nconsole.log(JSON.stringify(out).replace(/[^\\x00-\\x7f]/g,"
            " (c) => '\\\\u' + c.charCodeAt(0).toString(16).padStart(4, '0')));"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duree.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="ascii", check=True, timeout=30)
        self.assertEqual(
            json.loads(result.stdout),
            ["Durée inconnue", "Durée inconnue", "Durée inconnue", "Durée inconnue", "0 min 00 s", "1 min 05 s", "1 h 02 min"],
        )

    def test_no_blocking_alert_dialogs_remain(self) -> None:
        self.assertNotIn("window.alert", (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
