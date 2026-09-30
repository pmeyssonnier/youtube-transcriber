from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

from .config import JOBS_DIR, get_openai_api_key
from .constants import (
    DIARIZATION_MAX_AUDIO_SECONDS,
    DIARIZATION_MAX_CHUNK_MINUTES,
    DIARIZATION_MODEL,
    MAX_AUDIO_FILE_BYTES,
    STANDARD_MODEL,
)
from .exporters import write_exports
from .stitching import OVERLAP_BY_MODE, stitch_segments
from .store import JobStore
from .youtube import youtube_video_identity


# Une dernière partie plus courte que cela est absorbée par la précédente.
MIN_LAST_CHUNK_SECONDS = 30.0


class PipelineError(RuntimeError):
    """Error with a message suitable for the local user interface."""


class TranscriptionPipeline:
    """One-video-at-a-time pipeline with parallel, checkpointed audio chunks."""

    def __init__(self, store: JobStore) -> None:
        self.store = store
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcription")
        self._submitted: set[str] = set()
        self._submitted_lock = threading.Lock()

    def submit(self, job_id: str) -> bool:
        with self._submitted_lock:
            if job_id in self._submitted:
                return False
            self._submitted.add(job_id)
        self.executor.submit(self._run_safely, job_id)
        return True

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def resume_unfinished(self) -> None:
        for job in self.store.unfinished():
            self.store.update(
                job["id"],
                status="queued",
                message="Reprise du traitement à partir du dernier point sauvegardé…",
            )
            self.submit(job["id"])

    def _run_safely(self, job_id: str) -> None:
        failure: Exception | None = None
        try:
            self._run(job_id)
        except Exception as exc:  # noqa: BLE001 - worker boundary
            failure = exc
        finally:
            # Libéré avant de marquer l'échec : un « Relancer » immédiat ne doit pas être ignoré.
            with self._submitted_lock:
                self._submitted.discard(job_id)
        if failure is not None and self.store.get(job_id) is not None:
            self.store.update(
                job_id,
                status="failed",
                message=str(failure),
                error=type(failure).__name__,
            )

    def inspect_video(self, url: str, cookie_browser: str | None = None) -> dict[str, Any]:
        """Read bounded metadata without downloading the media."""
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--playlist-items",
            "1",
            "--dump-single-json",
            "--skip-download",
            "--no-warnings",
            *self._cookie_args(cookie_browser),
            url,
        ]
        output = self._run_command(
            command,
            "Impossible de lire les informations de cette vidéo.",
            timeout=120,
        )
        try:
            metadata = json.loads(output)
        except json.JSONDecodeError as exc:
            raise PipelineError("Les informations renvoyées par YouTube sont illisibles.") from exc

        expected_identity = youtube_video_identity(url)
        actual_identity = str(metadata.get("id") or "")
        if metadata.get("_type") == "playlist" or not expected_identity or actual_identity != expected_identity:
            raise PipelineError("Le lien doit désigner une seule vidéo YouTube.")
        if metadata.get("is_live") or metadata.get("live_status") in {"is_live", "is_upcoming"}:
            raise PipelineError(
                "Les directs en cours ou à venir ne sont pas acceptés. "
                "Attendez que la rediffusion soit disponible."
            )

        video_date = None
        stamp = metadata.get("release_timestamp") or metadata.get("timestamp")
        if isinstance(stamp, (int, float)):
            video_date = datetime.fromtimestamp(stamp, UTC).date().isoformat()
        elif re.fullmatch(r"\d{8}", str(metadata.get("upload_date") or "")):
            raw_date = str(metadata["upload_date"])
            video_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"

        duration_value = metadata.get("duration")
        try:
            duration = float(duration_value) if duration_value is not None else None
        except (TypeError, ValueError):
            duration = None
        return {
            "video_identity": expected_identity,
            "title": str(metadata.get("title") or "Vidéo YouTube").strip(),
            "duration": duration,
            "uploader": str(metadata.get("uploader") or "").strip() or None,
            "video_date": video_date,
            "webpage_url": str(metadata.get("webpage_url") or url),
        }

    def _run(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return

        api_key = get_openai_api_key()
        if not api_key:
            raise PipelineError("Configurez d'abord votre clé API OpenAI dans l'application.")

        job_dir = JOBS_DIR / job_id
        source_dir = job_dir / "source"
        chunks_dir = job_dir / "chunks"
        results_dir = job_dir / "chunk-results"
        output_dir = job_dir / "outputs"
        for directory in (source_dir, chunks_dir, results_dir, output_dir):
            directory.mkdir(exist_ok=True)

        title = str(job.get("title") or "Vidéo YouTube")
        duration = job.get("duration")
        cookie_browser = job.get("cookie_browser")

        self.store.update(
            job_id,
            status="downloading",
            progress=max(5, int(job.get("progress") or 0)),
            message="Vérification de la piste audio locale…",
            error=None,
        )
        audio_path = self._existing_audio(source_dir)
        if audio_path is None:
            self.store.update(
                job_id,
                status="downloading",
                progress=5,
                message="Téléchargement de la piste audio…",
            )
            audio_path = self._download_audio(job["url"], source_dir, cookie_browser)

        if duration is None:
            duration = self._duration(audio_path)

        self.store.update(
            job_id,
            title=title,
            duration=duration,
            status="preparing",
            progress=max(18, int(job.get("progress") or 0)),
            message="Préparation des parties audio…",
            audio_path=str(audio_path),
        )
        diarize = bool(job.get("diarize", True))
        chunk_minutes = int(job.get("chunk_minutes", 10))
        if diarize:
            chunk_minutes = min(chunk_minutes, DIARIZATION_MAX_CHUNK_MINUTES)
        range_start = job.get("start_seconds")
        range_end = job.get("end_seconds")
        # Recouvrement entre parties : relie les étiquettes d'intervenants d'une partie à l'autre.
        # Les anciens traitements (champ absent) gardent leur comportement d'origine : pas de recouvrement.
        link_mode = str(job.get("speaker_linking", "off")) if diarize else "off"
        overlap = OVERLAP_BY_MODE.get(link_mode, 0.0)
        link_speakers = overlap > 0
        chunk_seconds = chunk_minutes * 60
        if diarize:
            chunk_seconds = min(chunk_seconds, int(DIARIZATION_MAX_AUDIO_SECONDS - overlap))
        chunks = self._split_audio(
            audio_path, chunks_dir, results_dir, chunk_seconds, range_start, range_end, overlap
        )
        if not chunks:
            raise PipelineError("FFmpeg n'a créé aucune partie audio.")

        model = DIARIZATION_MODEL if diarize else STANDARD_MODEL
        checkpoints = {
            index: checkpoint
            for index, checkpoint in (
                (index, self._load_chunk_result(results_dir, index, model))
                for index in range(len(chunks))
            )
            if checkpoint is not None
        }
        completed_count = len(checkpoints)
        processing_seconds = sum(float(item.get("processing_seconds", 0)) for item in checkpoints.values())
        transcription_started_at = self._now()
        first_pending = next((index for index in range(len(chunks)) if index not in checkpoints), None)

        self.store.update(
            job_id,
            status="transcribing" if first_pending is not None else "exporting",
            progress=22 + round((completed_count / len(chunks)) * 68),
            message=(
                f"Reprise à la partie {first_pending + 1} sur {len(chunks)}…"
                if first_pending is not None and completed_count
                else f"Transcription de la partie 1 sur {len(chunks)}…"
                if first_pending is not None
                else "Toutes les parties sont déjà sauvegardées. Création des fichiers…"
            ),
            current_chunk=(first_pending + 1) if first_pending is not None else len(chunks),
            chunk_count=len(chunks),
            chunks_completed=completed_count,
            checkpointed_chunks=completed_count,
            transcription_started_at=transcription_started_at,
            last_chunk_completed_at=self._now() if completed_count else None,
            chunk_processing_seconds_total=processing_seconds,
        )

        chunk_durations = [self._duration(chunk) for chunk in chunks]
        offsets = self._planned_offsets(chunks_dir, len(chunks))
        if offsets is None:
            offsets = []
            running_offset = float(range_start or 0.0)
            for chunk_duration in chunk_durations:
                offsets.append(running_offset)
                running_offset += chunk_duration

        api_concurrency = max(1, min(6, int(job.get("api_concurrency", 4))))
        pending_indices = [index for index in range(len(chunks)) if index not in checkpoints]
        self.store.update(
            job_id,
            api_concurrency=api_concurrency,
            message=(
                f"Transcription parallèle de {len(pending_indices)} partie(s), "
                f"jusqu'à {api_concurrency} simultanément…"
                if pending_indices
                else "Toutes les parties sont déjà sauvegardées. Création des fichiers…"
            ),
        )

        failures: list[Exception] = []
        futures: dict[Future[dict[str, Any]], int] = {}
        with ThreadPoolExecutor(
            max_workers=min(api_concurrency, max(1, len(pending_indices))),
            thread_name_prefix=f"chunks-{job_id[:6]}",
        ) as chunk_executor:
            for index in pending_indices:
                future = chunk_executor.submit(
                    self._transcribe_and_checkpoint,
                    api_key,
                    chunks[index],
                    index,
                    diarize,
                    model,
                    offsets[index],
                    chunk_durations[index],
                    self._chunk_result_path(results_dir, index),
                )
                futures[future] = index

            for future in as_completed(futures):
                index = futures[future]
                try:
                    checkpoint = future.result()
                except Exception as exc:  # noqa: BLE001 - preserve successful checkpoints
                    failures.append(exc)
                    for other in futures:
                        if other is not future:
                            other.cancel()
                    continue

                checkpoints[index] = checkpoint
                completed_count += 1
                processing_seconds += float(checkpoint.get("processing_seconds", 0))
                self.store.update(
                    job_id,
                    progress=22 + round((completed_count / len(chunks)) * 68),
                    message=f"{completed_count} partie(s) sur {len(chunks)} sauvegardée(s)…",
                    current_chunk=index + 1,
                    chunks_completed=completed_count,
                    checkpointed_chunks=completed_count,
                    last_chunk_completed_at=self._now(),
                    chunk_processing_seconds_total=processing_seconds,
                )

        if failures:
            raise PipelineError(f"Une partie n'a pas pu être transcrite : {failures[0]}") from failures[0]

        results: list[list[dict[str, Any]]] = []
        for index in range(len(chunks)):
            checkpoint = checkpoints.get(index)
            if checkpoint is None:
                raise PipelineError(f"Le résultat sauvegardé de la partie {index + 1} est manquant.")
            results.append(checkpoint["segments"])
        windows = [(offsets[index], offsets[index] + chunk_durations[index]) for index in range(len(chunks))]
        segments, stitching = stitch_segments(results, windows, relabel=link_speakers)
        offset = float(range_start or 0.0) + sum(chunk_durations)

        speakers = sorted({segment["speaker"] for segment in segments})
        speaker_names = {speaker: speaker for speaker in speakers}
        self.store.update(
            job_id,
            status="exporting",
            progress=94,
            message="Création des fichiers…",
            segments=segments,
            speaker_names=speaker_names,
            speaker_labels_raw=stitching["raw_labels"],
            speaker_labels_merged=stitching["voices"],
        )

        metadata = {
            "title": title,
            "source_url": job["url"],
            "duration_seconds": duration or offset,
            "transcribed_from": range_start,
            "transcribed_to": range_end,
            "model": model,
            "speaker_labels_need_review": diarize,
        }
        files = write_exports(output_dir, metadata, segments, speaker_names)
        files.append({"name": "source.m4a", "label": "Audio M4A", "kind": "source"})
        warning = None if segments else "Aucune parole n'a été détectée dans la vidéo."
        done = "Transcription terminée."
        if link_speakers and segments:
            done += f" Intervenants regroupés entre les parties : {stitching['raw_labels']} → {stitching['voices']}."
        self.store.update(
            job_id,
            status="completed",
            progress=100,
            message=warning or done,
            warning=warning,
            files=files,
            output_dir=str(output_dir),
            job_dir=str(job_dir),
            completed_at=self._now(),
        )

    @staticmethod
    def _cookie_args(cookie_browser: str | None) -> list[str]:
        return ["--cookies-from-browser", cookie_browser] if cookie_browser else []

    @staticmethod
    def _run_command(
        command: list[str],
        failure_message: str,
        timeout: float | None = None,
    ) -> str:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise PipelineError(f"Programme introuvable : {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise PipelineError(f"{failure_message}\nDélai d'attente dépassé.") from exc
        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()[-1200:]
            raise PipelineError(f"{failure_message}\n{details}".strip())
        return result.stdout.strip()

    def _existing_audio(self, source_dir: Path) -> Path | None:
        candidate = source_dir / "source.m4a"
        if not candidate.exists() or candidate.stat().st_size <= 0:
            return None
        try:
            self._duration(candidate)
        except PipelineError:
            return None
        return candidate.resolve()

    def _download_audio(
        self,
        url: str,
        source_dir: Path,
        cookie_browser: str | None,
    ) -> Path:
        output = self._run_command(
            [
                sys.executable,
                "-m",
                "yt_dlp",
                "--no-playlist",
                "--playlist-items",
                "1",
                "--match-filters",
                "!is_live",
                "--concurrent-fragments",
                "8",
                "--windows-filenames",
                "--no-warnings",
                "--print",
                "after_move:filepath",
                "-f",
                "bestaudio/best",
                "-x",
                "--audio-format",
                "m4a",
                "--audio-quality",
                "0",
                "-P",
                str(source_dir),
                "-o",
                "source.%(ext)s",
                *self._cookie_args(cookie_browser),
                url,
            ],
            "Le téléchargement audio a échoué.",
        )
        candidates = [Path(line.strip()) for line in output.splitlines() if line.strip()]
        audio_path = next(
            (path for path in reversed(candidates) if path.exists() and path.suffix.lower() == ".m4a"),
            None,
        )
        if audio_path is None:
            candidate = source_dir / "source.m4a"
            audio_path = candidate if candidate.exists() else None
        if audio_path is None:
            raise PipelineError("Le fichier audio M4A téléchargé n'a pas été retrouvé.")
        self._duration(audio_path)
        return audio_path.resolve()

    def _split_audio(
        self,
        audio_path: Path,
        chunks_dir: Path,
        results_dir: Path,
        seconds: int,
        start: float | None = None,
        end: float | None = None,
        overlap: float = 0.0,
    ) -> list[Path]:
        source_duration = self._duration(audio_path)
        fingerprint = {
            "source_size": audio_path.stat().st_size,
            "source_duration": round(source_duration, 3),
            "chunk_seconds": seconds,
            "start_seconds": start,
            "end_seconds": end,
        }
        range_args: list[str] = []
        if start:
            range_args += ["-ss", f"{float(start):.3f}"]
        if end:
            range_args += ["-t", f"{float(end) - float(start or 0.0):.3f}"]
        manifest_path = chunks_dir / "manifest.json"
        manifest = self._read_json(manifest_path)
        if (
            manifest
            and all(manifest.get(key) == value for key, value in fingerprint.items())
            and float(manifest.get("overlap_seconds", 0.0)) == float(overlap)
        ):
            names = manifest.get("chunks", [])
            existing = [chunks_dir / str(name) for name in names]
            if existing and all(path.exists() and path.stat().st_size > 0 for path in existing):
                self._validate_chunk_sizes(existing)
                return existing

        for old_chunk in chunks_dir.glob("chunk_*.mp3"):
            old_chunk.unlink()
        for old_result in results_dir.glob("chunk_*.json"):
            old_result.unlink()
        if manifest_path.exists():
            manifest_path.unlink()

        if overlap > 0:
            return self._split_with_overlap(
                audio_path, chunks_dir, manifest_path, fingerprint, seconds, start, end, source_duration, overlap
            )

        self._run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                *range_args,
                "-i",
                str(audio_path),
                "-map",
                "0:a:0",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "64k",
                "-f",
                "segment",
                "-segment_time",
                str(seconds),
                "-reset_timestamps",
                "1",
                str(chunks_dir / "chunk_%03d.mp3"),
            ],
            "La préparation audio avec FFmpeg a échoué.",
        )
        chunks = sorted(chunks_dir.glob("chunk_*.mp3"))
        self._validate_chunk_sizes(chunks)
        self._write_json_atomic(
            manifest_path,
            {**fingerprint, "version": 1, "chunks": [path.name for path in chunks]},
        )
        return chunks

    @staticmethod
    def _chunk_plan(span_start: float, span_end: float, seconds: int, overlap: float) -> list[tuple[float, float]]:
        """(start, length) of every chunk: each one runs ``overlap`` seconds into the next."""
        span = max(0.0, span_end - span_start)
        count = max(1, math.ceil(span / seconds - 1e-9))
        if count > 1 and span - (count - 1) * seconds < MIN_LAST_CHUNK_SECONDS:
            count -= 1
        plan = []
        for index in range(count):
            chunk_start = span_start + index * seconds
            remaining = span_end - chunk_start
            length = remaining if index == count - 1 else min(seconds + overlap, remaining)
            plan.append((chunk_start, length))
        return plan

    def _split_with_overlap(
        self,
        audio_path: Path,
        chunks_dir: Path,
        manifest_path: Path,
        fingerprint: dict[str, Any],
        seconds: int,
        start: float | None,
        end: float | None,
        source_duration: float,
        overlap: float,
    ) -> list[Path]:
        span_start = float(start or 0.0)
        span_end = min(float(end), source_duration) if end else source_duration
        plan = self._chunk_plan(span_start, span_end, seconds, overlap)
        chunks: list[Path] = []
        for index, (chunk_start, length) in enumerate(plan):
            target = chunks_dir / f"chunk_{index:03d}.mp3"
            self._run_command(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{chunk_start:.3f}", "-t", f"{length:.3f}",
                    "-i", str(audio_path),
                    "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-b:a", "64k",
                    str(target),
                ],
                "La préparation audio avec FFmpeg a échoué.",
            )
            chunks.append(target)
        self._validate_chunk_sizes(chunks)
        self._write_json_atomic(
            manifest_path,
            {
                **fingerprint,
                "version": 1,
                "overlap_seconds": float(overlap),
                "offsets": [round(chunk_start, 3) for chunk_start, _ in plan],
                "chunks": [path.name for path in chunks],
            },
        )
        return chunks

    def _planned_offsets(self, chunks_dir: Path, count: int) -> list[float] | None:
        """Start of each chunk in the video when the split recorded it (overlapping chunks)."""
        manifest = self._read_json(chunks_dir / "manifest.json")
        offsets = manifest.get("offsets") if manifest else None
        if isinstance(offsets, list) and len(offsets) == count:
            return [float(value) for value in offsets]
        return None

    @staticmethod
    def _validate_chunk_sizes(chunks: list[Path]) -> None:
        oversized = next((path for path in chunks if path.stat().st_size > MAX_AUDIO_FILE_BYTES), None)
        if oversized is not None:
            raise PipelineError(
                f"La partie {oversized.name} dépasse la limite de sécurité de 24 Mo. "
                "Choisissez des parties plus courtes."
            )

    def _duration(self, audio_path: Path) -> float:
        output = self._run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            "Impossible de mesurer une partie audio.",
        )
        try:
            duration = float(output.splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise PipelineError("Durée audio illisible.") from exc
        if duration <= 0:
            raise PipelineError("Durée audio invalide.")
        return duration

    @staticmethod
    def _chunk_result_path(results_dir: Path, index: int) -> Path:
        return results_dir / f"chunk_{index:03d}.json"

    def _load_chunk_result(self, results_dir: Path, index: int, model: str) -> dict[str, Any] | None:
        data = self._read_json(self._chunk_result_path(results_dir, index))
        if not data:
            return None
        if data.get("version") != 1 or data.get("chunk_index") != index or data.get("model") != model:
            return None
        if not isinstance(data.get("segments"), list):
            return None
        try:
            if float(data.get("duration", 0)) <= 0:
                return None
        except (TypeError, ValueError):
            return None
        return data

    def _transcribe_and_checkpoint(
        self,
        api_key: str,
        chunk: Path,
        chunk_index: int,
        diarize: bool,
        model: str,
        offset: float,
        chunk_duration: float,
        checkpoint_path: Path,
    ) -> dict[str, Any]:
        """Transcribe one chunk and persist it before reporting success."""
        client = OpenAI(api_key=api_key, max_retries=5, timeout=900)
        started = time.monotonic()
        chunk_segments = self._transcribe_chunk(client, chunk, chunk_index, diarize)
        elapsed = max(0.0, time.monotonic() - started)
        absolute_segments: list[dict[str, Any]] = []
        for segment in chunk_segments:
            segment["start"] = round(float(segment["start"]) + offset, 3)
            segment["end"] = round(float(segment["end"]) + offset, 3)
            absolute_segments.append(segment)
        checkpoint = {
            "version": 1,
            "chunk_index": chunk_index,
            "model": model,
            "duration": chunk_duration,
            "processing_seconds": elapsed,
            "segments": absolute_segments,
        }
        self._write_json_atomic(checkpoint_path, checkpoint)
        return checkpoint

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _speaker_label(chunk_index: int, raw: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", raw.strip() or "speaker")
        return f"P{chunk_index + 1:02d}-{normalized}"

    def _transcribe_chunk(
        self,
        client: OpenAI,
        chunk: Path,
        chunk_index: int,
        diarize: bool,
    ) -> list[dict[str, Any]]:
        with chunk.open("rb") as audio_file:
            if diarize:
                response = client.audio.transcriptions.create(
                    model=DIARIZATION_MODEL,
                    file=audio_file,
                    response_format="diarized_json",
                    chunking_strategy="auto",
                )
                data = response.model_dump() if hasattr(response, "model_dump") else dict(response)
                return [
                    {
                        "start": float(segment.get("start", 0)),
                        "end": float(segment.get("end", segment.get("start", 0) + 0.2)),
                        "speaker": self._speaker_label(chunk_index, str(segment.get("speaker", "speaker"))),
                        "text": str(segment.get("text", "")).strip(),
                        "chunk_index": chunk_index,
                    }
                    for segment in data.get("segments", [])
                    if str(segment.get("text", "")).strip()
                ]

            response = client.audio.transcriptions.create(
                model=STANDARD_MODEL,
                file=audio_file,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
            data = response.model_dump() if hasattr(response, "model_dump") else dict(response)
            return [
                {
                    "start": float(segment.get("start", 0)),
                    "end": float(segment.get("end", segment.get("start", 0) + 0.2)),
                    "speaker": "Intervenant",
                    "text": str(segment.get("text", "")).strip(),
                    "chunk_index": chunk_index,
                }
                for segment in data.get("segments", [])
                if str(segment.get("text", "")).strip()
            ]
