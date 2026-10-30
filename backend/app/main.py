"""HTTP API for the X-ray/MRI reader. Run: uvicorn app.main:app --port 8000"""

from __future__ import annotations

import asyncio
import hmac
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import gemma
from .body_parts import BODY_PARTS, MODALITIES, normalize_body_part, normalize_modality
from .config import settings
from .imaging import ImageError, prepare_image
from .jobs import JobQueue
from .pipeline import run_analysis
from .references import ReferenceLibrary
from .schemas import AnalysisResult, JobStatus, Options, ReferenceUsed

app = FastAPI(
    title="Hacktober-Fest X-ray API",
    version="0.1.0",
    description=(
        "Upload an X-ray, MRI or CT image and get a plain-language explanation from Gemma 4, "
        "compared against verified reference cases. Educational only, not a diagnosis."
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


def _read_limit() -> int:
    return int(settings.max_upload_mb * 1024 * 1024)


async def _read_upload(upload: UploadFile) -> bytes:
    data = await upload.read()
    if len(data) > _read_limit():
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb:g} MB.")
    return data


def _check_choices(body_part: str, modality: str) -> tuple[str, str]:
    bp = normalize_body_part(body_part)
    if bp is None:
        raise HTTPException(422, f"Unknown body_part '{body_part}'. Use one of: {', '.join(BODY_PARTS)}.")
    mod = normalize_modality(modality)
    if mod is None:
        raise HTTPException(422, f"Unknown modality '{modality}'. Use one of: {', '.join(MODALITIES)}.")
    return bp, mod


@app.get("/api/health", tags=["status"])
def health() -> dict:
    """Is the API up, is Gemma 4 ready, and how many reference cases are loaded."""
    return {"status": "ok", **gemma.status(), "mock": settings.mock_gemma, "reference_cases": len(library.cases)}


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
    image: UploadFile = File(..., description="X-ray/MRI/CT image: JPG, PNG, WEBP or DICOM (.dcm)"),
    body_part: str = Form(..., description="e.g. chest, knee, wrist, lumbar_spine (see /api/options)"),
    modality: str = Form("xray", description="xray, mri or ct"),
    question: str = Form("", description="Optional question from the patient"),
    wait: bool = Query(False, description="true = wait for the result instead of getting a job id"),
):
    """Start an analysis. Returns a job id to poll at /api/jobs/{job_id} (or the result if wait=true)."""
    bp, mod = _check_choices(body_part, modality)
    data = await _read_upload(image)
    try:
        prepared = prepare_image(data, image.filename or "", max_side=settings.max_image_side)
    except ImageError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        del data

    q = (question or "").strip()[:500]
    job_id = jobs.submit(lambda: run_analysis(image=prepared, body_part=bp, modality=mod, question=q, library=library))

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


if settings.frontend_dir:
    app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
else:

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {"service": "Hacktober-Fest X-ray API", "docs": "/docs", "health": "/api/health"}
