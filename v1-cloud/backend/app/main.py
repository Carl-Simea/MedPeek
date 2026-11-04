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
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles

from . import gemma, places
from .body_parts import BODY_PARTS, MODALITIES, SPECIALISTS, body_part_from_text, normalize_body_part, normalize_modality
from .config import settings
from .imaging import ImageError, prepare_image
from .jobs import JobQueue
from .pipeline import run_analysis
from .prompts import CHAT_SCHEMA, CHAT_SYSTEM_PROMPT, report_context
from .references import ReferenceLibrary
from .report_pdf import build_pdf, verification_code
from .safety import AI_NOTICE, EMERGENCY_NOTICE
from .schemas import AnalysisResult, JobStatus, Options, ReferenceUsed

VERSION = "2.0.0"
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
_recent_requests: dict[tuple, deque] = defaultdict(deque)


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


def _rate_limit(request: Request, bucket: str = "analyze", per_minute: int | None = None, queue_check: bool = True) -> None:
    """Per-visitor limits for a public URL: analyses per minute (and other endpoints), plus a cap on the waiting line."""
    limit = settings.analyze_per_minute if per_minute is None else per_minute
    if limit > 0:
        now, window = time.monotonic(), _recent_requests[(bucket, _client_ip(request))]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            raise HTTPException(429, "Too many requests in a minute. Please wait a moment and try again.")
        window.append(now)
    if queue_check and settings.max_waiting_jobs > 0 and jobs.waiting() >= settings.max_waiting_jobs:
        raise HTTPException(503, "MedPeek is busy right now. Please try again in a minute.")


def _read_limit() -> int:
    return int(settings.max_upload_mb * 1024 * 1024)


async def _read_upload(upload: UploadFile) -> bytes:
    data = await upload.read()
    if len(data) > _read_limit():
        raise HTTPException(413, f"File is larger than {settings.max_upload_mb:g} MB.")
    return data


def _check_choices(body_part: str, modality: str) -> tuple[str, str]:
    if (modality or "").strip().lower() == "auto":
        bp = "auto" if (body_part or "").strip().lower() in ("", "auto") else normalize_body_part(body_part)
        if bp is None:
            raise HTTPException(422, f"Unknown body_part '{body_part}'. Use one of: {', '.join(BODY_PARTS)}.")
        return bp, "auto"
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
        "privacy_note": gemma.privacy_note(),
    }


@app.get("/api/privacy", tags=["status"])
def privacy() -> dict:
    """The privacy line to show near the upload button."""
    return {"privacy_note": gemma.privacy_note(), "engine": gemma.engine()}


def _pdf_response(result: dict) -> Response:
    if not isinstance(result, dict) or not result.get("summary"):
        raise HTTPException(422, "Send the finished analysis result (the JSON from /api/jobs/{job_id}).")
    pdf = build_pdf(result, privacy_note=gemma.privacy_note())
    name = f"MedPeek-report-{result.get('id', 'report')}.pdf"
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "X-Verification-Code": verification_code(result),
    })


@app.post("/api/report/pdf", tags=["reports"], response_class=Response)
async def report_pdf(request: Request) -> Response:
    """Turn a finished analysis (the `result` JSON) into a PDF the patient can bring to their doctor."""
    try:
        result = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "Send the analysis result as JSON.") from exc
    return _pdf_response(result)


@app.get("/api/jobs/{job_id}/pdf", tags=["reports"], response_class=Response)
def job_pdf(job_id: str) -> Response:
    """PDF for a finished job (works while the server keeps the job in memory)."""
    job = jobs.get(job_id)
    if job is None or job["status"] != "done":
        raise HTTPException(404, "That report isn't available. Use POST /api/report/pdf with the result instead.")
    return _pdf_response(job["result"])


# ---------------------------------------------------------------- care finder + chat
class NearbyRequest(BaseModel):
    zip: str = Field(..., min_length=5, max_length=10, description="US ZIP code")
    specialist: str = Field("", description="A specialist name from a MedPeek report (optional)")
    urgency: str = Field("soon", description="routine, soon, urgent or emergency")
    limit: int = Field(3, ge=1, le=5)
    include: list[str] = Field(default_factory=lambda: ["emergency", "urgent_care", "specialist"])


