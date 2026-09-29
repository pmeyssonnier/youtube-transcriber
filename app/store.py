from __future__ import annotations

import json
import re
import shutil
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import JOBS_DIR
from .youtube import youtube_video_identity


JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class DuplicateJobError(RuntimeError):
    """Raised when the same video already has an unfinished job."""

    def __init__(self, job: dict[str, Any]) -> None:
        super().__init__("Cette vidéo possède déjà un traitement en cours.")
        self.job = job


class JobStore:
    """Thread-safe JSON job storage designed for a single local user."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _job_file(job_id: str) -> Path:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise ValueError("Identifiant de traitement invalide.")
        return JOBS_DIR / job_id / "job.json"

    def create(self, job: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            job_dir = JOBS_DIR / job["id"]
            job_dir.mkdir(parents=True, exist_ok=False)
            now = self._now()
            job["created_at"] = now
            job["updated_at"] = now
            self._write(job)
            return deepcopy(job)

    def create_unique(self, job: dict[str, Any], active_statuses: set[str] | frozenset[str]) -> dict[str, Any]:
        """Atomically reject an active duplicate and create the new job."""
        with self._lock:
            identity = job.get("video_identity")
            for existing in self.list():
                existing_identity = existing.get("video_identity") or youtube_video_identity(existing.get("url", ""))
                if existing.get("status") in active_statuses and existing_identity == identity:
                    raise DuplicateJobError(existing)
            return self.create(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                path = self._job_file(job_id)
            except ValueError:
                return None
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs: list[dict[str, Any]] = []
            if not JOBS_DIR.exists():
                return jobs
            for path in JOBS_DIR.glob("*/job.json"):
                try:
                    jobs.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
            return sorted(jobs, key=lambda item: item.get("created_at", ""), reverse=True)

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job.update(changes)
            job["updated_at"] = self._now()
            self._write(job)
            return deepcopy(job)

    def unfinished(self) -> list[dict[str, Any]]:
        return [
            job
            for job in self.list()
            if job.get("status") in {"queued", "downloading", "preparing", "transcribing", "exporting"}
        ]

    def delete(self, job_id: str) -> None:
        """Delete one validated job directory and all of its local files."""
        with self._lock:
            path = self._job_file(job_id).parent
            if not path.exists():
                raise KeyError(job_id)
            shutil.rmtree(path)

    def _write(self, job: dict[str, Any]) -> None:
        path = self._job_file(job["id"])
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(job, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
