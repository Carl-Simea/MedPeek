"""MedPeek API: Gemma 4 explains X-ray, MRI and CT images, checked against verified reference cases.

Run: uvicorn app.main:app --port 8000   (or ./run.sh)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import gemma
from .body_parts import BODY_PARTS, MODALITIES, body_part_from_text, normalize_body_part, normalize_modality
from .config import settings
from .imaging import ImageError, prepare_image, thumbnail_vector
from .jobs import JobQueue
from .pipeline import run_analysis
from .references import ReferenceLibrary
from .schemas import AnalysisResult, JobStatus, Options, ReferenceUsed

VERSION = "1.1.0"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("medpeek.api")

app = FastAPI(
    title="MedPeek API",
    version=VERSION,
    description=(
        "Upload an X-ray, MRI or CT image and get a plain-language explanation from Gemma 4, "
        "compared against verified reference cases, with a referral to the right specialist. "
        "MedPeek is AI and can make mistakes. It is not a diagnosis."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

library = ReferenceLibrary(settings.reference_dir)
jobs = JobQueue()
_recent_requests: dict[str, deque] = defaultdict(deque)


@app.middleware("http")
async def request_log(request: Request, call_next):
    """Give every request an id and log method, path, status and time (never the image or question)."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("request %s %s %s failed", request_id, request.method, request.url.path)
        response = JSONResponse({"detail": "Something went wrong on our side. Please try again."}, status_code=500)
    response.headers["X-Request-ID"] = request_id
    if not request.url.path.startswith("/api/jobs/"):  # polling is too chatty to log
        log.info("%s %s %s %d %.0fms", request_id, request.method, request.url.path, response.status_code,
                 (time.monotonic() - started) * 1000)
    return response


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _rate_limit(request: Request) -> None:
    """At most ANALYZE_PER_MINUTE analyses per visitor per minute, and a cap on the waiting line."""
    if settings.analyze_per_minute > 0:
        now, window = time.monotonic(), _recent_requests[_client_ip(request)]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= settings.analyze_per_minute:
            raise HTTPException(429, "Too many images in a minute. Please wait a moment and try again.")
        window.append(now)
    if settings.max_waiting_jobs > 0 and jobs.waiting() >= settings.max_waiting_jobs:
        raise HTTPException(503, "MedPeek is busy right now. Please try again in a minute.")


def _read_limit() -> int:
    return int(settings.max_upload_mb * 1024 * 1024)


async def _read_upload(upload: UploadFile) -> bytes:
    data = await upload.read()
    if len(data) > _read_limit():
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb:g} MB.")
    return data


def _check_choices(body_part: str, modality: str) -> tuple[str, str]:
    if (body_part or "").strip().lower() in ("", "auto"):
        bp = "auto"
        mod = normalize_modality(modality)
        if mod is None:
            raise HTTPException(422, f"Unknown modality '{modality}'. Use one of: {', '.join(MODALITIES)}.")
        return bp, mod
    bp = normalize_body_part(body_part)
    if bp is None:
        raise HTTPException(422, f"Unknown body_part '{body_part}'. Use one of: {', '.join(BODY_PARTS)}.")
    mod = normalize_modality(modality)
    if mod is None:
        raise HTTPException(422, f"Unknown modality '{modality}'. Use one of: {', '.join(MODALITIES)}.")
    return bp, mod


@app.get("/api/health", tags=["status"])
def health() -> dict:
    """Is the API up, which Gemma 4 engine answers (google = hosted, ollama = local), and how many reference cases are loaded."""
    return {
        "status": "ok",
        "service": "MedPeek API",
        "version": VERSION,
        **gemma.status(),
        "mock": settings.mock_gemma,
        "reference_cases": len(library.cases),
        "waiting_jobs": jobs.waiting(),
    }


@app.get("/api/options", response_model=Options, tags=["status"])
def options() -> dict:
    """Body parts and imaging types the frontend can offer, with reference-case counts."""
    return {
        "body_parts": [
            {"id": key, "label": info["label"], "reference_cases": library.count(key)} for key, info in BODY_PARTS.items()
        ],
        "modalities": [{"id": key, "label": info["label"]} for key, info in MODALITIES.items()],
    }


