from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import detect_synced_folder
from app.contract import job_metadata
from app.exporters import write_exports
from app.main import app, store
from app.schema import SCHEMA_VERSION

ROOT = Path(__file__).resolve().parent.parent
PLAN = {"duration_seconds": 3600, "chunk_minutes": 10, "diarize": True}


class OriginTests(unittest.TestCase):
    """Seule notre propre origine (schéma, hôte ET port) peut modifier des données (audit P2-10)."""

    def setUp(self) -> None:
        self.client = TestClient(app, base_url="http://localhost:8765")

    def post(self, **headers):
        return self.client.post("/api/plan", json=PLAN, headers=headers)

    def test_own_origin_is_accepted(self) -> None:
        for origin in ("http://localhost:8765", "http://localhost:8765/", "HTTP://LOCALHOST:8765"):
            with self.subTest(origin):
                self.assertEqual(self.post(Origin=origin).status_code, 200)
        loopback = TestClient(app, base_url="http://127.0.0.1:8765")
        self.assertEqual(loopback.post("/api/plan", json=PLAN, headers={"Origin": "http://127.0.0.1:8765"}).status_code, 200)

    def test_another_port_scheme_or_host_is_refused(self) -> None:
        for origin in (
            "http://localhost:3000",        # autre port de la même machine
            "http://localhost",              # port 80
            "http://127.0.0.1:8765",         # même port mais autre nom d'hôte que celui utilisé
            "https://localhost:8765",        # autre schéma
            "null",                          # page locale ou iframe isolée
            "https://malveillant.example",
            "",
        ):
            with self.subTest(origin):
                self.assertEqual(self.post(Origin=origin).status_code, 403)

    def test_fetch_metadata_must_be_same_origin_or_none(self) -> None:
        for site, expected in (("same-origin", 200), ("none", 200), ("same-site", 403), ("cross-site", 403), ("autre", 403)):
            with self.subTest(site):
                self.assertEqual(self.post(**{"Sec-Fetch-Site": site}).status_code, expected)
        # un navigateur qui envoie l'origine exacte mais déclare same-site (autre port) est refusé
        self.assertEqual(self.post(Origin="http://localhost:8765", **{"Sec-Fetch-Site": "same-site"}).status_code, 403)

    def test_requests_without_browser_headers_still_work_for_local_scripts(self) -> None:
        self.assertEqual(self.post().status_code, 200)

    def test_destructive_requests_are_refused_before_anything_happens(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.store.JOBS_DIR", Path(directory)):
            job_id = "ab" * 16
            store.create({"id": job_id, "status": "failed", "url": "https://youtu.be/euuLzgjj7z4"})
            response = self.client.delete(f"/api/jobs/{job_id}", headers={"Origin": "http://localhost:3000"})
            self.assertEqual(response.status_code, 403)
            self.assertIsNotNone(store.get(job_id))
            self.assertEqual(self.client.delete(f"/api/jobs/{job_id}", headers={"Origin": "http://localhost:8765"}).status_code, 200)

    def test_reads_stay_open_and_no_cors_is_granted(self) -> None:
        self.assertEqual(self.client.get("/api/health", headers={"Origin": "http://localhost:3000"}).status_code, 200)
        preflight = self.client.options(
            "/api/plan",
            headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
        )
        self.assertNotIn("access-control-allow-origin", preflight.headers)


class SyncFolderTests(unittest.TestCase):
    def test_detection(self) -> None:
        cases = {
            r"C:\Users\Pierre\OneDrive\Documents\youtube-transcriber": "OneDrive",
            r"C:\Users\Pierre\OneDrive - Consultora sprl\apps": "OneDrive",
            "/home/pierre/Dropbox/apps/t": "Dropbox",
            "/Users/p/Library/Mobile Documents/iCloud~com~x": None,  # nom exact « iCloud » seulement
            "/Users/p/iCloud Drive/apps": "iCloud",
            "/Users/p/Google Drive/apps": "Google Drive",
            r"D:\Transcriptions": None,
            "/home/user/youtube-transcriber": None,
            "/home/user/my-megaphone": None,
        }
        for path, expected in cases.items():
            with self.subTest(path):
                self.assertEqual(detect_synced_folder(Path(path)), expected)

    def test_health_reports_it(self) -> None:
        client = TestClient(app, base_url="http://localhost")
        with patch("app.main.synced_folder_name", return_value="OneDrive"):
            self.assertEqual(client.get("/api/health").json()["sync_folder"], "OneDrive")
        self.assertIn("data_dir", client.get("/api/health").json())


class DataDirectoryTests(unittest.TestCase):
    def read_config(self, **env) -> list[str]:
        script = f"import sys; sys.path.insert(0, {str(ROOT)!r}); from app.config import DATA_DIR, JOBS_DIR; print(DATA_DIR); print(JOBS_DIR)"
        environment = {key: value for key, value in os.environ.items() if key != "TRANSCRIBER_DATA_DIR"}
        environment.update(env)
        result = subprocess.run(
            [sys.executable, "-I", "-c", script], env=environment, capture_output=True, text=True,
            encoding="utf-8", check=True, timeout=60,
        )
        return result.stdout.split("\n")[:2]

    def test_default_and_custom_data_directory(self) -> None:
        self.assertEqual(Path(self.read_config()[0]), ROOT / "data")
        with tempfile.TemporaryDirectory() as directory:
            data, jobs = self.read_config(TRANSCRIBER_DATA_DIR=directory)
            self.assertEqual(Path(data), Path(directory))
            self.assertEqual(Path(jobs), Path(directory) / "jobs")


def validate(value, schema, path="$"):
    """Small JSON Schema checker (type, enum, required, properties, items, minimum, pattern)."""
    import re

    errors = []
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        checks = {
            "string": lambda v: isinstance(v, str), "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool), "boolean": lambda v: isinstance(v, bool),
            "object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list), "null": lambda v: v is None,
        }
        if not any(checks[kind](value) for kind in kinds):
            return [f"{path}: type {type(value).__name__} au lieu de {kinds}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} hors de {schema['enum']}")
    if "pattern" in schema and isinstance(value, str) and not re.search(schema["pattern"], value):
        errors.append(f"{path}: {value!r} ne respecte pas {schema['pattern']}")
    if "minimum" in schema and isinstance(value, (int, float)) and value < schema["minimum"]:
        errors.append(f"{path}: {value} < {schema['minimum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: champ « {key} » absent")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}"))
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for key, item in value.items():
                if key not in schema.get("properties", {}):
                    errors.extend(validate(item, extra, f"{path}.{key}"))
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{index}]"))
    return errors


