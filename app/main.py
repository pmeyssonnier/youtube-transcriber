from __future__ import annotations

import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI, OpenAIError
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import config as app_config
from .config import (
    DATA_DIR,
    STATIC_DIR,
    dependency_status,
    get_openai_api_key,
    initialize_directories,
    key_sync_folder,
    migration,
    save_openai_api_key,
    synced_folder_name,
)
from .constants import (
    ACTIVE_JOB_STATUSES,
    DIARIZATION_MAX_CHUNK_MINUTES,
    APP_NAME,
    APP_VERSION,
    DEFAULT_LANGUAGE,
    INSPECTION_TTL_SECONDS,
    LONG_VIDEO_SECONDS,
    TERMINAL_JOB_STATUSES,
)
from .contract import job_metadata
from .dates import SOURCE_LABELS, reference_date
from .exporters import clean_inline_text, safe_filename_stem, write_exports
from .planning import plan_all_modes
from .pipeline import PipelineError, TranscriptionPipeline
from .store import DuplicateJobError, JobStore
from .ytdlp import YtDlpUpdateError, update_ytdlp, ytdlp_version
from .timerange import effective_duration, validate_time_range
from .people import analyze_names, cue_suggestions, parse_video_date, roster_available, roster_names, write_names_csv
from .speakers import SuggestionError, naming_contexts, request_name_suggestions, speaker_stats
from .youtube import validate_cookie_browser, validate_youtube_url, youtube_video_identity


store = JobStore()
pipeline = TranscriptionPipeline(store)
inspections: dict[str, dict] = {}
inspections_lock = threading.RLock()


class YtDlpUpdateRequest(BaseModel):
    include_dev: bool = False


class InspectionRequest(BaseModel):
    url: str
    chunk_minutes: int = Field(default=10, ge=5, le=30)
    diarize: bool = True
    cookie_browser: str | None = Field(default=None, max_length=20)


class PlanRequest(BaseModel):
    duration_seconds: float = Field(gt=0, le=172800)
    chunk_minutes: int = Field(default=10, ge=5, le=30)
    diarize: bool = True


class SessionDateRequest(BaseModel):
    # Date de la séance (AAAA-MM-JJ) ; null : revenir à la date de publication YouTube.
    session_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


def _valid_session_date(value: str | None) -> str | None:
    if value is None:
        return None
    if parse_video_date(value) is None:
        raise HTTPException(status_code=400, detail="Date de séance invalide (format AAAA-MM-JJ).")
    return value


class JobRequest(BaseModel):
    url: str
    inspection_id: str = Field(min_length=32, max_length=32)
    confirm_long_video: bool = False
    diarize: bool = True
    # Langue parlée (ISO 639-1) ; null : détection automatique par OpenAI.
    language: str | None = Field(default=DEFAULT_LANGUAGE, pattern=r"^[a-z]{2}$")
    speaker_linking: Literal["off", "light", "strong"] = "light"
    chunk_minutes: int = Field(default=10, ge=5, le=30)
    api_concurrency: int = Field(default=4, ge=1, le=6)
    cookie_browser: str | None = Field(default=None, max_length=20)
    start_seconds: float | None = Field(default=None, ge=0, le=172800)
    end_seconds: float | None = Field(default=None, ge=0, le=172800)
    # Date réelle de la séance (utile pour une rediffusion) : choisit les élus en fonction.
    session_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


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
        "language",
        "speaker_linking",
        "chunk_minutes",
        "api_concurrency",
        "start_seconds",
        "end_seconds",
        "duration",
        "created_at",
        "updated_at",
        "stage_started_at",
        "completed_at",
        "video_date",
        "session_date",
        "names_analysis_stale",
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
    metadata = job_metadata(job, duration=job.get("duration"))
    files = write_exports(Path(job["output_dir"]), metadata, job.get("segments", []), speaker_names)
    if job.get("names_analysis"):
        files.append(write_names_csv(Path(job["output_dir"]), job["names_analysis"]))
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


def own_origin(request: Request) -> str:
    """The origin the browser used to reach this server (scheme, host AND port)."""
    return f"{request.url.scheme}://{request.headers.get('host', '')}".lower()


