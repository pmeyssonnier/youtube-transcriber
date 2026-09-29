from __future__ import annotations

import math
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import STATIC_DIR, dependency_status, get_openai_api_key, initialize_directories, save_openai_api_key
from .constants import (
    ACTIVE_JOB_STATUSES,
    APP_NAME,
    APP_VERSION,
    DIARIZATION_MODEL,
    INSPECTION_TTL_SECONDS,
    LONG_VIDEO_SECONDS,
    STANDARD_MODEL,
    TERMINAL_JOB_STATUSES,
)
from .exporters import clean_inline_text, safe_filename_stem, write_exports
from .pipeline import PipelineError, TranscriptionPipeline
from .store import DuplicateJobError, JobStore
from .youtube import validate_cookie_browser, validate_youtube_url, youtube_video_identity


store = JobStore()
pipeline = TranscriptionPipeline(store)
inspections: dict[str, dict] = {}
inspections_lock = threading.RLock()


class InspectionRequest(BaseModel):
    url: str
    chunk_minutes: int = Field(default=10, ge=5, le=30)
    cookie_browser: str | None = Field(default=None, max_length=20)


class JobRequest(BaseModel):
    url: str
    inspection_id: str = Field(min_length=32, max_length=32)
    confirm_long_video: bool = False
    diarize: bool = True
    chunk_minutes: int = Field(default=10, ge=5, le=30)
    api_concurrency: int = Field(default=4, ge=1, le=6)
    cookie_browser: str | None = Field(default=None, max_length=20)


class ApiKeyRequest(BaseModel):
    api_key: str = Field(min_length=20, max_length=300)


class SpeakerNamesRequest(BaseModel):
    names: dict[str, str]


def find_active_duplicate(jobs: list[dict], url: str) -> dict | None:
    """Return an unfinished job for the same YouTube video, if present."""
    video_identity = youtube_video_identity(url)
    return next(
        (
            job
            for job in jobs
            if job.get("status") in ACTIVE_JOB_STATUSES
            and (job.get("video_identity") or youtube_video_identity(job.get("url", ""))) == video_identity
        ),
        None,
    )


def job_summary(job: dict) -> dict:
    """Return the lightweight representation polled by the browser."""
    fields = (
        "id",
        "url",
        "title",
        "status",
        "progress",
        "message",
        "warning",
        "diarize",
        "chunk_minutes",
        "api_concurrency",
        "duration",
        "created_at",
        "updated_at",
        "completed_at",
        "current_chunk",
        "chunk_count",
        "chunks_completed",
        "checkpointed_chunks",
        "transcription_started_at",
        "last_chunk_completed_at",
        "chunk_processing_seconds_total",
        "files",
        "error",
    )
    return {key: job.get(key) for key in fields if key in job}


def _purge_expired_inspections() -> None:
    cutoff = time.monotonic() - INSPECTION_TTL_SECONDS
    expired = [key for key, value in inspections.items() if value["created_monotonic"] < cutoff]
    for key in expired:
        inspections.pop(key, None)


def _output_files(job: dict, speaker_names: dict[str, str]) -> list[dict[str, str]]:
    metadata = {
        "title": job.get("title"),
        "source_url": job.get("url"),
        "duration_seconds": job.get("duration"),
        "model": DIARIZATION_MODEL if job.get("diarize") else STANDARD_MODEL,
        "speaker_labels_need_review": bool(job.get("diarize")),
    }
    files = write_exports(Path(job["output_dir"]), metadata, job.get("segments", []), speaker_names)
    source = Path(job.get("job_dir") or Path(job["output_dir"]).parent) / "source" / "source.m4a"
    if source.exists():
        files.append({"name": "source.m4a", "label": "Audio M4A", "kind": "source"})
    return files


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_directories()
    pipeline.resume_unfinished()
    try:
        yield
    finally:
        pipeline.shutdown()


