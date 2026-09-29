from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.pipeline import PipelineError, TranscriptionPipeline
from app.store import JobStore


class PipelineTests(unittest.TestCase):
    def test_parallel_results_are_ordered_and_checkpoints_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            source_audio = root / "source.m4a"
            source_audio.write_bytes(b"audio")
            chunks = []
            for index in range(3):
                chunk = root / f"chunk_{index:03d}.mp3"
                chunk.write_bytes(b"chunk")
                chunks.append(chunk)

            with (
                patch("app.store.JOBS_DIR", jobs_dir),
                patch("app.pipeline.JOBS_DIR", jobs_dir),
                patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key-for-unit-tests-only"}),
            ):
                store = JobStore()
                job_id = "a" * 32
                store.create(
                    {
                        "id": job_id,
                        "url": "https://youtu.be/euuLzgjj7z4",
                        "video_identity": "euuLzgjj7z4",
                        "title": "Vidéo de test",
                        "duration": 3.0,
                        "status": "queued",
                        "progress": 0,
                        "message": "En attente…",
                        "diarize": True,
                        "chunk_minutes": 10,
                        "api_concurrency": 3,
                        "segments": [],
                        "speaker_names": {},
                        "files": [],
                    }
                )
                pipeline = TranscriptionPipeline(store)
                active = 0
                maximum_active = 0
                active_lock = threading.Lock()

                def transcribe(_client, _chunk, index, _diarize):
                    nonlocal active, maximum_active
                    with active_lock:
                        active += 1
                        maximum_active = max(maximum_active, active)
                    time.sleep(0.04 * (3 - index))
                    with active_lock:
                        active -= 1
                    return [
                        {
                            "start": 0.0,
                            "end": 0.8,
                            "speaker": f"P{index + 1:02d}-A",
                            "text": f"partie-{index}",
                            "chunk_index": index,
                        }
                    ]

                with (
                    patch.object(pipeline, "_existing_audio", return_value=source_audio),
                    patch.object(pipeline, "_split_audio", return_value=chunks),
                    patch.object(pipeline, "_duration", return_value=1.0),
                    patch.object(pipeline, "_transcribe_chunk", side_effect=transcribe) as transcribe_mock,
                    patch("app.pipeline.OpenAI", return_value=object()),
                ):
                    pipeline._run(job_id)

                result = store.get(job_id)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["progress"], 100)
                self.assertEqual(result["chunks_completed"], 3)
                self.assertEqual(result["checkpointed_chunks"], 3)
                self.assertEqual([item["text"] for item in result["segments"]], ["partie-0", "partie-1", "partie-2"])
                self.assertEqual([item["start"] for item in result["segments"]], [0.0, 1.0, 2.0])
                self.assertGreaterEqual(maximum_active, 2)
                self.assertEqual(transcribe_mock.call_count, 3)
                self.assertEqual(len(result["files"]), 6)

                checkpoint_dir = jobs_dir / job_id / "chunk-results"
                self.assertEqual(len(list(checkpoint_dir.glob("chunk_*.json"))), 3)

                store.update(job_id, status="queued", progress=0)
                with (
                    patch.object(pipeline, "_existing_audio", return_value=source_audio),
                    patch.object(pipeline, "_split_audio", return_value=chunks),
                    patch.object(pipeline, "_duration", return_value=1.0),
                    patch.object(
                        pipeline,
                        "_transcribe_chunk",
                        side_effect=AssertionError("Un checkpoint ne doit pas être refacturé"),
                    ) as second_transcribe,
                ):
                    pipeline._run(job_id)
                self.assertEqual(second_transcribe.call_count, 0)
                self.assertEqual(store.get(job_id)["status"], "completed")
                pipeline.shutdown()

    def _make_job(self, store: JobStore, job_id: str, **overrides) -> None:
        store.create(
            {
                "id": job_id,
                "url": "https://youtu.be/euuLzgjj7z4",
                "video_identity": "euuLzgjj7z4",
                "title": "Vidéo de test",
                "duration": 3.0,
                "status": "queued",
                "progress": 0,
                "message": "En attente…",
                "diarize": True,
                "chunk_minutes": 10,
                "api_concurrency": 1,
                "segments": [],
                "speaker_names": {},
                "files": [],
                **overrides,
            }
        )

    def test_diarization_caps_chunk_length_but_standard_model_does_not(self) -> None:
        for diarize, expected_seconds in ((True, 20 * 60), (False, 30 * 60)):
            with self.subTest(diarize=diarize), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                jobs_dir = root / "jobs"
                jobs_dir.mkdir()
                source_audio = root / "source.m4a"
                source_audio.write_bytes(b"audio")
                with (
                    patch("app.store.JOBS_DIR", jobs_dir),
                    patch("app.pipeline.JOBS_DIR", jobs_dir),
                    patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key-for-unit-tests-only"}),
                ):
                    store = JobStore()
                    job_id = "b" * 32
                    self._make_job(store, job_id, diarize=diarize, chunk_minutes=30)
                    pipeline = TranscriptionPipeline(store)
                    with (
                        patch.object(pipeline, "_existing_audio", return_value=source_audio),
                        patch.object(pipeline, "_split_audio", return_value=[]) as split,
                    ):
                        with self.assertRaises(PipelineError):
                            pipeline._run(job_id)
                    self.assertEqual(split.call_args.args[3], expected_seconds)
                    pipeline.shutdown()

    def test_failed_job_can_be_resubmitted_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jobs_dir = Path(directory) / "jobs"
            jobs_dir.mkdir()
            with patch("app.store.JOBS_DIR", jobs_dir), patch("app.pipeline.JOBS_DIR", jobs_dir):
                store = JobStore()
                job_id = "c" * 32
                self._make_job(store, job_id)
                pipeline = TranscriptionPipeline(store)
                pipeline._submitted.add(job_id)
                submitted_when_failed: list[bool] = []
                original_update = store.update

                def spy(job_id_: str, **changes):
                    if changes.get("status") == "failed":
                        submitted_when_failed.append(job_id_ in pipeline._submitted)
                    return original_update(job_id_, **changes)

                with (
                    patch.object(pipeline, "_run", side_effect=RuntimeError("boom")),
                    patch.object(store, "update", side_effect=spy),
                ):
                    pipeline._run_safely(job_id)

                self.assertEqual(submitted_when_failed, [False])
                self.assertEqual(store.get(job_id)["status"], "failed")
                pipeline.shutdown()


if __name__ == "__main__":
    unittest.main()