def refuse_request(request: Request) -> str | None:
    """Reason for refusing a state-changing request, or None when it comes from our own page.

    - ``Origin`` must be exactly this server's origin: another port on localhost is another site.
    - ``Sec-Fetch-Site`` may only be ``same-origin`` or ``none`` (``same-site`` also covers another
      port on the same machine, ``cross-site`` any other website).
    - A request carrying neither header does not come from a browser page (scripts, curl): a
      cross-site attack always sends at least one of them, so it is accepted.
    """
    origin = request.headers.get("origin")
    if origin is not None and origin.lower().rstrip("/") != own_origin(request):
        return "Origine de requête refusée."
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return "Requête externe refusée."
    return None


@app.middleware("http")
async def local_security(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        reason = refuse_request(request)
        if reason:
            return JSONResponse(status_code=403, content={"detail": reason})

    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://www.youtube.com; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://i.ytimg.com; connect-src 'self'; "
        "frame-src https://www.youtube-nocookie.com https://www.youtube.com; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
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
        "ytdlp_version": ytdlp_version(),
        "api_key_configured": get_openai_api_key() is not None,
        "sync_folder": synced_folder_name(),
        "key_sync_folder": key_sync_folder(),
        "key_location": str(app_config.ENV_FILE),
        "key_migrated": bool(migration["moved"]),
        "key_migration_error": migration["error"],
        "data_dir": str(DATA_DIR),
    }


@app.get("/api/yt-dlp")
def ytdlp_status() -> dict:
    return {"version": ytdlp_version()}


@app.post("/api/yt-dlp/update")
def ytdlp_update(request: YtDlpUpdateRequest | None = None) -> dict:
    if store.unfinished():
        raise HTTPException(
            status_code=409,
            detail="Attendez la fin des traitements en cours avant de mettre à jour yt-dlp.",
        )
    try:
        return update_ytdlp(include_dev=bool(request and request.include_dev))
    except YtDlpUpdateError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
    plans = plan_all_modes(duration, request.chunk_minutes, request.diarize) if duration else None
    estimated_chunks = plans["off"]["chunks"] if plans else None
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
        "video_id": metadata["video_identity"],
        "title": metadata["title"],
        "uploader": metadata.get("uploader"),
        "duration": duration,
        "estimated_chunks": estimated_chunks,
        "plans": plans,
        "video_date": metadata.get("video_date"),
        "requires_confirmation": requires_confirmation,
        "pricing_note": "Le coût dépend du modèle et du tarif API OpenAI en vigueur.",
    }