@app.post("/api/nearby", tags=["care finder"])
def nearby(req: NearbyRequest, request: Request) -> dict:
    """Top 3 emergency rooms, urgent care centers and matching specialists near a US ZIP code, with map coordinates
    and directions links. Free government data: CMS hospital list, HIFLD coordinates, NPI Registry, GeoNames."""
    _rate_limit(request, bucket="nearby", per_minute=30, queue_check=False)
    if not places.ready():
        raise HTTPException(503, "The care finder data isn't built yet (run scripts/build_places.py).")
    specialist = req.specialist if req.specialist in SPECIALISTS else ""
    urgency = req.urgency if req.urgency in ("routine", "soon", "urgent", "emergency") else "soon"
    try:
        return places.find_care(req.zip, specialist, urgency, req.limit,
                                include=tuple(i for i in req.include if i in ("emergency", "urgent_care", "specialist")))
    except places.PlacesError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/zip/{zip_code}", tags=["care finder"])
def zip_info(zip_code: str) -> dict:
    """City, state and coordinates for a US ZIP code (to confirm what the patient typed)."""
    try:
        return places.lookup_zip(zip_code)
    except places.PlacesError as exc:
        raise HTTPException(422, str(exc)) from exc


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., max_length=4000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1, max_length=30)
    zip: str = Field("", max_length=10, description="The patient's ZIP code, if already known")
    report: Optional[dict] = Field(None, description="The last MedPeek report in this chat, for follow-up questions")
    find_care: bool = Field(True, description="Look up nearby care when the patient asks for it and a ZIP is known")


def _chat_turn(req: ChatRequest) -> dict:
    history = [{"role": m.role, "content": m.content.strip()} for m in req.messages[-12:] if m.content.strip()]
    if not history or history[-1]["role"] != "user":
        raise HTTPException(422, "The last message must be from the patient.")
    context = report_context(req.report)
    if context:
        history[-1] = {"role": "user", "content": context + "\n\nPatient's message: " + history[-1]["content"]}
    if settings.mock_gemma:
        raw = {"reply": "This is a mock chat answer for frontend testing. A wrist that hurts after a fall should be "
                        "checked for a fracture; attaching the X-ray lets me look at it.", "topic_ok": True,
               "needs_image": True, "body_part": "wrist_hand", "specialist": "Orthopedic surgeon (orthopedics)",
               "urgency": "soon", "emergency": False,
               "wants_nearby_care": any(w in history[-1]["content"].lower() for w in ("near", "nearby", "where", "hospital")),
               "zip_code": places.find_zip_in_text(history[-1]["content"]) or ""}
        info = {"engine": "mock", "model": settings.gemma_model, "seconds": 0.0}
    else:
        try:
            raw, info = gemma.converse(CHAT_SYSTEM_PROMPT, history, CHAT_SCHEMA, max_tokens=1024)
        except gemma.GemmaUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except gemma.GemmaBadOutput as exc:
            raise HTTPException(502, str(exc)) from exc

    last_user = req.messages[-1].content
    zip_code = places.find_zip_in_text(last_user) or places.find_zip_in_text(str(raw.get("zip_code", ""))) \
        or places.find_zip_in_text(req.zip)
    specialist = raw.get("specialist") if raw.get("specialist") in SPECIALISTS else ""
    urgency = raw.get("urgency") if raw.get("urgency") in ("routine", "soon", "urgent", "emergency") else "routine"
    emergency = bool(raw.get("emergency")) or urgency == "emergency"
    if emergency:
        urgency, specialist = "emergency", "Emergency department"
    body_part = normalize_body_part(str(raw.get("body_part") or "")) if raw.get("body_part") else None
    wants_care = bool(raw.get("wants_nearby_care")) or emergency
    reply = " ".join(str(raw.get("reply") or "").split())

    care, care_error = None, None
    if req.find_care and wants_care and zip_code and places.ready():
        try:
            include = ("emergency",) if emergency else ("specialist", "urgent_care", "emergency")
            care = places.find_care(zip_code, specialist, urgency, 3, include=include)
        except places.PlacesError as exc:
            care_error = str(exc)
    elif wants_care and not zip_code and "zip" not in reply.lower():
        reply += " If you tell me your ZIP code, I'll list the closest places that can help."

    return {
        "reply": reply,
        "topic_ok": bool(raw.get("topic_ok", True)),
        "needs_image": bool(raw.get("needs_image")),
        "suggested_body_part": body_part,
        "suggested_body_part_label": BODY_PARTS[body_part]["label"] if body_part else "",
        "specialist": specialist,
        "urgency": urgency,
        "urgency_notice": EMERGENCY_NOTICE if emergency else None,
        "zip": zip_code or "",
        "care": care,
        "care_error": care_error,
        "ai_notice": AI_NOTICE,
        "engine": info.get("engine", ""),
        "model": info.get("model", ""),
        "processing_seconds": info.get("seconds", 0.0),
    }