class JsonContractTests(unittest.TestCase):
    """Le JSON exporté respecte le schéma documenté (audit P3-14)."""

    JOB = {
        "title": "Séance", "url": "https://youtu.be/euuLzgjj7z4", "video_identity": "euuLzgjj7z4", "diarize": True,
        "language": "fr", "speaker_linking": "light", "video_date": "2025-09-16", "session_date": "2025-09-10",
        "stitch_uncertain": 1,
    }
    SEGMENTS = [
        {"start": 0.0, "end": 4.0, "speaker": "Voix 01", "raw_speaker": "P01-A", "text": "Bonjour.", "chunk_index": 0},
        {"start": 4.0, "end": 9.5, "speaker": "Voix 02", "text": "Merci.", "chunk_index": 0, "stitch_uncertain": True,
         "text_normalized": "Merci.", "mentions": []},
    ]

    def export(self, directory: Path, names: dict[str, str]) -> dict:
        write_exports(directory, job_metadata(self.JOB, duration=600.0), self.SEGMENTS, names)
        return json.loads((directory / "transcription.json").read_text(encoding="utf-8"))

    def test_export_matches_the_schema(self) -> None:
        schema = json.loads((ROOT / "docs" / "transcription.schema.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            data = self.export(Path(directory), {"Voix 01": "Le président", "Voix 02": "Voix 02"})
        self.assertEqual(validate(data, schema), [])
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)
        self.assertEqual(data["metadata"]["reference_date_used"], "2025-09-10")
        self.assertEqual(data["metadata"]["reference_date_source"], "saisie")
        self.assertEqual(data["metadata"]["stitch_uncertain_segments"], 1)
        self.assertEqual(data["metadata"]["video_id"], "euuLzgjj7z4")

    def test_the_checker_detects_a_broken_file(self) -> None:
        schema = json.loads((ROOT / "docs" / "transcription.schema.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            data = self.export(Path(directory), {})
        del data["metadata"]["language"]
        data["segments"][0]["start"] = "zéro"
        data["schema_version"] = "2.0"
        problems = " ".join(validate(data, schema))
        self.assertIn("language", problems)
        self.assertIn("start", problems)
        self.assertIn("schema_version", problems)

    def test_renaming_keeps_ids_and_speaker_ids_stable_and_text_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            before = self.export(Path(directory), {"Voix 01": "Voix 01"})
            after = self.export(Path(directory), {"Voix 01": "Le président"})
        for old, new in zip(before["segments"], after["segments"]):
            self.assertEqual((old["id"], old["speaker_id"], old["text"]), (new["id"], new["speaker_id"], new["text"]))
        self.assertEqual(after["segments"][0]["speaker_name"], "Le président")
        self.assertEqual([item["id"] for item in after["segments"]], [1, 2])

    def test_legacy_fields_are_all_still_there(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = self.export(Path(directory), {})
        for key in ("title", "source_url", "duration_seconds", "transcribed_from", "transcribed_to", "model",
                    "speaker_labels_need_review", "stitch_uncertain_segments", "session_date", "publication_date"):
            self.assertIn(key, data["metadata"])
        for key in ("start", "end", "speaker", "text", "speaker_name", "chunk_index"):
            self.assertIn(key, data["segments"][0])

    def test_documentation_is_shipped_and_consistent(self) -> None:
        doc = (ROOT / "docs" / "format-json.md").read_text(encoding="utf-8")
        self.assertIn(f'"schema_version": "{SCHEMA_VERSION}"', doc)
        schema = json.loads((ROOT / "docs" / "transcription.schema.json").read_text(encoding="utf-8"))
        for field in schema["properties"]["segments"]["items"]["properties"]:
            self.assertIn(field, doc, f"« {field} » n'est pas documenté dans docs/format-json.md")


if __name__ == "__main__":
    unittest.main()
