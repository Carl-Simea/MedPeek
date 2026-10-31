"""Response shapes (these also drive the interactive docs at /docs)."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

class Finding(BaseModel):
    finding: str
    location: str = ""
    explanation: str = ""
    supported_by_reference_data: bool


class Referral(BaseModel):
    specialist: str
    reason: str
    timeframe: str
    required: bool


class ReferenceUsed(BaseModel):
    case_id: str
    body_part: str
    modality: str
    label: str
    diagnosis: str = ""
    source: str = ""
    verified_by: str = ""
    added_at: str = ""


class AnalysisResult(BaseModel):
    id: str
    body_part: str
    body_part_label: str
    modality: str
    modality_label: str
    question: str
    is_medical_image: bool
    detected_body_part: str = ""
    image_quality: Literal["good", "limited", "unreadable"]
    primary_condition: str = ""
    condition_in_reference_data: bool
    coverage_note: str = ""
    summary: str
    findings: list[Finding]
    urgency: Literal["routine", "soon", "urgent", "emergency"]
    urgency_notice: Optional[str] = None
    referral: Referral
    reference_comparison: str = ""
    next_steps: list[str]
    questions_for_doctor: list[str]
    references_used: list[ReferenceUsed]
    reference_library_size: int = 0
    safety_notes: list[str]
    disclaimer: str
    ai_notice: str  # always "MedPeek is AI and can make mistakes." -- show it highlighted at the bottom of the report
    privacy_note: str = ""  # where the image goes (show it near the upload button and on reports)
    image_info: dict = {}  # format, width, height of the uploaded image (for the PDF report)
    body_part_detected_by: str = "user"  # "user", "question" or "image" (when body_part was "auto")
    engine: str = ""  # "google" (hosted Gemma 4), "ollama" (local Gemma 4) or "mock"
    model: str
    mock: bool = False
    processing_seconds: float
    created_at: str


class JobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "running", "done", "error"]
    position_in_queue: Optional[int] = None
    elapsed_seconds: float
    result: Optional[AnalysisResult] = None
    error: Optional[str] = None


class BodyPart(BaseModel):
    id: str
    label: str
    reference_cases: int


class Modality(BaseModel):
    id: str
    label: str


class Options(BaseModel):
    body_parts: list[BodyPart]
    modalities: list[Modality]
    privacy_note: str = ""