@app.post(
    "/api/analyze",
    tags=["analysis"],
    status_code=202,
    responses={200: {"model": AnalysisResult, "description": "The finished result (only when wait=true)"}},
)
async def analyze(
    request: Request,
    image: UploadFile = File(..., description="X-ray/MRI/CT image: JPG, PNG, WEBP or DICOM (.dcm)"),
    body_part: str = Form("auto", description="An id from /api/options (e.g. wrist_hand), plain words (knee), or auto"),
    modality: str = Form("xray", description="xray, mri or ct"),
    question: str = Form("", description="Optional question from the patient"),
    wait: bool = Query(False, description="true = wait for the result instead of getting a job id"),
):
    """Start an analysis. Returns a job id to poll at /api/jobs/{job_id} (or the result if wait=true).

    body_part=auto: MedPeek uses the body part named in the question, or else the most similar verified images.
    """
    bp, mod = _check_choices(body_part, modality)
    _rate_limit(request)
    data = await _read_upload(image)
    try:
        prepared = prepare_image(data, image.filename or "", max_side=settings.max_image_side)
    except ImageError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        del data

    q = (question or "").strip()[:500]
    detected_by = "user"
    if bp == "auto":
        bp, detected_by = body_part_from_text(q), "question"
        if bp is None:
            bp, detected_by = library.guess_body_part(mod, thumbnail_vector(prepared.png_bytes)), "image"
        if bp is None:
            bp, detected_by = "whole_body", "image"
    job_id = jobs.submit(
        lambda: run_analysis(
            image=prepared, body_part=bp, modality=mod, question=q, library=library, detected_by=detected_by
        )
    )

    if not wait:
        return {"job_id": job_id, "status": "queued", "poll_url": f"/api/jobs/{job_id}"}

    while True:
        job = jobs.get(job_id)
        if job and job["status"] == "done":
            return JSONResponse(job["result"], status_code=200)
        if job and job["status"] == "error":
            raise HTTPException(503, job["error"])
        await asyncio.sleep(1)


@app.get("/api/jobs/{job_id}", response_model=JobStatus, tags=["analysis"])
def job_status(job_id: str) -> dict:
    """Poll this every 2-3 seconds. status: queued -> running -> done (result filled in) or error."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown job id (results are kept in memory and cleared when the server restarts).")
    return job


@app.get("/api/references/stats", tags=["reference library"])
def reference_stats() -> dict:
    """How many verified cases exist per body part and imaging type."""
    return {"total": len(library.cases), "by_body_part": library.stats()}


@app.post("/api/references", response_model=ReferenceUsed, tags=["reference library"], status_code=201)
async def add_reference(
    image: UploadFile = File(...),
    body_part: str = Form(...),
    modality: str = Form("xray"),
    report: str = Form(..., description="The verified report text for this image"),
    label: str = Form("unknown", description="normal, abnormal or unknown"),
    diagnosis: str = Form("", description="Short diagnosis, e.g. 'distal radius fracture'"),
    source: str = Form("", description="Lab, hospital or dataset the case came from"),
    verified_by: str = Form("", description="Who verified the report"),
    x_admin_token: Optional[str] = Header(None),
):
    """Add one verified case to the reference library (needs the X-Admin-Token header)."""
    if not settings.admin_token or not x_admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(401, "Missing or wrong X-Admin-Token.")
    bp, mod = _check_choices(body_part, modality)
    data = await _read_upload(image)
    try:
        case = library.add_case(
            image_bytes=data,
            filename=image.filename or "case.png",
            body_part=bp,
            modality=mod,
            report=report,
            label=label,
            diagnosis=diagnosis,
            source=source,
            verified_by=verified_by,
            max_side=settings.max_image_side,
        )
    except ImageError as exc:
        raise HTTPException(400, str(exc)) from exc
    return case.public()


@app.post("/api/references/reload", tags=["reference library"])
def reload_references(x_admin_token: Optional[str] = Header(None)) -> dict:
    """Re-scan the reference folder after importing files with the script."""
    if not settings.admin_token or not x_admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(401, "Missing or wrong X-Admin-Token.")
    library.reload()
    return {"total": len(library.cases)}


TRY_PAGE = Path(__file__).resolve().parent / "static" / "try.html"
SAMPLES_DIR = settings.reference_dir.parent / "samples"
SAMPLE_TYPES = {".jpg", ".jpeg", ".png"}


@app.get("/api/samples", tags=["status"])
def samples() -> list[dict]:
    """Example X-rays for trying the app: held-out images from the open datasets (not in the library)."""
    manifest = SAMPLES_DIR / "samples.json"
    if not manifest.exists():
        return []
    try:
        items = json.loads(manifest.read_text())
    except ValueError:
        return []
    return [i for i in items if isinstance(i, dict) and (SAMPLES_DIR / str(i.get("file", ""))).is_file()]


@app.get("/api/samples/{name}", include_in_schema=False)
def sample_file(name: str) -> FileResponse:
    path = (SAMPLES_DIR / name).resolve()
    if path.parent != SAMPLES_DIR.resolve() or path.suffix.lower() not in SAMPLE_TYPES or not path.is_file():
        raise HTTPException(404, "No such sample.")
    return FileResponse(path)


@app.get("/try", include_in_schema=False)
def try_page() -> FileResponse:
    """A simple page for trying the API in a browser (not the team's frontend)."""
    return FileResponse(TRY_PAGE, media_type="text/html")


if settings.frontend_dir:
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
else:

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"service": "MedPeek API", "version": VERSION, "try_it": "/try", "docs": "/docs", "health": "/api/health"}
