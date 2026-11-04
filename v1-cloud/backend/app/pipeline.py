"""One analysis, end to end: similar verified cases -> prompt -> Gemma 4 -> safety rules."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from . import gemma
from .body_parts import MODALITIES, body_part_label, modality_label
from .config import settings
from .imaging import PreparedImage, thumbnail_vector
from .prompts import build_user_prompt
from .references import ReferenceLibrary
from .safety import finalize


def resolve_auto(
    image: PreparedImage, body_part: str, modality: str, library: ReferenceLibrary, detected_by: str
) -> tuple[str, str, str]:
    """Fill in body_part / modality left as "auto": hosted Gemma 4 looks at the image first (seconds);
    without it, the body part is voted from the most similar verified images and the scan type defaults to X-ray."""
    if body_part != "auto" and modality != "auto":
        return body_part, modality, detected_by
    seen = None if settings.mock_gemma else gemma.identify(image.b64)
    if modality == "auto":
        modality = seen["modality"] if seen and seen.get("modality") in MODALITIES else "xray"
    if body_part == "auto":
        if seen and seen.get("is_medical_image", True):
            body_part, detected_by = seen["body_part"], "image"
        else:
            body_part = library.guess_body_part(modality, thumbnail_vector(image.png_bytes)) or "whole_body"
            detected_by = "image"
    return body_part, modality, detected_by


def run_analysis(
    *,
    image: PreparedImage,
    body_part: str,
    modality: str,
    question: str,
    library: ReferenceLibrary,
    detected_by: str = "user",
) -> dict:
    started = time.monotonic()
    body_part, modality, detected_by = resolve_auto(image, body_part, modality, library, detected_by)
    refs = library.retrieve(body_part, modality, settings.max_reference_cases, query_vector=thumbnail_vector(image.png_bytes))
    covered = library.conditions(body_part, modality)

    if settings.mock_gemma:
        raw, info = gemma.mock_answer(body_part, modality)
    else:
        attach = settings.send_reference_images and bool(refs)
        images = [library.image_b64(c) for c in refs] if attach else []
        images.append(image.b64)  # the patient's image always goes last
        prompt = build_user_prompt(
            body_part=body_part,
            modality=modality,
            question=question,
            references=refs,
            reference_images_attached=attach,
            covered_conditions=covered,
            sources=library.sources(body_part, modality),
        )
        raw, info = gemma.analyze(prompt, images)

    final = finalize(raw, body_part=body_part, modality=modality, references_used=len(refs), covered_conditions=covered)
    return {
        "id": uuid.uuid4().hex[:12],
        "body_part": body_part,
        "body_part_label": body_part_label(body_part),
        "modality": modality,
        "modality_label": modality_label(modality),
        "question": question,
        **final,
        "references_used": [c.public() for c in refs],
        "reference_library_size": sum(n for _, n in covered),
        "body_part_detected_by": detected_by,
        "image_info": {"format": image.source_format, "width": image.width, "height": image.height},
        "privacy_note": gemma.privacy_note(),
        "engine": info.get("engine", ""),
        "model": info.get("model", settings.gemma_model),
        "mock": settings.mock_gemma,
        "processing_seconds": round(time.monotonic() - started, 1),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
