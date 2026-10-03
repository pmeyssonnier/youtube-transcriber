from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app, inspections
from app.pipeline import TranscriptionPipeline
from app.store import JobStore
from app.timerange import effective_duration, validate_time_range


class TimeRangeTests(unittest.TestCase):
    def test_empty_and_full_ranges_mean_no_limit(self) -> None:
        self.assertEqual(validate_time_range(None, None, 3600), (None, None))
        self.assertEqual(validate_time_range(0, 3600, 3600), (None, None))
        self.assertEqual(validate_time_range(0.4, 3599.5, 3600), (None, None))

    def test_valid_range(self) -> None:
        self.assertEqual(validate_time_range(1200, 3000, 3600), (1200.0, 3000.0))
        self.assertEqual(validate_time_range(1200, None, None), (1200.0, None))

    def test_invalid_ranges(self) -> None:
        for start, end, duration in (
            (4000, None, 3600),
            (100, 102, 3600),
            (None, 2, 3600),
            (100, 90, 3600),
            (None, 300, None),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                validate_time_range(start, end, duration)

    def test_effective_duration(self) -> None:
        self.assertEqual(effective_duration(1200, 3000, 3600), 1800)
        self.assertEqual(effective_duration(1200, None, 3600), 2400)
        self.assertEqual(effective_duration(None, None, 3600), 3600)
        self.assertIsNone(effective_duration(1200, None, None))


class RangeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app, base_url="http://localhost")

    def _inspection(self, duration: float | None) -> str:
        inspection_id = "a1" * 16
        inspections[inspection_id] = {
            "id": inspection_id,
            "video_identity": "euuLzgjj7z4",
            "title": "Séance",
            "duration": duration,
            "url": "https://youtu.be/euuLzgjj7z4",
            "cookie_browser": None,
            "chunk_minutes": 10,
            "created_monotonic": __import__("time").monotonic(),
            "requires_confirmation": True,
        }
        return inspection_id

    def _post(self, inspection_id: str, **extra):
        body = {"url": "https://youtu.be/euuLzgjj7z4", "inspection_id": inspection_id, "chunk_minutes": 10, **extra}
        return self.client.post("/api/jobs", json=body)

    def test_range_is_validated_stored_and_reduces_confirmation_need(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("app.store.JOBS_DIR", Path(directory)),
            patch("app.main.get_openai_api_key", return_value="sk-test-key-for-unit-tests-only"),
            patch("app.main.pipeline.submit", return_value=True),
        ):
            # 6 h de vidéo : sans plage, la confirmation est exigée
            response = self._post(self._inspection(22200.0))
            self.assertEqual(response.status_code, 409)

            # plage de 40 min : plus de confirmation nécessaire
            response = self._post(self._inspection(22200.0), start_seconds=1200, end_seconds=3600)
            self.assertEqual(response.status_code, 202)
            body = response.json()
            self.assertEqual((body["start_seconds"], body["end_seconds"]), (1200.0, 3600.0))

    def test_invalid_range_is_rejected(self) -> None:
        with patch("app.main.get_openai_api_key", return_value="sk-test-key-for-unit-tests-only"):
            response = self._post(self._inspection(600.0), start_seconds=700)
        self.assertEqual(response.status_code, 400)

    def test_csp_allows_only_youtube_player_hosts(self) -> None:
        response = self.client.get("/api/health")
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self' https://www.youtube.com;", csp)
        self.assertIn("frame-src https://www.youtube-nocookie.com https://www.youtube.com;", csp)
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")


class PipelineRangeTests(unittest.TestCase):
    def test_split_uses_seek_and_length_and_invalidates_old_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "source.m4a"
            audio.write_bytes(b"audio")
            chunks_dir = root / "chunks"
            results_dir = root / "results"
            chunks_dir.mkdir()
            results_dir.mkdir()
            store = JobStore()
            pipeline = TranscriptionPipeline(store)
            commands: list[list[str]] = []

            def fake_run(command, message, timeout=None):
                commands.append(command)
                (chunks_dir / "chunk_000.mp3").write_bytes(b"x")
                return ""

            with (
                patch.object(pipeline, "_duration", return_value=7200.0),
                patch.object(pipeline, "_run_command", side_effect=fake_run),
            ):
                pipeline._split_audio(audio, chunks_dir, results_dir, 600, 1200.0, 3000.0)
                command = commands[-1]
                self.assertLess(command.index("-ss"), command.index("-i"))
                self.assertEqual(command[command.index("-ss") + 1], "1200.000")
                self.assertEqual(command[command.index("-t") + 1], "1800.000")

                # même plage : les parties existantes sont réutilisées (pas de nouvel appel)
                before = len(commands)
                pipeline._split_audio(audio, chunks_dir, results_dir, 600, 1200.0, 3000.0)
                self.assertEqual(len(commands), before)

                # plage différente : découpage refait, anciens résultats supprimés
                (results_dir / "chunk_000.json").write_text("{}")
                pipeline._split_audio(audio, chunks_dir, results_dir, 600, 60.0, None)
                self.assertGreater(len(commands), before)
                self.assertFalse((results_dir / "chunk_000.json").exists())
                self.assertNotIn("-t", commands[-1])
            pipeline.shutdown()

    def test_timestamps_are_shifted_by_range_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            audio = root / "source.m4a"
            audio.write_bytes(b"audio")
            chunks = []
            for index in range(2):
                chunk = root / f"chunk_{index:03d}.mp3"
                chunk.write_bytes(b"c")
                chunks.append(chunk)
            with (
                patch("app.store.JOBS_DIR", jobs_dir),
                patch("app.pipeline.JOBS_DIR", jobs_dir),
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key-for-unit-tests-only"}),
            ):
                store = JobStore()
                job_id = "9" * 32
                store.create(
                    {
                        "id": job_id, "url": "https://youtu.be/euuLzgjj7z4", "video_identity": "euuLzgjj7z4",
                        "title": "t", "duration": 7200.0, "status": "queued", "progress": 0, "message": "",
                        "diarize": False, "chunk_minutes": 10, "api_concurrency": 1,
                        "start_seconds": 1200.0, "end_seconds": None,
                        "segments": [], "speaker_names": {}, "files": [],
                    }
                )
                pipeline = TranscriptionPipeline(store)
                segment = lambda _c, _chunk, index, _d, _l=None: [
                    {"start": 5.0, "end": 6.0, "speaker": "Intervenant", "text": f"p{index}", "chunk_index": index}
                ]
                with (
                    patch.object(pipeline, "_existing_audio", return_value=audio),
                    patch.object(pipeline, "_split_audio", return_value=chunks) as split,
                    patch.object(pipeline, "_duration", return_value=600.0),
                    patch.object(pipeline, "_transcribe_chunk", side_effect=segment),
                    patch("app.pipeline.OpenAI", return_value=object()),
                ):
                    pipeline._run(job_id)
                self.assertEqual(split.call_args.args[4:], (1200.0, None, 0.0))
                result = store.get(job_id)
                self.assertEqual([item["start"] for item in result["segments"]], [1205.0, 1805.0])
                pipeline.shutdown()


if __name__ == "__main__":
    unittest.main()