@app.post("/api/plan")
def plan(request: PlanRequest) -> dict:
    """Real number of parts and audio to send, for each overlap setting (no fixed percentages)."""
    return {"plans": plan_all_modes(request.duration_seconds, request.chunk_minutes, request.diarize)}


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

    if request.diarize and request.chunk_minutes > DIARIZATION_MAX_CHUNK_MINUTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Avec la distinction des intervenants, les parties sont limitées à "
                f"{DIARIZATION_MAX_CHUNK_MINUTES} minutes."
            ),
        )

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
    try:
        start_seconds, end_seconds = validate_time_range(
            request.start_seconds, request.end_seconds, inspection.get("duration")
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    transcribed = effective_duration(start_seconds, end_seconds, inspection.get("duration"))
    requires_confirmation = transcribed is None or transcribed >= LONG_VIDEO_SECONDS
    if requires_confirmation and not request.confirm_long_video:
        raise HTTPException(status_code=409, detail="Confirmez le traitement de cette vidéo longue ou de durée inconnue.")

    job_id = uuid.uuid4().hex
    job_data = {
        "id": job_id,
        "url": url,
        "video_identity": inspection["video_identity"],
        "video_date": inspection.get("video_date"),
        "session_date": _valid_session_date(request.session_date),
        "title": inspection["title"],
        "duration": inspection.get("duration"),
        "status": "queued",
        "progress": 0,
        "message": "En attente…",
        "diarize": request.diarize,
        "language": request.language,
        "speaker_linking": request.speaker_linking if request.diarize else "off",
        "chunk_minutes": request.chunk_minutes,
        "api_concurrency": request.api_concurrency,
        "cookie_browser": cookie_browser,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
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


@app.get("/api/jobs/{job_id}/speakers")
def list_speakers(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Traitement introuvable.")
    if job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="La transcription n'est pas encore terminée.")
    return {
        "title": job.get("title"),
        "diarize": bool(job.get("diarize")),
        "speakers": speaker_stats(job.get("segments", []), job.get("speaker_names", {})),
    }


@app.post("/api/jobs/{job_id}/speakers/suggest")
def suggest_speaker_names(job_id: str) -> dict:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Traitement introuvable.")
    if job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="La transcription n'est pas encore terminée.")

    segments = job.get("segments", [])
    video_date, _ = reference_date(job)
    by_rules = cue_suggestions(segments, video_date)  # formules de passage de parole : gratuit, sans IA
    handled = {item["label"] for item in by_rules}
    contexts, skipped = naming_contexts(segments)
    contexts = [item for item in contexts if item["label"] not in handled]
    if not contexts and not by_rules:
        raise HTTPException(status_code=409, detail="Pas assez de paroles pour proposer des noms.")

    by_model: list[dict[str, str]] = []
    note = None
    api_key = get_openai_api_key()
    if contexts and api_key:
        try:
            client = OpenAI(api_key=api_key, max_retries=3, timeout=120)
            by_model = request_name_suggestions(client, contexts, roster_names(video_date))
        except SuggestionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except OpenAIError as exc:
            raise HTTPException(status_code=502, detail=f"L'analyse a échoué : {exc}") from exc
    elif contexts and not by_rules:
        raise HTTPException(status_code=409, detail="Configurez d'abord votre clé API OpenAI.")
    elif contexts:
        note = "Clé API OpenAI absente : seules les suggestions par règles sont proposées."
    return {
        "suggestions": by_rules + by_model,
        "analyzed_labels": len(contexts),
        "skipped_labels": skipped,
        "by_rules": len(by_rules),
        "note": note,
    }


@app.post("/api/jobs/{job_id}/names")
def analyze_job_names(job_id: str) -> dict:
    """(Re)compute the cited names of a finished job and regenerate its files; nothing is re-transcribed."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Traitement introuvable.")
    if job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="La transcription n'est pas encore terminée.")
    if not roster_available():
        raise HTTPException(
            status_code=409,
            detail="Aucune liste d'élus trouvée. Placez elus_mandats.json dans le dossier data/referentiel.",
        )
    segments = job.get("segments", [])
    used, source = reference_date(job)
    analysis = analyze_names(segments, used)
    analysis["stats"]["date_source"] = source
    analysis["stats"]["date_note"] = SOURCE_LABELS[source]
    job = {**job, "segments": segments, "names_analysis": analysis}
    files = _output_files(job, job.get("speaker_names", {}))
    store.update(
        job_id, segments=segments, names_analysis=analysis, names_analysis_stale=False, files=files, message="Noms cités analysés."
    )
    return {"stats": analysis["stats"], "files": files}


@app.post("/api/jobs/{job_id}/session-date")
def set_session_date(job_id: str, request: SessionDateRequest) -> dict:
    """Correct the session date of a finished job (e.g. a rebroadcast) and refresh its exports."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Traitement introuvable.")
    if job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="La transcription n'est pas encore terminée.")
    session_date = _valid_session_date(request.session_date)
    before, _ = reference_date(job)
    job = {**job, "session_date": session_date}
    after, _ = reference_date(job)
    changes = {"session_date": session_date, "message": "Date de séance mise à jour."}

    # Les noms corrigés dépendent de la date (élus en exercice) : si la date de référence change, les
    # corrections calculées avec l'ancienne ne doivent plus se trouver dans les fichiers. Le texte
    # d'origine (« text ») n'est jamais touché ; l'analyse se relance d'un clic.
    segments = job.get("segments", [])
    analysed = bool(job.get("names_analysis")) or any("mentions" in item or "text_normalized" in item for item in segments)
    if analysed and before != after:
        segments = [{key: value for key, value in item.items() if key not in {"mentions", "text_normalized"}} for item in segments]
        job = {**job, "segments": segments}
        job.pop("names_analysis", None)
        (Path(job["output_dir"]) / "noms_cites.csv").unlink(missing_ok=True)
        changes.update(segments=segments, names_analysis=None, names_analysis_stale=True)
        changes["message"] = "Date de séance mise à jour ; les noms cités doivent être analysés à nouveau."
    files = [item for item in _output_files(job, job.get("speaker_names", {})) if item["name"] != "noms_cites.csv" or job.get("names_analysis")]
    changes["files"] = files
    return job_summary(store.update(job_id, **changes))


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
