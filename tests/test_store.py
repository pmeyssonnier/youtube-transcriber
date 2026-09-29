from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.store import DuplicateJobError, JobStore


class StoreTests(unittest.TestCase):
    def test_rejects_duplicate_and_ignores_corrupt_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.store.JOBS_DIR", Path(directory)):
            store = JobStore()
            first_id = "1" * 32
            store.create(
                {
                    "id": first_id,
                    "url": "https://youtu.be/euuLzgjj7z4",
                    "status": "transcribing",
                }
            )
            with self.assertRaises(DuplicateJobError):
                store.create_unique(
                    {
                        "id": "2" * 32,
                        "url": "https://www.youtube.com/watch?v=euuLzgjj7z4",
                        "video_identity": "euuLzgjj7z4",
                        "status": "queued",
                    },
                    {"queued", "transcribing"},
                )

            corrupt_dir = Path(directory) / ("3" * 32)
            corrupt_dir.mkdir()
            (corrupt_dir / "job.json").write_text("{not-json", encoding="utf-8")
            self.assertIsNone(store.get("3" * 32))
            self.assertEqual(len(store.list()), 1)

    def test_validates_ids_and_deletes_only_exact_job_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("app.store.JOBS_DIR", Path(directory)):
            store = JobStore()
            job_id = "a" * 32
            store.create({"id": job_id, "url": "https://youtu.be/euuLzgjj7z4", "status": "completed"})
            self.assertIsNone(store.get("../outside"))
            store.delete(job_id)
            self.assertFalse((Path(directory) / job_id).exists())


if __name__ == "__main__":
    unittest.main()