app = FastAPI(title="Transcripteur vidéo local", version=APP_VERSION, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def local_security(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and (urlparse(origin).hostname or "").lower() not in {"127.0.0.1", "localhost"}:
            return JSONResponse(status_code=403, content={"detail": "Origine de requête refusée."})
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return JSONResponse(status_code=403, content={"detail": "Requête externe refusée."})

    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.get("/api/health")
def health() -> dict:
    dependencies = dependency_status()
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "ok": all(dependencies.values()),
        "dependencies": dependencies,
        "api_key_configured": get_openai_api_key() is not None,
    }


@app.post("/api/settings/api-key")
def configure_api_key(request: ApiKeyRequest) -> dict:
    try:
        save_openai_api_key(request.api_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"configured": True}


@app.post("/api/videos/inspect")
def inspect_video(request: InspectionRequest) -> dict:
    try:
        url = validate_youtube_url(request.url)
        cookie_browser = validate_cookie_browser(request.cookie_browser)
        metadata = pipeline.inspect_video(url, cookie_browser)
    except (ValueError, PipelineError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    duration = metadata.get("duration")
    estimated_chunks = math.ceil(duration / (request.chunk_minutes * 60)) if duration else None
    requires_confirmation = duration is None or duration >= LONG_VIDEO_SECONDS
    inspection_id = uuid.uuid4().hex
    inspection = {
        **metadata,
        "id": inspection_id,
        "url": url,
        "cookie_browser": cookie_browser,
        "chunk_minutes": request.chunk_minutes,
        "created_monotonic": time.monotonic(),
        "requires_confirmation": requires_confirmation,
    }
    with inspections_lock:
        _purge_expired_inspections()
        inspections[inspection_id] = inspection

    return {
        "inspection_id": inspection_id,
        "title": metadata["title"],
        "uploader": metadata.get("uploader"),
        "duration": duration,
        "estimated_chunks": estimated_chunks,
        "requires_confirmation": requires_confirmation,
        "pricing_note": "Le coût dépend du modèle et du tarif API OpenAI en vigueur.",
    }


@app.get("/api/jobs")
def list_jobs() -> list[dict]:
    return [job_summary(job) for job in store.list()]


@app.post("/api/jobs", status_code=202)
def create_job(request: JobRequest) -> dict:
    try:
        url = validate_youtube_url(request.url)
        cookie_browser = validate_cookie_browser(request.cookie_browser)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not get_openai_api_key():
        raise HTTPException(status_code=409, detail="Configurez d'abord votre clé API OpenAI.")

    with inspections_lock:
        _purge_expired_inspections()
        inspection = inspections.get(request.inspection_id)
    if inspection is None:
        raise HTTPException(status_code=409, detail="L'analyse préalable a expiré. Analysez à nouveau la vidéo.")
    if (
        inspection["video_identity"] != youtube_video_identity(url)
        or inspection["cookie_browser"] != cookie_browser
        or inspection["chunk_minutes"] != request.chunk_minutes
    ):
        raise HTTPException(status_code=409, detail="Le lien ou les options ont changé. Analysez à nouveau la vidéo.")
    if inspection["requires_confirmation"] and not request.confirm_long_video:
        raise HTTPException(status_code=409, detail="Confirmez le traitement de cette vidéo longue ou de durée inconnue.")

    job_id = uuid.uuid4().hex
    job_data = {
        "id": job_id,
        "url": url,
        "video_identity": inspection["video_identity"],
        "title": inspection["title"],
        "duration": inspection.get("duration"),
        "status": "queued",
        "progress": 0,
        "message": "En attente…",
        "diarize": request.diarize,
        "chunk_minutes": request.chunk_minutes,
        "api_concurrency": request.api_concurrency,
        "cookie_browser": cookie_browser,
        "segments": [],
        "speaker_names": {},
        "files": [],
        "error": None,
        "warning": None,
    }
    try:
        job = store.create_unique(job_data, ACTIVE_JOB_STATUSES)
    except DuplicateJobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    with inspections_lock:
        inspections.pop(request.inspection_id, None)
    pipeline.submit(job_id)
    return job_summary(job)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Traitement introuvable.")
    return job


@app.post("/api/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Traitement introuvable.")
    if job.get("status") != "failed":
        raise HTTPException(status_code=409, detail="Seul un traitement en erreur peut être relancé.")
    retried = store.update(
        job_id,
        status="queued",
        message="Reprise demandée…",
        error=None,
        warning=None,
        completed_at=None,
    )
    if not pipeline.submit(job_id):
        raise HTTPException(status_code=409, detail="Ce traitement est déjà en cours.")
    return job_summary(retried)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Traitement introuvable.")
    if job.get("status") not in TERMINAL_JOB_STATUSES:
        raise HTTPException(status_code=409, detail="Attendez la fin du traitement avant de le supprimer.")
    store.delete(job_id)
    return {"deleted": True}


@app.post("/api/jobs/{job_id}/speakers")
def rename_speakers(job_id: str, request: SpeakerNamesRequest) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Traitement introuvable.")
    if job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="La transcription n'est pas encore terminée.")

    allowed = set(job.get("speaker_names", {}))
    names = {
        key: (clean_inline_text(value) or key)[:100]
        for key, value in request.names.items()
        if key in allowed
    }
    names = {**job.get("speaker_names", {}), **names}
    files = _output_files(job, names)
    return job_summary(store.update(job_id, speaker_names=names, files=files, message="Noms mis à jour."))


@app.get("/api/jobs/{job_id}/files/{filename}")
def download_file(job_id: str, filename: str) -> FileResponse:
    job = store.get(job_id)
    if job is None or job.get("status") != "completed":
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    allowed = {item["name"]: item for item in job.get("files", [])}
    item = allowed.get(filename)
    if item is None or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Fichier introuvable.")

    if item.get("kind") == "source":
        path = Path(job.get("job_dir") or Path(job["output_dir"]).parent) / "source" / "source.m4a"
    else:
        path = Path(job["output_dir"]) / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Fichier introuvable.")

    extension = path.suffix.lower()
    suggested_name = f"{safe_filename_stem(job.get('title'))}{'-audio' if item.get('kind') == 'source' else '-transcription'}{extension}"
    return FileResponse(path, filename=suggested_name)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
