"""Response shapes (these also drive the interactive docs at /docs)."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

Confidence = Literal["high", "moderate", "low"]


class Finding(BaseModel):
    finding: str
    location: str = ""
    confidence: Confidence
    explanation: str = ""


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
    summary: str
    findings: list[Finding]
    overall_confidence: Confidence
    confidence_reason: str = ""
    urgency: Literal["routine", "soon", "urgent", "emergency"]
    urgency_notice: Optional[str] = None
    reference_comparison: str = ""
    next_steps: list[str]
    questions_for_doctor: list[str]
    references_used: list[ReferenceUsed]
    safety_notes: list[str]
    disclaimer: str
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