@app.post("/api/chat", tags=["chat"])
def chat(req: ChatRequest, request: Request) -> dict:
    """Talk to MedPeek without an image: symptoms, what a report means, which doctor to see, nearby care.
    Send the conversation so far (last message from the patient). Health topics only."""
    _rate_limit(request, bucket="chat", per_minute=20, queue_check=False)
    return _chat_turn(req)


class Feedback(BaseModel):
    kind: str = Field("mistake", description="mistake (wrong report), bug or issue")
    message: str = Field(..., min_length=3, max_length=2000)
    report_id: str = Field("", max_length=64)
    contact: str = Field("", max_length=200, description="Optional email if you want a reply")
    page: str = Field("", max_length=200)


FEEDBACK_FILE = settings.reference_dir.parent / "feedback" / "feedback.jsonl"
_feedback_times: dict[str, deque] = defaultdict(deque)


@app.post("/api/feedback", tags=["feedback"], status_code=201)
def send_feedback(item: Feedback, request: Request) -> dict:
    """Report a mistake in a report, a bug or any other issue. Saved on the server for the team to review."""
    now, window = time.monotonic(), _feedback_times[_client_ip(request)]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= 10:
        raise HTTPException(429, "Too many reports in a minute. Please wait a moment.")
    window.append(now)
    kind = item.kind.lower() if item.kind.lower() in ("mistake", "bug", "issue") else "issue"
    ticket = "FB-" + uuid.uuid4().hex[:6].upper()
    record = {"ticket": ticket, "kind": kind, "message": item.message.strip(), "report_id": item.report_id,
              "contact": item.contact.strip(), "page": item.page, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with FEEDBACK_FILE.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    log.info("feedback %s (%s) for report %s", ticket, kind, item.report_id or "-")
    return {"ok": True, "ticket": ticket, "message": "Thanks! The MedPeek team will look at this."}


@app.get("/api/feedback", tags=["feedback"])
def list_feedback(x_admin_token: Optional[str] = Header(None)) -> dict:
    """The latest 100 reports (team only: needs X-Admin-Token)."""
    if not settings.admin_token or not x_admin_token or not hmac.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(401, "Missing or wrong X-Admin-Token.")
    if not FEEDBACK_FILE.exists():
        return {"items": []}
    lines = FEEDBACK_FILE.read_text().splitlines()[-100:]
    return {"items": [json.loads(line) for line in reversed(lines) if line.strip()]}


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
    modality: str = Form("xray", description="xray, mri, ct or auto"),
    question: str = Form("", description="Optional question from the patient"),
    wait: bool = Query(False, description="true = wait for the result instead of getting a job id"),
):
    """Start an analysis. Returns a job id to poll at /api/jobs/{job_id} (or the result if wait=true).

    body_part=auto: MedPeek uses the body part named in the question; otherwise hosted Gemma 4 looks at the image
    (or, with only the local engine, the most similar verified images decide). modality=auto works the same way.
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
    if bp == "auto":  # a body part named in the question wins; otherwise the job looks at the image
        bp, detected_by = (body_part_from_text(q) or "auto"), "question"
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
